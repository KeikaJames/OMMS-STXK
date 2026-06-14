//! club-hot — the Rust hot-path service for the club registration system.
//!
//! Reimplements the six high-traffic endpoints of `main.py` (login, register,
//! cancel, list clubs, time gate, student info) plus `/healthz` and `/readyz`.
//! Shares one SQLite file and one Redis with the Python admin service under an
//! identical key/Lua/JSON contract, so a reverse proxy can split traffic
//! between them. Built for the registration rush: Redis-atomic seat acquisition
//! (no oversell), a dedicated single-writer SQLite pool, and tower backpressure
//! (global concurrency limit + load-shed + per-request timeout -> 503 when
//! overloaded).

mod auth;
mod db;
mod error;
mod handlers;
mod redis_seats;
mod state;
mod types;

use std::sync::Arc;
use std::time::Duration;

use axum::Router;
use axum::error_handling::HandleErrorLayer;
use axum::http::StatusCode;
use axum::routing::{get, post};
use deadpool_redis::{Config as RedisConfig, Runtime as RedisRuntime};
use tower::ServiceBuilder;
use tower::limit::GlobalConcurrencyLimitLayer;
use tracing_subscriber::EnvFilter;

use crate::db::Db;
use crate::state::{AppState, Config};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize tracing first so startup is observable.
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    // Capture the local UTC offset on the main thread BEFORE the Tokio runtime
    // spawns workers — `time` refuses to compute it in a multithreaded process.
    auth::init_local_offset();

    let cfg = Config::from_env();

    // Build the multi-thread runtime explicitly (so the offset capture above is
    // genuinely single-threaded) and drive the async entrypoint on it.
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    rt.block_on(run(cfg))
}

async fn run(cfg: Config) -> Result<(), Box<dyn std::error::Error>> {
    // --- SQLite pools (write=1, read=N) ---
    let db = Db::new(&cfg.db_path, cfg.read_pool_size)?;

    // --- Redis pool ---
    let redis_cfg = RedisConfig::from_url(cfg.redis_url.clone());
    let redis = redis_cfg
        .create_pool(Some(RedisRuntime::Tokio1))
        .map_err(|e| format!("redis pool: {e}"))?;

    let state = AppState {
        db: db.clone(),
        redis: redis.clone(),
        cfg: Arc::new(cfg.clone()),
    };

    // --- Startup: rebuild stock from SQLite ground truth + seed open_at ---
    let seats = redis_seats::Seats::new(redis.clone());
    if let Err(e) = redis_seats::rebuild_stock(&seats, &db).await {
        tracing::error!(error = %e, "initial rebuild_stock failed (continuing degraded)");
    }
    redis_seats::seed_open_at(&seats, &db).await;
    let redis_up = seats.alive().await;
    tracing::info!(redis = redis_up, "startup complete");

    // --- Router ---
    let app = Router::new()
        .route("/healthz", get(handlers::healthz))
        .route("/readyz", get(handlers::readyz))
        .route("/api/login", post(handlers::login))
        .route("/api/register_club", post(handlers::register_club))
        .route(
            "/api/cancel_registration",
            post(handlers::cancel_registration),
        )
        .route("/api/get_clubs", get(handlers::get_clubs))
        .route(
            "/api/check_registration_time",
            get(handlers::check_registration_time),
        )
        .route("/api/get_student_info", get(handlers::get_student_info))
        .with_state(state)
        // Backpressure stack (outermost first). load_shed converts a saturated
        // concurrency limit into an immediate error; HandleError maps that (and
        // timeouts) to 503. timeout bounds slow requests. The global limit caps
        // total in-flight work so the rush can't exhaust DB/Redis pools.
        .layer(
            ServiceBuilder::new()
                .layer(HandleErrorLayer::new(handle_overload))
                .load_shed()
                .layer(GlobalConcurrencyLimitLayer::new(cfg.max_concurrency))
                .timeout(Duration::from_secs(cfg.request_timeout_secs)),
        );

    // --- Serve ---
    let listener = tokio::net::TcpListener::bind(&cfg.bind).await?;
    tracing::info!(bind = %cfg.bind, "club-hot listening");
    axum::serve(listener, app.into_make_service())
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

/// Map a shed/timeout error from the tower stack to a 503 JSON body, matching
/// the shape every other endpoint uses on overload.
async fn handle_overload(_err: axum::BoxError) -> axum::response::Response {
    use axum::Json;
    use axum::response::IntoResponse;
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(serde_json::json!({ "success": false, "message": "服务繁忙，请稍后重试" })),
    )
        .into_response()
}

/// Graceful shutdown on Ctrl-C / SIGTERM.
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
