//! Redis seat/session gate — the authoritative concurrency layer.
//!
//! Mirrors `RedisGate` in `main.py` exactly so the Rust hot service and the
//! Python admin service share one Redis with an identical key contract:
//!   * `stock:club:{id}`   — remaining seats (source of truth for oversell)
//!   * `student:reg:{id}`  — confirmed registration -> club_id
//!   * `resv:{id}`         — in-flight reservation (TTL'd by the acquire Lua)
//!   * `sess:{token}`      — session JSON
//!   * `open_at`           — registration-open epoch seconds
//!   * `cache:clubs`       — clubs cache key (invalidated on mutation)
//!   * `seats:initialized` — set to "1" after `rebuild_stock`
//!   * `loginfail:{key}`   — per-minute failed-login counter
//!
//! Degradation contract: write paths that cannot reach Redis surface
//! [`AppError::RedisDown`] (-> 503) and MUST NOT fall back to an unlocked
//! SQLite path. Read paths may fall back to SQLite by treating a Redis miss as
//! `None` and letting the caller decide.

use std::sync::LazyLock;

use deadpool_redis::Pool as RedisPool;
use redis::{AsyncCommands, Script};

use crate::db::Db;
use crate::error::{AppError, AppResult};

// --- key helpers -----------------------------------------------------------

pub fn k_stock(club_id: i64) -> String {
    format!("stock:club:{club_id}")
}
pub fn k_stureg(student_id: i64) -> String {
    format!("student:reg:{student_id}")
}
pub fn k_resv(student_id: i64) -> String {
    format!("resv:{student_id}")
}
pub fn k_sess(token: &str) -> String {
    format!("sess:{token}")
}

pub const K_OPENAT: &str = "open_at";
pub const K_CACHE_CLUBS: &str = "cache:clubs";
pub const K_INIT: &str = "seats:initialized";

/// Acquire Lua — byte-for-byte equivalent to `LUA_ACQUIRE` in `main.py`.
/// KEYS[1]=stock, KEYS[2]=student:reg, KEYS[3]=resv; ARGV[1]=club_id,
/// ARGV[2]=ttl. Returns 1 success / 0 full / -1 already-registered / -2
/// uninitialized.
const LUA_ACQUIRE: &str = r#"
if redis.call('EXISTS', KEYS[1]) == 0 then return -2 end
if redis.call('EXISTS', KEYS[2]) == 1 then return -1 end
if redis.call('EXISTS', KEYS[3]) == 1 then return -1 end
local left = tonumber(redis.call('GET', KEYS[1]))
if left <= 0 then return 0 end
redis.call('DECR', KEYS[1])
redis.call('SET', KEYS[3], ARGV[1], 'EX', tonumber(ARGV[2]))
return 1
"#;

static ACQUIRE_SCRIPT: LazyLock<Script> = LazyLock::new(|| Script::new(LUA_ACQUIRE));

/// Outcome of a seat acquire attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AcquireOutcome {
    /// Reservation taken; caller must persist to SQLite then confirm/release.
    Ok,
    /// Club is full.
    Full,
    /// Already registered (confirmed or in-flight) for some club.
    Already,
    /// Stock key missing — needs a stock rebuild.
    Uninitialized,
}

impl AcquireOutcome {
    fn from_code(code: i64) -> Self {
        match code {
            1 => AcquireOutcome::Ok,
            0 => AcquireOutcome::Full,
            -1 => AcquireOutcome::Already,
            _ => AcquireOutcome::Uninitialized, // -2 and any unexpected value
        }
    }
}

/// Thin handle over the Redis pool with all seat/session operations.
///
/// Every method that a *write* endpoint depends on returns `AppResult` and maps
/// a connection/command failure to [`AppError::RedisDown`], so the handler can
/// reject with 503. Read-side helpers return `Option`/`bool` and swallow errors
/// into a "miss" so callers can fall back to SQLite.
#[derive(Clone)]
pub struct Seats {
    pool: RedisPool,
}

