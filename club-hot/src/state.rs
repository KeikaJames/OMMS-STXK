//! 进程级配置与共享状态。

use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::time::Duration;

use deadpool_redis::Pool as RedisPool;
use tokio::sync::{Mutex, Semaphore};

use crate::auth::AuthPool;
use crate::cache::{CachedJson, Catalog};
use crate::db::Db;

#[derive(Debug, Clone)]
pub struct Config {
    pub bind: String,
    pub db_path: String,
    pub redis_url: String,
    pub redis_prefix: String,
    pub redis_timeout: Duration,
    pub session_ttl: i64,
    pub cookie_secure: bool,
    pub seat_op_ttl: i64,
    pub login_max_fails: i64,
    pub auth_concurrency: usize,
    pub auth_queue: usize,
    pub argon2_memory_kib: u32,
    pub argon2_time_cost: u32,
    pub argon2_parallelism: u32,
    pub registration_concurrency: usize,
    pub register_wait: Duration,
    pub read_pool_size: usize,
    pub redis_pool_size: usize,
    pub redis_pool_wait_ms: u64,
    pub max_concurrency: usize,
    pub request_timeout_secs: u64,
    pub sqlite_synchronous: String,
    pub utc_offset: String,
    pub seats_cache: Duration,
}

impl Config {
    /// 从环境变量读取配置，默认值与 main.py 一致。
    pub fn from_env() -> Self {
        fn env_or(key: &str, default: &str) -> String {
            std::env::var(key).unwrap_or_else(|_| default.to_string())
        }
        fn env_parse<T: std::str::FromStr>(key: &str, default: T) -> T {
            std::env::var(key)
                .ok()
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(default)
        }
        fn env_bool(key: &str) -> bool {
            matches!(
                std::env::var(key)
                    .unwrap_or_default()
                    .trim()
                    .to_ascii_lowercase()
                    .as_str(),
                "1" | "true" | "yes" | "on"
            )
        }
        let cores = std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(1);
        let synchronous = env_or("SQLITE_SYNCHRONOUS", "NORMAL")
            .trim()
            .to_ascii_uppercase();
        let synchronous = match synchronous.as_str() {
            "OFF" | "NORMAL" | "FULL" | "EXTRA" => synchronous,
            _ => "NORMAL".to_string(),
        };
        // 口令校验线程：核数的一半，不少于 4、不多于 8，核数更少时取核数。
        let auth_concurrency =
            env_parse("AUTH_CONCURRENCY", cores.min(8).min((cores / 2).max(4))).max(1);
        Config {
            bind: env_or("BIND", "127.0.0.1:2002"),
            db_path: env_or("DB_PATH", "club_system.db"),
            redis_url: env_or("REDIS_URL", "redis://127.0.0.1:6379/0"),
            redis_prefix: env_or("REDIS_PREFIX", "omms:"),
            redis_timeout: Duration::from_secs_f64(env_parse("REDIS_TIMEOUT", 2.0f64).max(0.2)),
            session_ttl: env_parse("SESSION_TTL", 8 * 3600i64).max(60),
            cookie_secure: env_bool("COOKIE_SECURE"),
            seat_op_ttl: env_parse("SEAT_OP_TTL", 30i64).max(5),
            login_max_fails: env_parse("LOGIN_MAX_FAILS", 10i64).max(1),
            auth_concurrency,
            auth_queue: env_parse("AUTH_QUEUE", auth_concurrency * 64).max(auth_concurrency),
            argon2_memory_kib: env_parse("ARGON2_MEMORY_KIB", 19_456u32).max(8_192),
            argon2_time_cost: env_parse("ARGON2_TIME_COST", 2u32).max(1),
            argon2_parallelism: env_parse("ARGON2_PARALLELISM", 1u32).max(1),
            registration_concurrency: env_parse("REGISTER_CONCURRENCY", 1usize).max(1),
            register_wait: Duration::from_millis(env_parse("REGISTER_WAIT_MS", 8_000u64).max(100)),
            read_pool_size: env_parse("DB_POOL_SIZE", 12usize).max(1),
            redis_pool_size: env_parse("REDIS_POOL_SIZE", 32usize).max(1),
            redis_pool_wait_ms: env_parse("REDIS_POOL_WAIT_MS", 1_000u64).max(1),
            max_concurrency: env_parse("MAX_CONCURRENCY", 1024usize).max(1),
            request_timeout_secs: env_parse("REQUEST_TIMEOUT_SECS", 15u64).max(1),
            sqlite_synchronous: synchronous,
            utc_offset: env_or("APP_UTC_OFFSET", "+08:00"),
            seats_cache: Duration::from_millis(env_parse("SEATS_CACHE_MS", 500u64)),
        }
    }
}

/// 交给每个 handler 的状态；字段都可廉价克隆。
#[derive(Clone)]
pub struct AppState {
    pub db: Db,
    pub redis: RedisPool,
    pub cfg: Arc<Config>,
    pub auth: Arc<AuthPool>,
    pub registration_gate: Arc<Semaphore>,
    pub reconcile_requested: Arc<AtomicBool>,
    pub catalog: Arc<Mutex<Option<Arc<Catalog>>>>,
    pub seats: Arc<Mutex<Option<CachedJson>>>,
}
