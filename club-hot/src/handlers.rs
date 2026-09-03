//! HTTP handlers — Rust ports of the six hot-path endpoints from `main.py`,
//! plus `/healthz` and `/readyz`.
//!
//! Behaviour, JSON shapes, Redis keys, and the acquire Lua are kept identical
//! to the Python implementation so the two services are drop-in interchangeable
//! behind a reverse proxy. Identity always comes from the session cookie (no
//! IDOR surface). Write endpoints fail closed (503) when Redis is down.

use axum::Json;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde::Deserialize;
use serde_json::{Value, json};
use std::sync::atomic::Ordering;

use crate::auth;
use crate::db::RegistrationInsertOutcome;
use crate::redis_seats::{AcquireOutcome, Seats, new_operation_id, reservation_value};
use crate::state::AppState;
use crate::types::{ClubId, StudentId};

/// Local helper to attach a `Set-Cookie` header to a JSON body + status.
fn json_with_cookie(status: StatusCode, body: Value, cookie: &str) -> Response {
    (
        status,
        [(header::SET_COOKIE, cookie)],
        [(header::CACHE_CONTROL, "no-store")],
        Json(body),
    )
        .into_response()
}

fn json_status(status: StatusCode, body: Value) -> Response {
    (status, Json(body)).into_response()
}

/// `{"success": false, "message": ...}` at the given status.
fn fail(status: StatusCode, msg: &str) -> Response {
    json_status(status, json!({ "success": false, "message": msg }))
}

fn busy(msg: &str) -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        [(header::RETRY_AFTER, "1")],
        Json(json!({ "success": false, "message": msg })),
    )
        .into_response()
}

/// 401 for student endpoints missing a session.
fn unauthorized() -> Response {
    fail(StatusCode::UNAUTHORIZED, "未登录或会话已过期")
}

// ===========================================================================
// 1. POST /api/login
// ===========================================================================

#[derive(Debug, Deserialize)]
pub struct LoginReq {
    #[serde(default)]
    username: String,
    #[serde(default)]
    password: String,
}

