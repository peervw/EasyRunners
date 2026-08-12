import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runner_manager.docker import DockerRunnerManager
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


class FakeClient:
    def __init__(self) -> None:
        self.containers = FakeContainers()

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


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
    assert kwargs["labels"]["com.easy-runners.repository"] == "o/repo"
    assert runner.pool == "deploy"
    assert runner.repository == "o/repo"


@pytest.mark.asyncio
async def test_rust_pool_uses_universal_image(settings) -> None:
    client = FakeClient()
    manager = DockerRunnerManager(settings, client)
    await manager.create_runner(
        "rust",
        RunnerPoolConfig(labels=["rust"], docker_mode="none"),
        "token",
        "https://github.com/o/r",
    )
    assert client.containers.kwargs["image"] == settings.runner_image


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
