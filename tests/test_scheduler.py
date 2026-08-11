from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runner_manager.database import Database
from runner_manager.demand import DemandTracker
from runner_manager.models import ManagedRunner, RunnerPoolConfig, WorkflowJob
from runner_manager.scheduler import Scheduler


class ConnectedStore:
    def credentials(self, *args, **kwargs):
        return SimpleNamespace(
            connection=SimpleNamespace(scope=SimpleNamespace(value="repo"), target_name="peer/repo")
        )


class FakeGitHub:
    def __init__(self) -> None:
        self.store = ConnectedStore()
        self.remote: list[dict[str, Any]] = []
        self.deleted: list[int] = []
        self.tokens = 0

    async def list_runners(self):
        return list(self.remote)

    async def delete_runner(self, runner_id: int):
        self.deleted.append(runner_id)

    async def registration_token(self):
        self.tokens += 1
        return f"token-{self.tokens}"

    def target_url(self):
        return "https://github.com/peer/repo"

    async def queued_jobs(self, repositories=None):
        return []


class FakeDocker:
    def __init__(self) -> None:
        self.runners: list[ManagedRunner] = []
        self.removed: list[tuple[str, str]] = []
        self.created = 0

    async def ping(self):
        return True

    async def list_managed(self):
        return [runner.model_copy(deep=True) for runner in self.runners]

    async def create_runner(self, pool_name, pool, registration_token, target_url):
        self.created += 1
        runner = ManagedRunner(
            runner_id=f"id-{self.created}",
            name=f"er-test-{pool_name}-{self.created}",
            pool=pool_name,
            container_id=f"container-{self.created}",
            container_status="running",
            created_at=datetime.now(UTC),
            labels=sorted(pool.effective_labels),
        )
        self.runners.append(runner)
        return runner

    async def remove_runner(self, runner, reason):
        self.removed.append((runner.name, reason))
        self.runners = [item for item in self.runners if item.name != runner.name]

    async def prune_logs(self):
        return None


@pytest.mark.asyncio
async def test_reconcile_creates_capacity_once_under_concurrency(settings, tmp_path: Path) -> None:
    settings = settings.model_copy(
        update={"runner_pools": {"default": RunnerPoolConfig(min=1, max=2)}}
    )
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    github = FakeGitHub()
    docker = FakeDocker()
    scheduler = Scheduler(settings, github, docker, demand)
    await __import__("asyncio").gather(scheduler.reconcile("one"), scheduler.reconcile("two"))
    assert docker.created == 1
    assert github.tokens == 1
    database.close()


@pytest.mark.asyncio
async def test_queued_demand_scales_and_restart_runner_is_adopted(settings, tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    await demand.apply_poll(
        [
            WorkflowJob(
                id=1,
                repository="peer/repo",
                labels=["self-hosted", "docker"],
                status="queued",
            )
        ],
        stale_after_seconds=600,
    )
    github = FakeGitHub()
    docker = FakeDocker()
    scheduler = Scheduler(settings, github, docker, demand)
    await scheduler.reconcile("startup")
    assert docker.created == 1
    github.remote = [{"id": 9, "name": docker.runners[0].name, "status": "online", "busy": False}]
    second = Scheduler(settings, github, docker, demand)
    await second.reconcile("restart")
    assert docker.created == 1
    assert second.runners()[0]["state"] == "idle"
    database.close()


@pytest.mark.asyncio
async def test_exited_container_and_stale_registration_cleaned(settings, tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    github = FakeGitHub()
    github.remote = [{"id": 44, "name": "er-test-default-old", "status": "offline", "busy": False}]
    docker = FakeDocker()
    docker.runners.append(
        ManagedRunner(
            runner_id="old",
            name="er-test-default-exited",
            pool="default",
            container_id="old-container",
            container_status="exited",
            created_at=datetime.now(UTC),
        )
    )
    scheduler = Scheduler(settings, github, docker, demand)
    await scheduler.reconcile("restart")
    assert docker.removed == [("er-test-default-exited", "container_exited")]
    assert github.deleted == [44]
    database.close()


@pytest.mark.asyncio
async def test_busy_runner_job_timeout_is_enforced(settings, tmp_path: Path) -> None:
    pool = RunnerPoolConfig(min=0, max=1, job_timeout=60, max_lifetime=120)
    settings = settings.model_copy(update={"runner_pools": {"default": pool}})
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    github = FakeGitHub()
    docker = FakeDocker()
    runner = ManagedRunner(
        runner_id="busy",
        name="er-test-default-busy",
        pool="default",
        container_id="busy-container",
        container_status="running",
        created_at=datetime.now(UTC),
    )
    docker.runners.append(runner)
    github.remote = [{"id": 5, "name": runner.name, "status": "online", "busy": True}]
    scheduler = Scheduler(settings, github, docker, demand)
    scheduler._busy_since[runner.name] = datetime.now(UTC) - timedelta(seconds=61)
    await scheduler.reconcile("timeout")
    assert docker.removed == [(runner.name, "job_timeout")]
    database.close()
