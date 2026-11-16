//! Redis 名额与会话：与 main.py 共用键名、Lua 脚本（shared/lua）和会话格式。
//!
//! 写路径在 Redis 不可达时返回 [`AppError::RedisDown`]（503）；读路径失败时返回 None，
//! 由调用方回落 SQLite。

use std::collections::{BTreeMap, HashSet};
use std::future::Future;
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use deadpool_redis::Pool as RedisPool;
use redis::{AsyncCommands, Script};

use crate::db::{Db, Snapshot};
use crate::error::{AppError, AppResult};
use crate::state::Config;

static ACQUIRE: LazyLock<Script> =
    LazyLock::new(|| Script::new(include_str!("../../shared/lua/acquire.lua")));
static CONFIRM: LazyLock<Script> =
    LazyLock::new(|| Script::new(include_str!("../../shared/lua/confirm.lua")));
static ROLLBACK: LazyLock<Script> =
    LazyLock::new(|| Script::new(include_str!("../../shared/lua/rollback.lua")));
static CANCEL_BEGIN: LazyLock<Script> =
    LazyLock::new(|| Script::new(include_str!("../../shared/lua/cancel_begin.lua")));
static CANCEL_RELEASE: LazyLock<Script> =
    LazyLock::new(|| Script::new(include_str!("../../shared/lua/cancel_release.lua")));
static LEASE_END: LazyLock<Script> =
    LazyLock::new(|| Script::new(include_str!("../../shared/lua/lease_end.lua")));
static FENCE_PUBLISH: LazyLock<Script> =
    LazyLock::new(|| Script::new(include_str!("../../shared/lua/fence_publish.lua")));
static MIRROR_SET: LazyLock<Script> =
    LazyLock::new(|| Script::new(include_str!("../../shared/lua/mirror_set.lua")));
static SESSION_CREATE: LazyLock<Script> =
    LazyLock::new(|| Script::new(include_str!("../../shared/lua/session_create.lua")));
static LOGIN_FAIL: LazyLock<Script> =
    LazyLock::new(|| Script::new(include_str!("../../shared/lua/login_fail.lua")));

const MAINTENANCE_TTL: i64 = 300;
const FENCE_TTL: i64 = 30;
const FENCE_WAIT: Duration = Duration::from_secs(5);
const LOGIN_WINDOW: i64 = 60;
const DEVICE_TTL: i64 = 180 * 24 * 3600;

/// 抢位脚本的结果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Acquire {
    Acquired,
    OtherAttempt,
    Full,
    Existing,
    Uninitialized,
    Fenced,
    NotOpen,
    Locked,
    Invalid,
}

impl Acquire {
    fn from_code(code: i64) -> Self {
        match code {
            1 | 2 => Acquire::Acquired,
            3 => Acquire::OtherAttempt,
            0 => Acquire::Full,
            -1 => Acquire::Existing,
            -2 => Acquire::Uninitialized,
            -3 => Acquire::Fenced,
            -4 => Acquire::NotOpen,
            -6 => Acquire::Locked,
            _ => Acquire::Invalid,
        }
    }
}

#[derive(Clone)]
pub struct Seats {
    pool: RedisPool,
    prefix: String,
    timeout: Duration,
    op_ttl: i64,
}

impl Seats {
    pub fn new(pool: RedisPool, cfg: &Config) -> Self {
        Seats {
            pool,
            prefix: cfg.redis_prefix.clone(),
            timeout: cfg.redis_timeout,
            op_ttl: cfg.seat_op_ttl,
        }
    }

