# 开发与调试

面向接手维护的开发者：代码结构、本地运行、测试、调试方法、发布要点、文案约定和函数索引。系统设计（抢位、修复、会话、限流）见 [architecture.md](architecture.md)，测试清单见 [tests/README.md](../tests/README.md)，AI 代理的运行指南见 [agent/AGENTS.md](../agent/AGENTS.md)。

## 1. 代码结构

| 路径 | 内容 |
|---|---|
| `main.py` | Python 管理服务与热接口的备用实现：路由、会话、导入导出、备份恢复、调剂、操作记录（标准库 + SQLite + Redis） |
| `sheets.py` | 名单表格读取（xlsx、xls、csv、网页表格、XML 表格 2003）与 Excel 写出，只用标准库 |
| `netinfo.py` | 本机各网卡的局域网地址，控制台和 AI 代理接口共用 |
| `club-hot/` | Rust 热服务（axum）：登录、报名、退选、名额、目录 |
| `shared/` | 两个服务共用的 Lua 脚本（`shared/lua`）和提示文案（`shared/messages.json`） |
| `web/` | 页面、样式、ES5 脚本和插画；`app-fallback.css` 由工具生成 |
| `agent/` | AI 代理接口：MCP 服务 `omms_mcp.py`、运行指南 `AGENTS.md`、各客户端配置 |
| `tools/` | `css_fallbacks.py`（颜色回退样式）、`check_es5.js`（ES5 语法检查） |
| `tests/` | 回归测试与压测，每个测试使用临时数据库和随机端口的 Redis |
| `run.sh` | 一键启动、平滑重启、停止；`启动.command`、`停止.command` 为双击入口 |
| `nginx.conf` | 边缘路由、限流、缓存模板，`run.sh` 按运行参数生成实际配置 |
| `deploy/` | Redis 配置、systemd 单元、环境变量模板；本机运行参数 `deploy/omms.env` |

运行时文件（都在 .gitignore 中）：`club_system.db`、`admin-password.txt`、`backups/`、`club-images/`、`run/`、`exports/`、`deploy/omms.env`。

## 2. 本地运行

### 2.1 只运行 Python 服务

开发时最快的方式：一个 Redis 加上 `main.py`，页面和全部接口都由 Python 提供。

```bash
python3 -m pip install redis argon2-cffi pypinyin Pillow
redis-server --port 6390 --save "" --daemonize yes
mkdir -p /tmp/omms
DB_PATH=/tmp/omms/club_system.db REDIS_URL=redis://127.0.0.1:6390/0 python3 main.py
```

打开 `http://127.0.0.1:2001`。管理员账号、密码和一次性登录链接：

```bash
DB_PATH=/tmp/omms/club_system.db REDIS_URL=redis://127.0.0.1:6390/0 python3 main.py admin-login http://127.0.0.1:2001
```

输出三行：`ADMIN_USER=`、`ADMIN_PASSWORD=`、`LOGIN_URL=`。登录链接 2 分钟内有效、只能使用一次，只在本机打开。

常用环境变量：`HOST`（监听地址，局域网访问时设为 `0.0.0.0`）、`PORT`（默认 2001）、`WEB_DIR`、`LOG_LEVEL`。完整列表见 README 的“配置”一节。

### 2.2 完整运行

```bash
OMMS_RUN_DIR=/tmp/omms/run DB_PATH=/tmp/omms/club_system.db REDIS_URL=redis://127.0.0.1:6396/0 \
  OMMS_SETTINGS=/tmp/omms/omms.env OMMS_OPEN_BROWSER=0 bash run.sh
bash run.sh stop
```

- 上面的变量把运行文件、数据库、Redis 和参数文件放到临时位置，和正式数据分开。
- Python 固定在 2001，Rust 在 2002，nginx 在 `HTTP_PORT`（默认 8080）。
- `bash run.sh stop` 停止 nginx、Rust 和 Python，Redis 继续运行；停 Redis 用 `redis-cli -p 端口 shutdown nosave`。

### 2.3 浏览器走查

- 管理员与学生同时登录时，一个用 `localhost`、一个用 `127.0.0.1` 打开，两者的 Cookie 分开保存。
- 在开发者工具里模拟上传名单：用 `DataTransfer` 构造 `File`，在 `#dropzone` 上派发 `drop` 事件。
- 页面在后台标签页时截图会停在旧画面，先刷新再截图。

## 3. 测试与检查

