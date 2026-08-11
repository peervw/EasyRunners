from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runner_manager.database import Database
from runner_manager.demand import DemandTracker
from runner_manager.models import GitHubScope, ManagedRunner, RunnerPoolConfig, WorkflowJob
from runner_manager.scheduler import Scheduler


class ConnectedStore:
    def credentials(self, *args, **kwargs):
        return SimpleNamespace(
            connection=SimpleNamespace(
                scope=GitHubScope.REPO,
                target_name="peer",
                repository=None,
            )
        )


class FakeGitHub:
    def __init__(self) -> None:
        self.store = ConnectedStore()
        self.remote: list[dict[str, Any]] = []
        self.deleted: list[int] = []
        self.tokens = 0
        self.token_repositories: list[str | None] = []

    async def list_runners(self, repositories=None):
        if repositories is None:
            return list(self.remote)
        return [
            runner
            for runner in self.remote
            if not runner.get("repository") or runner.get("repository") in repositories
        ]

    async def delete_runner(self, runner_id: int, repository=None):
        self.deleted.append(runner_id)

    async def registration_token(self, repository=None):
        self.tokens += 1
        self.token_repositories.append(repository)
        return f"token-{self.tokens}"

    def target_url(self, repository=None):
        return f"https://github.com/{repository or 'peer/repo'}"

    async def queued_jobs(self, repositories=None):
        return []

    async def list_repositories(self):
        return ["peer/one", "peer/repo", "peer/two"]


class FakeDocker:
    def __init__(self) -> None:
        self.runners: list[ManagedRunner] = []
        self.removed: list[tuple[str, str]] = []
        self.created = 0
        self.created_repositories: list[str | None] = []

    async def ping(self):
        return True

    async def list_managed(self):
        return [runner.model_copy(deep=True) for runner in self.runners]

    async def create_runner(
        self, pool_name, pool, registration_token, target_url, repository=None
    ):
        self.created += 1
        self.created_repositories.append(repository)
        runner = ManagedRunner(
            runner_id=f"id-{self.created}",
            name=f"er-test-{pool_name}-{self.created}",
            pool=pool_name,
            container_id=f"container-{self.created}",
            container_status="running",
            created_at=datetime.now(UTC),
            labels=sorted(pool.effective_labels),
            repository=repository,
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
async def test_job_view_explains_when_no_pool_matches(settings, tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    await demand.apply_poll(
        [
            WorkflowJob(
                id=99,
                repository="peer/repo",
                labels=["self-hosted", "missing-capability"],
                status="queued",
            )
        ],
        stale_after_seconds=600,
    )
    scheduler = Scheduler(settings, FakeGitHub(), FakeDocker(), demand)
    jobs = await scheduler.job_views()
    assert jobs[0]["waiting_code"] == "no_matching_pool"
    assert "requested label" in jobs[0]["waiting_reason"]
    database.close()


@pytest.mark.asyncio
async def test_personal_installation_creates_repository_bound_runners(
    settings, tmp_path: Path
) -> None:
    settings = settings.model_copy(
        update={
            "runner_pools": {
                "default": RunnerPoolConfig(labels=["docker"], min=0, max=2)
            }
        }
    )
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    await demand.apply_poll(
        [
            WorkflowJob(
                id=1,
                repository="peer/one",
                labels=["self-hosted", "docker"],
                status="queued",
            ),
            WorkflowJob(
                id=2,
                repository="peer/two",
                labels=["self-hosted", "docker"],
                status="queued",
            ),
        ],
        stale_after_seconds=600,
    )
    github = FakeGitHub()
    docker = FakeDocker()
    scheduler = Scheduler(settings, github, docker, demand)
    await scheduler.reconcile("webhook")
    assert github.token_repositories == ["peer/one", "peer/two"]
    assert docker.created_repositories == ["peer/one", "peer/two"]
    database.close()


@pytest.mark.asyncio
async def test_manual_capacity_requires_and_targets_a_selected_repository(
    settings, tmp_path: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    github = FakeGitHub()
    docker = FakeDocker()
    scheduler = Scheduler(settings, github, docker, demand)
    with pytest.raises(ValueError, match="repository is required"):
        await scheduler.set_manual_floor("default", 1, 600)
    with pytest.raises(ValueError, match="not selected"):
        await scheduler.set_manual_floor("default", 1, 600, "peer/missing")
    await scheduler.set_manual_floor("default", 1, 600, "PEER/TWO")
    assert github.token_repositories == ["peer/two"]
    assert docker.created_repositories == ["peer/two"]
    status = await scheduler.status()
    assert status["pools"]["default"]["manual_floors"][0]["repository"] == "peer/two"
    database.close()


@pytest.mark.asyncio
async def test_idle_runner_from_another_repository_is_replaced_for_queued_work(
    settings, tmp_path: Path
) -> None:
    pool = RunnerPoolConfig(labels=["docker"], min=0, max=2, idle_timeout=0)
    settings = settings.model_copy(update={"runner_pools": {"default": pool}})
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    await demand.apply_poll(
        [
            WorkflowJob(
                id=3,
                repository="peer/two",
                labels=["self-hosted", "docker"],
                status="queued",
            )
        ],
        stale_after_seconds=600,
    )
    github = FakeGitHub()
    docker = FakeDocker()
    old = ManagedRunner(
        runner_id="old",
        name="er-test-default-old",
        pool="default",
        repository="peer/one",
        container_id="old-container",
        container_status="running",
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    docker.runners.append(old)
    github.remote = [
        {
            "id": 4,
            "name": old.name,
            "status": "online",
            "busy": False,
            "repository": "peer/one",
        }
    ]
    scheduler = Scheduler(settings, github, docker, demand)
    scheduler._idle_since[old.name] = datetime.now(UTC) - timedelta(minutes=1)
    await scheduler.reconcile("webhook")
    assert docker.created_repositories == ["peer/two"]
    assert docker.removed == [(old.name, "idle_scale_down")]
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
