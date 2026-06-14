//! Unified error type that converts into HTTP responses.

use axum::Json;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::json;

#[derive(Debug)]
pub enum AppError {
    /// Bad request body / params (400).
    BadRequest(String),
    /// Missing / invalid session (401).
    Unauthorized,
    /// Redis is unreachable. Write endpoints MUST reject (503) rather than fall
    /// back to an unlocked SQLite path (that would reintroduce oversell).
    RedisDown,
    /// SQLite pool / query failure (500).
    Db(String),
    /// Generic internal error (500).
    Internal(String),
}

impl std::fmt::Display for AppError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AppError::BadRequest(m) => write!(f, "bad request: {m}"),
            AppError::Unauthorized => write!(f, "unauthorized"),
            AppError::RedisDown => write!(f, "redis unavailable"),
            AppError::Db(m) => write!(f, "db error: {m}"),
            AppError::Internal(m) => write!(f, "internal error: {m}"),
        }
    }
}

impl std::error::Error for AppError {}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, message) = match &self {
            AppError::BadRequest(m) => (StatusCode::BAD_REQUEST, m.clone()),
            AppError::Unauthorized => (StatusCode::UNAUTHORIZED, "未登录或会话已过期".to_string()),
            AppError::RedisDown => (
                StatusCode::SERVICE_UNAVAILABLE,
                "服务暂时不可用，请稍后重试".to_string(),
            ),
            AppError::Db(m) => {
                tracing::error!(error = %m, "db error");
                (StatusCode::INTERNAL_SERVER_ERROR, "服务器错误".to_string())
            }
            AppError::Internal(m) => {
                tracing::error!(error = %m, "internal error");
                (StatusCode::INTERNAL_SERVER_ERROR, "服务器错误".to_string())
            }
        };
        (status, Json(json!({ "success": false, "message": message }))).into_response()
    }
}

/// Convenience: any deadpool/redis/rusqlite error -> AppError::Internal at call
/// sites that don't need finer granularity.
impl From<deadpool_sqlite::PoolError> for AppError {
    fn from(e: deadpool_sqlite::PoolError) -> Self {
        AppError::Db(format!("sqlite pool: {e}"))
    }
}

impl From<rusqlite::Error> for AppError {
    fn from(e: rusqlite::Error) -> Self {
        AppError::Db(format!("rusqlite: {e}"))
    }
}

pub type AppResult<T> = Result<T, AppError>;
