<div align="center">

# 社团选课系统 · OMMS-STXK

开抢时间一到,一个年级几百名学生在同一秒争抢有限的社团名额。这是一套为这种场景写的选课系统。

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-stdlib-3776AB?logo=python&logoColor=white)
![Rust](https://img.shields.io/badge/Rust-hot_path-CE422B?logo=rust&logoColor=white)

<sub>为 <b>鄂尔多斯市实验中学</b> 而做</sub>

<img src="docs/login.jpg" alt="登录页" width="760">

</div>

## 这是一个秒杀问题

抢课的难点不在功能多,而在那一瞬间的并发。一个 30 人的社团,可能有 300 人同时点"报名"。系统要做的事只有一件,但必须做对:在所有人里挑出前 30 个,一个不多——不能把 30 人的社团塞进 31 个人。

把这件事做对,是这套系统的全部设计意图。下面讲它是怎么做到的,以及怎么用。

## 怎么实现的

**名额的真相只有一处。** 每个社团的剩余名额是一个 Redis 整数键 `stock:club:{id}`。SQLite 里也有一列 `current_students`,但那只是给管理后台看的派生镜像——判断"还有没有名额",永远以 Redis 为准。把真相收敛到一个地方,才谈得上让它原子。

**抢占是一段 Lua 脚本,在 Redis 里一步完成。** 报名请求到达时,服务端不是先查询、再判断、再扣减——那样会在两步之间留下缝隙,让两个人同时看到"还剩 1 个"。它把整个判断交给一段 Lua,Redis 单线程逐条执行,中途不会插进别的请求:

```lua
if redis.call('EXISTS', KEYS[1]) == 0 then return -2 end   -- 名额未初始化
if redis.call('EXISTS', KEYS[2]) == 1 then return -1 end   -- 已确认报名
if redis.call('EXISTS', KEYS[3]) == 1 then return -1 end   -- 已有在途占位
local left = tonumber(redis.call('GET', KEYS[1]))
if left <= 0 then return 0 end                             -- 满员
redis.call('DECR', KEYS[1])                                -- 扣一个名额
redis.call('SET', KEYS[3], ARGV[1], 'EX', tonumber(ARGV[2]))  -- 占位,带 TTL
return 1                                                    -- 抢到
```

"查重 → 看还剩几个 → 扣减 → 占位"四步,要么全做、要么全不做。所以两个人不可能同时扣到最后一个名额:Redis 让其中一个先跑完拿到 1,另一个再跑时 `left` 已是 0,只能拿到"满员"。一人一社也在这里保证——已报名或已有在途占位的人,脚本第一关就把他挡回去。

**抢到之后,先占位、再落库。** 脚本返回 1 只是抢到了"预留",这个预留写在 `resv:{id}` 上,带一个十几秒的 TTL。接着服务端才把这条报名写进 SQLite 做持久记录,写成功后清掉预留、把 `student:reg:{id}` 置实。那个 TTL 是兜底:万一落库这一步卡住或进程没了,预留会自己过期,名额不会被一个没写成的报名永久占着。

**两个服务,一套键。** 管理后台用 Python 写(`main.py`):导名单、建社团、设时间、看进度、导表——都是低频操作,标准库加 SQLite 足够,零第三方框架。抢课热路径另用 Rust 写(`club-hot/`,axum + Redis 连接池),扛开抢瞬间的并发。两者共享同一个 Redis(键的契约逐字对齐,Lua 脚本两边一模一样)和同一个 SQLite 文件,所以它们是同一个系统的两张面孔,可以互换。最外层 nginx 做三件事:静态页面直接发、不惊动后端;按客户端限流当"等候室";把六个热端点转给 Rust,其余转给 Python。Rust 没构建或挂了,nginx 自动回落到 Python 接管热路径。

**代价说在前面。** 这套设计防的是超卖——绝不会发超。但它换来一个相反方向的、极小的风险:如果进程恰好在"扣了名额"和"落库成功"这两步之间被强杀,那个名额已经在 Redis 减掉、却没有对应的数据库记录,于是会**少卖一个**。这不影响公平,也不会有人凭空多占,只是一个空座没放出来。系统不假装这不会发生:每次重启,它以 SQLite 里的实际报名记录为准,重算每个社团的 `max - 已用`,覆盖回 Redis——对账一次,偏差归零。

## 怎么用

**管理员**(`/admin/dashboard`,登录时切到"管理员"):

1. **导入学生名单**——粘贴或上传(姓名、班级、学号)。系统为每人生成账号和随机密码,**这批密码只在导入完成时显示这一次**,请当场导出、线下发给学生。
2. **建社团、设名额**——逐个建,或批量导入(社团名 + 容量)。
3. **设开抢时间**——到这个时间点之前,后端拒绝一切报名;到点自动放行。
4. **开抢后**——实时看各社团报名进度、导出报名表、导出未报名名单、按社团下载名册。学生账号密码表也在这里导出。

**学生**(`/student/dashboard`):用老师发的账号登录,页面有倒计时和每个社团的实时余额。到点点"报名";已报名的可以在"个人信息"里退选、改选——始终一人一社。手机、电脑都能用。

<div align="center"><img src="docs/dashboard.png" alt="学生抢课页" width="760"></div>

## 跑起来

单机起 Python 一个进程就能用,适合开发和小规模:

```bash
pip install redis argon2-cffi pypinyin   # 三个都可选,缺了会降级;装上才有完整的抢占与口令哈希
python3 main.py                          # 打开 http://127.0.0.1:2001
```

首次启动会在运行窗口打印一行随机管理员密码,抄下、登录、尽快改掉。

要完整的双服务(nginx 限流 + Rust 热路径 + Redis),一键编排:

```bash
bash run.sh                              # 打开 http://127.0.0.1:8080
```

`run.sh` 会拉起 Redis、Python(:2001)、Rust(:2002,若已用 `cargo build --release` 构建)和 nginx(:8080),并把仓库路径注入临时 nginx 配置。Rust 未构建时,nginx 自动让 Python 顶上热路径。

## 上线前

- **生产必须开 HTTPS。** 自带的 `nginx.conf` 是开发用的明文 8080;正式部署改 443 + TLS,否则密码明文过网。
- **初始密码一次性下发。** 管理员密码首启打印一次,学生密码导入时返回一次——系统不长期保存明文,丢了只能重置或重新导入。
- **校园限流要留余量。** 全校常共用一个出口 IP,按 IP 限流容易误伤,默认放得较宽(30r/s);按规模再调,或给校园网段加白名单。

## 项目结构

```
main.py        Python 管理后台 + 热路径后备(标准库 + SQLite)
club-hot/      Rust 热服务(axum + Redis,可选)
web/           前端(纯 HTML/CSS,无构建步骤)
nginx.conf     边缘限流 / 静态 / 路由        run.sh  一键编排
```

## 参与贡献

欢迎 PR。本项目只接受 Pull Request、不直推 `main`,并有代码风格约定——动手前请读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 作者与许可

在 **Vles0123** 的原始版本之上,由 **BIRI GA([KeikaJames](https://github.com/KeikaJames))** 经原作者授权强化改造(并发抢占、安全加固、界面重做),以 [Apache-2.0](LICENSE) 分发,署名见 [NOTICE](NOTICE)。