pub async fn login(
    State(state): State<AppState>,
    body: Result<Json<LoginReq>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let Json(req) = match body {
        Ok(j) => j,
        Err(_) => return fail(StatusCode::BAD_REQUEST, "JSON 解析失败"),
    };
    let username = req.username.trim().to_string();
    let password = req.password;
    if username.is_empty() || password.is_empty() {
        return fail(StatusCode::BAD_REQUEST, "用户名和密码不能为空");
    }
    if username.len() > 80 || password.len() > 256 {
        return fail(StatusCode::BAD_REQUEST, "用户名或密码过长");
    }

    let seats = Seats::new(state.redis.clone());
    // A campus egress IP is shared by many students. Account-level throttling
    // belongs here; Nginx supplies the bounded source-IP resource ceiling.
    let u_key = format!("u:{username}");
    if seats.login_blocked(&u_key, state.cfg.login_max_fails).await {
        return fail(StatusCode::TOO_MANY_REQUESTS, "尝试过于频繁，请稍后再试");
    }
    let role_epoch = match seats.session_role_epoch("student").await {
        Ok(epoch) => epoch,
        Err(e) => return e.into_response(),
    };

    let row = match state.db.find_student_by_username(username.clone()).await {
        Ok(r) => r,
        Err(e) => return e.into_response(),
    };
    let Some(row) = row else {
        seats.login_fail(&u_key).await;
        return fail(StatusCode::UNAUTHORIZED, "用户名或密码错误");
    };

    let principal_version = match seats
        .session_principal_version("student", &row.id.to_string())
        .await
    {
        Ok(version) => version,
        Err(e) => return e.into_response(),
    };

    let permit = match tokio::time::timeout(
        std::time::Duration::from_secs(1),
        state.auth_gate.clone().acquire_owned(),
    )
    .await
    {
        Ok(Ok(permit)) => permit,
        _ => return busy("登录繁忙，请稍后重试"),
    };
    let stored = row.password.clone();
    let password_for_hash = password.clone();
    let (v, upgraded_hash) = match tokio::task::spawn_blocking(move || {
        let _permit = permit;
        let v = auth::verify_password(&stored, &password_for_hash);
        let upgraded = if v.ok && v.needs_upgrade {
            auth::hash_password(&password_for_hash).ok()
        } else {
            None
        };
        (v, upgraded)
    })
    .await
    {
        Ok(result) => result,
        Err(e) => {
            tracing::error!(error = %e, "password worker failed");
            return fail(StatusCode::INTERNAL_SERVER_ERROR, "服务器错误");
        }
    };
    if !v.ok {
        seats.login_fail(&u_key).await;
        return fail(StatusCode::UNAUTHORIZED, "用户名或密码错误");
    }

    // Verify that the database record survived the Argon2 work unchanged. The
    // generation script below fences a later delete/password change; this read
    // also catches one that completed before the generation snapshot.
    let current = match state.db.find_student_by_username(username.clone()).await {
        Ok(Some(current)) => current,
        Ok(None) => return fail(StatusCode::UNAUTHORIZED, "用户名或密码错误"),
        Err(e) => return e.into_response(),
    };
    if current.id != row.id || current.password != row.password {
        return fail(StatusCode::UNAUTHORIZED, "用户名或密码错误");
    }

    let payload = json!({
        "role": "student",
        "student_id": row.id,
        "name": row.name,
        "class": row.class,
        "student_no": row.student_no,
    });
    let token = match seats
        .session_create(
            &payload,
            state.cfg.session_ttl,
            role_epoch,
            principal_version,
        )
        .await
    {
        Ok(Some(token)) => token,
        Ok(None) => return busy("账号状态已变化，请重新登录"),
        Err(e) => return e.into_response(), // Redis down -> 503
    };

    // Upgrade legacy plaintext to argon2 on first successful login (best-effort).
    if let Some(new_hash) = upgraded_hash
        && let Err(e) = state
            .db
            .update_password_if_matches(StudentId(row.id), new_hash, row.password.clone())
            .await
    {
        tracing::warn!(error = %e, "password upgrade write failed");
    }
    seats.login_ok(&u_key).await;

    let cookie = auth::set_session_cookie(&token, state.cfg.session_ttl, state.cfg.cookie_secure);
    json_with_cookie(
        StatusCode::OK,
        json!({
            "success": true,
            "student_id": row.id,
            "name": row.name,
            "class": row.class,
            "student_no": row.student_no,
        }),
        &cookie,
    )
}

// ===========================================================================
// 2. POST /api/register_club
// ===========================================================================

#[derive(Debug, Deserialize)]
pub struct RegisterReq {
    // Python accepts int or numeric; we accept a JSON number. Missing/!number
    // -> 400 "缺少或非法的社团ID".
    club_id: Option<i64>,
}