```bash
cargo build --release --manifest-path club-hot/Cargo.toml   # test_edge_security 使用 release 构建
python3 -m unittest discover -s tests -p 'test_*.py' -v
cargo test --manifest-path club-hot/Cargo.toml
ruff check main.py sheets.py netinfo.py agent/ tests/ tools/
shellcheck run.sh
node --expose-internals tools/check_es5.js web/js/*.js web/easter-egg.js
python3 tools/css_fallbacks.py --check
```

- 需要 `redis-server`；`test_edge_security.py` 另需 nginx。
- 装了 openpyxl 时，`test_sheets.py` 用它核对生成的 Excel。
- ES5 检查用 Node 自带的 acorn；当前 Node 版本不带 acorn 时，该项测试记为跳过。
- 各测试文件覆盖的内容见 tests/README.md。

## 4. 调试方法

### 4.1 日志

`run.sh` 启动后，日志在 `run/` 目录：

| 文件 | 内容 |
|---|---|
| `python.log` | Python 服务；上一次运行的在 `python.log.1` |
| `rust.log` | Rust 热服务；上一次运行的在 `rust.log.1` |
| `nginx-error.log` | nginx 运行日志 |
| `nginx-access.log` | 访问日志，含上游耗时与限流状态，超过 64 MB 时换新文件 |
| `redis.log` | Redis |
| `build.log` | 最近一次 Rust 编译 |
| `agent-start.log` | 最近一次经 MCP 的 `start_service` |

排查时把 `LOG_LEVEL` 设为 `DEBUG`（写入 `deploy/omms.env` 后重启）。

### 4.2 直接调用接口

所有 POST 请求带 `X-Requested-With: XMLHttpRequest`。管理员登录并保存 Cookie：

```bash
curl -s -c /tmp/omms/jar -H 'X-Requested-With: XMLHttpRequest' -H 'Content-Type: application/json' \
  -d "{\"username\": \"admin\", \"password\": \"$(cat admin-password.txt)\"}" http://127.0.0.1:2001/api/admin_login
curl -s -b /tmp/omms/jar http://127.0.0.1:2001/api/admin/summary
curl -s -b /tmp/omms/jar -H 'X-Requested-With: XMLHttpRequest' -H 'X-File-Name: roster.xlsx' \
  --data-binary @roster.xlsx http://127.0.0.1:2001/api/admin/import/parse
```

路由与处理函数的对应关系在 `main.py` 的 `GET_ROUTES`、`POST_ROUTES`、`RAW_POST_ROUTES`、`PAGES` 四张表里；nginx 把热接口转给 Rust，其余转给 Python（见 `nginx.conf` 的 `location`）。

### 4.3 Redis

```bash
redis-cli -p 6379 --scan --pattern 'omms:*' | head
redis-cli -p 6379 GET omms:stock:1
redis-cli -p 6379 GET omms:open_at
```

键的含义见 architecture.md 的“Redis 键”。名额以 SQLite 为准，Redis 清空后由后台线程自动重建。

### 4.4 SQLite

```bash
sqlite3 club_system.db
```

```sql
SELECT id, name, max_students, current_students, enabled, revision FROM clubs;
SELECT COUNT(*) FROM students;
SELECT COUNT(*) FROM registrations;
SELECT registration_start_time, registration_end_time, registration_locked FROM settings;
SELECT occurred_at, actor_role, actor_id, action, reason FROM audit_events ORDER BY occurred_at DESC LIMIT 20;
```

修改数据请经控制台或接口进行：接口在同一事务里写操作记录，并同步 Redis 名额。

### 4.5 健康检查

`GET /healthz`、`GET /readyz`：Python 在 2001，Rust 在 2002；经 nginx 访问时只接受本机。

### 4.6 名单解析

```bash
python3 -c "import json, sheets; print(json.dumps(sheets.parse_roster('a.xlsx', open('a.xlsx', 'rb').read()), ensure_ascii=False, indent=1))"
```

输出每行的 `sheet`、`line`、`name`、`class`、`notes`，以及各工作表的表头位置和略过的行数。

### 4.7 局域网地址

```bash
python3 netinfo.py
```

每行一个地址：IP、有线或无线、网卡名。默认路由所在的网卡在第一行。

### 4.8 MCP 服务

```bash
python3 agent/omms_mcp.py tools
python3 agent/omms_mcp.py call service_status
python3 agent/omms_mcp.py call get_overview
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python3 agent/omms_mcp.py
```

可用环境变量指向其他实例：

