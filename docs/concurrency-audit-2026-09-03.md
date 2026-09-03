# OMMS-STXK 高并发审计与验证记录

日期：2026-09-03（Asia/Shanghai）
起始版本：`86b7bf4`
范围：Python 回退服务、Rust 热路径、Redis/SQLite 一致性、Nginx 边缘、启动流程、前端轮询与隔离压测。

## 结论

修复前只能证明“单次 Redis Lua 扣减是原子的”，不能证明完整报名流程不会超卖。已经确认的破坏路径包括：Python 双退选重复回补、在线启动重建覆盖在途扣减、库存键缺失时错误重算、新社团初始化覆盖并发扣减，以及旧 confirm/release 修改新一代状态。

修复后，SQLite 增加了跨 Python/Rust 共用的最终容量触发器；Redis 报名状态使用 `club_id|operation_id` 代际并由 CAS Lua 确认、回滚和退选；Rust 在扣 Redis 之前先取得 finalizer 槽位，扣减后的任务不会随 HTTP 超时一起取消。隔离短测支持“几百至一千名学生同秒发起报名”的校园场景，但结果不是生产 SLA，仍需在实际服务器、TLS、真实校园 NAT 和混合轮询流量下复测。

## 已确认问题与处理

| 问题 | 原后果 | 当前处理 |
|---|---|---|
| Python 并发退选先查后开事务，且不检查删除行数 | 同一座位可被 `INCR` 多次并最终超卖 | `BEGIN IMMEDIATE` 后查询；只允许删除一行；退选按 registration operation ID 幂等回补 |
| 应用启动无条件从 SQLite 覆盖 live Redis | 在途扣减被恢复成可卖库存 | Python/Rust 共用 maintenance fence 与 `seat:op`；仅在无在途操作且仍持有租约时原子发布一致快照 |
| stock 键缺失时按 DB 即时猜测容量 | 独立存在的 reservation 被忽略，可超卖 | 热请求直接 503 并报警，不做请求内恢复 |
| SQLite 没有社团容量约束 | 任一 Redis 漂移都能落成第 `max+1` 人 | `registrations_capacity_guard` 在 DB 层拒绝超容量 INSERT |
| confirm/release 无代际、无条件删除三把键 | 延迟旧请求可删新 reservation/stureg 或重复加库存 | reservation、registration 都带 operation ID；三类 Lua 各自 CAS/幂等 |
| Redis 扣减后再排单 SQLite writer，HTTP timeout 会取消 handler | 503 后仍提交、少卖或确认态缺失 | 取得槽位后把 acquire、DB 与 Redis 最终化整体放进 detached task；命令超时可按同 operation 重试；关机等待 drain |
| Argon2 在 Tokio reactor 上同步执行 | 登录峰值阻塞事件循环并放大内存 | 有界 `AUTH_CONCURRENCY` + `spawn_blocking`；Python 同样限制并发且不再持有 DB 连接做哈希 |
| Python 一连接一线程且无上限 | Rust 故障时线程/内存膨胀 | `MAX_HTTP_WORKERS` 默认 128，饱和立即 503 + `Retry-After` |
| Nginx 对校园公网 IP 统一 30 r/s、50 连接 | 约几十名正常学生即可互相误伤 | 合法形状 token 按 session；畸形/空 token 回落 IP；另加 5000 r/s 全站总量桶 |
| 前端每 4–6.5 秒固定请求时间、社团、个人信息三个接口 | 数百学生持续放大 DB/Redis 读流量 | 稳态只轮询社团；时间 30 秒、个人信息 60 秒低频刷新，并在窗口 focus/写后对账 |

## 隔离性能结果

测试均使用临时 SQLite、随机回环端口的独立 Redis、每名学生独立 session；没有使用仓库数据库或默认 Redis `6379`。

