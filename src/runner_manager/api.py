from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import quote

import httpx
import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from runner_manager.auth import AuthManager
from runner_manager.database import Database
from runner_manager.demand import DemandTracker
from runner_manager.github import GitHubClientRegistry, GitHubConnectionStore
from runner_manager.metrics import WEBHOOK_FAILURES
from runner_manager.models import (
    DiagnosticSettings,
    GitHubConnectRequest,
    GitHubScope,
    GitHubSetupRequest,
    PoolYamlRequest,
    RunnerPoolConfig,
    ScaleRequest,
    TokenCreateRequest,
    TokenScope,
)
from runner_manager.notifications import NotificationManager
from runner_manager.scheduler import Scheduler

router = APIRouter()


@dataclass(frozen=True)
class AuthContext:
    kind: str
    session: dict[str, Any] | None = None
    token_scope: str | None = None


def _auth(request: Request) -> AuthManager:
    return cast(AuthManager, request.app.state.auth)


def _database(request: Request) -> Database:
    return cast(Database, request.app.state.database)


def _scheduler(request: Request) -> Scheduler:
    return cast(Scheduler, request.app.state.scheduler)


def _github(request: Request) -> GitHubClientRegistry:
    return cast(GitHubClientRegistry, request.app.state.github)


def _store(request: Request) -> GitHubConnectionStore:
    return cast(GitHubConnectionStore, request.app.state.github_store)


def _notifications(request: Request) -> NotificationManager:
    return cast(NotificationManager, request.app.state.notifications)


def require_auth(request: Request) -> AuthContext:
    auth = _auth(request)
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        record = auth.authenticate_api_token(authorization[7:])
        if record:
            scope = str(record.get("scope") or "manage")
            if scope == "metrics":
                raise HTTPException(status_code=403, detail="token is restricted to metrics")
            return AuthContext("token", token_scope=scope)
    session = auth.verify_session(request.cookies.get(auth.cookie_name))
    if session:
        if auth.must_change_password:
            raise HTTPException(status_code=403, detail="change the bootstrap password first")
        return AuthContext("session", session)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")


def require_mutation(
    request: Request, context: Annotated[AuthContext, Depends(require_auth)]
) -> AuthContext:
    if context.kind == "token" and context.token_scope != TokenScope.MANAGE.value:
        raise HTTPException(status_code=403, detail="token does not have manage scope")
    if context.kind == "session":
        csrf = request.headers.get("X-CSRF-Token")
        if not context.session or not _auth(request).verify_csrf(context.session, csrf):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
    return context


def require_metrics(request: Request) -> AuthContext:
    auth = _auth(request)
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        record = auth.authenticate_api_token(authorization[7:])
        if record:
            return AuthContext("token", token_scope=str(record.get("scope") or "manage"))
    session = auth.verify_session(request.cookies.get(auth.cookie_name))
    if session and not auth.must_change_password:
        return AuthContext("session", session)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")


def _spawn(request: Request, coroutine: Coroutine[Any, Any, Any]) -> None:
    task = asyncio.create_task(coroutine)
    request.app.state.background_tasks.add(task)
    task.add_done_callback(request.app.state.background_tasks.discard)


def _save_pools(request: Request, pools: dict[str, RunnerPoolConfig]) -> None:
    payload = {name: pool.model_dump(mode="json") for name, pool in pools.items()}
    _database(request).set_setting("runner_pools_override", json.dumps(payload))


def require_session(request: Request) -> dict[str, Any]:
    session = _auth(request).verify_session(request.cookies.get(_auth(request).cookie_name))
    if not session:
        raise HTTPException(status_code=401, detail="browser session required")
    return session


