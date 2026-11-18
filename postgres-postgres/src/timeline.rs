//! Opt-in phase/lifecycle timeline evidence for campaign #266.
//!
//! The recorder uses only public OxideBatch 0.7.0 listener and lifecycle
//! hooks. Normal workload execution is unchanged unless
//! OXIDE_BATCH_TIMELINE_DIR is set. Callbacks retain bounded, redacted
//! in-memory records; filesystem I/O occurs only when the run flushes the
//! completed timeline.

use std::fs::{self, File};
use std::io::{BufWriter, Write};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Instant;

use anyhow::Context;
use oxide_batch::{
    BoxFuture, ItemListenerContext, LifecycleEvent, LifecycleEventSink, ListenerError,
    ProcessListener, ReadListener, WriteListener,
};
use serde::Serialize;

use crate::processor::{ProjectedRow, SourceRow};

pub const TIMELINE_DIR_ENV: &str = "OXIDE_BATCH_TIMELINE_DIR";
pub const TIMELINE_SCHEMA: &str = "oxide-batch-workload-timeline/v1";
pub const TIMELINE_SUMMARY_SCHEMA: &str = "oxide-batch-workload-timeline-summary/v1";
pub const MAX_TIMELINE_RECORDS: usize = 50_000;

#[derive(Clone, Debug, Serialize)]
pub struct TimelineRecord {
    schema: &'static str,
    sequence: u64,
    elapsed_ns: u64,
    source: &'static str,
    phase: String,
    transition: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    chunk_sequence: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    item_count: Option<u64>,
}

#[derive(Debug, Default)]
struct TimelineState {
    next_sequence: u64,
    dropped_records: u64,
    records: Vec<TimelineRecord>,
}

#[derive(Debug, Serialize)]
struct TimelineSummary {
    schema: &'static str,
    framework_telemetry_schema_version: u16,
    clock: &'static str,
    claim: &'static str,
    max_records: usize,
    observed_records: u64,
    retained_records: usize,
    dropped_records: u64,
    limitations: Vec<&'static str>,
}

/// Bounded, non-authoritative campaign timeline recorder.
#[derive(Debug)]
pub struct TimelineRecorder {
    started: Instant,
    output_dir: PathBuf,
    state: Mutex<TimelineState>,
}

impl TimelineRecorder {
    #[must_use]
    pub fn new(output_dir: PathBuf) -> Self {
        Self {
            started: Instant::now(),
            output_dir,
            state: Mutex::new(TimelineState {
                records: Vec::with_capacity(MAX_TIMELINE_RECORDS.min(8_192)),
                ..TimelineState::default()
            }),
        }
    }

    /// Creates a recorder only when the campaign-specific environment
    /// variable is present.
    pub fn from_env() -> anyhow::Result<Option<std::sync::Arc<Self>>> {
        let Some(value) = std::env::var_os(TIMELINE_DIR_ENV) else {
            return Ok(None);
        };
        if value.is_empty() {
            anyhow::bail!("{TIMELINE_DIR_ENV} must not be empty when set");
        }
        Ok(Some(std::sync::Arc::new(Self::new(PathBuf::from(value)))))
    }

    fn elapsed_ns(&self) -> u64 {
        u64::try_from(self.started.elapsed().as_nanos()).unwrap_or(u64::MAX)
    }

    fn record(
        &self,
        source: &'static str,
        phase: impl Into<String>,
        transition: &'static str,
        chunk_sequence: Option<u64>,
        item_count: Option<u64>,
    ) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let sequence = state.next_sequence;
        state.next_sequence = state.next_sequence.saturating_add(1);

        if state.records.len() >= MAX_TIMELINE_RECORDS {
            state.dropped_records = state.dropped_records.saturating_add(1);
            return;
        }

