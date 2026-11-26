# 社团选课系统 · 代理运行指南

本指南写给代表学校管理员运行和管理本系统的 AI 代理。管理员通常不使用命令行，由代理通过本目录的 MCP 服务 `omms_mcp.py` 完成启动、设置、导入、导出、备份等全部工作，再用简短的中文向管理员汇报结果。

系统服务于鄂尔多斯市实验中学的社团选课：管理员导入学生和社团、设定开放时间；到点后学生在网页上抢报，每人一个社团；截止后锁定名单，把未报名的学生调剂到有空位的社团，最后导出名单并备份。

报名方式有两种，按学校的实际安排选：

- **同时报名**：全体学生在同一时段报名，共用各社团的名额。
- **分批报名**：学生分几节课到机房报名时使用。按班级分批，各批在自己的时段报名，社团名额每批单独计算；同一批报名的学生通常也一起上课。见 4.6。

---

## 1. 工作方式与边界

**操作途径**

- 用 `omms` 服务提供的工具完成全部操作（第 5 节列出全部工具）。
- 客户端暂时连不上 MCP 时，用命令行入口，效果相同：

  ```bash
  python3 agent/omms_mcp.py call 工具名 '{"参数": "值"}'
  python3 agent/omms_mcp.py tools
  ```

**仓库文件只读**

- 程序、网页、`run.sh`、`nginx.conf`、`deploy/` 下的模板和本目录的文件都保持原样。
- 运行参数（端口、HTTPS、时区、导入上限等）用 `get_settings` 查看、`update_settings` 修改，写入 `deploy/omms.env`，重启服务后生效。
- 遇到需要修改程序才能解决的问题：记录现象、出现时间、相关日志（`service_logs`）和复现步骤，交给管理员转给维护者。

**需要管理员同意的操作**

下列工具先返回“待确认”和将要发生的变化，带 `confirm: true` 再次调用时执行：

| 操作 | 工具 |
|---|---|
| 报名进行中或一小时内开放时重启、停止服务 | `start_service`、`stop_service`、`update_settings(restart)` |
| 报名进行中锁定名单、改动开放时间 | `set_lock`、`set_schedule`、`clear_schedule` |
| 报名进行中或一小时内开放时改动报名方式或批次 | `set_batches` |
| 当前批次现在开放或现在截止 | `batch_now` |
| 导入名单 | `import_roster` |
| 重置学生密码 | `reset_passwords` |
| 删除学生、删除社团 | `delete_student`、`delete_club` |
| 执行调剂 | `adjustment(run)` |
| 恢复数据 | `restore` |
| 清空报名、删除全部学生或社团 | `clear_data` |

收到“待确认”后，用一两句话向管理员说明：要做什么、影响多少人或多少条记录、能否恢复（清空、调剂、恢复之前系统会自动保存快照，可以用 `restore` 回到操作前）。管理员明确同意后，带 `confirm: true` 再调用一次。

**数据与隐私**

- 学生名单、账号、密码和导出文件都保存在这台电脑上，只告知管理员本人。
- 对话里展示密码时，只展示管理员要的那部分；整班或整校的密码以 Excel 文件交付，告诉管理员文件路径。
- 是否把文件发到网盘、邮件或聊天软件，由管理员自己决定并操作。

**本机软件安装**

安装 Homebrew、Redis、nginx、Python 依赖或 Rust 属于环境准备：先说明要装什么、为什么装，征得同意后再执行。需要输入电脑登录密码的步骤（例如安装 Homebrew）由管理员本人在终端输入。

**汇报方式**

- 用中文，先说结果，再说下一步；给出具体数字、时间、地址和文件路径。
- 时间一律写学校时间，例如“11 月 20 日 08:00 开放”。
- 账号和密码放在代码格式里，方便辨认。

---

## 2. 系统组成与文件

