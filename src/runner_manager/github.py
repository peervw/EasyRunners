from __future__ import annotations

import asyncio
import secrets
import time
import uuid
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
    id: str = ""
    auth_type: str
    scope: GitHubScope
    owner: str
    account_type: str = "user"
    account_id: int | None = None
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
        self._migrate_legacy_onboarding()

    @staticmethod
    def _environment_id(kind: str, owner: str) -> str:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"easy-runners:{kind}:{owner.lower()}").hex

    def _secret_dir(self, connection_id: str) -> Path:
        return self.github_dir / connection_id

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
            id=self._environment_id("app", owner),
            auth_type="app",
            scope=s.github_scope,
            owner=owner,
            account_type="organization" if s.github_org else "user",
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
            id=self._environment_id("pat", owner),
            auth_type="pat",
            scope=s.github_scope,
            owner=owner,
            account_type="organization" if s.github_org else "user",
            organization=s.github_org,
            repository=s.github_repo,
            webhook_enabled=False,
        )
        return Credentials(connection, token=s.github_token.get_secret_value())

    def _onboarded(self, connection_id: str) -> Credentials | None:
        record = self.database.get_github_connection(connection_id)
        if not record:
            return None
        connection = GitHubConnection.model_validate_json(str(record["payload"]))
        secret_dir = self._secret_dir(connection.id)
        pem_path = secret_dir / "app.pem"
        secret_path = secret_dir / "webhook.secret"
        if connection.auth_type == "app" and not pem_path.exists():
            return None
        webhook = secret_path.read_text(encoding="utf-8").strip() if secret_path.exists() else None
        return Credentials(
            connection,
            private_key=pem_path.read_text(encoding="utf-8") if pem_path.exists() else None,
            webhook_secret=webhook,
        )

    def connections(self, *, include_incomplete: bool = True) -> list[GitHubConnection]:
        connections = [
            GitHubConnection.model_validate_json(str(record["payload"]))
            for record in self.database.list_github_connections()
        ]
        mode = self.settings.github_auth_mode
        environment = [self._environment_app(), self._environment_pat()]
        if mode == "app":
            connections = [item.connection for item in environment[:1] if item]
        elif mode == "pat":
            connections = [item.connection for item in environment[1:] if item]
        elif mode == "auto":
            known_owners = {connection.owner.lower() for connection in connections}
            for item in environment:
                if item and item.connection.owner.lower() not in known_owners:
                    connections.append(item.connection)
                    known_owners.add(item.connection.owner.lower())
        if not include_incomplete:
            connections = [
                connection
                for connection in connections
                if connection.auth_type != "app" or connection.installation_id
            ]
        return connections

    def connection(self, connection_id: str) -> GitHubConnection | None:
        return next(
            (item for item in self.connections() if item.id == connection_id),
            None,
        )

    def find_by_owner(self, owner: str) -> GitHubConnection | None:
        return next(
            (
                connection
                for connection in self.connections()
                if connection.owner.lower() == owner.lower()
            ),
            None,
        )

    def find_by_repository(self, repository: str) -> GitHubConnection | None:
        owner, separator, _ = repository.partition("/")
        return self.find_by_owner(owner) if separator else None

    def find_by_installation(self, installation_id: int) -> GitHubConnection | None:
        return next(
            (
                connection
                for connection in self.connections()
                if connection.installation_id == installation_id
            ),
            None,
        )

    def credentials(
        self,
        *,
        connection_id: str | None = None,
        repository: str | None = None,
        require_installation: bool = True,
    ) -> Credentials | None:
        mode = self.settings.github_auth_mode
        selected = self.connection(connection_id) if connection_id else None
        if repository and not selected:
            selected = self.find_by_repository(repository)
        choices: list[Credentials | None] = []
        if mode == "app":
            choices = [self._environment_app()]
        elif mode == "pat":
            choices = [self._environment_pat()]
        else:
            if selected:
                choices = [
                    self._onboarded(selected.id)
                    if selected.source == "onboarding"
                    else self._environment_app()
                    if selected.auth_type == "app"
                    else self._environment_pat()
                ]
            else:
                choices = [
                    *(self._onboarded(connection.id) for connection in self.connections()),
                    self._environment_app(),
                    self._environment_pat(),
                ]
        result = next((choice for choice in choices if choice is not None), None)
        if connection_id and result and result.connection.id != connection_id:
            return None
        if repository and result:
            owner = repository.partition("/")[0]
            if result.connection.owner.lower() != owner.lower():
                return None
        if require_installation and result and result.connection.auth_type == "app":
            if not result.connection.installation_id:
                return None
        return result

    def all_credentials(self, *, require_installation: bool = True) -> list[Credentials]:
        result: list[Credentials] = []
        for connection in self.connections(include_incomplete=not require_installation):
            credentials = self.credentials(
                connection_id=connection.id,
                require_installation=require_installation,
            )
            if credentials:
                result.append(credentials)
        return result

    def save_manifest_result(
        self, setup: GitHubSetupRequest, manifest_result: dict[str, Any]
    ) -> GitHubConnection:
        existing = self.find_by_owner(setup.owner)
        if existing and existing.id != setup.connection_id:
            raise ValueError(f"{setup.owner} is already connected")
        secret_dir = self._secret_dir(setup.connection_id)
        self._write_secret(secret_dir / "app.pem", str(manifest_result["pem"]))
        self._write_secret(
            secret_dir / "webhook.secret", str(manifest_result["webhook_secret"])
        )
        connection = GitHubConnection(
            id=setup.connection_id,
            auth_type="app",
            scope=setup.scope,
            owner=setup.owner,
            account_type=setup.app_owner_kind,
            repository=setup.repository,
            organization=setup.owner if setup.scope == GitHubScope.ORG else None,
            app_id=int(manifest_result["id"]),
            app_slug=str(manifest_result["slug"]),
            source="onboarding",
            webhook_enabled=setup.webhook_enabled,
        )
        self.database.upsert_github_connection(
            connection.id, connection.owner, connection.model_dump_json()
        )
        self.database.delete_setting(f"webhook_last_received_at:{connection.id}")
        return connection

    def save_installation(
        self,
        connection_id: str | int,
        installation_id: int | None = None,
        *,
        account_id: int | None = None,
        account_type: str | None = None,
        repository_selection: str | None = None,
        repositories_count: int | None = None,
    ) -> GitHubConnection:
        # Preserve the original single-connection call shape for upgrades that
        # finish an onboarding flow started by an older EasyRunners release.
        if isinstance(connection_id, int):
            installation_id = connection_id
            connections = self.connections()
            if len(connections) != 1:
                raise RuntimeError("a GitHub connection ID is required")
            existing = self.credentials(
                connection_id=connections[0].id, require_installation=False
            )
            if not existing:
                raise RuntimeError("GitHub App manifest has not been completed")
            connection_id = existing.connection.id
        credentials = self.credentials(
            connection_id=connection_id, require_installation=False
        )
        if not credentials:
            raise RuntimeError("GitHub App manifest has not been completed")
        updates: dict[str, Any] = {"installation_id": installation_id}
        if account_id is not None:
            updates["account_id"] = account_id
        if account_type is not None:
            updates["account_type"] = account_type
        if repository_selection is not None:
            updates["repository_selection"] = repository_selection
        if repositories_count is not None:
            updates["repositories_count"] = repositories_count
        connection = credentials.connection.model_copy(update=updates)
        self.database.upsert_github_connection(
            connection.id, connection.owner, connection.model_dump_json()
        )
        return connection

    def update_repository_metadata(
        self,
        connection_id: str,
        *,
        repository_selection: str | None = None,
        repositories_count: int | None = None,
    ) -> GitHubConnection:
        credentials = self.credentials(
            connection_id=connection_id, require_installation=False
        )
        if not credentials:
            raise RuntimeError("GitHub is not connected")
        updates: dict[str, Any] = {}
        if repository_selection is not None:
            updates["repository_selection"] = repository_selection
        if repositories_count is not None:
            updates["repositories_count"] = repositories_count
        connection = credentials.connection.model_copy(update=updates)
        self.database.upsert_github_connection(
            connection.id, connection.owner, connection.model_dump_json()
        )
        return connection

    def disconnect(self, connection_id: str | None = None) -> None:
        connection = (
            self.connection(connection_id)
            if connection_id
            else next(iter(self.connections()), None)
        )
        if not connection:
            return
        self.database.delete_github_connection(connection.id)
        self.database.delete_setting(f"webhook_last_received_at:{connection.id}")
        secret_dir = self._secret_dir(connection.id)
        for name in ("app.pem", "webhook.secret"):
            path = secret_dir / name
            if path.exists():
                path.unlink()
        if secret_dir.exists():
            secret_dir.rmdir()

    def _migrate_legacy_onboarding(self) -> None:
        raw = self.database.get_setting("github_connection")
        if not raw:
            return
        connection = GitHubConnection.model_validate_json(raw)
        if not connection.id:
            connection.id = uuid.uuid4().hex
        existing = self.database.find_github_connection(connection.owner)
        if existing:
            connection = GitHubConnection.model_validate_json(str(existing["payload"]))
        else:
            self.database.upsert_github_connection(
                connection.id, connection.owner, connection.model_dump_json()
            )
        secret_dir = self._secret_dir(connection.id)
        for name in ("app.pem", "webhook.secret"):
            source = self.github_dir / name
            destination = secret_dir / name
            if source.exists() and not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.replace(destination)
                destination.chmod(0o600)
        if last_webhook := self.database.get_setting("webhook_last_received_at"):
            self.database.set_setting(
                f"webhook_last_received_at:{connection.id}", last_webhook
            )
            self.database.delete_setting("webhook_last_received_at")
        self.database.delete_setting("github_connection")


