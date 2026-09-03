//! Redis seat/session gate — the authoritative concurrency layer.
//!
//! Mirrors `RedisGate` in `main.py` exactly so the Rust hot service and the
//! Python admin service share one Redis with an identical key contract:
//!   * `stock:club:{id}`   — remaining seats (source of truth for oversell)
//!   * `student:reg:{id}`  — confirmed registration -> club_id|operation_id
//!   * `resv:{id}`         — in-flight club_id|operation_id (TTL'd)
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

use std::future::Future;
use std::sync::LazyLock;
use std::time::Duration;

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
pub fn k_sess_epoch(role: &str) -> String {
    format!("sess:epoch:{role}")
}
pub fn k_sess_version(role: &str, principal: &str) -> String {
    format!("sess:version:{role}:{principal}")
}
pub fn k_sess_role_mutation(role: &str) -> String {
    format!("sess:mutation:role:{role}")
}
pub fn k_sess_principal_mutation(role: &str, principal: &str) -> String {
    format!("sess:mutation:{role}:{principal}")
}
pub fn k_op(student_id: i64) -> String {
    format!("seat:op:{student_id}")
}

pub const K_OPENAT: &str = "open_at";
pub const K_REGISTRATION_LOCK: &str = "registration_locked";
pub const K_CACHE_CLUBS: &str = "cache:clubs";
pub const K_INIT: &str = "seats:initialized";
pub const K_MAINT: &str = "seats:maintenance";

/// Acquire Lua — byte-for-byte equivalent to `LUA_ACQUIRE` in `main.py`.
/// Open-time check, maintenance gate, duplicate check, and decrement all happen
/// in one Redis operation. KEYS=stock/stureg/resv/open_at/maintenance/seat-op/lock;
/// ARGV=reservation_value/reservation-ttl/operation-ttl.
const LUA_ACQUIRE: &str = r#"
    local reservation_ttl = tonumber(ARGV[2])
    local operation_ttl = tonumber(ARGV[3])
    if not reservation_ttl or reservation_ttl <= 0 or not operation_ttl or operation_ttl <= 0 then return -5 end
    if redis.call('GET', KEYS[7]) == '1' then return -6 end
    if redis.call('GET', KEYS[6]) == ARGV[1] then
        if redis.call('GET', KEYS[3]) ~= ARGV[1] then
            redis.call('SET', KEYS[3], ARGV[1], 'EX', reservation_ttl)
        end
        return 1
    end
    if redis.call('EXISTS', KEYS[5]) == 1 then return -3 end
    local open_at = redis.call('GET', KEYS[4])
    if not open_at then return -2 end
    local now = redis.call('TIME')
    if tonumber(now[1]) < tonumber(open_at) then return -4 end
    if redis.call('EXISTS', KEYS[1]) == 0 then return -2 end
    if redis.call('EXISTS', KEYS[2]) == 1 then return -1 end
    if redis.call('EXISTS', KEYS[3]) == 1 then return -1 end
    if redis.call('EXISTS', KEYS[6]) == 1 then return -1 end
    local left = tonumber(redis.call('GET', KEYS[1]))
    if left <= 0 then return 0 end
    local resv_set = redis.pcall('SET', KEYS[3], ARGV[1], 'EX', reservation_ttl)
    if type(resv_set) == 'table' and resv_set.err then return -5 end
    local op_set = redis.pcall('SET', KEYS[6], ARGV[1], 'EX', operation_ttl)
    if type(op_set) == 'table' and op_set.err then
        redis.call('DEL', KEYS[3])
        return -5
    end
    local decremented = redis.pcall('DECR', KEYS[1])
    if type(decremented) == 'table' and decremented.err then
        redis.call('DEL', KEYS[3])
        redis.call('DEL', KEYS[6])
        return -5
    end
return 1
"#;

static ACQUIRE_SCRIPT: LazyLock<Script> = LazyLock::new(|| Script::new(LUA_ACQUIRE));

