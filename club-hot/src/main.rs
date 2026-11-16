//! club-hot：选课系统的 Rust 热路径服务。
//!
//! 提供登录、报名、退选、社团目录、名额快照、开放时间、学生信息与 `/healthz`、`/readyz`。
//! 与 Python 管理服务共用 SQLite 文件、Redis 键与 Lua 脚本，反向代理可在两者之间切换。

mod auth;
mod cache;
mod db;
mod error;
mod handlers;
mod messages;
mod redis_seats;
mod state;
mod types;

use std::collections::BTreeSet;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use axum::Router;
use axum::error_handling::HandleErrorLayer;
use axum::http::StatusCode;
use axum::routing::{get, post};
use deadpool_redis::{
    Config as RedisConfig, PoolConfig as RedisPoolConfig, Runtime as RedisRuntime,
};
use tokio::sync::{Mutex, Semaphore};
use tower::ServiceBuilder;
use tower::limit::GlobalConcurrencyLimitLayer;
use tower_http::trace::TraceLayer;
use tracing_subscriber::EnvFilter;

use crate::db::Db;
use crate::redis_seats::Seats;
use crate::state::{AppState, Config};

const REPAIR_INTERVAL: Duration = Duration::from_secs(1);
const ORPHAN_SCAN_INTERVAL: Duration = Duration::from_secs(10);

fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();
    let cfg = Config::from_env();
    auth::init(&cfg);
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    rt.block_on(run(cfg))
}