    // ---- 键 ----
    fn key(&self, parts: &[&dyn std::fmt::Display]) -> String {
        let joined: Vec<String> = parts.iter().map(|p| p.to_string()).collect();
        format!("{}{}", self.prefix, joined.join(":"))
    }
    pub fn k_stock(&self, club: i64) -> String {
        self.key(&[&"stock", &club])
    }
    pub fn k_reg(&self, student: i64) -> String {
        self.key(&[&"reg", &student])
    }
    pub fn k_op(&self, student: i64) -> String {
        self.key(&[&"op", &student])
    }
    pub fn k_inflight(&self, club: i64) -> String {
        self.key(&[&"inflight", &club])
    }
    fn k_fence(&self, club: i64) -> String {
        self.key(&[&"fence", &club])
    }
    fn k_open_at(&self) -> String {
        self.key(&[&"open_at"])
    }
    fn k_locked(&self) -> String {
        self.key(&[&"locked"])
    }
    fn k_init(&self) -> String {
        self.key(&[&"init"])
    }
    fn k_maint(&self) -> String {
        self.key(&[&"maint"])
    }
    fn k_repair(&self) -> String {
        self.key(&[&"repair"])
    }
    fn k_sess(&self, token: &str) -> String {
        self.key(&[&"sess", &token])
    }
    fn k_epoch(&self, role: &str) -> String {
        self.key(&[&"sessgen", &"epoch", &role])
    }
    fn k_version(&self, role: &str, principal: &str) -> String {
        self.key(&[&"sessgen", &"ver", &role, &principal])
    }
    fn k_role_mutation(&self, role: &str) -> String {
        self.key(&[&"sessmut", &"role", &role])
    }
    fn k_principal_mutation(&self, role: &str, principal: &str) -> String {
        self.key(&[&"sessmut", &role, &principal])
    }
    fn k_fail(&self, key: &str) -> String {
        self.key(&[&"fail", &key])
    }
    fn k_trust(&self, role: &str, username: &str, device: &str) -> String {
        self.key(&[&"trust", &role, &username, &device])
    }

    async fn deadline<T, F>(&self, future: F) -> AppResult<T>
    where
        F: Future<Output = redis::RedisResult<T>>,
    {
        tokio::time::timeout(self.timeout, future)
            .await
            .map_err(|_| AppError::RedisDown)?
            .map_err(|_| AppError::RedisDown)
    }

    async fn conn(&self) -> AppResult<deadpool_redis::Connection> {
        self.pool.get().await.map_err(|_| AppError::RedisDown)
    }

    /// 运行脚本；失败重试一次（脚本对同一参数幂等）。
    async fn run(&self, script: &Script, keys: &[String], args: &[String]) -> AppResult<i64> {
        for _ in 0..2 {
            let Ok(mut conn) = self.pool.get().await else {
                continue;
            };
            let mut invocation = script.prepare_invoke();
            for key in keys {
                invocation.key(key);
            }
            for arg in args {
                invocation.arg(arg);
            }
            if let Ok(code) = self
                .deadline(invocation.invoke_async::<i64>(&mut conn))
                .await
            {
                return Ok(code);
            }
        }
        Err(AppError::RedisDown)
    }

    pub async fn alive(&self) -> bool {
        let Ok(mut conn) = self.pool.get().await else {
            return false;
        };
        self.deadline(redis::cmd("PING").query_async::<String>(&mut conn))
            .await
            .is_ok()
    }

    pub async fn initialized(&self) -> bool {
        let Ok(mut conn) = self.pool.get().await else {
            return false;
        };
        self.deadline(conn.exists(self.k_init()))
            .await
            .unwrap_or(false)
    }

    pub async fn maintenance_active(&self) -> Option<bool> {
        let mut conn = self.pool.get().await.ok()?;
        self.deadline(conn.exists(self.k_maint())).await.ok()
    }

    // ---- 抢位、确认、撤销 ----
    pub async fn acquire(
        &self,
        student: i64,
        club: i64,
        lease: &str,
        member: &str,
        idem_prefix: &str,
    ) -> AppResult<Acquire> {
        let keys = [
            self.k_stock(club),
            self.k_reg(student),
            self.k_op(student),
            self.k_open_at(),
            self.k_maint(),
            self.k_locked(),
            self.k_fence(club),
            self.k_inflight(club),
        ];
        let args = [
            lease.to_string(),
            self.op_ttl.to_string(),
            member.to_string(),
            idem_prefix.to_string(),
        ];
        Ok(Acquire::from_code(self.run(&ACQUIRE, &keys, &args).await?))
    }

    pub async fn confirm(
        &self,
        student: i64,
        club: i64,
        lease: &str,
        mirror: &str,
        member: &str,
    ) -> AppResult<bool> {
        let keys = [
            self.k_op(student),
            self.k_reg(student),
            self.k_inflight(club),
        ];
        let args = [lease.to_string(), mirror.to_string(), member.to_string()];
        Ok(self.run(&CONFIRM, &keys, &args).await? == 1)
    }