| 组件 | 端口 | 作用 |
|---|---|---|
| nginx | 8080（`HTTP_PORT`），启用 HTTPS 时为 8443（`TLS_PORT`） | 学生和管理员访问的唯一入口：网页、限流、名额缓存 |
| Rust 热服务 | 2002（仅本机） | 登录、报名、退选等高并发接口；只运行 Python 时由 Python 承担 |
| Python 管理服务 | 2001（仅本机） | 管理接口和全部数据写入；本服务的工具都经它操作 |
| Redis | 6379（仅本机） | 名额和登录状态；重启后按数据库自动重建 |
| SQLite 数据库 | — | `club_system.db`，学生、社团、报名、操作记录全部在里面 |

| 文件或目录（相对程序目录） | 内容 |
|---|---|
| `club_system.db` | 数据库 |
| `admin-password.txt` | 管理员密码（仅本机用户可读）；控制台或 `change_admin_password` 修改密码时同步更新 |
| `backups/` | 自动快照：锁定、调剂、清空、恢复之前各存一份，保留最近 30 份（`BACKUP_KEEP`） |
| `club-images/` | 上传的社团图片 |
| `exports/` | 本服务导出的文件：账号表、报名表、完整备份 |
| `run/` | 运行日志与进程文件 |
| `deploy/omms.env` | 运行参数（由 `update_settings` 维护） |
| `启动.command`、`停止.command` | 管理员双击启动、停止 |

`service_status` 返回这些文件在本机的完整路径。

---

## 3. 接手时的第一步

1. 调用 `service_status`：看各组件是否运行、访问地址、报名状态、依赖是否齐全。
2. 服务需要启动时调用 `start_service`。返回里有学生访问地址、管理控制台地址、管理员账号和密码。
3. 调用 `get_overview`：报名状态、开放与截止时间、学生和报名人数、各社团名额，以及“流程”各步骤的状态。
4. 按“流程”里第一个“待办”的步骤，向管理员说明现在处在哪一步、下一步做什么。

---

## 4. 一次选课的完整流程

### 4.1 准备（开放前几天）

1. **导入学生**
   - 请管理员提供名单表格，第一行附近有“姓名”和“班级”两列（也认“学生姓名”“所在班级”“行政班”等写法，或“年级”+“班号”两列）。支持 xlsx、xls、csv、从网页另存的表格和 XML 表格；WPS 的 .et、Numbers 文件先另存为 xlsx。
   - `preview_roster(file_path)`：返回各班人数、略过的行（合计、说明、空姓名）和需核对的行。
   - 把需核对的行讲给管理员，常见标注：

     | 标注 | 含义 | 处理 |
     |---|---|---|
     | 待补班级 | 该行班级为空 | 问管理员班级，用 `fixes` 补上，或用 `skip` 跳过 |
     | 班级沿用上一行 | 班级单元格为空，按上一行（多为合并单元格）推断 | 核对后导入 |
     | 班级取自工作表名 | 表里只有姓名列，工作表名是“高二3班”这类班级名 | 核对后导入 |
     | 待核对姓名 | 姓名含数字或符号 | 用 `fixes` 改正，或 `skip` |
     | 同班同名 | 同一个班有两名同名学生 | 两人都会导入，账号自动区分 |
     | 已导入 | 系统里已有同名同班的学生 | 默认跳过；确属另一名同名学生时用 `include_imported: true` |

   - `import_roster(file_path, fixes, skip, confirm: true)`：按每批 200 人创建账号，返回导入人数和账号表 Excel 的路径（列为姓名、班级、账号、密码，按班分页）。把路径告诉管理员。
   - 账号由姓名拼音生成，重名时自动加数字（如 `lina`、`lina1`）。新账号密码 8 位，只用容易辨认的字母和数字，区分大小写。
   - 中途暂停时，已创建的账号在返回的账号表里；再次调用 `import_roster`，已导入的行自动跳过。

