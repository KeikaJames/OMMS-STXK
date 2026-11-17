<div align="center">

# 社团选课 · OMMS-STXK

面向中学社团选课的报名系统：到点开放、全员抢报、锁定后统一调剂、导出名单。

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.7%2B-3776AB?logo=python&logoColor=white)
![Rust](https://img.shields.io/badge/Rust-1.88%2B-CE422B?logo=rust&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-3.2%2B-DC382D?logo=redis&logoColor=white)

<sub>为 <b>鄂尔多斯市实验中学</b> 而做</sub>

<img src="docs/login.jpg" alt="登录页" width="760">

</div>

## 功能

**学生端**

- 倒计时按服务器时钟计算，到开放时间后报名按钮可用。
- 全部社团在一屏内列出：照片、名称、时间地点、剩余名额和报名按钮。手机上为左图右文。
- 报名与退选在排队或确认期间按同一请求自动重试，结果以服务器为准。

**管理端**

- 从 Excel 粘贴名单，识别姓名、班级、学号列，分批创建账号并下载账号密码表。
- 编辑社团名称、名额、简介、老师、时间地点、年级与班级范围和照片；没有上传照片时，3D 打印、传媒、木工等社团显示对应插图。
- 设置开放时间；报名结束后锁定名单。
- 调剂：锁定后把未报名的学生随机安排到有空余名额的社团，遵守年级和班级范围，安排人数取最大值。
- 下载 Excel 报名表：总表、各社团名单、未报名名单，“方式”一列区分自选、调剂和管理员安排。
- 在学生名单里为单个学生代为报名、调整社团、移出社团或重置密码。
- 清空报名：保留学生账号和社团，用于演练后重新开放或下一轮选课。
- 备份：下载完整备份（数据库和社团照片）；名单锁定时，以及调剂、清空报名、清空学生、清空社团之前，自动保存数据库快照。
- 操作记录分别列出管理操作和学生报名。

**并发与运维**

- 抢位在 Redis 里一步完成；SQLite 容量触发器为每个社团设定最终上限。
- 名额修复以社团为单位进行，其他社团照常报名；Redis 重启后由 SQLite 自动重建。
- 名额快照带 ETag，由 nginx 每秒生成一次，全校轮询集中在一份缓存上。
- 登录高峰时口令校验按到达顺序排队，浏览器按服务器约定的时间自动重试，直到登录成功。
- 一条命令启动整套服务；没有 Rust 编译环境的旧电脑自动只运行 Python 服务。

<div align="center">
<img src="docs/dashboard.jpg" alt="学生选课页" width="560">&nbsp;&nbsp;<img src="docs/mobile.jpg" alt="手机上的选课页" width="180">
</div>

## 快速开始

### 开发机：Python + Redis

```bash
pip install redis argon2-cffi pypinyin Pillow
redis-server --port 6379 --save "" --daemonize yes
python3 main.py                     # 打开 http://127.0.0.1:2001
```

`pypinyin` 用于按姓名生成拼音账号，`Pillow` 用于处理社团卡片图片。

首次启动时，日志里输出一次管理员账号和初始密码，登录后请在"设置"里修改。

### 完整部署：nginx + Rust 热路径 + Python 管理服务

```bash
bash run.sh                         # 打开 http://127.0.0.1:8080
```

`run.sh` 适用于 macOS 和 Linux，依次完成以下步骤：

1. 编译 Rust 热服务，启动专用 Redis（`deploy/redis.conf`），生成并校验 nginx 配置。
2. 平滑切换：先等在途报名完成，再启动 Python（:2001）、Rust（:2002）和 nginx（:8080）。

运行文件都放在 `run/` 目录下。再次执行即为平滑重启，`bash run.sh stop` 停止。

```bash
bash run.sh reset-admin             # 重置管理员密码，新密码只输出一次
bash run.sh restore 备份文件         # 停止服务，从完整备份（.zip）或自动快照（.db）恢复，再启动
```

恢复前，当前数据库先存为一份快照；恢复后学生和管理员需重新登录。

没有 cargo、Rust 版本低于 1.88 或首次编译失败时，`run.sh` 自动改用只运行 Python 的模式。也可以手动指定：

```bash
OMMS_PYTHON_ONLY=1 bash run.sh
```

### Linux 服务器：systemd

单元文件按仓库位于 `/opt/omms`、以 `omms` 用户运行编写，部署到其他路径时同步修改。

```bash
cp deploy/omms.env.example deploy/omms.env
cargo build --release --manifest-path club-hot/Cargo.toml
sudo cp deploy/systemd/omms-*.service /etc/systemd/system/
sudo systemctl enable --now omms-python omms-hot
```

nginx 使用仓库里的 `nginx.conf`：把 `__APP_ROOT__`、`__RUN_DIR__` 替换为实际路径，再加上 443 端口的 TLS 配置。

## 在旧电脑上运行

学校的旧台式机可以直接做服务器：装好 Python 3.7+、nginx 和 Redis 3.2+ 即可，Rust 热服务可选。

下表在 Apple M4 Max 的 4 个能效核上以后台优先级运行全部服务端进程测得，单核速度约为平时的六分之一；浏览器端按前端的重试规则模拟 1000 名学生，同时操作。

| 场景 | 只运行 Python | 加上 Rust 热服务 |
|---|---|---|
| 同时登录，全部登上 | 26 秒 | 17 秒 |
| 抢 100 个名额，全部拿到结果 | 2.6 秒 | 0.2 秒 |
| 各自报名不同社团，全部写库 | 3.3 秒 | 0.7 秒 |
| 开放期间每人每 2–2.8 秒刷新名额 | nginx 占单核 6%，后端基本空闲 | 同左 |

| 进程 | 空闲内存 | 1000 人登录后 |
|---|---|---|
| Python 服务 | 62 MB | 151 MB |
| Rust 热服务 | 6 MB | 126 MB |
| nginx（2 个工作进程） | 20 MB | 32 MB |
| Redis | 7 MB | 10 MB |

登录后增加的内存是口令哈希的工作区：每个校验线程 19 MB，线程数为 `AUTH_CONCURRENCY`。

- **登录**：口令校验是最耗 CPU 的环节，这类机器上每次约 50–70 毫秒。请学生提前几分钟登录；人多时浏览器自动排队，最长等 2 分钟。
- **时钟**：报名按服务器时钟开放。服务器与管理员电脑相差超过一分钟时，工作台会提示校准（Linux：`timedatectl set-ntp true`）。
- **内存 2 GB 以下**：把 `AUTH_CONCURRENCY` 设为 2，口令校验最多占用 38 MB。
- **Rust**：同样条件下从零编译约 3 分钟，单个编译进程峰值约 650 MB，`run.sh` 按内存决定并行数。也可以在同架构的另一台 Linux 机器上编译，把 `club-hot/target/release/club-hot` 拷过来。
- **nginx**：1.12 及以上；`run.sh` 在 1.17.6 以前的版本上自动调整访问日志格式。
- **浏览器**：Chrome 57、Edge 15、Firefox 52、Safari 10.1 及以上。360、QQ、搜狗浏览器默认使用极速模式，更旧的浏览器会看到换浏览器的提示。

在目标电脑上可以用同样的方法测一次：

```bash
python3 tests/stress_site.py login --backend python --users 1000
python3 tests/stress_site.py hot --backend python --users 1000
```

## 使用流程

**管理员**（登录页选择"管理员"）

1. **导入学生**：粘贴名单，核对识别出的列后创建账号，在“账号发放”中下载账号密码表并发给学生。
2. **添加社团**：批量粘贴“名称，人数上限”，再在社团卡片中补充简介、时间地点、年级范围和照片。各社团人数上限之和不少于学生人数时，调剂后每名学生都有社团。
3. **设置开放时间**：按学校时间填写，学生端显示倒计时。新安装时不设开放时间，学生页显示“开放时间待定”。
4. **报名期间**：在“报名数据”中查看各社团进度。
5. **锁定**：报名结束后在“设置 → 开放与锁定”中锁定名单，报名与退选停止，系统同时保存一份数据库快照。
6. **调剂**：在“报名数据 → 调剂”中查看未报名人数和各社团空余名额，确认后一次安排。
7. **导出**：在“报名数据 → 导出”中下载报名表。
8. **备份**：在“设置 → 备份”中下载完整备份，保存到另一台电脑。

**演练**：正式开放前可以先开放一次演练。演练结束后锁定名单，在“报名数据 → 清空报名”中清空，再设置正式开放时间并解除锁定。

**学生**：用发放的账号在开放前登录；到开放时间后点“报名”。每人限报一个社团，锁定前可以退选后改报。锁定时未报名的学生由学校统一调剂。

## 工作原理

```
浏览器 ──▶ nginx :8080 ──┬─ 静态资源、名额快照缓存（1 秒）
                         ├─ 热接口 ──▶ Rust :2002（备用：Python :2001）
                         └─ 管理接口 ──▶ Python :2001
                                  │
                       Redis（名额、会话） + SQLite（报名记录、审计）
```

- **抢位一步完成**：`shared/lua/acquire.lua` 在一个 Redis 脚本里依次检查锁定、维护、开放时间、是否已报名，然后扣减名额、登记租约和在途成员。
- **两层上限**：Redis 负责快速放行；SQLite 触发器 `registrations_capacity_guard` 让落库人数始终在容量以内，`UNIQUE(student_id)` 保证一人一社。
- **只有抢到名额的请求写库**：单写者队列按顺序落库，确认后写入报名镜像；落库结果待确认时按操作代际查库，再决定确认或撤销。
- **按社团修复**：每个社团有在途集合和栅栏。修复时只暂停这个社团，等在途操作完成后，以 SQLite 为准发布库存。失效租约、确认中断、管理端调整都会进入后台修复队列。
- **幂等请求**：前端为每次报名生成 `request_id`。服务端把它和学生 ID 组成操作代际，重试同一请求总能得到同一结果。
- **两个服务，一份协议**：Python 和 Rust 读取同一组 Lua 脚本（`shared/lua`）和提示文案（`shared/messages.json`），共用 Redis 键名和会话格式。

完整设计见 [docs/architecture.md](docs/architecture.md)。

## 配置

两个服务读取相同的环境变量。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DB_PATH` | `club_system.db` | SQLite 数据库路径 |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis 地址 |
| `REDIS_PREFIX` | `omms:` | Redis 键前缀，便于与其他应用共用实例 |
| `APP_UTC_OFFSET` | `+08:00` | 学校所在时区，开放时间按此解释 |
| `COOKIE_SECURE` | `0` | 使用 HTTPS 时设为 `1` |
| `SQLITE_SYNCHRONOUS` | `NORMAL` | SQLite 同步级别 |
| `BACKUP_DIR` | 数据库所在目录下的 `backups` | 自动快照目录 |
| `BACKUP_KEEP` | `30` | 保留的自动快照份数 |
| `ARGON2_MEMORY_KIB` / `ARGON2_TIME_COST` / `ARGON2_PARALLELISM` | `19456` / `2` / `1` | 口令哈希参数 |
| `AUTH_CONCURRENCY` | CPU 核数（最多 4） | 同时计算口令哈希的线程数 |
| `AUTH_QUEUE` | Python：线程数 × 16；Rust：线程数 × 64 | 排队等待口令校验的请求上限 |
| `SEAT_OP_TTL` | `30` | 报名租约秒数 |
| `MAX_IMPORT_STUDENTS` | `500` | 每批导入学生上限 |
| `SEATS_CACHE_MS` | `500` | 后端名额快照缓存毫秒数 |
| `REGISTER_WAIT_MS` | `8000` | Rust 报名请求等待结果的时长，超出后返回 `pending` |
| `MAX_CONCURRENCY` | `1024` | Rust 同时处理的请求上限 |

## 学生端接口

| 接口 | 说明 |
|---|---|
| `POST /api/login` | 学生登录，签发会话与受信设备 Cookie |
| `GET /api/catalog` | 社团目录，带 ETag |
| `GET /api/seats` | 名额快照、开放时间与锁定状态，带 ETag |
| `GET /api/check_registration_time` | 服务器时间与开放时间 |
| `POST /api/register_club` | 报名，参数 `club_id`、`request_id` |
| `POST /api/cancel_registration` | 退选 |
| `GET /api/get_student_info` | 个人信息与报名结果 |

响应统一包含 `success`、`code`、`message`：

- 200：业务结果，例如 `registered`、`full`、`not_open`、`registered_other`；
- 202 `pending`：已受理，正在确认；
- 503：繁忙或名额更新中，按 `Retry-After` 用同一个 `request_id` 重试即可。

提示码与文案见 `shared/messages.json`。

## 测试与压测

```bash
cargo build --release --manifest-path club-hot/Cargo.toml
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tests/stress_registration.py --seats 100 --users 1000 --concurrency 1000
```

压测在临时 SQLite 和随机端口的私有 Redis 上进行，结束后检查 16 项一致性条件：成功数等于名额、数据库人数、Redis 库存、报名镜像、在途成员与租约。

`tests/stress_site.py` 经 nginx 压测整站：登录高峰、抢课、分散报名和轮询，浏览器端按前端的重试规则执行，并报告每个服务进程的 CPU 时间和内存。

在一台 Apple Silicon 笔记本上的实测结果：

| 场景 | 吞吐 | p99 |
|---|---|---|
| 1000 名学生同时争抢 100 个名额 | 5828 次/秒 | 109 ms |
| 1000 名学生全部报名成功、全部写库 | 3369 次/秒 | 198 ms |

上线前请在目标服务器上用同样的命令测一次。

## 上线清单

- 开启 HTTPS，两个服务设置 `COOKIE_SECURE=1`。
- Redis 使用 `deploy/redis.conf`，或者为本系统单独分配一个库和键前缀。
- 按学校时区设置 `APP_UTC_OFFSET`，并校准服务器时钟。
- 提醒学生提前几分钟登录，让口令校验的计算分散在开放之前。
- 锁定和调剂之后下载完整备份，保存到另一台电脑。

## 项目结构

```
main.py          Python 管理服务与热接口备用实现（标准库 + SQLite）
club-hot/        Rust 热路径服务（axum + Redis + SQLite）
shared/          两个服务共用的 Lua 脚本与提示文案
web/             前端页面、样式、脚本与插画（原生 HTML、CSS、JS）
nginx.conf       边缘限流、缓存与路由
run.sh           一键启动与平滑重启
deploy/          Redis 配置、环境变量模板、systemd 单元
tests/           隔离的回归测试与压测
docs/            设计说明与截图
```

## 参与贡献

欢迎提交 Pull Request，提交前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 作者与许可

由 **BIRI GA（[KeikaJames](https://github.com/KeikaJames)）** 在 **Vles0123** 的原始版本上经授权重写和扩展，以 [Apache-2.0](LICENSE) 分发，署名见 [NOTICE](NOTICE)。
