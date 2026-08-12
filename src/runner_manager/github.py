from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import httpx
import jwt
import structlog
from pydantic import BaseModel

from runner_manager.config import Settings
from runner_manager.database import Database
from runner_manager.metrics import GITHUB_API_FAILURES, GITHUB_RATE_LIMIT_REMAINING
from runner_manager.models import (
    ARCHITECTURE_LABELS,
    DockerMode,
    GitHubConnectRequest,
    GitHubScope,
    GitHubSetupRequest,
    RunnerPoolConfig,
    WorkflowJob,
)

log = structlog.get_logger()


class GitHubConnection(BaseModel):
    auth_type: str
    scope: GitHubScope
    owner: str
    repository: str | None = None
    organization: str | None = None
    app_id: int | None = None
    installation_id: int | None = None
    app_slug: str | None = None
    source: str = "environment"
    webhook_enabled: bool = True
    repository_selection: str | None = None
    repositories_count: int | None = None

    @property
    def target_name(self) -> str:
        if self.scope == GitHubScope.ORG:
            return self.organization or self.owner
        if self.repository:
            return f"{self.owner}/{self.repository}"
        return self.owner


@dataclass(frozen=True)
class Credentials:
    connection: GitHubConnection
    private_key: str | None = None
    webhook_secret: str | None = None
    token: str | None = None


class GitHubRateLimitError(RuntimeError):
    pass