环境摘要：Darwin ARM64；Python 3.14.0；Rust 1.94.0；Redis 8.8.0；Nginx 1.31.1。数据走 loopback，不包含校园网络、TLS、反向代理或磁盘耐久化成本。

| 实现/阶段 | 名额 | 学生请求 | 并发 | 结果 | 完成吞吐 | p99 |
|---|---:|---:|---:|---|---:|---:|
| 修复前 Rust，全部可成功 | 1000 | 1000 | 500 | 1000 成功 | 2170.3 req/s | 387.5 ms |
| 修复后 Rust，热点争抢（最终收尾三次） | 100 | 1000 | 300 | 三次均为 100 成功、900 满员、0 HTTP/传输错误 | 中位 3077.5 req/s | 中位 119.8 ms；范围 107.5–121.4 ms |
| 修复后 Rust，全部真实写入（最终收尾三次） | 1000 | 1000 | 300 | 三次均为 1000 成功、0 HTTP/传输错误 | 中位 1817.3 req/s | 中位 165.1 ms；范围 152.0–167.2 ms |
| 修复后 Rust，热点争抢（默认 1024 上限） | 100 | 1000 | 1000 | 三次均为 100 成功、900 满员、0 HTTP/传输错误 | 中位 3064.1 req/s | 中位 237.3 ms；范围 235.5–240.0 ms |
| 修复后 Rust，全部真实写入（默认 1024 上限） | 1000 | 1000 | 1000 | 三次均为 1000 成功、0 HTTP/传输错误 | 中位 1780.7 req/s | 中位 470.2 ms；范围 450.2–556.6 ms |
| 修复后 Python 回退，热点争抢 | 100 | 1000 | 100 | 100 成功、900 满员、0 HTTP/传输错误 | 2006.9 req/s | 140.0 ms |

这些行的成功写比例和并发不同，不能把吞吐数字直接当作前后性能提升百分比。热点社团满员后，大多数请求由 Redis 快速拒绝，因此会比“所有请求都必须写 SQLite”更快。

开发过程中的一个中间版本曾观测到一次约 1.5 秒 p99 抖动，但没有错误或状态漂移；随后继续修正恢复路径。上表只统计最终代码的收尾重复结果，仍不应据此承诺生产尾延迟。

调整前上限为 512 时，1000 并发测试有 488 个请求被明确以 503 丢弃，正好符合全局并发层；库存状态仍完全正确。将默认值调到 1024 后，同样的 1000 并发热点与全写场景均无 503。慢依赖下另有 2 秒报名队列等待上限，避免 1024 个请求无限滞留。

修复后 Rust 的 16 项检查全部通过：

- 成功数严格等于可用名额；
- 其余用户全部得到明确满员结果；
- 无 HTTP 或传输错误；
- 每名学生最多一条 SQLite registration；
- DB 报名数不超过 `max_students`；
- `current_students` 等于 DB 实际行数；
- Redis stock 等于 `max - DB count`；
- 每条 registration 都有 operation ID；
- Redis stureg 与 SQLite 的 `club_id|operation_id` 逐学生精确一致；
- 所有响应结束后无残留 reservation；
- 所有响应结束后无残留 `seat:op` operation lease。

## 故障与竞态回归

### HTTP 超时后的最终化

测试把 Rust `REQUEST_TIMEOUT_SECS` 临时设为 1 秒，并由另一个 SQLite 连接持有写锁 5 秒。客户端在约 1.013 秒收到 503；此时 Redis 中同时存在同 operation 的 reservation 与 `seat:op`，证明 acquire 已归 detached task 管理。写锁释放后，finalizer 继续完成：

- SQLite registration：1；
- `current_students`：1；
- Redis stock：9（初始 10）；
- stureg：与 SQLite operation ID 精确一致；
- reservation：0；
- `seat:op`：0；
- `/readyz`：ready。

这证明超时不再让操作停在半完成状态。客户端仍无法从这次 503 单独判断最终结果；见“剩余边界”。