impl Seats {
    pub fn new(pool: RedisPool) -> Self {
        Seats { pool }
    }

    /// Liveness `PING`. `true` only if a connection is obtainable and answers.
    pub async fn alive(&self) -> bool {
        let Ok(mut conn) = self.pool.get().await else {
            return false;
        };
        redis::cmd("PING")
            .query_async::<String>(&mut conn)
            .await
            .is_ok()
    }

    /// Atomic seat acquire via the Lua script. Redis unreachable -> RedisDown.
    pub async fn acquire(&self, student_id: i64, club_id: i64, ttl: i64) -> AppResult<AcquireOutcome> {
        let mut conn = self.pool.get().await.map_err(|_| AppError::RedisDown)?;
        let code: i64 = ACQUIRE_SCRIPT
            .key(k_stock(club_id))
            .key(k_stureg(student_id))
            .key(k_resv(student_id))
            .arg(club_id)
            .arg(ttl)
            .invoke_async(&mut conn)
            .await
            .map_err(|_| AppError::RedisDown)?;
        Ok(AcquireOutcome::from_code(code))
    }

    /// Confirm a reservation after SQLite persistence: persist
    /// `student:reg:{sid}` and drop `resv:{sid}`. Best-effort (logged, not
    /// fatal) — the SQLite row is the durable record.
    pub async fn confirm(&self, student_id: i64, club_id: i64) {
        if let Err(e) = self.confirm_inner(student_id, club_id).await {
            tracing::error!(student_id, club_id, error = %e, "confirm_seat failed");
        }
    }

    async fn confirm_inner(&self, student_id: i64, club_id: i64) -> redis::RedisResult<()> {
        let mut conn = self
            .pool
            .get()
            .await
            .map_err(|e| redis_pool_err("confirm", e))?;
        redis::pipe()
            .atomic()
            .set(k_stureg(student_id), club_id)
            .del(k_resv(student_id))
            .query_async::<()>(&mut conn)
            .await
    }

    /// Release a reservation/registration: `INCR` stock back, drop `resv` and
    /// `student:reg`. Used both on SQLite-persist failure (compensating the
    /// acquire) and on cancel. Best-effort.
    pub async fn release(&self, student_id: i64, club_id: i64) {
        if let Err(e) = self.release_inner(student_id, club_id).await {
            tracing::error!(student_id, club_id, error = %e, "release_seat failed");
        }
    }

    async fn release_inner(&self, student_id: i64, club_id: i64) -> redis::RedisResult<()> {
        let mut conn = self
            .pool
            .get()
            .await
            .map_err(|e| redis_pool_err("release", e))?;
        redis::pipe()
            .atomic()
            .incr(k_stock(club_id), 1)
            .del(k_resv(student_id))
            .del(k_stureg(student_id))
            .query_async::<()>(&mut conn)
            .await
    }

    /// Batch live remaining-seats for `club_ids`. `None` on any Redis failure
    /// (so the read handler can fall back to stored `current_students`). Inner
    /// `None` means the key was absent.
    pub async fn stock_left(&self, club_ids: &[i64]) -> Option<Vec<Option<i64>>> {
        if club_ids.is_empty() {
            return Some(Vec::new());
        }
        let mut conn = self.pool.get().await.ok()?;
        let keys: Vec<String> = club_ids.iter().map(|c| k_stock(*c)).collect();
        // MGET returns one entry per key; missing keys come back as nil.
        let vals: Vec<Option<String>> = conn.mget(keys).await.ok()?;
        Some(
            vals.into_iter()
                .map(|v| v.and_then(|s| s.parse::<i64>().ok()))
                .collect(),
        )
    }

