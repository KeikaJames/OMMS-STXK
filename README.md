<div align="center">

# 社团选课 · OMMS-STXK

面向中学社团选课的报名系统：到点开放、全员抢报、锁定后统一调剂、导出名单。

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.7%2B-3776AB?logo=python&logoColor=white)
![Rust](https://img.shields.io/badge/Rust-1.88%2B-CE422B?logo=rust&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-3.2%2B-DC382D?logo=redis&logoColor=white)


<img src="docs/login.jpg" alt="登录页" width="760">

</div>

## 功能

**学生端**

- 登录页显示开放与截止时间。
- 倒计时按服务器时钟计算，到开放时间后报名按钮可用。
- 全部社团在一屏内列出：插图或上传的照片、名称、时间地点、剩余名额和报名按钮。手机上为左图右文。
- 分批报名时，学生页显示本班所在批次的倒计时与截止时间，名额是本批的余量。
- 报名与退选使用同一 `request_id` 自动重试；退选前弹出确认框。

**管理控制台**

- 双击 `启动.command` 即启动全部服务，终端里打印访问地址、管理员账号和密码，浏览器自动打开并登录控制台。
- 左侧栏分为工作台、学生、社团、报名、设置五项，各项内用分段控件切换。
- 工作台显示学生访问地址，可拷贝，或整屏放大显示后用电子教室软件的屏幕广播投到学生机；服务器同时接有线和无线时列出各网卡的地址。
- 工作台按使用顺序列出各步骤的状态和入口：导入学生、添加社团、开放时间、报名、调剂、导出与备份。名额合计与学生人数并列显示；报名表和完整备份在最近一次数据变化之后下载，显示为完成。
- 导入学生：上传表头含“姓名”“班级”的表格（xlsx、xls、csv、网页表格、XML 表格），服务器识别表头、合并单元格、班级标题行、多个工作表和合计行，在核对页逐行列出，标注待补班级、待核对姓名、同班同名、已导入等情况，可直接改正后导入。
- 导入后自动下载账号表（Excel：姓名、班级、账号、密码），按班分页，每行加框线。
- 按班或为单个学生重置密码，新密码同样生成 Excel。
- 编辑社团名称、名额、简介、老师、时间地点、年级与班级范围和卡片图：从 14 幅单线插图里选一幅（激光雕刻、平面设计、3D 建模、3D 打印、影视传媒、木工、编程、机器人、美术、音乐、科学实验、体育、阅读写作、通用），或上传照片。新社团按名称先选好一幅，之后改名不影响已选的插图；编辑页的预览就是学生看到的样子。照片 10 MB 以内，服务器缩到长边 960 像素后保存。
- 两种报名方式：**同时报名**，全体学生一个时段、共用名额；**分批报名**，学生分几节课到机房报名时使用，按班级分批，各批在自己的时段报名，社团名额每批单独计算。“快速分批”按班级顺序一次排好各批时段；学生提前到齐或提前下课时，工作台上点“现在开放”“现在截止”。当前批次截止后自动切换到下一批，最后一批截止后锁定名单。放大显示的访问地址下方同时显示当前批次和时段，学生名单多一列批次。
- 设置开放时间和截止时间；到截止时间自动锁定名单，也可以手动锁定。
- 调剂：锁定后把未报名的学生随机安排到有空余名额的社团，遵守年级和班级范围，安排人数取最大值。
- 下载 Excel 报名表：总表、各社团名单、未报名名单，“方式”一列区分自选、调剂和管理员安排。
- 在学生名单里修改姓名和班级，为单个学生代为报名、调整社团、移出社团或重置密码。
- 备份与恢复：下载完整备份（数据库和社团图片）；名单锁定时，以及调剂、清空、恢复之前，自动保存数据库快照。快照和备份文件都可以在控制台里直接恢复。
- 删除社团时，它的报名一并删除，删除前自动保存快照；这些学生随后可以改报其他社团。
- 清空报名、删除全部学生或社团前需确认；确认框默认焦点为“取消”。
- 操作记录分别列出管理操作和学生报名，导出与备份下载也记入其中。

**并发与运维**

- 抢位在 Redis 里一步完成；SQLite 容量触发器为每个社团设定最终上限。
- 名额修复以社团为单位进行，其他社团照常报名；Redis 重启后由 SQLite 自动重建。
- 名额快照带 ETag，由 nginx 每秒生成一次，全校轮询集中在一份缓存上。
- 登录高峰时口令校验按到达顺序排队；浏览器按服务器给出的间隔重试并显示结果。
- 写操作接口要求 `X-Requested-With: XMLHttpRequest` 请求头。

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

管理员密码保存在数据库旁的 `admin-password.txt`（权限 0600），首次启动时生成。

### 完整部署：nginx + Rust 热路径 + Python 管理服务

在 Mac 上双击 `启动.command`，或在终端执行：

```bash
bash run.sh
```

`run.sh` 适用于 macOS 和 Linux，依次完成以下步骤：

1. 编译 Rust 热服务，启动专用 Redis（`deploy/redis.conf`），生成并校验 nginx 配置。
2. 平滑切换：先等在途报名完成，再启动 Python（:2001）、Rust（:2002）和 nginx（:8080）。
3. 打印学生访问地址、控制台地址、管理员账号和密码，用一次性链接（2 分钟内有效）在浏览器里打开并登录控制台。设置 `OMMS_OPEN_BROWSER=0` 时只打印。

运行文件都放在 `run/` 目录下。再次执行即为平滑重启；双击 `停止.command` 或执行 `bash run.sh stop` 停止。

Cargo 1.88 及以上且编译成功时，报名接口由 Rust 热服务处理；其他情况下由 Python 服务处理：

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

nginx 使用仓库里的 `nginx.conf`：把 `__APP_ROOT__`、`__RUN_DIR__` 替换为实际路径；需要 HTTPS 时按 `run.sh` 中 `TLS_CERT` 的写法加入证书配置。

## 在 Mac 上运行

Apple 芯片的 Mac 可以直接做服务器。用 Homebrew 安装依赖后双击 `启动.command`：

```bash
brew install redis nginx
python3 -m pip install redis argon2-cffi pypinyin Pillow
```

- **文件句柄**：终端默认每个进程 256 个，`run.sh` 启动前调到 65536，nginx 每个工作进程 16384。
- **睡眠**：服务运行期间，`run.sh` 用 `caffeinate` 保持 Mac 唤醒。MacBook 接通电源并保持开盖。
- **防火墙**：首次运行时系统询问是否允许 nginx 接受传入连接，选择允许。
- **实测**：Apple M4 Max（16 核）全速运行，3000 名学生同时操作：

  | 场景 | 结果 | 用时 |
  |---|---|---|
  | 同时登录，Rust 热服务 | 全部登上 | 5.4 秒 |
  | 同时登录，只运行 Python | 全部登上 | 10.1 秒 |
  | 抢同一社团的 120 个名额 | 120 人报上，2880 人收到“名额已满” | 1.8 秒 |
  | 各自随机报三个社团之一 | 2904 人报上，96 人遇到所选社团已满 | 1.8 秒 |

- **网络**：使用有线网口。每名学生首次打开登录页和报名页共下载约 30 KB（卡片使用插图时；每张上传的照片另加 70–160 KB）：3000 人约 90 MB，千兆网口 1 秒内传完。开放瞬间的报名请求和每 2 秒一次的名额刷新，每次在 1 KB 以内。

## 低资源设备运行参考

运行时需要 Python 3.7+、nginx 1.12+ 和 Redis 3.2+；Rust 热服务可选。

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

- **登录**：每次口令校验约 50–70 毫秒。登录高峰时浏览器自动排队，最长等待 2 分钟。
- **时钟**：报名按服务器时钟开放。工作台显示服务器与管理员电脑的时间差；Linux 可用 `timedatectl set-ntp true` 校准。
- **内存 2 GB 以下**：把 `AUTH_CONCURRENCY` 设为 2，口令校验最多占用 38 MB。
- **Rust**：同样条件下从零编译约 3 分钟，单个编译进程峰值约 650 MB，`run.sh` 按内存决定并行数。也可以在同架构的另一台 Linux 机器上编译，把 `club-hot/target/release/club-hot` 拷过来。
- **nginx**：1.12 及以上；`run.sh` 在 1.17.6 以前的版本上自动调整访问日志格式。
- **浏览器**：IE11、Edge、Chrome、Firefox、Safari；360、QQ、搜狗浏览器使用极速模式。

在目标机器上运行以下压测：

```bash
python3 tests/stress_site.py login --backend python --users 1000
python3 tests/stress_site.py hot --backend python --users 1000
```

## 由 AI 代理运行

`agent/` 提供以 Python 标准库实现的 MCP 服务 `omms_mcp.py`，Claude、Codex、Cursor 等客户端可用它管理服务、参数、管理员账户、名单、社团、报名批次、调剂、导出和备份恢复。删除、清空、恢复、重置密码和调剂等操作需管理员确认；完整工具清单见 [代理运行指南](agent/AGENTS.md)。

- Claude Code、Cursor、VS Code：在 `agent` 文件夹中打开，配置已写好。
- Claude 桌面版：`python3 agent/omms_mcp.py register claude-desktop`，重新打开 Claude。
- Codex：`python3 agent/omms_mcp.py register codex`。
- 命令行：`python3 agent/omms_mcp.py call 工具名 '{"参数": "值"}'`。

运行参数保存在 `deploy/omms.env`；代理可通过 `update_settings` 修改。

## 使用流程

**管理员**（双击 `启动.command` 后控制台自动打开；在其他电脑上从登录页选择“管理员”）

1. **导入学生**：在“学生 → 导入”上传名单表格，在核对页改正标注的行后导入，账号表随即下载。
2. **添加社团**：在“社团 → 添加”填写名称和人数上限，再在社团卡片的“编辑”中补充简介、时间地点、年级范围和图片。各社团人数上限之和不少于学生人数时，调剂后每名学生都有社团。
3. **设置开放与截止时间**：在“设置 → 时间”按学校时间填写，学生端显示倒计时。首次设置开放时间前，学生页显示“开放时间待定”。截止时间为可选项。学生分几节课到机房报名时，在同一页选“分批报名”，按班级添加批次并设置各批时段，社团的人数上限即每批名额。
4. **报名期间**：在“报名 → 概览”查看各社团进度。
5. **锁定**：到截止时间自动锁定；也可以随时在“设置 → 时间”中锁定名单。锁定后报名与退选停止，系统同时保存一份数据库快照。
6. **调剂**：在“报名 → 调剂”查看未报名人数和各社团空余名额，确认后一次安排。
7. **导出**：在“报名 → 导出”下载报名表。
8. **备份**：在“设置 → 备份”下载完整备份，保存到另一台电脑。

**演练**：正式开放前可以先开放一次演练。演练结束后锁定名单，在“设置 → 数据”中清空报名，再设置正式开放时间并解除锁定。

**学生**：在开放前登录；到开放时间后点“报名”。每人报一个社团，锁定前可以退选后改报。

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
| `AUTH_CONCURRENCY` | 核数的一半，不少于 4、不多于 8（核数少于 4 时取核数） | 同时计算口令哈希的线程数 |
| `AUTH_QUEUE` | Python：线程数 × 16；Rust：线程数 × 64 | 排队等待口令校验的请求上限 |
| `SEAT_OP_TTL` | `30` | 报名租约秒数 |
| `MAX_IMPORT_STUDENTS` | `500` | 每批导入学生上限 |
| `SEATS_CACHE_MS` | `500` | 后端名额快照缓存毫秒数 |
| `REGISTER_WAIT_MS` | `8000` | Rust 报名请求等待结果的时长，超出后返回 `pending` |
| `MAX_CONCURRENCY` | `1024` | Rust 同时处理的请求上限 |

`run.sh` 从 `deploy/omms.env` 读取本表以及 `APP_UTC_OFFSET`、`BACKUP_KEEP`、`MAX_IMPORT_STUDENTS`、`SESSION_TTL`、`LOGIN_MAX_FAILS`、`AUTH_CONCURRENCY`、`MAX_HTTP_WORKERS`、`SEATS_CACHE_MS`、`LOG_LEVEL` 和 `PYTHON`。启动命令中的同名变量优先；systemd 单元读取整份环境文件。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HTTP_PORT` | `8080` | 访问端口；设为 80 时访问地址只写 IP |
| `OMMS_PYTHON_ONLY` | `0` | 设为 `1` 时只运行 Python 服务 |
| `OMMS_OPEN_BROWSER` | `1` | 启动后在本机浏览器打开控制台 |
| `TLS_CERT` / `TLS_KEY` | 空 | 证书与私钥路径。设置后在 `TLS_PORT` 上提供 HTTPS，`HTTP_PORT` 跳转到 HTTPS，两个服务使用 `COOKIE_SECURE=1` |
| `TLS_PORT` | `8443` | HTTPS 端口 |
| `REAL_IP_FROM` | 空 | 内网穿透或反向代理的地址，逗号分隔。nginx 只从这些地址接受 `REAL_IP_HEADER` 中的学生地址，限流按学生地址计算 |
| `REAL_IP_HEADER` | `X-Forwarded-For` | 学生地址所在的请求头；Cloudflare Tunnel 为 `CF-Connecting-IP` |

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

不同场景的压测方法和数据见 [tests/README.md](tests/README.md)。


## 上线清单

- 从校外访问时设置 `TLS_CERT`、`TLS_KEY` 启用 HTTPS；经内网穿透访问时设置 `REAL_IP_FROM`。
- Redis 使用 `deploy/redis.conf`，或者为本系统单独分配一个库和键前缀。
- 按学校时区设置 `APP_UTC_OFFSET`，并校准服务器时钟。
- 安排学生提前几分钟登录，分散开放前的口令校验负载。
- 锁定和调剂之后下载完整备份，保存到另一台电脑。

## 项目结构

```
main.py          Python 管理服务与热接口备用实现（标准库 + SQLite）
sheets.py        名单表格读取（xlsx、xls、csv、网页表格、XML 表格）与账号表生成，只用标准库
netinfo.py       本机各网卡的局域网地址，控制台和 AI 代理接口共用
club-hot/        Rust 热路径服务（axum + Redis + SQLite）
shared/          两个服务共用的 Lua 脚本与提示文案
web/             前端页面、样式、脚本与插画（原生 HTML、CSS、ES5 脚本）
nginx.conf       边缘限流、缓存与路由
run.sh           一键启动与平滑重启；启动.command、停止.command 为双击入口
agent/           AI 代理接口：MCP 服务、运行指南、各客户端配置
deploy/          Redis 配置、环境变量模板、systemd 单元；本机运行参数 omms.env
tools/           颜色回退样式生成、ES5 语法检查
tests/           隔离的回归测试与压测
docs/            设计说明（architecture.md）、开发与调试（development.md）、截图
```

## 参与贡献

贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)；本地运行、测试、调试方法和函数索引见 [docs/development.md](docs/development.md)。

## 作者与许可

作者：**BIRI GA（[KeikaJames](https://github.com/KeikaJames)）**。本项目基于 Vles0123 的原始版本重写与扩展，采用 [Apache-2.0](LICENSE) 许可；完整署名见 [NOTICE](NOTICE)。
