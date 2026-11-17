//! 社团目录与名额快照的短时缓存；按内容生成 ETag，支持 304。

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::body::Bytes;
use serde_json::{Value, json};

use crate::db::ClubRow;
use crate::error::AppResult;
use crate::redis_seats::Seats;
use crate::state::AppState;

const CATALOG_TTL: Duration = Duration::from_secs(1);

#[derive(Clone)]
pub struct CachedJson {
    pub body: Bytes,
    pub etag: String,
    created: Instant,
}

impl CachedJson {
    fn new(value: &Value) -> Self {
        let body = Bytes::from(serde_json::to_vec(value).unwrap_or_default());
        CachedJson {
            etag: etag(&body),
            body,
            created: Instant::now(),
        }
    }
}

pub struct Catalog {
    pub rows: Vec<ClubRow>,
    by_id: HashMap<i64, usize>,
    pub json: CachedJson,
}

impl Catalog {
    pub fn club(&self, id: i64) -> Option<&ClubRow> {
        self.by_id.get(&id).map(|index| &self.rows[*index])
    }
}

/// FNV-1a 64 位摘要，用作弱 ETag。
fn etag(body: &[u8]) -> String {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in body {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("W/\"{hash:016x}\"")
}

fn list(value: &str) -> Value {
    serde_json::from_str::<Value>(value)
        .ok()
        .filter(Value::is_array)
        .unwrap_or_else(|| Value::Array(Vec::new()))
}

pub async fn catalog(state: &AppState) -> AppResult<Arc<Catalog>> {
    let mut slot = state.catalog.lock().await;
    if let Some(current) = slot.as_ref()
        && current.json.created.elapsed() < CATALOG_TTL
    {
        return Ok(current.clone());
    }
    let rows = state.db.list_clubs().await?;
    let items: Vec<Value> = rows
        .iter()
        .map(|r| {
            json!({
                "id": r.id,
                "name": r.name,
                "max_students": r.max_students,
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
    let by_id = rows.iter().enumerate().map(|(i, r)| (r.id, i)).collect();
    let fresh = Arc::new(Catalog {
        json: CachedJson::new(&Value::Array(items)),
        rows,
        by_id,
    });
    *slot = Some(fresh.clone());
    Ok(fresh)
}

pub async fn seats(state: &AppState) -> AppResult<CachedJson> {
    let mut slot = state.seats.lock().await;
    if let Some(current) = slot.as_ref()
        && current.created.elapsed() < state.cfg.seats_cache
    {
        return Ok(current.clone());
    }
    let catalog = catalog(state).await?;
    let redis = Seats::new(state.redis.clone(), &state.cfg);
    let ids: Vec<i64> = catalog.rows.iter().map(|r| r.id).collect();
    let live = redis.stock_left(&ids).await;
    let seats: Vec<Value> = catalog
        .rows
        .iter()
        .enumerate()
        .map(|(i, r)| {
            let left = live
                .as_ref()
                .and_then(|values| values.get(i).copied().flatten())
                .unwrap_or(r.max_students - r.current_students);
            json!([r.id, left.clamp(0, r.max_students)])
        })
        .collect();
    let mut open_at = redis.open_at_get().await;
    if open_at.is_none()
        && let Some(text) = state.db.registration_start_time().await?
    {
        open_at = crate::auth::parse_local_datetime(&text);
    }
    let end_time = state.db.registration_end_time().await?;
    let close_at = end_time
        .as_deref()
        .and_then(crate::auth::parse_local_datetime);
    let now = redis.now_epoch_ms().await;
    let locked = state.db.registration_locked().await?
        || redis.locked_get().await.unwrap_or(false)
        || close_at.is_some_and(|c| now >= c.saturating_mul(1000));
    let fresh = CachedJson::new(&json!({
        "catalog": catalog.json.etag,
        "open_at": open_at.map(|value| value.saturating_mul(1000)),
        "close_at": close_at.map(|value| value.saturating_mul(1000)),
        "end_time": if close_at.is_some() { json!(end_time) } else { Value::Null },
        "locked": locked,
        "seats": seats,
    }));
    *slot = Some(fresh.clone());
    Ok(fresh)
}

#[cfg(test)]
mod tests {
    #[test]
    fn etag_changes_with_content() {
        assert_ne!(super::etag(b"[1,2]"), super::etag(b"[1,3]"));
        assert_eq!(super::etag(b"abc"), super::etag(b"abc"));
    }
}