pub async fn register_club(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Result<Json<RegisterReq>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let sess = match auth::student_session(&state, &headers).await {
        Ok(Some(sess)) => sess,
        Ok(None) => return unauthorized(),
        Err(e) => return e.into_response(),
    };
    let req = match body {
        Ok(Json(r)) => r,
        Err(_) => return fail(StatusCode::BAD_REQUEST, "缺少或非法的社团ID"),
    };
    let Some(club_id) = req.club_id else {
        return fail(StatusCode::BAD_REQUEST, "缺少或非法的社团ID");
    };
    if club_id <= 0 {
        return fail(StatusCode::BAD_REQUEST, "缺少或非法的社团ID");
    }
    if state.reconcile_requested.load(Ordering::Acquire) {
        return busy("名额状态正在安全对账，请稍后重试");
    }

    let seats = Seats::new(state.redis.clone());
    let sid = sess.student_id;
    if state.reconcile_requested.load(Ordering::Acquire) {
        return busy("名额状态正在安全对账，请稍后重试");
    }

    // Wait for a finalizer slot BEFORE decrementing Redis. Requests cancelled
    // while queued therefore have no side effect, and at most this small number
    // of reservations can be waiting on SQLite.
    let permit = match tokio::time::timeout(
        std::time::Duration::from_millis(state.cfg.registration_queue_timeout_ms),
        state.registration_gate.clone().acquire_owned(),
    )
    .await
    {
        Ok(Ok(permit)) => permit,
        _ => return busy("报名队列繁忙，请稍后重试"),
    };

    let operation_id = new_operation_id();
    let reservation = reservation_value(club_id, &operation_id);
    let when = auth::now_local_string();
    let db = state.db.clone();
    let reconcile_requested = state.reconcile_requested.clone();
    let resv_ttl = state.cfg.resv_ttl;
    // Acquire and every subsequent side effect live inside the detached task.
    // Tower timeout or a client disconnect can stop waiting for the reply, but
    // the task still commits+confirms or rolls back the exact reservation.
    let finalize = tokio::spawn(async move {
        let _permit = permit;
        if reconcile_requested.load(Ordering::Acquire) {
            return RegistrationFinalize::Unavailable;
        }
        let outcome = match seats.acquire(sid, club_id, &reservation, resv_ttl).await {
            Ok(outcome) => outcome,
            Err(e) => {
                tracing::error!(student_id = sid, club_id, error = %e, "seat acquire failed");
                reconcile_requested.store(true, Ordering::Release);
                return RegistrationFinalize::Unavailable;
            }
        };
        match outcome {
            AcquireOutcome::Full => return RegistrationFinalize::Full,
            AcquireOutcome::Already => {
                return match db.registered_club_id(StudentId(sid)).await {
                    Ok(Some(existing)) if existing == club_id => {
                        RegistrationFinalize::AlreadySameClub
                    }
                    Ok(_) => RegistrationFinalize::Already,
                    Err(e) => {
                        tracing::error!(student_id = sid, error = %e, "idempotency lookup failed");
                        RegistrationFinalize::Failed
                    }
                };
            }
            AcquireOutcome::Uninitialized => {
                reconcile_requested.store(true, Ordering::Release);
                return RegistrationFinalize::Uninitialized;
            }
            AcquireOutcome::Maintenance => return RegistrationFinalize::Maintenance,
            AcquireOutcome::NotOpen => return RegistrationFinalize::NotOpen,
            AcquireOutcome::Ok => {}
        }
        let inserted = db
            .insert_registration(StudentId(sid), ClubId(club_id), when, operation_id.clone())
            .await;
        match inserted {
            Ok(RegistrationInsertOutcome::Inserted { registration_id }) => {
                match seats.confirm(sid, &reservation).await {
                    Ok(confirmed) => {
                        if !confirmed {
                            reconcile_requested.store(true, Ordering::Release);
                        }
                        RegistrationFinalize::Success {
                            registration_id,
                            confirmed,
                        }
                    }
                    Err(e) => {
                        tracing::error!(student_id = sid, club_id, error = %e, "confirm failed after DB commit");
                        reconcile_requested.store(true, Ordering::Release);
                        RegistrationFinalize::Success {
                            registration_id,
                            confirmed: false,
                        }
                    }
                }
            }
            Ok(RegistrationInsertOutcome::AlreadyRegistered) => {
                if let Err(e) = seats.rollback_reservation(sid, club_id, &reservation).await {
                    tracing::error!(student_id = sid, club_id, error = %e, "reservation rollback failed");
                }
                reconcile_requested.store(true, Ordering::Release);
                match db.registered_club_id(StudentId(sid)).await {
                    Ok(Some(existing)) if existing == club_id => {
                        RegistrationFinalize::AlreadySameClub
                    }
                    Ok(_) => RegistrationFinalize::Already,
                    Err(e) => {
                        tracing::error!(student_id = sid, error = %e, "idempotency lookup failed");
                        RegistrationFinalize::Failed
                    }
                }
            }
            Ok(RegistrationInsertOutcome::ClubFull) => {
                if let Err(e) = seats.rollback_reservation(sid, club_id, &reservation).await {
                    tracing::error!(student_id = sid, club_id, error = %e, "reservation rollback failed");
                }
                reconcile_requested.store(true, Ordering::Release);
                RegistrationFinalize::Full
            }
            Ok(RegistrationInsertOutcome::ClubMissing) => {
                if let Err(e) = seats.rollback_reservation(sid, club_id, &reservation).await {
                    tracing::error!(student_id = sid, club_id, error = %e, "reservation rollback failed");
                }
                reconcile_requested.store(true, Ordering::Release);
                RegistrationFinalize::Missing
            }
            Err(e) => {
                tracing::error!(student_id = sid, club_id, error = %e, "registration persist failed");
                if let Err(release_error) =
                    seats.rollback_reservation(sid, club_id, &reservation).await
                {
                    tracing::error!(student_id = sid, club_id, error = %release_error, "reservation rollback failed");
                    reconcile_requested.store(true, Ordering::Release);
                }
                reconcile_requested.store(true, Ordering::Release);
                RegistrationFinalize::Failed
            }
        }
    });

    match finalize.await {
        Ok(RegistrationFinalize::Success {
            registration_id,
            confirmed,
        }) => {
            tracing::debug!(registration_id, confirmed, "registration finalized");
            json_status(
                StatusCode::OK,
                json!({
                    "success": true,
                    "message": if confirmed { "报名成功" } else { "报名成功，状态同步稍有延迟" }
                }),
            )
        }
        Ok(RegistrationFinalize::Already) => json_status(
            StatusCode::OK,
            json!({ "success": false, "message": "您已报名其他社团或请勿重复提交" }),
        ),
        Ok(RegistrationFinalize::AlreadySameClub) => json_status(
            StatusCode::OK,
            json!({ "success": true, "message": "您已报名该社团" }),
        ),
        Ok(RegistrationFinalize::Full) => json_status(
            StatusCode::OK,
            json!({ "success": false, "message": "该社团已满员" }),
        ),
        Ok(RegistrationFinalize::Missing) => json_status(
            StatusCode::OK,
            json!({ "success": false, "message": "社团不存在" }),
        ),
        Ok(RegistrationFinalize::Uninitialized) => busy("名额状态暂不可用，请联系管理员"),
        Ok(RegistrationFinalize::Maintenance) => busy("系统维护中，请稍后重试"),
        Ok(RegistrationFinalize::NotOpen) => json_status(
            StatusCode::OK,
            json!({ "success": false, "message": "报名尚未开始" }),
        ),
        Ok(RegistrationFinalize::Unavailable) => busy("系统繁忙，请稍后重试"),
        Ok(RegistrationFinalize::Failed) | Err(_) => {
            fail(StatusCode::INTERNAL_SERVER_ERROR, "报名失败，请重试")
        }
    }
}

