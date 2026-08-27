from __future__ import annotations

import platform
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class GitHubScope(StrEnum):
    REPO = "repo"
    ORG = "org"


class DockerMode(StrEnum):
    SOCKET = "socket"
    ISOLATED = "isolated"
    NONE = "none"


class TokenScope(StrEnum):
    METRICS = "metrics"
    READ = "read"
    MANAGE = "manage"


ARCHITECTURE_LABELS = {"x64", "arm64", "arm"}


def native_architecture_label(machine: str | None = None) -> str:
    value = (machine or platform.machine()).lower()
    aliases = {
        "amd64": "x64",
        "x86_64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    if value not in aliases:
        raise ValueError(f"unsupported runner architecture: {value}")
    return aliases[value]


NATIVE_ARCHITECTURE = native_architecture_label()
BUILTIN_LABELS = {"self-hosted", "linux", NATIVE_ARCHITECTURE}


class RunnerPoolConfig(BaseModel):
    labels: list[str] = Field(default_factory=lambda: sorted(BUILTIN_LABELS))
    aliases: list[str] = Field(default_factory=list)
    min: int = Field(default=0, ge=0)
    max: int = Field(default=5, ge=0)
    priority: int = 0
    image: str | None = None
    cpu: float = Field(default=4.0, gt=0)
    memory: str = "8g"
    pids_limit: int = Field(default=1024, ge=64)
    idle_timeout: int = Field(default=60, ge=0)
    job_timeout: int = Field(default=3600, ge=60)
    max_lifetime: int = Field(default=3900, ge=60)
    registration_timeout: int = Field(default=300, ge=30)
    docker_mode: DockerMode = DockerMode.NONE
    runner_group: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    environment_from: list[str] = Field(default_factory=list)
    volumes: list[str] = Field(default_factory=list)

    @field_validator("labels")
    @classmethod
    def normalize_labels(cls, value: list[str]) -> list[str]:
        labels = list(dict.fromkeys(item.strip().lower() for item in value if item.strip()))
        requested_architectures = set(labels) & ARCHITECTURE_LABELS
        if requested_architectures - {NATIVE_ARCHITECTURE}:
            requested = ", ".join(sorted(requested_architectures))
            raise ValueError(
                f"pool requests architecture {requested}, but this host is {NATIVE_ARCHITECTURE}"
            )
        return sorted(BUILTIN_LABELS | set(labels))

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, value: list[str]) -> list[str]:
        aliases = list(dict.fromkeys(item.strip().lower() for item in value if item.strip()))
        if set(aliases) & ARCHITECTURE_LABELS:
            raise ValueError("pool aliases must not contain architecture labels")
        return sorted(set(aliases) - BUILTIN_LABELS)

    @model_validator(mode="after")
    def validate_limits(self) -> RunnerPoolConfig:
        if self.min > self.max:
            raise ValueError("runner pool min must not exceed max")
        if self.max_lifetime < self.job_timeout:
            raise ValueError("max_lifetime must be greater than or equal to job_timeout")
        return self

    @property
    def effective_labels(self) -> set[str]:
        return set(self.labels) | BUILTIN_LABELS

    @property
    def matching_labels(self) -> set[str]:
        """Labels accepted for jobs and registered on compatibility runners."""
        return self.effective_labels | set(self.aliases)

    @property
    def custom_labels(self) -> list[str]:
        return sorted(self.matching_labels - BUILTIN_LABELS)


class WorkflowJob(BaseModel):
    id: int
    run_id: int | None = None
    repository: str
    connection_id: str | None = None
    name: str = ""
    labels: list[str] = Field(default_factory=list)
    status: Literal["queued", "in_progress", "completed"]
    conclusion: str | None = None
    pool: str | None = None
    runner_name: str | None = None
    queued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @field_validator("labels")
    @classmethod
    def lower_labels(cls, value: list[str]) -> list[str]:
        return sorted({label.lower() for label in value})


class ManagedRunner(BaseModel):
    runner_id: str
    name: str
    pool: str
    container_id: str
    container_status: str
    created_at: datetime
    labels: list[str] = Field(default_factory=list)
    repository: str | None = None
    connection_id: str | None = None
    compose_project: str | None = None
    state: Literal["starting", "idle", "busy", "exited", "unknown"] = "starting"
    github_runner_id: int | None = None
    github_status: str | None = None
    busy: bool = False
    busy_since: datetime | None = None
    idle_since: datetime | None = None
    exit_code: int | None = None

    def uptime_seconds(self, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        return max(0, int((current - self.created_at).total_seconds()))


class ScaleRequest(BaseModel):
    desired: int = Field(ge=0)
    ttl_seconds: int = Field(default=600, ge=30, le=86400)
    repository: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    connection_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class TokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scope: TokenScope = TokenScope.READ
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class GitHubSetupRequest(BaseModel):
    connection_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        pattern=r"^[a-f0-9]{32}$",
    )
    scope: GitHubScope
    owner: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    repository: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    app_owner_kind: Literal["user", "organization"] = "user"
    webhook_enabled: bool = True


class GitHubConnectRequest(BaseModel):
    target_url: str = Field(min_length=3, max_length=500)
    organization_wide: bool = False
    webhook_enabled: bool = True


class PoolYamlRequest(BaseModel):
    yaml: str = Field(min_length=1, max_length=100_000)


class DiagnosticSettings(BaseModel):
    capture_enabled: bool = True
    cleanup_enabled: bool = True
    retention_days: int = Field(default=7, ge=1, le=365)


class DockerCleanupRequest(BaseModel):
    dry_run: bool = True
    include_volumes: bool = False
    target_keys: list[str] | None = Field(default=None, max_length=1000)