class GitHubAuth:
    def __init__(
        self,
        settings: Settings,
        store: GitHubConnectionStore,
        client: httpx.AsyncClient,
        connection_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.connection_id = connection_id
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
        credentials = self.store.credentials(connection_id=self.connection_id)
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
        *,
        connection_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.http = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._owns_client = client is None
        self.connection_id = connection_id
        self.auth = GitHubAuth(settings, store, self.http, connection_id)
        self._latest_runner: tuple[float, str | None] = (0.0, None)
        self._latest_manager: tuple[float, dict[str, Any] | None] = (0.0, None)
        self._repositories: tuple[float, list[str]] = (0.0, [])
        self._adoption_results: dict[str, dict[str, Any]] = {}
        self._adoption_task: asyncio.Task[None] | None = None
        self._adoption_scan_started_at: datetime | None = None
        self._adoption_scan_completed_at: datetime | None = None
        self._adoption_scan_completed_monotonic = 0.0
        self._adoption_scan_total = 0
        self._adoption_scan_completed = 0
        self._adoption_scan_error: str | None = None
        self._installation_metadata_at = 0.0
        self._rate_limited_until = 0.0
        self._rate_limit_remaining: int | None = None
        self._rate_limit_reset_at: datetime | None = None

    def credentials(self, *, require_installation: bool = True) -> Credentials | None:
        return self.store.credentials(
            connection_id=self.connection_id,
            require_installation=require_installation,
        )

    async def close(self) -> None:
        if self._adoption_task and not self._adoption_task.done():
            self._adoption_task.cancel()
            await asyncio.gather(self._adoption_task, return_exceptions=True)
        if self._owns_client:
            await self.http.aclose()

    def invalidate_connection_cache(self) -> None:
        self.auth.invalidate()
        self._repositories = (0.0, [])
        if self._adoption_task and not self._adoption_task.done():
            self._adoption_task.cancel()
        self._adoption_task = None
        self._adoption_results = {}
        self._adoption_scan_started_at = None
        self._adoption_scan_completed_at = None
        self._adoption_scan_completed_monotonic = 0.0
        self._adoption_scan_total = 0
        self._adoption_scan_completed = 0
        self._adoption_scan_error = None
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
            "setup_url": (
                f"{self.settings.public_url}/setup/github/installed"
                f"?connection_id={setup.connection_id}"
            ),
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
        if self.store.find_by_owner(owner):
            raise ValueError(f"{owner} is already connected")
        return GitHubSetupRequest(
            scope=scope,
            owner=owner,
            repository=repository,
            app_owner_kind=owner_kind,
            webhook_enabled=request.webhook_enabled,
        )

    async def validate_installation(self, installation_id: int) -> dict[str, Any]:
        credentials = self.credentials(require_installation=False)
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
            credentials.connection.id,
            installation_id,
            account_id=(
                int(installation["account"]["id"])
                if installation.get("account", {}).get("id") is not None
                else None
            ),
            account_type=(
                "organization"
                if str(installation.get("account", {}).get("type", "")).lower()
                == "organization"
                else "user"
            ),
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
                self.store.save_installation(credentials.connection.id, None)
                self.auth.invalidate()
                raise ValueError(
                    "the GitHub App was not granted access to the selected repository"
                ) from exc
            except ValueError:
                self.store.save_installation(credentials.connection.id, None)
                self.auth.invalidate()
                raise
        return installation

    def _target_path(self, suffix: str, repository: str | None = None) -> str:
        credentials = self.credentials()
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
        credentials = self.credentials()
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
        credentials = self.credentials()
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
            return [
                dict(runner, connection_id=connection.id)
                for runner in body.get("runners", [])
            ]
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
                return [
                    dict(
                        runner,
                        repository=repository,
                        connection_id=connection.id,
                    )
                    for runner in body.get("runners", [])
                ]

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
        credentials = self.credentials()
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
            self.store.update_repository_metadata(
                connection.id, repositories_count=len(selected)
            )
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
        if "standard" in pools:
            return "standard"
        # Older installations used `ci`; continue to recommend it until the
        # persisted pool configuration is migrated.
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
        wait: bool = True,
    ) -> dict[str, Any]:
        repositories = await self.list_repositories(refresh=refresh)
        running = bool(self._adoption_task and not self._adoption_task.done())
        stale = (
            not self._adoption_scan_completed_monotonic
            or time.monotonic() - self._adoption_scan_completed_monotonic
            >= self.settings.adoption_scan_interval
        )
        if (refresh or stale) and not running:
            scan_targets = self._adoption_scan_targets(repositories)
            self._adoption_scan_started_at = datetime.now(UTC)
            self._adoption_scan_total = len(scan_targets)
            self._adoption_scan_completed = 0
            self._adoption_scan_error = None
            self._adoption_task = asyncio.create_task(
                self._scan_repository_adoption(repositories, scan_targets),
                name="github-repository-adoption",
            )
        if wait and self._adoption_task:
            await asyncio.shield(self._adoption_task)
        return self._adoption_snapshot(repositories, pools)

    def _adoption_snapshot(
        self,
        repositories: list[str],
        pools: dict[str, RunnerPoolConfig],
    ) -> dict[str, Any]:
        results = [
            self._adoption_results[repository]
            for repository in repositories
            if repository in self._adoption_results
        ]
        scanning = bool(self._adoption_task and not self._adoption_task.done())
        completed = self._adoption_scan_completed
        total = self._adoption_scan_total
        return {
            "repositories": results,
            "repository_count_total": len(repositories),
            "repository_count_scanned": len(results),
            "scan": {
                "scanning": scanning,
                "completed": min(completed, total),
                "total": total,
                "started_at": (
                    self._adoption_scan_started_at.isoformat()
                    if self._adoption_scan_started_at
                    else None
                ),
                "error": self._adoption_scan_error,
            },
            "scanned_at": (
                self._adoption_scan_completed_at.isoformat()
                if self._adoption_scan_completed_at
                else None
            ),
            "cached_for_seconds": self.settings.adoption_scan_interval,
            "scan_concurrency": self.settings.poll_concurrency,
            "scan_batch_size": self.settings.adoption_max_repositories,
            **self._adoption_pool_details(pools),
        }

    def _adoption_scan_targets(self, repositories: list[str]) -> list[str]:
        priorities = {
            "error": 1,
            "no_recent_jobs": 1,
            "needs_migration": 2,
            "mixed": 2,
            "using_easy_runners": 3,
        }

        def scan_priority(repository: str) -> tuple[int, str, str]:
            result = self._adoption_results.get(repository, {})
            status = str(result.get("status") or "")
            scanned_at = str(result.get("scanned_at") or "")
            return priorities.get(status, 0), scanned_at, repository.lower()

        limit = max(1, self.settings.adoption_max_repositories)
        return sorted(repositories, key=scan_priority)[:limit]

    async def _scan_repository_adoption(
        self,
        repositories: list[str],
        scan_targets: list[str],
    ) -> None:
        selected = set(repositories)
        self._adoption_results = {
            repository: result
            for repository, result in self._adoption_results.items()
            if repository in selected
        }
        self._adoption_scan_started_at = datetime.now(UTC)
        self._adoption_scan_total = len(scan_targets)
        self._adoption_scan_completed = 0
        self._adoption_scan_error = None
        stopped = asyncio.Event()
        queue: asyncio.Queue[str] = asyncio.Queue()
        for repository in scan_targets:
            queue.put_nowait(repository)

        async def worker() -> None:
            while not queue.empty() and not stopped.is_set():
                try:
                    repository = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    result = await self._repository_adoption(repository)
                except GitHubRateLimitError as exc:
                    self._adoption_scan_error = str(exc)
                    stopped.set()
                    log.warning("github.adoption_scan_paused", error=str(exc))
                    return
                except Exception as exc:
                    log.warning(
                        "github.adoption_scan_failed",
                        repository=repository,
                        error=str(exc),
                    )
                    result = {
                        "repository": repository,
                        "status": "error",
                        "hosted_jobs": 0,
                        "self_hosted_jobs": 0,
                        "examples": [],
                        "error": str(exc),
                    }
                result["scanned_at"] = datetime.now(UTC).isoformat()
                self._adoption_results[repository] = result
                self._adoption_scan_completed += 1

        worker_count = min(max(1, self.settings.poll_concurrency), queue.qsize())
        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        try:
            if workers:
                await asyncio.gather(*workers)
        finally:
            for worker_task in workers:
                if not worker_task.done():
                    worker_task.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
        self._adoption_scan_completed_at = datetime.now(UTC)
        self._adoption_scan_completed_monotonic = time.monotonic()

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
                                "workflow_path": str(run.get("path") or ""),
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
        credentials = self.credentials(require_installation=False)
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
                connection.id,
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
                        connection_id=self.connection_id,
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