const LUA_CONFIRM: &str = r#"
    if redis.call('GET', KEYS[2]) == ARGV[1] then
        if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('DEL', KEYS[1]) end
        if redis.call('GET', KEYS[3]) == ARGV[1] then redis.call('DEL', KEYS[3]) end
        return 1
    end
    if redis.call('GET', KEYS[1]) ~= ARGV[1] and redis.call('GET', KEYS[3]) ~= ARGV[1] then return 0 end
    redis.call('SET', KEYS[2], ARGV[1])
    if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('DEL', KEYS[1]) end
    if redis.call('GET', KEYS[3]) == ARGV[1] then redis.call('DEL', KEYS[3]) end
    return 1
"#;

const LUA_ROLLBACK: &str = r#"
    local stock = redis.call('GET', KEYS[1])
    if not stock or not tonumber(stock) then return -1 end
    if redis.call('EXISTS', KEYS[3]) == 1 then return 0 end
    redis.call('SET', KEYS[3], '1', 'EX', 604800)
    redis.call('INCR', KEYS[1])
    if redis.call('GET', KEYS[2]) == ARGV[1] then redis.call('DEL', KEYS[2]) end
    if redis.call('GET', KEYS[4]) == ARGV[1] then redis.call('DEL', KEYS[4]) end
    return 1
"#;

const LUA_CANCEL: &str = r#"
    local stock = redis.call('GET', KEYS[1])
    if not stock or not tonumber(stock) then return -1 end
    if redis.call('EXISTS', KEYS[4]) == 1 then return 0 end
    redis.call('SET', KEYS[4], '1', 'EX', 604800)
    local confirmed = redis.call('GET', KEYS[2])
    if confirmed == ARGV[1] or confirmed == ARGV[2] then redis.call('DEL', KEYS[2]) end
    if redis.call('GET', KEYS[3]) == ARGV[1] then redis.call('DEL', KEYS[3]) end
    redis.call('INCR', KEYS[1])
    return 1
"#;

static CONFIRM_SCRIPT: LazyLock<Script> = LazyLock::new(|| Script::new(LUA_CONFIRM));
static ROLLBACK_SCRIPT: LazyLock<Script> = LazyLock::new(|| Script::new(LUA_ROLLBACK));
static CANCEL_SCRIPT: LazyLock<Script> = LazyLock::new(|| Script::new(LUA_CANCEL));

const LUA_STUDENT_OP_BEGIN: &str = r#"
    if redis.call('GET', KEYS[2]) == ARGV[1] then return 1 end
    if redis.call('EXISTS', KEYS[1]) == 1 then return -1 end
    if redis.call('SET', KEYS[2], ARGV[1], 'NX', 'EX', tonumber(ARGV[2])) then return 1 end
    return 0
"#;

const LUA_OWNED_DEL: &str = r#"
    if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
    return 0
"#;

static STUDENT_OP_BEGIN_SCRIPT: LazyLock<Script> =
    LazyLock::new(|| Script::new(LUA_STUDENT_OP_BEGIN));
static OWNED_DEL_SCRIPT: LazyLock<Script> = LazyLock::new(|| Script::new(LUA_OWNED_DEL));

// Keep the session-generation protocol byte-for-byte equivalent to main.py.
// A stale login that raced a delete/password change returns 0 and never emits a
// usable cookie; an ordinary successful login increments its account version.
const LUA_SESSION_CREATE: &str = r#"
    local epoch = tonumber(redis.call('GET', KEYS[1]) or '0')
    local version = tonumber(redis.call('GET', KEYS[2]) or '0')
    if not epoch or not version then return -1 end
    if redis.call('EXISTS', KEYS[4]) == 1 or redis.call('EXISTS', KEYS[5]) == 1 then return -2 end
    if epoch ~= tonumber(ARGV[3]) or version ~= tonumber(ARGV[4]) then return 0 end
    local next_version = version + 1
    local payload = cjson.decode(ARGV[1])
    payload['_session_epoch'] = epoch
    payload['_session_version'] = next_version
    local created = redis.pcall('SET', KEYS[3], cjson.encode(payload), 'EX', tonumber(ARGV[2]))
    if type(created) == 'table' and created.err then return -3 end
    local advanced = redis.pcall('INCR', KEYS[2])
    if type(advanced) == 'table' and advanced.err then
      redis.call('DEL', KEYS[3])
      return -3
    end
    if tonumber(advanced) ~= next_version then
      redis.call('DEL', KEYS[3])
      return -3
    end
    return 1