    /// Unified clock: prefer Redis `TIME`, fall back to local wall clock.
    pub async fn now_epoch(&self) -> i64 {
        if let Ok(mut conn) = self.pool.get().await {
            // TIME -> [secs, usecs] as strings.
            if let Ok((secs, _usecs)) =
                redis::cmd("TIME").query_async::<(i64, i64)>(&mut conn).await
            {
                return secs;
            }
        }
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0)
    }

    // --- sessions ----------------------------------------------------------

    /// Create a session: SET `sess:{token}` = JSON with TTL. Token is 32 random
    /// URL-safe bytes (matching `secrets.token_urlsafe(32)`). Redis unreachable
    /// -> RedisDown (login then fails closed rather than minting a dead cookie).
    pub async fn session_create(&self, payload: &serde_json::Value, ttl: i64) -> AppResult<String> {
        let token = gen_token();
        let mut conn = self.pool.get().await.map_err(|_| AppError::RedisDown)?;
        let body = payload.to_string();
        let _: () = redis::cmd("SET")
            .arg(k_sess(&token))
            .arg(body)
            .arg("EX")
            .arg(ttl)
            .query_async(&mut conn)
            .await
            .map_err(|_| AppError::RedisDown)?;
        Ok(token)
    }

    /// Fetch + parse a session by token. `None` on miss or any error (treated
    /// as "not logged in").
    pub async fn session_get(&self, token: &str) -> Option<serde_json::Value> {
        if token.is_empty() {
            return None;
        }
        let mut conn = self.pool.get().await.ok()?;
        let raw: Option<String> = conn.get(k_sess(token)).await.ok()?;
        raw.and_then(|s| serde_json::from_str(&s).ok())
    }

    /// Delete a session (logout). Best-effort.
    pub async fn session_del(&self, token: &str) {
        if token.is_empty() {
            return;
        }
        if let Ok(mut conn) = self.pool.get().await {
            let _: Result<(), _> = conn.del::<_, ()>(k_sess(token)).await;
        }
    }

    // --- login throttle (count failures only) ------------------------------

    /// Read-only: is this key's failure count over `limit`? Errors -> not
    /// blocked (fail open on the throttle so Redis hiccups don't lock users
    /// out; the underlying password check still gates access).
    pub async fn login_blocked(&self, key: &str, limit: i64) -> bool {
        let Ok(mut conn) = self.pool.get().await else {
            return false;
        };
        let n: Option<String> = conn.get(format!("loginfail:{key}")).await.unwrap_or(None);
        n.and_then(|s| s.parse::<i64>().ok()).unwrap_or(0) > limit
    }

    /// Count one failed attempt; first failure in the window sets a 60s expiry.
    pub async fn login_fail(&self, key: &str) {
        let Ok(mut conn) = self.pool.get().await else {
            return;
        };
        let k = format!("loginfail:{key}");
        if let Ok(n) = conn.incr::<_, _, i64>(&k, 1).await {
            if n == 1 {
                let _: Result<bool, _> = conn.expire(&k, 60).await;
            }
        }
    }

    /// Clear a user's failure counter after a successful login.
    pub async fn login_ok(&self, key: &str) {
        if let Ok(mut conn) = self.pool.get().await {
            let _: Result<(), _> = conn.del::<_, ()>(format!("loginfail:{key}")).await;
        }
    }

    // --- open_at -----------------------------------------------------------

    /// Read the registration-open epoch from Redis. `None` on miss/error.
    pub async fn open_at_get(&self) -> Option<i64> {
        let mut conn = self.pool.get().await.ok()?;
        let v: Option<String> = conn.get(K_OPENAT).await.ok()?;
        v.and_then(|s| s.parse::<i64>().ok())
    }

    /// Set the registration-open epoch. Best-effort.
    pub async fn open_at_set(&self, epoch: i64) {
        if let Ok(mut conn) = self.pool.get().await {
            let _: Result<(), _> = conn.set::<_, _, ()>(K_OPENAT, epoch).await;
        }
    }

    /// Whether `seats:initialized` is set — used by `/readyz`.
    pub async fn initialized(&self) -> bool {
        let Ok(mut conn) = self.pool.get().await else {
            return false;
        };
        conn.exists(K_INIT).await.unwrap_or(false)
    }
}

