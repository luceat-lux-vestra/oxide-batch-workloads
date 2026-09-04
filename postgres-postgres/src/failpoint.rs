//! Deterministic, reproducible fault injection built into the shipped
//! binary itself (`--fail-at-chunk N --failure-mode ... [--pause-for-kill
//! <path>]` on the `run` command), not just the test suite.
//!
//! These decorators are built on exactly the same public traits a real
//! consumer implements against (`ItemWriter`, `ChunkTransactionManager`,
//! `ChunkTransaction`), never a reimplementation of framework internals --
//! mirroring `csv-postgres/src/failpoint.rs`'s established shape in this
//! same repository (an independent, workload-local copy, not a shared
//! dependency -- see the crate README's scope notes).
//!
//! Two distinct actions a firing failpoint can take, selected by
//! [`FailAction`]:
//!
//! - [`FailAction::TypedError`]: returns a typed, graceful error (the
//!   pipeline rolls the chunk transaction back; the process then exits
//!   non-zero on its own). Used for pre-commit typed-rollback evidence.
//! - [`FailAction::PauseForKill`]: instead of erroring or self-terminating,
//!   writes this process's PID to the given marker file and then blocks
//!   forever. The *test harness* (a separate parent process) polls for that
//!   marker file and, once it appears, sends a real `SIGKILL` to this exact
//!   child (`std::process::Child::kill`) -- so the crash is a genuine,
//!   externally delivered OS-level process termination, synchronized to the
//!   precise semantic chunk/commit boundary under test, never a fixed sleep
//!   guess and never a self-inflicted `abort()`.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Arc;

use oxide_batch::{
    BoxFuture, BusinessTransaction, ChunkCommitReceipt, ChunkCounts, ChunkFaultProgress,
    ChunkTransaction, ChunkTransactionContext, ChunkTransactionError, ChunkTransactionManager,
    ComponentStateEnvelope, FailureCategory, InheritedStepProgress, ItemWriter, WriteContext,
    WriteOutcome, WriterError,
};

/// Which point in one chunk's lifecycle a targeted chunk ordinal fires at.
#[derive(Clone, Copy, Debug, PartialEq, Eq, clap::ValueEnum)]
pub enum FailureMode {
    /// After the writer's real `INSERT` executes inside the still-open
    /// enlisted transaction, but before the chunk transaction commits: the
    /// business rows exist only in an uncommitted transaction. Used for
    /// both the typed pre-commit rollback proof and the hard-crash-before-
    /// commit proof.
    #[value(name = "during-write")]
    DuringWrite,
    /// After the chunk transaction's commit call returns success -- the
    /// business rows and checkpoint are already durable. Used for the
    /// hard-crash-immediately-after-commit proof.
    #[value(name = "after-business-commit")]
    AfterBusinessCommit,
}

/// What a firing failpoint does. See the module documentation.
#[derive(Clone, Debug)]
pub enum FailAction {
    TypedError,
    PauseForKill(PathBuf),
}

/// Either pauses this process for an external kill, or does nothing (the
/// call site returns its own typed error afterward).
async fn fire(action: &FailAction, where_: &str) {
    match action {
        FailAction::PauseForKill(marker_path) => {
            let pid = std::process::id();
            // Diagnostic evidence for the parent test harness: it already
            // knows this child's PID from `Child::id()`, but persisting it
            // here too means the marker file is independently self-
            // describing proof of exactly which process paused, not just a
            // presence flag. A failure to write it leaves the harness
            // waiting forever with no way to proceed, so this deliberately
            // aborts loudly rather than silently pausing without ever
            // signaling readiness -- this whole branch only runs when a
            // caller has explicitly armed `--pause-for-kill`, never on the
            // production golden path.
            if let Err(error) = std::fs::write(marker_path, format!("{pid}\n{where_}\n")) {
                tracing::error!(%error, marker_path = %marker_path.display(), "failpoint: failed to write pause marker file");
                std::process::abort();
            }
            tracing::error!(
                where_,
                pid,
                marker_path = %marker_path.display(),
                "failpoint firing: paused, awaiting external kill"
            );
            std::future::pending::<()>().await;
            unreachable!("this process must be killed externally before pending() resolves");
        }
        FailAction::TypedError => {
            tracing::warn!(where_, "failpoint firing: typed graceful error");
        }
    }
}

// ------------------------------------------------------------------- write --

/// Wraps a writer so the targeted chunk's write call fires `DuringWrite`
/// after the inner writer's real `INSERT` executes, before this call
/// returns to the chunk runtime's own commit.
pub struct FailingWriter<W> {
    inner: W,
    chunk_ordinal: Arc<AtomicU32>,
    fail_at_chunk: u32,
    mode: FailureMode,
    action: FailAction,
    fired: Arc<AtomicBool>,
}

impl<W> FailingWriter<W> {
    pub fn new(
        inner: W,
        chunk_ordinal: Arc<AtomicU32>,
        fail_at_chunk: u32,
        mode: FailureMode,
        action: FailAction,
        fired: Arc<AtomicBool>,
    ) -> Self {
        Self {
            inner,
            chunk_ordinal,
            fail_at_chunk,
            mode,
            action,
            fired,
        }
    }
}

impl<I: Send + Sync, W: ItemWriter<I>> ItemWriter<I> for FailingWriter<W> {
    async fn write<'a>(
        &'a self,
        items: &'a [I],
        context: WriteContext<'a>,
    ) -> Result<WriteOutcome, WriterError> {
        let outcome = self.inner.write(items, context).await?;
        let targeted = self.fail_at_chunk != 0
            && self.chunk_ordinal.load(Ordering::SeqCst) == self.fail_at_chunk;
        if targeted && self.mode == FailureMode::DuringWrite {
            self.fired.store(true, Ordering::SeqCst);
            fire(&self.action, "writer during-write").await;
            return Err(WriterError::with_category(
                FailureCategory::TransientInfrastructure,
            ));
        }
        Ok(outcome)
    }
}