| 变量 | 默认 | 作用 |
|---|---|---|
| `OMMS_BACKEND` | `http://127.0.0.1:2001` | 管理接口地址 |
| `DB_PATH` | 程序目录下 `club_system.db` | 读取管理员账号名，定位密码文件 |
| `ADMIN_SECRET_FILE` | 数据库旁 `admin-password.txt` | 管理员密码文件 |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | `main.py admin-login` 写一次性登录令牌 |
| `OMMS_RUN_DIR` | 程序目录下 `run/` | 日志与进程文件 |
| `OMMS_EXPORT_DIR` | 程序目录下 `exports/` | 导出文件 |
| `OMMS_SETTINGS` | `deploy/omms.env` | 运行参数文件 |
| `PYTHON` | `python3` | 运行 `main.py admin-login` 的 Python |

服务的标准输出只写 JSON-RPC 消息，诊断信息写到 stderr。

### 4.9 前端

- 修改 `app.css` 或脚本后，提高所有页面里对应的 `?v=` 版本号：nginx 把带版本号的样式和脚本缓存 7 天，页面本身每次重新校验。
- 修改 `app.css` 的颜色后运行 `python3 tools/css_fallbacks.py`，重新生成 `app-fallback.css`。
- 脚本按 ES5 编写：`var`、函数表达式、`Array.prototype.forEach` 等；DOM 用 `appendChild`、`removeChild`，类名用 `App.toggleClass`；Promise、`Object.assign`、`find`、`closest` 由 `common.js` 补齐。`tools/check_es5.js` 与 `tests/test_frontend.py` 检查这些约定。
- `legacy.js` 在其他脚本之前检查浏览器能力，需要时显示换用浏览器的提示。
- 深色模式由 `prefers-color-scheme` 切换颜色变量；页面断点为 900 和 560 像素。

## 5. 发布要点

以下是打包发布时会用到的事实。

**组成与依赖**

- 运行时需要：Python 3.7 及以上（依赖 redis、argon2-cffi、pypinyin、Pillow）、Redis 3.2 及以上、nginx 1.12 及以上；Rust 热服务可选。
- Rust 热服务是唯一需要编译的组件，产物为 `club-hot/target/release/club-hot`（Windows 上为 `club-hot.exe`），需要 Rust 1.88 及以上。

**构建目标**

- Linux：`x86_64-unknown-linux-gnu`、`aarch64-unknown-linux-gnu`。
- macOS：`aarch64-apple-darwin` 与 `x86_64-apple-darwin`，可用 `lipo -create` 合成通用二进制。
- Windows：`x86_64-pc-windows-msvc`。

**run.sh 如何选择 Rust 热服务**

- cargo 可用时，先执行 `cargo build --release`，再运行上面的产物；其余情况只运行 Python 服务。
- 打包时把预编译的二进制放在 `club-hot/target/release/` 下，目录结构与源码一致。
- 希望目标机器只装 Python 时也用上预编译的二进制：在 `run.sh` 的这段选择逻辑里，先检查该文件是否可执行。

**Windows 11**

- `run.sh` 是 bash 脚本。在 WSL2 的 Ubuntu 里可以直接使用 Linux 版。
- WSL2 的服务要让局域网访问，需在 `%USERPROFILE%\.wslconfig` 的 `[wsl2]` 下设置 `networkingMode=mirrored`，并在 Windows 防火墙放行端口。
- 原生运行的组成：
  - Python 3 及依赖；
  - 兼容 Redis 协议的服务：Memurai，或 WSL2 里的 Redis（经 localhost 访问）；
  - nginx 官方 Windows 版：使用 select 模型，单进程，连接数适合机房规模；
  - `club-hot.exe`：SIGTERM 处理写在 `#[cfg(unix)]` 下，Windows 上用 Ctrl+C 结束。
- 只用 Python 时，`HOST=0.0.0.0 PORT=8080 python main.py` 即可直接提供全部页面与接口。`main.py` 只信任本机反向代理写入的 `X-Real-IP`，直接对外时按连接地址识别学生。

**macOS 分发**

- 发布时对二进制签名并公证。
- 也可以在目标机器上执行 `xattr -dr com.apple.quarantine 程序目录`，清除下载带来的隔离属性。
- 用 tar.gz 打包，可保留 `run.sh`、`启动.command` 的可执行权限。

**版本**

页面资源的版本号写在三个 HTML 文件里。Git 标签与发布说明按功能列出当前行为。

## 6. 文案约定

