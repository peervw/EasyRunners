from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any, cast
from urllib.parse import quote

import httpx
import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from runner_manager.auth import AuthManager
from runner_manager.database import Database
from runner_manager.demand import DemandTracker
from runner_manager.github import GitHubClient, GitHubConnectionStore
from runner_manager.metrics import WEBHOOK_FAILURES
from runner_manager.models import (
    GitHubConnectRequest,
    GitHubScope,
    GitHubSetupRequest,
    PoolYamlRequest,
    RunnerPoolConfig,
    ScaleRequest,
    TokenCreateRequest,
)
from runner_manager.scheduler import Scheduler
from runner_manager.workflows import workflow_for

router = APIRouter()


@dataclass(frozen=True)
class AuthContext:
    kind: str
    session: dict[str, Any] | None = None


def _auth(request: Request) -> AuthManager:
    return cast(AuthManager, request.app.state.auth)


def _database(request: Request) -> Database:
    return cast(Database, request.app.state.database)


def _scheduler(request: Request) -> Scheduler:
    return cast(Scheduler, request.app.state.scheduler)


def _github(request: Request) -> GitHubClient:
    return cast(GitHubClient, request.app.state.github)


def _store(request: Request) -> GitHubConnectionStore:
    return cast(GitHubConnectionStore, request.app.state.github_store)


def require_auth(request: Request) -> AuthContext:
    auth = _auth(request)
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer ") and auth.verify_api_token(authorization[7:]):
        return AuthContext("token")
    session = auth.verify_session(request.cookies.get(auth.cookie_name))
    if session:
        if auth.must_change_password:
            raise HTTPException(status_code=403, detail="change the bootstrap password first")
        return AuthContext("session", session)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")


def require_mutation(
    request: Request, context: Annotated[AuthContext, Depends(require_auth)]
) -> AuthContext:
    if context.kind == "session":
        csrf = request.headers.get("X-CSRF-Token")
        if not context.session or not _auth(request).verify_csrf(context.session, csrf):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
    return context


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
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return ()


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
    credentials = _store(request).credentials(require_installation=False)
    return request.app.state.templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "csrf": session["csrf"],
            "must_change": _auth(request).must_change_password,
            "connection": credentials.connection if credentials else None,
            "webhook_enabled": request.app.state.settings.webhook_enabled,
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
    jobs = await request.app.state.demand.snapshot()
    return [job.model_dump(mode="json") for job in jobs]


@router.get("/api/pools")
async def api_pools(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> dict[str, Any]:
    return cast(dict[str, Any], (await _scheduler(request).status())["pools"])


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


@router.post("/api/pools/{pool}/scale")
async def api_scale(
    request: Request,
    pool: str,
    body: ScaleRequest,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> dict[str, Any]:
    try:
        await _scheduler(request).set_manual_floor(pool, body.desired, body.ttl_seconds)
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
    credentials = _store(request).credentials(require_installation=False)
    installed = bool(credentials and credentials.connection.installation_id)
    docker_ok = await request.app.state.docker.ping()
    images: dict[str, bool] = {}
    for name, pool in settings.runner_pools.items():
        image = settings.image_for_pool(pool)
        images[name] = await request.app.state.docker.image_exists(image)
    scheduler_status = await _scheduler(request).status()
    github_ok = installed and scheduler_status["github"] == "connected"
    webhook_enabled = bool(
        credentials and credentials.connection.webhook_enabled
    ) or (not credentials and settings.webhook_enabled)
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
            "detail": "App installation authenticated" if installed else "Setup not completed",
        },
        "webhook": {
            "ok": not webhook_enabled
            or bool(_database(request).get_setting("webhook_last_received_at")),
            "optional": True,
            "detail": (
                _database(request).get_setting("webhook_last_received_at")
                if webhook_enabled
                else "Disabled; periodic polling repairs demand"
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
) -> dict[str, Any]:
    if not _store(request).credentials():
        raise HTTPException(status_code=409, detail="complete the GitHub App installation first")
    try:
        return await _scheduler(request).test_runner(pool)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown pool") from exc


@router.get("/api/version")
async def api_version(
    request: Request, _: Annotated[AuthContext, Depends(require_auth)]
) -> dict[str, Any]:
    try:
        manager_version = version("easy-runners")
    except PackageNotFoundError:
        manager_version = "0.1.0-dev"
    current = request.app.state.settings.runner_version
    latest = await _github(request).latest_runner_version()
    return {
        "manager": manager_version,
        "runner": current,
        "latest_runner": latest,
        "update_available": bool(
            latest and _version_parts(latest) > _version_parts(current)
        ),
    }


@router.get("/api/pools/{pool}/workflow")
async def api_workflow(
    request: Request,
    pool: str,
    _: Annotated[AuthContext, Depends(require_auth)],
    template: str = "python",
) -> dict[str, Any]:
    config = _scheduler(request).settings.runner_pools.get(pool)
    if not config:
        raise HTTPException(status_code=404, detail="unknown pool")
    try:
        content = workflow_for(pool, config, template)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail="unknown workflow template") from exc
    credentials = _store(request).credentials(require_installation=False)
    create_url = None
    if credentials and credentials.connection.scope == GitHubScope.REPO:
        create_url = (
            f"{_github(request).settings.github_web_url}/"
            f"{credentials.connection.target_name}/actions/new"
        )
    return {"filename": f"{template}.yml", "yaml": content, "create_url": create_url}


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
    token, record = _auth(request).create_api_token(body.name)
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
    credentials = _store(request).credentials(require_installation=False)
    return {
        "configured": bool(credentials),
        "installed": bool(credentials and credentials.connection.installation_id),
        "connection": credentials.connection.model_dump(mode="json") if credentials else None,
    }


@router.post("/api/github/setup/manifest")
async def github_manifest(
    request: Request,
    setup_request: GitHubConnectRequest | GitHubSetupRequest,
    context: Annotated[AuthContext, Depends(require_mutation)],
) -> dict[str, Any]:
    if context.kind != "session":
        raise HTTPException(status_code=403, detail="setup requires a browser session")
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
async def github_installed(request: Request, installation_id: int) -> Response:
    if not _auth(request).verify_session(request.cookies.get(_auth(request).cookie_name)):
        return _login_redirect(request)
    try:
        await _github(request).validate_installation(installation_id)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail="GitHub could not validate the App installation"
        ) from exc
    _spawn(request, _scheduler(request).poll_demand(full=True))
    return RedirectResponse("/?connected=1", status_code=303)