2. **添加社团**
   - `add_club(name, capacity, description, advisor, meeting_time, location, grades, classes)`。
   - 同时报名：`capacity` 是人数上限。各社团人数上限之和大于或等于学生人数时，调剂后每名学生都有社团；`get_overview` 的“流程”并列显示名额与学生人数。
   - 分批报名：`capacity` 是每批名额，通常就是这个社团一节课能容纳的人数（教室座位、设备数量）。每批名额之和大于或等于人数最多的一批时，调剂后每名学生都有社团。
   - 社团卡片显示管理员选定的插图。新社团按名称先选好一幅（例如“激光雕切”为激光雕刻、“3D设计”为 3D 建模），之后改名不影响已选的插图。`list_clubs` 的“图片”一列写明当前用哪幅。
   - 换插图：`update_club(club, art: "laser")`，可选 club（通用）、laser（激光雕刻）、design（平面设计）、model3d（3D 建模）、print（3D 打印）、media（影视传媒）、wood（木工）、code（编程）、robot（机器人）、paint（美术）、music（音乐）、science（科学实验）、sports（体育）、book（阅读写作）。
   - 换成照片：`update_club(club, image: "/完整路径/图片.jpg")`，照片优先于插图显示；改回插图：`image: "default"`。

3. **设置开放与截止时间**
   - 先确认报名方式：问管理员学生是一起报名，还是分几节课到机房报名。分几节课时用分批报名（4.6），时间在各批里设置。
   - 同时报名：`set_schedule(open_at, close_at)`，学校时间，例如 `"2026-11-20 08:00"`。
   - 到开放时间自动开放，到截止时间自动锁定名单；截止时间可以不设，届时手动锁定。
   - 管理员说相对时间（“明天早上八点”）时，先用 `get_overview` 的“服务器当前时间”换算成具体日期，再向管理员复述一遍确认。

4. **演练（建议）**
   - 先设一个很近的开放时间，请几位老师用学生账号试报。
   - 结束后：`set_lock(true)` → `clear_data(registrations, confirm)` → 设置正式开放时间 → `set_lock(false)`。

### 4.2 开放当天

- 开放前至少 30 分钟确认服务在运行：`service_status`、`get_overview`。Mac 接通电源；MacBook 保持开盖。
- 学生访问 `service_status` 返回的“学生访问”地址（同一局域网）。请学生提前几分钟登录：登录高峰时浏览器显示“排队中”并自动重试，最长 2 分钟。
- 报名进行中保持服务连续运行，参数修改安排在报名时段之外；管理员问进度时调用 `get_overview`。

### 4.3 在机房（电脑课）进行

- 服务器用网线接到机房交换机，和学生机在同一网段。学校的无线网与机房有线网常在不同网段，或开启了终端隔离。
- 服务器地址固定下来：请网管做 DHCP 保留，或在 Mac 的“系统设置 → 网络 → 以太网 → 详细信息 → TCP/IP”里手动设定网管给的地址。
- 防火墙允许 nginx 接受传入连接（第一次启动时 macOS 会询问）。
- 课前在一台学生机上打开学生访问地址，看到登录页即可。
- 把地址给到学生：
  - 控制台工作台的“学生访问”卡片显示地址；“放大显示”整屏大字，可用极域等电子教室软件的屏幕广播投到所有学生机。
  - `service_status` 与 `get_overview` 的“访问地址”是同一个地址。服务器同时接有线和无线时，另列“其他网卡”的地址，选和学生机同一网段的那个。
  - 地址想更短时，在报名时段之外把 `HTTP_PORT` 设为 80 并重启。
- 共用电脑：
  - 提醒学生用完点“退出”。
  - 可以把 `SESSION_TTL` 设为一节课的秒数（如 2700），下课后登录自动结束；这个设置同样作用于管理员的控制台。
- 学生机浏览器设置了代理服务器时，在“Internet 选项 → 连接 → 局域网设置”里勾选“对于本地地址跳过代理服务器”，或把服务器地址加入例外。

### 4.4 截止之后