        state.records.push(TimelineRecord {
            schema: TIMELINE_SCHEMA,
            sequence,
            elapsed_ns: self.elapsed_ns(),
            source,
            phase: phase.into(),
            transition,
            chunk_sequence,
            item_count,
        });
    }

    /// Writes retained raw events plus a compact contract summary.
    ///
    /// This is intentionally called only by the opt-in profiling path after
    /// the framework launch has returned. A write failure therefore cannot
    /// alter the already-committed batch transaction, but it does fail the
    /// profiling command so incomplete evidence is never accepted silently.
    pub fn flush(&self) -> anyhow::Result<()> {
        fs::create_dir_all(&self.output_dir)
            .with_context(|| format!("create timeline directory {}", self.output_dir.display()))?;

        let state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);

        let events_path = self.output_dir.join("events.jsonl");
        let events_file = File::create(&events_path)
            .with_context(|| format!("create {}", events_path.display()))?;
        let mut events = BufWriter::new(events_file);
        for record in &state.records {
            serde_json::to_writer(&mut events, record)
                .with_context(|| format!("serialize {}", events_path.display()))?;
            events
                .write_all(b"\n")
                .with_context(|| format!("write {}", events_path.display()))?;
        }
        events
            .flush()
            .with_context(|| format!("flush {}", events_path.display()))?;

        let summary = TimelineSummary {
            schema: TIMELINE_SUMMARY_SCHEMA,
            framework_telemetry_schema_version: oxide_batch::TELEMETRY_SCHEMA_VERSION,
            clock: "process-monotonic-std-instant",
            claim: "phase-boundary-attribution-only",
            max_records: MAX_TIMELINE_RECORDS,
            observed_records: state.next_sequence,
            retained_records: state.records.len(),
            dropped_records: state.dropped_records,
            limitations: vec![
                "listener timestamps include listener dispatch overhead and are not benchmark timing",
                "item.read and item.write are database-facing operation boundaries, not isolated database-wait measurements",
                "OxideBatch 0.7.0 exposes checkpoint durability through the same chunk commit boundary; no separate checkpoint duration is invented",
                "the postgres-postgres workload uses deterministic single-threaded chunk orchestration, so queue/backpressure and worker-concurrency claims are not inferred",
            ],
        };
        let summary_path = self.output_dir.join("summary.json");
        let summary_bytes =
            serde_json::to_vec_pretty(&summary).context("serialize timeline summary")?;
        fs::write(&summary_path, summary_bytes)
            .with_context(|| format!("write {}", summary_path.display()))?;
        Ok(())
    }

    #[cfg(test)]
    fn snapshot(&self) -> (u64, u64, Vec<TimelineRecord>) {
        let state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        (
            state.next_sequence,
            state.dropped_records,
            state.records.clone(),
        )
    }
}

impl LifecycleEventSink for TimelineRecorder {
    fn emit(&self, event: &LifecycleEvent) {
        self.record(
            "lifecycle",
            event.kind().telemetry_kind().as_str(),
            "event",
            event.chunk_sequence().map(|value| value.get()),
            None,
        );
    }
}

impl ReadListener<SourceRow> for TimelineRecorder {
    fn before_read<'a>(
        &'a self,
        context: ItemListenerContext<'a>,
    ) -> BoxFuture<'a, Result<(), ListenerError>> {
        self.record(
            "item-listener",
            "item.read",
            "before",
            Some(context.chunk_sequence().get()),
            None,
        );
        Box::pin(async { Ok(()) })
    }

    fn after_read<'a>(
        &'a self,
        _item: &'a SourceRow,
        context: ItemListenerContext<'a>,
    ) -> BoxFuture<'a, Result<(), ListenerError>> {
        self.record(
            "item-listener",
            "item.read",
            "after",
            Some(context.chunk_sequence().get()),
            Some(1),
        );
        Box::pin(async { Ok(()) })
    }

    fn on_read_error<'a>(
        &'a self,
        _fault: oxide_batch::FaultDescriptor,
        context: ItemListenerContext<'a>,
    ) -> BoxFuture<'a, Result<(), ListenerError>> {
        self.record(
            "item-listener",
            "item.read",
            "error",
            Some(context.chunk_sequence().get()),
            None,
        );
        Box::pin(async { Ok(()) })
    }
}

impl ProcessListener<SourceRow, ProjectedRow> for TimelineRecorder {
    fn before_process<'a>(
        &'a self,
        _input: &'a SourceRow,
        context: ItemListenerContext<'a>,
    ) -> BoxFuture<'a, Result<(), ListenerError>> {
        self.record(
            "item-listener",
            "item.process",
            "before",
            Some(context.chunk_sequence().get()),
            Some(1),
        );
        Box::pin(async { Ok(()) })
    }

    fn after_process<'a>(
        &'a self,
        _input: &'a SourceRow,
        _output: Option<&'a ProjectedRow>,
        context: ItemListenerContext<'a>,
    ) -> BoxFuture<'a, Result<(), ListenerError>> {
        self.record(
            "item-listener",
            "item.process",
            "after",
            Some(context.chunk_sequence().get()),
            Some(1),
        );
        Box::pin(async { Ok(()) })
    }

    fn on_process_error<'a>(
        &'a self,
        _input: &'a SourceRow,
        _fault: oxide_batch::FaultDescriptor,
        context: ItemListenerContext<'a>,
    ) -> BoxFuture<'a, Result<(), ListenerError>> {
        self.record(
            "item-listener",
            "item.process",
            "error",
            Some(context.chunk_sequence().get()),
            Some(1),
        );
        Box::pin(async { Ok(()) })
    }
}

