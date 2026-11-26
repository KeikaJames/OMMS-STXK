# 部署指南

本指南说明如何在 Mac 或 Linux 上启动服务、设置运行参数，并让学生从校园网访问。

## Mac：一键启动

在 Mac 上双击 `启动.command`，或在终端执行 `bash run.sh`。首次运行前安装依赖：

```bash
brew install redis nginx
python3 -m pip install redis argon2-cffi pypinyin Pillow
bash run.sh
```

启动脚本准备 Redis 和 nginx，构建并启动 Rust 热服务；Rust 编译环境不可用时，报名接口由 Python 服务处理。终端会显示学生访问地址、管理控制台地址、管理员账号和密码，并用两分钟有效的一次性链接打开控制台。设置 `OMMS_OPEN_BROWSER=0` 时只打印链接。

只运行 Python 服务：

```bash
OMMS_PYTHON_ONLY=1 bash run.sh
```

平滑重启可再次执行 `bash run.sh`；停止服务执行 `bash run.sh stop`。服务运行期间，脚本用 `caffeinate` 阻止 Mac 闲置睡眠。Mac 接通电源；MacBook 保持开盖。首次启动时允许 nginx 接受传入连接。

## Linux：systemd

安装 Python 3.7+、Redis 3.2+、nginx 1.12+ 和 Python 依赖；Rust 1.88+ 用于构建可选的热服务。以下单元按程序位于 `/opt/omms`、以 `omms` 用户运行编写。部署到其他目录时，按实际路径调整配置。

```bash
cp deploy/omms.env.example deploy/omms.env
cargo build --release --manifest-path club-hot/Cargo.toml
sudo cp deploy/systemd/omms-*.service /etc/systemd/system/
sudo systemctl enable --now omms-python omms-hot
```

nginx 使用仓库中的 `nginx.conf`。把 `__APP_ROOT__` 和 `__RUN_DIR__` 替换为实际路径。启用 HTTPS 时，在配置中加入证书路径；`run.sh` 中 `TLS_CERT` 和 `TLS_KEY` 的设置方式可作参考。

## 低资源部署与兼容性

运行时需要 Python 3.7+、Redis 3.2+ 和 nginx 1.12+；Rust 热服务可选。低内存服务器可把 `AUTH_CONCURRENCY` 设为 2，减少同时计算口令哈希的线程数。单个校验线程约使用 19 MB 工作区。

Rust 从源码编译时，单个编译进程峰值约 650 MB。`run.sh` 会根据可用内存决定编译并行数；也可以在同架构的机器上编译后，复制 `club-hot/target/release/club-hot`。

页面支持 IE11、Edge、Chrome、Firefox、Safari；360、QQ、搜狗浏览器使用极速模式。低资源机器的具体压测环境和数据见[测试说明](../tests/README.md)。

## 校园网与远程访问

每名学生首次打开登录页和报名页下载约 30 KB；使用照片卡片时每张再增加约 70–160 KB。3000 名学生使用插图时总下载量约 90 MB。建议服务器使用有线网口，并在正式开放前从学生机访问一次。

校外访问时配置 `TLS_CERT` 和 `TLS_KEY` 启用 HTTPS。经过内网穿透或反向代理时，设置 `REAL_IP_FROM` 与 `REAL_IP_HEADER`，让 nginx 按学生地址限流。学校时区通过 `APP_UTC_OFFSET` 设置，并校准服务器时钟。

## 运行参数

第一张表列出服务参数，第二张表列出 `run.sh` 参数。`run.sh` 从 `deploy/omms.env` 读取第二张表和以下变量；其余参数由 systemd 的 `EnvironmentFile` 注入服务。启动命令中的同名变量优先：`APP_UTC_OFFSET`、`BACKUP_KEEP`、`MAX_IMPORT_STUDENTS`、`SESSION_TTL`、`LOGIN_MAX_FAILS`、`AUTH_CONCURRENCY`、`MAX_HTTP_WORKERS`、`SEATS_CACHE_MS`、`LOG_LEVEL`、`PYTHON`。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DB_PATH` | `club_system.db` | SQLite 数据库路径 |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis 地址 |
| `REDIS_PREFIX` | `omms:` | Redis 键前缀 |
| `APP_UTC_OFFSET` | `+08:00` | 学校时区，开放时间按此解释 |
| `COOKIE_SECURE` | `0` | 使用 HTTPS 时设为 `1` |
| `SQLITE_SYNCHRONOUS` | `NORMAL` | SQLite 同步级别 |
| `BACKUP_DIR` | 数据库旁的 `backups` | 自动快照目录 |
| `BACKUP_KEEP` | `30` | 保留的自动快照份数 |
| `ARGON2_MEMORY_KIB` / `ARGON2_TIME_COST` / `ARGON2_PARALLELISM` | `19456 / 2 / 1` | 口令哈希参数 |
| `AUTH_CONCURRENCY` | 核数的一半，至少 4、至多 8；核数少于 4 时取核数 | 同时计算口令哈希的线程数 |
| `AUTH_QUEUE` | Python：线程数 × 16；Rust：线程数 × 64 | 排队等待口令校验的请求上限 |
| `SEAT_OP_TTL` | `30` | 报名租约秒数 |
| `MAX_IMPORT_STUDENTS` | `500` | 每批导入学生上限 |
| `SEATS_CACHE_MS` | `500` | 后端名额快照缓存毫秒数 |
| `REGISTER_WAIT_MS` | `8000` | Rust 报名请求等待结果的时长，超时返回 `pending` |
| `MAX_CONCURRENCY` | `1024` | Rust 同时处理的请求上限 |

| 启动变量 | 默认值 | 说明 |
|---|---|---|
| `HTTP_PORT` | `8080` | 访问端口；设为 80 时访问地址只写 IP |
| `OMMS_PYTHON_ONLY` | `0` | 设为 `1` 时只运行 Python 服务 |
| `OMMS_OPEN_BROWSER` | `1` | 启动后在本机打开控制台 |
| `TLS_CERT` / `TLS_KEY` | 空 | 证书与私钥路径；设置后在 `TLS_PORT` 提供 HTTPS，HTTP 跳转到 HTTPS，两个服务设 `COOKIE_SECURE=1` |
| `TLS_PORT` | `8443` | HTTPS 端口 |
| `REAL_IP_FROM` | 空 | 可信反向代理地址，多个地址以逗号分隔 |
| `REAL_IP_HEADER` | `X-Forwarded-For` | 学生地址所在请求头；Cloudflare Tunnel 使用 `CF-Connecting-IP` |

更多运行脚本参数见 `deploy/omms.env.example` 和 `run.sh`。

## 上线前

- 校准服务器时钟，并设置学校时区。
- 从学生机确认访问地址与登录页面。
- 校外访问时启用 HTTPS；经反向代理时配置可信代理地址。
- 锁定和调剂后下载完整备份，并保存到另一台电脑。
- 按[测试说明](../tests/README.md)在目标服务器执行相同场景。
