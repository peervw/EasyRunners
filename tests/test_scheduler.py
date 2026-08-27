from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runner_manager.database import Database
from runner_manager.demand import DemandTracker
from runner_manager.github import GitHubRateLimitError
from runner_manager.models import GitHubScope, ManagedRunner, RunnerPoolConfig, WorkflowJob
from runner_manager.scheduler import Scheduler


class ConnectedStore:
    connection_id = "a" * 32

    def __init__(self) -> None:
        self._connection = SimpleNamespace(
            id=self.connection_id,
            scope=GitHubScope.REPO,
            owner="peer",
            target_name="peer",
            repository=None,
        )

    def credentials(self, *args, **kwargs):
        return SimpleNamespace(connection=self._connection)

    def all_credentials(self, *args, **kwargs):
        return [self.credentials()]

    def connections(self, *args, **kwargs):
        return [self._connection]

    def connection(self, connection_id):
        return self._connection if connection_id == self.connection_id else None


class FakeGitHub:
    def __init__(self) -> None:
        self.store = ConnectedStore()
        self.remote: list[dict[str, Any]] = []
        self.deleted: list[int] = []
        self.tokens = 0
        self.token_repositories: list[str | None] = []
        self.runner_queries: list[tuple[list[str] | None, set[str] | None]] = []

    def connection_for_repository(self, repository):
        return self.store._connection if repository.lower().startswith("peer/") else None

    async def list_runners(self, repositories=None, *, connection_ids=None):
        self.runner_queries.append((repositories, connection_ids))
        if repositories is None:
            selected = list(self.remote)
        else:
            selected = [
            runner
            for runner in self.remote
            if not runner.get("repository") or runner.get("repository") in repositories
            ]
        return [
            {"connection_id": self.store.connection_id, **runner}
            for runner in selected
        ]

    async def delete_runner(self, connection_id, runner_id: int, repository=None):
        self.deleted.append(runner_id)

    async def registration_token(self, connection_id, repository=None):
        self.tokens += 1
        self.token_repositories.append(repository)
        return f"token-{self.tokens}"

    def target_url(self, connection_id, repository=None):
        return f"https://github.com/{repository or 'peer/repo'}"

    async def queued_jobs(self, repositories=None):
        return []

    async def list_repositories(self, *, connection_id=None, refresh=False):
        return ["peer/one", "peer/repo", "peer/two"]


class FakeDocker:
    def __init__(self) -> None:
        self.runners: list[ManagedRunner] = []
        self.removed: list[tuple[str, str]] = []
        self.created = 0
        self.created_repositories: list[str | None] = []
        self.created_connections: list[str | None] = []
        self.cleanup_calls: list[tuple[bool, bool]] = []
        self.resource_inventory_data = {
            "counts": {
                "networks": 4,
                "stopped_containers": 0,
                "volumes": 0,
                "suspected_leftovers": 0,
                "eligible_leftovers": 0,
            },
            "warning": False,
            "network_warning_threshold": 24,
        }

    async def ping(self):
        return True

    async def list_managed(self):
        return [runner.model_copy(deep=True) for runner in self.runners]

    async def create_runner(
        self,
        pool_name,
        pool,
        registration_token,
        target_url,
        repository=None,
        connection_id=None,
    ):
        self.created += 1
        self.created_repositories.append(repository)
        self.created_connections.append(connection_id)
        runner = ManagedRunner(
            runner_id=f"id-{self.created}",
            name=f"er-test-{pool_name}-{self.created}",
            pool=pool_name,
            container_id=f"container-{self.created}",
            container_status="running",
            created_at=datetime.now(UTC),
            labels=sorted(pool.effective_labels),
            repository=repository,
            connection_id=connection_id,
        )
        self.runners.append(runner)
        return runner

    async def remove_runner(self, runner, reason):
        self.removed.append((runner.name, reason))
        self.runners = [item for item in self.runners if item.name != runner.name]

    async def prune_logs(self):
        return None

    async def host_resources(self):
        return {
            "cpus_total": 8,
            "memory_total_bytes": 16 * 1024**3,
            "disk_total_bytes": 100 * 1024**3,
            "disk_free_bytes": 75 * 1024**3,
        }

    async def resource_inventory(self, *, refresh=False):
        return self.resource_inventory_data

    async def cleanup_orphans(self, *, dry_run, include_volumes):
        self.cleanup_calls.append((dry_run, include_volumes))
        return {"removed": [{"kind": "network"}]}