@router.get("/setup/github/resume")
async def github_resume(request: Request) -> Response:
    if not _auth(request).verify_session(request.cookies.get(_auth(request).cookie_name)):
        return _login_redirect(request)
    credentials = _store(request).credentials(require_installation=False)
    if not credentials:
        return RedirectResponse("/?setup=missing", status_code=303)
    connection = credentials.connection
    if connection.installation_id:
        return RedirectResponse("/?connected=1", status_code=303)
    if not connection.app_slug:
        return RedirectResponse("/?setup=incomplete", status_code=303)
    url = f"{_github(request).settings.github_web_url}/apps/{connection.app_slug}/installations/new"
    return RedirectResponse(url, status_code=303)


@router.post("/api/github/disconnect")
async def github_disconnect(
    request: Request,
    _: Annotated[AuthContext, Depends(require_mutation)],
) -> Response:
    _store(request).disconnect()
    _github(request).auth.invalidate()
    return Response(status_code=204)


@router.post("/webhooks/github")
async def github_webhook(request: Request) -> JSONResponse:
    credentials = _store(request).credentials(require_installation=False)
    if not credentials or not credentials.webhook_secret:
        WEBHOOK_FAILURES.labels(reason="not_configured").inc()
        raise HTTPException(status_code=503, detail="webhooks are not configured")
    if not credentials.connection.webhook_enabled:
        raise HTTPException(status_code=404, detail="webhooks are disabled")
    body = await request.body()
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
    if not _database(request).claim_delivery(delivery):
        return JSONResponse({"accepted": True, "duplicate": True})
    if request.headers.get("X-GitHub-Event") != "workflow_job":
        return JSONResponse({"accepted": True, "ignored": True})
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        WEBHOOK_FAILURES.labels(reason="invalid_json").inc()
        raise HTTPException(status_code=400, detail="invalid JSON payload") from exc
    connection = credentials.connection
    installation = payload.get("installation", {}).get("id")
    if connection.installation_id and int(installation or 0) != connection.installation_id:
        WEBHOOK_FAILURES.labels(reason="installation").inc()
        raise HTTPException(status_code=403, detail="webhook installation does not match")
    repository = str(payload.get("repository", {}).get("full_name", ""))
    if (
        connection.scope == GitHubScope.REPO
        and repository.lower() != connection.target_name.lower()
    ):
        raise HTTPException(status_code=403, detail="webhook repository does not match")
    if connection.scope == GitHubScope.ORG and not repository.lower().startswith(
        f"{connection.organization or connection.owner}/".lower()
    ):
        raise HTTPException(status_code=403, detail="webhook organization does not match")
    _database(request).set_setting("webhook_last_received_at", datetime.now(UTC).isoformat())
    tracker: DemandTracker = request.app.state.demand
    job = await tracker.handle_webhook(payload)
    if job and job.status == "queued" and job.pool:
        _spawn(request, _scheduler(request).reconcile("webhook"))
    return JSONResponse({"accepted": True, "matched_pool": job.pool if job else None})


@router.get("/metrics")
async def metrics(_: Annotated[AuthContext, Depends(require_auth)]) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
