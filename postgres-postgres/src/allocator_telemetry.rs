//! Opt-in allocator-native telemetry for campaign #266.
//!
//! Lane A allocator builds remain unchanged. Native statistics are compiled
//! only behind allocator-specific telemetry features and are emitted only when
//! OXIDE_BATCH_ALLOCATOR_TELEMETRY_DIR is set on a successful workload run.

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::Context;
use serde::Serialize;
use serde_json::Value;

pub const TELEMETRY_DIR_ENV: &str = "OXIDE_BATCH_ALLOCATOR_TELEMETRY_DIR";
pub const TELEMETRY_SCHEMA: &str = "oxide-batch-allocator-native-telemetry/v1";

#[derive(Debug, Serialize)]
struct AllocatorTelemetrySnapshot {
    schema: &'static str,
    allocator: &'static str,
    capture: &'static str,
    native_api: &'static str,
    native_payload: Value,
    limitations: Vec<&'static str>,
}

/// Persist one post-run native allocator snapshot when explicitly requested.
///
/// The workload transaction has already completed when this is called. A
/// telemetry failure therefore fails the profiling command but cannot rewrite
/// committed workload state.
pub fn capture_from_env() -> anyhow::Result<()> {
    let Some(value) = std::env::var_os(TELEMETRY_DIR_ENV) else {
        return Ok(());
    };
    if value.is_empty() {
        anyhow::bail!("{TELEMETRY_DIR_ENV} must not be empty when set");
    }
    capture_to(&PathBuf::from(value))
}

fn capture_to(output_dir: &Path) -> anyhow::Result<()> {
    let snapshot = selected_snapshot()?;
    fs::create_dir_all(output_dir).with_context(|| {
        format!(
            "create allocator telemetry directory {}",
            output_dir.display()
        )
    })?;
    let output = output_dir.join("snapshot.json");
    let bytes =
        serde_json::to_vec_pretty(&snapshot).context("serialize allocator telemetry snapshot")?;
    fs::write(&output, bytes).with_context(|| format!("write {}", output.display()))
}

#[cfg(feature = "allocator-jemalloc-telemetry")]
fn selected_snapshot() -> anyhow::Result<AllocatorTelemetrySnapshot> {
    refresh_jemalloc_epoch()?;
    Ok(AllocatorTelemetrySnapshot {
        schema: TELEMETRY_SCHEMA,
        allocator: "jemalloc",
        capture: "post-successful-workload-run",
        native_api: "jemalloc mallctl stats.*",
        native_payload: serde_json::json!({
            "stats.allocated": jemalloc_read_usize(b"stats.allocated\0")?,
            "stats.active": jemalloc_read_usize(b"stats.active\0")?,
            "stats.metadata": jemalloc_read_usize(b"stats.metadata\0")?,
            "stats.resident": jemalloc_read_usize(b"stats.resident\0")?,
            "stats.mapped": jemalloc_read_usize(b"stats.mapped\0")?,
            "stats.retained": jemalloc_read_usize(b"stats.retained\0")?,
        }),
        limitations: vec![
            "jemalloc stats are allocator-native counters refreshed through the epoch mallctl",
            "thread-cache accounting can lag exact application object lifetime semantics",
            "allocator-native values are not normalized against other allocator metric names",
            "snapshot collection has its own allocator and serialization overhead",
        ],
    })
}

#[cfg(feature = "allocator-jemalloc-telemetry")]
fn refresh_jemalloc_epoch() -> anyhow::Result<()> {
    let mut epoch = 0_u64;
    let new_epoch = 1_u64;
    let mut size = std::mem::size_of::<u64>();
    let status = unsafe {
        tikv_jemalloc_sys::mallctl(
            b"epoch\0".as_ptr().cast(),
            std::ptr::addr_of_mut!(epoch).cast(),
            std::ptr::addr_of_mut!(size),
            std::ptr::addr_of!(new_epoch).cast_mut().cast(),
            std::mem::size_of::<u64>(),
        )
    };
    if status != 0 {
        anyhow::bail!("jemalloc mallctl epoch refresh failed with status {status}");
    }
    if size != std::mem::size_of::<u64>() {
        anyhow::bail!("jemalloc epoch returned unexpected value size {size}");
    }
    Ok(())
}