async fn run(cfg: Config) -> Result<(), Box<dyn std::error::Error>> {
    let db = Db::new(&cfg.db_path, cfg.read_pool_size, &cfg.sqlite_synchronous)?;
    db.ensure_schema_guards().await?;

    let mut redis_cfg = RedisConfig::from_url(cfg.redis_url.clone());
    let mut pool_cfg = RedisPoolConfig::new(cfg.redis_pool_size);
    // 取连接时会先 PING 校验；旧电脑上 Redis 稍慢，建连与校验按 REDIS_TIMEOUT 等待。
    pool_cfg.timeouts.wait = Some(Duration::from_millis(cfg.redis_pool_wait_ms));
    pool_cfg.timeouts.create = Some(cfg.redis_timeout);
    pool_cfg.timeouts.recycle = Some(cfg.redis_timeout);
    redis_cfg.pool = Some(pool_cfg);
    let redis = redis_cfg
        .create_pool(Some(RedisRuntime::Tokio1))
        .map_err(|e| format!("redis pool: {e}"))?;

    let registration_gate = Arc::new(Semaphore::new(cfg.registration_concurrency));
    let reconcile_requested = Arc::new(AtomicBool::new(false));
    let state = AppState {
        db: db.clone(),
        redis: redis.clone(),
        cfg: Arc::new(cfg.clone()),
        auth: auth::AuthPool::new(cfg.auth_concurrency, cfg.auth_queue, handlers::AUTH_WAIT),
        registration_gate: registration_gate.clone(),
        reconcile_requested: reconcile_requested.clone(),
        catalog: Arc::new(Mutex::new(None)),
        seats: Arc::new(Mutex::new(None)),
    };

    let seats = Seats::new(redis.clone(), &cfg);
    if seats.alive().await {
        if seats.initialized().await {
            seed_settings(&seats, &db).await;
        } else if !matches!(redis_seats::rebuild_all(&seats, &db).await, Ok(true)) {
            reconcile_requested.store(true, Ordering::Release);
        }
    } else {
        reconcile_requested.store(true, Ordering::Release);
    }
    tokio::spawn(seat_worker(
        seats.clone(),
        db.clone(),
        reconcile_requested.clone(),
    ));

    let probes = Router::new()
        .route("/healthz", get(handlers::healthz))
        .route("/readyz", get(handlers::readyz))
        .with_state(state.clone());
    let api = Router::new()
        .route("/api/login", post(handlers::login))
        .route("/api/register_club", post(handlers::register_club))
        .route(
            "/api/cancel_registration",
            post(handlers::cancel_registration),
        )
        .route("/api/get_clubs", get(handlers::get_clubs))
        .route("/api/catalog", get(handlers::catalog))
        .route("/api/seats", get(handlers::seats_snapshot))
        .route(
            "/api/check_registration_time",
            get(handlers::check_registration_time),
        )
        .route("/api/get_student_info", get(handlers::get_student_info))
        .with_state(state)
        // 超过总并发上限时立即返回 503；单个请求超时同样返回 503。
        .layer(
            ServiceBuilder::new()
                .layer(HandleErrorLayer::new(handle_overload))
                .load_shed()
                .layer(GlobalConcurrencyLimitLayer::new(cfg.max_concurrency))
                .timeout(Duration::from_secs(cfg.request_timeout_secs))
                .layer(TraceLayer::new_for_http()),
        );
    let app = probes.merge(api);

    let listener = tokio::net::TcpListener::bind(&cfg.bind).await?;
    tracing::info!(bind = %cfg.bind, "club-hot listening");
    let serve_result = axum::serve(listener, app.into_make_service())
        .with_graceful_shutdown(shutdown_signal())
        .await;

    // 后台报名任务在退出前排空。
    let drain = async {
        while registration_gate.available_permits() < cfg.registration_concurrency {
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    };
    if tokio::time::timeout(Duration::from_secs(cfg.request_timeout_secs + 5), drain)
        .await
        .is_err()
    {
        tracing::error!("registration tasks did not drain before shutdown deadline");
    }
    serve_result?;
    Ok(())
}

/// 把 SQLite 中的开放时间与锁定状态同步到 Redis。
async fn seed_settings(seats: &Seats, db: &Db) {
    if let Ok(Some(text)) = db.registration_start_time().await
        && let Some(epoch) = auth::parse_local_datetime(&text)
    {
        let _ = seats.open_at_set(Some(epoch)).await;
    }
    if let Ok(locked) = db.registration_locked().await {
        let _ = seats.locked_set(locked).await;
    }
}

/// 后台维护名额：Redis 重启后整体发布，按需修复单个社团，定期清理失效的在途成员。
async fn seat_worker(seats: Seats, db: Db, reconcile_requested: Arc<AtomicBool>) {
    let mut last_scan = Instant::now() - ORPHAN_SCAN_INTERVAL;
    loop {
        tokio::time::sleep(REPAIR_INTERVAL).await;
        if !seats.alive().await {
            continue;
        }
        if reconcile_requested.load(Ordering::Acquire) || !seats.initialized().await {
            reconcile_requested.store(true, Ordering::Release);
            if matches!(redis_seats::rebuild_all(&seats, &db).await, Ok(true)) {
                reconcile_requested.store(false, Ordering::Release);
                tracing::info!("seat state published after Redis became available");
            }
            continue;
        }
        let mut targets: BTreeSet<i64> = seats.pending_repairs().await.into_iter().collect();
        if let Ok(pending) = db.clubs_pending_sync().await {
            targets.extend(pending);
        }
        if last_scan.elapsed() >= ORPHAN_SCAN_INTERVAL {
            last_scan = Instant::now();
            if let Ok(clubs) = db.club_ids().await {
                for club in clubs {
                    if matches!(seats.inflight_state(club).await, Ok((_, dead)) if dead > 0) {
                        targets.insert(club);
                    }
                }
            }
        }
        for club in targets {
            redis_seats::repair_club(&seats, &db, club).await;
        }
    }
}

async fn handle_overload(_err: axum::BoxError) -> axum::response::Response {
    messages::fail(StatusCode::SERVICE_UNAVAILABLE, "queued")
}

async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut sig) => {
                sig.recv().await;
            }
            Err(_) => std::future::pending::<()>().await,
        }
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();
    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
    tracing::info!("shutdown signal received");
}