- 用正面陈述：写现在的状态、做法和下一步。提示以“请……”给出下一步，例如“请重试”“请刷新页面”“请检查网络连接”。
- 按钮用两个字为主：存储、取消、删除、重置、导入、拷贝。破坏性操作用确认框，写清将发生的变化，默认焦点在“取消”。
- 状态名称保持统一：开放时间待定、待开放、报名中、名单已锁定、已报名、未报名、已满、暂停报名、名额更新中。
- 核对页标注：待补班级、待补姓名、待核对姓名、待核对内容、班级沿用上一行、班级取自工作表名、同班同名、已导入。
- “调剂”专指锁定后的统一安排；管理员为单个学生的操作叫“调整”。
- 文档和 PR 说明写功能现在怎样工作，以及如何验证。

## 7. 函数索引

### 7.1 main.py（控制台、导入、备份恢复、管理员登录相关）

| 函数 | 功能 |
|---|---|
| `RedisGate.take(key)` | 读取并删除一个键，用于一次性登录令牌 |
| `attachment_header(filename, fallback)` | 生成下载头：中文文件名按 RFC 5987，附 ASCII 备用名 |
| `export_stamp()` | 导出文件名里的时间戳 |
| `gen_admin_password()` | 生成 12 位管理员密码，只用容易辨认的字符 |
| `read_admin_secret()` / `write_admin_secret(password)` | 读写 `admin-password.txt`，写入时权限 0600、先写临时文件再替换 |
| `copy_database(source_path, target_path)` | 用 SQLite 备份接口复制数据库，得到某一时刻的完整状态 |
| `list_snapshots()` | 列出备份目录中的自动快照，新的在前 |
| `snapshot_database(label)` | 保存一份快照并返回文件名，同一秒重名时加序号，只保留最近 `BACKUP_KEEP` 份 |
| `backup_archive()` | 生成完整备份 zip：数据库与社团图片 |
| `database_counts(path)` | 校验备份文件（完整性与四张数据表），返回学生、社团、报名数量 |
| `cli_admin_login(base_url)` | `main.py admin-login`：核对密码文件与数据库哈希，需要时生成新密码，签发一次性登录令牌，打印三行结果 |
| `ensure_schema(conn)` | 建表、在线迁移、触发器；启动时和恢复备份后各执行一次 |
| `ClubSystemHandler._client_ip()` | 取学生地址：只采用本机反向代理写入的 `X-Real-IP` |
| `ClubSystemHandler._read_raw_body(max_bytes)` | 按长度上限读取请求体 |
| `ClubSystemHandler.do_GET()` | 分发 GET：页面、静态资源、`/admin/login` 一次性登录、`GET_ROUTES` |
| `ClubSystemHandler.do_POST()` | 检查 `X-Requested-With`，再分发 `RAW_POST_ROUTES`（原始上传）与 `POST_ROUTES`（JSON） |
| `ClubSystemHandler._snapshot_before(label)` | 危险操作前保存快照，快照保存后才继续 |
| `ClubSystemHandler._record_download(sess, action, metadata)` | 把导出与备份下载记入操作记录 |
| `ClubSystemHandler._token_login()` | 校验一次性令牌（只接受本机），签发管理员会话并跳转控制台 |
| `ClubSystemHandler._h_import_parse(sess, raw)` | `POST /api/admin/import/parse`：解析上传的名单，标注已导入的行，只返回结果 |
| `ClubSystemHandler._h_accounts_workbook(sess, data)` | `POST /api/admin/accounts_workbook`：把账号列表写成账号表 Excel |
| `ClubSystemHandler._h_restore(sess, data)` | `POST /api/admin/restore`：从自动快照恢复 |
| `ClubSystemHandler._h_restore_upload(sess, raw)` | `POST /api/admin/restore_upload`：从上传的 zip 或 .db 恢复，zip 中的社团图片一并写回 |
| `ClubSystemHandler._restore_from(sess, source, label, images)` | 恢复的公共步骤：维护标记、恢复前快照、整库替换、重建表结构、保留管理员账号、吊销学生会话、重新发布名额 |
| `ClubSystemHandler._h_backup(sess)` / `_h_backups(sess)` | 下载完整备份或指定快照；列出快照 |
| `ClubSystemHandler._h_addresses(sess)` | `GET /api/admin/addresses`：本机各网卡的局域网地址 |

### 7.2 sheets.py

