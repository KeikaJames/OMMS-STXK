//! 统一错误类型，转换为带 code 的 HTTP 响应。

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};

use crate::messages;

#[derive(Debug)]
pub enum AppError {
    /// Redis 不可达；写路径返回 503。
    RedisDown,
    Db(String),
    Internal(String),
}

impl std::fmt::Display for AppError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AppError::RedisDown => write!(f, "redis unavailable"),
            AppError::Db(m) => write!(f, "db error: {m}"),
            AppError::Internal(m) => write!(f, "internal error: {m}"),
        }
    }
}

impl std::error::Error for AppError {}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        match &self {
            AppError::RedisDown => messages::fail(StatusCode::SERVICE_UNAVAILABLE, "unavailable"),
            AppError::Db(m) | AppError::Internal(m) => {
                tracing::error!(error = %m, "request failed");
                messages::fail(StatusCode::INTERNAL_SERVER_ERROR, "server_error")
            }
        }
    }
}

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