enum RegistrationFinalize {
    Success {
        registration_id: i64,
        confirmed: bool,
    },
    Already,
    AlreadySameClub,
    Full,
    Missing,
    Uninitialized,
    Maintenance,
    NotOpen,
    Unavailable,
    Failed,
}

// ===========================================================================
// 3. POST /api/cancel_registration
// ===========================================================================

pub async fn cancel_registration(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let sess = match auth::student_session(&state, &headers).await {
        Ok(Some(sess)) => sess,
        Ok(None) => return unauthorized(),
        Err(e) => return e.into_response(),
    };
    let sid = sess.student_id;

    let permit = match tokio::time::timeout(
        std::time::Duration::from_millis(state.cfg.registration_queue_timeout_ms),
        state.registration_gate.clone().acquire_owned(),
    )
    .await
    {
        Ok(Ok(permit)) => permit,
        _ => return busy("报名状态正在处理，请稍后重试"),
    };
    if state.reconcile_requested.load(Ordering::Acquire) {
        return busy("名额状态正在安全对账，请稍后重试");
    }
    let seats = Seats::new(state.redis.clone());
    let operation_lock = match seats.begin_student_op(sid).await {
        Ok(Some(token)) => token,
        Ok(None) => return busy("报名状态正在处理，请稍后重试"),
        Err(_) => return busy("系统繁忙，请稍后重试"),
    };
    let db = state.db.clone();
    let reconcile_requested = state.reconcile_requested.clone();
    let finalize = tokio::spawn(async move {
        let _permit = permit;
        let result = match db.cancel_registration(StudentId(sid)).await {
            Ok(Some(registration)) => {
                let club_id = registration.club_id.get();
                let value = registration
                    .operation_id
                    .as_deref()
                    .map(|op| reservation_value(club_id, op))
                    .unwrap_or_else(|| club_id.to_string());
                let cancel_event = registration.operation_id.unwrap_or_else(|| {
                    format!(
                        "legacy-{}-{}-{}",
                        registration.registration_id, sid, club_id
                    )
                });
                match seats
                    .release_registration(&cancel_event, sid, club_id, &value)
                    .await
                {
                    Ok(_) => CancelFinalize::Success,
                    Err(e) => {
                        tracing::error!(student_id = sid, error = %e, "cancel Redis finalize failed");
                        reconcile_requested.store(true, Ordering::Release);
                        CancelFinalize::SyncFailed
                    }
                }
            }
            Ok(None) => CancelFinalize::NotRegistered,
            Err(e) => {
                tracing::error!(student_id = sid, error = %e, "cancel failed");
                CancelFinalize::Failed
            }
        };
        if let Err(e) = seats.end_student_op(sid, &operation_lock).await {
            tracing::warn!(student_id = sid, error = %e, "student operation lock cleanup failed");
            reconcile_requested.store(true, Ordering::Release);
        }
        result
    });

    match finalize.await {
        Ok(CancelFinalize::Success) => json_status(
            StatusCode::OK,
            json!({ "success": true, "message": "取消报名成功" }),
        ),
        Ok(CancelFinalize::NotRegistered) => json_status(
            StatusCode::OK,
            json!({ "success": false, "message": "您还未报名任何社团" }),
        ),
        Ok(CancelFinalize::SyncFailed) => fail(
            StatusCode::SERVICE_UNAVAILABLE,
            "退选已记录，后台将在操作排空后安全对账",
        ),
        Ok(CancelFinalize::Failed) | Err(_) => json_status(
            StatusCode::OK,
            json!({ "success": false, "message": "取消报名失败，请重试" }),
        ),
    }
}

