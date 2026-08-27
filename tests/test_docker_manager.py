import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import docker as docker_sdk
import pytest

from runner_manager.docker import DockerRunnerManager, parse_byte_size
from runner_manager.models import DockerMode, ManagedRunner, RunnerPoolConfig


class FakeContainer:
    def __init__(self, container_id: str = "abcdef1234567890") -> None:
        self.id = container_id
        self.short_id = container_id[:12]
        self.name = "container"
        self.status = "running"
        self.attrs: dict[str, Any] = {}
        self.stopped = False
        self.removed = False

    def reload(self) -> None:
        return None

    def stop(self, timeout: int) -> None:
        self.stopped = True
        self.status = "exited"

    def remove(self, force: bool, v: bool) -> None:
        self.removed = True

    def get_archive(self, path: str):
        return iter([b"diagnostics"]), {"size": 11}


class FakeDockerResource:
    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.id = name
        self.name = name
        self.attrs = {
            "Labels": labels,
            "Created": "2020-01-01T00:00:00Z",
        }
        self.removed = False

    def remove(self, **kwargs) -> None:
        self.removed = True


class FakeContainers:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None
        self.container = FakeContainer()

    def run(self, **kwargs):
        self.kwargs = kwargs
        return self.container

    def list(self, **kwargs):
        return []

    def get(self, container_id: str):
        return self.container


class FakeImages:
    def __init__(self) -> None:
        self.missing = False

    def get(self, image: str):
        if self.missing:
            raise docker_sdk.errors.ImageNotFound(image)
        return SimpleNamespace(id=f"sha256:{image}")


class FakeResources:
    def __init__(self, resources: list[Any] | None = None) -> None:
        self.resources = resources or []

    def list(self, **kwargs):
        return list(self.resources)


class FakeClient:
    def __init__(self) -> None:
        self.containers = FakeContainers()
        self.images = FakeImages()
        self.networks = FakeResources()
        self.volumes = FakeResources()

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None

    def info(self) -> dict[str, Any]:
        return {
            "NCPU": 8,
            "MemTotal": 16 * 1024**3,
            "DockerRootDir": "/var/lib/docker",
            "Architecture": "x86_64",
            "OperatingSystem": "Linux",
        }


@pytest.mark.asyncio
async def test_create_runner_applies_security_and_resources(settings) -> None:
    client = FakeClient()
    manager = DockerRunnerManager(settings, client)
    pool = RunnerPoolConfig(
        labels=["docker", "deploy"],
        docker_mode=DockerMode.NONE,
        cpu=2,
        memory="4g",
        pids_limit=512,
        runner_group="Deploy",
        environment={"PLAIN": "value"},
    )
    runner = await manager.create_runner(
        "deploy", pool, "secret-token", "https://github.com/o/repo", "o/repo"
    )
    kwargs = client.containers.kwargs
    assert kwargs and kwargs["privileged"] is False
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["user"] == "1001:1001"
    assert kwargs["mem_limit"] == "4g"
    assert kwargs["nano_cpus"] == 2_000_000_000
    assert kwargs["pids_limit"] == 512
    assert kwargs["volumes"] == {}
    assert kwargs["environment"]["RUNNER_TOKEN"].endswith("-token")
    assert kwargs["environment"]["RUNNER_LABELS"] == "deploy,docker"
    assert kwargs["environment"]["RUNNER_GROUP"] == "Deploy"
    assert kwargs["environment"]["COMPOSE_PROJECT_NAME"].startswith("er-test-")
    assert kwargs["environment"]["EASY_RUNNERS_RUNNER_ID"] == runner.runner_id
    assert kwargs["environment"]["RUNNER_DOCKER_MODE"] == "none"
    assert kwargs["labels"]["com.easy-runners.compose-project"] == runner.compose_project
    assert kwargs["labels"]["com.easy-runners.repository"] == "o/repo"
    assert runner.pool == "deploy"
    assert runner.repository == "o/repo"


@pytest.mark.asyncio
async def test_standard_pool_uses_universal_image(settings) -> None:
    client = FakeClient()
    manager = DockerRunnerManager(settings, client)
    await manager.create_runner(
        "standard",
        RunnerPoolConfig(labels=["standard"], docker_mode="none"),
        "token",
        "https://github.com/o/r",
    )
    assert client.containers.kwargs["image"] == settings.runner_image


