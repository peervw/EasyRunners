from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from runner_manager.api import router
from runner_manager.auth import AuthManager
from runner_manager.config import Settings, load_settings, migrate_legacy_pools
from runner_manager.database import Database
from runner_manager.demand import DemandTracker
from runner_manager.docker import DockerRunnerManager
from runner_manager.github import GitHubClientRegistry, GitHubConnectionStore
from runner_manager.models import DiagnosticSettings, RunnerPoolConfig
from runner_manager.notifications import NotificationManager
from runner_manager.scheduler import Scheduler


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper()), format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def create_app(settings: Settings | None = None, *, start_scheduler: bool = True) -> FastAPI:
    configured = settings or load_settings()
    configured.assert_production_safe()
    configure_logging(configured.log_level)
    package_dir = Path(__file__).parent
    asset_version = hashlib.sha256(
        b"".join(
            (package_dir / "static" / name).read_bytes()
            for name in ("app.css", "easy.css", "theme.js", "app.js")
        )
    ).hexdigest()[:12]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configured.data_dir.mkdir(parents=True, exist_ok=True)
        database = Database(configured.data_dir / "easyrunners.sqlite3", configured.history_limit)
        if saved_pools := database.get_setting("runner_pools_override"):
            try:
                saved_pool_config = {
                    name: RunnerPoolConfig.model_validate(pool)
                    for name, pool in json.loads(saved_pools).items()
                }
                configured.runner_pools, migrations = migrate_legacy_pools(
                    saved_pool_config
                )
                if migrations:
                    if not database.get_setting("runner_pools_override_pre_standard"):
                        database.set_setting(
                            "runner_pools_override_pre_standard", saved_pools
                        )
                    migrated_payload = {
                        name: pool.model_dump(mode="json")
                        for name, pool in configured.runner_pools.items()
                    }
                    database.set_setting(
                        "runner_pools_override", json.dumps(migrated_payload)
                    )
                    structlog.get_logger().info(
                        "config.runner_pools_migrated", changes=migrations
                    )
            except (TypeError, ValueError) as exc:
                structlog.get_logger().error("config.saved_pools_invalid", error=str(exc))
        if saved_diagnostics := database.get_setting("diagnostic_settings"):
            try:
                diagnostics = DiagnosticSettings.model_validate_json(saved_diagnostics)
                configured.runner_log_capture_enabled = diagnostics.capture_enabled
                configured.runner_log_cleanup_enabled = diagnostics.cleanup_enabled
                configured.runner_log_retention_days = diagnostics.retention_days
            except ValueError as exc:
                structlog.get_logger().error(
                    "config.saved_diagnostic_settings_invalid", error=str(exc)
                )
        auth = AuthManager(configured, database)
        store = GitHubConnectionStore(configured, database)
        github = GitHubClientRegistry(configured, store)
        docker = DockerRunnerManager(configured)
        demand = DemandTracker(configured.runner_pools, database)
        notifications = NotificationManager(configured)
        scheduler = Scheduler(configured, github, docker, demand, notifications)
        app.state.settings = configured
        app.state.database = database
        app.state.auth = auth
        app.state.github_store = store
        app.state.github = github
        app.state.docker = docker
        app.state.demand = demand
        app.state.notifications = notifications
        app.state.scheduler = scheduler
        if start_scheduler:
            scheduler.start()
        try:
            yield
        finally:
            await scheduler.shutdown()
            tasks = list(app.state.background_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await github.close()
            await notifications.close()
            await docker.close()
            database.close()

    try:
        manager_version = version("easy-runners")
    except PackageNotFoundError:
        manager_version = "0.2.0-dev"
    app = FastAPI(
        title="EasyRunners",
        version=manager_version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.background_tasks = set()
    app.state.templates = Jinja2Templates(directory=package_dir / "templates")
    app.state.templates.env.globals["asset_version"] = asset_version
    app.mount("/static", StaticFiles(directory=package_dir / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://github.com"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/static/"):
            if request.query_params.get("v") == asset_version:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
        elif response.headers.get("Content-Type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        if configured.public_url.startswith("https://"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    app.include_router(router)
    return app