enum CancelFinalize {
    Success,
    NotRegistered,
    SyncFailed,
    Failed,
}

// ===========================================================================
// 4. GET /api/get_clubs
// ===========================================================================

pub async fn get_clubs(State(state): State<AppState>) -> Response {
    let rows = match state.db.list_clubs().await {
        Ok(r) => r,
        Err(e) => return e.into_response(),
    };
    let ids: Vec<i64> = rows.iter().map(|r| r.id).collect();

    // Live occupancy: current = max - remaining(stock). Redis miss -> fall back
    // to the stored current_students (read path may degrade to SQLite).
    let seats = Seats::new(state.redis.clone());
    let live = seats.stock_left(&ids).await;

    let mut data = Vec::with_capacity(rows.len());
    for (i, r) in rows.iter().enumerate() {
        let used = match &live {
            Some(vals) => match vals.get(i).and_then(|v| *v) {
                Some(left) => {
                    if left < 0 || left > r.max_students {
                        tracing::error!(
                            club_id = r.id,
                            left,
                            max = r.max_students,
                            "Redis stock outside valid range"
                        );
                    }
                    r.max_students - left
                }
                None => r.current_students, // key absent for this club
            },
            None => r.current_students, // redis unavailable
        };
        let clamped = used.clamp(0, r.max_students);
        data.push(json!({
            "id": r.id,
            "name": r.name,
            "max_students": r.max_students,
            "current_students": clamped,
        }));
    }
    json_status(StatusCode::OK, Value::Array(data))
}

// ===========================================================================
// 5. GET /api/check_registration_time
// ===========================================================================