@pytest.mark.asyncio
async def test_missing_local_tag_uses_compose_image_anchor(settings) -> None:
    client = FakeClient()
    client.images.missing = True
    anchor = FakeContainer("anchor123456789")
    anchor.name = "easy-runners-runner-image-1"
    anchor.image = SimpleNamespace(id="sha256:anchored-runner-image")
    client.containers.list = lambda **kwargs: [anchor]
    manager = DockerRunnerManager(settings, client)

    assert await manager.image_exists(settings.runner_image)
    await manager.create_runner(
        "standard",
        RunnerPoolConfig(labels=["standard"], docker_mode="none"),
        "token",
        "https://github.com/o/r",
    )

    assert client.containers.kwargs["image"] == "sha256:anchored-runner-image"


@pytest.mark.asyncio
async def test_runner_image_anchor_is_scoped_to_this_compose_project(settings) -> None:
    client = FakeClient()
    client.images.missing = True
    client.containers.container.attrs = {
        "Config": {"Labels": {"com.docker.compose.project": "current-project"}}
    }
    anchor = FakeContainer("current-project-anchor")
    anchor.image = SimpleNamespace(id="sha256:current-project-image")

    def list_containers(**kwargs):
        assert kwargs["filters"]["label"] == [
            "com.easy-runners.runner-image=true",
            "com.docker.compose.project=current-project",
        ]
        return [anchor]

    client.containers.list = list_containers
    manager = DockerRunnerManager(settings, client)

    assert manager._resolve_image(settings.runner_image) == (
        "sha256:current-project-image"
    )


@pytest.mark.asyncio
async def test_missing_source_image_has_actionable_error(settings) -> None:
    client = FakeClient()
    client.images.missing = True
    manager = DockerRunnerManager(settings, client)

    with pytest.raises(RuntimeError, match="runner-image service is running"):
        await manager.create_runner(
            "standard",
            RunnerPoolConfig(labels=["standard"], docker_mode="none"),
            "token",
            "https://github.com/o/r",
        )


@pytest.mark.asyncio
async def test_socket_mode_mounts_socket_and_host_gid(settings, monkeypatch) -> None:
    socket_path = Path("/var/run/test-docker.sock")
    socket_stat = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_gid=123)
    monkeypatch.setattr(Path, "stat", lambda self: socket_stat)
    configured = settings.model_copy(update={"docker_socket_path": socket_path})
    client = FakeClient()
    manager = DockerRunnerManager(configured, client)
    await manager.create_runner(
        "default",
        RunnerPoolConfig(docker_mode="socket"),
        "token",
        "https://github.com/o/r",
    )
    kwargs = client.containers.kwargs
    assert kwargs["volumes"][str(socket_path)] == {
        "bind": "/var/run/docker.sock",
        "mode": "rw",
    }
    assert kwargs["group_add"] == [123]


@pytest.mark.asyncio
async def test_isolated_mode_uses_privileged_private_daemon(settings) -> None:
    client = FakeClient()
    manager = DockerRunnerManager(settings, client)
    await manager.create_runner(
        "isolated",
        RunnerPoolConfig(labels=["docker", "isolated"], docker_mode="isolated"),
        "token",
        "https://github.com/o/r",
    )
    kwargs = client.containers.kwargs
    assert kwargs["privileged"] is True
    assert kwargs["user"] == "0:0"
    assert "cap_drop" not in kwargs
    assert "security_opt" not in kwargs
    assert kwargs["volumes"] == {}
    assert kwargs["environment"]["DOCKER_HOST"].endswith("easyrunners-docker/docker.sock")


@pytest.mark.asyncio
async def test_remove_archives_then_destroys(settings) -> None:
    client = FakeClient()
    manager = DockerRunnerManager(settings, client)
    runner = ManagedRunner(
        runner_id="runner-id",
        name="runner",
        pool="default",
        container_id=client.containers.container.id,
        container_status="running",
        created_at=datetime.now(UTC),
    )
    await manager.remove_runner(runner, "test")
    assert client.containers.container.stopped
    assert client.containers.container.removed
    assert (settings.data_dir / "runner-logs/runner-id.tar").read_bytes() == b"diagnostics"


