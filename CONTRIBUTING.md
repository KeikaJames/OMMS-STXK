# 贡献指南

感谢参与。提交前请读完本页。

## 提交方式

改动通过 Pull Request 合并到 `main`。

1. Fork 仓库或新建分支，例如 `fix/seat-repair`、`feat/export-roster`。
2. 一个 PR 完成一件事，描述里写清做了什么、为什么、怎样验证。
3. 维护者审阅后合并。

## 代码约定

**后端**

- Python 服务使用标准库与内联 SQL。`pypinyin`、`Pillow` 为可选依赖，代码保留降级路径。
- Rust 服务通过 `cargo fmt` 与 `cargo clippy -- -D warnings`。
- 4 空格缩进，`snake_case`。注释精简，说明原因。

**共享协议**

- Redis 写操作写成 `shared/lua/` 里的脚本，Python 与 Rust 调用同一份文件。
- 返回码与提示文案写在 `shared/messages.json`，两个服务都从这里读取。
- 新增 Redis 键、返回码或会话字段时，同时修改两个服务并补测试。

**前端**

- 页面在 `web/*.html`，脚本在 `web/js/`，样式在 `web/app.css`。
- 脚本和样式写在独立文件里，页面的 CSP 为 `script-src 'self'; style-src 'self'`。
- 颜色、圆角、阴影使用 `:root` 中的变量，并在深色模式下同步定义。
- 修改静态资源后，更新页面里对应的 `?v=` 版本号。
- `web/js/legacy.js` 只用 ES3 语法，其余脚本以 Chrome 57、Safari 10.1 为下限；CSS 新特性要先写退回写法。
- 界面文案直接写用户能做什么、结果是什么。

## 提交前检查

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
cargo test --manifest-path club-hot/Cargo.toml
cargo clippy --manifest-path club-hot/Cargo.toml --all-targets -- -D warnings
cargo fmt --manifest-path club-hot/Cargo.toml --check
ruff check main.py tests
node --check web/js/*.js
shellcheck run.sh
```

改动涉及登录或报名路径时，再运行压测：

```bash
python3 tests/stress_registration.py --seats 100 --users 1000 --concurrency 1000
python3 tests/stress_site.py login --backend python --users 1000
python3 tests/stress_site.py hot --backend rust --users 1000
```

数据库、学生名单、密码和 `run/` 目录保留在本地。

提交贡献即表示同意以 [Apache License 2.0](LICENSE) 发布。