fn redis_pool_err(op: &str, e: deadpool_redis::PoolError) -> redis::RedisError {
    redis::RedisError::from((redis::ErrorKind::Io, "pool", format!("{op}: {e}")))
}

/// 32 random bytes, URL-safe base64 without padding — same alphabet/entropy as
/// Python's `secrets.token_urlsafe(32)`.
fn gen_token() -> String {
    use rand::RngExt;
    let mut bytes = [0u8; 32];
    rand::rng().fill(&mut bytes[..]);
    url_safe_b64(&bytes)
}

/// URL-safe base64 (RFC 4648 §5) without padding. Small inline impl to avoid a
/// base64 crate dependency.
fn url_safe_b64(input: &[u8]) -> String {
    const ALPHABET: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[((n >> 18) & 63) as usize] as char);
        out.push(ALPHABET[((n >> 12) & 63) as usize] as char);
        if chunk.len() > 1 {
            out.push(ALPHABET[((n >> 6) & 63) as usize] as char);
        }
        if chunk.len() > 2 {
            out.push(ALPHABET[(n & 63) as usize] as char);
        }
    }
    out
}

/// Rebuild Redis stock from SQLite ground truth at startup (idempotent):
/// `SET stock:club:{id} = max - used` for every club, and rebuild the
/// `student:reg:{sid}` mirror. Deletes any stale `stock:club:*` /
/// `student:reg:*` first so removed clubs/students don't linger. Sets
/// `seats:initialized=1`. No-op (with a warning) if Redis is down.
pub async fn rebuild_stock(seats: &Seats, db: &Db) -> AppResult<()> {
    if !seats.alive().await {
        tracing::warn!("Redis unavailable, skipping stock rebuild (seckill degraded)");
        return Ok(());
    }
    let clubs = db.club_stock_snapshot().await?;
    let regs = db.all_registrations().await?;

    let mut conn = seats.pool.get().await.map_err(|_| AppError::RedisDown)?;

    // Clear stale stock/mirror keys via SCAN (avoids blocking KEYS).
    for pattern in ["stock:club:*", "student:reg:*"] {
        let mut cursor: u64 = 0;
        loop {
            let (next, keys): (u64, Vec<String>) = redis::cmd("SCAN")
                .arg(cursor)
                .arg("MATCH")
                .arg(pattern)
                .arg("COUNT")
                .arg(512)
                .query_async(&mut conn)
                .await
                .map_err(|_| AppError::RedisDown)?;
            if !keys.is_empty() {
                let _: () = conn.del(keys).await.map_err(|_| AppError::RedisDown)?;
            }
            cursor = next;
            if cursor == 0 {
                break;
            }
        }
    }

    let mut pipe = redis::pipe();
    pipe.atomic();
    for c in &clubs {
        let left = (c.max_students - c.used).max(0);
        pipe.set(k_stock(c.club_id), left).ignore();
    }
    for (sid, cid) in &regs {
        pipe.set(k_stureg(*sid), *cid).ignore();
    }
    pipe.set(K_INIT, "1").ignore();
    pipe.del(K_CACHE_CLUBS).ignore();
    pipe.query_async::<()>(&mut conn)
        .await
        .map_err(|_| AppError::RedisDown)?;

    tracing::info!(clubs = clubs.len(), "Redis stock rebuilt");
    Ok(())
}

/// Seed `open_at` from `settings.registration_start_time` at startup. Parses the
/// `YYYY-MM-DD HH:MM:SS` local-time string to an epoch. Best-effort.
pub async fn seed_open_at(seats: &Seats, db: &Db) {
    match db.registration_start_time().await {
        Ok(Some(s)) => match crate::auth::parse_local_datetime(&s) {
            Some(epoch) => seats.open_at_set(epoch).await,
            None => tracing::warn!(value = %s, "registration_start_time unparseable; open_at not set"),
        },
        Ok(None) => {}
        Err(e) => tracing::error!(error = %e, "seed_open_at failed"),
    }
}