class FakeNotifications:
    configured = True

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def send(self, event, title, message, **kwargs):
        self.events.append((event, kwargs.get("details") or {}))
        return True


class FailingDocker(FakeDocker):
    async def create_runner(
        self,
        pool_name,
        pool,
        registration_token,
        target_url,
        repository=None,
        connection_id=None,
    ):
        raise RuntimeError("runner image could not start")


class FailingGitHub(FakeGitHub):
    async def queued_jobs(self, repositories=None):
        raise RuntimeError("GitHub installation token failed")


class RateLimitedGitHub(FakeGitHub):
    async def queued_jobs(self, repositories=None):
        raise GitHubRateLimitError("GitHub API rate limit is active until reset")


class MultiConnectedStore:
    def __init__(self) -> None:
        self._connections = [
            SimpleNamespace(
                id="a" * 32,
                scope=GitHubScope.REPO,
                owner="peer",
                target_name="peer",
                repository=None,
            ),
            SimpleNamespace(
                id="b" * 32,
                scope=GitHubScope.REPO,
                owner="acme",
                target_name="acme",
                repository=None,
            ),
        ]

    def connections(self, *args, **kwargs):
        return list(self._connections)

    def connection(self, connection_id):
        return next(
            (item for item in self._connections if item.id == connection_id), None
        )

    def all_credentials(self, *args, **kwargs):
        return [SimpleNamespace(connection=item) for item in self._connections]


