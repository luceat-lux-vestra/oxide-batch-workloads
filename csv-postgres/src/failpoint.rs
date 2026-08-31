//! Deterministic, reproducible fault injection built into the shipped
//! binary itself (`--fail-at chunk:N|row:N --failure-mode ... [--hard-crash]`
//! on the `run` command), not just the test suite.
//!
//! `oxide-batch-test`'s injection types (`InjectedReader`,
//! `InjectedTransactions`, ...) are a dev-dependency and cannot ship in a
//! production binary, so these decorators are necessary glue -- but they are
//! built on exactly the same public traits a real consumer implements
//! against (`ItemReader`, `ItemWriter`, `ChunkTransactionManager`), never a
//! reimplementation of framework internals.
//!
//! `ChunkTransactionManager`'s durable adapters (see `PostgresChunkTransactionManager`)
//! reject an unbound `begin()` and are driven through `begin_for` by the real
//! repository-backed launch path, and `commit_with_component_state` -- not
//! `commit` -- is what actually runs once an `ItemStream` (our CSV reader's
//! restart position) is registered. Both wrappers below override every one
//! of those methods explicitly rather than relying on the trait's
//! standalone-friendly defaults, which would otherwise silently no-op this
//! injection against the real launch path.

use std::str::FromStr;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::Arc;

use oxide_batch::{
    BoxFuture, BusinessTransaction, ChunkCommitReceipt, ChunkCounts, ChunkFaultProgress,
    ChunkTransaction, ChunkTransactionContext, ChunkTransactionError, ChunkTransactionManager,
    ComponentStateEnvelope, FailureCategory, InheritedStepProgress, ItemReader, ItemWriter,
    ReadContext, ReadOutcome, ReaderError, WriteContext, WriteOutcome, WriterError,
};

/// A `--fail-at` target: either a chunk-attempt ordinal or a source-row
/// ordinal.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FailAt {
    /// 1-based ordinal of the chunk transaction attempt to fail.
    Chunk(u32),
    /// 1-based ordinal of the source row (as read) to fail at.
    Row(u64),
}

impl FailAt {
    /// Parses `"chunk:N"` or `"row:N"`.
    ///
    /// # Errors
    ///
    /// Returns an error if `value` is not one of those two shapes.
    pub fn parse(value: &str) -> anyhow::Result<Self> {
        let (kind, number) = value.split_once(':').ok_or_else(|| {
            anyhow::anyhow!("--fail-at must be 'chunk:N' or 'row:N', got '{value}'")
        })?;
        let number: u64 = number.parse().map_err(|_| {
            anyhow::anyhow!("--fail-at position must be a positive integer, got '{number}'")
        })?;
        match kind {
            "chunk" => Ok(Self::Chunk(u32::try_from(number)?)),
            "row" => Ok(Self::Row(number)),
            other => Err(anyhow::anyhow!(
                "unknown --fail-at kind '{other}' (expected chunk|row)"
            )),
        }
    }
}

/// Which point in one chunk's lifecycle a `chunk:N` target fires at.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FailureMode {
    /// Before the writer sends anything to `PostgreSQL` for this chunk
    /// (F2: the DB write never starts).
    BeforeWrite,
    /// After the writer's INSERT executes inside the still-open enlisted
    /// transaction, but before the chunk transaction commits
    /// (F3: business rows exist only in an uncommitted transaction).
    DuringWrite,
    /// After the chunk transaction's commit call returns success -- the
    /// business rows and checkpoint are already durable
    /// (F4/F6: the highest-stakes crash window).
    AfterBusinessCommit,
}

impl FromStr for FailureMode {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "before-write" => Ok(Self::BeforeWrite),
            "during-write" => Ok(Self::DuringWrite),
            "after-business-commit" => Ok(Self::AfterBusinessCommit),
            other => Err(format!(
                "unknown failure mode '{other}' (expected before-write|during-write|after-business-commit)"
            )),
        }
    }
}

/// Either aborts the process (never returns) or does nothing, depending on
/// `hard_crash`. Call sites return their own typed graceful error afterward.
fn maybe_abort(hard_crash: bool, where_: &str) {
    if hard_crash {
        tracing::error!(where_, "failpoint firing: hard process abort");
        std::process::abort();
    }
    tracing::warn!(where_, "failpoint firing: graceful typed error");
}

// --------------------------------------------------------------------- row --

/// Wraps a reader so the Nth item it successfully reads never reaches the
/// pipeline: either the process aborts, or a typed `ReaderError` is
/// returned before the item is yielded.
pub struct FailingReader<R> {
    inner: R,
    row_ordinal: AtomicU64,
    fail_at_row: u64,
    hard_crash: bool,
    fired: Arc<AtomicBool>,
}

impl<R> FailingReader<R> {
    pub fn new(inner: R, fail_at_row: u64, hard_crash: bool, fired: Arc<AtomicBool>) -> Self {
        Self {
            inner,
            row_ordinal: AtomicU64::new(0),
            fail_at_row,
            hard_crash,
            fired,
        }
    }
}

