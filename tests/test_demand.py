from pathlib import Path

import pytest

from runner_manager.database import Database
from runner_manager.demand import DemandTracker
from runner_manager.models import RunnerPoolConfig, WorkflowJob


@pytest.mark.asyncio
async def test_webhook_lifecycle_and_history(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    tracker = DemandTracker({"default": RunnerPoolConfig(labels=["docker"])}, database)
    base = {
        "repository": {"full_name": "peer/repo"},
        "workflow_job": {
            "id": 1,
            "run_id": 2,
            "name": "test",
            "labels": ["self-hosted", "linux", "x64", "docker"],
            "created_at": "2026-01-01T00:00:00Z",
        },
    }
    job = await tracker.handle_webhook({**base, "action": "queued"})
    assert job and job.pool == "default"
    assert await tracker.queued_counts() == {"default": 1}
    completed = {
        **base,
        "action": "completed",
        "workflow_job": {
            **base["workflow_job"],
            "conclusion": "success",
            "completed_at": "2026-01-01T00:01:00Z",
        },
    }
    await tracker.handle_webhook(completed)
    assert await tracker.queued_counts() == {"default": 0}
    assert database.list_history()[0]["conclusion"] == "success"
    database.close()


@pytest.mark.asyncio
async def test_poll_adds_matching_demand(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    tracker = DemandTracker({"default": RunnerPoolConfig(labels=["docker"])}, database)
    await tracker.apply_poll(
        [
            WorkflowJob(
                id=5,
                repository="peer/repo",
                labels=["self-hosted", "docker"],
                status="queued",
            )
        ],
        stale_after_seconds=600,
    )
    assert await tracker.queued_counts() == {"default": 1}
    database.close()