| 函数 | 功能 |
|---|---|
| `TableError` | 表格读取提示，消息直接显示给管理员 |
| `Sheet` | 一个工作表：名称、行、合并单元格 |
| `read_table(filename, data)` | 先按内容识别格式（zip、复合文档、标记语言、文本），再参考扩展名，返回工作表列表 |
| `read_xlsx(data)` | 读取 xlsx：限制解压大小，只解析普通 XML |
| `_read_part(archive, name)` | 读取 xlsx 中的一个 XML 部件 |
| `_xlsx_sheet(name, root, shared)` | 解析一个 xlsx 工作表的单元格与合并区域 |
| `_texts(element)` | 拼接共享字符串或行内字符串的各段文字，略过注音 |
| `_local(tag)` / `_column_index(letters)` / `_relationship_id(element)` | XML 本地名、列字母转序号、关系 ID |
| `_number_text(value)` | 数值单元格转文本，整数写成整数形式 |
| `_dense(cells)` | `{(行, 列): 文本}` 转成行列表 |
| `read_xls(data)` | 读取 xls（BIFF8） |
| `_cfb_stream(data)` | 从复合文档中取出 Workbook 流，核对扇区链 |
| `_records(stream, position)` | 逐条读取 BIFF 记录 |
| `_Segments` | 共享字符串表及其续记录，字符数组跨记录时切换编码宽度 |
| `_unicode_string(body, offset, short)` / `_rk_number(value)` | 解析 BIFF 字符串与 RK 数值 |
| `_xls_sheet(name, stream, position, shared)` | 解析一个 xls 工作表：文本、数值、公式结果、合并单元格 |
| `decode_text(data)` | 识别 BOM、UTF-8、GB18030 并解码 |
| `read_csv(text)` | 识别分隔符（逗号、Tab、分号、全角逗号、竖线）并读取 |
| `_HtmlTables` / `read_markup(text)` | 读取网页表格（含跨列）与 XML 表格 2003 |
| `_read_spreadsheet_2003(text)` / `_int_attribute` / `_index_attribute` | XML 表格 2003 的单元格与 `ss:Index` |
| `header_key(text)` | 表头规整：全角转半角，括号说明与符号略过 |
| `clean_name(text)` / `clean_class(text)` | 姓名、班级规整：空白、间隔号、全角数字、“高一（1）班”写法 |
| `_join_grade(grade, number)` | “年级”与“班号”两列拼成班级 |
| `_locate_header(rows)` | 在前 40 行中定位表头，返回姓名、班级、年级、班号所在列 |
| `_merged_source(merges, row, col)` / `_cell(rows, row, col)` | 合并单元格的源单元格、安全取值 |
| `parse_roster(filename, data)` | 名单解析：逐行给出姓名、班级和标注，统计略过的行 |
| `write_xlsx(sheets)` | 写出 xlsx，单元格一律为文本 |
| `_package(names, sheet_xmls, styles, defined_names)` / `_text_cell(ref, value, style)` | 组装 xlsx 文件、生成文本单元格 |
| `column_letter(index)` / `sheet_names(titles)` | 列序号转字母；生成合规且不重名的工作表名 |
| `natural_key(text)` | 班级自然排序键：中文数字与阿拉伯数字按数值比较 |
| `accounts_xlsx(accounts)` | 账号表：按班排序、每班新起一页、框线、32 磅行高、每页重复表头、按页宽缩放 |

### 7.3 netinfo.py

| 函数 | 功能 |
|---|---|
| `lan_addresses(max_age)` | 返回 `[{address, label, interface}]`，默认路由所在的网卡在前，结果缓存 30 秒 |
| `collect(platform, run)` | 按平台执行命令并整理地址；`run` 可替换，供测试使用 |
| `parse_ifconfig(text)` / `parse_ip_addr(text)` | 解析 macOS `ifconfig`、Linux `ip -4 -o addr` 的输出 |
| `parse_hardware_ports(text)` | 解析 `networksetup -listallhardwareports`：网卡名到“Wi-Fi”“USB LAN”等名称 |
| `parse_default_interface(text)` | 从 `route -n get default` 或 `ip route` 取默认路由所在的网卡 |
| `label(interface, port)` | 标注有线、无线 |
| `usable(address)` | 判断是否为可供学生机访问的常规 IPv4 地址 |
| `_route_address()` | 默认路由的源地址（UDP 套接字选路，不发送数据），作为最后的结果 |
| `_run(arguments)` | 在常见系统目录中找到命令并执行，返回标准输出 |

### 7.4 agent/omms_mcp.py

**路径与运行参数**