1. 到截止时间自动锁定；也可以随时 `set_lock(true, confirm)`。
2. 调剂：`adjustment()` 查看未报名人数、各社团空余和将安排人数 → 向管理员说明 → `adjustment(run: true, confirm: true)`。调剂遵守年级和班级范围，随机安排，安排人数取最大值；空余名额小于未报名人数时，先提高人数上限或放宽范围。分批报名时每批只用本批的空余名额，预览按批列出。
3. 导出：`export_data(kind: "workbook")`，Excel 里有总表、各社团名单和未报名名单，“方式”一列区分自选、调剂和管理员安排。分批报名时每张表的第一列是批次，按批次排序，方便按节次打印。
4. 备份：`backup_now()`，建议管理员把这个 zip 另存到其他电脑或 U 盘。

### 4.5 下一轮或新学期

- 学生不变：`set_lock(true)` → `clear_data(registrations)` → 调整社团 → `set_schedule` → `set_lock(false)`。
- 学生更换：`clear_data(students)` 后重新导入；社团更换：`clear_data(clubs)` 后重新添加。每次清空前系统自动保存快照。

### 4.6 分批报名（学生分几节课到机房报名）

机房一次坐不下全部学生、各班轮流来报名时使用。每一批在自己的时段报名，只和同一批的学生竞争名额。

1. **确认安排**：向管理员问清每节课来哪几个班、几点开始、几点结束，以及每个社团一节课收多少人。
2. **导入全部学生**：和同时报名一样，一次导入全部班级。
3. **社团名额按批设置**：`update_club(club, capacity: 每批名额)`。例如每个社团一节课收 30 人，就设 30；4 批时这个社团合计收 120 人。
4. **设置批次**：`set_batches(batch_mode: true, batches: [...])`，给出全部批次：

   ```json
   {"batch_mode": true, "batches": [
     {"open_at": "2026-11-27 10:00", "close_at": "2026-11-27 10:40", "classes": ["高一1班", "高一2班"]},
     {"open_at": "2026-11-27 10:50", "close_at": "2026-11-27 11:30", "classes": ["高一3班", "高一4班"]}
   ]}
   ```

   - 批次按开放时间自动编号，第 1 批最早；后一批在前一批截止时或之后开放。
   - 每个班级只放在一批里。`get_overview` 的“待分批的班级”列出还没加入批次的班级，把它们加入某一批。
   - 改动某一批时也给出全部批次；`batches` 缺省时只改报名方式。
   - 管理员说“每节课来两个班、10 点开始、每节 40 分钟、课间 10 分钟”时，按班级顺序每两个班一批，依次排出各批时段，复述后保存。控制台的“快速分批”做的是同一件事。
5. **报名期间**：学生提前到齐时 `batch_now(batch, action: "open")` 让这一批现在开放；提前下课时 `batch_now(batch, action: "close")` 现在截止。当前批次截止后系统自动切换到下一批；学生页显示本班所在批次的倒计时和截止时间。`get_overview` 的“报名状态”写明当前批次，例如“第 2 批报名中”，“批次”列出各批的人数和状态。
6. **最后一批截止后**：名单自动锁定并保存快照，按 4.4 调剂、导出、备份。
7. **改回同时报名**：`set_batches(batch_mode: false)`，再用 `set_schedule` 设置开放时间。已保存的批次保留，之后改回分批报名时继续使用。

切换报名方式或改批次时，系统核对每个名额池里的报名人数：某社团在某一批的报名人数超过每批名额时，返回提示，先按提示提高名额再保存。已经用同时报名报过名、准备改成分批报名时，常见做法是：`set_lock(true)` → `clear_data(registrations)` → `set_batches` → 设置每批名额 → `set_lock(false)`。

---

## 5. 工具一览