    /// 撤销抢位；absorb=true 时不归还库存。租约失效时登记该社团待修复。
    pub async fn rollback(
        &self,
        student: i64,
        club: i64,
        lease: &str,
        member: &str,
        absorb: bool,
    ) -> AppResult<i64> {
        let keys = [
            self.k_stock(club),
            self.k_op(student),
            self.k_inflight(club),
            self.key(&[&"rb", &lease]),
        ];
        let mode = if absorb { "absorb" } else { "return" };
        let args = [lease.to_string(), member.to_string(), mode.to_string()];
        let code = self.run(&ROLLBACK, &keys, &args).await?;
        if code == 2 {
            self.request_repair(club).await;
        }
        if code < 0 {
            return Err(AppError::Internal("stock key missing".into()));
        }
        Ok(code)
    }

    pub async fn cancel_begin(
        &self,
        student: i64,
        club: i64,
        lease: &str,
        member: &str,
    ) -> AppResult<i64> {
        let keys = [
            self.k_maint(),
            self.k_fence(club),
            self.k_op(student),
            self.k_inflight(club),
            self.k_locked(),
        ];
        let args = [
            lease.to_string(),
            self.op_ttl.to_string(),
            member.to_string(),
        ];
        self.run(&CANCEL_BEGIN, &keys, &args).await
    }

    pub async fn cancel_release(
        &self,
        student: i64,
        club: i64,
        mirror: &str,
        lease: &str,
        member: &str,
        event_id: &str,
    ) -> AppResult<i64> {
        let keys = [
            self.k_stock(club),
            self.k_reg(student),
            self.k_op(student),
            self.k_inflight(club),
            self.key(&[&"cx", &event_id]),
        ];
        let args = [
            mirror.to_string(),
            club.to_string(),
            lease.to_string(),
            member.to_string(),
        ];
        let code = self.run(&CANCEL_RELEASE, &keys, &args).await?;
        if code == 2 {
            self.request_repair(club).await;
        }
        if code < 0 {
            return Err(AppError::Internal("stock key missing".into()));
        }
        Ok(code)
    }

    pub async fn lease_end(&self, key: String, value: &str, set: Option<(String, &str)>) {
        let mut keys = vec![key];
        let mut args = vec![value.to_string()];
        if let Some((set_key, member)) = set {
            keys.push(set_key);
            args.push(member.to_string());
        }
        let _ = self.run(&LEASE_END, &keys, &args).await;
    }

    pub async fn lease_value(&self, student: i64) -> AppResult<Option<String>> {
        let mut conn = self.conn().await?;
        self.deadline(conn.get(self.k_op(student))).await
    }

    pub async fn mirror_value(&self, student: i64) -> AppResult<Option<String>> {
        let mut conn = self.conn().await?;
        self.deadline(conn.get(self.k_reg(student))).await
    }

    pub async fn mirror_set(&self, student: i64, mode: &str, value: &str) -> AppResult<i64> {
        self.run(
            &MIRROR_SET,
            &[self.k_reg(student)],
            &[mode.to_string(), value.to_string()],
        )
        .await
    }

    // ---- 单社团修复 ----
    pub async fn request_repair(&self, club: i64) {
        if let Ok(mut conn) = self.pool.get().await {
            let _: AppResult<i64> = self.deadline(conn.sadd(self.k_repair(), club)).await;
        }
    }

    pub async fn pending_repairs(&self) -> HashSet<i64> {
        let Ok(mut conn) = self.pool.get().await else {
            return HashSet::new();
        };
        self.deadline(conn.smembers::<_, HashSet<i64>>(self.k_repair()))
            .await
            .unwrap_or_default()
    }

    pub async fn repair_done(&self, club: i64) {
        if let Ok(mut conn) = self.pool.get().await {
            let _: AppResult<i64> = self.deadline(conn.srem(self.k_repair(), club)).await;
        }
    }

    /// (仍持有租约的在途成员数, 已失效的成员数)。
    pub async fn inflight_state(&self, club: i64) -> AppResult<(usize, usize)> {
        let mut conn = self.conn().await?;
        let members: Vec<String> = self.deadline(conn.smembers(self.k_inflight(club))).await?;
        if members.is_empty() {
            return Ok((0, 0));
        }
        let parsed: Vec<(i64, String)> = members
            .iter()
            .filter_map(|m| {
                let (student, lease) = m.split_once('|')?;
                Some((student.parse().ok()?, lease.to_string()))
            })
            .collect();
        let keys: Vec<String> = parsed.iter().map(|(s, _)| self.k_op(*s)).collect();
        let current: Vec<Option<String>> = if keys.is_empty() {
            Vec::new()
        } else {
            self.deadline(conn.mget(keys)).await?
        };
        let live = parsed
            .iter()
            .zip(current.iter())
            .filter(|((_, lease), value)| value.as_deref() == Some(lease.as_str()))
            .count();
        Ok((live, members.len() - live))
    }

