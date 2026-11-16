//! 热路径 HTTP 接口：登录、报名、退选、社团目录、名额快照、开放时间、学生信息与探针。
//! JSON 结构、Redis 键与 Lua 脚本与 main.py 一致，两个服务可以在反向代理后互换。

use axum::Json;
use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use std::sync::atomic::Ordering;

use crate::auth;
use crate::cache::{self, CachedJson};
use crate::db::{DeleteOutcome, RegistrationInsertOutcome, restriction_code};
use crate::messages::{self, fail};
use crate::redis_seats::{Acquire, Seats, mirror_value, random_token};
use crate::state::AppState;
use crate::types::{ClubId, StudentId};

fn seats(state: &AppState) -> Seats {
    Seats::new(state.redis.clone(), &state.cfg)
}

fn conditional(cached: CachedJson, headers: &HeaderMap) -> Response {
    let matches = headers
        .get(header::IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.split(',').any(|tag| tag.trim() == cached.etag));
    let status = if matches {
        StatusCode::NOT_MODIFIED
    } else {
        StatusCode::OK
    };
    let mut builder = Response::builder()
        .status(status)
        .header(header::ETAG, &cached.etag)
        .header(header::CACHE_CONTROL, "no-cache");
    if !matches {
        builder = builder.header(header::CONTENT_TYPE, "application/json");
    }
    let body = if matches {
        Body::empty()
    } else {
        Body::from(cached.body)
    };
    builder
        .body(body)
        .unwrap_or_else(|_| fail(StatusCode::INTERNAL_SERVER_ERROR, "server_error"))
}

// ===========================================================================
// 登录
// ===========================================================================

pub const AUTH_WAIT: std::time::Duration = std::time::Duration::from_secs(10);

/// 口令队列排不进去：告诉浏览器约定好的等待秒数。
fn login_queued(wait: u64) -> Response {
    let mut response = fail(StatusCode::SERVICE_UNAVAILABLE, "login_queued");
    if let Ok(value) = HeaderValue::from_str(&wait.to_string()) {
        response.headers_mut().insert(header::RETRY_AFTER, value);
    }
    response
}

#[derive(Debug, Deserialize)]
pub struct LoginReq {
    #[serde(default)]
    username: String,
    #[serde(default)]
    password: String,
}