| 函数 | 功能 |
|---|---|
| `settings_path()` / `db_path()` / `run_dir()` / `export_dir()` / `secret_file()` / `backend_url()` | 各文件与服务地址，均可用环境变量指定 |
| `read_settings()` | 读取 `deploy/omms.env`，返回键值与原始行 |
| `effective(key)` | 参数的生效值：启动环境、参数文件、默认值依次查找，与 `run.sh` 一致 |
| `child_env(**extra)` | 子进程环境，补上 Homebrew 与 cargo 的常见目录 |
| `python_command()` | 运行服务所用的 Python |
| `school_tz()` / `school_time(ms)` / `parse_time(text, field)` | 学校时区、毫秒时间转学校时间、解析多种日期写法 |

**本机状态**

| 函数 | 功能 |
|---|---|
| `port_open(port)` / `pid_alive(name)` | 端口是否在监听、`run/` 中记录的进程是否存在 |
| `lan_ip()` | 默认路由的局域网地址，`netinfo` 不可用时使用 |
| `web_scheme_port()` / `web_base(host)` / `addresses()` | 访问协议与端口、拼出访问地址；多网卡时列出其他网卡 |
| `backend_port()` / `redis_port()` | 管理服务与 Redis 的端口 |
| `tail(text, lines)` / `open_target(target, reveal)` | 取末尾若干行；在访达或系统默认程序中打开 |

**管理员身份与接口调用**

| 函数 | 功能 |
|---|---|
| `run_admin_login()` | 调用 `main.py admin-login`，取得账号、密码和一次性登录链接 |
| `file_credentials()` | 从密码文件与数据库读取管理员账号密码 |
| `Reply` | HTTP 响应：取头、解析 JSON、下载文件名、`Retry-After` |
| `Backend.send(...)` / `Backend.login(heal)` / `Backend.api(...)` | 发请求；管理员登录（需要时调用 `run_admin_login` 再登录）；带会话的接口调用，401 自动重新登录、503 按约定时间重试 |
| `get(path)` / `post(path, payload)` / `rid()` | GET、POST 的简写；生成请求编号 |

**数据整理**

| 函数 | 功能 |
|---|---|
| `short_name(name)` / `natural_key(text)` / `clean_text(value, field)` / `text_list(value)` | 社团短名、自然排序、文字字段校验、列表参数拆分 |
| `phase_text(info)` | 报名状态文字 |
| `all_clubs()` / `all_students()` / `club_row(club)` / `student_row(student, clubs)` | 读取并整理社团、学生 |
| `find_club(ref)` / `find_student(ref)` | 按名称、编号或账号、“姓名 班级”定位，重名时列出候选 |
| `save_file(data, filename, output_dir)` / `read_input(path_text, limit)` | 写出文件（0600，重名加序号）；读取输入文件并检查大小 |
| `save_accounts(credentials, output_dir)` | 调用账号表接口并保存 Excel，每个文件最多 5000 行 |
| `flow_steps(summary, info)` | 使用流程各步骤的状态 |

**工具**（每个 `tool_*` 对应一个 MCP 工具，参数与说明见 `TOOLS` 表）

| 函数 | 功能 |
|---|---|
| `tool_service_status` / `tool_start_service` / `tool_stop_service` / `tool_service_logs` / `tool_open_console` | 服务状态、启动（新会话中执行 `run.sh`）、停止、日志、打开控制台 |
| `start_in_progress()` / `registration_running()` / `opening_soon(info)` | 启动是否进行中；报名状态；是否报名中或一小时内开放（决定是否需要确认） |
| `validate_setting(key, value)` / `tool_get_settings` / `tool_update_settings` | 参数校验、查看、写入 |
| `tool_admin_credentials` / `tool_change_admin_password` | 管理员账号密码、修改密码 |
| `tool_get_overview` / `tool_set_schedule` / `tool_clear_schedule` / `tool_set_lock` | 概览、开放与截止时间、锁定 |
| `parse_roster(path_text)` / `row_view(row)` / `class_counts(rows)` | 上传解析、行摘要、各班人数 |
| `tool_preview_roster` / `tool_import_roster` | 名单预览；按位置改正、跳过，按姓名和班级对照现有学生，分批导入并生成账号表 |
| `tool_list_students` / `tool_update_student` / `tool_reset_passwords` / `tool_delete_student` | 学生查找、修改与调整社团、重置密码、删除 |
| `club_payload(club, args)` / `apply_club_image(club, image)` | 编辑社团的完整参数；更换图片或恢复默认 |
| `tool_list_clubs` / `tool_add_club` / `tool_update_club` / `tool_delete_club` | 社团管理 |
| `tool_adjustment` / `tool_export_data` / `tool_audit_log` | 调剂、导出、操作记录 |
| `tool_backup_now` / `tool_list_snapshots` / `tool_restore` / `tool_clear_data` | 备份、快照、恢复、清空 |

