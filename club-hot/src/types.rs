//! Strongly-typed newtypes for IDs that cross the wire / DB boundary.
//!
//! `main.py` mixes `str` and `int` for the two different "student id" concepts
//! (the DB primary key `students.id` vs. the school-issued `student_id` text).
//! We keep them as distinct newtypes so the compiler refuses to confuse them.

use serde::{Deserialize, Serialize};

/// Internal DB primary key `students.id`. This is the identity carried in the
/// session and used for all registration logic.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct StudentId(pub i64);

impl StudentId {
    #[inline]
    pub fn get(self) -> i64 {
        self.0
    }
}

impl std::fmt::Display for StudentId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// `clubs.id`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ClubId(pub i64);

impl ClubId {
    #[inline]
    pub fn get(self) -> i64 {
        self.0
    }
}

impl std::fmt::Display for ClubId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// School-issued student number `students.student_id` (a TEXT column, e.g. a
/// learner card number). Distinct from [`StudentId`] on purpose.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct StudentNo(pub String);
