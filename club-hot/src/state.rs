//! Shared application state: the two SQLite pools (via [`Db`]), the Redis pool,
//! and process-wide config. Cloned cheaply into every handler (all fields are
//! `Arc`-like: pools are `Clone`, config is wrapped in `Arc`).

use std::sync::Arc;

use deadpool_redis::Pool as RedisPool;

use crate::db::Db;

/// Immutable runtime configuration, resolved from env at startup.
#[derive(Debug, Clone)]
pub struct Config {
    /// `BIND` — socket address to listen on (default `127.0.0.1:2002`).
    pub bind: String,
    /// `DB_PATH` — SQLite file shared with the Python service.
    pub db_path: String,
    /// `REDIS_URL` — redis connection URL.
    pub redis_url: String,
    /// Session cookie TTL in seconds (8h, matching Python `SESSION_TTL`).
    pub session_ttl: i64,
    /// Reservation TTL in seconds for the acquire Lua (`RESV_TTL`).
    pub resv_ttl: i64,
    /// Per-user failed-login cap per minute (`LOGIN_MAX_FAILS`).
    pub login_max_fails: i64,
    /// Per-IP failed-login cap per minute (`LOGIN_IP_MAX_FAILS`).
    pub login_ip_max_fails: i64,
    /// Reader pool size.
    pub read_pool_size: usize,
    /// Max in-flight requests before tower sheds load with 503.
    pub max_concurrency: usize,
    /// Per-request timeout in seconds.
    pub request_timeout_secs: u64,
}

impl Config {
    /// Read config from environment, applying the same defaults as `main.py`.
    pub fn from_env() -> Self {
        fn env_or(key: &str, default: &str) -> String {
            std::env::var(key).unwrap_or_else(|_| default.to_string())
        }
        fn env_parse<T: std::str::FromStr>(key: &str, default: T) -> T {
            std::env::var(key)
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(default)
        }
        Config {
            bind: env_or("BIND", "127.0.0.1:2002"),
            db_path: env_or("DB_PATH", "club_system.db"),
            redis_url: env_or("REDIS_URL", "redis://127.0.0.1:6379/0"),
            session_ttl: env_parse("SESSION_TTL", 8 * 3600),
            resv_ttl: env_parse("RESV_TTL", 15),
            login_max_fails: env_parse("LOGIN_MAX_FAILS", 10),
            login_ip_max_fails: env_parse("LOGIN_IP_MAX_FAILS", 50),
            read_pool_size: env_parse("DB_POOL_SIZE", 12),
            max_concurrency: env_parse("MAX_CONCURRENCY", 512),
            request_timeout_secs: env_parse("REQUEST_TIMEOUT_SECS", 15),
        }
    }
}

/// Application state handed to every axum handler via `State<AppState>`.
#[derive(Clone)]
pub struct AppState {
    pub db: Db,
    pub redis: RedisPool,
    pub cfg: Arc<Config>,
}
