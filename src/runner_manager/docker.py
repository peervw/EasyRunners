from __future__ import annotations

import asyncio
import os
import stat
import uuid
from datetime import UTC, datetime
from typing import Any

import docker as docker_sdk
import structlog
from docker.models.containers import Container
from docker.types import LogConfig

from runner_manager.config import Settings
from runner_manager.models import DockerMode, ManagedRunner, RunnerPoolConfig

log = structlog.get_logger()

MANAGED_LABEL = "com.easy-runners.managed"
INSTANCE_LABEL = "com.easy-runners.instance"
POOL_LABEL = "com.easy-runners.pool"
RUNNER_ID_LABEL = "com.easy-runners.runner-id"
RUNNER_NAME_LABEL = "com.easy-runners.runner-name"
CREATED_LABEL = "com.easy-runners.created-at"


class DockerRunnerManager:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client or docker_sdk.DockerClient(base_url=settings.docker_host)

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)

    async def ping(self) -> bool:
        try:
            return bool(await asyncio.to_thread(self.client.ping))
        except Exception:
            return False

    async def image_exists(self, image: str) -> bool:
        try:
            await asyncio.to_thread(self.client.images.get, image)
        except (docker_sdk.errors.ImageNotFound, docker_sdk.errors.NotFound):
            return False
        except Exception:
            return False
        return True

    async def list_managed(self) -> list[ManagedRunner]:
        return await asyncio.to_thread(self._list_managed)

    def _list_managed(self) -> list[ManagedRunner]:
        containers: list[Container] = self.client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{MANAGED_LABEL}=true",
                    f"{INSTANCE_LABEL}={self.settings.instance_id}",
                ]
            },
        )
        runners: list[ManagedRunner] = []
        for container in containers:
            container.reload()
            attrs = container.attrs
            labels = attrs.get("Config", {}).get("Labels", {}) or {}
            created = labels.get(CREATED_LABEL) or attrs.get("Created")
            created_at = _parse_datetime(created)
            state = attrs.get("State", {})
            runners.append(
                ManagedRunner(
                    runner_id=labels.get(RUNNER_ID_LABEL, container.short_id),
                    name=labels.get(RUNNER_NAME_LABEL, container.name),
                    pool=labels.get(POOL_LABEL, "unknown"),
                    container_id=container.id,
                    container_status=state.get("Status", container.status),
                    created_at=created_at,
                    labels=(labels.get("com.easy-runners.labels") or "").split(","),
                    state="exited" if state.get("Status") in {"exited", "dead"} else "starting",
                    exit_code=state.get("ExitCode") if state.get("Status") == "exited" else None,
                )
            )
        return runners

    async def create_runner(
        self,
        pool_name: str,
        pool: RunnerPoolConfig,
        registration_token: str,
        target_url: str,
    ) -> ManagedRunner:
        return await asyncio.to_thread(
            self._create_runner,
            pool_name,
            pool,
            registration_token,
            target_url,
        )

    def _create_runner(
        self,
        pool_name: str,
        pool: RunnerPoolConfig,
        registration_token: str,
        target_url: str,
    ) -> ManagedRunner:
        runner_id = uuid.uuid4().hex
        name = f"er-{self.settings.instance_id}-{pool_name}-{runner_id[:8]}"[:64]
        now = datetime.now(UTC)
        image = pool.image or self.settings.runner_image
        environment = {
            "RUNNER_NAME": name,
            "RUNNER_URL": target_url,
            "RUNNER_TOKEN": registration_token,
            "RUNNER_LABELS": ",".join(pool.custom_labels),
            "RUNNER_GROUP": pool.runner_group or "",
            "RUNNER_MAX_LIFETIME": str(pool.max_lifetime),
            "RUNNER_JOB_TIMEOUT": str(pool.job_timeout),
        }
        environment.update(pool.environment)
        for variable in pool.environment_from:
            if variable not in os.environ:
                raise ValueError(
                    f"pool {pool_name!r} requires missing environment variable {variable}"
                )
            environment[variable] = os.environ[variable]

        labels = {
            MANAGED_LABEL: "true",
            INSTANCE_LABEL: self.settings.instance_id,
            POOL_LABEL: pool_name,
            RUNNER_ID_LABEL: runner_id,
            RUNNER_NAME_LABEL: name,
            CREATED_LABEL: now.isoformat(),
            "com.easy-runners.labels": ",".join(sorted(pool.effective_labels)),
        }
        volumes: dict[str, dict[str, str]] = {}
        group_add: list[int] = []
        if pool.docker_mode == DockerMode.SOCKET:
            socket_path = self.settings.docker_socket_path
            socket_stat = socket_path.stat()
            if not stat.S_ISSOCK(socket_stat.st_mode):
                raise ValueError(f"{socket_path} is not a Docker socket")
            volumes[str(socket_path)] = {"bind": "/var/run/docker.sock", "mode": "rw"}
            group_add.append(socket_stat.st_gid)
        for value in pool.volumes:
            source, target, mode = _parse_volume(value)
            volumes[source] = {"bind": target, "mode": mode}

        kwargs: dict[str, Any] = {
            "image": image,
            "name": name,
            "detach": True,
            "environment": environment,
            "labels": labels,
            "user": "1001:1001",
            "working_dir": "/home/runner",
            "mem_limit": pool.memory,
            "nano_cpus": int(pool.cpu * 1_000_000_000),
            "pids_limit": pool.pids_limit,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "privileged": False,
            "volumes": volumes,
            "group_add": group_add,
            "log_config": LogConfig(
                type=LogConfig.types.JSON,
                config={"max-size": "10m", "max-file": "3"},
            ),
        }
        if self.settings.runner_network:
            kwargs["network"] = self.settings.runner_network
        container: Container = self.client.containers.run(**kwargs)
        log.info(
            "runner.created",
            runner_id=runner_id,
            runner=name,
            pool=pool_name,
            container_id=container.short_id,
            image=image,
        )
        return ManagedRunner(
            runner_id=runner_id,
            name=name,
            pool=pool_name,
            container_id=container.id,
            container_status="running",
            created_at=now,
            labels=sorted(pool.effective_labels),
            state="starting",
        )

    async def remove_runner(self, runner: ManagedRunner, reason: str) -> None:
        await asyncio.to_thread(self._remove_runner, runner, reason)

    def _remove_runner(self, runner: ManagedRunner, reason: str) -> None:
        try:
            container: Container = self.client.containers.get(runner.container_id)
        except docker_sdk.errors.NotFound:
            return
        self._archive_diagnostics(container, runner)
        try:
            container.reload()
            if container.status not in {"exited", "dead"}:
                container.stop(timeout=30)
        finally:
            container.remove(force=True, v=True)
        log.info(
            "runner.removed",
            runner_id=runner.runner_id,
            runner=runner.name,
            pool=runner.pool,
            container_id=runner.container_id[:12],
            reason=reason,
        )

    def _archive_diagnostics(self, container: Container, runner: ManagedRunner) -> None:
        logs_dir = self.settings.data_dir / "runner-logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        try:
            stream, _ = container.get_archive("/home/runner/_diag")
            path = logs_dir / f"{runner.runner_id}.tar"
            with path.open("wb") as output:
                for chunk in stream:
                    output.write(chunk)
            path.chmod(0o600)
        except (docker_sdk.errors.NotFound, docker_sdk.errors.APIError):
            try:
                raw = container.logs(stdout=True, stderr=True, tail=5000)
                path = logs_dir / f"{runner.runner_id}.log"
                path.write_bytes(raw)
                path.chmod(0o600)
            except docker_sdk.errors.APIError:
                log.warning("runner.log_archive_failed", runner_id=runner.runner_id)

    async def prune_logs(self) -> None:
        await asyncio.to_thread(self._prune_logs)

    def _prune_logs(self) -> None:
        logs_dir = self.settings.data_dir / "runner-logs"
        if not logs_dir.exists():
            return
        cutoff = datetime.now(UTC).timestamp() - self.settings.runner_log_retention_days * 86400
        for path in logs_dir.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_volume(value: str) -> tuple[str, str, str]:
    parts = value.rsplit(":", 2)
    if len(parts) == 2:
        return parts[0], parts[1], "ro"
    if len(parts) == 3 and parts[2] in {"ro", "rw"}:
        return parts[0], parts[1], parts[2]
    raise ValueError(f"invalid volume specification: {value!r}")