"#;

const LUA_LOGIN_FAIL: &str = r#"
    local n = redis.call('INCR', KEYS[1])
    if n == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1])) end
    return n
"#;

static SESSION_CREATE_SCRIPT: LazyLock<Script> = LazyLock::new(|| Script::new(LUA_SESSION_CREATE));
static LOGIN_FAIL_SCRIPT: LazyLock<Script> = LazyLock::new(|| Script::new(LUA_LOGIN_FAIL));

const REDIS_COMMAND_TIMEOUT: Duration = Duration::from_secs(1);
const REDIS_RESOLUTION_TIMEOUT: Duration = Duration::from_secs(5);
const OPERATION_TTL_SECS: i64 = 120;

async fn redis_deadline<T, F>(future: F) -> AppResult<T>
where
    F: Future<Output = redis::RedisResult<T>>,
{
    tokio::time::timeout(REDIS_COMMAND_TIMEOUT, future)
        .await
        .map_err(|_| AppError::RedisDown)?
        .map_err(|_| AppError::RedisDown)
}

fn parse_session_counter(value: Option<String>) -> AppResult<i64> {
    match value {
        None => Ok(0),
        Some(value) => value
            .parse::<i64>()
            .ok()
            .filter(|value| *value >= 0)
            .ok_or_else(|| AppError::Internal("invalid session generation".into())),
    }
}

fn session_identity(payload: &serde_json::Value) -> Option<(String, String)> {
    match payload.get("role").and_then(|value| value.as_str()) {
        Some("student") => payload
            .get("student_id")
            .and_then(|value| value.as_i64())
            .filter(|student_id| *student_id > 0)
            .map(|student_id| ("student".to_string(), student_id.to_string())),
        Some("admin") => payload
            .get("username")
            .and_then(|value| value.as_str())
            .filter(|username| !username.is_empty() && username.len() <= 80)
            .map(|username| ("admin".to_string(), username.to_string())),
        _ => None,
    }
}

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
    /// A maintenance operation currently owns the write gate.
    Maintenance,
    /// Registration is not open according to Redis TIME/open_at.
    NotOpen,
    /// Administrator locked the published registration list.
    Locked,
}