| 工具 | 用途 | 主要参数 |
|---|---|---|
| `service_status` | 组件状态、访问地址、报名状态、依赖、文件路径 | — |
| `start_service` | 启动或平滑重启全部服务 | `open_browser`、`python_only`、`wait_seconds`、`confirm` |
| `stop_service` | 停止服务，数据保留 | `confirm` |
| `service_logs` | 日志末尾 | `source`: python、rust、nginx、access、redis、build、start；`lines` |
| `open_console` | 在本机浏览器打开并自动登录控制台 | `open_browser` |
| `get_settings` / `update_settings` | 查看、修改运行参数 | `values`、`restart`、`confirm` |
| `admin_credentials` | 管理员账号、密码、控制台地址 | — |
| `change_admin_password` | 修改管理员密码（缺省随机 12 位） | `new_password` |
| `get_overview` | 报名概览、报名方式与各批次、流程状态 | — |
| `set_schedule` / `clear_schedule` | 同时报名的开放与截止时间 | `open_at`、`close_at`、`confirm` |
| `set_batches` | 报名方式与批次（各批的时段和班级） | `batch_mode`、`batches`、`confirm` |
| `batch_now` | 当前批次现在开放或现在截止 | `batch`、`action`、`confirm` |
| `set_lock` | 锁定或解除锁定名单 | `locked`、`reason`、`confirm` |
| `preview_roster` / `import_roster` | 预览、导入名单 | `file_path`、`fixes`、`skip`、`include_imported`、`output_dir`、`reveal`、`confirm` |
| `list_students` | 查找学生 | `query`、`class`、`club`、`status`、`limit`、`offset` |
| `update_student` | 改姓名班级、代为报名、调整或移出社团 | `student`、`name`、`class`、`club`、`ignore_limits`、`reason` |
| `reset_passwords` | 重置单个学生或整班密码 | `student` 或 `class`、`save_excel`、`confirm` |
| `delete_student` | 删除学生 | `student`、`confirm` |
| `list_clubs` / `add_club` / `update_club` / `delete_club` | 社团管理、卡片插图与照片 | `club`、`name`、`capacity`、`description`、`advisor`、`meeting_time`、`location`、`grades`、`classes`、`enabled`、`art`、`image` |
| `adjustment` | 调剂预览与执行 | `run`、`confirm` |
| `export_data` | 导出报名表、CSV、社团名单 | `kind`、`club`、`output_dir`、`reveal` |
| `audit_log` | 操作记录 | `role`: admin、student、all；`limit` |
| `backup_now` / `list_snapshots` / `restore` | 备份与恢复 | `output_dir`、`snapshot`、`file_path`、`confirm` |
| `clear_data` | 清空报名、删除全部学生或社团 | `target`、`confirm` |

约定：

- 学生用账号指定最可靠；也可以写“姓名 班级”或“姓名（班级）”。重名时工具会列出候选账号。
- 社团用名称或编号指定。
- 文件参数一律用完整路径（可以 `~/` 开头）。管理员把文件拖进对话框或终端时，通常就得到完整路径。
- 生成的文件默认放在程序目录的 `exports/`；`output_dir` 可以改到桌面等位置；`reveal: true` 在访达里选中文件。
- 工具返回中文说明时，按说明调整参数后再试。

---

## 6. 常见请求与做法