#[cfg(feature = "allocator-jemalloc-telemetry")]
fn jemalloc_read_usize(name: &'static [u8]) -> anyhow::Result<usize> {
    if name.last().copied() != Some(0) {
        anyhow::bail!("jemalloc mallctl name must be NUL terminated");
    }

    let mut value = 0_usize;
    let mut size = std::mem::size_of::<usize>();
    let status = unsafe {
        tikv_jemalloc_sys::mallctl(
            name.as_ptr().cast(),
            std::ptr::addr_of_mut!(value).cast(),
            std::ptr::addr_of_mut!(size),
            std::ptr::null_mut(),
            0,
        )
    };
    if status != 0 {
        anyhow::bail!("jemalloc mallctl read failed with status {status}");
    }
    if size != std::mem::size_of::<usize>() {
        anyhow::bail!("jemalloc mallctl returned unexpected value size {size}");
    }
    Ok(value)
}

#[cfg(feature = "allocator-mimalloc-telemetry")]
fn selected_snapshot() -> anyhow::Result<AllocatorTelemetrySnapshot> {
    let native = mimalloc::MiMalloc::stats_json()
        .map_err(anyhow::Error::msg)
        .context("capture mimalloc native stats JSON")?;
    let native_text = native
        .to_str()
        .context("mimalloc native stats JSON was not UTF-8")?;
    let native_payload =
        serde_json::from_str(native_text).context("parse mimalloc native stats JSON")?;

    Ok(AllocatorTelemetrySnapshot {
        schema: TELEMETRY_SCHEMA,
        allocator: "mimalloc",
        capture: "post-successful-workload-run",
        native_api: "mimalloc::MiMalloc::stats_json / mi_stats_get_json",
        native_payload,
        limitations: vec![
            "the payload is mimalloc's native JSON schema and is intentionally not normalized",
            "stats_json allocates its JSON buffer with mimalloc and therefore contributes capture overhead",
            "libmimalloc-sys 0.1.49 does not encode the exact bundled upstream mimalloc revision in its crate version",
        ],
    })
}

#[cfg(feature = "allocator-snmalloc-telemetry")]
fn selected_snapshot() -> anyhow::Result<AllocatorTelemetrySnapshot> {
    let stats = snmalloc_rs::SnMalloc::memory_stats();
    Ok(AllocatorTelemetrySnapshot {
        schema: TELEMETRY_SCHEMA,
        allocator: "snmalloc",
        capture: "post-successful-workload-run",
        native_api: "snmalloc_rs::SnMalloc::memory_stats",
        native_payload: serde_json::json!({
            "current_memory_usage": stats.current_memory_usage,
            "peak_memory_usage": stats.peak_memory_usage,
        }),
        limitations: vec![
            "snmalloc memory_stats reports range-level bytes reserved from the OS at slab/chunk granularity",
            "current_memory_usage is not the number of bytes in live individual application allocations",
            "allocator-native values are not normalized against other allocator metric names",
        ],
    })
}

#[cfg(not(any(
    feature = "allocator-jemalloc-telemetry",
    feature = "allocator-mimalloc-telemetry",
    feature = "allocator-snmalloc-telemetry"
)))]
fn selected_snapshot() -> anyhow::Result<AllocatorTelemetrySnapshot> {
    anyhow::bail!(
        "allocator-native telemetry is not wired for selected allocator {}; use a registry-declared allocator telemetry feature",
        crate::allocator::selected_allocator()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(any(
        feature = "allocator-jemalloc-telemetry",
        feature = "allocator-mimalloc-telemetry",
        feature = "allocator-snmalloc-telemetry"
    ))]
    #[test]
    fn selected_native_snapshot_is_structured() {
        let snapshot = selected_snapshot().expect("wired allocator telemetry must capture");
        assert_eq!(snapshot.schema, TELEMETRY_SCHEMA);
        assert_eq!(snapshot.allocator, crate::allocator::selected_allocator());
        assert_eq!(snapshot.capture, "post-successful-workload-run");
        assert!(snapshot.native_payload.is_object());
        assert!(!snapshot.native_api.is_empty());
        assert!(!snapshot.limitations.is_empty());
    }

    #[cfg(not(any(
        feature = "allocator-jemalloc-telemetry",
        feature = "allocator-mimalloc-telemetry",
        feature = "allocator-snmalloc-telemetry"
    )))]
    #[test]
    fn unwired_allocator_fails_closed() {
        let error = selected_snapshot().expect_err("unwired telemetry must fail");
        assert!(error.to_string().contains("not wired"));
    }
}
