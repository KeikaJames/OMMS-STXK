# 测试

每个测试都在临时 SQLite 和随机回环端口上的独立 Redis 里运行，结束后清理进程、数据库和日志。仓库数据库和 6379 端口保持原样。

## 准备

```bash
pip install redis argon2-cffi pypinyin Pillow
cargo build --release --manifest-path club-hot/Cargo.toml
redis-server --version
nginx -v
```

`test_edge_security.py` 需要 Rust release 构建、`redis-server` 和 `nginx`；其余 Python 测试需要 `redis-server`。

## 回归测试

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
cargo test --manifest-path club-hot/Cargo.toml
```

| 文件 | 覆盖内容 |
|---|---|
| `test_python_races.py` | Python 服务：同一请求幂等、失效租约按社团修复、旧报名镜像清理、受信设备登录、名额快照 ETag、学校时区、100 个并发退选只归还一次、SQLite 容量触发器、会话吊销、管理员代为报名与调整、锁定后调剂（按年级范围安排人数最多、重复提交只执行一次、Redis 名额同步）、Excel 与 CSV 导出、重置密码、社团编辑与图片、操作记录 |
| `test_redis_recovery.py` | Lua 脚本：写入前开启效果复制、租约回滚与吸收模式、Redis 晚于服务启动、提交后故障保持名额占用 |
| `test_edge_security.py` | nginx + Python + Rust：响应头、Cookie 轮换限额、图片代理、栅栏只暂停对应社团、后台修复、登录失败计数隔离、两个服务的准入规则与会话格式一致、请求 ID 幂等 |
| `test_inflight_scan.py` | 后台扫描两次读取之间完成的报名不算失效，租约缺失或被替换的成员算失效 |
| `test_lifecycle.py` | 空数据库首次启动不开放报名，开放时间的设置、清除与重启后的同步；截止时间到点停止报名与退选并写入锁定；清空报名、自动快照、完整备份的下载与恢复、命令行重置管理员密码；修改学生信息、按班重置密码；工作台摘要的名额合计与导出、备份记录 |
| `test_low_end.py` | 口令队列的容量、顺序、超时与约定的等待时间，新旧 nginx 访问日志格式，按需加载 pypinyin 与 Pillow，繁忙响应的提示码 |
| `club-hot` 单元测试 | Cookie 解析、口令哈希与 Python 互通、口令队列、学校时区、ETag、提示文案覆盖全部返回码、报名镜像格式、令牌字符集 |

## 压测

`stress_registration.py` 启动独立的 Redis、SQLite 和 Rust 服务，为每名虚拟学生建立会话，然后同时发起一次报名。

```bash
python3 tests/stress_registration.py --seats 3 --users 12 --concurrency 6
python3 tests/stress_registration.py --seats 100 --users 1000 --concurrency 1000
python3 tests/stress_registration.py --seats 1000 --users 1000 --concurrency 1000
```

结束时输出结果分布、p50/p95/p99 延迟、SQLite 与 Redis 状态，并检查 16 项一致性条件：

- 每名学生得到一个最终结果，成功数等于名额，其余学生得到满员结果；
- 业务响应、HTTP 和传输层全部符合预期；
- 每名学生最多一条报名，报名数在容量以内，`current_students` 等于实际行数；
- Redis 库存等于容量减去报名数；
- 每条报名带操作代际，Redis 报名镜像与 SQLite 逐人一致；
- 成功响应的学生与 SQLite 中的学生一致；
- 全部响应结束后，在途成员和学生租约清空。

全部通过时退出码为 0，一致性条件失败为 1，环境准备失败为 2。

常用参数：

| 参数 | 说明 |
|---|---|
| `--rust-bin` | 指定要测的 Rust 可执行文件 |
| `--server-max-concurrency` | 传给服务的 `MAX_CONCURRENCY` |
| `--server-request-timeout` | 传给服务的 `REQUEST_TIMEOUT_SECS` |
| `--reservation-ttl` | 传给服务的 `SEAT_OP_TTL` |
| `--request-timeout` | 客户端单次请求超时 |

压测直接访问 Rust 服务，测量的是热路径本身。nginx 限流、TLS 和校园网络在目标服务器上另行测试。

## 整站压测

`stress_site.py` 在临时目录里启动 Redis、SQLite、Python 服务、可选的 Rust 服务和按仓库 `nginx.conf` 生成的 nginx，请求全部经过 nginx。每名虚拟学生带自己的 `X-Forwarded-For`，按前端的规则重试：

| 场景 | 内容 |
|---|---|
| `login` | 全部学生同时登录，503、429 和网络错误按 `Retry-After` 重试，最长 2 分钟 |
| `hot` | 全部学生同时报名同一个社团（`--hot-seats` 个名额），同一 `request_id` 重试 25 秒 |
| `spread` | 全部学生各自随机报名一个社团 |
| `poll` | 全部学生每 2–2.8 秒带 `If-None-Match` 刷新一次名额 |

```bash
python3 tests/stress_site.py login --backend python --users 1000
python3 tests/stress_site.py hot --backend rust --users 1000
python3 tests/stress_site.py poll --backend python --users 1000 --poll-seconds 30
```

报告包含结果分布、每名学生拿到最终结果的时间、各状态码的请求数、每个服务进程的 CPU 时间与内存；报名场景另外检查社团人数上限、重复报名、Redis 库存与遗留租约。全部学生完成且检查通过时退出码为 0。

`--wrap` 给每个服务端命令加前缀，用来模拟慢机器：macOS 上 `--wrap "taskpolicy -b"` 让服务只在能效核上以后台优先级运行，Linux 上可以用 `--wrap "taskset -c 0,1"` 限定核数。`--keep` 保留临时目录里的服务日志。

## 实测结果

Apple Silicon 笔记本，回环网络，每组 1000 名学生同时报名：

| 场景 | 吞吐 | p99 |
|---|---|---|
| 100 个名额，900 人得到满员 | 5828 次/秒 | 109 ms |
| 1000 个名额，全部写库 | 3369 次/秒 | 198 ms |

`stress_site.py`，同一台笔记本的 4 个能效核、后台优先级（单核速度约为平时的六分之一），1000 名学生：

| 场景 | 只运行 Python | 加上 Rust 热服务 |
|---|---|---|
| `login`：全部登上 | 25.8 秒，2742 次请求 | 17.3 秒，1714 次请求 |
| `hot`：100 个名额，全部拿到结果 | 2.6 秒 | 0.2 秒 |
| `spread`：895 人报上，105 人满员 | 3.3 秒 | 0.7 秒 |
| `poll`：30 秒 12572 次刷新 | nginx 1.8 秒 CPU，后端 0.05 秒 | nginx 1.9 秒 CPU，后端 0.03 秒 |