| 管理员说 | 做法 |
|---|---|
| 启动 / 打开选课系统 | `service_status` → `start_service(open_browser: true)` → 告知学生访问地址和控制台地址 |
| 管理员密码是多少 / 打印默认密码 | `admin_credentials` → 给出账号和密码 |
| 改管理员密码 | `change_admin_password`（管理员指定或随机生成）→ 给出新密码；已打开的控制台需用新密码重新登录 |
| 打开管理后台 | `open_console` |
| 学生用什么地址 / 机房里怎么让学生打开 | `service_status` 的“访问地址”；控制台工作台“学生访问”卡片的“放大显示”可用于屏幕广播；做法见 4.3 |
| 明天 8 点开放、17 点截止 | 换算日期并复述 → `set_schedule` |
| 分几节课报名 / 每节课来两个班 | 问清各批的班级和时段、每个社团一节课收多少人 → `update_club(capacity)` → `set_batches(batch_mode: true, batches)`；做法见 4.6 |
| 第二批的学生到齐了，现在开始 / 这节课提前结束 | `batch_now(batch: 2, action: "open", confirm)` / `batch_now(batch, action: "close", confirm)` |
| 第二批推迟十分钟 | `get_overview` 取出全部批次 → 改第二批及之后各批的时间 → `set_batches`（给出全部批次） |
| 不分批了，大家一起报 | `set_batches(batch_mode: false)` → `set_schedule` |
| 推迟或提前开放 | `set_schedule(open_at)`；报名已在进行时需管理员确认 |
| 现在就开放 | `set_schedule(open_at: 当前服务器时间)`；名单锁定时再 `set_lock(false)` |
| 提前结束报名 | `set_lock(true, confirm)` |
| 导入这份名单 | `preview_roster` → 讲需核对的行 → `import_roster(..., confirm)` → 给出账号表路径 |
| 某学生需要新密码 | `reset_passwords(student, confirm)` → 给出新密码（12 位） |
| 某班需要新的账号表 | `reset_passwords(class, confirm)` → 给出新的账号表路径；该班使用新密码登录 |
| 帮某学生换到木工 | `update_student(student, club: "木工", reason)` |
| 帮某学生报名 | `update_student(student, club)`；社团已满时先问是否提高人数上限（`update_club(capacity)`） |
| 把某学生移出社团 | `update_student(student, club: "")` |
| 改学生姓名或班级 | `update_student(student, name, class)` |
| 木工名额改成 50 | `update_club(club: "木工", capacity: 50)`；人数上限至少为已报名人数（分批报名时为每批名额，至少为各批中报名最多的人数） |
| 加一个社团 / 删社团 | `add_club` / `delete_club(confirm)` |
| 只让高一报 / 只让某几个班报 | `update_club(grades: ["高一"])` / `update_club(classes: [...])` |
| 暂停某社团报名 | `update_club(enabled: false)` |
| 换社团插图 / 用照片 / 改回插图 | `update_club(art: "laser")` / `update_club(image: 路径)` / `update_club(image: "default")` |
| 现在报了多少人 | `get_overview` |
| 导出名单 / 导出某社团名单 | `export_data(kind: "workbook")` / `export_data(kind: "club", club)` |
| 查看未报名的学生 | `list_students(status: "unregistered")` 或 `export_data(kind: "unregistered")` |
| 备份一下 | `backup_now` |
| 恢复到某个时候的数据 | `list_snapshots` → 与管理员确认是哪一份 → `restore(snapshot, confirm)` |
| 刚才误删了 / 调剂错了 | `list_snapshots` 找操作前的快照 → `restore(snapshot, confirm)` |
| 换一台电脑做服务器 | 旧电脑 `backup_now` → 新电脑装好环境并启动 → `restore(file_path, confirm)` |
| 网址里不想带 :8080 | 报名时段之外 `update_settings(values: {"HTTP_PORT": 80}, restart: true)` |
| 从校外访问 / 用内网穿透 | 见第 7 节 `TLS_CERT`、`TLS_KEY`、`REAL_IP_FROM` |
| 谁改过什么 | `audit_log(role: "admin")`；学生的报名与退选用 `role: "student"` |
| 关掉系统 | `stop_service`（报名期间需管理员确认） |

---

## 7. 运行参数

`get_settings` 列出全部参数的当前值、默认值和来源；`update_settings(values: {键: 值})` 写入 `deploy/omms.env`，值为 `null` 或空字符串时恢复默认。修改后重启服务（`start_service` 或 `update_settings` 带 `restart: true`）才生效；报名进行中重启需管理员确认，尽量安排在报名时段之外。

