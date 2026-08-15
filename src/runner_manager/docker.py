from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import stat
import uuid
from datetime import UTC, datetime
from time import monotonic
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
REPOSITORY_LABEL = "com.easy-runners.repository"
CONNECTION_LABEL = "com.easy-runners.connection"
RUNNER_IMAGE_LABEL = "com.easy-runners.runner-image"
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"


class DockerRunnerManager:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client or docker_sdk.DockerClient(base_url=settings.docker_host)
        self._host_cache_at = 0.0
        self._host_cache: dict[str, Any] | None = None

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)

    async def ping(self) -> bool:
        try:
            return bool(await asyncio.to_thread(self.client.ping))
        except Exception:
            return False

    async def image_exists(self, image: str) -> bool:
        try:
            await asyncio.to_thread(self._resolve_image, image)
        except Exception:
            return False
        return True

    def _resolve_image(self, image: str) -> str:
        try:
            self.client.images.get(image)
            return image
        except (docker_sdk.errors.ImageNotFound, docker_sdk.errors.NotFound):
            pass

        if image == self.settings.runner_image:
            anchors = self._compose_image_anchors()
            for anchor in sorted(
                anchors,
                key=lambda container: container.status == "running",
                reverse=True,
            ):
                anchor_image = anchor.image
                image_id = str(anchor_image.id) if anchor_image else ""
                if image_id:
                    log.warning(
                        "runner.image_resolved_from_anchor",
                        configured_image=image,
                        resolved_image=image_id,
                        anchor=anchor.name,
                    )
                    return image_id

            if "/" not in image:
                raise RuntimeError(
                    f"runner image {image!r} is not available on this Docker host; "
                    "redeploy EasyRunners with docker compose up -d --build so the "
                    "runner-image service is running"
                )

        # Preserve registry-backed custom images: Docker SDK will pull these
        # on first use exactly as it did before local image anchoring existed.
        return image

    def _compose_image_anchors(self) -> list[Container]:
        project = self._compose_project()
        labels = [f"{RUNNER_IMAGE_LABEL}=true"]
        if project:
            labels.append(f"{COMPOSE_PROJECT_LABEL}={project}")
        anchors = self.client.containers.list(
            all=True,
            filters={"label": labels},
        )
        if anchors or not project:
            return anchors

        # Before the anchor label existed, Compose still left an exited helper
        # container behind. Use it only when it belongs to this manager's
        # project so two EasyRunners deployments cannot borrow each other's image.
        return self.client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{COMPOSE_PROJECT_LABEL}={project}",
                    f"{COMPOSE_SERVICE_LABEL}=runner-image",
                ]
            },
        )

    def _compose_project(self) -> str | None:
        try:
            manager = self.client.containers.get(socket.gethostname())
            labels = manager.attrs.get("Config", {}).get("Labels", {}) or {}
            return str(labels[COMPOSE_PROJECT_LABEL]) or None
        except (docker_sdk.errors.NotFound, KeyError, AttributeError):
            return None

    async def host_resources(self) -> dict[str, Any]:
        if (
            self._host_cache is not None
            and monotonic() - self._host_cache_at < self.settings.host_resource_cache_seconds
        ):
            return dict(self._host_cache)
        resources = await asyncio.to_thread(self._host_resources)
        self._host_cache = resources
        self._host_cache_at = monotonic()
        return dict(resources)

    def _host_resources(self) -> dict[str, Any]:
        info = self.client.info()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(self.settings.data_dir)
        return {
            "cpus_total": int(info.get("NCPU") or 0),
            "memory_total_bytes": int(info.get("MemTotal") or 0),
            "disk_total_bytes": int(disk.total),
            "disk_free_bytes": int(disk.free),
            "docker_root_dir": info.get("DockerRootDir"),
            "architecture": info.get("Architecture"),
            "operating_system": info.get("OperatingSystem"),
        }

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
                    repository=labels.get(REPOSITORY_LABEL) or None,
                    connection_id=labels.get(CONNECTION_LABEL) or None,
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
        repository: str | None = None,
        connection_id: str | None = None,
    ) -> ManagedRunner:
        return await asyncio.to_thread(
            self._create_runner,
            pool_name,
            pool,
            registration_token,
            target_url,
            repository,
            connection_id,
        )

    def _create_runner(
        self,
        pool_name: str,
        pool: RunnerPoolConfig,
        registration_token: str,
        target_url: str,
        repository: str | None = None,
        connection_id: str | None = None,
    ) -> ManagedRunner:
        runner_id = uuid.uuid4().hex
        name = f"er-{self.settings.instance_id}-{pool_name}-{runner_id[:8]}"[:64]
        now = datetime.now(UTC)
        configured_image = self.settings.image_for_pool(pool)
        image = self._resolve_image(configured_image)
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
            "com.easy-runners.labels": ",".join(sorted(pool.matching_labels)),
        }
        if repository:
            labels[REPOSITORY_LABEL] = repository
        if connection_id:
            labels[CONNECTION_LABEL] = connection_id
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
            configured_image=configured_image,
            repository=repository,
            connection_id=connection_id,
        )
        return ManagedRunner(
            runner_id=runner_id,
            name=name,
            pool=pool_name,
            container_id=container.id,
            container_status="running",
            created_at=now,
            labels=sorted(pool.matching_labels),
            repository=repository,
            connection_id=connection_id,
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
        if not self.settings.runner_log_capture_enabled:
            return
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
        if not self.settings.runner_log_cleanup_enabled:
            return
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


def parse_byte_size(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?i?b?)?\s*", value.lower())
    if not match:
        raise ValueError(f"invalid byte size: {value!r}")
    amount = float(match.group(1))
    unit = (match.group(2) or "b").rstrip("b")
    unit = unit.rstrip("i")
    powers = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4}
    return int(amount * (1024 ** powers[unit]))
