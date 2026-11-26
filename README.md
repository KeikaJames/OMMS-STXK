<div align="center">

# 社团选课 · OMMS-STXK

面向中学社团选课的报名系统：定时开放、名额管理、名单锁定、调剂与导出。

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.7%2B-3776AB?logo=python&logoColor=white)
![Rust](https://img.shields.io/badge/Rust-1.88%2B-CE422B?logo=rust&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-3.2%2B-DC382D?logo=redis&logoColor=white)

<img src="docs/login.jpg" alt="登录页" width="760">

</div>

<div align="center">
<img src="docs/dashboard.jpg" alt="管理控制台" width="560">&nbsp;&nbsp;<img src="docs/mobile.jpg" alt="手机上的选课页" width="180">
</div>

## 功能

**学生端**

- 按服务器时间开放报名，显示倒计时、社团信息与实时名额。
- 支持全体同时报名或按班级分批报名；名单锁定前可退选和改报。

**管理端**

- 导入学生名单并核对识别结果，生成账号表。
- 管理社团名额、介绍、老师、时间地点、年级班级范围与卡片图片。
- 设置报名时段，锁定名单后调剂未报名学生。
- 导出报名表，查看操作记录，备份或恢复数据。

## 快速开始

### 本机开发：Python + Redis

```bash
pip install redis argon2-cffi pypinyin Pillow
redis-server --port 6379 --save "" --daemonize yes
python3 main.py
```

打开 `http://127.0.0.1:2001`。首次启动时生成管理员密码，保存在数据库旁的 `admin-password.txt`，权限为 `0600`。

### 完整部署

Mac 或 Linux 上执行：

```bash
bash run.sh
```

部署依赖、服务管理、HTTPS 与网络配置见[部署指南](docs/deployment.md)。只运行 Python 服务时执行 `OMMS_PYTHON_ONLY=1 bash run.sh`。

## 使用流程

1. 导入学生名单并检查待核对的行。
2. 添加社团，设置人数上限和报名范围。
3. 设置开放时间；分批报名时按班级设置批次与时段。
4. 报名截止后锁定名单，预览并执行调剂。
5. 导出报名表并下载备份。

学生在开放前登录，到开放时间后选择一个社团；锁定前可以退选后改报。

## 测试与压测

运行隔离的回归测试：

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
cargo test --manifest-path club-hot/Cargo.toml
```

压测方法、环境与实测结果见[测试说明](tests/README.md)。

## 可选：AI/MCP 集成

`agent/` 提供 MCP 服务，可从 Claude、Codex、Cursor 等客户端运行管理工具。接入步骤、工具范围与操作规则见[代理运行指南](agent/AGENTS.md)。

## 架构与开发

系统由 nginx、Rust 热服务、Python 管理服务、Redis 和 SQLite 组成。报名接口以 Redis 控制并发名额，以 SQLite 保存报名记录。

- [系统设计](docs/architecture.md)
- [开发与调试](docs/development.md)
- [学生端 API](docs/api.md)

## 参与贡献

欢迎提交 Pull Request。提交前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；开发和调试说明见 [docs/development.md](docs/development.md)。

## 作者与许可

由 **BIRI GA（[KeikaJames](https://github.com/KeikaJames)）** 在 **Vles0123** 的原始版本上经授权重写和扩展，以 [Apache-2.0](LICENSE) 分发，署名见 [NOTICE](NOTICE)。
