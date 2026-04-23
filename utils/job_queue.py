"""
Asyncio-based sequential job queue for Locust test runs.

No external dependencies — uses Python's built-in asyncio.Queue.
Queued tests run one after another automatically. Each job tracks its own
metrics, timeseries and script so history is per-job, not per-runner reset.
"""

import asyncio
import logging
import time
import uuid
from enum import Enum
from typing import Optional

from models import TestConfig, TestMetrics, TestStatus
from utils.runner import LocustRunner
from utils import history as hist

logger = logging.getLogger(__name__)

MAX_QUEUE_DEPTH = 20    # reject submissions beyond this
MAX_STORED_JOBS  = 100  # keep this many jobs in memory before pruning old ones


class JobStatus(str, Enum):
    QUEUED    = "queued"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    CANCELLED = "cancelled"


class TestJob:
    def __init__(self, config: TestConfig):
        self.job_id        = str(uuid.uuid4())[:8]
        self.config        = config
        self.history_target: str = config.history_target.value
        self.status        = JobStatus.QUEUED
        self.submitted_at  = time.time()
        self.started_at:  Optional[float] = None
        self.finished_at: Optional[float] = None
        self.metrics:     Optional[dict]  = None
        self.timeseries:  list[dict]      = []
        self.script:      Optional[str]   = None
        self.error:       Optional[str]   = None

    def summary(self) -> dict:
        cfg = self.config
        elapsed = None
        if self.started_at and self.finished_at:
            elapsed = round(self.finished_at - self.started_at, 1)
        return {
            "job_id":       self.job_id,
            "status":       self.status,
            "base_url":     cfg.base_url,
            "users":        cfg.users,
            "duration":     cfg.duration,
            "submitted_at": self.submitted_at,
            "started_at":   self.started_at,
            "finished_at":  self.finished_at,
            "elapsed":      elapsed,
            "error":        self.error,
            "rps":          self.metrics.get("rps") if self.metrics else None,
            "total_requests": self.metrics.get("total_requests") if self.metrics else None,
        }

    def to_dict(self) -> dict:
        return {**self.summary(), "metrics": self.metrics, "timeseries": self.timeseries, "script": self.script}