| 键 | 默认 | 何时调整 |
|---|---|---|
| `HTTP_PORT` | 8080 | 地址只写 IP 时设为 80；8080 已被其他程序使用时换一个 |
| `TLS_CERT` / `TLS_KEY` | 空 | 从校外经公网访问、已有证书时设置，两者同时设置 |
| `TLS_PORT` | 8443 | HTTPS 端口；设为 443 时地址只写域名或 IP |
| `REAL_IP_FROM` | 空 | 经内网穿透或反向代理访问时，填代理所在的地址（如 `127.0.0.1`），按学生真实地址限流 |
| `REAL_IP_HEADER` | X-Forwarded-For | Cloudflare Tunnel 填 `CF-Connecting-IP` |
| `APP_UTC_OFFSET` | +08:00 | 学校所在时区；中国境内保持默认 |
| `OMMS_PYTHON_ONLY` | 0 | 只运行 Python 服务时设为 1 |
| `OMMS_OPEN_BROWSER` | 1 | 双击启动后是否打开浏览器 |
| `PYTHON` | python3 | 装了多个 Python、依赖装在其中一个时，填它的完整路径 |
| `BACKUP_KEEP` | 30 | 自动快照保留份数 |
| `MAX_IMPORT_STUDENTS` | 500 | 单次导入请求的上限（本服务每批 200 人） |
| `SESSION_TTL` | 28800 | 登录保持秒数（默认 8 小时） |
| `LOGIN_MAX_FAILS` | 10 | 同一账号在同一设备上一分钟内允许输错的次数 |
| `AUTH_CONCURRENCY` | 自动 | 内存 2 GB 以下的旧电脑设为 2 |
| `MAX_HTTP_WORKERS` | 128 | Python 服务线程上限，一般保持默认 |
| `SEATS_CACHE_MS` | 500 | 名额快照缓存毫秒数，一般保持默认 |
| `LOG_LEVEL` | INFO | 排查问题时临时设为 DEBUG |

---

## 8. 故障排查

**服务**

- **`service_status` 显示有组件已停止**：调用 `start_service`，读返回的“输出”；需要排查时用 `service_logs` 查看对应日志（python、rust、nginx、redis、build、start）。
- **提示安装依赖**：按 `service_status` 的“下一步”安装（征得同意）：

  ```bash
  brew install redis nginx
  python3 -m pip install redis argon2-cffi pypinyin Pillow
  ```

  安装 Homebrew：请管理员按 brew.sh 首页的说明操作（需要输入电脑登录密码）。Rust 可选：装有 cargo 时同时运行 Rust 热服务。
- **`start_service` 返回“启动进行中”**：首次编译 Rust 在旧电脑上需要几分钟。稍后调用 `service_status`，或 `service_logs(source: "start")` 看进度。等不及时 `start_service(python_only: true)`。
- **端口被占用**（nginx 日志里有 `Address already in use`）：换一个 `HTTP_PORT`，或请管理员关闭占用端口的程序。
- **电脑重启或断电后**：数据在数据库里，`start_service` 即可恢复。
- **Mac 睡眠**：服务运行期间系统用 `caffeinate` 阻止闲置睡眠；接通电源，MacBook 保持开盖。

**学生访问**

- **让学生机打开网页**：
  1. 学生和服务器在同一局域网，地址用 `service_status` 返回的“学生访问”（服务器的局域网地址可能随网络变化，以最新状态为准）。
  2. 地址以 `http://` 开头（启用 HTTPS 时为 `https://`），带上端口。
  3. 第一次启动时 macOS 会询问是否允许 nginx 接受传入连接，选“允许”；也可以在“系统设置 → 网络 → 防火墙 → 选项”里把 nginx 设为允许。
  4. 学校无线网开启终端隔离时，服务器改用有线网，或请学校网管放行。
- **页面提示换浏览器**：360、QQ、搜狗浏览器切换到极速模式，或改用 Edge、Chrome。
- **登录时显示“排队中”**：登录高峰的正常排队，浏览器自动重试，最长 2 分钟。
- **登录提示“请核对账号和密码”**：密码区分大小写；同一设备连续输入 10 次后暂停一分钟；需要时 `reset_passwords(student)` 生成新密码。
- **学生页显示“开放时间待定”**：同时报名时用 `set_schedule` 设置开放时间；分批报名时这名学生的班级待分批，用 `set_batches` 把班级加入某一批。显示“报名已截止”：名单已锁定，或已到截止时间（分批报名时为本批的截止时间）。
- **分批报名时学生说“报名待开放”**：这名学生的批次还没到，学生页有本批的开放时间和倒计时。
- **报名时显示“名额更新中”**：系统正在核对该社团的名额，几秒内自动恢复，浏览器会自动重试。持续一分钟以上时查看 `service_logs(source: "python")`。

