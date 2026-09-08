use std::sync::Arc;
use std::time::SystemTime;

use anyhow::{bail, Result};
use oxide_batch::{
    BatchStatus, JobInstanceKey, JobName, JobParameters, JobRepository, PostgresConfig,
    PostgresJobRepository, SystemClock, TlsMode,
};
use serde_json::json;

const JOB_PREFIX: &str = "postgres-local-partition";

pub async fn inspect(url: &str, run_name: &str) -> Result<()> {
    let repository =
        PostgresJobRepository::connect(framework_config(url, 2)?, Arc::new(SystemClock)).await?;
    let key = JobInstanceKey::new(job_name(run_name)?, &JobParameters::new());
    let mut unit = repository.begin().await?;
    let instance = unit
        .find_job_instance(&key)
        .await?
        .ok_or_else(|| anyhow::anyhow!("durable job instance is missing"))?;
    let execution = unit
        .job_executions(instance.id())
        .await?
        .into_iter()
        .last()
        .ok_or_else(|| anyhow::anyhow!("durable job execution is missing"))?;
    let steps = unit.step_executions(execution.id()).await?;
    let parent = steps
        .iter()
        .find(|step| step.step_name().as_str() == "partitioned")
        .cloned()
        .ok_or_else(|| anyhow::anyhow!("partition manager step is missing"))?;
    let partitions = unit.step_partition_plan(parent.id()).await?;
    let mut workers = Vec::with_capacity(partitions.len());
    for partition in partitions {
        if partition.status() != BatchStatus::Completed {
            bail!(
                "partition {} is {}, expected COMPLETED",
                partition.key(),
                partition.status()
            );
        }
        let worker_id = partition.worker_step_execution_id().ok_or_else(|| {
            anyhow::anyhow!(
                "completed partition {} has no durable worker step execution id",
                partition.key()
            )
        })?;
        let worker = steps
            .iter()
            .find(|step| step.id() == worker_id)
            .cloned()
            .ok_or_else(|| {
                anyhow::anyhow!(
                    "durable worker step execution {worker_id} referenced by partition {} is missing from the job execution snapshot",
                    partition.key()
                )
            })?;
        workers.push(worker);
    }
    unit.rollback().await?;
    repository.close().await?;

    if execution.metadata().status() != BatchStatus::Completed {
        bail!(
            "latest job execution is {}, expected COMPLETED",
            execution.metadata().status()
        );
    }
    let job_timestamps = execution.metadata().timestamps();
    let job_ended = job_timestamps
        .ended_at()
        .ok_or_else(|| anyhow::anyhow!("completed job execution has no ended_at"))?;
    let job_started = job_timestamps
        .started_at()
        .ok_or_else(|| anyhow::anyhow!("completed job execution has no started_at"))?;

    if parent.metadata().status() != BatchStatus::Completed {
        bail!(
            "partition manager is {}, expected COMPLETED",
            parent.metadata().status()
        );
    }
    let parent_timestamps = parent.metadata().timestamps();
    let parent_started = parent_timestamps
        .started_at()
        .ok_or_else(|| anyhow::anyhow!("completed parent has no started_at"))?;
    let parent_ended = parent_timestamps
        .ended_at()
        .ok_or_else(|| anyhow::anyhow!("completed parent has no ended_at"))?;

    let mut worker_duration_seconds = Vec::with_capacity(workers.len());
    let mut worker_ended = Vec::with_capacity(workers.len());
    for step in workers {
        if step.metadata().status() != BatchStatus::Completed {
            bail!(
                "worker step {} is {}, expected COMPLETED",
                step.id(),
                step.metadata().status()
            );
        }
        let timestamps = step.metadata().timestamps();
        let started = timestamps
            .started_at()
            .ok_or_else(|| anyhow::anyhow!("completed worker {} has no started_at", step.id()))?;
        let ended = timestamps
            .ended_at()
            .ok_or_else(|| anyhow::anyhow!("completed worker {} has no ended_at", step.id()))?;
        worker_duration_seconds.push(seconds_between(started, ended)?);
        worker_ended.push(ended);
    }
    if worker_duration_seconds.is_empty() {
        bail!("no completed partition worker step executions were observed")
    }
    let last_worker_ended = worker_ended
        .into_iter()
        .max()
        .ok_or_else(|| anyhow::anyhow!("no worker end timestamp"))?;

    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "run_name": run_name,
            "job_execution_id": execution.id().to_string(),
            "job_created_to_ended_seconds": seconds_between(job_timestamps.created_at(), job_ended)?,
            "job_started_to_ended_seconds": seconds_between(job_started, job_ended)?,
            "parent_started_to_ended_seconds": seconds_between(parent_started, parent_ended)?,
            "worker_execution_count": worker_duration_seconds.len(),
            "worker_duration_seconds": worker_duration_seconds,
            "aggregation_tail_seconds": seconds_between(last_worker_ended, parent_ended)?,
            "source": "published oxide-batch 0.6.0 durable execution timestamps via public JobRepository and durable partition worker ids joined to step_executions",
        }))?
    );
    Ok(())
}

fn seconds_between(start: SystemTime, end: SystemTime) -> Result<f64> {
    Ok(end.duration_since(start)?.as_secs_f64())
}

fn job_name(run_name: &str) -> Result<JobName> {
    Ok(JobName::new(format!("{JOB_PREFIX}-{run_name}"))?)
}

fn framework_config(url: &str, pool_size: u32) -> Result<PostgresConfig> {
    Ok(PostgresConfig::new(url.to_owned())?
        .with_tls_mode(TlsMode::Plaintext)
        .with_pool_size(pool_size)?)
}
