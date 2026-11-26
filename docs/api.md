# 学生端 API

以下接口由学生页面使用。管理端接口由 Python 服务提供，路由表见 `main.py` 中的 `GET_ROUTES`、`POST_ROUTES` 和 `RAW_POST_ROUTES`。

| 方法与路径 | 说明 |
|---|---|
| `POST /api/login` | 学生登录并签发会话 Cookie |
| `GET /api/catalog` | 社团目录，带 ETag |
| `GET /api/seats` | 名额快照、开放时间与锁定状态，带 ETag |
| `GET /api/check_registration_time` | 服务器时间与开放时间 |
| `POST /api/register_club` | 报名，参数为 `club_id`、`request_id` |
| `POST /api/cancel_registration` | 退选 |
| `GET /api/get_student_info` | 个人信息与报名结果 |

POST 请求需要 `X-Requested-With: XMLHttpRequest` 请求头。

响应包含 `success`、`code`、`message`：

- `200`：业务结果，例如 `registered`、`full`、`not_open`、`registered_other`。
- `202 pending`：请求已受理，服务器正在确认结果。
- `503`：服务繁忙或名额正在更新；按 `Retry-After` 使用同一 `request_id` 重试。

提示码和显示文案见 `shared/messages.json`。
