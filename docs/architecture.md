# 设计说明

本文说明报名、退选、名额修复、会话和读流量的实现方式，以及每一部分对应的代码位置。

## 组件

| 组件 | 职责 | 代码 |
|---|---|---|
| nginx | 静态资源、限流、名额快照缓存、按路径分发 | `nginx.conf` |
| Rust 热服务 `club-hot`（:2002） | 学生登录、报名、退选、目录、名额快照 | `club-hot/src` |
| Python 服务（:2001） | 管理端全部接口；热接口的备用实现；后台修复 | `main.py` |
| Redis | 名额、报名镜像、在途租约、会话、登录计数 | `shared/lua` |
| SQLite（WAL） | 学生、社团、报名、设置、审计记录 | 建表语句在 `main.py` 与 `club-hot/src/db.rs` |

两个服务读取同一组 Lua 脚本和 `shared/messages.json`，共用 Redis 键、会话格式和返回码。nginx 的 `club_hot` 上游把 Rust 设为主节点、Python 设为 `backup`，Rust 停止时热接口由 Python 接管。

## 一次报名

1. 前端为这次点击生成 `request_id`，`POST /api/register_club`。排队、超时或 503 时用同一个 `request_id` 重试。
2. nginx 按会话令牌限速（每会话 8 次/秒），转发到 Rust。
3. Rust 用会话里的年级、班级和内存中的社团目录判断资格，目录随社团修订号刷新。
4. `acquire.lua` 在一次调用里依次检查：
   - 同一请求是否已在处理（返回 2 或 3，进入结果查询）；
   - 名单锁定、全局维护、社团栅栏、开放时间；
   - 学生是否已有报名或其他操作；
   - 名额是否大于 0。

   全部通过后扣减 `stock:{c}`，写入学生租约 `op:{s}` = `社团|操作代际|随机数`（有效期 `SEAT_OP_TTL`），并把 `学生|租约` 加入 `inflight:{c}`。
5. 抢到名额的请求取得写入许可（`REGISTER_CONCURRENCY`，默认 1），在 SQLite 事务里插入报名并写审计记录。触发器 `registrations_capacity_guard` 在库内再次检查容量。
6. `confirm.lua` 校验租约仍属于本次操作，写入报名镜像 `reg:{s}` = `社团|操作代际`，移除租约和在途成员。
7. 写库时触发器判定满员，`rollback.lua` 以吸收模式结束租约，名额保持扣减状态，社团进入修复队列，由修复流程按 SQLite 重新发布名额。
8. Rust 在 `REGISTER_WAIT_MS` 内等待结果，超出时返回 202 `pending`。写库任务独立于 HTTP 请求继续执行，前端重试同一请求即可拿到最终结果。

操作代际 `sid-request_id` 写入 `registrations.operation_id`。结果待确认时，服务按操作代际查库：查到报名则确认，否则撤销租约并归还名额。

### `acquire.lua` 返回码

| 返回 | 含义 | 对外结果 |
|---|---|---|
| 1 | 抢到名额 | 继续写库 |
| 2 | 同一请求的重放 | 按操作代际查库 |
| 3 | 同一请求的另一次尝试正在处理 | 202 `pending` |
| 0 | 满员 | `full` |
| -1 | 已有报名或其他操作 | `registered_other` / `registered_already` |
| -2 | 名额未初始化 | 503 `syncing`，触发重建 |
| -3 | 维护中或社团修复中 | 503 `syncing` |
| -4 | 未到开放时间 | `not_open` |
| -6 | 名单已锁定 | `locked` |

## 退选

`cancel_begin.lua` 为退选写入租约 `社团|x-令牌`，SQLite 删除报名行（只删除一行，`BEGIN IMMEDIATE` 内完成），`cancel_release.lua` 归还名额并清除镜像。`cx:{event}` 记录已完成的退选事件，重复请求直接返回首次结果。

## Redis 键

所有键带 `REDIS_PREFIX` 前缀（默认 `omms:`）。

| 键 | 值 | 用途 |
|---|---|---|
| `stock:{c}` | 整数 | 社团剩余名额 |
| `reg:{s}` | `社团|操作代际` | 学生报名镜像 |
| `op:{s}` | `社团|操作代际|随机数` | 学生在途租约，带有效期 |
| `inflight:{c}` | 集合，成员 `学生|租约` | 社团在途操作 |
| `fence:{c}` | 标记 | 社团修复栅栏 |
| `maint` / `init` | 标记 | 全局维护 / 名额已初始化 |
| `open_at` / `locked` | 时间戳 / 标记 | 开放时间 / 名单锁定 |
| `repair` | 集合 | 待修复社团 |
| `rb:{lease}` / `cx:{event}` | 标记 | 回滚与退选的幂等记录 |
| `sess:{token}` | 会话 JSON | 登录会话，有效期 `SESSION_TTL` |
| `sessgen:epoch:{role}` / `sessgen:ver:{role}:{id}` | 整数 | 会话代际，用于整体或按账号吊销 |
| `fail:{key}` / `trust:{role}:{user}:{device}` | 计数 / 标记 | 登录失败计数 / 受信设备 |