    /// 取得社团栅栏并等该社团的在途操作完成；超时放弃。
    pub async fn fence_acquire(&self, club: i64) -> Option<String> {
        let token = crate::redis_seats::random_token(12);
        let mut conn = self.pool.get().await.ok()?;
        let set: Option<String> = self
            .deadline(
                redis::cmd("SET")
                    .arg(self.k_fence(club))
                    .arg(&token)
                    .arg("NX")
                    .arg("EX")
                    .arg(FENCE_TTL)
                    .query_async(&mut conn),
            )
            .await
            .ok()?;
        set?;
        drop(conn);
        let deadline = Instant::now() + FENCE_WAIT;
        loop {
            match self.inflight_state(club).await {
                Ok((0, _)) => return Some(token),
                Ok(_) if Instant::now() < deadline => {
                    tokio::time::sleep(Duration::from_millis(50)).await
                }
                _ => break,
            }
        }
        self.fence_release(club, &token).await;
        None
    }

    pub async fn fence_release(&self, club: i64, token: &str) {
        self.lease_end(self.k_fence(club), token, None).await;
    }

    pub async fn fence_publish(
        &self,
        club: i64,
        token: &str,
        stock: i64,
        mirrors: &BTreeMap<i64, String>,
    ) -> AppResult<bool> {
        let mut keys = vec![
            self.k_fence(club),
            self.k_stock(club),
            self.k_inflight(club),
        ];
        let mut args = vec![token.to_string(), stock.to_string()];
        for (student, value) in mirrors {
            keys.push(self.k_reg(*student));
            args.push(value.clone());
        }
        Ok(self.run(&FENCE_PUBLISH, &keys, &args).await? == 1)
    }

    pub async fn drop_club(&self, club: i64, token: &str) -> AppResult<()> {
        let mut conn = self.conn().await?;
        let owner: Option<String> = self.deadline(conn.get(self.k_fence(club))).await?;
        if owner.as_deref() == Some(token) {
            let _: i64 = self
                .deadline(conn.del(vec![
                    self.k_stock(club),
                    self.k_inflight(club),
                    self.k_fence(club),
                ]))
                .await?;
        }
        Ok(())
    }

    // ---- 全局维护(冷启动与清空类操作) ----
    pub async fn maintenance_begin(&self) -> AppResult<Option<String>> {
        let token = random_token(18);
        let mut conn = self.conn().await?;
        let set: Option<String> = self
            .deadline(
                redis::cmd("SET")
                    .arg(self.k_maint())
                    .arg(&token)
                    .arg("NX")
                    .arg("EX")
                    .arg(MAINTENANCE_TTL)
                    .query_async(&mut conn),
            )
            .await?;
        if set.is_none() {
            return Ok(None);
        }
        drop(conn);
        let deadline = Instant::now() + FENCE_WAIT;
        loop {
            match self.has_active_operations().await {
                Some(false) => return Ok(Some(token)),
                Some(true) if Instant::now() < deadline => {
                    tokio::time::sleep(Duration::from_millis(50)).await
                }
                _ => break,
            }
        }
        self.maintenance_end(&token).await;
        Ok(None)
    }

    pub async fn maintenance_end(&self, token: &str) {
        self.lease_end(self.k_maint(), token, None).await;
    }

    async fn scan(
        &self,
        conn: &mut deadpool_redis::Connection,
        pattern: &str,
        first_only: bool,
    ) -> AppResult<Vec<String>> {
        let mut cursor = 0u64;
        let mut found = Vec::new();
        loop {
            let (next, keys): (u64, Vec<String>) = self
                .deadline(
                    redis::cmd("SCAN")
                        .arg(cursor)
                        .arg("MATCH")
                        .arg(pattern)
                        .arg("COUNT")
                        .arg(1000)
                        .query_async(conn),
                )
                .await?;
            found.extend(keys);
            if (first_only && !found.is_empty()) || next == 0 {
                return Ok(found);
            }
            cursor = next;
        }
    }