impl AcquireOutcome {
    fn from_code(code: i64) -> Self {
        match code {
            1 => AcquireOutcome::Ok,
            0 => AcquireOutcome::Full,
            -1 => AcquireOutcome::Already,
            -2 => AcquireOutcome::Uninitialized,
            -3 => AcquireOutcome::Maintenance,
            -4 => AcquireOutcome::NotOpen,
            -6 => AcquireOutcome::Locked,
            _ => AcquireOutcome::Uninitialized,
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
        redis_deadline(redis::cmd("PING").query_async::<String>(&mut conn))
            .await
            .is_ok()
    }

    /// Atomic seat acquire via the Lua script. Redis unreachable -> RedisDown.
    pub async fn acquire(
        &self,
        student_id: i64,
        club_id: i64,
        reservation_value: &str,
        ttl: i64,
    ) -> AppResult<AcquireOutcome> {
        for _ in 0..2 {
            let Ok(mut conn) = self.pool.get().await else {
                continue;
            };
            let result = redis_deadline(
                ACQUIRE_SCRIPT
                    .key(k_stock(club_id))
                    .key(k_stureg(student_id))
                    .key(k_resv(student_id))
                    .key(K_OPENAT)
                    .key(K_MAINT)
                    .key(k_op(student_id))
                    .key(K_REGISTRATION_LOCK)
                    .arg(reservation_value)
                    .arg(ttl)
                    .arg(OPERATION_TTL_SECS)
                    .invoke_async::<i64>(&mut conn),
            )
            .await;
            if let Ok(code) = result {
                return Ok(AcquireOutcome::from_code(code));
            }
        }
        let resolution_deadline =
            tokio::time::Instant::now() + Duration::from_secs(OPERATION_TTL_SECS as u64);
        while tokio::time::Instant::now() < resolution_deadline {
            let Ok(mut conn) = self.pool.get().await else {
                tokio::time::sleep(Duration::from_millis(100)).await;
                continue;
            };
            let owner = tokio::time::timeout(
                REDIS_RESOLUTION_TIMEOUT,
                conn.get::<_, Option<String>>(k_op(student_id)),
            )
            .await;
            match owner {
                Ok(Ok(Some(value))) if value == reservation_value => {
                    return Ok(AcquireOutcome::Ok);
                }
                Ok(Ok(Some(_))) => return Ok(AcquireOutcome::Already),
                Ok(Ok(None)) => return Err(AppError::RedisDown),
                _ => tokio::time::sleep(Duration::from_millis(100)).await,
            }
        }
        Err(AppError::RedisDown)
    }

    /// Confirm only the exact reservation generation that reached SQLite.
    pub async fn confirm(&self, student_id: i64, reservation_value: &str) -> AppResult<bool> {
        for _ in 0..2 {
            let Ok(mut conn) = self.pool.get().await else {
                continue;
            };
            let result = redis_deadline(
                CONFIRM_SCRIPT
                    .key(k_resv(student_id))
                    .key(k_stureg(student_id))
                    .key(k_op(student_id))
                    .arg(reservation_value)
                    .invoke_async::<i64>(&mut conn),
            )
            .await;
            if let Ok(changed) = result {
                return Ok(changed == 1);
            }
        }
        Err(AppError::RedisDown)
    }

    /// Compensate one failed acquire exactly once. The operation key makes a
    /// retry safe even if the reservation TTL has elapsed or a reply was lost.
    pub async fn rollback_reservation(
        &self,
        student_id: i64,
        club_id: i64,
        reservation_value: &str,
    ) -> AppResult<bool> {
        for _ in 0..2 {
            let Ok(mut conn) = self.pool.get().await else {
                continue;
            };
            let result = redis_deadline(
                ROLLBACK_SCRIPT
                    .key(k_stock(club_id))
                    .key(k_resv(student_id))
                    .key(format!("seat:rollback:{reservation_value}"))
                    .key(k_op(student_id))
                    .arg(reservation_value)
                    .invoke_async::<i64>(&mut conn),
            )
            .await;
            if let Ok(changed) = result {
                if changed < 0 {
                    return Err(AppError::Internal("invalid Redis stock state".to_string()));
                }
                return Ok(changed == 1);
            }
        }
        Err(AppError::RedisDown)
    }

    /// Release one committed registration exactly once and only clear Redis
    /// mirrors that belong to its operation generation.
    pub async fn release_registration(
        &self,
        event_id: &str,
        student_id: i64,
        club_id: i64,
        reservation_value: &str,
    ) -> AppResult<bool> {
        for _ in 0..2 {
            let Ok(mut conn) = self.pool.get().await else {
                continue;
            };
            let result = redis_deadline(
                CANCEL_SCRIPT
                    .key(k_stock(club_id))
                    .key(k_stureg(student_id))
                    .key(k_resv(student_id))
                    .key(format!("seat:cancel:{event_id}"))
                    .arg(reservation_value)
                    .arg(club_id)
                    .invoke_async::<i64>(&mut conn),
            )
            .await;
            if let Ok(changed) = result {
                if changed < 0 {
                    return Err(AppError::Internal("invalid Redis stock state".to_string()));
                }
                return Ok(changed == 1);
            }
        }
        Err(AppError::RedisDown)
    }

    /// Acquire a cross-service student mutation lock while respecting the
    /// maintenance fence. Used by cancel; register creates the same key in its
    /// acquire Lua.
    pub async fn begin_student_op(&self, student_id: i64) -> AppResult<Option<String>> {
        let token = new_operation_id();
        for _ in 0..2 {
            let Ok(mut conn) = self.pool.get().await else {
                continue;
            };
            let result = redis_deadline(
                STUDENT_OP_BEGIN_SCRIPT
                    .key(K_MAINT)
                    .key(k_op(student_id))
                    .arg(&token)
                    .arg(OPERATION_TTL_SECS)
                    .invoke_async::<i64>(&mut conn),
            )
            .await;
            if let Ok(code) = result {
                return Ok((code == 1).then_some(token));
            }
        }
        Err(AppError::RedisDown)
    }

    pub async fn end_student_op(&self, student_id: i64, token: &str) -> AppResult<()> {
        for _ in 0..2 {
            let Ok(mut conn) = self.pool.get().await else {
                continue;
            };
            if redis_deadline(
                OWNED_DEL_SCRIPT
                    .key(k_op(student_id))
                    .arg(token)
                    .invoke_async::<i64>(&mut conn),
            )
            .await
            .is_ok()
            {
                return Ok(());
            }
        }
        Err(AppError::RedisDown)
    }

    /// Acquire the global maintenance fence and verify that no mutation from an
    /// older generation is still active. New acquire/cancel operations observe
    /// the marker atomically and refuse to start.
    pub async fn begin_maintenance(&self) -> AppResult<Option<String>> {
        let token = new_operation_id();
        let mut acquired = false;
        let mut conn = self.pool.get().await.map_err(|_| AppError::RedisDown)?;
        for _ in 0..2 {
            let result: AppResult<Option<String>> = redis_deadline(
                redis::cmd("SET")
                    .arg(K_MAINT)
                    .arg(&token)
                    .arg("NX")
                    .arg("EX")
                    .arg(300)
                    .query_async(&mut conn),
            )
            .await;
            match result {
                Ok(Some(_)) => {
                    acquired = true;
                    break;
                }
                Ok(None) => {
                    let owner: Option<String> = redis_deadline(conn.get(K_MAINT)).await?;
                    acquired = owner.as_deref() == Some(token.as_str());
                    break;
                }
                Err(_) => continue,
            }
        }
        if !acquired {
            drop(conn);
            let _ = self.end_maintenance(&token).await;
            return Ok(None);
        }

        for pattern in ["resv:*", "seat:op:*"] {
            let mut cursor = 0u64;
            loop {
                let scan = redis_deadline(
                    redis::cmd("SCAN")
                        .arg(cursor)
                        .arg("MATCH")
                        .arg(pattern)
                        .arg("COUNT")
                        .arg(64)
                        .query_async::<(u64, Vec<String>)>(&mut conn),
                )
                .await;
                let (next, keys) = match scan {
                    Ok(result) => result,
                    Err(e) => {
                        let cleanup = redis_deadline(
                            OWNED_DEL_SCRIPT
                                .key(K_MAINT)
                                .arg(&token)
                                .invoke_async::<i64>(&mut conn),
                        )
                        .await;
                        if cleanup.is_err() {
                            drop(conn);
                            let _ = self.end_maintenance(&token).await;
                        }
                        return Err(e);
                    }
                };
                if !keys.is_empty() {
                    let cleanup = redis_deadline(
                        OWNED_DEL_SCRIPT
                            .key(K_MAINT)
                            .arg(&token)
                            .invoke_async::<i64>(&mut conn),
                    )
                    .await;
                    if cleanup.is_err() {
                        drop(conn);
                        let _ = self.end_maintenance(&token).await;
                    }
                    return Ok(None);
                }
                cursor = next;
                if cursor == 0 {
                    break;
                }
            }
        }
        Ok(Some(token))
    }

    pub async fn end_maintenance(&self, token: &str) -> AppResult<()> {
        for _ in 0..2 {
            let Ok(mut conn) = self.pool.get().await else {
                continue;
            };
            if redis_deadline(
                OWNED_DEL_SCRIPT
                    .key(K_MAINT)
                    .arg(token)
                    .invoke_async::<i64>(&mut conn),
            )
            .await
            .is_ok()
            {
                return Ok(());
            }
        }
        Err(AppError::RedisDown)
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
        let vals: Vec<Option<String>> = redis_deadline(conn.mget(keys)).await.ok()?;
        Some(
            vals.into_iter()
                .map(|v| v.and_then(|s| s.parse::<i64>().ok()))
                .collect(),
        )
    }

    /// Same clock with millisecond precision for browser offset calculation.
    pub async fn now_epoch_ms(&self) -> i64 {
        if let Ok(mut conn) = self.pool.get().await
            && let Ok((secs, usecs)) =
                redis_deadline(redis::cmd("TIME").query_async::<(i64, i64)>(&mut conn)).await
        {
            return secs.saturating_mul(1000) + usecs / 1000;
        }
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0)
    }

