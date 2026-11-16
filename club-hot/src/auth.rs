//! 口令哈希、Cookie、会话读取与学校时区下的时间换算。

use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::mpsc::{SyncSender, sync_channel};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use argon2::password_hash::{PasswordHash, PasswordHasher, PasswordVerifier, SaltString};
use argon2::{Algorithm, Argon2, Params, Version};
use axum::http::{HeaderMap, header};

use crate::error::AppResult;
use crate::state::{AppState, Config};

static OFFSET: OnceLock<time::UtcOffset> = OnceLock::new();
static ARGON2_PARAMS: OnceLock<(u32, u32, u32)> = OnceLock::new();

/// 启动时调用一次：记录学校所在时区与 Argon2 参数。
pub fn init(cfg: &Config) {
    let _ = OFFSET.set(parse_offset(&cfg.utc_offset));
    let _ = ARGON2_PARAMS.set((
        cfg.argon2_memory_kib,
        cfg.argon2_time_cost,
        cfg.argon2_parallelism,
    ));
}

fn parse_offset(text: &str) -> time::UtcOffset {
    let text = text.trim();
    let (sign, rest) = match text.as_bytes().first() {
        Some(b'+') => (1, &text[1..]),
        Some(b'-') => (-1, &text[1..]),
        _ => return time::UtcOffset::from_hms(8, 0, 0).expect("valid offset"),
    };
    let digits: String = rest.chars().filter(|c| *c != ':').collect();
    let hours = digits.get(0..2).and_then(|h| h.parse::<i8>().ok());
    let minutes = digits.get(2..4).and_then(|m| m.parse::<i8>().ok());
    match (hours, minutes) {
        (Some(h), Some(m)) => time::UtcOffset::from_hms(sign * h, sign * m, 0)
            .unwrap_or_else(|_| time::UtcOffset::from_hms(8, 0, 0).expect("valid offset")),
        _ => time::UtcOffset::from_hms(8, 0, 0).expect("valid offset"),
    }
}

fn offset() -> time::UtcOffset {
    OFFSET
        .get()
        .copied()
        .unwrap_or_else(|| time::UtcOffset::from_hms(8, 0, 0).expect("valid offset"))
}

fn hasher() -> Argon2<'static> {
    let (m, t, p) = ARGON2_PARAMS.get().copied().unwrap_or((19_456, 2, 1));
    let params = Params::new(m, t, p, None).unwrap_or_default();
    Argon2::new(Algorithm::Argon2id, Version::V0x13, params)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VerifyResult {
    pub ok: bool,
    pub needs_upgrade: bool,
}