    pub async fn has_active_operations(&self) -> Option<bool> {
        let mut conn = self.pool.get().await.ok()?;
        let pattern = self.key(&[&"op", &"*"]);
        self.scan(&mut conn, &pattern, true)
            .await
            .ok()
            .map(|keys| !keys.is_empty())
    }

    /// 持有全局维护锁时整体发布库存、镜像、开放时间与锁定状态。
    pub async fn publish_all(
        &self,
        token: &str,
        snapshot: &Snapshot,
        open_at: Option<i64>,
    ) -> AppResult<()> {
        let mut conn = self.conn().await?;
        let mut stale = Vec::new();
        for name in ["stock", "reg", "inflight", "fence"] {
            let pattern = self.key(&[&name, &"*"]);
            stale.extend(self.scan(&mut conn, &pattern, false).await?);
        }
        self.deadline(
            redis::cmd("WATCH")
                .arg(self.k_maint())
                .query_async::<()>(&mut conn),
        )
        .await?;
        let owner: Option<String> = self.deadline(conn.get(self.k_maint())).await?;
        if owner.as_deref() != Some(token) {
            let _: Result<(), _> = redis::cmd("UNWATCH").query_async(&mut conn).await;
            return Err(AppError::Internal("maintenance lease changed".into()));
        }
        let mut pipe = redis::pipe();
        pipe.atomic();
        if !stale.is_empty() {
            pipe.del(stale).ignore();
        }
        for club in &snapshot.clubs {
            pipe.set(
                self.k_stock(club.club_id),
                (club.max_students - club.used).max(0),
            )
            .ignore();
        }
        for (student, club, operation_id) in &snapshot.registrations {
            pipe.set(
                self.k_reg(*student),
                mirror_value(*club, operation_id.as_deref()),
            )
            .ignore();
        }
        match open_at {
            Some(epoch) => pipe.set(self.k_open_at(), epoch).ignore(),
            None => pipe.del(self.k_open_at()).ignore(),
        };
        if snapshot.locked {
            pipe.set(self.k_locked(), "1").ignore();
        } else {
            pipe.del(self.k_locked()).ignore();
        }
        pipe.del(self.k_repair()).ignore();
        pipe.set(self.k_init(), "1").ignore();
        let committed: Option<()> = self.deadline(pipe.query_async(&mut conn)).await?;
        committed
            .ok_or_else(|| AppError::Internal("maintenance lease changed during publish".into()))
    }

    pub async fn stock_left(&self, clubs: &[i64]) -> Option<Vec<Option<i64>>> {
        if clubs.is_empty() {
            return Some(Vec::new());
        }
        let mut conn = self.pool.get().await.ok()?;
        let keys: Vec<String> = clubs.iter().map(|c| self.k_stock(*c)).collect();
        let values: Vec<Option<String>> = self.deadline(conn.mget(keys)).await.ok()?;
        Some(
            values
                .into_iter()
                .map(|v| v.and_then(|s| s.parse().ok()))
                .collect(),
        )
    }

