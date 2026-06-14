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

use crate::auth;
use crate::redis_seats::{AcquireOutcome, Seats};
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
    headers: HeaderMap,
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

    let seats = Seats::new(state.redis.clone());
    let ip = client_ip(&headers);

    // Throttle: block if either the username or IP failure count is over cap.
    let u_key = format!("u:{username}");
    let ip_key = format!("ip:{ip}");
    if seats.login_blocked(&u_key, state.cfg.login_max_fails).await
        || seats
            .login_blocked(&ip_key, state.cfg.login_ip_max_fails)
            .await
    {
        return fail(StatusCode::TOO_MANY_REQUESTS, "尝试过于频繁，请稍后再试");
    }

    let row = match state.db.find_student_by_username(username.clone()).await {
        Ok(r) => r,
        Err(e) => return e.into_response(),
    };
    let Some(row) = row else {
        seats.login_fail(&u_key).await;
        seats.login_fail(&ip_key).await;
        return fail(StatusCode::UNAUTHORIZED, "用户名或密码错误");
    };

    let v = auth::verify_password(&row.password, &password);
    if !v.ok {
        seats.login_fail(&u_key).await;
        seats.login_fail(&ip_key).await;
        return fail(StatusCode::UNAUTHORIZED, "用户名或密码错误");
    }

    // Upgrade legacy plaintext to argon2 on first successful login (best-effort).
    if v.needs_upgrade {
        if let Ok(new_hash) = auth::hash_password(&password) {
            if let Err(e) = state.db.update_password(StudentId(row.id), new_hash).await {
                tracing::warn!(error = %e, "password upgrade write failed");
            }
        }
    }

    seats.login_ok(&u_key).await;

    let payload = json!({
        "role": "student",
        "student_id": row.id,
        "name": row.name,
        "class": row.class,
        "student_no": row.student_no,
    });
    let token = match seats.session_create(&payload, state.cfg.session_ttl).await {
        Ok(t) => t,
        Err(e) => return e.into_response(), // Redis down -> 503
    };

    let cookie = auth::set_session_cookie(&token, state.cfg.session_ttl);
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
    let Some(sess) = auth::student_session(&state, &headers).await else {
        return unauthorized();
    };
    let req = match body {
        Ok(Json(r)) => r,
        Err(_) => return fail(StatusCode::BAD_REQUEST, "缺少或非法的社团ID"),
    };
    let Some(club_id) = req.club_id else {
        return fail(StatusCode::BAD_REQUEST, "缺少或非法的社团ID");
    };

    let seats = Seats::new(state.redis.clone());
    let sid = sess.student_id;

    // Backend time gate: must have open_at and now >= open_at.
    let open_at = seats.open_at_get().await;
    let now = seats.now_epoch().await;
    if open_at.is_none() || now < open_at.unwrap() {
        return json_status(
            StatusCode::OK,
            json!({ "success": false, "message": "报名尚未开始" }),
        );
    }

    // Atomic acquire. Redis down -> 503.
    let mut outcome = match seats.acquire(sid, club_id, state.cfg.resv_ttl).await {
        Ok(o) => o,
        Err(_) => return fail(StatusCode::SERVICE_UNAVAILABLE, "系统繁忙，请稍后重试"),
    };

    // -2 = this club's stock key is missing. Once initialized, recover only THIS club's
    // key from its committed count (SET NX, can't oversell since a missing key means no
    // in-flight reservation for it); never full-rebuild here (would re-add seats reserved
    // on other clubs). Cold start: full rebuild.
    if outcome == AcquireOutcome::Uninitialized {
        if seats.initialized().await {
            let exists = crate::redis_seats::init_club_stock(&seats, &state.db, club_id)
                .await
                .unwrap_or(false);
            if !exists {
                return json_status(
                    StatusCode::OK,
                    json!({ "success": false, "message": "该社团不存在" }),
                );
            }
        } else {
            let _ = crate::redis_seats::rebuild_stock(&seats, &state.db).await;
        }
        outcome = match seats.acquire(sid, club_id, state.cfg.resv_ttl).await {
            Ok(o) => o,
            Err(_) => return fail(StatusCode::SERVICE_UNAVAILABLE, "系统繁忙，请稍后重试"),
        };
    }

    match outcome {
        AcquireOutcome::Full => {
            return json_status(
                StatusCode::OK,
                json!({ "success": false, "message": "该社团已满员" }),
            );
        }
        AcquireOutcome::Already => {
            return json_status(
                StatusCode::OK,
                json!({ "success": false, "message": "您已报名其他社团或请勿重复提交" }),
            );
        }
        AcquireOutcome::Uninitialized => {
            return json_status(
                StatusCode::OK,
                json!({ "success": false, "message": "社团不存在或暂不可报名" }),
            );
        }
        AcquireOutcome::Ok => {}
    }

    // Won the seat -> persist to SQLite (blocking pool handles the work).
    let when = auth::now_local_string();
    match state
        .db
        .insert_registration(StudentId(sid), ClubId(club_id), when)
        .await
    {
        Ok(()) => {
            seats.confirm(sid, club_id).await;
            json_status(
                StatusCode::OK,
                json!({ "success": true, "message": "报名成功" }),
            )
        }
        Err(e) => {
            tracing::error!(student_id = sid, club_id, error = %e, "registration persist failed");
            seats.release(sid, club_id).await; // compensate the acquire
            json_status(
                StatusCode::OK,
                json!({ "success": false, "message": "报名失败，请重试" }),
            )
        }
    }
}

// ===========================================================================
// 3. POST /api/cancel_registration
// ===========================================================================

pub async fn cancel_registration(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Response {
    let Some(sess) = auth::student_session(&state, &headers).await else {
        return unauthorized();
    };
    let sid = sess.student_id;

    match state.db.cancel_registration(StudentId(sid)).await {
        Ok(Some(club_id)) => {
            let seats = Seats::new(state.redis.clone());
            seats.release(sid, club_id.get()).await; // give the seat back
            json_status(
                StatusCode::OK,
                json!({ "success": true, "message": "取消报名成功" }),
            )
        }
        Ok(None) => json_status(
            StatusCode::OK,
            json!({ "success": false, "message": "您还未报名任何社团" }),
        ),
        Err(e) => {
            tracing::error!(student_id = sid, error = %e, "cancel failed");
            json_status(
                StatusCode::OK,
                json!({ "success": false, "message": "取消报名失败，请重试" }),
            )
        }
    }
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
                Some(left) => r.max_students - left,
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

    if open_at.is_none() {
        // Fall back to SQLite settings string.
        if let Ok(Some(s)) = state.db.registration_start_time().await {
            start_str = Some(s.clone());
            open_at = auth::parse_local_datetime(&s);
        }
    } else {
        // Render the epoch back to a human string for the client.
        start_str = auth::format_local_datetime(open_at.unwrap());
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

pub async fn get_student_info(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Response {
    let Some(sess) = auth::student_session(&state, &headers).await else {
        return unauthorized();
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
    if seats.initialized().await {
        json_status(StatusCode::OK, json!({ "status": "ready" }))
    } else {
        json_status(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({ "status": "not-ready" }),
        )
    }
}

// --- helpers ---------------------------------------------------------------

/// Client IP for login throttling. Trust only X-Real-IP (our nginx sets it to the
/// peer address); client-supplied X-Forwarded-For is spoofable and must not be used.
fn client_ip(headers: &HeaderMap) -> String {
    headers
        .get("x-real-ip")
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| "local".to_string())
}