    // --- sessions ----------------------------------------------------------

    pub async fn session_role_epoch(&self, role: &str) -> AppResult<i64> {
        let mut conn = self.pool.get().await.map_err(|_| AppError::RedisDown)?;
        let value: Option<String> = redis_deadline(conn.get(k_sess_epoch(role))).await?;
        parse_session_counter(value)
    }

    pub async fn session_principal_version(&self, role: &str, principal: &str) -> AppResult<i64> {
        let mut conn = self.pool.get().await.map_err(|_| AppError::RedisDown)?;
        let value: Option<String> =
            redis_deadline(conn.get(k_sess_version(role, principal))).await?;
        parse_session_counter(value)
    }

    /// Create a session only if the caller's prior database read still matches
    /// the account/role generation. `Ok(None)` means a password change,
    /// deletion, clear-all, or concurrent login invalidated that read.
    pub async fn session_create(
        &self,
        payload: &serde_json::Value,
        ttl: i64,
        expected_epoch: i64,
        expected_version: i64,
    ) -> AppResult<Option<String>> {
        let Some((role, principal)) = session_identity(payload) else {
            return Err(AppError::Internal("invalid session payload".into()));
        };
        let token = gen_token();
        let mut conn = self.pool.get().await.map_err(|_| AppError::RedisDown)?;
        let code = redis_deadline(
            SESSION_CREATE_SCRIPT
                .key(k_sess_epoch(&role))
                .key(k_sess_version(&role, &principal))
                .key(k_sess(&token))
                .key(k_sess_role_mutation(&role))
                .key(k_sess_principal_mutation(&role, &principal))
                .arg(payload.to_string())
                .arg(ttl)
                .arg(expected_epoch)
                .arg(expected_version)
                .invoke_async::<i64>(&mut conn),
        )
        .await?;
        match code {
            1 => Ok(Some(token)),
            0 | -2 => Ok(None),
            _ => Err(AppError::Internal("invalid session generation".into())),
        }
    }