**时间**

- 开放、截止都按服务器时钟和 `APP_UTC_OFFSET` 计算。需要校准 `get_overview` 的“服务器当前时间”时，请管理员在 Mac 的“系统设置 → 通用 → 日期与时间”打开自动设置时间。
- 控制台工作台提示“服务器时间比本机快/慢”时，比较的是服务器与打开控制台的那台电脑；以服务器为准，必要时校准其中慢的一台。

**导入**

- **提示“表头需包含姓名和班级”**：表头行需在前 40 行内并含“姓名”。请管理员把名单整理成“姓名”“班级”两列。
- **提示“请上传 xlsx、xls、csv 或网页表格”**：图片、PDF 或加密表格请另存为 xlsx 或 csv。
- **预览里的“略过的行”**：合计、小计、说明和空姓名的行。

**数据**

- **误操作后恢复**：`list_snapshots` 列出自动快照（名称含 locked、before-adjust、before-clear、before-delete-students、before-delete-clubs、before-restore）→ 与管理员确认 → `restore(snapshot, confirm)`。恢复后学生需重新登录。
- **重新取得管理员密码**：`admin_credentials` 核对密码文件与数据库，需要时生成新密码并返回。

---

## 9. 在 AI 客户端中连接本服务

本服务是标准 MCP 服务（stdio），只用 Python 标准库，命令为 `python3 <程序目录>/agent/omms_mcp.py`。

| 客户端 | 做法 |
|---|---|
| Claude Code（终端或 Claude 桌面版的 Code） | 在 `agent` 文件夹中打开，`.mcp.json` 自动登记 omms，按提示允许即可；或执行 `claude mcp add --scope user omms -- python3 <完整路径>/agent/omms_mcp.py` |
| Claude 桌面版（对话） | 执行 `python3 agent/omms_mcp.py register claude-desktop`，完全退出并重新打开 Claude |
| Codex | 执行 `python3 agent/omms_mcp.py register codex`，或在 `agent` 文件夹中启动 Codex 读取本指南 |
| Cursor、VS Code | 打开 `agent` 文件夹，`.cursor/mcp.json`、`.vscode/mcp.json` 已写好 |
| 其他客户端 | `python3 agent/omms_mcp.py register show` 输出各种配置写法 |

连接后，客户端的工具列表里出现 `omms` 的 32 个工具；本指南也作为资源 `omms://guide` 提供。

---

## 10. 时间格式与“择时开放”

- `open_at`、`close_at` 接受 `2026-11-20 08:00`、`2026/11/20 8:00`、`2026-11-20T08:00:00`、`2026年11月20日 08:00`。
- 系统到开放时间自动开放，前提是服务在运行。服务空闲时占用的资源很少，可以提前一天启动。
- 截止时间到达后自动锁定名单，锁定时自动保存快照。
- 开放时间设在过去时，保存后立即开放；工具返回里会说明。

---

## 11. 报名流程里的规则（便于回答管理员和学生的问题）

- 每名学生报一个社团；锁定前可以退选后改报。
- 报名按先到先得，名额在服务器上一步扣减，报名人数始终在人数上限以内。
- 社团可以限定年级（如只收高一）、班级，或暂停报名；学生端会显示限制。
- 调剂只在锁定后进行，只安排未报名的学生，遵守社团的年级、班级范围。
- 分批报名时，学生在本班所在批次的时段内报名和退选，只和同一批的学生竞争名额；社团的人数上限是每批名额；调剂时每批只用本批的空余名额。
- 管理员代为报名、调整、移出都会记入操作记录，并在报名表的“方式”一列标为管理员安排。
- 学生的密码由管理员重置（`reset_passwords`）。
