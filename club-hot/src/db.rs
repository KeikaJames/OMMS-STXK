//! SQLite access via two deadpool-sqlite pools sharing the same WAL file:
//! - `write` pool with size 1 (serializes writers -> no `SQLITE_BUSY` storms),
//! - `read` pool with size N for concurrent readers.
//!
//! Every physical connection runs `busy_timeout=10000` + `foreign_keys=ON` on
//! creation via a deadpool hook. WAL itself is already persisted on the file.

use deadpool_sqlite::{Config, Hook, HookError, Pool, Runtime};
use rusqlite::{Connection, TransactionBehavior};

use crate::error::{AppError, AppResult};
use crate::types::{ClubId, StudentId};

/// A club row as exposed by `/api/get_clubs` (static part).
#[derive(Debug, Clone)]
pub struct ClubRow {
    pub id: i64,
    pub name: String,
    pub max_students: i64,
    pub current_students: i64,
}

/// One `(club_id, max_students, used_count)` triple for stock rebuild.
#[derive(Debug, Clone, Copy)]
pub struct ClubStock {
    pub club_id: i64,
    pub max_students: i64,
    pub used: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RegistrationInsertOutcome {
    Inserted { registration_id: i64 },
    AlreadyRegistered,
    ClubFull,
    ClubMissing,
}

#[derive(Debug, Clone)]
pub struct CancelledRegistration {
    pub registration_id: i64,
    pub club_id: ClubId,
    pub operation_id: Option<String>,
}

#[derive(Clone)]
pub struct Db {
    pub write: Pool,
    pub read: Pool,
}

fn pragma_hook() -> Hook {
    Hook::async_fn(|obj, _metrics| {
        Box::pin(async move {
            obj.interact(|conn: &mut Connection| {
                // busy_timeout: wait up to 10s for a writer lock instead of
                // erroring immediately. foreign_keys: enforce FK constraints
                // (off by default per-connection in SQLite).
                conn.execute_batch("PRAGMA busy_timeout=10000; PRAGMA foreign_keys=ON;")
            })
            .await
            .map_err(|e| HookError::message(format!("interact: {e}")))?
            .map_err(|e| HookError::message(format!("pragma: {e}")))?;
            Ok(())
        })
    })
}

impl Db {
    /// Build the two pools for `db_path`. `read_size` is the reader pool size.
    pub fn new(db_path: &str, read_size: usize) -> AppResult<Self> {
        let make = |size: usize| -> AppResult<Pool> {
            let cfg = Config::new(db_path);
            let pool = cfg
                .builder(Runtime::Tokio1)
                .map_err(|e| AppError::Internal(format!("sqlite pool build: {e}")))?
                .max_size(size)
                .post_create(pragma_hook())
                .build()
                .map_err(|e| AppError::Internal(format!("sqlite pool: {e}")))?;
            Ok(pool)
        };
        Ok(Db {
            write: make(1)?,
            read: make(read_size.max(1))?,
        })
    }

    /// Migrate existing databases and install a final SQLite capacity guard.
    /// Redis remains the fast admission layer, but a stale/incorrect Redis count
    /// must never be able to persist the `(max + 1)`th registration.
    pub async fn ensure_schema_guards(&self) -> AppResult<()> {
        let conn = self.write.get().await?;
        conn.interact(|conn| {
            let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
            let has_operation_id = {
                let mut stmt = tx.prepare("PRAGMA table_info(registrations)")?;
                let names = stmt
                    .query_map([], |r| r.get::<_, String>(1))?
                    .collect::<Result<Vec<_>, _>>()?;
                names.iter().any(|name| name == "operation_id")
            };
            if !has_operation_id {
                tx.execute("ALTER TABLE registrations ADD COLUMN operation_id TEXT", [])?;
            }
            tx.execute(
                "UPDATE registrations SET operation_id = lower(hex(randomblob(16))) \
                 WHERE operation_id IS NULL OR operation_id = ''",
                [],
            )?;
            tx.execute_batch(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_registrations_operation_id \
                   ON registrations(operation_id) WHERE operation_id IS NOT NULL; \
                 CREATE TRIGGER IF NOT EXISTS registrations_capacity_guard \
                 BEFORE INSERT ON registrations FOR EACH ROW BEGIN \
                   SELECT CASE WHEN \
                     (SELECT COUNT(*) FROM registrations WHERE club_id = NEW.club_id) >= \
                     (SELECT max_students FROM clubs WHERE id = NEW.club_id) \
                   THEN RAISE(ABORT, 'club full') END; \
                 END; \
                 CREATE TRIGGER IF NOT EXISTS registrations_capacity_guard_update \
                 BEFORE UPDATE OF club_id ON registrations \
                 FOR EACH ROW WHEN NEW.club_id != OLD.club_id BEGIN \
                   SELECT CASE WHEN \
                     (SELECT COUNT(*) FROM registrations WHERE club_id = NEW.club_id) >= \
                     (SELECT max_students FROM clubs WHERE id = NEW.club_id) \
                   THEN RAISE(ABORT, 'club full') END; \
                 END; \
                 CREATE TRIGGER IF NOT EXISTS clubs_capacity_limit_insert \
                 BEFORE INSERT ON clubs FOR EACH ROW \
                 WHEN NEW.max_students < 1 OR NEW.max_students > 10000 BEGIN \
                   SELECT RAISE(ABORT, 'invalid club capacity'); \
                 END; \
                 CREATE TRIGGER IF NOT EXISTS clubs_capacity_limit_update \
                 BEFORE UPDATE OF max_students ON clubs FOR EACH ROW \
                 WHEN NEW.max_students < 1 OR NEW.max_students > 10000 BEGIN \
                   SELECT RAISE(ABORT, 'invalid club capacity'); \
                 END;",
            )?;
            tx.commit()?;
            Ok::<_, rusqlite::Error>(())
        })
        .await
        .map_err(|e| AppError::Db(format!("interact: {e}")))??;
        Ok(())
    }