def _safe_next(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else "/"


def _version_parts(value: str) -> tuple[int, ...]:
    match = re.search(r"\d+(?:\.\d+)*", value)
    return tuple(int(part) for part in match.group().split(".")) if match else ()


def _diagnostic_files(request: Request) -> list[Path]:
    directory = request.app.state.settings.data_dir / "runner-logs"
    if not directory.exists():
        return []
    return [
        path
        for path in directory.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and path.name.endswith((".tar", ".log"))
    ]


def _diagnostic_settings(request: Request) -> DiagnosticSettings:
    settings = request.app.state.settings
    return DiagnosticSettings(
        capture_enabled=settings.runner_log_capture_enabled,
        cleanup_enabled=settings.runner_log_cleanup_enabled,
        retention_days=settings.runner_log_retention_days,
    )


def _diagnostic_summary(request: Request) -> dict[str, Any]:
    files = _diagnostic_files(request)
    oldest = min((path.stat().st_mtime for path in files), default=None)
    return {
        **_diagnostic_settings(request).model_dump(),
        "file_count": len(files),
        "total_size": sum(path.stat().st_size for path in files),
        "oldest_at": datetime.fromtimestamp(oldest, UTC).isoformat() if oldest else None,
    }


def _login_redirect(request: Request) -> RedirectResponse:
    destination = request.url.path
    if request.url.query:
        destination += f"?{request.url.query}"
    return RedirectResponse(
        f"/auth/login?next={quote(destination, safe='')}", status_code=303
    )


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(
    request: Request, next_url: Annotated[str, Query(alias="next")] = "/"
) -> Any:
    next_url = _safe_next(next_url)
    if _auth(request).verify_session(request.cookies.get(_auth(request).cookie_name)):
        return RedirectResponse(next_url, status_code=303)
    return request.app.state.templates.TemplateResponse(
        request, "login.html", {"next_url": next_url}
    )


@router.post("/auth/login")
async def login(
    request: Request,
    password: Annotated[str, Form()],
    next_url: Annotated[str, Form()] = "/",
) -> Response:
    auth = _auth(request)
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    if not auth.login_allowed(client, now):
        raise HTTPException(status_code=429, detail="too many login attempts")
    if not auth.verify_password(password):
        auth.record_login_failure(client, now)
        return cast(
            Response,
            request.app.state.templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Invalid password", "next_url": _safe_next(next_url)},
                status_code=401,
            ),
        )
    auth.clear_login_failures(client)
    token, _ = auth.create_session()
    response = RedirectResponse(_safe_next(next_url), status_code=303)
    response.set_cookie(
        auth.cookie_name,
        token,
        max_age=auth.settings.session_ttl_seconds,
        httponly=True,
        secure=auth.settings.public_url.startswith("https://"),
        samesite="lax",
        path="/",
    )
    return response


@router.post("/auth/logout")
async def logout(
    request: Request,
    csrf_token: Annotated[str, Form()],
) -> Response:
    session = require_session(request)
    if not _auth(request).verify_csrf(session, csrf_token):
        raise HTTPException(status_code=403, detail="invalid CSRF token")
    response = RedirectResponse("/auth/login", status_code=303)
    response.delete_cookie(_auth(request).cookie_name, path="/")
    return response


@router.post("/auth/password")
async def change_password(
    request: Request,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
) -> Response:
    session = require_session(request)
    auth = _auth(request)
    if not auth.verify_csrf(session, csrf_token):
        raise HTTPException(status_code=403, detail="invalid CSRF token")
    try:
        auth.change_password(current_password, new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = RedirectResponse("/auth/login", status_code=303)
    response.delete_cookie(auth.cookie_name, path="/")
    return response


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> Any:
    session = _auth(request).verify_session(request.cookies.get(_auth(request).cookie_name))
    if not session:
        return RedirectResponse("/auth/login", status_code=303)
    connections = _store(request).connections()
    connection = connections[0] if len(connections) == 1 else None
    return request.app.state.templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "csrf": session["csrf"],
            "must_change": _auth(request).must_change_password,
            "connection": connection,
            "connections": connections,
            "webhook_enabled": request.app.state.settings.webhook_enabled,
            "github_onboarding_enabled": request.app.state.settings.github_auth_mode
            in {"onboarding", "auto"},
        },
    )