    pub async fn now_epoch_ms(&self) -> i64 {
        if let Ok(mut conn) = self.pool.get().await
            && let Ok((secs, usecs)) = self
                .deadline(redis::cmd("TIME").query_async::<(i64, i64)>(&mut conn))
                .await
        {
            return secs.saturating_mul(1000) + usecs / 1000;
        }
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0)
    }

    pub async fn open_at_get(&self) -> Option<i64> {
        let mut conn = self.pool.get().await.ok()?;
        let v: Option<String> = self.deadline(conn.get(self.k_open_at())).await.ok()?;
        v.and_then(|s| s.parse().ok())
    }

    pub async fn open_at_set(&self, epoch: Option<i64>) -> AppResult<()> {
        let mut conn = self.conn().await?;
        match epoch {
            Some(epoch) => {
                self.deadline(conn.set::<_, _, ()>(self.k_open_at(), epoch))
                    .await
            }
            None => self.deadline(conn.del::<_, ()>(self.k_open_at())).await,
        }
    }

    pub async fn locked_get(&self) -> Option<bool> {
        let mut conn = self.pool.get().await.ok()?;
        let value: Option<String> = self.deadline(conn.get(self.k_locked())).await.ok()?;
        Some(value.as_deref() == Some("1"))
    }

    pub async fn locked_set(&self, locked: bool) -> AppResult<()> {
        let mut conn = self.conn().await?;
        if locked {
            self.deadline(conn.set::<_, _, ()>(self.k_locked(), "1"))
                .await
        } else {
            self.deadline(conn.del::<_, ()>(self.k_locked())).await
        }
    }

    // ---- 会话 ----
    pub async fn session_role_epoch(&self, role: &str) -> AppResult<i64> {
        let mut conn = self.conn().await?;
        parse_counter(self.deadline(conn.get(self.k_epoch(role))).await?)
    }

    pub async fn session_principal_version(&self, role: &str, principal: &str) -> AppResult<i64> {
        let mut conn = self.conn().await?;
        parse_counter(
            self.deadline(conn.get(self.k_version(role, principal)))
                .await?,
        )
    }

    /// 只有账号代际仍等于登录读取时的值才签发会话；成功后该账号的旧会话全部失效。
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
        let token = random_token(32);
        let mut conn = self.conn().await?;
        let mut invocation = SESSION_CREATE.prepare_invoke();
        invocation
            .key(self.k_epoch(&role))
            .key(self.k_version(&role, &principal))
            .key(self.k_sess(&token))
            .key(self.k_role_mutation(&role))
            .key(self.k_principal_mutation(&role, &principal))
            .arg(payload.to_string())
            .arg(ttl)
            .arg(expected_epoch)
            .arg(expected_version);
        let code = self
            .deadline(invocation.invoke_async::<i64>(&mut conn))
            .await?;
        match code {
            1 => Ok(Some(token)),
            0 | -2 => Ok(None),
            _ => Err(AppError::Internal("invalid session generation".into())),
        }
    }

    pub async fn session_get(&self, token: &str) -> AppResult<Option<serde_json::Value>> {
        if token.is_empty() {
            return Ok(None);
        }
        let mut conn = self.conn().await?;
        let raw: Option<String> = self.deadline(conn.get(self.k_sess(token))).await?;
        let Some(payload) =
            raw.and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok())
        else {
            return Ok(None);
        };
        let Some((role, principal)) = session_identity(&payload) else {
            return Ok(None);
        };
        let epoch = payload.get("_session_epoch").and_then(|v| v.as_i64());
        let version = payload.get("_session_version").and_then(|v| v.as_i64());
        let (Some(epoch), Some(version)) = (epoch, version) else {
            return Ok(None);
        };
        let values: Vec<Option<String>> = self
            .deadline(conn.mget(vec![self.k_epoch(&role), self.k_version(&role, &principal)]))
            .await?;
        let current_epoch = parse_counter(values.first().cloned().flatten())?;
        let current_version = parse_counter(values.get(1).cloned().flatten())?;
        Ok((epoch == current_epoch && version == current_version).then_some(payload))
    }

    // ---- 登录限流与受信设备 ----
    pub async fn login_blocked(&self, key: &str, limit: i64) -> bool {
        let Ok(mut conn) = self.pool.get().await else {
            return false;
        };
        let n: Option<String> = self
            .deadline(conn.get(self.k_fail(key)))
            .await
            .unwrap_or(None);
        n.and_then(|s| s.parse::<i64>().ok()).unwrap_or(0) >= limit
    }

    pub async fn login_fail(&self, key: &str) {
        let _ = self
            .run(
                &LOGIN_FAIL,
                &[self.k_fail(key)],
                &[LOGIN_WINDOW.to_string()],
            )
            .await;
    }

    pub async fn login_ok(&self, keys: &[&str]) {
        if let Ok(mut conn) = self.pool.get().await {
            let keys: Vec<String> = keys.iter().map(|k| self.k_fail(k)).collect();
            let _: AppResult<i64> = self.deadline(conn.del(keys)).await;
        }
    }

    pub async fn device_trusted(&self, role: &str, username: &str, device: Option<&str>) -> bool {
        let Some(device) = device else {
            return false;
        };
        let Ok(mut conn) = self.pool.get().await else {
            return false;
        };
        self.deadline(conn.exists(self.k_trust(role, username, device)))
            .await
            .unwrap_or(false)
    }

    pub async fn trust_device(&self, role: &str, username: &str, device: &str) {
        if let Ok(mut conn) = self.pool.get().await {
            let _: AppResult<()> = self
                .deadline(conn.set_ex(self.k_trust(role, username, device), "1", DEVICE_TTL as u64))
                .await;
        }
    }
}