class MultiFakeGitHub(FakeGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.store = MultiConnectedStore()
        self.token_targets: list[tuple[str, str | None]] = []

    def connection_for_repository(self, repository):
        owner = repository.partition("/")[0].lower()
        return next(
            (item for item in self.store._connections if item.owner == owner), None
        )

    async def list_repositories(self, *, connection_id=None, refresh=False):
        connection = self.store.connection(connection_id)
        return [f"{connection.owner}/project"] if connection else []

    async def registration_token(self, connection_id, repository=None):
        self.token_targets.append((connection_id, repository))
        return await super().registration_token(connection_id, repository)


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
async def test_idle_reconcile_scopes_runner_discovery_to_zero_repositories(
    settings, tmp_path: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    github = FakeGitHub()
    scheduler = Scheduler(settings, github, FakeDocker(), demand)

    await scheduler.reconcile("startup")
    github.runner_queries.clear()
    await scheduler.reconcile("scheduled")

    assert github.runner_queries == [([], set())]
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
async def test_status_reports_host_limited_runner_capacity(settings, tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    scheduler = Scheduler(settings, FakeGitHub(), FakeDocker(), demand)
    status = await scheduler.status()
    assert status["host"]["available"] is True
    assert status["host"]["cpus_available"] == 8
    assert status["host"]["memory_available_bytes"] == 16 * 1024**3
    assert status["host"]["available_runner_capacity"] == 2
    assert status["pools"]["default"]["available_capacity"] == 2
    database.close()


@pytest.mark.asyncio
async def test_resource_janitor_cleans_owned_orphans_and_warns_on_network_pressure(
    settings, tmp_path: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    docker = FakeDocker()
    docker.resource_inventory_data = {
        "counts": {
            "networks": 25,
            "stopped_containers": 1,
            "volumes": 2,
            "suspected_leftovers": 2,
            "eligible_leftovers": 1,
        },
        "warning": True,
        "network_warning_threshold": 24,
    }
    notifications = FakeNotifications()
    scheduler = Scheduler(settings, FakeGitHub(), docker, demand, notifications)

    await scheduler._maintain_docker_resources()

    assert docker.cleanup_calls == [(False, False)]
    assert notifications.events == [
        (
            "docker_address_pool_pressure",
            {
                "networks": 25,
                "warning_threshold": 24,
                "suspected_leftovers": 2,
                "eligible_leftovers": 1,
            },
        )
    ]
    database.close()


@pytest.mark.asyncio
async def test_stuck_job_sends_failure_notification(settings, tmp_path: Path) -> None:
    configured = settings.model_copy(update={"notification_stuck_job_seconds": 60})
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(configured.runner_pools, database)
    await demand.apply_poll(
        [
            WorkflowJob(
                id=77,
                repository="peer/repo",
                labels=["self-hosted", "docker"],
                status="queued",
                queued_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        ],
        stale_after_seconds=600,
    )
    notifications = FakeNotifications()
    scheduler = Scheduler(
        configured,
        FakeGitHub(),
        FakeDocker(),
        demand,
        notifications,
    )
    await scheduler.reconcile("test")
    assert notifications.events[0][0] == "job_stuck"
    assert notifications.events[0][1]["job_id"] == 77
    database.close()


@pytest.mark.asyncio
async def test_runner_startup_failure_sends_notification(settings, tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    await demand.apply_poll(
        [
            WorkflowJob(
                id=88,
                repository="peer/repo",
                labels=["self-hosted", "docker"],
                status="queued",
            )
        ],
        stale_after_seconds=600,
    )
    notifications = FakeNotifications()
    scheduler = Scheduler(
        settings,
        FakeGitHub(),
        FailingDocker(),
        demand,
        notifications,
    )
    await scheduler.reconcile("test")
    assert notifications.events[-1][0] == "runner_startup_failure"
    assert notifications.events[-1][1]["pool"] == "default"
    database.close()


@pytest.mark.asyncio
async def test_unhealthy_github_connection_sends_notification(
    settings, tmp_path: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    notifications = FakeNotifications()
    scheduler = Scheduler(
        settings,
        FailingGitHub(),
        FakeDocker(),
        demand,
        notifications,
    )
    await scheduler.poll_demand(full=True)
    assert notifications.events == [
        (
            "github_connection_unhealthy",
            {
                "operation": "queue_poll",
                "error": "GitHub installation token failed",
            },
        )
    ]
    database.close()


@pytest.mark.asyncio
async def test_rate_limited_poll_remains_due_for_automatic_recovery(
    settings, tmp_path: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    scheduler = Scheduler(
        settings,
        RateLimitedGitHub(),
        FakeDocker(),
        DemandTracker(settings.runner_pools, database),
        FakeNotifications(),
    )

    await scheduler.poll_demand(full=True)

    assert scheduler._last_full_poll == 0
    assert scheduler._last_queue_poll == 0
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
async def test_jobs_from_multiple_connections_get_connection_scoped_runners(
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
                repository="peer/project",
                connection_id="a" * 32,
                labels=["self-hosted", "docker"],
                status="queued",
            ),
            WorkflowJob(
                id=2,
                repository="acme/project",
                connection_id="b" * 32,
                labels=["self-hosted", "docker"],
                status="queued",
            ),
        ],
        stale_after_seconds=600,
    )
    github = MultiFakeGitHub()
    docker = FakeDocker()
    scheduler = Scheduler(settings, github, docker, demand)
    await scheduler.reconcile("webhook")

    assert github.token_targets == [
        ("a" * 32, "peer/project"),
        ("b" * 32, "acme/project"),
    ]
    assert docker.created_connections == ["a" * 32, "b" * 32]
    assert docker.created_repositories == ["peer/project", "acme/project"]
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
async def test_wrong_target_idle_runner_does_not_block_a_full_pool(
    settings, tmp_path: Path
) -> None:
    pool = RunnerPoolConfig(labels=["docker"], min=0, max=1, idle_timeout=0)
    settings = settings.model_copy(update={"runner_pools": {"default": pool}})
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    await demand.apply_poll(
        [
            WorkflowJob(
                id=4,
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
        connection_id=github.store.connection_id,
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
    await scheduler.reconcile("replace")
    assert docker.removed == [(old.name, "idle_scale_down")]
    await scheduler.reconcile("replace")
    assert docker.created_repositories == ["peer/two"]
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
async def test_github_discovery_failure_does_not_remove_a_live_runner(
    settings, tmp_path: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    demand = DemandTracker(settings.runner_pools, database)
    github = FakeGitHub()
    github.runner_errors = {github.store.connection_id: "GitHub unavailable"}
    docker = FakeDocker()
    runner = ManagedRunner(
        runner_id="live",
        name="er-test-default-live",
        pool="default",
        connection_id=github.store.connection_id,
        repository="peer/repo",
        container_id="live-container",
        container_status="running",
        created_at=datetime.now(UTC) - timedelta(minutes=10),
        state="busy",
        busy=True,
    )
    docker.runners.append(runner)
    scheduler = Scheduler(settings, github, docker, demand)
    scheduler._runners = [runner.model_copy(deep=True)]
    await scheduler.reconcile("github-outage")
    assert docker.removed == []
    assert scheduler.runners()[0]["state"] == "busy"
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