@pytest.mark.asyncio
async def test_runner_teardown_removes_exact_compose_project_resources(settings) -> None:
    client = FakeClient()
    project = "er-test-abc123def456"
    runner_container = client.containers.container
    sibling = FakeContainer("sibling-container")
    sibling.name = "job-service"
    sibling.attrs = {
        "Config": {"Labels": {"com.docker.compose.project": project}}
    }
    owned_network = FakeDockerResource(
        "job-default", {"com.docker.compose.project": project}
    )
    unrelated_network = FakeDockerResource(
        "customer-default", {"com.docker.compose.project": "customer"}
    )
    client.containers.list = lambda **kwargs: [runner_container, sibling]
    client.networks = FakeResources([owned_network, unrelated_network])
    manager = DockerRunnerManager(settings, client)
    runner = ManagedRunner(
        runner_id="abc123def456",
        name="runner",
        pool="default",
        container_id=runner_container.id,
        container_status="running",
        created_at=datetime.now(UTC),
        compose_project=project,
    )

    await manager.remove_runner(runner, "test")

    assert sibling.stopped
    assert sibling.removed
    assert owned_network.removed
    assert not unrelated_network.removed


@pytest.mark.asyncio
async def test_diagnostic_capture_and_cleanup_can_be_disabled(settings) -> None:
    settings.runner_log_capture_enabled = False
    settings.runner_log_cleanup_enabled = False
    client = FakeClient()
    manager = DockerRunnerManager(settings, client)
    runner = ManagedRunner(
        runner_id="no-log",
        name="runner",
        pool="default",
        container_id=client.containers.container.id,
        container_status="running",
        created_at=datetime.now(UTC),
    )
    await manager.remove_runner(runner, "test")
    assert not (settings.data_dir / "runner-logs/no-log.tar").exists()

    logs = settings.data_dir / "runner-logs"
    logs.mkdir(parents=True)
    old = logs / "old.log"
    old.write_text("old")
    os.utime(old, (0, 0))
    await manager.prune_logs()
    assert old.exists()
    settings.runner_log_cleanup_enabled = True
    await manager.prune_logs()
    assert not old.exists()


@pytest.mark.asyncio
async def test_orphan_scan_and_cleanup_only_touch_runner_owned_resources(settings) -> None:
    configured = settings.model_copy(
        update={"docker_resource_cleanup_grace_seconds": 30}
    )
    client = FakeClient()
    project = "er-test-deadbeef1234"
    orphan = FakeContainer("orphan-container")
    orphan.name = "orphan-container"
    orphan.status = "exited"
    orphan.attrs = {
        "Config": {"Labels": {"com.docker.compose.project": project}},
        "State": {"Status": "exited"},
        "Created": "2020-01-01T00:00:00Z",
    }
    owned_network = FakeDockerResource(
        "owned-network", {"com.docker.compose.project": project}
    )
    unrelated_network = FakeDockerResource(
        "unrelated-network", {"com.docker.compose.project": "customer-stack"}
    )
    owned_volume = FakeDockerResource(
        "owned-volume", {"com.docker.compose.project": project}
    )
    client.containers.list = lambda **kwargs: [orphan]
    client.networks = FakeResources([owned_network, unrelated_network])
    client.volumes = FakeResources([owned_volume])
    manager = DockerRunnerManager(configured, client)

    inventory = await manager.resource_inventory(refresh=True)
    assert inventory["counts"] == {
        "networks": 2,
        "containers": 1,
        "stopped_containers": 1,
        "volumes": 1,
        "suspected_leftovers": 3,
        "eligible_leftovers": 3,
    }
    preview = await manager.cleanup_orphans(dry_run=True, include_volumes=False)
    assert {(item["kind"], item["name"]) for item in preview["targets"]} == {
        ("container", "orphan-container"),
        ("network", "owned-network"),
    }
    assert not orphan.removed
    assert not unrelated_network.removed

    selected = [
        item["key"]
        for item in inventory["targets"]
        if item["name"] != "owned-network"
    ]
    result = await manager.cleanup_orphans(
        dry_run=False,
        include_volumes=True,
        target_keys=selected,
    )
    assert len(result["removed"]) == 2
    assert orphan.removed
    assert not owned_network.removed
    assert owned_volume.removed
    assert not unrelated_network.removed


@pytest.mark.asyncio
async def test_host_resources_report_docker_host_and_disk(settings) -> None:
    manager = DockerRunnerManager(settings, FakeClient())
    resources = await manager.host_resources()
    assert resources["cpus_total"] == 8
    assert resources["memory_total_bytes"] == 16 * 1024**3
    assert resources["disk_total_bytes"] > resources["disk_free_bytes"] > 0
    assert parse_byte_size("8g") == 8 * 1024**3
    assert parse_byte_size("512MiB") == 512 * 1024**2
