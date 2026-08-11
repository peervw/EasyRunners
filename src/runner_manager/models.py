from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class GitHubScope(StrEnum):
    REPO = "repo"
    ORG = "org"


class DockerMode(StrEnum):
    SOCKET = "socket"
    NONE = "none"


BUILTIN_LABELS = {"self-hosted", "linux", "x64"}


class RunnerPoolConfig(BaseModel):
    labels: list[str] = Field(default_factory=lambda: ["self-hosted", "linux", "x64"])
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
    docker_mode: DockerMode = DockerMode.SOCKET
    runner_group: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    environment_from: list[str] = Field(default_factory=list)
    volumes: list[str] = Field(default_factory=list)

    @field_validator("labels")
    @classmethod
    def normalize_labels(cls, value: list[str]) -> list[str]:
        labels = list(dict.fromkeys(item.strip().lower() for item in value if item.strip()))
        return sorted(BUILTIN_LABELS | set(labels))

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
    def custom_labels(self) -> list[str]:
        return sorted(self.effective_labels - BUILTIN_LABELS)


class WorkflowJob(BaseModel):
    id: int
    run_id: int | None = None
    repository: str
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


class TokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class GitHubSetupRequest(BaseModel):
    scope: GitHubScope
    owner: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    repository: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    app_owner_kind: Literal["user", "organization"] = "user"
    webhook_enabled: bool = True

    @model_validator(mode="after")
    def require_repository(self) -> GitHubSetupRequest:
        if self.scope == GitHubScope.REPO and not self.repository:
            raise ValueError("repository is required for repository scope")
        return self


class GitHubConnectRequest(BaseModel):
    target_url: str = Field(min_length=3, max_length=500)
    organization_wide: bool = False
    webhook_enabled: bool = True


class PoolYamlRequest(BaseModel):
    yaml: str = Field(min_length=1, max_length=100_000)