impl WriteListener<ProjectedRow> for TimelineRecorder {
    fn before_write<'a>(
        &'a self,
        outputs: &'a [ProjectedRow],
        context: ItemListenerContext<'a>,
    ) -> BoxFuture<'a, Result<(), ListenerError>> {
        self.record(
            "item-listener",
            "item.write",
            "before",
            Some(context.chunk_sequence().get()),
            Some(u64::try_from(outputs.len()).unwrap_or(u64::MAX)),
        );
        Box::pin(async { Ok(()) })
    }

    fn after_write<'a>(
        &'a self,
        outputs: &'a [ProjectedRow],
        context: ItemListenerContext<'a>,
    ) -> BoxFuture<'a, Result<(), ListenerError>> {
        self.record(
            "item-listener",
            "item.write",
            "after",
            Some(context.chunk_sequence().get()),
            Some(u64::try_from(outputs.len()).unwrap_or(u64::MAX)),
        );
        Box::pin(async { Ok(()) })
    }

    fn on_write_error<'a>(
        &'a self,
        outputs: &'a [ProjectedRow],
        _fault: oxide_batch::FaultDescriptor,
        context: ItemListenerContext<'a>,
    ) -> BoxFuture<'a, Result<(), ListenerError>> {
        self.record(
            "item-listener",
            "item.write",
            "error",
            Some(context.chunk_sequence().get()),
            Some(u64::try_from(outputs.len()).unwrap_or(u64::MAX)),
        );
        Box::pin(async { Ok(()) })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recorder_is_bounded_and_sequences_observations_monotonically() {
        let recorder = TimelineRecorder::new(PathBuf::from("unused"));
        for index in 0..(MAX_TIMELINE_RECORDS + 3) {
            recorder.record(
                "test",
                "item.process",
                "after",
                Some(u64::try_from(index).expect("fixture index fits u64")),
                Some(1),
            );
        }

        let (observed, dropped, records) = recorder.snapshot();
        assert_eq!(
            observed,
            u64::try_from(MAX_TIMELINE_RECORDS + 3).expect("bound fits u64")
        );
        assert_eq!(dropped, 3);
        assert_eq!(records.len(), MAX_TIMELINE_RECORDS);
        assert_eq!(records.first().expect("first record").sequence, 0);
        assert_eq!(
            records.last().expect("last record").sequence,
            u64::try_from(MAX_TIMELINE_RECORDS - 1).expect("bound fits u64")
        );
    }

    #[test]
    fn flush_writes_jsonl_and_fail_closed_summary_metadata() {
        let unique = format!(
            "oxide-batch-timeline-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("system clock after epoch")
                .as_nanos()
        );
        let output = std::env::temp_dir().join(unique);
        let recorder = TimelineRecorder::new(output.clone());
        recorder.record("test", "chunk.started", "event", Some(0), None);
        recorder.record("test", "item.read", "before", Some(0), None);
        recorder.record("test", "item.read", "after", Some(0), Some(1));

        recorder.flush().expect("timeline flush succeeds");

        let events = fs::read_to_string(output.join("events.jsonl")).expect("events jsonl exists");
        let lines = events.lines().collect::<Vec<_>>();
        assert_eq!(lines.len(), 3);
        let first: serde_json::Value = serde_json::from_str(lines[0]).expect("event is valid json");
        assert_eq!(first["schema"], TIMELINE_SCHEMA);
        assert_eq!(first["sequence"], 0);

        let summary: serde_json::Value =
            serde_json::from_slice(&fs::read(output.join("summary.json")).expect("summary exists"))
                .expect("summary is valid json");
        assert_eq!(summary["schema"], TIMELINE_SUMMARY_SCHEMA);
        assert_eq!(summary["framework_telemetry_schema_version"], 1);
        assert_eq!(summary["observed_records"], 3);
        assert_eq!(summary["retained_records"], 3);
        assert_eq!(summary["dropped_records"], 0);
        assert_eq!(summary["claim"], "phase-boundary-attribution-only");

        fs::remove_dir_all(&output).expect("cleanup timeline fixture");
    }

    #[test]
    fn output_path_is_not_created_until_flush() {
        let output = std::path::Path::new("definitely-not-created-by-constructor");
        let recorder = TimelineRecorder::new(output.to_path_buf());
        assert_eq!(recorder.output_dir, output);
    }
}