    /// Fetch + validate a session against its role/account generations.
    /// Sessions from before the migration intentionally require one re-login.
    pub async fn session_get(&self, token: &str) -> AppResult<Option<serde_json::Value>> {
        if token.is_empty() {
            return Ok(None);
        }
        let mut conn = self.pool.get().await.map_err(|_| AppError::RedisDown)?;
        let raw: Option<String> = redis_deadline(conn.get(k_sess(token))).await?;
        let Some(raw) = raw else {
            return Ok(None);
        };
        let Ok(payload) = serde_json::from_str::<serde_json::Value>(&raw) else {
            return Ok(None);
        };
        let Some((role, principal)) = session_identity(&payload) else {
            return Ok(None);
        };
        let Some(epoch) = payload.get("_session_epoch").and_then(|v| v.as_i64()) else {
            return Ok(None);
        };
        let Some(version) = payload.get("_session_version").and_then(|v| v.as_i64()) else {
            return Ok(None);
        };
        if epoch < 0 || version < 0 {
            return Ok(None);
        }
        let values: Vec<Option<String>> =
            redis_deadline(conn.mget(vec![k_sess_epoch(&role), k_sess_version(&role, &principal)]))
                .await?;
        let current_epoch = parse_session_counter(values.first().cloned().flatten())?;
        let current_version = parse_session_counter(values.get(1).cloned().flatten())?;
        if epoch != current_epoch || version != current_version {
            return Ok(None);
        }
        Ok(Some(payload))
    }

    // --- login throttle (count failures only) ------------------------------

    /// Read-only: is this key's failure count over `limit`? Errors -> not
    /// blocked (fail open on the throttle so Redis hiccups don't lock users
    /// out; the underlying password check still gates access).
    pub async fn login_blocked(&self, key: &str, limit: i64) -> bool {
        let Ok(mut conn) = self.pool.get().await else {
            return false;
        };
        let n: Option<String> = redis_deadline(conn.get(format!("loginfail:{key}")))
            .await
            .unwrap_or(None);
        n.and_then(|s| s.parse::<i64>().ok()).unwrap_or(0) >= limit
    }