@router.get("/api/status")
async def api_status(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> dict[str, Any]:
    return await _scheduler(request).status()


@router.get("/api/runners")
async def api_runners(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> list[dict[str, Any]]:
    return await _scheduler(request).runner_views()


@router.get("/api/jobs")
async def api_jobs(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> list[dict[str, Any]]:
    return await _scheduler(request).job_views()


@router.get("/api/pools")
async def api_pools(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> dict[str, Any]:
    return cast(dict[str, Any], (await _scheduler(request).status())["pools"])


@router.get("/api/repositories/adoption")
async def api_repository_adoption(
    request: Request,
    _: Annotated[AuthContext, Depends(require_auth)],
) -> dict[str, Any]:
    if not _store(request).all_credentials():
        return {
            "repositories": [],
            "repository_count_total": 0,
            "repository_count_scanned": 0,
            "scan": {
                "scanning": False,
                "completed": 0,
                "total": 0,
                "started_at": None,
                "error": None,
            },
            "scanned_at": None,
            "recommended_pool": None,
            "recommended_runs_on": None,
            "replacements": {},
        }
    return await _github(request).repository_adoption(
        _scheduler(request).settings.runner_pools,
        refresh=False,
        wait=False,
    )


@router.post("/api/repositories/adoption/scan")
async def api_scan_repository_adoption(
    request: Request,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> dict[str, Any]:
    if not _store(request).all_credentials():
        raise HTTPException(status_code=409, detail="GitHub is not connected")
    return await _github(request).repository_adoption(
        _scheduler(request).settings.runner_pools,
        refresh=True,
        wait=False,
    )


@router.get("/api/pools/config.yaml")
async def api_pool_yaml(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> Response:
    pools = {
        name: pool.model_dump(mode="json", exclude_none=True)
        for name, pool in _scheduler(request).settings.runner_pools.items()
    }
    return Response(
        yaml.safe_dump({"runner_pools": pools}, sort_keys=False),
        media_type="application/yaml",
    )


@router.put("/api/pools/config")
async def api_import_pools(
    request: Request,
    body: PoolYamlRequest,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(body.yaml) or {}
        values = raw.get("runner_pools", raw) if isinstance(raw, dict) else None
        if not isinstance(values, dict):
            raise ValueError("YAML must contain a runner_pools mapping")
        pools = {
            str(name): RunnerPoolConfig.model_validate(value) for name, value in values.items()
        }
        await _scheduler(request).replace_pools(pools)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _save_pools(request, pools)
    return await _scheduler(request).status()


@router.put("/api/pools/{pool}")
async def api_put_pool(
    request: Request,
    pool: str,
    body: RunnerPoolConfig,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> dict[str, Any]:
    pools = dict(_scheduler(request).settings.runner_pools)
    pools[pool] = body
    try:
        await _scheduler(request).replace_pools(pools)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _save_pools(request, pools)
    return await _scheduler(request).status()


@router.delete("/api/pools/{pool}")
async def api_delete_pool(
    request: Request,
    pool: str,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> Response:
    pools = dict(_scheduler(request).settings.runner_pools)
    if pool not in pools:
        raise HTTPException(status_code=404, detail="unknown pool")
    pools.pop(pool)
    try:
        await _scheduler(request).replace_pools(pools)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _save_pools(request, pools)
    return Response(status_code=204)


@router.get("/api/history")
async def api_history(
    request: Request,
    _: Annotated[AuthContext, Depends(require_auth)],
    limit: int = 100,
) -> list[dict[str, Any]]:
    return _database(request).list_history(max(1, min(limit, 500)))


@router.get("/api/usage")
async def api_usage(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> dict[str, dict[str, Any]]:
    return _database(request).usage_summary()


@router.get("/api/settings/diagnostics")
async def api_diagnostic_settings(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> dict[str, Any]:
    return _diagnostic_summary(request)


@router.put("/api/settings/diagnostics")
async def api_update_diagnostic_settings(
    request: Request,
    body: DiagnosticSettings,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> dict[str, Any]:
    settings = request.app.state.settings
    settings.runner_log_capture_enabled = body.capture_enabled
    settings.runner_log_cleanup_enabled = body.cleanup_enabled
    settings.runner_log_retention_days = body.retention_days
    _database(request).set_setting("diagnostic_settings", body.model_dump_json())
    if body.cleanup_enabled:
        await request.app.state.docker.prune_logs()
    return _diagnostic_summary(request)


@router.get("/api/diagnostics")
async def api_diagnostics(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> list[dict[str, Any]]:
    files = _diagnostic_files(request)
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
        }
        for path in files[:100]
    ]


@router.delete("/api/diagnostics")
async def api_clear_diagnostics(
    request: Request,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> dict[str, int]:
    files = _diagnostic_files(request)
    released = sum(path.stat().st_size for path in files)
    for path in files:
        path.unlink()
    return {"deleted": len(files), "released_bytes": released}


@router.get("/api/diagnostics/{name}")
async def api_diagnostic_download(
    request: Request,
    name: str,
    _: Annotated[AuthContext, Depends(require_auth)],
) -> FileResponse:
    if name != Path(name).name or not name.endswith((".tar", ".log")):
        raise HTTPException(status_code=404, detail="diagnostic archive not found")
    path = request.app.state.settings.data_dir / "runner-logs" / name
    if not path.is_file() or path.is_symlink():
        raise HTTPException(status_code=404, detail="diagnostic archive not found")
    return FileResponse(path, filename=name, media_type="application/octet-stream")


@router.post("/api/pools/{pool}/scale")
async def api_scale(
    request: Request,
    pool: str,
    body: ScaleRequest,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> dict[str, Any]:
    try:
        await _scheduler(request).set_manual_floor(
            pool,
            body.desired,
            body.ttl_seconds,
            body.repository,
            body.connection_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown pool") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _scheduler(request).status()


@router.post("/api/reconcile")
async def api_reconcile(
    request: Request,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> dict[str, Any]:
    await _scheduler(request).poll_demand(full=True)
    return await _scheduler(request).reconcile("api")


@router.get("/api/readiness")
async def api_readiness(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> dict[str, Any]:
    settings = request.app.state.settings
    connections = _store(request).connections()
    installed_connections = [
        connection for connection in connections if connection.installation_id
    ]
    installed = bool(installed_connections)
    docker_ok = await request.app.state.docker.ping()
    images: dict[str, bool] = {}
    for name, pool in settings.runner_pools.items():
        image = settings.image_for_pool(pool)
        images[name] = await request.app.state.docker.image_exists(image)
    scheduler_status = await _scheduler(request).status()
    github_ok = installed and scheduler_status["github"] == "connected"
    webhook_enabled = any(
        connection.webhook_enabled for connection in installed_connections
    ) or (not connections and settings.webhook_enabled)
    checks = {
        "public_url": {
            "ok": not webhook_enabled or settings.public_url.startswith("https://"),
            "detail": "HTTPS callback and webhook URL" if webhook_enabled else "Polling-only mode",
        },
        "docker": {"ok": docker_ok, "detail": "Docker Engine reachable"},
        "runner_images": {
            "ok": bool(images) and all(images.values()),
            "detail": images,
        },
        "github": {
            "ok": github_ok,
            "detail": (
                f"{len(installed_connections)} App installation(s) authenticated"
                if installed
                else "Setup not completed"
            ),
        },
        "webhook": {
            "ok": not webhook_enabled
            or any(
                _database(request).get_setting(
                    f"webhook_last_received_at:{connection.id}"
                )
                for connection in installed_connections
                if connection.webhook_enabled
            ),
            "optional": True,
            "detail": (
                max(
                    [
                        value
                        for connection in installed_connections
                        if connection.webhook_enabled
                        and (
                            value := _database(request).get_setting(
                                f"webhook_last_received_at:{connection.id}"
                            )
                        )
                    ],
                    default=None,
                )
                if webhook_enabled
                else "Disabled; periodic polling repairs demand"
            ),
        },
        "notifications": {
            "ok": _notifications(request).configured,
            "optional": True,
            "detail": (
                "Failure webhook configured"
                if _notifications(request).configured
                else "Optional failure webhook is not configured"
            ),
        },
    }
    required = [item for item in checks.values() if not item.get("optional")]
    return {"ready": all(bool(item["ok"]) for item in required), "checks": checks}


@router.post("/api/readiness/test-runner")
async def api_test_runner(
    request: Request,
    _: Annotated[AuthContext, Depends(require_mutation)],
    pool: str | None = None,
    repository: str | None = None,
    connection_id: str | None = None,
) -> dict[str, Any]:
    if not _store(request).all_credentials():
        raise HTTPException(status_code=409, detail="complete the GitHub App installation first")
    try:
        return await _scheduler(request).test_runner(pool, repository, connection_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown pool") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/version")
async def api_version(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> dict[str, Any]:
    try:
        manager_version = version("easy-runners")
    except PackageNotFoundError:
        manager_version = "0.2.0-dev"
    current = request.app.state.settings.runner_version
    latest, manager_release = await asyncio.gather(
        _github(request).latest_runner_version(),
        _github(request).latest_manager_release(),
    )
    latest_manager = str(manager_release["version"]) if manager_release else None
    runner_update = bool(latest and _version_parts(latest) > _version_parts(current))
    manager_update = bool(
        latest_manager
        and _version_parts(latest_manager) > _version_parts(manager_version)
    )
    return {
        "manager": manager_version,
        "latest_manager": latest_manager,
        "latest_manager_release": manager_release,
        "runner": current,
        "latest_runner": latest,
        "manager_update_available": manager_update,
        "runner_update_available": runner_update,
        "update_available": manager_update or runner_update,
        "source_update_command": "git pull --ff-only && docker compose up -d --build",
        "manager_release_url": (
            f"{request.app.state.settings.github_web_url}/"
            f"{request.app.state.settings.manager_repository}/releases"
        ),
        "runner_release_url": "https://github.com/actions/runner/releases",
        "checked_at": datetime.now(UTC).isoformat(),
    }


@router.get("/api/notifications")
async def api_notifications(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> dict[str, Any]:
    return _notifications(request).status()


@router.post("/api/notifications/test")
async def api_test_notification(
    request: Request,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> dict[str, bool]:
    if not _notifications(request).configured:
        raise HTTPException(status_code=409, detail="notification webhook is not configured")
    delivered = await _notifications(request).send(
        "test",
        "EasyRunners test notification",
        "Your failure webhook is configured correctly.",
        details={"trigger": "dashboard"},
        force=True,
    )
    if not delivered:
        raise HTTPException(status_code=502, detail="notification webhook delivery failed")
    return {"delivered": True}


@router.get("/api/auth/tokens")
async def api_tokens(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> list[dict[str, Any]]:
    return _database(request).list_api_tokens()


@router.post("/api/auth/tokens")
async def api_create_token(
    request: Request,
    body: TokenCreateRequest,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> dict[str, Any]:
    token, record = _auth(request).create_api_token(
        body.name,
        body.scope,
        body.expires_in_days,
    )
    return {**record, "token": token}


@router.delete("/api/auth/tokens/{token_id}")
async def api_delete_token(
    request: Request,
    token_id: str,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> Response:
    if not _database(request).delete_api_token(token_id):
        raise HTTPException(status_code=404, detail="unknown token")
    return Response(status_code=204)


@router.get("/api/github")
async def api_github(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> dict[str, Any]:
    github = _github(request)
    repository_errors = github.repository_errors
    runner_errors = github.runner_errors
    queue_errors = github.queue_errors
    items: list[dict[str, Any]] = []
    all_repositories: list[str] = []
    for original in _store(request).connections():
        connection = original
        repositories: list[str] = []
        metadata_error: str | None = None
        repositories_error: str | None = None
        if connection.installation_id:
            try:
                repositories = github.cached_repositories(connection.id)
            except (KeyError, RuntimeError, ValueError) as exc:
                repositories_error = str(exc)
            repositories_error = repositories_error or repository_errors.get(connection.id)
        rate_limit = (
            github.rate_limit_status(connection.id)
            if connection.installation_id
            else {"remaining": None, "reset_at": None, "limited_until": None}
        )
        operational_error = (
            repositories_error
            or runner_errors.get(connection.id)
            or queue_errors.get(connection.id)
        )
        all_repositories.extend(repositories)
        configure_path = (
            f"/organizations/{quote(connection.owner)}/settings/installations"
            if connection.account_type == "organization"
            else "/settings/installations"
        )
        configure_url = (
            f"{_github(request).settings.github_web_url}{configure_path}/"
            f"{connection.installation_id}"
            if connection.installation_id
            else None
        )
        items.append(
            {
                "connection": connection.model_dump(mode="json"),
                "installed": bool(connection.installation_id),
                "repositories": repositories,
                "metadata_error": metadata_error,
                "repositories_error": repositories_error,
                "healthy": bool(connection.installation_id)
                and not operational_error
                and not rate_limit.get("limited_until"),
                "rate_limit": rate_limit,
                "configure_url": configure_url,
                "repository_bound": connection.scope == GitHubScope.REPO,
                "last_webhook_at": _database(request).get_setting(
                    f"webhook_last_received_at:{connection.id}"
                ),
            }
        )
    primary = items[0] if len(items) == 1 else None
    return {
        "configured": bool(items),
        "installed": any(item["installed"] for item in items),
        "connections": items,
        "connection": primary["connection"] if primary else None,
        "repositories": sorted(set(all_repositories), key=str.lower),
        "metadata_error": primary["metadata_error"] if primary else None,
        "repositories_error": primary["repositories_error"] if primary else None,
        "rate_limit": primary["rate_limit"] if primary else {"remaining": None},
        "configure_url": primary["configure_url"] if primary else None,
        "repository_bound": bool(primary and primary["repository_bound"]),
    }


@router.post("/api/github/connections/{connection_id}/refresh")
async def github_refresh_connection(
    request: Request,
    connection_id: str,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> dict[str, Any]:
    connection = _store(request).connection(connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail="unknown GitHub connection")
    if not connection.installation_id:
        raise HTTPException(status_code=409, detail="GitHub App installation is incomplete")
    try:
        connection = await _github(request).refresh_installation_metadata(
            connection_id, refresh=True
        )
        repositories = await _github(request).list_repositories(
            connection_id=connection_id, refresh=True
        )
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "connection": connection.model_dump(mode="json"),
        "repositories": repositories,
        "rate_limit": _github(request).rate_limit_status(connection_id),
    }


@router.post("/api/github/setup/manifest")
async def github_manifest(
    request: Request,
    setup_request: GitHubConnectRequest | GitHubSetupRequest,
    context: Annotated[AuthContext, Depends(require_mutation)],
) -> dict[str, Any]:
    if context.kind != "session":
        raise HTTPException(status_code=403, detail="setup requires a browser session")
    if _github(request).settings.github_auth_mode not in {"onboarding", "auto"}:
        raise HTTPException(
            status_code=409,
            detail="GitHub onboarding is disabled by GITHUB_AUTH_MODE",
        )
    if isinstance(setup_request, GitHubConnectRequest):
        try:
            setup = await _github(request).resolve_setup(setup_request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail="GitHub could not inspect that URL; try again"
            ) from exc
    else:
        setup = setup_request
    settings = _github(request).settings
    if (
        setup.webhook_enabled
        and not settings.allow_insecure_public_url
        and not settings.public_url.startswith("https://")
    ):
        raise HTTPException(
            status_code=422,
            detail="instant webhooks require an HTTPS PUBLIC_URL; choose polling-only mode",
        )
    state = __import__("secrets").token_urlsafe(32)
    _database(request).create_setup_state(state, setup.model_dump(mode="json"))
    manifest = _github(request).build_manifest(setup)
    if setup.app_owner_kind == "organization":
        action = (
            f"{_github(request).settings.github_web_url}/organizations/"
            f"{quote(setup.owner)}/settings/apps/new"
        )
    else:
        action = f"{_github(request).settings.github_web_url}/settings/apps/new"
    return {"action": f"{action}?state={quote(state)}", "manifest": manifest}


@router.get("/setup/github/callback")
async def github_callback(request: Request, code: str, state: str) -> Response:
    if not _auth(request).verify_session(request.cookies.get(_auth(request).cookie_name)):
        return _login_redirect(request)
    raw = _database(request).consume_setup_state(state)
    if not raw:
        raise HTTPException(
            status_code=400, detail="setup state is invalid, expired, or already used"
        )
    setup = GitHubSetupRequest.model_validate(raw)
    try:
        connection = await _github(request).convert_manifest(code, setup)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail="GitHub could not complete the App registration"
        ) from exc
    if not connection.app_slug:
        raise HTTPException(status_code=502, detail="GitHub did not return an App slug")
    install_url = (
        f"{_github(request).settings.github_web_url}/apps/{connection.app_slug}/installations/new"
    )
    return RedirectResponse(install_url, status_code=303)


@router.get("/setup/github/installed")
async def github_installed(
    request: Request, installation_id: int, connection_id: str
) -> Response:
    if not _auth(request).verify_session(request.cookies.get(_auth(request).cookie_name)):
        return _login_redirect(request)
    try:
        await _github(request).validate_installation(connection_id, installation_id)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail="unknown GitHub setup") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail="GitHub could not validate the App installation"
        ) from exc
    _spawn(request, _scheduler(request).poll_demand(full=True))
    return RedirectResponse("/?connected=1", status_code=303)


@router.get("/setup/github/resume")
async def github_resume(request: Request, connection_id: str | None = None) -> Response:
    if not _auth(request).verify_session(request.cookies.get(_auth(request).cookie_name)):
        return _login_redirect(request)
    credentials = _store(request).credentials(
        connection_id=connection_id, require_installation=False
    )
    if not credentials:
        return RedirectResponse("/?setup=missing", status_code=303)
    connection = credentials.connection
    if connection.installation_id:
        return RedirectResponse("/?connected=1", status_code=303)
    if not connection.app_slug:
        return RedirectResponse("/?setup=incomplete", status_code=303)
    url = f"{_github(request).settings.github_web_url}/apps/{connection.app_slug}/installations/new"
    return RedirectResponse(url, status_code=303)


@router.post("/api/github/connections/{connection_id}/disconnect")
async def github_disconnect(
    request: Request,
    connection_id: str,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> Response:
    connection = _store(request).connection(connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail="unknown GitHub connection")
    if connection.source != "onboarding":
        raise HTTPException(
            status_code=409,
            detail="this GitHub connection is managed through environment configuration",
        )
    active_runners = [
        runner
        for runner in _scheduler(request).runners()
        if runner.get("connection_id") == connection_id
    ]
    active_jobs = [
        job
        for job in await request.app.state.demand.snapshot()
        if job.connection_id == connection_id
    ]
    if active_runners or active_jobs:
        raise HTTPException(
            status_code=409,
            detail=(
                "wait for this connection's active runners and jobs to finish before "
                "disconnecting it"
            ),
        )
    _store(request).disconnect(connection_id)
    _github(request).invalidate_connection_cache(connection_id)
    return Response(status_code=204)


@router.post("/api/github/disconnect")
async def github_disconnect_legacy(
    request: Request,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> Response:
    connections = _store(request).connections()
    if len(connections) != 1:
        raise HTTPException(
            status_code=409,
            detail="choose the GitHub connection to disconnect",
        )
    return await github_disconnect(request, connections[0].id, _)


@router.post("/webhooks/github")
async def github_webhook(request: Request) -> JSONResponse:
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        WEBHOOK_FAILURES.labels(reason="invalid_json").inc()
        raise HTTPException(status_code=400, detail="invalid JSON payload") from exc
    try:
        installation = int(payload.get("installation", {}).get("id") or 0)
    except (TypeError, ValueError):
        installation = 0
    connection = _store(request).find_by_installation(installation)
    credentials = (
        _store(request).credentials(
            connection_id=connection.id, require_installation=False
        )
        if connection
        else None
    )
    if not credentials or not credentials.webhook_secret:
        WEBHOOK_FAILURES.labels(reason="not_configured").inc()
        raise HTTPException(status_code=403, detail="unknown webhook installation")
    if not credentials.connection.webhook_enabled:
        raise HTTPException(status_code=404, detail="webhooks are disabled")
    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = (
        "sha256=" + hmac.new(credentials.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    )
    if not hmac.compare_digest(signature, expected):
        WEBHOOK_FAILURES.labels(reason="signature").inc()
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    delivery = request.headers.get("X-GitHub-Delivery")
    if not delivery:
        WEBHOOK_FAILURES.labels(reason="missing_delivery").inc()
        raise HTTPException(status_code=400, detail="missing delivery ID")
    if not _database(request).claim_delivery(
        delivery, connection_id=credentials.connection.id
    ):
        return JSONResponse({"accepted": True, "duplicate": True})
    if request.headers.get("X-GitHub-Event") != "workflow_job":
        return JSONResponse({"accepted": True, "ignored": True})
    connection = credentials.connection
    if connection.installation_id and installation != connection.installation_id:
        WEBHOOK_FAILURES.labels(reason="installation").inc()
        raise HTTPException(status_code=403, detail="webhook installation does not match")
    repository = str(payload.get("repository", {}).get("full_name", ""))
    if connection.scope == GitHubScope.REPO:
        if connection.auth_type == "app":
            parts = repository.split("/")
            if len(parts) != 2 or parts[0].lower() != connection.owner.lower():
                raise HTTPException(status_code=403, detail="webhook owner does not match")
        elif repository.lower() != connection.target_name.lower():
            raise HTTPException(status_code=403, detail="webhook repository does not match")
    if connection.scope == GitHubScope.ORG and not repository.lower().startswith(
        f"{connection.organization or connection.owner}/".lower()
    ):
        raise HTTPException(status_code=403, detail="webhook organization does not match")
    _database(request).set_setting(
        f"webhook_last_received_at:{connection.id}", datetime.now(UTC).isoformat()
    )
    tracker: DemandTracker = request.app.state.demand
    job = await tracker.handle_webhook(payload, connection.id)
    if job and job.status == "queued" and job.pool:
        _spawn(request, _scheduler(request).reconcile("webhook"))
    return JSONResponse({"accepted": True, "matched_pool": job.pool if job else None})


@router.get("/metrics")
async def metrics(_: Annotated[AuthContext, Depends(require_metrics)]) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