fn parse_counter(value: Option<String>) -> AppResult<i64> {
    match value {
        None => Ok(0),
        Some(value) => value
            .parse::<i64>()
            .ok()
            .filter(|v| *v >= 0)
            .ok_or_else(|| AppError::Internal("invalid session generation".into())),
    }
}

fn session_identity(payload: &serde_json::Value) -> Option<(String, String)> {
    match payload.get("role").and_then(|value| value.as_str()) {
        Some("student") => payload
            .get("student_id")
            .and_then(|value| value.as_i64())
            .filter(|id| *id > 0)
            .map(|id| ("student".to_string(), id.to_string())),
        Some("admin") => payload
            .get("username")
            .and_then(|value| value.as_str())
            .filter(|name| !name.is_empty() && name.len() <= 80)
            .map(|name| ("admin".to_string(), name.to_string())),
        _ => None,
    }
}

/// URL 安全的 base64 随机串，与 Python `secrets.token_urlsafe(n)` 同字母表。
pub fn random_token(bytes: usize) -> String {
    use rand::RngExt;
    let mut raw = vec![0u8; bytes];
    rand::rng().fill(&mut raw[..]);
    url_safe_b64(&raw)
}

fn url_safe_b64(input: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let n = (u32::from(chunk[0]) << 16)
            | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8)
            | u32::from(*chunk.get(2).unwrap_or(&0));
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

/// 报名镜像值：`club_id|operation_id`，历史数据没有 operation_id 时只存 club_id。
pub fn mirror_value(club: i64, operation_id: Option<&str>) -> String {
    match operation_id {
        Some(op) if !op.is_empty() => format!("{club}|{op}"),
        _ => club.to_string(),
    }
}

// ---- 修复 ------------------------------------------------------------------

/// 持有社团栅栏时以 SQLite 为准发布该社团的库存与镜像。
pub async fn publish_club_with_fence(
    seats: &Seats,
    db: &Db,
    club: i64,
    token: &str,
) -> AppResult<bool> {
    let Some((maximum, registrations)) = db.club_seat_truth(club).await? else {
        seats.drop_club(club, token).await?;
        seats.repair_done(club).await;
        return Ok(true);
    };
    let used = registrations.len() as i64;
    let mirrors: BTreeMap<i64, String> = registrations
        .iter()
        .map(|(student, op)| (*student, mirror_value(club, op.as_deref())))
        .collect();
    if !seats
        .fence_publish(club, token, (maximum - used).max(0), &mirrors)
        .await?
    {
        return Ok(false);
    }
    db.mark_club_synced(club, used).await?;
    seats.repair_done(club).await;
    Ok(true)
}

/// 单社团修复：只暂停这一个社团的抢位。
pub async fn repair_club(seats: &Seats, db: &Db, club: i64) -> bool {
    let Some(token) = seats.fence_acquire(club).await else {
        return false;
    };
    let result = publish_club_with_fence(seats, db, club, &token).await;
    seats.fence_release(club, &token).await;
    match result {
        Ok(done) => done,
        Err(e) => {
            tracing::warn!(club, error = %e, "club seat repair failed");
            false
        }
    }
}

/// 以 SQLite 为准整体发布：首次启动或 Redis 重启后使用。
pub async fn rebuild_all(seats: &Seats, db: &Db) -> AppResult<bool> {
    if !seats.alive().await {
        return Err(AppError::RedisDown);
    }
    let Some(token) = seats.maintenance_begin().await? else {
        return Ok(false);
    };
    let result = async {
        let snapshot = db.snapshot().await?;
        let open_at = snapshot
            .start_time
            .as_deref()
            .and_then(crate::auth::parse_local_datetime);
        seats.publish_all(&token, &snapshot, open_at).await?;
        db.mark_all_synced().await?;
        tracing::info!(clubs = snapshot.clubs.len(), "Redis seats published");
        Ok::<_, AppError>(())
    }
    .await;
    seats.maintenance_end(&token).await;
    result.map(|_| true)
}

#[cfg(test)]
mod tests {
    #[test]
    fn mirror_values_match_python() {
        assert_eq!("3|7-abc", super::mirror_value(3, Some("7-abc")));
        assert_eq!("3", super::mirror_value(3, None));
    }

    #[test]
    fn tokens_use_the_url_safe_alphabet() {
        let token = super::random_token(32);
        assert_eq!(43, token.len());
        assert!(
            token
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        );
    }
}
