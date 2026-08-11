from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from runner_manager.database import Database
from runner_manager.metrics import JOB_FAILURES, JOBS_COMPLETED, QUEUED_JOBS
from runner_manager.models import RunnerPoolConfig, WorkflowJob

log = structlog.get_logger()


def match_pool(labels: list[str], pools: dict[str, RunnerPoolConfig]) -> str | None:
    required = {label.lower() for label in labels}
    candidates: list[tuple[int, int, str]] = []
    for name, pool in pools.items():
        available = pool.effective_labels
        if required <= available:
            candidates.append((len(available - required), -pool.priority, name))
    return min(candidates)[2] if candidates else None


class DemandTracker:
    def __init__(self, pools: dict[str, RunnerPoolConfig], database: Database) -> None:
        self.pools = pools
        self.database = database
        self._jobs: dict[int, WorkflowJob] = {}
        self._last_seen: dict[int, datetime] = {}
        self._lock = asyncio.Lock()

    async def handle_webhook(self, payload: dict[str, Any]) -> WorkflowJob | None:
        raw = payload.get("workflow_job") or {}
        repository = str(payload.get("repository", {}).get("full_name", ""))
        action = str(payload.get("action", ""))
        if (
            not raw.get("id")
            or not repository
            or action
            not in {
                "queued",
                "in_progress",
                "completed",
            }
        ):
            return None
        now = datetime.now(UTC)
        job = WorkflowJob(
            id=int(raw["id"]),
            run_id=raw.get("run_id"),
            repository=repository,
            name=raw.get("name", ""),
            labels=raw.get("labels") or [],
            status=action,
            conclusion=raw.get("conclusion"),
            pool=match_pool(raw.get("labels") or [], self.pools),
            runner_name=raw.get("runner_name"),
            queued_at=_parse_time(raw.get("created_at"), now),
            started_at=_parse_time(raw.get("started_at"), now) if action != "queued" else None,
            completed_at=_parse_time(raw.get("completed_at"), now)
            if action == "completed"
            else None,
        )
        async with self._lock:
            if action == "completed":
                previous = self._jobs.pop(job.id, None)
                if previous and not job.pool:
                    job.pool = previous.pool
                self._last_seen.pop(job.id, None)
                self.database.add_history(job.id, job.model_dump(mode="json"))
                pool = job.pool or "unmatched"
                conclusion = job.conclusion or "unknown"
                JOBS_COMPLETED.labels(pool=pool, conclusion=conclusion).inc()
                if conclusion not in {"success", "neutral", "skipped"}:
                    JOB_FAILURES.labels(pool=pool).inc()
            else:
                previous = self._jobs.get(job.id)
                if previous and not job.pool:
                    job.pool = previous.pool
                self._jobs[job.id] = job
                self._last_seen[job.id] = now
            self._update_metrics()
        event = {
            "queued": "runner.job_queued",
            "in_progress": "runner.job_started",
            "completed": "runner.job_finished",
        }[action]
        log.info(
            event,
            job_id=job.id,
            repository=job.repository,
            pool=job.pool,
            conclusion=job.conclusion,
        )
        return job

    async def apply_poll(self, jobs: list[WorkflowJob], *, stale_after_seconds: int) -> None:
        now = datetime.now(UTC)
        async with self._lock:
            for job in jobs:
                job.pool = match_pool(job.labels, self.pools)
                self._jobs[job.id] = job
                self._last_seen[job.id] = now
            stale_before = now - timedelta(seconds=stale_after_seconds)
            stale = [
                job_id
                for job_id, last_seen in self._last_seen.items()
                if last_seen < stale_before and self._jobs[job_id].status == "queued"
            ]
            for job_id in stale:
                self._jobs.pop(job_id, None)
                self._last_seen.pop(job_id, None)
            self._update_metrics()

    async def snapshot(self) -> list[WorkflowJob]:
        async with self._lock:
            return [job.model_copy(deep=True) for job in self._jobs.values()]

    async def queued_counts(self) -> dict[str, int]:
        result = dict.fromkeys(self.pools, 0)
        async with self._lock:
            for job in self._jobs.values():
                if job.status == "queued" and job.pool:
                    result[job.pool] += 1
        return result

    def _update_metrics(self) -> None:
        counts = dict.fromkeys(self.pools, 0)
        for job in self._jobs.values():
            if job.status == "queued" and job.pool:
                counts[job.pool] += 1
        for pool, count in counts.items():
            QUEUED_JOBS.labels(pool=pool).set(count)


def _parse_time(value: str | None, default: datetime) -> datetime:
    if not value:
        return default
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