/// 校验口令。参数与当前配置不同的 argon2 哈希、存量明文在登录成功后升级。
pub fn verify_password(stored: &str, plain: &str) -> VerifyResult {
    if stored.starts_with("$argon2") {
        let Ok(parsed) = PasswordHash::new(stored) else {
            return VerifyResult {
                ok: false,
                needs_upgrade: false,
            };
        };
        let ok = Argon2::default()
            .verify_password(plain.as_bytes(), &parsed)
            .is_ok();
        let (m, t, p) = ARGON2_PARAMS.get().copied().unwrap_or((19_456, 2, 1));
        let current = Params::try_from(&parsed).ok();
        let needs_upgrade = parsed.algorithm.as_str() != "argon2id"
            || current.is_none_or(|c| c.m_cost() != m || c.t_cost() != t || c.p_cost() != p);
        VerifyResult {
            ok,
            needs_upgrade: ok && needs_upgrade,
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

pub fn hash_password(plain: &str) -> Result<String, argon2::password_hash::Error> {
    let mut salt_bytes = [0u8; 16];
    {
        use rand::RngExt;
        rand::rng().fill(&mut salt_bytes[..]);
    }
    let salt = SaltString::encode_b64(&salt_bytes)?;
    Ok(hasher().hash_password(plain.as_bytes(), &salt)?.to_string())
}

fn ct_eq(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    let mut diff = a.len() ^ b.len();
    for i in 0..a.len().max(b.len()) {
        diff |= (*a.get(i).unwrap_or(&0) ^ *b.get(i).unwrap_or(&0)) as usize;
    }
    diff == 0
}

// ---- 口令哈希队列 -----------------------------------------------------------

type Job = Box<dyn FnOnce() + Send + 'static>;

/// 口令哈希在固定数量的线程里先到先算，占用内存不超过线程数 × ARGON2_MEMORY_KIB。
///
/// 排不进去的请求按服务速度依次约定回来的时间（Retry-After），回来时队列正好有空位。
pub struct AuthPool {
    sender: SyncSender<Job>,
    pending: AtomicUsize,
    limit: usize,
    workers: usize,
    budget_us: u64,
    average_us: AtomicU64,
    next_slot: Mutex<Instant>,
}

impl AuthPool {
    /// wait 是请求在队列里最多等待的时间；按最近耗时估算超过它的八成就不再接收。
    pub fn new(workers: usize, limit: usize, wait: Duration) -> Arc<Self> {
        let workers = workers.max(1);
        let limit = limit.max(workers);
        let (sender, receiver) = sync_channel::<Job>(limit);
        let receiver = Arc::new(Mutex::new(receiver));
        for index in 0..workers {
            let receiver = receiver.clone();
            std::thread::Builder::new()
                .name(format!("auth-{index}"))
                .spawn(move || {
                    loop {
                        let next = match receiver.lock() {
                            Ok(guard) => guard.recv(),
                            Err(_) => return,
                        };
                        match next {
                            Ok(job) => job(),
                            Err(_) => return,
                        }
                    }
                })
                .expect("spawn auth worker");
        }
        Arc::new(AuthPool {
            sender,
            pending: AtomicUsize::new(0),
            limit,
            workers,
            budget_us: wait.as_micros().min(u64::MAX as u128) as u64 * 4 / 5,
            average_us: AtomicU64::new(100_000),
            next_slot: Mutex::new(Instant::now()),
        })
    }

    fn over_budget(&self, pending: usize) -> bool {
        let average = self.average_us.load(Ordering::Relaxed);
        pending >= self.limit || pending as u64 * average / self.workers as u64 >= self.budget_us
    }

    /// 给排不进去的请求约定回来的时间，返回建议等待的秒数。
    fn defer(&self) -> u64 {
        let average = Duration::from_micros(self.average_us.load(Ordering::Relaxed));
        let interval = average / self.workers as u32;
        let pending = self.pending.load(Ordering::Acquire).min(u32::MAX as usize) as u32;
        let now = Instant::now();
        let Ok(mut next) = self.next_slot.lock() else {
            return 1;
        };
        *next = (*next).max(now + interval * pending) + interval;
        ((*next - now).as_secs() + 1).clamp(1, 30)
    }

    /// 有空位返回 None，否则返回建议等待的秒数。
    pub fn saturated(&self) -> Option<u64> {
        self.over_budget(self.pending.load(Ordering::Acquire))
            .then(|| self.defer())
    }

    fn record(&self, elapsed: Duration) {
        let sample = elapsed.as_micros().min(u64::MAX as u128) as u64;
        let average = self.average_us.load(Ordering::Relaxed);
        let next = if sample >= average {
            average + (sample - average) / 5
        } else {
            average - (average - sample) / 5
        };
        self.average_us.store(next, Ordering::Relaxed);
    }

    /// 排队执行 work；排不进去或等待超过 wait 时返回建议等待的秒数。
    pub async fn run<T, F>(self: &Arc<Self>, wait: Duration, work: F) -> Result<T, u64>
    where
        T: Send + 'static,
        F: FnOnce() -> T + Send + 'static,
    {
        if self.over_budget(self.pending.fetch_add(1, Ordering::AcqRel)) {
            self.pending.fetch_sub(1, Ordering::AcqRel);
            return Err(self.defer());
        }
        let (tx, rx) = tokio::sync::oneshot::channel();
        let pool = self.clone();
        let job: Job = Box::new(move || {
            let value = (!tx.is_closed()).then(|| {
                let started = Instant::now();
                let value = work();
                pool.record(started.elapsed());
                value
            });
            pool.pending.fetch_sub(1, Ordering::AcqRel);
            if let Some(value) = value {
                let _ = tx.send(value);
            }
        });
        if self.sender.try_send(job).is_err() {
            self.pending.fetch_sub(1, Ordering::AcqRel);
            return Err(self.defer());
        }
        match tokio::time::timeout(wait, rx).await {
            Ok(Ok(value)) => Ok(value),
            _ => Err(1),
        }
    }
}

// ---- Cookie ----------------------------------------------------------------

/// 只接受唯一的同名 Cookie，与 Python 判定一致。
pub fn cookie_value(cookie_header: &str, name: &str) -> Option<String> {
    let mut found: Option<String> = None;
    for part in cookie_header.split(';') {
        let Some((key, value)) = part.split_once('=') else {
            continue;
        };
        if key.trim() == name {
            let value = value.trim();
            if found.is_some() || value.is_empty() {
                return None;
            }
            found = Some(value.to_string());
        }
    }
    found
}

/// 请求只能带一行 Cookie 头。
fn single_cookie_header(headers: &HeaderMap) -> Option<&str> {
    let mut values = headers.get_all(header::COOKIE).iter();
    let raw = values.next()?.to_str().ok()?;
    if values.next().is_some() {
        return None;
    }
    Some(raw)
}

pub fn session_token_from_headers(headers: &HeaderMap) -> Option<String> {
    cookie_value(single_cookie_header(headers)?, "session")
}

pub fn device_token_from_headers(headers: &HeaderMap) -> Option<String> {
    let value = cookie_value(single_cookie_header(headers)?, "device")?;
    let valid = (20..=64).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_');
    valid.then_some(value)
}

pub fn cookie(name: &str, value: &str, max_age: i64, secure: bool) -> String {
    let secure = if secure { "; Secure" } else { "" };
    format!("{name}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}{secure}")
}

// ---- 会话 --------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct StudentSession {
    pub student_id: i64,
    pub grade: Option<String>,
    pub class: Option<String>,
}

pub async fn student_session(
    state: &AppState,
    headers: &HeaderMap,
) -> AppResult<Option<StudentSession>> {
    let Some(token) = session_token_from_headers(headers) else {
        return Ok(None);
    };
    let seats = crate::redis_seats::Seats::new(state.redis.clone(), &state.cfg);
    let Some(payload) = seats.session_get(&token).await? else {
        return Ok(None);
    };
    if payload.get("role").and_then(|v| v.as_str()) != Some("student") {
        return Ok(None);
    }
    let text = |key: &str| {
        payload
            .get(key)
            .and_then(|v| v.as_str())
            .map(str::to_string)
    };
    Ok(payload
        .get("student_id")
        .and_then(|v| v.as_i64())
        .map(|student_id| StudentSession {
            student_id,
            grade: text("grade"),
            class: text("class"),
        }))
}

// ---- 时间 --------------------------------------------------------------------

/// 按学校时区解析 `YYYY-MM-DD HH:MM:SS`。
pub fn parse_local_datetime(s: &str) -> Option<i64> {
    use time::macros::format_description;
    let fmt = format_description!("[year]-[month]-[day] [hour]:[minute]:[second]");
    let naive = time::PrimitiveDateTime::parse(s.trim(), &fmt).ok()?;
    Some(naive.assume_offset(offset()).unix_timestamp())
}

pub fn format_local_datetime(epoch: i64) -> Option<String> {
    use time::macros::format_description;
    let fmt = format_description!("[year]-[month]-[day] [hour]:[minute]:[second]");
    time::OffsetDateTime::from_unix_timestamp(epoch)
        .ok()?
        .to_offset(offset())
        .format(&fmt)
        .ok()
}

pub fn now_local_string() -> String {
    use time::macros::format_description;
    let fmt = format_description!("[year]-[month]-[day] [hour]:[minute]:[second]");
    time::OffsetDateTime::now_utc()
        .to_offset(offset())
        .format(&fmt)
        .unwrap_or_else(|_| "1970-01-01 00:00:00".to_string())
}

#[cfg(test)]
mod tests {
    use super::{
        AuthPool, cookie_value, parse_local_datetime, parse_offset, session_token_from_headers,
        verify_password,
    };
    use axum::http::{HeaderMap, header::COOKIE};

    #[test]
    fn cookie_parser_rejects_duplicates_and_ignores_malformed_other_pairs() {
        assert_eq!(
            cookie_value("x; session=one; y=2", "session"),
            Some("one".into())
        );
        assert_eq!(cookie_value("session=one; session=two", "session"), None);
        assert_eq!(cookie_value("session=; session=two", "session"), None);
        assert_eq!(cookie_value("Session=one", "session"), None);
    }

    #[test]
    fn cookie_parser_rejects_multiple_header_fields() {
        let mut headers = HeaderMap::new();
        headers.append(COOKIE, "session=one".parse().unwrap());
        headers.append(COOKIE, "theme=dark".parse().unwrap());
        assert_eq!(session_token_from_headers(&headers), None);
    }

    #[test]
    fn legacy_plaintext_password_remains_compatible_during_one_time_upgrade() {
        assert!(verify_password("student-password", "student-password").ok);
        assert!(!verify_password("student-password", "wrong-password").ok);
    }

    #[test]
    fn python_argon2_cffi_hash_verifies_in_rust_and_is_marked_for_upgrade() {
        let python_hash = "$argon2id$v=19$m=65536,t=3,p=4$ZwUxbnMzOherbEkDhuvTlg$53zarfOwxfaDdhJ9Slnvd6aqcoV4qFllM8UlLDig+AA";
        let result = verify_password(python_hash, "student-password");
        assert!(result.ok);
        assert!(result.needs_upgrade);
        assert!(!verify_password(python_hash, "wrong-password").ok);
    }

    #[test]
    fn school_offset_is_independent_of_the_host_timezone() {
        let _ = super::OFFSET.set(parse_offset("+08:00"));
        assert_eq!(
            Some(1_788_235_200),
            parse_local_datetime("2026-09-01 12:00:00")
        );
        assert_eq!(-5 * 3600 - 30 * 60, parse_offset("-05:30").whole_seconds());
    }

    #[tokio::test]
    async fn auth_pool_bounds_waiting_work_and_spaces_return_times() {
        use std::sync::atomic::Ordering;
        use std::time::Duration;

        let pool = AuthPool::new(1, 2, Duration::from_secs(10));
        let (release, wait) = std::sync::mpsc::channel::<()>();
        let blocked = {
            let pool = pool.clone();
            tokio::spawn(async move {
                pool.run(Duration::from_secs(5), move || wait.recv().is_ok())
                    .await
            })
        };
        while pool.pending.load(Ordering::Acquire) < 1 {
            tokio::task::yield_now().await;
        }
        let queued = {
            let pool = pool.clone();
            tokio::spawn(async move { pool.run(Duration::from_secs(5), || 7).await })
        };
        while pool.pending.load(Ordering::Acquire) < 2 {
            tokio::task::yield_now().await;
        }
        let first_wait = pool.saturated().expect("queue is full");
        let later_wait = pool.run(Duration::from_secs(1), || 8).await.unwrap_err();
        assert!((1..=30).contains(&first_wait));
        assert!(first_wait <= later_wait && later_wait <= 30);
        release.send(()).expect("release first job");
        assert_eq!(Ok(true), blocked.await.expect("first job"));
        assert_eq!(Ok(7), queued.await.expect("queued job"));
        assert_eq!(None, pool.saturated());
        assert_eq!(Ok(9), pool.run(Duration::from_millis(500), || 9).await);
    }
}
