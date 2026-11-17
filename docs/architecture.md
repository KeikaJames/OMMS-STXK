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
| -4 | 未到开放时间或未设置开放时间 | `not_open` |
| -6 | 名单已锁定或已到截止时间 | `locked` |

## 退选

`cancel_begin.lua` 为退选写入租约 `社团|x-令牌`，SQLite 删除报名行（只删除一行，`BEGIN IMMEDIATE` 内完成），`cancel_release.lua` 归还名额并清除镜像。`cx:{event}` 记录已完成的退选事件，重复请求直接返回首次结果。

## 调剂

报名结束、名单锁定后，管理员一次安排所有未报名的学生（`POST /api/admin/adjust_registrations`，`GET /api/admin/adjustment` 返回预览）。

1. 要求名单已锁定；取得全局维护标记，等在途的报名操作结束。
2. 在一个 SQLite 事务里读出未报名学生和各社团空余名额（只计开放中的社团），按年级、班级范围算出每名学生可去的社团。
3. 把可去社团相同的学生归为一组，用最大流求出每组安排到各社团的人数，安排总人数最多；同组学生的顺序随机打乱后依次分配。没有符合条件空余名额的学生保持未报名。
4. 写入报名（操作代际 `adjust-<批次>-<学生>`）和一条操作记录，记录里保存本批安排的学生与社团；同一 `request_id` 重复提交时返回第一次的结果。
5. 按 SQLite 整体重新发布 Redis 名额和报名镜像，再释放维护标记。

## 截止时间

管理员设置截止时间后，`close_at` 与开放时间一起发布到 Redis。`acquire.lua` 和 `cancel_begin.lua` 在同一个脚本里读取 Redis 时钟，到截止时间即返回 `-6`，报名与退选在这一刻停止。Python 后台线程每秒检查一次：到截止时间后把名单写为锁定、清除截止时间、记一条操作人为“系统”的操作记录，并保存快照。解除锁定时，已经过去的截止时间一并清除。

## 清空报名

`POST /api/admin/clear_registrations` 要求名单已锁定：取得全局维护标记，保存快照，在一个事务里删除全部报名、把各社团人数归零并写一条操作记录，然后整体重新发布 Redis 名额与报名镜像。同一 `request_id` 重复提交返回第一次的结果。

## 备份与恢复

- **自动快照**：名单锁定时，以及调剂、清空报名、清空学生、清空社团之前，用 SQLite 备份接口把数据库复制到 `BACKUP_DIR`，文件名为 `年月日-时分秒-原因.db`，保留最近 `BACKUP_KEEP` 份。危险操作的快照失败时，操作不执行。
- **完整备份**：`GET /api/admin/backup` 返回 zip，内含 `club_system.db`；`?name=` 下载指定的自动快照。
- **恢复**：`bash run.sh restore 文件` 先停止服务，再由 `main.py restore` 校验文件（`integrity_check` 和四张数据表），把当前数据库存为 `before-restore` 快照，复制备份中的数据库。随后删除本系统在 Redis 中的全部键，服务启动时按 SQLite 重新发布名额，原有会话失效。
- **管理员密码**：`bash run.sh reset-admin` 生成新密码、写入哈希、使管理员会话失效，并记入操作记录。

## 导出

`GET /api/export_workbook` 生成 Excel 报名表（只用标准库）：总表、每个社团一张表、未报名名单（有未报名学生时）。所有单元格按文本写入，学号前导零原样保留。“方式”一列由操作代际得出：`adjust-` 为调剂，`admin-` 为管理员安排，其余为自选。CSV 导出（报名总表、未报名名单、单个社团名单、学生账号表）使用同样的列，文件名为中文，同时附带 ASCII 备用名。

## 工作台流程

`GET /api/admin/summary` 返回学生数、报名数、社团数、开放报名社团的名额合计，以及最近一次导出（`registrations.exported`）、完整备份下载（`backup.downloaded`）和数据变化的时间。数据变化指报名、退选、管理员调整、锁定与解除、调剂、清空，以及学生和社团的增删改。工作台按这些数据给出各步骤的状态：导出与备份的时间晚于最近一次数据变化，才显示为完成。

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
| `open_at` / `locked` | 时间戳 / 标记 | 开放时间（未设置时不存在）/ 名单锁定 |
| `close_at` | 时间戳 | 截止时间（未设置时不存在） |
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
- `clubs.seat_sync_pending = 1`：管理端修改容量、调整学生报名、删除已报名的学生；
- 失效租约扫描：Python 与 Rust 各有一个后台任务，每秒处理修复队列，每 10 秒扫描一次 `inflight`。扫描先读成员、再读各自的租约，租约不符的成员再确认一次是否仍在集合里，仍在的才算失效。

`init` 缺失（Redis 重启或清空）时，后台线程在 `maint` 标记下按 SQLite 重建全部名额、开放时间和锁定状态。学生镜像与数据库有差异时，在下一次读取该学生时用 `mirror_set.lua` 单独更正。

管理端的容量修改、手工报名、移出和转移都先取得相关社团的栅栏，在同一 SQLite 事务里写数据和审计记录，再发布名额。发布未完成时返回 503 和 `Retry-After`，用同一 `request_id` 重试即可补发。

## 会话与登录

