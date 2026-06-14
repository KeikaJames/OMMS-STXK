//! Password hashing, cookie/session plumbing, and the local-time parser used by
//! the registration time gate.
//!
//! argon2 is PHC-format (`$argon2id$v=19$...`), interoperable with Python's
//! `argon2-cffi`: verification reads the cost params out of the stored hash, so
//! a hash minted by either side verifies on the other. Legacy plaintext rows
//! are matched directly and rehashed on first successful login (parity with
//! `verify_password` in `main.py`).

use std::sync::OnceLock;

use argon2::password_hash::{PasswordHash, PasswordHasher, PasswordVerifier, SaltString};
use argon2::Argon2;

use crate::state::AppState;

/// Process-wide local UTC offset, captured once on the main thread *before* the
/// async runtime spawns worker threads. `time::UtcOffset::current_local_offset`
/// refuses to run in a multithreaded process (soundness around `getenv`), so we
/// snapshot it early; all local-time formatting/parsing reads this. Falls back
/// to UTC if never initialized or if the platform can't determine it.
static LOCAL_OFFSET: OnceLock<time::UtcOffset> = OnceLock::new();

/// Capture the host local offset. Call exactly once, from `main`, before any
/// Tokio worker threads exist. Idempotent (later calls are ignored).
pub fn init_local_offset() {
    let off = time::UtcOffset::current_local_offset().unwrap_or(time::UtcOffset::UTC);
    let _ = LOCAL_OFFSET.set(off);
}

/// The captured local offset (UTC if `init_local_offset` was never called).
fn local_offset() -> time::UtcOffset {
    LOCAL_OFFSET.get().copied().unwrap_or(time::UtcOffset::UTC)
}

/// Result of verifying a stored secret against a supplied password.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VerifyResult {
    /// Password matched.
    pub ok: bool,
    /// Stored form is legacy/plaintext (or rehash recommended) -> upgrade.
    pub needs_upgrade: bool,
}

/// Verify `plain` against the `stored` secret.
/// - `$argon2*`  -> argon2 verify; `needs_upgrade=false` on match.
/// - `plain$xxx` -> compare suffix; always `needs_upgrade=true` on match.
/// - anything else (legacy plaintext like "123456") -> direct compare;
///   `needs_upgrade=true` on match.
pub fn verify_password(stored: &str, plain: &str) -> VerifyResult {
    if stored.starts_with("$argon2") {
        match PasswordHash::new(stored) {
            Ok(parsed) => {
                let ok = Argon2::default()
                    .verify_password(plain.as_bytes(), &parsed)
                    .is_ok();
                VerifyResult {
                    ok,
                    needs_upgrade: false,
                }
            }
            Err(_) => VerifyResult {
                ok: false,
                needs_upgrade: false,
            },
        }
    } else if let Some(rest) = stored.strip_prefix("plain$") {
        VerifyResult {
            ok: ct_eq(rest, plain),
            needs_upgrade: true,
        }
    } else {
        VerifyResult {
            ok: ct_eq(stored, plain),
            needs_upgrade: true,
        }
    }
}

/// Hash a password to a PHC argon2id string compatible with argon2-cffi.
pub fn hash_password(plain: &str) -> Result<String, argon2::password_hash::Error> {
    // 16 random salt bytes -> base64 SaltString (avoids cross-crate rand_core
    // version coupling that `SaltString::generate` would impose).
    let mut salt_bytes = [0u8; 16];
    {
        use rand::Rng;
        rand::rng().fill(&mut salt_bytes[..]);
    }
    let salt = SaltString::encode_b64(&salt_bytes)?;
    let hash = Argon2::default().hash_password(plain.as_bytes(), &salt)?;
    Ok(hash.to_string())
}

/// Constant-time-ish string equality (length-independent compare to avoid
/// trivial timing leaks; `secrets.compare_digest` analogue).
fn ct_eq(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    let mut diff = a.len() ^ b.len();
    let n = a.len().max(b.len());
    for i in 0..n {
        let x = *a.get(i).unwrap_or(&0);
        let y = *b.get(i).unwrap_or(&0);
        diff |= (x ^ y) as usize;
    }
    diff == 0
}