    /// Count one failed attempt; only the first failure starts the 60s window.
    /// This prevents an attacker from indefinitely extending a retry window.
    pub async fn login_fail(&self, key: &str) {
        let Ok(mut conn) = self.pool.get().await else {
            return;
        };
        let k = format!("loginfail:{key}");
        let _ = redis_deadline(
            LOGIN_FAIL_SCRIPT
                .key(k)
                .arg(60)
                .invoke_async::<i64>(&mut conn),
        )
        .await;
    }

    /// Clear a user's failure counter after a successful login.
    pub async fn login_ok(&self, key: &str) {
        if let Ok(mut conn) = self.pool.get().await {
            let _ = redis_deadline(conn.del::<_, ()>(format!("loginfail:{key}"))).await;
        }
    }

    // --- open_at -----------------------------------------------------------

    /// Read the registration-open epoch from Redis. `None` on miss/error.
    pub async fn open_at_get(&self) -> Option<i64> {
        let mut conn = self.pool.get().await.ok()?;
        let v: Option<String> = redis_deadline(conn.get(K_OPENAT)).await.ok()?;
        v.and_then(|s| s.parse::<i64>().ok())
    }

    /// Synchronize the durable registration-open epoch while maintenance owns
    /// the write fence.
    pub async fn open_at_seed(&self, epoch: i64) -> AppResult<bool> {
        let mut conn = self.pool.get().await.map_err(|_| AppError::RedisDown)?;
        let _: () = redis_deadline(
            redis::cmd("SET")
                .arg(K_OPENAT)
                .arg(epoch)
                .query_async(&mut conn),
        )
        .await?;
        Ok(true)
    }

    pub async fn registration_locked_get(&self) -> Option<bool> {
        let mut conn = self.pool.get().await.ok()?;
        let value: Option<String> = redis_deadline(conn.get(K_REGISTRATION_LOCK)).await.ok()?;
        Some(value.as_deref() == Some("1"))
    }

    pub async fn registration_lock_seed(&self, locked: bool) -> AppResult<()> {
        let mut conn = self.pool.get().await.map_err(|_| AppError::RedisDown)?;
        if locked {
            let _: () = redis_deadline(conn.set(K_REGISTRATION_LOCK, "1")).await?;
        } else {
            let _: () = redis_deadline(conn.del(K_REGISTRATION_LOCK)).await?;
        }
        Ok(())
    }

    /// Whether `seats:initialized` is set — used by `/readyz`.
    pub async fn initialized(&self) -> bool {
        let Ok(mut conn) = self.pool.get().await else {
            return false;
        };
        redis_deadline(conn.exists(K_INIT)).await.unwrap_or(false)
    }

    pub async fn maintenance_active(&self) -> Option<bool> {
        let mut conn = self.pool.get().await.ok()?;
        redis_deadline(conn.exists(K_MAINT)).await.ok()
    }