// ------------------------------------------------------------- transaction --

async fn fire_after_commit(
    action: &FailAction,
    fired: &AtomicBool,
) -> Result<(), ChunkTransactionError> {
    fired.store(true, Ordering::SeqCst);
    fire(action, "after-business-commit").await;
    Err(ChunkTransactionError::CommitOutcomeUnknown)
}

struct FailingChunkTransaction<'a> {
    inner: Box<dyn ChunkTransaction + 'a>,
    should_fire: bool,
    action: FailAction,
    fired: Arc<AtomicBool>,
}

impl ChunkTransaction for FailingChunkTransaction<'_> {
    fn business_transaction(&mut self) -> Option<&mut dyn BusinessTransaction> {
        self.inner.business_transaction()
    }

    fn commit(
        &mut self,
        counts: ChunkCounts,
        fault: ChunkFaultProgress,
    ) -> BoxFuture<'_, Result<ChunkCommitReceipt, ChunkTransactionError>> {
        let should_fire = self.should_fire;
        let action = self.action.clone();
        let fired = Arc::clone(&self.fired);
        let inner = &mut self.inner;
        Box::pin(async move {
            let receipt = inner.commit(counts, fault).await?;
            if should_fire {
                fire_after_commit(&action, &fired).await?;
            }
            Ok(receipt)
        })
    }

    fn commit_with_component_state<'a>(
        &'a mut self,
        counts: ChunkCounts,
        fault: ChunkFaultProgress,
        component_state: &'a [ComponentStateEnvelope],
    ) -> BoxFuture<'a, Result<ChunkCommitReceipt, ChunkTransactionError>> {
        let should_fire = self.should_fire;
        let action = self.action.clone();
        let fired = Arc::clone(&self.fired);
        let inner = &mut self.inner;
        Box::pin(async move {
            let receipt = inner
                .commit_with_component_state(counts, fault, component_state)
                .await?;
            if should_fire {
                fire_after_commit(&action, &fired).await?;
            }
            Ok(receipt)
        })
    }

    fn rollback(&mut self) -> BoxFuture<'_, Result<(), ChunkTransactionError>> {
        self.inner.rollback()
    }
}

/// Wraps a `ChunkTransactionManager` so the targeted chunk's transaction --
/// which the real production launch path reaches through `begin_for`, never
/// bare `begin` -- fires `AfterBusinessCommit` only once the wrapped commit
/// has genuinely already succeeded. Also advances `chunk_ordinal` once per
/// chunk transaction attempt, which [`FailingWriter`] reads (never
/// increments) to decide whether the chunk it is about to write is the
/// targeted one -- `begin_for` always runs before `write` for the same
/// chunk in the real production launch path.
pub struct FailingTransactionManager<M> {
    inner: M,
    chunk_ordinal: Arc<AtomicU32>,
    fail_at_chunk: u32,
    fire_after_commit: bool,
    action: FailAction,
    fired: Arc<AtomicBool>,
}

impl<M> FailingTransactionManager<M> {
    pub fn new(
        inner: M,
        chunk_ordinal: Arc<AtomicU32>,
        fail_at_chunk: u32,
        fire_after_commit: bool,
        action: FailAction,
        fired: Arc<AtomicBool>,
    ) -> Self {
        Self {
            inner,
            chunk_ordinal,
            fail_at_chunk,
            fire_after_commit,
            action,
            fired,
        }
    }
}

impl<M: ChunkTransactionManager> FailingTransactionManager<M> {
    fn wrap<'a>(
        &'a self,
        inner_future: BoxFuture<'a, Result<Box<dyn ChunkTransaction + 'a>, ChunkTransactionError>>,
    ) -> BoxFuture<'a, Result<Box<dyn ChunkTransaction + 'a>, ChunkTransactionError>> {
        let ordinal = self.chunk_ordinal.fetch_add(1, Ordering::SeqCst) + 1;
        let should_fire =
            self.fire_after_commit && self.fail_at_chunk != 0 && ordinal == self.fail_at_chunk;
        let action = self.action.clone();
        let fired = Arc::clone(&self.fired);
        Box::pin(async move {
            let inner_txn = inner_future.await?;
            Ok(Box::new(FailingChunkTransaction {
                inner: inner_txn,
                should_fire,
                action,
                fired,
            }) as Box<dyn ChunkTransaction + 'a>)
        })
    }
}

impl<M: ChunkTransactionManager> ChunkTransactionManager for FailingTransactionManager<M> {
    fn begin(
        &self,
    ) -> BoxFuture<'_, Result<Box<dyn ChunkTransaction + '_>, ChunkTransactionError>> {
        self.wrap(self.inner.begin())
    }

    fn begin_for(
        &self,
        context: ChunkTransactionContext,
    ) -> BoxFuture<'_, Result<Box<dyn ChunkTransaction + '_>, ChunkTransactionError>> {
        self.wrap(self.inner.begin_for(context))
    }

    fn inherited_progress(
        &self,
        context: ChunkTransactionContext,
    ) -> BoxFuture<'_, Result<InheritedStepProgress, ChunkTransactionError>> {
        self.inner.inherited_progress(context)
    }

    fn inherited_component_state(
        &self,
        context: ChunkTransactionContext,
    ) -> BoxFuture<'_, Result<Vec<ComponentStateEnvelope>, ChunkTransactionError>> {
        self.inner.inherited_component_state(context)
    }
}
