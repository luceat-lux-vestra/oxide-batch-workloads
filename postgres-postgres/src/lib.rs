//! `postgres-postgres`: OxideBatch 0.6.0 real-workload validation for a
//! PostgreSQL -> PostgreSQL restartable transform, in either cursor mode or
//! paging mode (`--reader cursor|paging`). See the crate README for
//! architecture, schemas, and scope.
//!
//! Split into a library plus a thin binary (`src/main.rs`) so integration
//! tests (e.g. `tests/source_identity.rs`) can call `source_digest::compute`
//! directly against a test database, instead of scraping it back out of the
//! CLI's stdout/logs.

pub mod failpoint;
pub mod generator;
pub mod hex;
pub mod job;
pub mod processor;
pub mod source_digest;
pub mod verify;
pub mod writer;
