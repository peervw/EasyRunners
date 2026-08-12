from __future__ import annotations

import asyncio
import contextlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

import structlog

from runner_manager.config import Settings
from runner_manager.demand import DemandTracker
from runner_manager.docker import DockerRunnerManager, parse_byte_size
from runner_manager.github import GitHubClient
from runner_manager.metrics import RECONCILE_DURATION, RUNNER_CREATION_FAILURES, RUNNERS
from runner_manager.models import GitHubScope, ManagedRunner, RunnerPoolConfig
from runner_manager.notifications import NotificationManager

log = structlog.get_logger()


@dataclass
class ManualFloor:
    desired: int
    expires_at: datetime


@dataclass(frozen=True)
class ScaleDecision:
    target: int
    create: int
    excess_idle: int


def calculate_scale(
    pool: RunnerPoolConfig,
    *,
    queued: int,
    starting: int,
    idle: int,
    busy: int,
    manual_floor: int = 0,
) -> ScaleDecision:
    target = min(pool.max, max(pool.min, busy + queued, manual_floor))
    total = starting + idle + busy
    create = max(0, target - total)
    allowed_idle = max(0, target - busy - starting)
    return ScaleDecision(target, create, max(0, idle - allowed_idle))


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        github: GitHubClient,
        docker: DockerRunnerManager,
        demand: DemandTracker,
        notifications: NotificationManager | None = None,
    ) -> None:
        self.settings = settings
        self.github = github
        self.docker = docker
        self.demand = demand
        self.notifications = notifications
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._manual: dict[tuple[str, str | None], ManualFloor] = {}
        self._busy_since: dict[str, datetime] = {}
        self._idle_since: dict[str, datetime] = {}
        self._runners: list[ManagedRunner] = []
        self._github_connected = False
        self._docker_connected = False
        self._last_reconcile: datetime | None = None
        self._last_error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_queue_poll = 0.0
        self._last_full_poll = 0.0
        self._last_runner_sweep = 0.0
        self._pool_errors: dict[str, str] = {}

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self.run(), name="runner-scheduler")

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.settings.cleanup_idle_on_shutdown:
            for runner in self._runners:
                if runner.state == "idle":
                    await self.docker.remove_runner(runner, "manager_shutdown")

    async def run(self) -> None:
        await self.poll_demand(full=True)
        while not self._stop.is_set():
            await self.reconcile("scheduled")
            now = monotonic()
            if now - self._last_full_poll >= self.settings.full_poll_interval:
                await self.poll_demand(full=True)
            elif now - self._last_queue_poll >= self.settings.queue_poll_interval:
                await self.poll_demand(full=False)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.reconcile_interval)
            except TimeoutError:
                pass

    async def poll_demand(self, *, full: bool) -> None:
        if not self.github.store.credentials():
            return
        now = monotonic()
        repositories: list[str] | None = None
        if not full:
            jobs = await self.demand.snapshot()
            repositories = sorted({job.repository for job in jobs})
            connection = self.github.store.credentials()
            if connection and connection.connection.scope == GitHubScope.REPO:
                repositories = None
            elif not repositories:
                self._last_queue_poll = now
                return
        try:
            jobs = await self.github.queued_jobs(repositories)
            await self.demand.apply_poll(
                jobs, stale_after_seconds=max(self.settings.full_poll_interval * 2, 600)
            )
            self._github_connected = True
            if full:
                self._last_full_poll = now
            self._last_queue_poll = now
            log.info("scheduler.queue_poll", full=full, queued=len(jobs))
        except Exception as exc:
            self._github_connected = False
            self._last_error = str(exc)
            log.error("scheduler.queue_poll_failed", full=full, error=str(exc))
            await self._notify_github_unhealthy(str(exc), operation="queue_poll")
            if full:
                self._last_full_poll = now
            self._last_queue_poll = now

    async def _manual_repository(self, repository: str | None) -> str | None:
        credentials = self.github.store.credentials()
        repository_bound = bool(
            credentials and credentials.connection.scope == GitHubScope.REPO
        )
        if not repository_bound:
            if repository:
                raise ValueError("organization runner pre-warming does not use a repository")
            return None
        if not repository:
            raise ValueError("repository is required for repository-specific runners")
        selected = await self.github.list_repositories()
        match = next(
            (item for item in selected if item.lower() == repository.lower()),
            None,
        )
        if not match:
            raise ValueError("repository is not selected in the GitHub App installation")
        return match

    async def set_manual_floor(
        self,
        pool: str,
        desired: int,
        ttl_seconds: int,
        repository: str | None = None,
    ) -> str | None:
        if pool not in self.settings.runner_pools:
            raise KeyError(pool)
        if desired > self.settings.runner_pools[pool].max:
            raise ValueError("desired capacity exceeds pool max")
        target_repository = await self._manual_repository(repository)
        key = (pool, target_repository)
        if desired == 0:
            self._manual.pop(key, None)
        else:
            self._manual[key] = ManualFloor(
                desired=desired,
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            )
        await self.reconcile("manual_scale")
        return target_repository

    async def test_runner(
        self, pool: str | None = None, repository: str | None = None
    ) -> dict[str, Any]:
        selected = pool
        if selected is None:
            selected = next(
                (
                    name
                    for name in ("standard", "default")
                    if name in self.settings.runner_pools
                ),
                None,
            )
        if selected is None:
            selected = next(iter(self.settings.runner_pools), None)
        if not selected or selected not in self.settings.runner_pools:
            raise KeyError(selected or "")
        target_repository = await self.set_manual_floor(selected, 1, 300, repository)
        return {
            "pool": selected,
            "repository": target_repository,
            "message": "Runner requested for five minutes",
        }

    async def replace_pools(self, pools: dict[str, RunnerPoolConfig]) -> None:
        if not pools:
            raise ValueError("at least one runner pool is required")
        fingerprints: dict[frozenset[str], str] = {}
        for name, pool in pools.items():
            if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
                raise ValueError(f"invalid pool name: {name}")
            fingerprint = frozenset(pool.matching_labels)
            if previous := fingerprints.get(fingerprint):
                raise ValueError(f"pools {previous!r} and {name!r} have identical labels")
            fingerprints[fingerprint] = name
        removed = set(self.settings.runner_pools) - set(pools)
        managed = await self.docker.list_managed()
        known = [*self._runners, *managed]
        active_removed = sorted({runner.pool for runner in known if runner.pool in removed})
        if active_removed:
            raise ValueError(
                "cannot remove pools with active runners: " + ", ".join(active_removed)
            )
        async with self._lock:
            self.settings.runner_pools = pools
            self.demand.pools = pools
            for key in list(self._manual):
                if key[0] not in pools:
                    self._manual.pop(key, None)

    async def reconcile(self, reason: str = "manual") -> dict[str, Any]:
        async with self._lock:
            started = monotonic()
            try:
                await self._reconcile_locked()
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
                log.exception("scheduler.reconcile_failed", reason=reason, error=str(exc))
            finally:
                elapsed = monotonic() - started
                RECONCILE_DURATION.labels(reason=reason).observe(elapsed)
                self._last_reconcile = datetime.now(UTC)
                log.info("scheduler.reconcile", reason=reason, duration=elapsed)
        return await self.status()

    async def _reconcile_locked(self) -> None:
        now = datetime.now(UTC)
        self._expire_manual(now)
        await self._notify_stuck_jobs(now)
        self._docker_connected = await self.docker.ping()
        if not self._docker_connected:
            raise RuntimeError("Docker Engine is unavailable")
        containers = await self.docker.list_managed()

        credentials = self.github.store.credentials()
        repository_bound = bool(
            credentials and credentials.connection.scope == GitHubScope.REPO
        )
        legacy_anchor = None
        if (
            repository_bound
            and credentials
            and getattr(credentials.connection, "repository", None)
        ):
            legacy_anchor = credentials.connection.target_name
        if legacy_anchor:
            for runner in containers:
                # Containers created before multi-repository support belong to the original target.
                runner.repository = runner.repository or legacy_anchor

        github_runners: list[dict[str, Any]] = []
        selected_repositories: list[str] = []
        if credentials:
            try:
                repositories: list[str] | None = None
                if repository_bound:
                    full_sweep = (
                        not self._last_runner_sweep
                        or monotonic() - self._last_runner_sweep
                        >= self.settings.full_poll_interval
                    )
                    if full_sweep:
                        self._last_runner_sweep = monotonic()
                        selected_repositories = await self.github.list_repositories()
                        selected_names = {
                            repository.lower() for repository in selected_repositories
                        }
                        for key in list(self._manual):
                            if key[1] and key[1].lower() not in selected_names:
                                self._manual.pop(key, None)
                        repositories = selected_repositories
                    else:
                        jobs = await self.demand.snapshot()
                        repositories = sorted(
                            {
                                *(runner.repository for runner in containers if runner.repository),
                                *(job.repository for job in jobs),
                                *(
                                    repository
                                    for _, repository in self._manual
                                    if repository
                                ),
                            }
                        )
                github_runners = await self.github.list_runners(repositories)
                self._github_connected = True
            except Exception as exc:
                self._github_connected = False
                self._last_error = str(exc)
                await self._notify_github_unhealthy(str(exc), operation="runner_discovery")
        by_name = {str(runner["name"]): runner for runner in github_runners}
        previous_by_name = {runner.name: runner for runner in self._runners}

        live: list[ManagedRunner] = []
        for runner in containers:
            pool = self.settings.runner_pools.get(runner.pool)
            if not pool:
                await self.docker.remove_runner(runner, "unknown_pool")
                continue
            if runner.container_status in {"exited", "dead"}:
                previous = previous_by_name.get(runner.name)
                if runner.exit_code not in {0} and (
                    previous is None or previous.state == "starting"
                ):
                    await self._notify_runner_failure(
                        "Runner container exited before becoming available",
                        pool=runner.pool,
                        repository=runner.repository,
                        runner=runner.name,
                        error=(
                            f"container status {runner.container_status}, "
                            f"exit code {runner.exit_code}"
                        ),
                    )
                await self.docker.remove_runner(runner, "container_exited")
                self._clear_timers(runner.name)
                continue
            age = runner.uptime_seconds(now)
            if age >= pool.max_lifetime:
                await self.docker.remove_runner(runner, "maximum_lifetime")
                self._clear_timers(runner.name)
                continue
            remote = by_name.get(runner.name)
            if remote:
                runner.github_runner_id = int(remote["id"])
                runner.github_status = str(remote.get("status"))
                runner.busy = bool(remote.get("busy"))
                if runner.busy:
                    busy_since = self._busy_since.setdefault(runner.name, now)
                    runner.busy_since = busy_since
                    runner.state = "busy"
                    self._idle_since.pop(runner.name, None)
                    if (now - busy_since).total_seconds() >= pool.job_timeout:
                        await self.docker.remove_runner(runner, "job_timeout")
                        self._clear_timers(runner.name)
                        continue
                elif remote.get("status") == "online":
                    idle_since = self._idle_since.setdefault(runner.name, now)
                    runner.idle_since = idle_since
                    runner.state = "idle"
                    self._busy_since.pop(runner.name, None)
                else:
                    runner.state = "starting"
            else:
                runner.state = "starting"
                if age >= pool.registration_timeout:
                    await self._notify_runner_failure(
                        "Runner did not register with GitHub in time",
                        pool=runner.pool,
                        repository=runner.repository,
                        runner=runner.name,
                        error=f"registration timed out after {pool.registration_timeout}s",
                    )
                    await self.docker.remove_runner(runner, "registration_timeout")
                    self._clear_timers(runner.name)
                    continue
            live.append(runner)

        live_names = {runner.name for runner in live}
        prefix = f"er-{self.settings.instance_id}-"
        for remote in github_runners:
            if (
                str(remote.get("name", "")).startswith(prefix)
                and remote.get("status") == "offline"
                and remote.get("name") not in live_names
            ):
                await self.github.delete_runner(
                    int(remote["id"]),
                    str(remote.get("repository")) if remote.get("repository") else None,
                )
                log.info("runner.stale_registration_removed", runner=remote.get("name"))

        queued = await self.demand.queued_counts()
        for pool_name, pool in self.settings.runner_pools.items():
            pool_runners = [runner for runner in live if runner.pool == pool_name]
            starting = sum(runner.state == "starting" for runner in pool_runners)
            idle = sum(runner.state == "idle" for runner in pool_runners)
            busy = sum(runner.state == "busy" for runner in pool_runners)
            manual_floors = {
                repository: floor
                for (name, repository), floor in self._manual.items()
                if name == pool_name
            }
            manual_total = sum(floor.desired for floor in manual_floors.values())
            decision: ScaleDecision
            create_targets: list[str | None]
            desired_by_repository: dict[str, int] = {}
            if repository_bound:
                queued_repositories = await self.demand.queued_repositories(pool_name)
                queued_by_repository = Counter(queued_repositories)
                busy_by_repository = Counter(
                    runner.repository
                    for runner in pool_runners
                    if runner.state == "busy" and runner.repository
                )
                starting_by_repository = Counter(
                    runner.repository
                    for runner in pool_runners
                    if runner.state == "starting" and runner.repository
                )
                idle_by_repository = Counter(
                    runner.repository
                    for runner in pool_runners
                    if runner.state == "idle" and runner.repository
                )
                for repository in {
                    *queued_by_repository,
                    *busy_by_repository,
                    *(repository for repository in manual_floors if repository),
                }:
                    desired_by_repository[repository] = max(
                        busy_by_repository[repository]
                        + queued_by_repository[repository],
                        manual_floors[repository].desired
                        if repository in manual_floors
                        else 0,
                    )
                desired_total = sum(desired_by_repository.values())
                if pool.min > desired_total:
                    if not selected_repositories:
                        selected_repositories = await self.github.list_repositories()
                    default_repository = legacy_anchor or next(
                        iter(sorted(selected_repositories)), None
                    )
                    if default_repository:
                        desired_by_repository[default_repository] = (
                            desired_by_repository.get(default_repository, 0)
                            + pool.min
                            - desired_total
                        )
                target = min(pool.max, sum(desired_by_repository.values()))
                total = starting + idle + busy
                allowed_idle = max(0, target - busy - starting)
                decision = ScaleDecision(
                    target=target,
                    create=max(0, target - total),
                    excess_idle=max(0, idle - allowed_idle),
                )
                deficits = {
                    repository: max(
                        0,
                        desired
                        - busy_by_repository[repository]
                        - starting_by_repository[repository]
                        - idle_by_repository[repository],
                    )
                    for repository, desired in desired_by_repository.items()
                }
                create_targets = []
                for repository in queued_repositories:
                    if deficits[repository] > 0:
                        create_targets.append(repository)
                        deficits[repository] -= 1
                for repository in sorted(deficits):
                    create_targets.extend([repository] * deficits[repository])
                available_slots = max(0, pool.max - len(pool_runners))
                create_targets = create_targets[:available_slots]
            else:
                decision = calculate_scale(
                    pool,
                    queued=queued.get(pool_name, 0),
                    starting=starting,
                    idle=idle,
                    busy=busy,
                    manual_floor=manual_total,
                )
                create_targets = [None] * decision.create

            for target_repository in create_targets:
                try:
                    token = await self.github.registration_token(target_repository)
                    created = await self.docker.create_runner(
                        pool_name,
                        pool,
                        token,
                        self.github.target_url(target_repository),
                        target_repository,
                    )
                    live.append(created)
                    pool_runners.append(created)
                    self._pool_errors.pop(pool_name, None)
                except Exception as exc:
                    self._pool_errors[pool_name] = str(exc)
                    RUNNER_CREATION_FAILURES.labels(pool=pool_name).inc()
                    log.exception("runner.create_failed", pool=pool_name, error=str(exc))
                    await self._notify_runner_failure(
                        "Runner startup failed",
                        pool=pool_name,
                        repository=target_repository,
                        error=str(exc),
                    )
                    break

            removal_count = max(0, len(pool_runners) - decision.target)
            removable: list[ManagedRunner] = []
            if repository_bound:
                starting_by_repository = Counter(
                    runner.repository
                    for runner in pool_runners
                    if runner.state == "starting" and runner.repository
                )
                idle_allowance = {
                    repository: max(
                        0,
                        desired
                        - busy_by_repository[repository]
                        - starting_by_repository[repository],
                    )
                    for repository, desired in desired_by_repository.items()
                }
                idle_repositories = {
                    runner.repository
                    for runner in pool_runners
                    if runner.state == "idle" and runner.repository
                }
                for repository in idle_repositories:
                    idle_runners = sorted(
                        (
                            runner
                            for runner in pool_runners
                            if runner.state == "idle"
                            and runner.repository == repository
                            and runner.idle_since
                            and (now - runner.idle_since).total_seconds()
                            >= max(pool.idle_timeout, self.settings.assignment_grace_seconds)
                        ),
                        key=lambda item: item.idle_since or now,
                    )
                    removable.extend(
                        idle_runners[
                            : max(
                                0,
                                len(idle_runners) - idle_allowance.get(repository, 0),
                            )
                        ]
                    )
                removable.sort(key=lambda item: item.idle_since or now)
            else:
                removable = sorted(
                    (
                        runner
                        for runner in pool_runners
                        if runner.state == "idle"
                        and runner.idle_since
                        and (now - runner.idle_since).total_seconds()
                        >= max(pool.idle_timeout, self.settings.assignment_grace_seconds)
                    ),
                    key=lambda item: item.idle_since or now,
                )
            for runner in removable[:removal_count]:
                await self.docker.remove_runner(runner, "idle_scale_down")
                live.remove(runner)
                self._clear_timers(runner.name)

        self._runners = live
        self._update_metrics()
        await self.docker.prune_logs()

    async def _notify_github_unhealthy(self, error: str, *, operation: str) -> None:
        if not self.notifications:
            return
        await self.notifications.send(
            "github_connection_unhealthy",
            "GitHub connection is unhealthy",
            f"EasyRunners could not complete {operation}.",
            details={"operation": operation, "error": error},
            key="github_connection_unhealthy",
        )

    async def _notify_runner_failure(
        self,
        message: str,
        *,
        pool: str,
        repository: str | None,
        error: str,
        runner: str | None = None,
    ) -> None:
        if not self.notifications:
            return
        await self.notifications.send(
            "runner_startup_failure",
            "Runner startup failed",
            message,
            details={
                "pool": pool,
                "repository": repository,
                "runner": runner,
                "error": error,
            },
            key=f"runner_startup_failure:{pool}:{repository or 'organization'}",
        )

    async def _notify_stuck_jobs(self, now: datetime) -> None:
        if not self.notifications or not self.notifications.configured:
            return
        jobs = await self.demand.snapshot()
        stuck = [
            job
            for job in jobs
            if job.status == "queued"
            and (now - job.queued_at).total_seconds()
            >= self.settings.notification_stuck_job_seconds
        ]
        if not stuck:
            return
        await asyncio.gather(
            *(
                self.notifications.send(
                    "job_stuck",
                    "GitHub Actions job is still queued",
                    f"{job.name or job.id} has waited for a runner.",
                    details={
                        "job_id": job.id,
                        "repository": job.repository,
                        "pool": job.pool,
                        "labels": job.labels,
                        "queued_seconds": int((now - job.queued_at).total_seconds()),
                    },
                    key=f"job_stuck:{job.id}",
                )
                for job in stuck
            )
        )

    def _clear_timers(self, name: str) -> None:
        self._busy_since.pop(name, None)
        self._idle_since.pop(name, None)

    def _expire_manual(self, now: datetime) -> None:
        for pool, floor in list(self._manual.items()):
            if floor.expires_at <= now:
                self._manual.pop(pool, None)

    def _update_metrics(self) -> None:
        for pool in self.settings.runner_pools:
            for state in ("starting", "idle", "busy"):
                RUNNERS.labels(pool=pool, state=state).set(
                    sum(runner.pool == pool and runner.state == state for runner in self._runners)
                )

    async def status(self) -> dict[str, Any]:
        queued = await self.demand.queued_counts()
        now = datetime.now(UTC)
        host = await self._host_capacity_status()
        pools: dict[str, Any] = {}
        for name, config in self.settings.runner_pools.items():
            runners = [runner for runner in self._runners if runner.pool == name]
            pool_manual_floors = [
                (repository, floor)
                for (pool, repository), floor in self._manual.items()
                if pool == name
            ]
            manual_floors = [
                {
                    "repository": repository,
                    "desired": floor.desired,
                    "expires_at": floor.expires_at.isoformat(),
                }
                for repository, floor in pool_manual_floors
            ]
            pools[name] = {
                "queued": queued.get(name, 0),
                "starting": sum(runner.state == "starting" for runner in runners),
                "idle": sum(runner.state == "idle" for runner in runners),
                "busy": sum(runner.state == "busy" for runner in runners),
                "min": config.min,
                "max": config.max,
                "labels": sorted(config.effective_labels),
                "config": config.model_dump(mode="json"),
                "last_error": self._pool_errors.get(name),
                "available_capacity": host.get("pool_capacity", {}).get(name),
                "manual_floor": sum(floor.desired for _, floor in pool_manual_floors),
                "manual_floor_expires_at": min(
                    (floor.expires_at.isoformat() for _, floor in pool_manual_floors),
                    default=None,
                ),
                "manual_floors": manual_floors,
            }
        credentials = self.github.store.credentials()
        return {
            "github": "connected" if self._github_connected else "disconnected",
            "docker": "connected" if self._docker_connected else "disconnected",
            "target": credentials.connection.target_name if credentials else None,
            "pools": pools,
            "host": host,
            "last_reconcile": self._last_reconcile.isoformat() if self._last_reconcile else None,
            "last_error": self._last_error,
            "time": now.isoformat(),
        }

    async def _host_capacity_status(self) -> dict[str, Any]:
        try:
            resources = await self.docker.host_resources()
        except Exception as exc:
            return {"available": False, "error": str(exc), "pool_capacity": {}}
        active = [runner for runner in self._runners if runner.state != "exited"]
        cpu_reserved = sum(
            self.settings.runner_pools[runner.pool].cpu
            for runner in active
            if runner.pool in self.settings.runner_pools
        )
        memory_reserved = sum(
            parse_byte_size(self.settings.runner_pools[runner.pool].memory)
            for runner in active
            if runner.pool in self.settings.runner_pools
        )
        cpu_total = float(resources.get("cpus_total") or 0)
        memory_total = int(resources.get("memory_total_bytes") or 0)
        cpu_available = max(0.0, cpu_total - cpu_reserved)
        memory_available = max(0, memory_total - memory_reserved)
        active_by_pool = Counter(runner.pool for runner in active)
        pool_capacity: dict[str, int] = {}
        for name, pool in self.settings.runner_pools.items():
            configured_slots = max(0, pool.max - active_by_pool[name])
            cpu_slots = int(cpu_available // pool.cpu) if cpu_total else configured_slots
            pool_memory = parse_byte_size(pool.memory)
            memory_slots = (
                memory_available // pool_memory
                if memory_total and pool_memory
                else configured_slots
            )
            pool_capacity[name] = max(
                0, min(configured_slots, cpu_slots, int(memory_slots))
            )
        disk_total = int(resources.get("disk_total_bytes") or 0)
        disk_free = int(resources.get("disk_free_bytes") or 0)
        disk_free_percent = round(disk_free * 100 / disk_total, 1) if disk_total else None
        pressure: list[str] = []
        if cpu_total and cpu_available < min(
            (pool.cpu for pool in self.settings.runner_pools.values()), default=1
        ):
            pressure.append("cpu")
        if memory_total and memory_available < min(
            (parse_byte_size(pool.memory) for pool in self.settings.runner_pools.values()),
            default=1,
        ):
            pressure.append("memory")
        if disk_free_percent is not None and disk_free_percent < 10:
            pressure.append("disk")
        return {
            **resources,
            "available": True,
            "cpus_reserved": round(cpu_reserved, 2),
            "cpus_available": round(cpu_available, 2),
            "memory_reserved_bytes": memory_reserved,
            "memory_available_bytes": memory_available,
            "disk_free_percent": disk_free_percent,
            "active_runners": len(active),
            "available_runner_capacity": max(pool_capacity.values(), default=0),
            "pool_capacity": pool_capacity,
            "pressure": pressure,
        }

    def runners(self) -> list[dict[str, Any]]:
        return [
            {
                **runner.model_dump(mode="json"),
                "uptime_seconds": runner.uptime_seconds(),
            }
            for runner in self._runners
        ]

    async def runner_views(self) -> list[dict[str, Any]]:
        jobs = await self.demand.snapshot()
        by_runner = {
            job.runner_name: job for job in jobs if job.status == "in_progress" and job.runner_name
        }
        result: list[dict[str, Any]] = []
        for runner in self.runners():
            job = by_runner.get(str(runner["name"]))
            runner["job"] = (
                {
                    "id": job.id,
                    "run_id": job.run_id,
                    "name": job.name,
                    "repository": job.repository,
                    "started_at": job.started_at.isoformat() if job.started_at else None,
                }
                if job
                else None
            )
            result.append(runner)
        return result

    async def job_views(self) -> list[dict[str, Any]]:
        jobs = await self.demand.snapshot()
        result: list[dict[str, Any]] = []
        for job in jobs:
            view = job.model_dump(mode="json")
            code: str | None = None
            detail: str | None = None
            if job.status == "queued":
                if not job.pool:
                    code = "no_matching_pool"
                    detail = "No runner pool has every requested label."
                elif not self._github_connected:
                    code = "github_unavailable"
                    detail = self._last_error or "GitHub is currently unavailable."
                elif not self._docker_connected:
                    code = "docker_unavailable"
                    detail = "Docker Engine is currently unavailable."
                else:
                    pool = self.settings.runner_pools[job.pool]
                    runners = [runner for runner in self._runners if runner.pool == job.pool]
                    repository_runners = [
                        runner
                        for runner in runners
                        if not job.repository or runner.repository in {None, job.repository}
                    ]
                    if error := self._pool_errors.get(job.pool):
                        code = "runner_creation_failed"
                        detail = error
                    elif any(runner.state == "starting" for runner in repository_runners):
                        code = "runner_starting"
                        detail = "A matching runner is registering with GitHub."
                    elif any(runner.state == "idle" for runner in repository_runners):
                        code = "awaiting_assignment"
                        detail = "A matching runner is online and waiting for GitHub assignment."
                    elif len(runners) >= pool.max:
                        code = "pool_at_capacity"
                        detail = f"Pool {job.pool!r} is at its maximum of {pool.max} runners."
                    else:
                        code = "awaiting_reconcile"
                        detail = "Capacity will be created during the next reconciliation."
            view["waiting_code"] = code
            view["waiting_reason"] = detail
            result.append(view)
        return result