pub async fn check_registration_time(State(state): State<AppState>) -> Response {
    let seats = Seats::new(state.redis.clone());
    let mut open_at = seats.open_at_get().await;
    let mut start_str: Option<String> = None;

    if let Some(epoch) = open_at {
        // Render the epoch back to a human string for the client.
        start_str = auth::format_local_datetime(epoch);
    } else if let Ok(Some(s)) = state.db.registration_start_time().await {
        // Fall back to SQLite settings string.
        start_str = Some(s.clone());
        open_at = auth::parse_local_datetime(&s);
    }

    let now = seats.now_epoch().await;
    let can = match open_at {
        Some(o) => now >= o,
        None => false,
    };
    json_status(
        StatusCode::OK,
        json!({ "can_register": can, "start_time": start_str }),
    )
}

// ===========================================================================
// 6. GET /api/get_student_info
// ===========================================================================

pub async fn get_student_info(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let sess = match auth::student_session(&state, &headers).await {
        Ok(Some(sess)) => sess,
        Ok(None) => return unauthorized(),
        Err(e) => return e.into_response(),
    };
    let info = match state.db.student_info(StudentId(sess.student_id)).await {
        Ok(r) => r,
        Err(e) => return e.into_response(),
    };
    let Some(info) = info else {
        return fail(StatusCode::NOT_FOUND, "学生不存在");
    };
    (
        StatusCode::OK,
        [(header::CACHE_CONTROL, "no-store")],
        Json(json!({
            "name": info.name,
            "class": info.class,
            "student_id": info.student_no,
            "username": info.username,
            "registered_club": info.registered_club,
            "registration_time": info.registration_time,
        })),
    )
        .into_response()
}

// ===========================================================================
// health / readiness
// ===========================================================================

/// `/healthz` — 200 only if SQLite answers `SELECT 1` and Redis answers `PING`.
pub async fn healthz(State(state): State<AppState>) -> Response {
    let db_ok = state.db.ping().await.is_ok();
    let seats = Seats::new(state.redis.clone());
    let redis_ok = seats.alive().await;
    if db_ok && redis_ok {
        json_status(StatusCode::OK, json!({ "status": "ok" }))
    } else {
        json_status(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({ "status": "degraded", "db": db_ok, "redis": redis_ok }),
        )
    }
}

/// `/readyz` — 200 only once stock has been initialized (`seats:initialized`).
pub async fn readyz(State(state): State<AppState>) -> Response {
    let seats = Seats::new(state.redis.clone());
    if state.reconcile_requested.load(Ordering::Acquire) {
        return json_status(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({ "status": "not-ready", "reason": "reconcile-pending" }),
        );
    }
    if !seats.initialized().await {
        state.reconcile_requested.store(true, Ordering::Release);
        return json_status(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({ "status": "not-ready" }),
        );
    }
    if seats.maintenance_active().await != Some(false) {
        return json_status(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({ "status": "not-ready", "reason": "maintenance" }),
        );
    }

    let clubs = match state.db.club_stock_snapshot().await {
        Ok(clubs) => clubs,
        Err(_) => {
            return json_status(
                StatusCode::SERVICE_UNAVAILABLE,
                json!({ "status": "not-ready", "reason": "database" }),
            );
        }
    };
    let ids: Vec<i64> = clubs.iter().map(|c| c.club_id).collect();
    let Some(live) = seats.stock_left(&ids).await else {
        return json_status(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({ "status": "not-ready", "reason": "redis" }),
        );
    };
    let mut drifted = 0usize;
    for (club, left) in clubs.iter().zip(live.iter()) {
        if club.used > club.max_students {
            drifted += 1;
            continue;
        }
        let expected = (club.max_students - club.used).max(0);
        match left {
            Some(value) if *value == expected => {}
            _ => drifted += 1,
        }
    }
    if drifted > 0 {
        let active = seats.has_active_operations().await;
        if active == Some(false) {
            state.reconcile_requested.store(true, Ordering::Release);
        }
        json_status(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({
                "status": "not-ready",
                "reason": if active == Some(true) { "operations-active" } else { "stock-drift" },
                "clubs": drifted
            }),
        )
    } else {
        json_status(StatusCode::OK, json!({ "status": "ready" }))
    }
}
