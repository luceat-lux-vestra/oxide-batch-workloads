use std::env;
use std::fs::OpenOptions;
use std::io::{self, ErrorKind, Write};
use std::path::PathBuf;
use std::time::{Duration, Instant};

use oxide_batch::{StopSource, StopToken};
use serde_json::json;

const CRASH_PARTITION_ENV: &str = "OXIDEBATCH_WORKLOAD_CRASH_PARTITION";
const CRASH_BOUNDARY_ENV: &str = "OXIDEBATCH_WORKLOAD_CRASH_BOUNDARY";
const CRASH_MARKER_ENV: &str = "OXIDEBATCH_WORKLOAD_CRASH_MARKER";
const READY_MARKER_ENV: &str = "OXIDEBATCH_WORKLOAD_READY_MARKER";
const WORKER_HOLD_MS_ENV: &str = "OXIDEBATCH_WORKLOAD_WORKER_HOLD_MS";
const STOP_FILE_ENV: &str = "OXIDEBATCH_WORKLOAD_STOP_FILE";
const POLL_INTERVAL: Duration = Duration::from_millis(5);
const HOLD_POLL_INTERVAL: Duration = Duration::from_millis(10);

#[derive(Clone, Debug)]
struct CrashControl {
    partition_key: String,
    boundary: String,
    marker: PathBuf,
}

pub fn validate_runtime_controls() -> io::Result<()> {
    let _ = crash_control()?;
    let _ = worker_hold_duration()?;
    let _ = optional_path(READY_MARKER_ENV)?;
    let _ = optional_path(STOP_FILE_ENV)?;
    Ok(())
}

pub async fn pause_if_configured(partition_key: &str, boundary: &str) -> io::Result<()> {
    let Some(control) = crash_control()? else {
        return Ok(());
    };
    if control.partition_key != partition_key || control.boundary != boundary {
        return Ok(());
    }

    write_marker(
        &control.marker,
        &json!({
            "pid": std::process::id(),
            "partition_key": partition_key,
            "boundary": boundary,
        })
        .to_string(),
        false,
    )?;

    loop {
        tokio::time::sleep(Duration::from_secs(3600)).await;
    }
}

pub fn emit_ready_marker(
    partition_key: &str,
    active_workers: usize,
    peak_workers: usize,
) -> io::Result<()> {
    let Some(path) = optional_path(READY_MARKER_ENV)? else {
        return Ok(());
    };
    write_marker(
        &path,
        &json!({
            "pid": std::process::id(),
            "partition_key": partition_key,
            "active_workers": active_workers,
            "peak_workers": peak_workers,
        })
        .to_string(),
        true,
    )
}

pub async fn hold_if_configured(stop: &StopToken) -> io::Result<bool> {
    let Some(duration) = worker_hold_duration()? else {
        return Ok(false);
    };
    let deadline = Instant::now() + duration;
    loop {
        if stop.is_stop_requested() {
            return Ok(true);
        }
        let now = Instant::now();
        if now >= deadline {
            return Ok(false);
        }
        let remaining = deadline.saturating_duration_since(now);
        tokio::time::sleep(remaining.min(HOLD_POLL_INTERVAL)).await;
    }
}

pub fn spawn_stop_watcher(source: StopSource) -> io::Result<Option<tokio::task::JoinHandle<()>>> {
    let Some(path) = optional_path(STOP_FILE_ENV)? else {
        return Ok(None);
    };
    Ok(Some(tokio::spawn(async move {
        loop {
            if path.exists() {
                source.request_stop();
                return;
            }
            tokio::time::sleep(POLL_INTERVAL).await;
        }
    })))
}

fn crash_control() -> io::Result<Option<CrashControl>> {
    let partition = env::var(CRASH_PARTITION_ENV).ok();
    let boundary = env::var(CRASH_BOUNDARY_ENV).ok();
    let marker = env::var_os(CRASH_MARKER_ENV);

    match (partition, boundary, marker) {
        (None, None, None) => Ok(None),
        (Some(partition_key), Some(boundary), Some(marker)) => {
            if partition_key.is_empty() {
                return Err(invalid_input(format!(
                    "{CRASH_PARTITION_ENV} must not be empty"
                )));
            }
            if !matches!(boundary.as_str(), "before-write" | "after-business-write") {
                return Err(invalid_input(format!(
                    "{CRASH_BOUNDARY_ENV} must be before-write or after-business-write"
                )));
            }
            let marker = PathBuf::from(marker);
            if marker.as_os_str().is_empty() {
                return Err(invalid_input(format!(
                    "{CRASH_MARKER_ENV} must not be empty"
                )));
            }
            Ok(Some(CrashControl {
                partition_key,
                boundary,
                marker,
            }))
        }
        _ => Err(invalid_input(format!(
            "{CRASH_PARTITION_ENV}, {CRASH_BOUNDARY_ENV}, and {CRASH_MARKER_ENV} must be set together"
        ))),
    }
}

fn worker_hold_duration() -> io::Result<Option<Duration>> {
    let Some(raw) = env::var_os(WORKER_HOLD_MS_ENV) else {
        return Ok(None);
    };
    let raw = raw
        .into_string()
        .map_err(|_| invalid_input(format!("{WORKER_HOLD_MS_ENV} must be valid UTF-8")))?;
    let millis: u64 = raw.parse().map_err(|error| {
        invalid_input(format!(
            "{WORKER_HOLD_MS_ENV} must be an unsigned integer: {error}"
        ))
    })?;
    if millis == 0 {
        return Err(invalid_input(format!(
            "{WORKER_HOLD_MS_ENV} must be greater than zero"
        )));
    }
    Ok(Some(Duration::from_millis(millis)))
}

fn optional_path(name: &str) -> io::Result<Option<PathBuf>> {
    let Some(value) = env::var_os(name) else {
        return Ok(None);
    };
    let path = PathBuf::from(value);
    if path.as_os_str().is_empty() {
        return Err(invalid_input(format!("{name} must not be empty")));
    }
    Ok(Some(path))
}

fn write_marker(path: &PathBuf, body: &str, tolerate_existing: bool) -> io::Result<()> {
    let opened = OpenOptions::new().write(true).create_new(true).open(path);
    let mut file = match opened {
        Ok(file) => file,
        Err(error) if tolerate_existing && error.kind() == ErrorKind::AlreadyExists => {
            return Ok(())
        }
        Err(error) => {
            return Err(io::Error::new(
                error.kind(),
                format!("could not create marker {}: {error}", path.display()),
            ))
        }
    };
    file.write_all(body.as_bytes()).map_err(|error| {
        io::Error::new(
            error.kind(),
            format!("could not write marker {}: {error}", path.display()),
        )
    })?;
    file.sync_all().map_err(|error| {
        io::Error::new(
            error.kind(),
            format!("could not sync marker {}: {error}", path.display()),
        )
    })?;
    Ok(())
}

fn invalid_input(message: String) -> io::Error {
    io::Error::new(ErrorKind::InvalidInput, message)
}