**协议与命令行**

| 函数 | 功能 |
|---|---|
| `schema(properties, required)` / `tool_descriptor(item)` | 工具参数结构与描述 |
| `run_tool(name, arguments)` | 执行工具，返回文本与是否需要处理 |
| `handle(message)` / `serve()` | JSON-RPC 分发（initialize、tools、resources、ping）；按行读写的 stdio 循环 |
| `guide_text()` | 资源 `omms://guide` 的内容 |
| `register(target)` / `claude_desktop_config()` | 在 Claude 桌面版或 Codex 的配置中登记本服务 |
| `main(argv)` | 命令行入口：`tools`、`call`、`register`，无参数时运行 MCP 服务 |
| `ToolError` / `NeedsConfirm` / `dumps(value)` | 返回给代理的提示；待确认的操作；JSON 输出 |

### 7.5 web/js/common.js（全局 `App`）

| 函数 | 功能 |
|---|---|
| Promise、`Object.assign`、`Array.prototype.find/findIndex`、`Element.prototype.matches/closest`、`NodeList.prototype.forEach` | 为 IE11 补齐的接口 |
| `$(selector, root)` / `$$(selector, root)` | 取单个元素、取元素数组 |
| `App.el(tag, className, text)` / `App.add(parent, ...)` / `App.clear(node)` / `App.button(label, className, onClick)` | 创建元素、追加子节点、清空、创建按钮 |
| `App.toggleClass(node, name, on)` / `App.hidden()` / `App.onVisible(callback)` | 切换类名；页面是否在后台；页面可见性变化 |
| `App.request(path, options)` | XHR 请求：带 `X-Requested-With`，GET 每次向服务器取最新数据，401 回登录页，返回 `{status, ok, data, blob, filename, retryAfter}` |
| `App.upload(path, file, options)` | 上传原始文件，文件名放在 `X-File-Name` |
| `App.saveBlob(blob, filename)` | 保存下载内容，IE 用 `msSaveOrOpenBlob` |
| `App.toast(message, kind)` / `App.busy(node, label)` | 提示条；按钮进入处理中、恢复 |
| `App.requestId()` / `App.pad(value)` / `App.schoolTime(text)` | 请求编号；两位补零；学校时间文本转“M 月 D 日 HH:MM” |
| `layer(options)` / `trapFocus(layer, event)` | 对话框与表格页的公共实现：焦点循环、Esc 关闭、返回原焦点 |
| `App.confirm(options)` / `App.alert(options)` / `App.sheet(options)` | 确认框（破坏性操作默认聚焦“取消”）、提示框、表格页（`narrow` 为窄页） |
| `App.bindLogout()` | 绑定“退出”按钮 |

### 7.6 web/js/admin.js（控制台）

