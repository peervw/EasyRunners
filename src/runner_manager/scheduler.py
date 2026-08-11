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
from runner_manager.docker import DockerRunnerManager
from runner_manager.github import GitHubClient
from runner_manager.metrics import RECONCILE_DURATION, RUNNER_CREATION_FAILURES, RUNNERS
from runner_manager.models import GitHubScope, ManagedRunner, RunnerPoolConfig

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
    ) -> None:
        self.settings = settings
        self.github = github
        self.docker = docker
        self.demand = demand
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._manual: dict[str, ManualFloor] = {}
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
            if full:
                self._last_full_poll = now
            self._last_queue_poll = now

    async def set_manual_floor(self, pool: str, desired: int, ttl_seconds: int) -> None:
        if pool not in self.settings.runner_pools:
            raise KeyError(pool)
        if desired > self.settings.runner_pools[pool].max:
            raise ValueError("desired capacity exceeds pool max")
        if desired == 0:
            self._manual.pop(pool, None)
        else:
            self._manual[pool] = ManualFloor(
                desired=desired,
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            )
        await self.reconcile("manual_scale")

    async def test_runner(self, pool: str | None = None) -> dict[str, Any]:
        selected = pool or ("default" if "default" in self.settings.runner_pools else None)
        if selected is None:
            selected = next(iter(self.settings.runner_pools), None)
        if not selected or selected not in self.settings.runner_pools:
            raise KeyError(selected or "")
        await self.set_manual_floor(selected, 1, 300)
        return {"pool": selected, "message": "Runner requested for five minutes"}

    async def replace_pools(self, pools: dict[str, RunnerPoolConfig]) -> None:
        if not pools:
            raise ValueError("at least one runner pool is required")
        fingerprints: dict[frozenset[str], str] = {}
        for name, pool in pools.items():
            if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
                raise ValueError(f"invalid pool name: {name}")
            fingerprint = frozenset(pool.effective_labels)
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
            for name in list(self._manual):
                if name not in pools:
                    self._manual.pop(name, None)

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
        self._docker_connected = await self.docker.ping()
        if not self._docker_connected:
            raise RuntimeError("Docker Engine is unavailable")
        containers = await self.docker.list_managed()

        credentials = self.github.store.credentials()
        repository_bound = bool(
            credentials and credentials.connection.scope == GitHubScope.REPO
        )
        anchor_repository = (
            credentials.connection.target_name if repository_bound and credentials else None
        )
        if anchor_repository:
            for runner in containers:
                # Containers created before multi-repository support belong to the original target.
                runner.repository = runner.repository or anchor_repository

        github_runners: list[dict[str, Any]] = []
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
                    else:
                        jobs = await self.demand.snapshot()
                        repositories = sorted(
                            {
                                *(runner.repository for runner in containers if runner.repository),
                                *(job.repository for job in jobs),
                            }
                        )
                github_runners = await self.github.list_runners(repositories)
                self._github_connected = True
            except Exception as exc:
                self._github_connected = False
                self._last_error = str(exc)
        by_name = {str(runner["name"]): runner for runner in github_runners}

        live: list[ManagedRunner] = []
        for runner in containers:
            pool = self.settings.runner_pools.get(runner.pool)
            if not pool:
                await self.docker.remove_runner(runner, "unknown_pool")
                continue
            if runner.container_status in {"exited", "dead"}:
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
            manual = self._manual.get(pool_name)
            decision = calculate_scale(
                pool,
                queued=queued.get(pool_name, 0),
                starting=starting,
                idle=idle,
                busy=busy,
                manual_floor=manual.desired if manual else 0,
            )
            create_targets: list[str | None]
            if repository_bound and anchor_repository:
                queued_repositories = await self.demand.queued_repositories(pool_name)
                queued_by_repository = Counter(queued_repositories)
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
                deficits = {
                    repository: max(
                        0,
                        count
                        - starting_by_repository[repository]
                        - idle_by_repository[repository],
                    )
                    for repository, count in queued_by_repository.items()
                }
                create_targets = []
                for repository in queued_repositories:
                    if deficits[repository] > 0:
                        create_targets.append(repository)
                        deficits[repository] -= 1
                available_slots = max(0, pool.max - len(pool_runners))
                create_targets = create_targets[:available_slots]
                floor_shortage = max(
                    0,
                    decision.target - len(pool_runners) - len(create_targets),
                )
                create_targets.extend(
                    [anchor_repository]
                    * min(floor_shortage, available_slots - len(create_targets))
                )
            else:
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
                except Exception as exc:
                    RUNNER_CREATION_FAILURES.labels(pool=pool_name).inc()
                    log.exception("runner.create_failed", pool=pool_name, error=str(exc))
                    break

            removal_count = max(0, len(pool_runners) - decision.target)
            removable: list[ManagedRunner] = []
            if repository_bound and anchor_repository:
                queued_by_repository = Counter(
                    await self.demand.queued_repositories(pool_name)
                )
                starting_by_repository = Counter(
                    runner.repository
                    for runner in pool_runners
                    if runner.state == "starting" and runner.repository
                )
                busy_total = sum(runner.state == "busy" for runner in pool_runners)
                floor = max(pool.min, manual.desired if manual else 0)
                floor_extra = max(0, floor - busy_total - sum(queued_by_repository.values()))
                spare_anchor_starting = max(
                    0,
                    starting_by_repository[anchor_repository]
                    - queued_by_repository[anchor_repository],
                )
                floor_idle = max(0, floor_extra - spare_anchor_starting)
                idle_allowance = {
                    repository: max(
                        0,
                        queued - starting_by_repository[repository],
                    )
                    for repository, queued in queued_by_repository.items()
                }
                idle_allowance[anchor_repository] = (
                    idle_allowance.get(anchor_repository, 0) + floor_idle
                )
                idle_repositories = {
                    runner.repository or anchor_repository
                    for runner in pool_runners
                    if runner.state == "idle"
                }
                for repository in idle_repositories:
                    idle_runners = sorted(
                        (
                            runner
                            for runner in pool_runners
                            if runner.state == "idle"
                            and (runner.repository or anchor_repository) == repository
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
        pools: dict[str, Any] = {}
        for name, config in self.settings.runner_pools.items():
            runners = [runner for runner in self._runners if runner.pool == name]
            manual = self._manual.get(name)
            pools[name] = {
                "queued": queued.get(name, 0),
                "starting": sum(runner.state == "starting" for runner in runners),
                "idle": sum(runner.state == "idle" for runner in runners),
                "busy": sum(runner.state == "busy" for runner in runners),
                "min": config.min,
                "max": config.max,
                "labels": sorted(config.effective_labels),
                "config": config.model_dump(mode="json"),
                "manual_floor": manual.desired if manual else 0,
                "manual_floor_expires_at": manual.expires_at.isoformat() if manual else None,
            }
        credentials = self.github.store.credentials()
        return {
            "github": "connected" if self._github_connected else "disconnected",
            "docker": "connected" if self._docker_connected else "disconnected",
            "target": credentials.connection.target_name if credentials else None,
            "pools": pools,
            "last_reconcile": self._last_reconcile.isoformat() if self._last_reconcile else None,
            "last_error": self._last_error,
            "time": now.isoformat(),
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