impl<I: Send + Sync + 'static, R: ItemReader<I>> ItemReader<I> for FailingReader<R> {
    async fn read(&mut self, context: ReadContext<'_>) -> Result<ReadOutcome<I>, ReaderError> {
        let outcome = self.inner.read(context).await?;
        if matches!(outcome, ReadOutcome::Item(_)) {
            let ordinal = self.row_ordinal.fetch_add(1, Ordering::SeqCst) + 1;
            if ordinal == self.fail_at_row {
                self.fired.store(true, Ordering::SeqCst);
                maybe_abort(self.hard_crash, "reader row target");
                return Err(ReaderError::with_category(FailureCategory::UserComponent));
            }
        }
        Ok(outcome)
    }
}

// ------------------------------------------------------------------- write --

/// Wraps a writer so the Nth chunk's write call fires `BeforeWrite`
/// (before delegating at all) or `DuringWrite` (after the inner writer's
/// real DB statement executes, before this call returns to the chunk
/// runtime's own commit).
pub struct FailingWriter<W> {
    inner: W,
    chunk_ordinal: Arc<AtomicU32>,
    fail_at_chunk: u32,
    mode: FailureMode,
    hard_crash: bool,
    fired: Arc<AtomicBool>,
}

impl<W> FailingWriter<W> {
    pub fn new(
        inner: W,
        chunk_ordinal: Arc<AtomicU32>,
        fail_at_chunk: u32,
        mode: FailureMode,
        hard_crash: bool,
        fired: Arc<AtomicBool>,
    ) -> Self {
        Self {
            inner,
            chunk_ordinal,
            fail_at_chunk,
            mode,
            hard_crash,
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
        let ordinal = self.chunk_ordinal.load(Ordering::SeqCst);
        let targeted = ordinal == self.fail_at_chunk;
        if targeted && self.mode == FailureMode::BeforeWrite {
            self.fired.store(true, Ordering::SeqCst);
            maybe_abort(self.hard_crash, "writer before-write");
            return Err(WriterError::with_category(
                FailureCategory::TransientInfrastructure,
            ));
        }
        let outcome = self.inner.write(items, context).await?;
        if targeted && self.mode == FailureMode::DuringWrite {
            self.fired.store(true, Ordering::SeqCst);
            maybe_abort(self.hard_crash, "writer during-write");
            return Err(WriterError::with_category(
                FailureCategory::TransientInfrastructure,
            ));
        }
        Ok(outcome)
    }
}

// ------------------------------------------------------------- transaction --

fn fire_after_commit(hard_crash: bool, fired: &AtomicBool) -> Result<(), ChunkTransactionError> {
    fired.store(true, Ordering::SeqCst);
    maybe_abort(hard_crash, "after-business-commit");
    Err(ChunkTransactionError::CommitOutcomeUnknown)
}

struct FailingChunkTransaction<'a> {
    inner: Box<dyn ChunkTransaction + 'a>,
    should_fire: bool,
    hard_crash: bool,
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
        let hard_crash = self.hard_crash;
        let fired = Arc::clone(&self.fired);
        let inner = &mut self.inner;
        Box::pin(async move {
            let receipt = inner.commit(counts, fault).await?;
            if should_fire {
                fire_after_commit(hard_crash, &fired)?;
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
        let hard_crash = self.hard_crash;
        let fired = Arc::clone(&self.fired);
        let inner = &mut self.inner;
        Box::pin(async move {
            let receipt = inner
                .commit_with_component_state(counts, fault, component_state)
                .await?;
            if should_fire {
                fire_after_commit(hard_crash, &fired)?;
            }
            Ok(receipt)
        })
    }

    fn rollback(&mut self) -> BoxFuture<'_, Result<(), ChunkTransactionError>> {
        self.inner.rollback()
    }
}

/// Wraps a `ChunkTransactionManager` so the Nth chunk transaction's commit
/// -- which the real production launch path reaches through `begin_for`,
/// never bare `begin` -- fires `AfterBusinessCommit` only once the wrapped
/// commit has genuinely already succeeded.
pub struct FailingTransactionManager<M> {
    inner: M,
    chunk_ordinal: Arc<AtomicU32>,
    fail_at_chunk: u32,
    fire_after_commit: bool,
    hard_crash: bool,
    fired: Arc<AtomicBool>,
}

impl<M> FailingTransactionManager<M> {
    pub fn new(
        inner: M,
        chunk_ordinal: Arc<AtomicU32>,
        fail_at_chunk: u32,
        fire_after_commit: bool,
        hard_crash: bool,
        fired: Arc<AtomicBool>,
    ) -> Self {
        Self {
            inner,
            chunk_ordinal,
            fail_at_chunk,
            fire_after_commit,
            hard_crash,
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
        let should_fire = self.fire_after_commit && ordinal == self.fail_at_chunk;
        let hard_crash = self.hard_crash;
        let fired = Arc::clone(&self.fired);
        Box::pin(async move {
            let inner_txn = inner_future.await?;
            Ok(Box::new(FailingChunkTransaction {
                inner: inner_txn,
                should_fire,
                hard_crash,
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