| 函数 | 功能 |
|---|---|
| `readRoute()` / `go(view, pane)` / `show(next, focus)` / `renderTabs()` / `load()` | 侧栏与分段控件的路由：读地址栏、跳转、显示、刷新所需数据 |
| `watching()` / `refreshOverview()` / `loadOverview()` | 工作台与报名概览每 6 秒刷新 |
| `renderPhase(time)` / `scheduleText(time)` / `renderFill(id, clubs)` / `renderFlow(summary, time)` / `renderClockSkew(...)` | 报名状态、开放与截止时间、各社团进度、流程步骤、服务器与本机的时钟差 |
| `loadAccess()` / `hostIsAddress(host)` / `siteBase(host)` / `copyText(text)` / `presentAccess()` / `fullscreenElement()` | 学生访问地址卡片：读取地址、拼出网址、拷贝、整屏放大显示 |
| `loadStudents()` / `matchingStudents()` / `renderStudents()` / `onSearch()` | 学生名单：读取、搜索、每页 200 行 |
| `openStudent(student)` 及其中的 `save`、`confirmReset`、`confirmRemove` | 学生编辑页：姓名班级、社团调整、重置密码、删除 |
| `readRoster(file)` / `openReview(fileName, parsed)` | 上传名单；核对页 |
| 核对页内的 `problem`、`notesFor`、`flagged`、`paintTags`、`summary`、`reviewRow`、`render`、`setFilter`、`runImport` | 逐行检查、标注、计数、编辑、筛选、分页、分批导入 |
| `downloadAccounts(accounts, button)` / `setCredentials(list, label)` / `renderCredentials()` | 生成并下载账号表；本页新账号列表 |
| `fillClassSelect()` | 按班重置的班级下拉框 |
| `loadClubs()` / `clubCard(club)` / `openClubEditor(original)` / `confirmDeleteClub(club)` | 社团卡片、编辑页（图片更换与恢复默认）、删除 |
| `uploadClubImage(...)` / `removeClubImage(...)` | 社团图片上传、恢复默认 |
| `loadAdjust()` | 调剂预览与执行 |
| `loadAudit(reset)` / `auditTime` / `auditTarget` / `auditDetail` | 操作记录与其文字说明 |
| `loadSettings()` / `toField` / `fromField` / `renderLock(time)` / `renderData(time, summary)` | 时间、锁定、数据页；日期输入在旧浏览器上按文本处理 |
| `destructive(...)` | 清空报名、删除全部学生或社团的确认与执行 |
| `loadBackups()` / `confirmRestore(item, button)` / `finishRestore(result, button)` | 快照列表、从快照或文件恢复 |
| `retry(path, body)` / `sequence(steps)` / `post(path, body, options)` | 503 与连接重置时重试；按顺序执行多个请求 |
| `ok` / `message` / `begin` / `latest` / `changed` / `trim` / `isInteger` / `wait` / `targetOf` / `teacher` / `splitRoom` / `splitList` / `naturalCompare` / `stampText` / `fileSize` | 结果判断、提示文字、请求版本号、内容签名、文本与数字工具 |
| `meter` / `head` / `emptyRow` / `tableMessage` / `field` / `textInput` / `checkbox` / `fileInput` / `takeFile` / `renderPager` | 界面元件 |
| `rememberClubs` / `clubName` / `setStudents` / `studentLabel` | 社团、学生索引 |

### 7.7 web/js/student.js（学生页）

| 函数 | 功能 |
|---|---|
| `phase()` / `registeredId()` / `remaining(club)` / `eligibility(club)` | 当前阶段（待定、倒计时、报名中、已报名、已截止）、已报社团、剩余名额、年级班级范围 |
| `loadMe()` / `syncTime()` / `loadCatalog()` / `loadSeats()` | 个人信息；与服务器对时；社团目录；名额快照 |
| `pollDelay()` / `schedulePoll()` / `scheduleOpen()` | 轮询间隔（报名中 2 秒，临近开放 4 秒，其余 10–15 秒）；到开放时间刷新 |
| `renderStatus()` / `tick()` / `formatDuration(ms)` / `formatClock(ms)` | 状态条、每秒更新倒计时或当前时间 |
| `renderMine()` / `confirmCancel(name)` | 已报名卡片、退选确认 |
| `createCard` / `renderArt` / `actionFor` / `updateCard` / `renderClubs` / `render` | 社团卡片按 id 复用节点，内容变化时才更新 |
| `register(club)` / `settle(data, club)` / `cancelRegistration()` / `refreshAll()` | 报名与退选，同一请求编号在排队或确认中自动重试 |
| `teacher` / `splitRoom` / `clubById` / `metaLine` / `now` / `wait` / `has` | 文本与时间工具 |

### 7.8 其他脚本

| 文件与函数 | 功能 |
|---|---|
| `login.js`：`shouldWait(result)`、`attempt(path, body, started)` | 登录排队：按 `Retry-After` 重试，最长 2 分钟；身份切换与开放时间显示 |
| `club-art.js`：`kind(name)`、`photo(club)`、`picture(source, label)`、`illustration(club)`，导出 `ClubArt.node`、`ClubArt.key` | 按社团名称选插图（3D 打印、传媒、木工、通用）；上传的图片以背景图显示 |
| `legacy.js` | 检查 JSON、XHR、`querySelector`、`classList`、`dataset`、`Uint8Array`，需要时显示换用浏览器的提示 |
| `easter-egg.js` | 在浏览器开发者控制台打印横幅 |

### 7.9 工具脚本与 Rust

| 函数 | 功能 |
|---|---|
| `tools/css_fallbacks.py`：`strip_comments`、`variables`、`resolve`、`rules`、`build`、`main` | 读取 `app.css` 的颜色变量，为每条使用变量的声明生成固定颜色版本；`--check` 核对生成结果是否最新 |
| `tools/check_es5.js` | 用 Node 自带的 acorn 按 ES5 语法解析给定脚本 |
| `club-hot/src/main.rs`：`require_page_request(request, next)` | 中间件：POST 请求带 `X-Requested-With` 才进入处理，其余返回 403 |