    pub async fn has_active_operations(&self) -> Option<bool> {
        let mut conn = self.pool.get().await.ok()?;
        let mut cursor = 0u64;
        loop {
            let (next, keys): (u64, Vec<String>) = redis_deadline(
                redis::cmd("SCAN")
                    .arg(cursor)
                    .arg("MATCH")
                    .arg("seat:op:*")
                    .arg("COUNT")
                    .arg(128)
                    .query_async(&mut conn),
            )
            .await
            .ok()?;
            if !keys.is_empty() {
                return Some(true);
            }
            cursor = next;
            if cursor == 0 {
                return Some(false);
            }
        }
    }
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
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
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
pub async fn rebuild_stock(seats: &Seats, db: &Db, maintenance_token: &str) -> AppResult<()> {
    if !seats.alive().await {
        tracing::warn!("Redis unavailable, skipping stock rebuild (seckill degraded)");
        return Err(AppError::RedisDown);
    }
    let (clubs, regs) = db.seat_snapshot().await?;

    let mut conn = seats.pool.get().await.map_err(|_| AppError::RedisDown)?;

    // Collect stale keys without changing live state. The maintenance marker
    // blocks mutations; all deletes and replacement values publish in one EXEC.
    let mut stale_keys = Vec::new();
    for pattern in ["stock:club:*", "student:reg:*"] {
        let mut cursor: u64 = 0;
        loop {
            let (next, keys): (u64, Vec<String>) = redis_deadline(
                redis::cmd("SCAN")
                    .arg(cursor)
                    .arg("MATCH")
                    .arg(pattern)
                    .arg("COUNT")
                    .arg(512)
                    .query_async(&mut conn),
            )
            .await?;
            if !keys.is_empty() {
                stale_keys.extend(keys);
            }
            cursor = next;
            if cursor == 0 {
                break;
            }
        }
    }

    redis_deadline(
        redis::cmd("WATCH")
            .arg(K_MAINT)
            .query_async::<()>(&mut conn),
    )
    .await?;
    let owner: Option<String> = redis_deadline(conn.get(K_MAINT)).await?;
    if owner.as_deref() != Some(maintenance_token) {
        let _: Result<(), _> = redis::cmd("UNWATCH").query_async(&mut conn).await;
        return Err(AppError::Internal(
            "maintenance lease changed before stock publish".to_string(),
        ));
    }

    let mut pipe = redis::pipe();
    pipe.atomic();
    if !stale_keys.is_empty() {
        pipe.del(stale_keys).ignore();
    }
    for c in &clubs {
        let left = (c.max_students - c.used).max(0);
        pipe.set(k_stock(c.club_id), left).ignore();
    }
    for (sid, cid, operation_id) in &regs {
        let value = operation_id
            .as_ref()
            .map(|op| reservation_value(*cid, op))
            .unwrap_or_else(|| cid.to_string());
        pipe.set(k_stureg(*sid), value).ignore();
    }
    pipe.set(K_INIT, "1").ignore();
    pipe.del(K_CACHE_CLUBS).ignore();
    let committed: Option<()> = redis_deadline(pipe.query_async(&mut conn)).await?;
    require_watch_commit(committed)?;
    // SQLite marks a club locally unavailable when a low-frequency admin
    // mutation commits but its targeted Redis publication is interrupted.  A
    // completed full snapshot above is authoritative for every club, so it is
    // now safe to release those durable per-club holds.
    db.clear_all_seat_sync_pending().await?;

    tracing::info!(clubs = clubs.len(), "Redis stock rebuilt");
    Ok(())
}

/// Redis returns Nil from EXEC when a watched key changed. `()` would parse
/// that Nil as success in redis-rs, so keep the Option boundary explicit.
fn require_watch_commit(committed: Option<()>) -> AppResult<()> {
    committed.ok_or_else(|| {
        AppError::Internal("maintenance lease changed during stock publish".to_string())
    })
}

/// Seed `open_at` from `settings.registration_start_time` at startup. Parses the
/// `YYYY-MM-DD HH:MM:SS` local-time string to an epoch. Best-effort.
pub async fn seed_open_at(seats: &Seats, db: &Db) -> AppResult<()> {
    if let Some(value) = db.registration_start_time().await? {
        let epoch = crate::auth::parse_local_datetime(&value).ok_or_else(|| {
            AppError::Internal(format!("registration_start_time unparseable: {value}"))
        })?;
        seats.open_at_seed(epoch).await?;
    }
    seats
        .registration_lock_seed(db.registration_locked().await?)
        .await?;
    Ok(())
}

/// Redis value shared by reservation and confirmed-registration mirrors.
pub fn reservation_value(club_id: i64, operation_id: &str) -> String {
    format!("{club_id}|{operation_id}")
}

/// A URL-safe random identifier for one registration generation.
pub fn new_operation_id() -> String {
    gen_token()
}

#[cfg(test)]
mod reconcile_tests {
    use super::require_watch_commit;
    use redis::FromRedisValue;

    #[test]
    fn watched_exec_abort_is_not_a_successful_rebuild() {
        assert!(require_watch_commit(Some(())).is_ok());
        assert!(require_watch_commit(None).is_err());
    }

    #[test]
    fn redis_exec_nil_deserializes_to_none_not_success() {
        let parsed = Option::<()>::from_redis_value(redis::Value::Nil).unwrap();
        assert_eq!(None, parsed);
        let committed = Option::<()>::from_redis_value(redis::Value::Array(vec![])).unwrap();
        assert_eq!(Some(()), committed);
    }
}