class JobQueue:
    """
    Sequential test-job queue backed by asyncio.Queue (zero external deps).

    Tests submitted via submit() are executed one at a time by a background
    worker task. The underlying LocustRunner is reused between jobs.
    """

    def __init__(self):
        self._q:               asyncio.Queue  = asyncio.Queue()
        self._jobs:            dict[str, TestJob] = {}
        self._current_job_id:  Optional[str]  = None
        self._worker_task:     Optional[asyncio.Task] = None
        self.runner = LocustRunner()

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background worker. Call once inside an async context (lifespan)."""
        self._worker_task = asyncio.create_task(self._worker(), name="job_queue_worker")
        logger.info("Job queue worker started")

    async def shutdown(self) -> None:
        """Graceful shutdown: stop the active test and cancel the worker."""
        if self.runner.is_running():
            await self.runner.stop()
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("Job queue worker stopped")

    # ── worker ─────────────────────────────────────────────────────────────────

    async def _worker(self) -> None:
        while True:
            try:
                job: TestJob = await self._q.get()
            except asyncio.CancelledError:
                break

            # Job may have been cancelled while waiting in the queue
            if job.status == JobStatus.CANCELLED:
                self._q.task_done()
                continue

            self._current_job_id = job.job_id
            job.status     = JobStatus.RUNNING
            job.started_at = time.time()
            logger.info(f"[job {job.job_id}] starting — {job.config.base_url} "
                        f"{job.config.users} users {job.config.duration}s")

            try:
                self.runner.history_target = job.history_target
                await self.runner.start(job.config)

                # Poll until the locust subprocess exits (0.5 s granularity)
                while self.runner.is_running():
                    await asyncio.sleep(0.5)

                metrics = self.runner.get_metrics()
                job.metrics    = metrics.model_dump()
                job.timeseries = list(self.runner.timeseries)
                job.script     = self.runner.get_script()
                job.status = (
                    JobStatus.COMPLETED
                    if metrics.status in (TestStatus.COMPLETED, TestStatus.STOPPING)
                    else JobStatus.FAILED
                )

                _save_job(job)
                logger.info(f"[job {job.job_id}] finished → {job.status}")

            except asyncio.CancelledError:
                job.status = JobStatus.CANCELLED
                await self.runner.stop()
                logger.info(f"[job {job.job_id}] cancelled")
                raise
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.error  = str(exc)
                logger.error(f"[job {job.job_id}] failed: {exc}")
            finally:
                job.finished_at      = time.time()
                self._current_job_id = None
                self._q.task_done()

    # ── public API ─────────────────────────────────────────────────────────────

    async def submit(self, config: TestConfig) -> tuple[str, int]:
        """
        Add a test to the queue. Returns (job_id, zero-based queue position).
        Position 0 means it will start immediately (no test currently running).
        Raises RuntimeError when the queue is full.
        """
        pending = sum(1 for j in self._jobs.values() if j.status == JobStatus.QUEUED)
        if pending >= MAX_QUEUE_DEPTH:
            raise RuntimeError(f"Queue is full ({MAX_QUEUE_DEPTH} pending). Try again later.")

        # Prune oldest inactive jobs when storage limit is reached
        self._prune_inactive()

        job = TestJob(config)
        self._jobs[job.job_id] = job

        # Position = number of already-queued + 1 if something is currently running
        position = pending + (1 if self._current_job_id else 0)

        await self._q.put(job)
        logger.info(f"[job {job.job_id}] queued at position {position + 1}")
        return job.job_id, position

    async def stop_current(self) -> None:
        """Stop the currently-running test. The queue continues with the next job."""
        if self.runner.is_running():
            await self.runner.stop()

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a queued job, or stop it if it is currently running."""
        job = self._jobs.get(job_id)
        if not job:
            return False
        if job.status == JobStatus.QUEUED:
            job.status = JobStatus.CANCELLED
            return True
        if job.status == JobStatus.RUNNING:
            await self.runner.stop()
            job.status = JobStatus.CANCELLED
            return True
        return False

    def get_job(self, job_id: str) -> Optional[TestJob]:
        return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 50) -> list[dict]:
        """Job summaries sorted by submitted_at descending."""
        jobs = sorted(self._jobs.values(), key=lambda j: j.submitted_at, reverse=True)
        return [j.summary() for j in jobs[:limit]]

    def clear_inactive_jobs(self) -> int:
        """Remove completed / failed / cancelled jobs from in-memory store."""
        inactive = [
            jid for jid, j in self._jobs.items()
            if j.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
            and jid != self._current_job_id
        ]
        for jid in inactive:
            del self._jobs[jid]
        return len(inactive)

    # ── convenience accessors (used by main.py routes) ─────────────────────────

    def is_running(self) -> bool:
        return self._current_job_id is not None and self.runner.is_running()

    def queue_depth(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status == JobStatus.QUEUED)

    def current_duration(self) -> int:
        """Duration of the currently-running test, for the UI progress bar."""
        if self.runner._config:
            return self.runner._config.duration
        return 0

    # ── internal ───────────────────────────────────────────────────────────────

    def _prune_inactive(self) -> None:
        if len(self._jobs) < MAX_STORED_JOBS:
            return
        inactive = sorted(
            [j for j in self._jobs.values()
             if j.status not in (JobStatus.QUEUED, JobStatus.RUNNING)],
            key=lambda j: j.submitted_at,
        )
        for job in inactive[: len(inactive) // 2]:
            del self._jobs[job.job_id]


# ── helpers ────────────────────────────────────────────────────────────────────

def _save_job(job: TestJob) -> None:
    """Persist a completed job to the configured history store (once)."""
    try:
        if job.metrics is None:
            return
        run_id = hist.save_run(
            config=job.config.model_dump(),
            metrics=job.metrics,
            timeseries=job.timeseries,
            script=job.script,
            source=job.history_target,
        )
        # Prevent any legacy _auto_save call from double-writing
        job._saved = True
        logger.info(f"[job {job.job_id}] auto-saved as run {run_id}")
    except Exception as exc:
        logger.warning(f"[job {job.job_id}] auto-save failed: {exc}")