### 崩溃遗留 operation lease 的自动恢复

隔离 Redis 人为放入一个“已扣 1 个库存、DB 无记录”的 orphan reservation 与 `seat:op`，租约设为 5 秒，再启动 Rust。启动阶段检测到在途 lease 后没有覆盖 live Redis；租约到期后，后台 worker 取得 maintenance fence 并自动对账。观测状态从 `stock=9` 恢复为 `stock=10`，reservation/op/stureg 均为空，日志明确记录 deferred reconciliation 完成。

### Redis 晚于 Rust 恢复

另一轮让 Rust 在 Redis 完全未监听时先启动，并把 `REDIS_POOL_SIZE` 压到 1。Rust先以 degraded 状态监听；随后启动空 Redis，后台 worker 自动发布 `K_INIT`、stock 和 SQLite 中的 `open_at`。最终 `/readyz=200`、`check_registration_time.can_register=true`，真实报名成功且 stock/stureg/resv/op 全部一致。另预置 active `seat:op` 验证单连接池路径，maintenance marker 能立即用当前连接清理，没有卡住 300 秒。

### 100 个并发退选

同一学生、同一条 registration 同时发送 100 个退选请求：

- 仅 1 个响应成功；
- 99 个明确返回未报名；
- DB registration 与 `current_students` 都从 1 变 0；
- Redis stock 只从 0 恢复到 1；
- stureg/reservation 均清除。

### Redis 库存被故意放大

SQLite 容量为 3，测试故意把 Redis stock 改成 20，再让 20 名学生并发报名。最终 SQLite 仍严格只有 3 条 registration，证明容量 trigger 能阻止 Redis 漂移落成真实超卖。readiness 会把明显的 overstock 标为 `stock-drift`；运维仍需修复 Redis 数值，不能把 DB 拒绝当作长期正常路径。

## 如何复现

```bash
cargo build --release --manifest-path club-hot/Cargo.toml
python3 tests/stress_registration.py --seats 100 --users 1000 --concurrency 300
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_python_races.py' -v
```

代码质量与配置校验：

```bash
cargo fmt --manifest-path club-hot/Cargo.toml --all -- --check
cargo clippy --all-targets --manifest-path club-hot/Cargo.toml -- -D warnings
cargo test --all-targets --manifest-path club-hot/Cargo.toml
ruff check main.py tests
shellcheck run.sh
nginx -t -c /absolute/path/to/nginx.conf
```

## 剩余边界

1. 进程若在 Redis 已扣减后被 `SIGKILL` 或主机掉电，内存 finalizer 无法继续；服务恢复后会等待孤儿 operation lease 到期，再做 maintenance-fenced 粗粒度对账。若要求保存每一步结果、跨 Redis 数据丢失精确重放，仍需 SQLite outbox/持久队列。
2. HTTP 503/客户端断线后，报名可能最终成功。生产版应接受客户端 idempotency key，并提供按 request ID 查询最终结果的接口，消除用户侧歧义。
3. 默认 `REGISTER_CONCURRENCY=1` 与单 SQLite writer 对齐，保护正确性和尾延迟。提高它不会增加 SQLite 的真实并行写能力；持续写吞吐不足时，应换支持多写并发的数据库或引入持久队列。
4. 多个 Rust 实例各自有本地 finalizer semaphore；SQLite trigger 仍能保证不超卖，但总排队、锁竞争和尾延迟需要单独压测。多机部署还需要共享幂等/outbox 协议。
5. Redis 应使用专用实例或独立 DB/namespace、`noeviction`、受控持久化和内存告警。关键 stock 键缺失时系统会拒绝报名，不会冒险在线猜测。
6. 本轮没有把 Nginx、TLS、真实共享 NAT 和浏览器轮询混合进 1000 用户压测；上线前必须在目标主机补做这一层，不能把 Rust loopback 数字直接当整站容量。