    /// Read all clubs (static columns + current_students as stored — callers
    /// that want live occupancy overlay Redis on top).
    pub async fn list_clubs(&self) -> AppResult<Vec<ClubRow>> {
        let conn = self.read.get().await?;
        let rows = conn
            .interact(|conn| {
                let mut stmt = conn.prepare(
                    "SELECT id, name, max_students, current_students FROM clubs ORDER BY id",
                )?;
                let rows = stmt
                    .query_map([], |r| {
                        Ok(ClubRow {
                            id: r.get(0)?,
                            name: r.get(1)?,
                            max_students: r.get(2)?,
                            current_students: r.get(3)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;
                Ok::<_, rusqlite::Error>(rows)
            })
            .await
            .map_err(|e| AppError::Db(format!("interact: {e}")))??;
        Ok(rows)
    }

    /// For stock rebuild: each club with its max and the live COUNT of
    /// registrations (authoritative — we never trust `current_students`).
    pub async fn club_stock_snapshot(&self) -> AppResult<Vec<ClubStock>> {
        let conn = self.read.get().await?;
        let rows = conn
            .interact(|conn| {
                let mut stmt = conn.prepare(
                    "SELECT c.id, c.max_students, \
                     (SELECT COUNT(*) FROM registrations r WHERE r.club_id = c.id) \
                     FROM clubs c",
                )?;
                let rows = stmt
                    .query_map([], |r| {
                        Ok(ClubStock {
                            club_id: r.get(0)?,
                            max_students: r.get(1)?,
                            used: r.get(2)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;
                Ok::<_, rusqlite::Error>(rows)
            })
            .await
            .map_err(|e| AppError::Db(format!("interact: {e}")))??;
        Ok(rows)
    }

    /// Capacity rows and registration mirrors from one SQLite read transaction,
    /// used by a maintenance-fenced Redis rebuild.
    pub async fn seat_snapshot(
        &self,
    ) -> AppResult<(Vec<ClubStock>, Vec<(i64, i64, Option<String>)>)> {
        let conn = self.read.get().await?;
        let snapshot = conn
            .interact(|conn| {
                let tx = conn.transaction()?;
                let clubs = {
                    let mut stmt = tx.prepare(
                        "SELECT c.id, c.max_students, \
                         (SELECT COUNT(*) FROM registrations r WHERE r.club_id = c.id) \
                         FROM clubs c",
                    )?;
                    stmt.query_map([], |r| {
                        Ok(ClubStock {
                            club_id: r.get(0)?,
                            max_students: r.get(1)?,
                            used: r.get(2)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?
                };
                let registrations = {
                    let mut stmt =
                        tx.prepare("SELECT student_id, club_id, operation_id FROM registrations")?;
                    stmt.query_map([], |r| {
                        Ok((
                            r.get::<_, i64>(0)?,
                            r.get::<_, i64>(1)?,
                            r.get::<_, Option<String>>(2)?,
                        ))
                    })?
                    .collect::<Result<Vec<_>, _>>()?
                };
                tx.commit()?;
                Ok::<_, rusqlite::Error>((clubs, registrations))
            })
            .await
            .map_err(|e| AppError::Db(format!("interact: {e}")))??;
        Ok(snapshot)
    }

    /// `settings.registration_start_time` (latest row), used as the SQLite
    /// fallback for the time gate when Redis `open_at` is absent.
    pub async fn registration_start_time(&self) -> AppResult<Option<String>> {
        let conn = self.read.get().await?;
        let v = conn
            .interact(|conn| {
                conn.query_row(
                    "SELECT registration_start_time FROM settings ORDER BY id DESC LIMIT 1",
                    [],
                    |r| r.get::<_, Option<String>>(0),
                )
                .optional()
            })
            .await
            .map_err(|e| AppError::Db(format!("interact: {e}")))??;
        // flatten Option<Option<String>>
        Ok(v.flatten())
    }

    /// Look up the student row used to build a session after a successful login.
    /// Returns the identity/profile fields plus password hash or legacy plaintext.
    pub async fn find_student_by_username(
        &self,
        username: String,
    ) -> AppResult<Option<StudentAuthRow>> {
        let conn = self.read.get().await?;
        let row = conn
            .interact(move |conn| {
                conn.query_row(
                    "SELECT id, name, class, student_id, password \
                     FROM students WHERE username = ?1",
                    [username],
                    |r| {
                        Ok(StudentAuthRow {
                            id: r.get(0)?,
                            name: r.get(1)?,
                            class: r.get(2)?,
                            student_no: r.get(3)?,
                            password: r.get(4)?,
                        })
                    },
                )
                .optional()
            })
            .await
            .map_err(|e| AppError::Db(format!("interact: {e}")))??;
        Ok(row)
    }

    /// Upgrade a legacy password only if it is still the exact value that was
    /// authenticated. A concurrent password change must never be overwritten
    /// by a delayed login's opportunistic rehash.
    pub async fn update_password_if_matches(
        &self,
        id: StudentId,
        new_hash: String,
        expected_old: String,
    ) -> AppResult<bool> {
        let conn = self.write.get().await?;
        let id = id.get();
        let changed = conn
            .interact(move |conn| {
                conn.execute(
                    "UPDATE students SET password = ?1 WHERE id = ?2 AND password = ?3",
                    rusqlite::params![new_hash, id, expected_old],
                )
            })
            .await
            .map_err(|e| AppError::Db(format!("interact: {e}")))??;
        Ok(changed == 1)
    }

    /// Profile for `/api/get_student_info`. JOINs the (optional) registration.
    pub async fn student_info(&self, id: StudentId) -> AppResult<Option<StudentInfoRow>> {
        let conn = self.read.get().await?;
        let id = id.get();
        let row = conn
            .interact(move |conn| {
                conn.query_row(
                    "SELECT s.name, s.class, s.student_id, s.username, \
                            c.name, r.registration_time \
                     FROM students s \
                     LEFT JOIN registrations r ON r.student_id = s.id \
                     LEFT JOIN clubs c ON c.id = r.club_id \
                     WHERE s.id = ?1",
                    [id],
                    |r| {
                        Ok(StudentInfoRow {
                            name: r.get(0)?,
                            class: r.get(1)?,
                            student_no: r.get(2)?,
                            username: r.get(3)?,
                            registered_club: r.get(4)?,
                            registration_time: r.get(5)?,
                        })
                    },
                )
                .optional()
            })
            .await
            .map_err(|e| AppError::Db(format!("interact: {e}")))??;
        Ok(row)
    }

    pub async fn registered_club_id(&self, id: StudentId) -> AppResult<Option<i64>> {
        let conn = self.read.get().await?;
        let id = id.get();
        let club_id = conn
            .interact(move |conn| {
                conn.query_row(
                    "SELECT club_id FROM registrations WHERE student_id = ?1",
                    [id],
                    |r| r.get(0),
                )
                .optional()
            })
            .await
            .map_err(|e| AppError::Db(format!("interact: {e}")))??;
        Ok(club_id)
    }

    /// Transactionally persist a registration after Redis acquire succeeded:
    /// INSERT into registrations + bump current_students. Returns Ok(()) on
    /// success. The IMMEDIATE transaction and explicit count are a DB-level
    /// capacity backstop shared with the Python service.
    pub async fn insert_registration(
        &self,
        sid: StudentId,
        cid: ClubId,
        when: String,
        operation_id: String,
    ) -> AppResult<RegistrationInsertOutcome> {
        let conn = self.write.get().await?;
        let (sid, cid) = (sid.get(), cid.get());
        let outcome = conn
            .interact(move |conn| {
                let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
                let max_students: Option<i64> = tx
                    .query_row("SELECT max_students FROM clubs WHERE id = ?1", [cid], |r| {
                        r.get(0)
                    })
                    .optional()?;
                let Some(max_students) = max_students else {
                    tx.rollback()?;
                    return Ok::<_, rusqlite::Error>(RegistrationInsertOutcome::ClubMissing);
                };
                let already: bool = tx.query_row(
                    "SELECT EXISTS(SELECT 1 FROM registrations WHERE student_id = ?1)",
                    [sid],
                    |r| r.get(0),
                )?;
                if already {
                    tx.rollback()?;
                    return Ok(RegistrationInsertOutcome::AlreadyRegistered);
                }
                let used: i64 = tx.query_row(
                    "SELECT COUNT(*) FROM registrations WHERE club_id = ?1",
                    [cid],
                    |r| r.get(0),
                )?;
                if used >= max_students {
                    tx.rollback()?;
                    return Ok(RegistrationInsertOutcome::ClubFull);
                }
                tx.execute(
                    "INSERT INTO registrations \
                 (student_id, club_id, registration_time, operation_id) \
                 VALUES (?1, ?2, ?3, ?4)",
                    rusqlite::params![sid, cid, when, operation_id],
                )?;
                let registration_id = tx.last_insert_rowid();
                tx.execute(
                    "UPDATE clubs SET current_students = current_students + 1 WHERE id = ?1",
                    [cid],
                )?;
                tx.commit()?;
                Ok::<_, rusqlite::Error>(RegistrationInsertOutcome::Inserted { registration_id })
            })
            .await
            .map_err(|e| AppError::Db(format!("interact: {e}")))??;
        Ok(outcome)
    }

    /// Cancel: look up the club, delete the registration, decrement
    /// current_students (floored at 0). Returns the freed `ClubId` so the caller
    /// can `INCR` the Redis stock, or None if the student had no registration.
    pub async fn cancel_registration(
        &self,
        sid: StudentId,
    ) -> AppResult<Option<CancelledRegistration>> {
        let conn = self.write.get().await?;
        let sid_i = sid.get();
        let freed = conn
            .interact(move |conn| {
                let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
                let registration: Option<(i64, i64, Option<String>)> = tx
                    .query_row(
                        "SELECT id, club_id, operation_id FROM registrations WHERE student_id = ?1",
                        [sid_i],
                        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                    )
                    .optional()?;
                let Some((registration_id, club_id, operation_id)) = registration else {
                    tx.rollback()?;
                    return Ok::<Option<CancelledRegistration>, rusqlite::Error>(None);
                };
                let deleted = tx.execute(
                    "DELETE FROM registrations WHERE id = ?1 AND student_id = ?2",
                    rusqlite::params![registration_id, sid_i],
                )?;
                if deleted != 1 {
                    tx.rollback()?;
                    return Ok(None);
                }
                // floor at 0 so concurrent/admin deletes can't drive it negative.
                tx.execute(
                    "UPDATE clubs SET current_students = current_students - 1 \
                     WHERE id = ?1 AND current_students > 0",
                    [club_id],
                )?;
                tx.commit()?;
                Ok(Some(CancelledRegistration {
                    registration_id,
                    club_id: ClubId(club_id),
                    operation_id,
                }))
            })
            .await
            .map_err(|e| AppError::Db(format!("interact: {e}")))??;
        Ok(freed)
    }

    /// Cheap liveness probe for `/healthz`: grab a reader and run `SELECT 1`.
    pub async fn ping(&self) -> AppResult<()> {
        let conn = self.read.get().await?;
        conn.interact(|conn| conn.query_row("SELECT 1", [], |r| r.get::<_, i64>(0)))
            .await
            .map_err(|e| AppError::Db(format!("interact: {e}")))??;
        Ok(())
    }
}

#[derive(Debug, Clone)]
pub struct StudentAuthRow {
    pub id: i64,
    pub name: String,
    pub class: String,
    pub student_no: String,
    pub password: String,
}

#[derive(Debug, Clone)]
pub struct StudentInfoRow {
    pub name: String,
    pub class: String,
    pub student_no: String,
    pub username: String,
    pub registered_club: Option<String>,
    pub registration_time: Option<String>,
}

// bring `.optional()` into scope for query_row.
use rusqlite::OptionalExtension;