// --- cookies ---------------------------------------------------------------

/// Extract the `session` cookie value from a raw `Cookie:` header.
/// Handles multiple `;`-separated pairs; ignores attributes.
pub fn session_token_from_cookie(cookie_header: &str) -> Option<String> {
    for part in cookie_header.split(';') {
        let part = part.trim();
        if let Some(val) = part.strip_prefix("session=") {
            if !val.is_empty() {
                return Some(val.to_string());
            }
        }
    }
    None
}

/// Build the `Set-Cookie` value for a freshly minted session.
/// Mirrors Python exactly: HttpOnly; SameSite=Strict; Path=/; Max-Age=ttl.
pub fn set_session_cookie(token: &str, ttl: i64) -> String {
    format!("session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={ttl}")
}

/// Build the `Set-Cookie` value that clears the session (logout).
pub fn clear_session_cookie() -> String {
    "session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0".to_string()
}

// --- session payload -------------------------------------------------------

/// A decoded student session. The JSON written to `sess:{token}` matches the
/// Python shape: `{"role":"student","student_id":i64,"name","class","student_no"}`.
#[derive(Debug, Clone)]
pub struct StudentSession {
    pub student_id: i64,
    pub name: String,
    pub class: String,
    pub student_no: String,
}

/// Pull the student session out of state for the given request headers.
/// Returns `None` when there is no valid student session (missing cookie,
/// expired, wrong role, or malformed JSON).
pub async fn student_session(
    state: &AppState,
    headers: &axum::http::HeaderMap,
) -> Option<StudentSession> {
    let raw = headers.get(axum::http::header::COOKIE)?.to_str().ok()?;
    let token = session_token_from_cookie(raw)?;
    let seats = crate::redis_seats::Seats::new(state.redis.clone());
    let payload = seats.session_get(&token).await?;
    if payload.get("role").and_then(|v| v.as_str()) != Some("student") {
        return None;
    }
    Some(StudentSession {
        student_id: payload.get("student_id").and_then(|v| v.as_i64())?,
        name: payload
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string(),
        class: payload
            .get("class")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string(),
        student_no: payload
            .get("student_no")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string(),
    })
}

// --- time ------------------------------------------------------------------

/// Parse a `YYYY-MM-DD HH:MM:SS` string (Python `registration_start_time`
/// format) into a Unix epoch, interpreting it in the host's local timezone to
/// match Python's `datetime.strptime(...).timestamp()`. Falls back to treating
/// it as UTC if the local offset can't be determined.
pub fn parse_local_datetime(s: &str) -> Option<i64> {
    use time::macros::format_description;
    let fmt = format_description!("[year]-[month]-[day] [hour]:[minute]:[second]");
    let naive = time::PrimitiveDateTime::parse(s.trim(), &fmt).ok()?;
    Some(naive.assume_offset(local_offset()).unix_timestamp())
}

/// Render an epoch back to `YYYY-MM-DD HH:MM:SS` in local time — used so
/// `/api/check_registration_time` can echo `start_time` when only `open_at`
/// (epoch) is present in Redis, matching Python's `datetime.fromtimestamp`.
pub fn format_local_datetime(epoch: i64) -> Option<String> {
    use time::macros::format_description;
    let fmt = format_description!("[year]-[month]-[day] [hour]:[minute]:[second]");
    let dt = time::OffsetDateTime::from_unix_timestamp(epoch)
        .ok()?
        .to_offset(local_offset());
    dt.format(&fmt).ok()
}

/// `YYYY-MM-DD HH:MM:SS` for "now" in local time — matches Python's
/// `datetime.now().strftime(...)` used for `registration_time`.
pub fn now_local_string() -> String {
    use time::macros::format_description;
    let fmt = format_description!("[year]-[month]-[day] [hour]:[minute]:[second]");
    time::OffsetDateTime::now_utc()
        .to_offset(local_offset())
        .format(&fmt)
        .unwrap_or_else(|_| "1970-01-01 00:00:00".to_string())
}