## 名额修复

修复以社团为单位进行：

1. 设置 `fence:{c}`，新的抢位请求得到 `-3`，前端显示"名额更新中"并自动重试。
2. 等待 `inflight:{c}` 里仍有效的操作结束，最长 5 秒（`FENCE_WAIT`）。
3. `fence_publish.lua` 按 SQLite 写入 `stock:{c}` = 容量 − 已报名人数，清理失效租约，移除栅栏。

修复来源：

- `repair` 集合：吸收模式回滚、确认中断、管理端调整；
- `clubs.seat_sync_pending = 1`：管理端修改容量、手工调剂；
- 失效租约扫描：Python 与 Rust 各有一个后台任务，每秒处理修复队列，每 10 秒扫描一次 `inflight`。

`init` 缺失（Redis 重启或清空）时，后台线程在 `maint` 标记下按 SQLite 重建全部名额、开放时间和锁定状态。学生镜像与数据库有差异时，在下一次读取该学生时用 `mirror_set.lua` 单独更正。

管理端的容量修改、手工报名、移出和转移都先取得相关社团的栅栏，在同一 SQLite 事务里写数据和审计记录，再发布名额。发布未完成时返回 503 和 `Retry-After`，用同一 `request_id` 重试即可补发。

## 会话与登录

- 会话保存在 `sess:{token}`，Cookie 为 HttpOnly、SameSite=Strict，`COOKIE_SECURE=1` 时加 Secure。
- 修改密码、重置密码、删除学生时提升该账号的会话代际，旧会话随之失效。
- 口令使用 Argon2id，参数可配置。`AUTH_CONCURRENCY` 限制同时进行的哈希计算，登录高峰时请求排队，CPU 和内存保持在固定范围。
- `login_fail.lua` 按"账号 + 设备"计数失败次数，达到 `LOGIN_MAX_FAILS`（默认 10）后该组合暂停登录。成功登录过的设备记为受信设备，学生在自己的设备上照常登录。

## 读流量

开放前后，大部分请求是轮询。

| 层 | 做法 |
|---|---|
| 浏览器 | 开放时每 2 秒、临近开放每 4 秒、其余时间每 10–15 秒请求一次 `/api/seats`；页面隐藏时暂停；带 `If-None-Match` |
| nginx | `/api/seats`、`/api/catalog` 缓存 1 秒，`proxy_cache_lock` 合并并发回源，更新期间返回旧副本 |
| 服务 | 名额快照缓存 500 毫秒，目录缓存 1 秒；报名或退选成功后立即刷新快照；ETag 相同时返回 304 |

名额快照包含每个社团的剩余名额、开放时间、锁定状态和目录版本。社团介绍等内容由 `/api/catalog` 提供，前端在目录版本变化时重新获取。

## 边缘限流

| 区域 | 限额 |
|---|---|
| 每个会话 | 8 次/秒，突发 12–20 |
| 登录（每 IP） | 60 次/秒，突发 300 |
| 轮询（每 IP） | 2000 次/秒，适配学校出口 NAT |
| 全站 | 5000 次/秒 |

触发限流或上游停止响应时，nginx 返回带 `code`、`message` 的 JSON 和 `Retry-After`，前端按同一格式处理。

## 低配服务器

- 只装 Python 的服务器使用 `OMMS_PYTHON_ONLY=1` 或直接运行 `main.py`，接口与 Rust 版本一致。
- `ARGON2_MEMORY_KIB`、`AUTH_CONCURRENCY`、`MAX_HTTP_WORKERS`、`DB_POOL_SIZE` 按内存和核数下调。
- `SQLITE_SYNCHRONOUS=NORMAL` 配合 WAL；数据盘较慢时同样适用。
- `deploy/redis.conf` 关闭持久化、限制 256 MB 内存并使用 `noeviction`，名额以 SQLite 为准，重启后自动重建。
- 前端为原生 HTML、CSS、JS，插画为 WebP（单张 5–56 KB）。

## 兼容范围

| 依赖 | 版本 |
|---|---|
| Python | 3.7 及以上 |
| Rust | 1.88 及以上（edition 2024） |
| Redis | 3.2 及以上；Lua 脚本在 3.2–4.x 上调用 `redis.replicate_commands()` 后再写入 |
| SQLite | 3.8 及以上（WAL、触发器、部分索引）；Rust 服务内置 SQLite |
| 浏览器 | 脚本为 ES2018，样式使用 CSS 自定义属性；Chrome 61、Safari 11.1、Firefox 60 及以上满足要求 |