- 会话保存在 `sess:{token}`，Cookie 为 HttpOnly、SameSite=Strict，`COOKIE_SECURE=1` 时加 Secure。
- 修改密码、重置密码、删除学生时提升该账号的会话代际，旧会话随之失效。
- 口令使用 Argon2id，参数可配置。哈希集中在 `AUTH_CONCURRENCY` 个专用线程里按到达顺序计算，同时占用的内存不超过线程数 × `ARGON2_MEMORY_KIB`。
- 排队请求数达到 `AUTH_QUEUE`，或按最近耗时估算要等 8 秒以上时，新的登录请求立即得到 503 `login_queued`。服务器按单次校验的耗时给这些请求依次约定回来的时间，写在 `Retry-After` 里，回来时队列正好有空位。登录页按这个时间自动重试，最长 2 分钟。
- `login_fail.lua` 按"账号 + 设备"计数失败次数，达到 `LOGIN_MAX_FAILS`（默认 10）后该组合暂停登录。成功登录过的设备记为受信设备，学生在自己的设备上照常登录。

## 读流量

开放前后，大部分请求是轮询。

| 层 | 做法 |
|---|---|
| 浏览器 | 开放时每 2 秒、临近开放每 4 秒、其余时间每 10–15 秒请求一次 `/api/seats`；页面隐藏时暂停；带 `If-None-Match` |
| nginx | `/api/seats`、`/api/catalog` 缓存 1 秒，`proxy_cache_lock` 合并并发回源，更新期间返回旧副本 |
| 服务 | 名额快照缓存 500 毫秒，目录缓存 1 秒；报名或退选成功后立即刷新快照；ETag 相同时返回 304 |

名额快照包含每个社团的剩余名额、开放时间、锁定状态和目录版本。社团介绍等内容由 `/api/catalog` 提供，前端在目录版本变化时重新获取。学生页每次轮询后只更新内容有变化的卡片。

管理端工作台每 6 秒读取 `/api/admin/summary`（学生数和报名数）与社团列表。

## 边缘限流

| 区域 | 限额 |
|---|---|
| 每个会话 | 8 次/秒，突发 12–20 |
| 登录（每 IP） | 60 次/秒，突发 300 |
| 轮询（每 IP） | 2000 次/秒，适配学校出口 NAT |
| 全站 | 5000 次/秒 |

触发限流或上游停止响应时，nginx 返回带 `code`、`message` 的 JSON 和 `Retry-After: 1`；后端已经给出 `Retry-After` 时原样转发。前端按同一格式处理。

## 低配服务器

- 没有 cargo、Rust 版本过旧或首次编译失败时，`run.sh` 只运行 Python 服务，nginx 配置去掉 Rust 上游和到 Python 的长连接，Python 的线程只在处理请求时占用。也可以用 `OMMS_PYTHON_ONLY=1` 指定。接口与 Rust 版本一致。
- 编译 Rust 时按内存决定并行数，每个编译进程按 1 GB 计。
- Python 服务启动时只加载必需的模块：pypinyin 在导入学生时加载，并且只加载单字表。空闲内存约 62 MB。
- `ARGON2_MEMORY_KIB`、`AUTH_CONCURRENCY`、`MAX_HTTP_WORKERS`、`DB_POOL_SIZE` 按内存和核数下调。
- Rust 从连接池取 Redis 连接最多等 1 秒（`REDIS_POOL_WAIT_MS`），建立和校验连接按 `REDIS_TIMEOUT`；Redis 稍慢时请求继续等待。
- `SQLITE_SYNCHRONOUS=NORMAL` 配合 WAL；数据盘较慢时同样适用。
- `deploy/redis.conf` 关闭持久化、限制 256 MB 内存并使用 `noeviction`，名额以 SQLite 为准，重启后自动重建。
- `run.sh` 每次启动保留上一次的服务日志（`python.log.1`、`rust.log.1`），访问日志超过 64 MB 时换新文件。
- 前端为原生 HTML、CSS、JS；登录页与工作台插画为 WebP（单张 21–56 KB），社团默认插图和管理端图标为内嵌 SVG。页面使用纯色顶栏和小范围阴影，适合集成显卡。
- 管理端工作台比较服务器与管理员电脑的时钟，相差超过一分钟时提示校准。

`tests/stress_site.py` 用来在目标机器上测量登录高峰、抢课和轮询，见 [tests/README.md](../tests/README.md)。

## 兼容范围

| 依赖 | 版本 |
|---|---|
| Python | 3.7 及以上 |
| Rust | 1.88 及以上（edition 2024） |
| Redis | 3.2 及以上；Lua 脚本在 3.2–4.x 上调用 `redis.replicate_commands()` 后再写入 |
| SQLite | 3.8 及以上（WAL、触发器、部分索引）；Rust 服务内置 SQLite |
| nginx | 1.12 及以上；1.17.6 以前的版本由 `run.sh` 去掉访问日志里的 `limit_req`、`limit_conn` 字段 |
| 浏览器 | Chrome 57、Edge 15、Firefox 52、Safari 10.1 及以上：脚本使用 async/await，请求显式携带 Cookie，CSS 新特性都有退回写法。页面声明使用 WebKit 内核，360、QQ、搜狗浏览器默认进入极速模式；更旧的浏览器由 `web/js/legacy.js` 显示换浏览器的提示 |