class GitHubClientRegistry:
    """Connection-scoped GitHub clients presented as one EasyRunners integration."""

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
        self._setup = GitHubClient(settings, store, self.http)
        self._clients: dict[str, GitHubClient] = {}
        self._repository_errors: dict[str, str] = {}
        self._runner_errors: dict[str, str] = {}
        self._queue_errors: dict[str, str] = {}

    @property
    def repository_errors(self) -> dict[str, str]:
        return dict(self._repository_errors)

    @property
    def runner_errors(self) -> dict[str, str]:
        return dict(self._runner_errors)

    @property
    def queue_errors(self) -> dict[str, str]:
        return dict(self._queue_errors)

    def client(self, connection_id: str) -> GitHubClient:
        if not self.store.connection(connection_id):
            raise KeyError(connection_id)
        if connection_id not in self._clients:
            self._clients[connection_id] = GitHubClient(
                self.settings,
                self.store,
                self.http,
                connection_id=connection_id,
            )
        return self._clients[connection_id]

    def installed_clients(self) -> list[GitHubClient]:
        return [
            self.client(connection.id)
            for connection in self.store.connections(include_incomplete=False)
        ]

    async def close(self) -> None:
        await asyncio.gather(
            self._setup.close(),
            *(client.close() for client in self._clients.values()),
        )
        if self._owns_client:
            await self.http.aclose()

    async def resolve_setup(self, request: GitHubConnectRequest) -> GitHubSetupRequest:
        return await self._setup.resolve_setup(request)

    def build_manifest(self, setup: GitHubSetupRequest) -> dict[str, Any]:
        return self._setup.build_manifest(setup)

    async def convert_manifest(
        self, code: str, setup: GitHubSetupRequest
    ) -> GitHubConnection:
        connection = await self._setup.convert_manifest(code, setup)
        self._clients.pop(connection.id, None)
        return connection

    async def validate_installation(
        self, connection_id: str, installation_id: int
    ) -> dict[str, Any]:
        return await self.client(connection_id).validate_installation(installation_id)

    def invalidate_connection_cache(self, connection_id: str | None = None) -> None:
        if connection_id:
            client = self._clients.pop(connection_id, None)
            if client:
                client.invalidate_connection_cache()
            self._repository_errors.pop(connection_id, None)
            self._runner_errors.pop(connection_id, None)
            self._queue_errors.pop(connection_id, None)
            return
        self._setup.invalidate_connection_cache()
        for client in self._clients.values():
            client.invalidate_connection_cache()
        self._repository_errors.clear()
        self._runner_errors.clear()
        self._queue_errors.clear()

    def connection_for_repository(self, repository: str) -> GitHubConnection | None:
        return self.store.find_by_repository(repository)

    async def list_repositories(
        self,
        *,
        connection_id: str | None = None,
        refresh: bool = False,
    ) -> list[str]:
        if connection_id:
            try:
                scoped_repositories = await self.client(connection_id).list_repositories(
                    refresh=refresh
                )
            except Exception as exc:
                self._repository_errors[connection_id] = str(exc)
                raise
            self._repository_errors.pop(connection_id, None)
            return scoped_repositories
        clients = self.installed_clients()
        results = await asyncio.gather(
            *(client.list_repositories(refresh=refresh) for client in clients),
            return_exceptions=True,
        )
        repositories: list[str] = []
        failures: list[BaseException] = []
        for client, result in zip(clients, results, strict=True):
            connection_id = client.connection_id or ""
            if isinstance(result, BaseException):
                failures.append(result)
                self._repository_errors[connection_id] = str(result)
            else:
                self._repository_errors.pop(connection_id, None)
                repositories.extend(result)
        if results and len(failures) == len(results):
            raise RuntimeError(
                "GitHub repository discovery failed for every connection"
            ) from failures[0]
        return sorted(set(repositories), key=str.lower)

    async def list_runners(
        self, repositories: list[str] | None = None
    ) -> list[dict[str, Any]]:
        selected = {repository.lower() for repository in repositories or []}

        async def scan(client: GitHubClient) -> list[dict[str, Any]]:
            credentials = client.credentials()
            if not credentials:
                return []
            if not repositories or credentials.connection.scope == GitHubScope.ORG:
                targets = None
            else:
                targets = [
                    repository
                    for repository in repositories
                    if repository.partition("/")[0].lower()
                    == credentials.connection.owner.lower()
                ]
                if not targets:
                    return []
            runners = await client.list_runners(targets)
            return [
                runner
                for runner in runners
                if not selected
                or credentials.connection.scope == GitHubScope.ORG
                or str(runner.get("repository", "")).lower() in selected
            ]

        clients = self.installed_clients()
        results = await asyncio.gather(
            *(scan(client) for client in clients),
            return_exceptions=True,
        )
        runners: list[dict[str, Any]] = []
        failures: list[BaseException] = []
        for client, result in zip(clients, results, strict=True):
            connection_id = client.connection_id or ""
            if isinstance(result, BaseException):
                failures.append(result)
                self._runner_errors[connection_id] = str(result)
            else:
                self._runner_errors.pop(connection_id, None)
                runners.extend(result)
        if results and len(failures) == len(results):
            raise RuntimeError(
                "GitHub runner discovery failed for every connection"
            ) from failures[0]
        return runners

    async def queued_jobs(
        self, repositories: list[str] | None = None
    ) -> list[WorkflowJob]:
        async def scan(client: GitHubClient) -> list[WorkflowJob]:
            credentials = client.credentials()
            if not credentials:
                return []
            targets = None
            if repositories is not None:
                targets = [
                    repository
                    for repository in repositories
                    if repository.partition("/")[0].lower()
                    == credentials.connection.owner.lower()
                ]
                if not targets:
                    return []
            return await client.queued_jobs(targets)

        clients = self.installed_clients()
        results = await asyncio.gather(
            *(scan(client) for client in clients),
            return_exceptions=True,
        )
        jobs: list[WorkflowJob] = []
        failures: list[BaseException] = []
        for client, result in zip(clients, results, strict=True):
            connection_id = client.connection_id or ""
            if isinstance(result, BaseException):
                failures.append(result)
                self._queue_errors[connection_id] = str(result)
                credentials = client.credentials()
                if not credentials:
                    continue
                connection = credentials.connection
                log.warning(
                    "github.poll_connection_failed",
                    connection_id=connection.id,
                    owner=connection.owner,
                    error=str(result),
                )
            else:
                self._queue_errors.pop(connection_id, None)
                jobs.extend(result)
        if results and len(failures) == len(results):
            raise RuntimeError(
                "GitHub queue polling failed for every connection"
            ) from failures[0]
        return jobs

    async def registration_token(
        self, connection_id: str, repository: str | None = None
    ) -> str:
        return await self.client(connection_id).registration_token(repository)

    def target_url(self, connection_id: str, repository: str | None = None) -> str:
        return self.client(connection_id).target_url(repository)

    async def delete_runner(
        self,
        connection_id: str,
        runner_id: int,
        repository: str | None = None,
    ) -> None:
        await self.client(connection_id).delete_runner(runner_id, repository)

    async def refresh_installation_metadata(
        self, connection_id: str, *, refresh: bool = False
    ) -> GitHubConnection:
        return await self.client(connection_id).refresh_installation_metadata(refresh=refresh)

    def rate_limit_status(self, connection_id: str) -> dict[str, Any]:
        return self.client(connection_id).rate_limit_status()

    async def repository_adoption(
        self,
        pools: dict[str, RunnerPoolConfig],
        *,
        refresh: bool = False,
        wait: bool = True,
    ) -> dict[str, Any]:
        clients = self.installed_clients()
        results = await asyncio.gather(
            *(
                client.repository_adoption(
                    pools,
                    refresh=refresh,
                    wait=wait,
                )
                for client in clients
            ),
            return_exceptions=True,
        )
        repositories: list[dict[str, Any]] = []
        scans: list[dict[str, Any]] = []
        scanned_at: list[str] = []
        total = 0
        scanned = 0
        errors: list[str] = []
        for client, result in zip(clients, results, strict=True):
            credentials = client.credentials()
            if not credentials:
                continue
            connection = credentials.connection
            if isinstance(result, BaseException):
                errors.append(f"{connection.owner}: {result}")
                continue
            for repository in result.get("repositories", []):
                repositories.append(
                    {
                        **repository,
                        "connection_id": connection.id,
                        "connection_owner": connection.owner,
                    }
                )
            total += int(result.get("repository_count_total") or 0)
            scanned += int(result.get("repository_count_scanned") or 0)
            scans.append(result.get("scan") or {})
            if result.get("scanned_at"):
                scanned_at.append(str(result["scanned_at"]))
        scan_total = sum(int(scan.get("total") or 0) for scan in scans)
        scan_completed = sum(int(scan.get("completed") or 0) for scan in scans)
        scan_errors = [str(scan["error"]) for scan in scans if scan.get("error")]
        return {
            "repositories": sorted(
                repositories, key=lambda item: str(item["repository"]).lower()
            ),
            "repository_count_total": total,
            "repository_count_scanned": scanned,
            "scan": {
                "scanning": any(bool(scan.get("scanning")) for scan in scans),
                "completed": scan_completed,
                "total": scan_total,
                "started_at": min(
                    (str(scan["started_at"]) for scan in scans if scan.get("started_at")),
                    default=None,
                ),
                "error": "; ".join([*errors, *scan_errors]) or None,
            },
            "scanned_at": max(scanned_at, default=None),
            "cached_for_seconds": self.settings.adoption_scan_interval,
            "scan_concurrency": self.settings.poll_concurrency,
            "scan_batch_size": self.settings.adoption_max_repositories,
            **GitHubClient._adoption_pool_details(pools),
        }

    async def latest_runner_version(self) -> str | None:
        return await self._setup.latest_runner_version()

    async def latest_manager_release(self) -> dict[str, Any] | None:
        return await self._setup.latest_manager_release()


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
