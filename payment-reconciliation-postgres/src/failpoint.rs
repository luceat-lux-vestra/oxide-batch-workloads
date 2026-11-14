use std::path::PathBuf;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;

use oxide_batch::{
    BoxFuture, BusinessTransaction, ChunkCommitReceipt, ChunkCounts, ChunkFaultProgress,
    ChunkTransaction, ChunkTransactionContext, ChunkTransactionError, ChunkTransactionManager,
    ComponentStateEnvelope, FailureCategory, InheritedStepProgress, ItemWriter, WriteContext,
    WriteOutcome, WriterError,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq, clap::ValueEnum)]
pub enum FailureMode {
    #[value(name = "during-write")]
    DuringWrite,
    #[value(name = "after-business-commit")]
    AfterBusinessCommit,
}

#[derive(Clone, Debug)]
pub struct Failpoint {
    target_chunk: u32,
    mode: FailureMode,
    marker: Option<PathBuf>,
}

impl Failpoint {
    pub fn disabled() -> Self {
        Self {
            target_chunk: 0,
            mode: FailureMode::DuringWrite,
            marker: None,
        }
    }

    pub fn armed(target_chunk: u32, mode: FailureMode, marker: PathBuf) -> anyhow::Result<Self> {
        if target_chunk == 0 {
            anyhow::bail!("failpoint target chunk must be >= 1");
        }
        Ok(Self {
            target_chunk,
            mode,
            marker: Some(marker),
        })
    }

    fn should_fire(&self, ordinal: u32, mode: FailureMode) -> bool {
        self.target_chunk != 0 && self.target_chunk == ordinal && self.mode == mode
    }

    async fn pause(&self, where_: &str) {
        if let Some(marker) = &self.marker {
            pause_for_external_kill(marker, where_).await;
        }
    }
}

pub struct FailingWriter<W> {
    inner: W,
    ordinal: Arc<AtomicU32>,
    failpoint: Failpoint,
}

impl<W> FailingWriter<W> {
    pub fn new(inner: W, ordinal: Arc<AtomicU32>, failpoint: Failpoint) -> Self {
        Self {
            inner,
            ordinal,
            failpoint,
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
        let ordinal = self.ordinal.load(Ordering::SeqCst);
        if self.failpoint.should_fire(ordinal, FailureMode::DuringWrite) {
            self.failpoint.pause("chunk-during-write").await;
            return Err(WriterError::with_category(
                FailureCategory::TransientInfrastructure,
            ));
        }
        Ok(outcome)
    }
}

struct FailingChunkTransaction<'a> {
    inner: Box<dyn ChunkTransaction + 'a>,
    should_pause_after_commit: bool,
    failpoint: Failpoint,
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
        let should_pause = self.should_pause_after_commit;
        let failpoint = self.failpoint.clone();
        let inner = &mut self.inner;
        Box::pin(async move {
            let receipt = inner.commit(counts, fault).await?;
            if should_pause {
                failpoint.pause("chunk-after-business-commit").await;
                return Err(ChunkTransactionError::CommitOutcomeUnknown);
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
        let should_pause = self.should_pause_after_commit;
        let failpoint = self.failpoint.clone();
        let inner = &mut self.inner;
        Box::pin(async move {
            let receipt = inner
                .commit_with_component_state(counts, fault, component_state)
                .await?;
            if should_pause {
                failpoint.pause("chunk-after-business-commit").await;
                return Err(ChunkTransactionError::CommitOutcomeUnknown);
            }
            Ok(receipt)
        })
    }

    fn rollback(&mut self) -> BoxFuture<'_, Result<(), ChunkTransactionError>> {
        self.inner.rollback()
    }
}

pub struct FailingTransactionManager<M> {
    inner: M,
    ordinal: Arc<AtomicU32>,
    failpoint: Failpoint,
}

impl<M> FailingTransactionManager<M> {
    pub fn new(inner: M, ordinal: Arc<AtomicU32>, failpoint: Failpoint) -> Self {
        Self {
            inner,
            ordinal,
            failpoint,
        }
    }
}

impl<M: ChunkTransactionManager> FailingTransactionManager<M> {
    fn wrap<'a>(
        &'a self,
        future: BoxFuture<'a, Result<Box<dyn ChunkTransaction + 'a>, ChunkTransactionError>>,
    ) -> BoxFuture<'a, Result<Box<dyn ChunkTransaction + 'a>, ChunkTransactionError>> {
        let ordinal = self.ordinal.fetch_add(1, Ordering::SeqCst) + 1;
        let should_pause_after_commit = self
            .failpoint
            .should_fire(ordinal, FailureMode::AfterBusinessCommit);
        let failpoint = self.failpoint.clone();
        Box::pin(async move {
            let inner = future.await?;
            Ok(Box::new(FailingChunkTransaction {
                inner,
                should_pause_after_commit,
                failpoint,
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

pub async fn pause_for_external_kill(marker: &PathBuf, where_: &str) {
    let pid = std::process::id();
    if let Err(error) = std::fs::write(marker, format!("{pid}\n{where_}\n")) {
        eprintln!(
            "failpoint could not write marker {}: {error}",
            marker.display()
        );
        std::process::abort();
    }
    std::future::pending::<()>().await;
    unreachable!("armed failpoint must be externally killed");
}
