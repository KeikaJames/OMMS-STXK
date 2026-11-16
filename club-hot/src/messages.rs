//! 与 Python 共用的提示文案：shared/messages.json。

use std::collections::HashMap;
use std::sync::LazyLock;

use axum::Json;
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde_json::{Map, Value};

static MESSAGES: LazyLock<HashMap<String, String>> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../../shared/messages.json"))
        .expect("shared/messages.json must be valid")
});

pub fn text(code: &str) -> String {
    MESSAGES
        .get(code)
        .cloned()
        .unwrap_or_else(|| code.to_string())
}

/// 统一的业务响应体：success、code、message，再加附加字段。
pub fn body(code: &str, success: bool, extra: Option<Map<String, Value>>) -> Value {
    let mut map = Map::new();
    map.insert("success".into(), Value::Bool(success));
    map.insert("code".into(), Value::String(code.to_string()));
    map.insert("message".into(), Value::String(text(code)));
    if let Some(extra) = extra {
        map.extend(extra);
    }
    Value::Object(map)
}

pub fn reply(status: StatusCode, code: &str, success: bool) -> Response {
    let mut response = (status, Json(body(code, success, None))).into_response();
    if matches!(status.as_u16(), 202 | 429 | 503) {
        response
            .headers_mut()
            .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
    }
    response
}

pub fn fail(status: StatusCode, code: &str) -> Response {
    reply(status, code, false)
}

#[cfg(test)]
mod tests {
    #[test]
    fn every_code_used_by_rust_has_a_message() {
        for code in [
            "registered",
            "registered_already",
            "registered_other",
            "cancelled",
            "full",
            "not_open",
            "locked",
            "club_missing",
            "club_disabled",
            "ineligible_grade",
            "ineligible_class",
            "ineligible_config",
            "syncing",
            "pending",
            "queued",
            "unavailable",
            "not_registered",
            "bad_request",
            "unauthorized",
            "login_failed",
            "login_empty",
            "login_throttled",
            "login_queued",
            "account_changed",
            "server_error",
        ] {
            assert_ne!(super::text(code), code, "missing message for {code}");
        }
    }
}
