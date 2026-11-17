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
| `test_python_races.py` | Python 服务：同一请求幂等、失效租约按社团修复、旧报名镜像清理、受信设备登录、名额快照 ETag、学校时区、100 个并发退选只归还一次、SQLite 容量触发器、会话吊销、管理端调剂、重置密码、社团编辑与图片、操作记录 |
| `test_redis_recovery.py` | Lua 脚本：写入前开启效果复制、租约回滚与吸收模式、Redis 晚于服务启动、提交后故障保持名额占用 |
| `test_edge_security.py` | nginx + Python + Rust：响应头、Cookie 轮换限额、图片代理、栅栏只暂停对应社团、后台修复、登录失败计数隔离、两个服务的准入规则与会话格式一致、请求 ID 幂等 |
| `club-hot` 单元测试 | Cookie 解析、口令哈希与 Python 互通、学校时区、ETag、提示文案覆盖全部返回码、报名镜像格式、令牌字符集 |

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

## 实测结果

Apple Silicon 笔记本，回环网络，每组 1000 名学生同时报名：

| 场景 | 吞吐 | p99 |
|---|---|---|
| 100 个名额，900 人得到满员 | 5828 次/秒 | 109 ms |
| 1000 个名额，全部写库 | 3369 次/秒 | 198 ms |