pub async fn login(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Result<Json<LoginReq>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let Ok(Json(req)) = body else {
        return fail(StatusCode::BAD_REQUEST, "bad_request");
    };
    let username = req.username.trim().to_string();
    let password = req.password;
    if username.is_empty() || password.is_empty() {
        return fail(StatusCode::BAD_REQUEST, "login_empty");
    }
    if username.len() > 80 || password.len() > 256 {
        return fail(StatusCode::BAD_REQUEST, "bad_request");
    }
    if let Some(wait) = state.auth.saturated() {
        return login_queued(wait);
    }
    let redis = seats(&state);
    // 受信设备按"账号+设备"计数，其他设备按账号计数。
    let device = auth::device_token_from_headers(&headers);
    let trusted = redis
        .device_trusted("student", &username, device.as_deref())
        .await;
    let account_key = format!("student:u:{username}");
    let throttle_key = match (&device, trusted) {
        (Some(device), true) => format!("student:d:{username}:{device}"),
        _ => account_key.clone(),
    };
    if redis
        .login_blocked(&throttle_key, state.cfg.login_max_fails)
        .await
    {
        let mut response = fail(StatusCode::TOO_MANY_REQUESTS, "login_throttled");
        response
            .headers_mut()
            .insert(header::RETRY_AFTER, HeaderValue::from_static("60"));
        return response;
    }
    let role_epoch = match redis.session_role_epoch("student").await {
        Ok(epoch) => epoch,
        Err(e) => return e.into_response(),
    };
    let row = match state.db.find_student_by_username(username.clone()).await {
        Ok(Some(row)) => row,
        Ok(None) => {
            redis.login_fail(&throttle_key).await;
            return fail(StatusCode::UNAUTHORIZED, "login_failed");
        }
        Err(e) => return e.into_response(),
    };
    let principal_version = match redis
        .session_principal_version("student", &row.id.to_string())
        .await
    {
        Ok(version) => version,
        Err(e) => return e.into_response(),
    };
    let stored = row.password.clone();
    let plain = password.clone();
    let verified = state
        .auth
        .run(AUTH_WAIT, move || {
            let v = auth::verify_password(&stored, &plain);
            let upgraded = (v.ok && v.needs_upgrade)
                .then(|| auth::hash_password(&plain).ok())
                .flatten();
            (v, upgraded)
        })
        .await;
    let (verify, upgraded_hash) = match verified {
        Ok(result) => result,
        Err(wait) => return login_queued(wait),
    };
    if !verify.ok {
        redis.login_fail(&throttle_key).await;
        return fail(StatusCode::UNAUTHORIZED, "login_failed");
    }
    // 账号必须仍是刚才验证的那一条。
    match state.db.find_student_by_username(username.clone()).await {
        Ok(Some(current)) if current.id == row.id && current.password == row.password => {}
        Ok(_) => return fail(StatusCode::UNAUTHORIZED, "login_failed"),
        Err(e) => return e.into_response(),
    }
    let payload = json!({
        "role": "student",
        "student_id": row.id,
        "name": row.name,
        "class": row.class,
        "grade": row.grade,
        "student_no": row.student_no,
    });
    let token = match redis
        .session_create(
            &payload,
            state.cfg.session_ttl,
            role_epoch,
            principal_version,
        )
        .await
    {
        Ok(Some(token)) => token,
        Ok(None) => return fail(StatusCode::SERVICE_UNAVAILABLE, "account_changed"),
        Err(e) => return e.into_response(),
    };
    if let Some(new_hash) = upgraded_hash
        && let Err(e) = state
            .db
            .update_password_if_matches(StudentId(row.id), new_hash, row.password.clone())
            .await
    {
        tracing::warn!(error = %e, "password upgrade write failed");
    }
    redis.login_ok(&[&throttle_key, &account_key]).await;
    let device = device.unwrap_or_else(|| random_token(24));
    redis.trust_device("student", &username, &device).await;

    let secure = state.cfg.cookie_secure;
    let mut response = Json(json!({
        "success": true,
        "student_id": row.id,
        "name": row.name,
        "class": row.class,
        "student_no": row.student_no,
    }))
    .into_response();
    let headers = response.headers_mut();
    for cookie in [
        auth::cookie("device", &device, 180 * 24 * 3600, secure),
        auth::cookie("session", &token, state.cfg.session_ttl, secure),
    ] {
        if let Ok(value) = HeaderValue::from_str(&cookie) {
            headers.append(header::SET_COOKIE, value);
        }
    }
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

// ===========================================================================
// 报名
// ===========================================================================

#[derive(Debug, Deserialize)]
pub struct RegisterReq {
    club_id: Option<i64>,
    request_id: Option<String>,
}

fn valid_request_id(value: &str) -> bool {
    (8..=64).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// 报名结果在后台任务里确定，HTTP 只等待有限时间。
enum Outcome {
    Reply(StatusCode, &'static str, bool),
}

impl Outcome {
    fn ok(code: &'static str) -> Self {
        Outcome::Reply(StatusCode::OK, code, true)
    }
    fn fail(status: StatusCode, code: &'static str) -> Self {
        Outcome::Reply(status, code, false)
    }
    fn into_response(self, request_id: &str) -> Response {
        let Outcome::Reply(status, code, success) = self;
        let mut extra = Map::new();
        extra.insert("request_id".into(), Value::String(request_id.to_string()));
        let mut response =
            (status, Json(messages::body(code, success, Some(extra)))).into_response();
        if matches!(status.as_u16(), 202 | 503) {
            response
                .headers_mut()
                .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
        }
        response
    }
}

struct Attempt {
    student: i64,
    club: i64,
    operation_id: String,
    mirror: String,
    lease: String,
    member: String,
}

pub async fn register_club(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Result<Json<RegisterReq>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let sess = match auth::student_session(&state, &headers).await {
        Ok(Some(sess)) => sess,
        Ok(None) => return fail(StatusCode::UNAUTHORIZED, "unauthorized"),
        Err(e) => return e.into_response(),
    };
    let Ok(Json(req)) = body else {
        return fail(StatusCode::BAD_REQUEST, "bad_request");
    };
    let Some(club) = req.club_id.filter(|id| *id > 0) else {
        return fail(StatusCode::BAD_REQUEST, "bad_request");
    };
    if req
        .request_id
        .as_deref()
        .is_some_and(|id| !valid_request_id(id))
    {
        return fail(StatusCode::BAD_REQUEST, "bad_request");
    }
    if state.reconcile_requested.load(Ordering::Acquire) {
        return fail(StatusCode::SERVICE_UNAVAILABLE, "unavailable");
    }
    // 准入只读内存目录和会话里的年级班级，不在热路径上查库。
    let catalog = match cache::catalog(&state).await {
        Ok(catalog) => catalog,
        Err(e) => return e.into_response(),
    };
    let Some(row) = catalog.club(club) else {
        return fail(StatusCode::OK, "club_missing");
    };
    if !row.enabled {
        return fail(StatusCode::OK, "club_disabled");
    }
    if row.seat_sync_pending {
        return fail(StatusCode::SERVICE_UNAVAILABLE, "syncing");
    }
    let (grade, class) = match (sess.grade.clone(), sess.class.clone()) {
        (Some(grade), Some(class)) => (grade, class),
        _ => match state
            .db
            .student_grade_class(StudentId(sess.student_id))
            .await
        {
            Ok(Some(pair)) => pair,
            Ok(None) => return fail(StatusCode::UNAUTHORIZED, "unauthorized"),
            Err(e) => return e.into_response(),
        },
    };
    if let Some(code) = restriction_code(&grade, &class, &row.allowed_grades, &row.allowed_classes)
    {
        return fail(StatusCode::OK, code);
    }

    let request_id = req.request_id.unwrap_or_else(|| random_token(12));
    let operation_id = format!("{}-{}", sess.student_id, request_id);
    let mirror = mirror_value(club, Some(&operation_id));
    let lease = format!("{mirror}|{}", random_token(6));
    let attempt = Attempt {
        student: sess.student_id,
        club,
        member: format!("{}|{}", sess.student_id, lease),
        operation_id,
        mirror,
        lease,
    };
    // 抢位、落库与确认在独立任务里完成；超时或断线不会中断它。
    let task = tokio::spawn(finalize_registration(state.clone(), attempt));
    match tokio::time::timeout(state.cfg.register_wait, task).await {
        Ok(Ok(outcome)) => outcome.into_response(&request_id),
        Ok(Err(_)) => fail(StatusCode::INTERNAL_SERVER_ERROR, "server_error"),
        Err(_) => Outcome::fail(StatusCode::ACCEPTED, "pending").into_response(&request_id),
    }
}

async fn finalize_registration(state: AppState, attempt: Attempt) -> Outcome {
    let redis = seats(&state);
    let (student, club) = (attempt.student, attempt.club);
    let mut mirror_repaired = false;
    let acquired = loop {
        let result = redis
            .acquire(
                student,
                club,
                &attempt.lease,
                &attempt.member,
                &format!("{}|", attempt.mirror),
            )
            .await;
        let acquired = match result {
            Ok(result) => result,
            Err(_) => {
                redis.request_repair(club).await;
                return Outcome::fail(StatusCode::SERVICE_UNAVAILABLE, "unavailable");
            }
        };
        if acquired == Acquire::Existing {
            match resolve_existing(&state, &redis, &attempt, !mirror_repaired).await {
                Resolution::Done(outcome) => return outcome,
                Resolution::Retry => {
                    mirror_repaired = true;
                    continue;
                }
            }
        }
        break acquired;
    };
    match acquired {
        Acquire::Acquired => {}
        Acquire::OtherAttempt => {
            return match state
                .db
                .registration_by_operation(attempt.operation_id.clone())
                .await
            {
                Ok(Some(_)) => Outcome::ok("registered"),
                _ => Outcome::fail(StatusCode::ACCEPTED, "pending"),
            };
        }
        Acquire::Full => return Outcome::fail(StatusCode::OK, "full"),
        Acquire::NotOpen => return Outcome::fail(StatusCode::OK, "not_open"),
        Acquire::Locked => return Outcome::fail(StatusCode::OK, "locked"),
        Acquire::Fenced => return Outcome::fail(StatusCode::SERVICE_UNAVAILABLE, "syncing"),
        Acquire::Uninitialized => {
            if redis.initialized().await {
                redis.request_repair(club).await;
            } else {
                state.reconcile_requested.store(true, Ordering::Release);
            }
            return Outcome::fail(StatusCode::SERVICE_UNAVAILABLE, "syncing");
        }
        Acquire::Existing | Acquire::Invalid => {
            return Outcome::fail(StatusCode::SERVICE_UNAVAILABLE, "unavailable");
        }
    }

    // 只有抢到名额的请求进入单写者队列。
    let _permit = state.registration_gate.clone().acquire_owned().await;
    let inserted = state
        .db
        .insert_registration(
            StudentId(student),
            ClubId(club),
            auth::now_local_string(),
            attempt.operation_id.clone(),
        )
        .await;
    match inserted {
        Ok(RegistrationInsertOutcome::Inserted { .. }) => {
            confirm(&redis, &attempt).await;
            Outcome::ok("registered")
        }
        Ok(RegistrationInsertOutcome::ClubFull) => {
            // 数据库已满而 Redis 放行：不归还这一个名额，用来抵消多出的库存。
            rollback(&redis, &attempt, true).await;
            redis.request_repair(club).await;
            Outcome::fail(StatusCode::OK, "full")
        }
        Ok(RegistrationInsertOutcome::AlreadyRegistered) => {
            rollback(&redis, &attempt, false).await;
            existing_registration(&state, &redis, &attempt).await
        }
        Ok(RegistrationInsertOutcome::ClubMissing) => {
            rollback(&redis, &attempt, false).await;
            Outcome::fail(StatusCode::OK, "club_missing")
        }
        Ok(RegistrationInsertOutcome::ClubDisabled) => {
            rollback(&redis, &attempt, false).await;
            Outcome::fail(StatusCode::OK, "club_disabled")
        }
        Ok(RegistrationInsertOutcome::SeatSyncPending) => {
            rollback(&redis, &attempt, false).await;
            Outcome::fail(StatusCode::SERVICE_UNAVAILABLE, "syncing")
        }
        Ok(RegistrationInsertOutcome::RegistrationLocked) => {
            rollback(&redis, &attempt, false).await;
            Outcome::fail(StatusCode::OK, "locked")
        }
        Ok(RegistrationInsertOutcome::Ineligible(code)) => {
            rollback(&redis, &attempt, false).await;
            Outcome::fail(StatusCode::OK, code)
        }
        Err(e) => {
            tracing::error!(student, club, error = %e, "registration persist failed");
            // 结果不明时按操作代际查库：已提交就确认，确定未提交才撤销。
            match state
                .db
                .registration_by_operation(attempt.operation_id.clone())
                .await
            {
                Ok(Some(_)) => {
                    confirm(&redis, &attempt).await;
                    Outcome::ok("registered")
                }
                Ok(None) => {
                    rollback(&redis, &attempt, false).await;
                    Outcome::fail(StatusCode::SERVICE_UNAVAILABLE, "unavailable")
                }
                Err(_) => {
                    redis.request_repair(club).await;
                    Outcome::fail(StatusCode::ACCEPTED, "pending")
                }
            }
        }
    }
}

async fn confirm(redis: &Seats, attempt: &Attempt) {
    let confirmed = redis
        .confirm(
            attempt.student,
            attempt.club,
            &attempt.lease,
            &attempt.mirror,
            &attempt.member,
        )
        .await
        .unwrap_or(false);
    if !confirmed {
        // 租约已失效：补写镜像，库存交给单社团修复。
        let _ = redis
            .mirror_set(attempt.student, "absent", &attempt.mirror)
            .await;
        redis.request_repair(attempt.club).await;
    }
}

async fn rollback(redis: &Seats, attempt: &Attempt, absorb: bool) {
    if let Err(e) = redis
        .rollback(
            attempt.student,
            attempt.club,
            &attempt.lease,
            &attempt.member,
            absorb,
        )
        .await
    {
        tracing::error!(student = attempt.student, club = attempt.club, error = %e, "reservation rollback failed");
        redis.request_repair(attempt.club).await;
    }
}

async fn existing_registration(state: &AppState, redis: &Seats, attempt: &Attempt) -> Outcome {
    match state
        .db
        .registration_for_student(StudentId(attempt.student))
        .await
    {
        Ok(Some(existing)) => {
            let value = mirror_value(existing.club_id, existing.operation_id.as_deref());
            let _ = redis.mirror_set(attempt.student, "absent", &value).await;
            if existing.club_id != attempt.club {
                Outcome::fail(StatusCode::OK, "registered_other")
            } else if existing.operation_id.as_deref() == Some(attempt.operation_id.as_str()) {
                Outcome::ok("registered")
            } else {
                Outcome::ok("registered_already")
            }
        }
        Ok(None) => Outcome::fail(StatusCode::SERVICE_UNAVAILABLE, "unavailable"),
        Err(_) => Outcome::fail(StatusCode::SERVICE_UNAVAILABLE, "unavailable"),
    }
}

enum Resolution {
    Done(Outcome),
    Retry,
}

/// 抢位返回"已报名或有其他操作"时，以数据库为准给出结果。
async fn resolve_existing(
    state: &AppState,
    redis: &Seats,
    attempt: &Attempt,
    repair_mirror: bool,
) -> Resolution {
    match state
        .db
        .registration_for_student(StudentId(attempt.student))
        .await
    {
        Ok(Some(_)) => return Resolution::Done(existing_registration(state, redis, attempt).await),
        Ok(None) => {}
        Err(_) => {
            return Resolution::Done(Outcome::fail(
                StatusCode::SERVICE_UNAVAILABLE,
                "unavailable",
            ));
        }
    }
    match redis.lease_value(attempt.student).await {
        Ok(Some(_)) => return Resolution::Done(Outcome::fail(StatusCode::ACCEPTED, "pending")),
        Ok(None) => {}
        Err(_) => {
            return Resolution::Done(Outcome::fail(
                StatusCode::SERVICE_UNAVAILABLE,
                "unavailable",
            ));
        }
    }
    match redis.mirror_value(attempt.student).await.ok().flatten() {
        Some(stale) if repair_mirror => {
            // 数据库没有报名记录而镜像还在：清掉这条旧镜像后重试一次。
            let _ = redis.mirror_set(attempt.student, "clear", &stale).await;
            Resolution::Retry
        }
        _ => Resolution::Done(Outcome::fail(StatusCode::ACCEPTED, "pending")),
    }
}

// ===========================================================================
// 退选
// ===========================================================================

pub async fn cancel_registration(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let sess = match auth::student_session(&state, &headers).await {
        Ok(Some(sess)) => sess,
        Ok(None) => return fail(StatusCode::UNAUTHORIZED, "unauthorized"),
        Err(e) => return e.into_response(),
    };
    if state.reconcile_requested.load(Ordering::Acquire) {
        return fail(StatusCode::SERVICE_UNAVAILABLE, "unavailable");
    }
    let student = sess.student_id;
    let registration = match state.db.registration_for_student(StudentId(student)).await {
        Ok(Some(registration)) => registration,
        Ok(None) => return fail(StatusCode::OK, "not_registered"),
        Err(e) => return e.into_response(),
    };
    if registration.seat_sync_pending {
        return fail(StatusCode::SERVICE_UNAVAILABLE, "syncing");
    }
    let redis = seats(&state);
    let club = registration.club_id;
    let lease = format!("{club}|x-{}", random_token(9));
    let member = format!("{student}|{lease}");
    match redis.cancel_begin(student, club, &lease, &member).await {
        Ok(1) => {}
        Ok(-6) => return fail(StatusCode::OK, "locked"),
        Ok(-3) => return fail(StatusCode::SERVICE_UNAVAILABLE, "syncing"),
        Ok(_) => return fail(StatusCode::ACCEPTED, "pending"),
        Err(e) => return e.into_response(),
    }
    let task_state = state.clone();
    let task = tokio::spawn(async move {
        let _permit = task_state.registration_gate.clone().acquire_owned().await;
        let outcome = task_state
            .db
            .delete_registration(
                StudentId(student),
                registration.registration_id,
                club,
                registration.operation_id.clone(),
            )
            .await;
        let lease_set = Some((redis.k_inflight(club), member.as_str()));
        match outcome {
            Ok(DeleteOutcome::Deleted) => {
                let event_id = registration.operation_id.clone().unwrap_or_else(|| {
                    format!("legacy-{}-{student}-{club}", registration.registration_id)
                });
                let mirror = mirror_value(club, registration.operation_id.as_deref());
                if let Err(e) = redis
                    .cancel_release(student, club, &mirror, &lease, &member, &event_id)
                    .await
                {
                    tracing::error!(student, club, error = %e, "cancel release failed");
                    redis.request_repair(club).await;
                    redis
                        .lease_end(redis.k_op(student), &lease, lease_set)
                        .await;
                }
                (StatusCode::OK, "cancelled", true)
            }
            other => {
                redis
                    .lease_end(redis.k_op(student), &lease, lease_set)
                    .await;
                match other {
                    Ok(DeleteOutcome::NotRegistered) => (StatusCode::OK, "not_registered", false),
                    Ok(DeleteOutcome::Locked) => (StatusCode::OK, "locked", false),
                    Ok(DeleteOutcome::Syncing) => {
                        (StatusCode::SERVICE_UNAVAILABLE, "syncing", false)
                    }
                    _ => (StatusCode::SERVICE_UNAVAILABLE, "unavailable", false),
                }
            }
        }
    });
    match tokio::time::timeout(state.cfg.register_wait, task).await {
        Ok(Ok((status, code, success))) => messages::reply(status, code, success),
        Ok(Err(_)) => fail(StatusCode::INTERNAL_SERVER_ERROR, "server_error"),
        Err(_) => fail(StatusCode::ACCEPTED, "pending"),
    }
}

// ===========================================================================
// 目录、名额与开放时间
// ===========================================================================

pub async fn catalog(State(state): State<AppState>, headers: HeaderMap) -> Response {
    match cache::catalog(&state).await {
        Ok(catalog) => conditional(catalog.json.clone(), &headers),
        Err(e) => e.into_response(),
    }
}

pub async fn seats_snapshot(State(state): State<AppState>, headers: HeaderMap) -> Response {
    match cache::seats(&state).await {
        Ok(cached) => conditional(cached, &headers),
        Err(e) => e.into_response(),
    }
}

pub async fn get_clubs(State(state): State<AppState>) -> Response {
    let rows = match state.db.list_clubs().await {
        Ok(rows) => rows,
        Err(e) => return e.into_response(),
    };
    let ids: Vec<i64> = rows.iter().map(|r| r.id).collect();
    let live = seats(&state).stock_left(&ids).await;
    let data: Vec<Value> = rows
        .iter()
        .enumerate()
        .map(|(i, r)| {
            let used = live
                .as_ref()
                .and_then(|values| values.get(i).copied().flatten())
                .map(|left| r.max_students - left)
                .unwrap_or(r.current_students);
            let list = |value: &str| {
                serde_json::from_str::<Value>(value).unwrap_or_else(|_| Value::Array(Vec::new()))
            };
            json!({
                "id": r.id,
                "name": r.name,
                "max_students": r.max_students,
                "current_students": used.clamp(0, r.max_students),
                "description": r.description,
                "advisor_name": r.advisor_name,
                "meeting_time": r.meeting_time,
                "location": r.location,
                "image_path": r.image_path,
                "allowed_grades": list(&r.allowed_grades),
                "allowed_classes": list(&r.allowed_classes),
                "enabled": r.enabled,
                "revision": r.revision,
                "seat_sync_pending": r.seat_sync_pending,
            })
        })
        .collect();
    Json(Value::Array(data)).into_response()
}

pub async fn check_registration_time(State(state): State<AppState>) -> Response {
    let redis = seats(&state);
    let mut open_at = redis.open_at_get().await;
    let mut start_str = open_at.and_then(auth::format_local_datetime);
    if open_at.is_none()
        && let Ok(Some(text)) = state.db.registration_start_time().await
    {
        open_at = auth::parse_local_datetime(&text);
        start_str = Some(text);
    }
    let server_now = redis.now_epoch_ms().await;
    let locked = state.db.registration_locked().await.unwrap_or(false)
        || redis.locked_get().await.unwrap_or(false);
    let can = open_at.is_some_and(|o| server_now >= o.saturating_mul(1000)) && !locked;
    (
        [(header::CACHE_CONTROL, "no-store")],
        Json(json!({
            "can_register": can,
            "start_time": start_str,
            "open_at": open_at.map(|value| value.saturating_mul(1000)),
            "server_now": server_now,
            "registration_locked": locked,
            "end_time": Value::Null,
        })),
    )
        .into_response()
}

pub async fn get_student_info(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let sess = match auth::student_session(&state, &headers).await {
        Ok(Some(sess)) => sess,
        Ok(None) => return fail(StatusCode::UNAUTHORIZED, "unauthorized"),
        Err(e) => return e.into_response(),
    };
    let info = match state.db.student_info(StudentId(sess.student_id)).await {
        Ok(Some(info)) => info,
        Ok(None) => return fail(StatusCode::NOT_FOUND, "unauthorized"),
        Err(e) => return e.into_response(),
    };
    (
        [(header::CACHE_CONTROL, "no-store")],
        Json(json!({
            "name": info.name,
            "class": info.class,
            "grade": info.grade,
            "student_id": info.student_no,
            "username": info.username,
            "registered_club": info.registered_club,
            "registered_club_id": info.registered_club_id,
            "registration_time": info.registration_time,
        })),
    )
        .into_response()
}

// ===========================================================================
// 探针
// ===========================================================================

pub async fn healthz(State(state): State<AppState>) -> Response {
    let db_ok = state.db.ping().await.is_ok();
    let redis_ok = seats(&state).alive().await;
    let status = if db_ok && redis_ok {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    let label = if status == StatusCode::OK {
        "ok"
    } else {
        "degraded"
    };
    (
        status,
        Json(json!({ "status": label, "db": db_ok, "redis": redis_ok })),
    )
        .into_response()
}

pub async fn readyz(State(state): State<AppState>) -> Response {
    let not_ready = |reason: &str| {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({ "status": "not-ready", "reason": reason })),
        )
            .into_response()
    };
    let redis = seats(&state);
    if !redis.alive().await {
        return not_ready("redis");
    }
    if state.reconcile_requested.load(Ordering::Acquire) || !redis.initialized().await {
        state.reconcile_requested.store(true, Ordering::Release);
        return not_ready("initializing");
    }
    if redis.maintenance_active().await != Some(false) {
        return not_ready("maintenance");
    }
    let Ok(clubs) = state.db.club_stock_snapshot().await else {
        return not_ready("database");
    };
    let ids: Vec<i64> = clubs.iter().map(|c| c.club_id).collect();
    let Some(live) = redis.stock_left(&ids).await else {
        return not_ready("redis");
    };
    let mut drifted = 0usize;
    for (club, left) in clubs.iter().zip(live.iter()) {
        if matches!(redis.inflight_state(club.club_id).await, Ok((active, _)) if active > 0) {
            continue;
        }
        let expected = (club.max_students - club.used).max(0);
        if club.used > club.max_students || *left != Some(expected) {
            redis.request_repair(club.club_id).await;
            drifted += 1;
        }
    }
    if drifted > 0 {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({ "status": "not-ready", "reason": "repairing", "clubs": drifted })),
        )
            .into_response();
    }
    Json(json!({ "status": "ready" })).into_response()
}