class GitHubConnectionStore:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.github_dir = settings.data_dir / "github"

    @staticmethod
    def _write_secret(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(value, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)

    def _environment_app(self) -> Credentials | None:
        s = self.settings
        if not (s.github_app_id and s.github_installation_id and s.github_scope):
            return None
        private_key: str | None = None
        if s.github_app_private_key_path:
            private_key = s.github_app_private_key_path.read_text(encoding="utf-8")
        elif s.github_app_private_key:
            private_key = s.github_app_private_key.get_secret_value().replace("\\n", "\n")
        if not private_key:
            return None
        owner = s.github_org if s.github_scope == GitHubScope.ORG else s.github_owner
        if not owner:
            return None
        connection = GitHubConnection(
            auth_type="app",
            scope=s.github_scope,
            owner=owner,
            organization=s.github_org,
            repository=s.github_repo,
            app_id=s.github_app_id,
            installation_id=s.github_installation_id,
            webhook_enabled=s.webhook_enabled,
        )
        secret = s.github_webhook_secret.get_secret_value() if s.github_webhook_secret else None
        return Credentials(connection, private_key=private_key, webhook_secret=secret)

    def _environment_pat(self) -> Credentials | None:
        s = self.settings
        if not (s.github_token and s.github_scope):
            return None
        owner = s.github_org if s.github_scope == GitHubScope.ORG else s.github_owner
        if not owner:
            return None
        connection = GitHubConnection(
            auth_type="pat",
            scope=s.github_scope,
            owner=owner,
            organization=s.github_org,
            repository=s.github_repo,
            webhook_enabled=False,
        )
        return Credentials(connection, token=s.github_token.get_secret_value())

    def _onboarded(self) -> Credentials | None:
        raw = self.database.get_setting("github_connection")
        pem_path = self.github_dir / "app.pem"
        secret_path = self.github_dir / "webhook.secret"
        if not raw or not pem_path.exists():
            return None
        connection = GitHubConnection.model_validate_json(raw)
        webhook = secret_path.read_text(encoding="utf-8").strip() if secret_path.exists() else None
        return Credentials(
            connection,
            private_key=pem_path.read_text(encoding="utf-8"),
            webhook_secret=webhook,
        )

    def credentials(self, *, require_installation: bool = True) -> Credentials | None:
        mode = self.settings.github_auth_mode
        choices: list[Credentials | None]
        if mode == "app":
            choices = [self._environment_app()]
        elif mode == "pat":
            choices = [self._environment_pat()]
        elif mode == "onboarding":
            choices = [self._onboarded()]
        else:
            choices = [self._onboarded(), self._environment_app(), self._environment_pat()]
        result = next((choice for choice in choices if choice is not None), None)
        if require_installation and result and result.connection.auth_type == "app":
            if not result.connection.installation_id:
                return None
        return result

    def save_manifest_result(
        self, setup: GitHubSetupRequest, manifest_result: dict[str, Any]
    ) -> GitHubConnection:
        self._write_secret(self.github_dir / "app.pem", str(manifest_result["pem"]))
        self._write_secret(
            self.github_dir / "webhook.secret", str(manifest_result["webhook_secret"])
        )
        connection = GitHubConnection(
            auth_type="app",
            scope=setup.scope,
            owner=setup.owner,
            repository=setup.repository,
            organization=setup.owner if setup.scope == GitHubScope.ORG else None,
            app_id=int(manifest_result["id"]),
            app_slug=str(manifest_result["slug"]),
            source="onboarding",
            webhook_enabled=setup.webhook_enabled,
        )
        self.database.set_setting("github_connection", connection.model_dump_json())
        self.database.delete_setting("webhook_last_received_at")
        return connection

    def save_installation(
        self,
        installation_id: int | None,
        *,
        repository_selection: str | None = None,
        repositories_count: int | None = None,
    ) -> GitHubConnection:
        credentials = self.credentials(require_installation=False)
        if not credentials:
            raise RuntimeError("GitHub App manifest has not been completed")
        updates: dict[str, Any] = {"installation_id": installation_id}
        if repository_selection is not None:
            updates["repository_selection"] = repository_selection
        if repositories_count is not None:
            updates["repositories_count"] = repositories_count
        connection = credentials.connection.model_copy(update=updates)
        self.database.set_setting("github_connection", connection.model_dump_json())
        return connection

    def update_repository_metadata(
        self, *, repository_selection: str | None = None, repositories_count: int | None = None
    ) -> GitHubConnection:
        credentials = self.credentials(require_installation=False)
        if not credentials:
            raise RuntimeError("GitHub is not connected")
        updates: dict[str, Any] = {}
        if repository_selection is not None:
            updates["repository_selection"] = repository_selection
        if repositories_count is not None:
            updates["repositories_count"] = repositories_count
        connection = credentials.connection.model_copy(update=updates)
        self.database.set_setting("github_connection", connection.model_dump_json())
        return connection

    def disconnect(self) -> None:
        self.database.delete_setting("github_connection")
        self.database.delete_setting("webhook_last_received_at")
        for name in ("app.pem", "webhook.secret"):
            path = self.github_dir / name
            if path.exists():
                path.unlink()


class GitHubAuth:
    def __init__(
        self,
        settings: Settings,
        store: GitHubConnectionStore,
        client: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self._installation_token: str | None = None
        self._installation_expires = datetime.min.replace(tzinfo=UTC)
        self._lock = asyncio.Lock()

    @staticmethod
    def app_jwt(app_id: int, private_key: str, now: int | None = None) -> str:
        timestamp = now or int(time.time())
        return jwt.encode(
            {"iat": timestamp - 60, "exp": timestamp + 540, "iss": str(app_id)},
            private_key,
            algorithm="RS256",
        )

    async def token(self, *, force_refresh: bool = False) -> str:
        credentials = self.store.credentials()
        if not credentials:
            raise RuntimeError("GitHub is not connected")
        if credentials.token:
            return credentials.token
        connection = credentials.connection
        if not (connection.app_id and connection.installation_id and credentials.private_key):
            raise RuntimeError("GitHub App installation is incomplete")
        async with self._lock:
            if (
                not force_refresh
                and self._installation_token
                and self._installation_expires > datetime.now(UTC) + timedelta(minutes=5)
            ):
                return self._installation_token
            app_token = self.app_jwt(connection.app_id, credentials.private_key)
            response = await self.client.post(
                f"{self.settings.github_api_url}/app/installations/"
                f"{connection.installation_id}/access_tokens",
                headers=self._headers(app_token),
            )
            response.raise_for_status()
            body = response.json()
            self._installation_token = str(body["token"])
            self._installation_expires = datetime.fromisoformat(
                str(body["expires_at"]).replace("Z", "+00:00")
            )
            return self._installation_token

    def invalidate(self) -> None:
        self._installation_token = None
        self._installation_expires = datetime.min.replace(tzinfo=UTC)

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": self.settings.github_api_version,
            "User-Agent": "EasyRunners/0.1",
        }


class GitHubClient:
    def __init__(
        self,
        settings: Settings,
        store: GitHubConnectionStore,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.http = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._owns_client = client is None
        self.auth = GitHubAuth(settings, store, self.http)
        self._latest_runner: tuple[float, str | None] = (0.0, None)
        self._latest_manager: tuple[float, dict[str, Any] | None] = (0.0, None)
        self._repositories: tuple[float, list[str]] = (0.0, [])
        self._adoption: tuple[float, dict[str, Any]] = (0.0, {})
        self._installation_metadata_at = 0.0
        self._rate_limited_until = 0.0
        self._rate_limit_remaining: int | None = None
        self._rate_limit_reset_at: datetime | None = None

    async def close(self) -> None:
        if self._owns_client:
            await self.http.aclose()

    def invalidate_connection_cache(self) -> None:
        self.auth.invalidate()
        self._repositories = (0.0, [])
        self._adoption = (0.0, {})
        self._installation_metadata_at = 0.0

    def _headers(self, token: str) -> dict[str, str]:
        return self.auth._headers(token)

    def rate_limit_status(self) -> dict[str, Any]:
        return {
            "remaining": self._rate_limit_remaining,
            "reset_at": self._rate_limit_reset_at.isoformat()
            if self._rate_limit_reset_at
            else None,
            "limited_until": datetime.fromtimestamp(
                self._rate_limited_until, UTC
            ).isoformat()
            if self._rate_limited_until > time.time()
            else None,
        }

    def _capture_rate_limit(self, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining is not None:
            try:
                self._rate_limit_remaining = int(remaining)
                GITHUB_RATE_LIMIT_REMAINING.set(self._rate_limit_remaining)
            except ValueError:
                pass
        if reset is not None:
            try:
                self._rate_limit_reset_at = datetime.fromtimestamp(int(reset), UTC)
            except ValueError:
                pass

    def _rate_limit_delay(self, response: httpx.Response) -> float | None:
        if response.status_code not in {403, 429}:
            return None
        retry_after = response.headers.get("retry-after")
        remaining = response.headers.get("x-ratelimit-remaining")
        secondary = "secondary rate limit" in response.text.lower()
        if retry_after:
            try:
                return max(1.0, float(retry_after))
            except ValueError:
                return 60.0
        if remaining == "0" and self._rate_limit_reset_at:
            return max(1.0, self._rate_limit_reset_at.timestamp() - time.time())
        if secondary or response.status_code == 429:
            return 60.0
        return None

    async def request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        if self._rate_limited_until > time.time():
            until = datetime.fromtimestamp(self._rate_limited_until, UTC).isoformat()
            raise GitHubRateLimitError(f"GitHub API rate limit is active until {until}")
        token = await self.auth.token()
        authentication_retried = False
        for attempt in range(3):
            try:
                response = await self.http.request(
                    method,
                    f"{self.settings.github_api_url}{path}",
                    headers=self._headers(token),
                    params=params,
                    json=json_body,
                )
            except httpx.TransportError:
                if attempt == 2:
                    raise
                await asyncio.sleep((0.5 * (2**attempt)) + secrets.randbelow(250) / 1000)
                continue
            self._capture_rate_limit(response)
            if response.status_code == 401 and not authentication_retried:
                self.auth.invalidate()
                token = await self.auth.token(force_refresh=True)
                authentication_retried = True
                continue
            if delay := self._rate_limit_delay(response):
                self._rate_limited_until = time.time() + delay
                until = datetime.fromtimestamp(self._rate_limited_until, UTC).isoformat()
                GITHUB_API_FAILURES.labels(operation=operation, status=response.status_code).inc()
                log.warning(
                    "github.rate_limited",
                    operation=operation,
                    status=response.status_code,
                    limited_until=until,
                )
                raise GitHubRateLimitError(f"GitHub API rate limit is active until {until}")
            if response.status_code >= 500 and attempt < 2:
                await asyncio.sleep((0.5 * (2**attempt)) + secrets.randbelow(250) / 1000)
                continue
            if response.is_error:
                GITHUB_API_FAILURES.labels(operation=operation, status=response.status_code).inc()
                log.error(
                    "github.api_error",
                    operation=operation,
                    status=response.status_code,
                    response=response.text[:500],
                )
            response.raise_for_status()
            return response.json() if response.content else None
        raise RuntimeError("GitHub API retry exhausted")

    async def convert_manifest(self, code: str, setup: GitHubSetupRequest) -> GitHubConnection:
        response = await self.http.post(
            f"{self.settings.github_api_url}/app-manifests/{code}/conversions",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": self.settings.github_api_version,
                "User-Agent": "EasyRunners/0.1",
            },
        )
        response.raise_for_status()
        self.invalidate_connection_cache()
        return self.store.save_manifest_result(setup, response.json())

    def build_manifest(self, setup: GitHubSetupRequest) -> dict[str, Any]:
        permissions: dict[str, str] = {"actions": "read"}
        if setup.scope == GitHubScope.REPO:
            permissions["administration"] = "write"
        else:
            permissions["organization_self_hosted_runners"] = "write"
        suffix = secrets.token_hex(3)
        return {
            "name": f"EasyRunners-{self.settings.instance_id}-{suffix}",
            "url": self.settings.public_url,
            "description": "Ephemeral Docker capacity for GitHub Actions",
            "public": False,
            "redirect_url": f"{self.settings.public_url}/setup/github/callback",
            "setup_url": f"{self.settings.public_url}/setup/github/installed",
            "setup_on_update": True,
            "default_events": ["workflow_job"],
            "default_permissions": permissions,
            "hook_attributes": {
                "url": f"{self.settings.public_url}/webhooks/github",
                "active": setup.webhook_enabled,
            },
        }

    async def resolve_setup(self, request: GitHubConnectRequest) -> GitHubSetupRequest:
        value = request.target_url.strip().rstrip("/")
        if "://" not in value:
            value = f"{self.settings.github_web_url}/{value.lstrip('/')}"
        parsed = urlparse(value)
        expected_host = urlparse(self.settings.github_web_url).hostname
        if parsed.hostname != expected_host:
            raise ValueError(f"GitHub URL must use {expected_host}")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError("GitHub URL must contain an owner or organization")
        owner = parts[0]
        if request.organization_wide:
            scope = GitHubScope.ORG
            repository = None
        else:
            scope = GitHubScope.REPO
            # Repository access is selected on GitHub's installation screen. A repository URL is
            # accepted for convenience, but only its account owner is needed or persisted.
            repository = None
        response = await self.http.get(
            f"{self.settings.github_api_url}/users/{owner}",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": self.settings.github_api_version,
                "User-Agent": "EasyRunners/0.1",
            },
        )
        if response.is_error:
            raise ValueError(
                "GitHub could not determine whether the target is a user or organization"
            )
        owner_kind = (
            "organization" if response.json().get("type") == "Organization" else "user"
        )
        if scope == GitHubScope.ORG and owner_kind != "organization":
            raise ValueError("organization-wide runners require an organization URL")
        return GitHubSetupRequest(
            scope=scope,
            owner=owner,
            repository=repository,
            app_owner_kind=owner_kind,
            webhook_enabled=request.webhook_enabled,
        )

    async def validate_installation(self, installation_id: int) -> dict[str, Any]:
        credentials = self.store.credentials(require_installation=False)
        if not credentials or not credentials.private_key or not credentials.connection.app_id:
            raise RuntimeError("GitHub App setup is incomplete")
        app_token = self.auth.app_jwt(credentials.connection.app_id, credentials.private_key)
        response = await self.http.get(
            f"{self.settings.github_api_url}/app/installations/{installation_id}",
            headers=self._headers(app_token),
        )
        response.raise_for_status()
        installation = cast(dict[str, Any], response.json())
        expected = credentials.connection.owner.lower()
        actual = str(installation.get("account", {}).get("login", "")).lower()
        if actual != expected:
            raise ValueError("installation account does not match the configured target")
        selection = str(installation.get("repository_selection") or "") or None
        self.store.save_installation(
            installation_id,
            repository_selection=selection,
        )
        self.auth.invalidate()
        self._repositories = (0.0, [])
        self._installation_metadata_at = time.monotonic()
        if credentials.connection.scope == GitHubScope.REPO:
            try:
                if credentials.connection.repository:
                    await self.request(
                        "GET",
                        f"/repos/{credentials.connection.target_name}",
                        operation="validate_repository_access",
                    )
                elif not await self.list_repositories(refresh=True):
                    raise ValueError(
                        "select at least one repository for the GitHub App installation"
                    )
            except httpx.HTTPStatusError as exc:
                self.store.save_installation(None)
                self.auth.invalidate()
                raise ValueError(
                    "the GitHub App was not granted access to the selected repository"
                ) from exc
            except ValueError:
                self.store.save_installation(None)
                self.auth.invalidate()
                raise
        return installation

    def _target_path(self, suffix: str, repository: str | None = None) -> str:
        credentials = self.store.credentials()
        if not credentials:
            raise RuntimeError("GitHub is not connected")
        connection = credentials.connection
        if connection.scope == GitHubScope.ORG:
            return f"/orgs/{connection.organization or connection.owner}/actions/{suffix}"
        target = self._repository_target(connection, repository)
        return f"/repos/{target}/actions/{suffix}"

    @staticmethod
    def _repository_target(
        connection: GitHubConnection, repository: str | None = None
    ) -> str:
        target = repository or connection.target_name
        parts = target.split("/")
        if (
            len(parts) != 2
            or parts[0].lower() != connection.owner.lower()
            or not parts[1]
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
                for character in parts[1]
            )
        ):
            raise ValueError("runner repository does not belong to the connected account")
        return target

    def target_url(self, repository: str | None = None) -> str:
        credentials = self.store.credentials()
        if not credentials:
            raise RuntimeError("GitHub is not connected")
        connection = credentials.connection
        if connection.scope == GitHubScope.ORG:
            return f"{self.settings.github_web_url}/{connection.organization or connection.owner}"
        return f"{self.settings.github_web_url}/{self._repository_target(connection, repository)}"

    async def registration_token(self, repository: str | None = None) -> str:
        body = await self.request(
            "POST",
            self._target_path("runners/registration-token", repository),
            operation="registration_token",
        )
        return str(body["token"])

    async def list_runners(
        self, repositories: list[str] | None = None
    ) -> list[dict[str, Any]]:
        credentials = self.store.credentials()
        if not credentials:
            return []
        connection = credentials.connection
        if connection.scope == GitHubScope.ORG:
            body = await self.request(
                "GET",
                self._target_path("runners"),
                operation="list_runners",
                params={"per_page": 100},
            )
            return list(body.get("runners", []))
        targets = repositories
        if targets is None:
            targets = await self.list_repositories()
        semaphore = asyncio.Semaphore(self.settings.poll_concurrency)

        async def scan(repository: str) -> list[dict[str, Any]]:
            async with semaphore:
                body = await self.request(
                    "GET",
                    self._target_path("runners", repository),
                    operation="list_runners",
                    params={"per_page": 100},
                )
                return [dict(runner, repository=repository) for runner in body.get("runners", [])]

        results = await asyncio.gather(*(scan(repo) for repo in targets), return_exceptions=True)
        runners: list[dict[str, Any]] = []
        failures: list[BaseException] = []
        for repository, result in zip(targets, results, strict=True):
            if isinstance(result, BaseException):
                failures.append(result)
                log.warning(
                    "github.list_runners_failed", repository=repository, error=str(result)
                )
            else:
                runners.extend(result)
        if targets and len(failures) == len(targets):
            raise RuntimeError(
                "GitHub runner discovery failed for every selected repository"
            ) from failures[0]
        return runners

    async def delete_runner(self, runner_id: int, repository: str | None = None) -> None:
        await self.request(
            "DELETE",
            self._target_path(f"runners/{runner_id}", repository),
            operation="delete_runner",
        )

    async def list_repositories(self, *, refresh: bool = False) -> list[str]:
        credentials = self.store.credentials()
        if not credentials:
            return []
        connection = credentials.connection
        if connection.scope == GitHubScope.REPO and connection.auth_type != "app":
            return [connection.target_name]
        cached_at, cached = self._repositories
        if not refresh and cached_at and time.monotonic() - cached_at < 60:
            return list(cached)
        if connection.auth_type == "app":
            path = "/installation/repositories"
        else:
            path = f"/orgs/{connection.organization or connection.owner}/repos"
        repositories: list[str] = []
        for page in range(1, 11):
            params: dict[str, Any] = {"per_page": 100, "page": page}
            if connection.auth_type != "app":
                params["type"] = "all"
            body = await self.request(
                "GET",
                path,
                operation="list_repositories",
                params=params,
            )
            items = body.get("repositories", []) if isinstance(body, dict) else body
            if not items:
                break
            repositories.extend(str(repo["full_name"]) for repo in items)
            if len(items) < 100 or len(repositories) >= self.settings.poll_max_repositories:
                break
        prefix = f"{connection.organization or connection.owner}/".lower()
        selected = [repo for repo in repositories if repo.lower().startswith(prefix)][
            : self.settings.poll_max_repositories
        ]
        self._repositories = (time.monotonic(), selected)
        if connection.repositories_count != len(selected):
            self.store.update_repository_metadata(repositories_count=len(selected))
        return list(selected)

    @staticmethod
    def workflow_labels(pool: RunnerPoolConfig) -> list[str]:
        labels = pool.effective_labels - ARCHITECTURE_LABELS
        ordered = [label for label in ("self-hosted", "linux") if label in labels]
        return ordered + sorted(labels - set(ordered))

    @classmethod
    def workflow_runs_on(cls, pool: RunnerPoolConfig) -> str:
        return f"runs-on: [{', '.join(cls.workflow_labels(pool))}]"

    @staticmethod
    def _recommended_pool(pools: dict[str, RunnerPoolConfig]) -> str | None:
        if "ci" in pools:
            return "ci"
        non_docker = sorted(
            name for name, pool in pools.items() if pool.docker_mode == DockerMode.NONE
        )
        if non_docker:
            return non_docker[0]
        if "default" in pools:
            return "default"
        return sorted(pools)[0] if pools else None

    @classmethod
    def _adoption_pool_details(
        cls, pools: dict[str, RunnerPoolConfig]
    ) -> dict[str, Any]:
        recommendation = cls._recommended_pool(pools)
        replacements = {
            name: {
                "labels": cls.workflow_labels(pool),
                "runs_on": cls.workflow_runs_on(pool),
                "docker_mode": pool.docker_mode.value,
            }
            for name, pool in sorted(pools.items())
        }
        return {
            "recommended_pool": recommendation,
            "recommended_runs_on": (
                replacements[recommendation]["runs_on"] if recommendation else None
            ),
            "replacements": replacements,
        }

    async def repository_adoption(
        self,
        pools: dict[str, RunnerPoolConfig],
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        cached_at, cached = self._adoption
        if (
            not refresh
            and cached_at
            and time.monotonic() - cached_at < self.settings.adoption_scan_interval
        ):
            return {**cached, **self._adoption_pool_details(pools)}

        repositories = (await self.list_repositories(refresh=refresh))[
            : self.settings.adoption_max_repositories
        ]
        semaphore = asyncio.Semaphore(self.settings.poll_concurrency)

        async def scan(repository: str) -> dict[str, Any]:
            async with semaphore:
                try:
                    return await self._repository_adoption(repository)
                except Exception as exc:
                    log.warning(
                        "github.adoption_scan_failed", repository=repository, error=str(exc)
                    )
                    return {
                        "repository": repository,
                        "status": "error",
                        "hosted_jobs": 0,
                        "self_hosted_jobs": 0,
                        "examples": [],
                        "error": str(exc),
                    }

        results = await asyncio.gather(*(scan(repository) for repository in repositories))
        payload = {
            "repositories": results,
            "scanned_at": datetime.now(UTC).isoformat(),
            "cached_for_seconds": self.settings.adoption_scan_interval,
            **self._adoption_pool_details(pools),
        }
        self._adoption = (time.monotonic(), payload)
        return dict(payload)

    async def _repository_adoption(self, repository: str) -> dict[str, Any]:
        body = await self.request(
            "GET",
            f"/repos/{repository}/actions/runs",
            operation="adoption_workflow_runs",
            params={"per_page": self.settings.adoption_runs_per_repo},
        )
        hosted = 0
        self_hosted = 0
        examples: list[dict[str, Any]] = []
        for run in body.get("workflow_runs", []):
            run_id = int(run["id"])
            jobs = await self.request(
                "GET",
                f"/repos/{repository}/actions/runs/{run_id}/jobs",
                operation="adoption_workflow_jobs",
                params={"filter": "latest", "per_page": 100},
            )
            for job in jobs.get("jobs", []):
                labels = sorted({str(label).lower() for label in job.get("labels") or []})
                is_self_hosted = "self-hosted" in labels
                is_hosted = any(
                    label == "ubuntu-latest"
                    or label.startswith(("ubuntu-", "windows-", "macos-"))
                    for label in labels
                )
                if is_self_hosted:
                    self_hosted += 1
                if is_hosted:
                    hosted += 1
                    if len(examples) < 5:
                        examples.append(
                            {
                                "workflow": str(run.get("name") or "Workflow"),
                                "job": str(job.get("name") or "Job"),
                                "labels": labels,
                                "url": str(job.get("html_url") or run.get("html_url") or ""),
                            }
                        )
        if hosted and self_hosted:
            status = "mixed"
        elif hosted:
            status = "needs_migration"
        elif self_hosted:
            status = "using_easy_runners"
        else:
            status = "no_recent_jobs"
        return {
            "repository": repository,
            "status": status,
            "hosted_jobs": hosted,
            "self_hosted_jobs": self_hosted,
            "examples": examples,
            "error": None,
        }

    async def refresh_installation_metadata(self, *, refresh: bool = False) -> GitHubConnection:
        credentials = self.store.credentials(require_installation=False)
        if not credentials:
            raise RuntimeError("GitHub is not connected")
        connection = credentials.connection
        if not (
            connection.auth_type == "app"
            and connection.app_id
            and connection.installation_id
            and credentials.private_key
        ):
            return connection
        if not refresh and time.monotonic() - self._installation_metadata_at < 60:
            return connection
        app_token = self.auth.app_jwt(connection.app_id, credentials.private_key)
        response = await self.http.get(
            f"{self.settings.github_api_url}/app/installations/{connection.installation_id}",
            headers=self._headers(app_token),
        )
        response.raise_for_status()
        selection = str(response.json().get("repository_selection") or "") or None
        self._installation_metadata_at = time.monotonic()
        if selection != connection.repository_selection:
            connection = self.store.update_repository_metadata(
                repository_selection=selection,
            )
        return connection

    async def queued_jobs(self, repositories: list[str] | None = None) -> list[WorkflowJob]:
        repos = repositories or await self.list_repositories()
        semaphore = asyncio.Semaphore(self.settings.poll_concurrency)

        async def scan(repo: str) -> list[WorkflowJob]:
            async with semaphore:
                return await self._queued_jobs_for_repo(repo)

        results = await asyncio.gather(*(scan(repo) for repo in repos), return_exceptions=True)
        jobs: list[WorkflowJob] = []
        for repo, result in zip(repos, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("github.poll_repository_failed", repository=repo, error=str(result))
            else:
                jobs.extend(result)
        return jobs

    async def _queued_jobs_for_repo(self, repository: str) -> list[WorkflowJob]:
        runs: dict[int, dict[str, Any]] = {}
        for status in ("queued", "in_progress"):
            body = await self.request(
                "GET",
                f"/repos/{repository}/actions/runs",
                operation="list_workflow_runs",
                params={
                    "status": status,
                    "per_page": self.settings.poll_runs_per_repo,
                },
            )
            for run in body.get("workflow_runs", []):
                runs[int(run["id"])] = run
        jobs: list[WorkflowJob] = []
        for run_id in runs:
            body = await self.request(
                "GET",
                f"/repos/{repository}/actions/runs/{run_id}/jobs",
                operation="list_workflow_jobs",
                params={"filter": "latest", "per_page": 100},
            )
            for raw in body.get("jobs", []):
                if raw.get("status") != "queued":
                    continue
                jobs.append(
                    WorkflowJob(
                        id=raw["id"],
                        run_id=run_id,
                        repository=repository,
                        name=raw.get("name", ""),
                        labels=raw.get("labels") or [],
                        status="queued",
                        queued_at=_parse_time(raw.get("started_at") or raw.get("created_at")),
                    )
                )
        return jobs

    async def connected(self) -> bool:
        try:
            await self.list_runners()
        except Exception:
            return False
        return True

    async def latest_runner_version(self) -> str | None:
        cached_at, version = self._latest_runner
        if time.monotonic() - cached_at < 3600:
            return version
        try:
            response = await self.http.get(
                f"{self.settings.github_api_url}/repos/actions/runner/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "EasyRunners/0.1"},
            )
            response.raise_for_status()
            version = str(response.json().get("tag_name", "")).removeprefix("v") or None
        except Exception:
            version = None
        self._latest_runner = (time.monotonic(), version)
        return version

    async def latest_manager_release(self) -> dict[str, Any] | None:
        cached_at, release = self._latest_manager
        if time.monotonic() - cached_at < 3600:
            return dict(release) if release else None
        try:
            response = await self.http.get(
                f"{self.settings.github_api_url}/repos/"
                f"{self.settings.manager_repository}/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "EasyRunners/0.1"},
            )
            response.raise_for_status()
            body = response.json()
            tag = str(body.get("tag_name", ""))
            release = {
                "tag": tag,
                "version": tag.removeprefix("v"),
                "name": str(body.get("name") or tag),
                "published_at": body.get("published_at"),
                "url": body.get("html_url"),
            }
        except Exception:
            release = None
        self._latest_manager = (time.monotonic(), release)
        return dict(release) if release else None

    async def latest_manager_version(self) -> str | None:
        release = await self.latest_manager_release()
        return str(release["version"]) if release else None


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
