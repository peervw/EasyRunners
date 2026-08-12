from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from runner_manager.models import GitHubScope, RunnerPoolConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
    )

    public_url: str = "http://localhost:8080"
    allow_insecure_public_url: bool = False
    manager_host: str = "0.0.0.0"  # noqa: S104 - container listener, host binding is restricted
    manager_port: int = 8080
    manager_bind_address: str = "127.0.0.1"
    log_level: str = "INFO"
    config_path: Path = Path("config.yaml")
    data_dir: Path = Path("/data")
    instance_id: str = "primary"
    trusted_proxy_cidrs: str = "127.0.0.1/32,172.16.0.0/12"
    session_ttl_seconds: int = 43200
    login_attempts: int = 5
    login_window_seconds: int = 300

    reconcile_interval: int = 10
    queue_poll_interval: int = 60
    full_poll_interval: int = 300
    poll_concurrency: int = 5
    poll_max_repositories: int = 500
    poll_runs_per_repo: int = 20
    assignment_grace_seconds: int = 15
    history_limit: int = 500
    runner_log_capture_enabled: bool = True
    runner_log_cleanup_enabled: bool = True
    runner_log_retention_days: int = 7
    cleanup_idle_on_shutdown: bool = False
    webhook_enabled: bool = True
    adoption_scan_interval: int = 600
    adoption_runs_per_repo: int = 5
    adoption_max_repositories: int = 100
    host_resource_cache_seconds: int = 15

    notification_webhook_url: SecretStr | None = None
    notification_webhook_secret: SecretStr | None = None
    notification_stuck_job_seconds: int = 900
    notification_cooldown_seconds: int = 900

    manager_repository: str = "peervw/EasyRunners"

    github_auth_mode: Literal["onboarding", "app", "pat", "auto"] = "onboarding"
    github_api_url: str = "https://api.github.com"
    github_web_url: str = "https://github.com"
    github_api_version: str = "2026-03-10"
    github_scope: GitHubScope | None = None
    github_owner: str | None = None
    github_repo: str | None = None
    github_org: str | None = None
    github_app_id: int | None = None
    github_installation_id: int | None = None
    github_app_private_key: SecretStr | None = None
    github_app_private_key_path: Path | None = None
    github_webhook_secret: SecretStr | None = None
    github_token: SecretStr | None = None

    docker_host: str = "unix:///var/run/docker.sock"
    runner_network: str | None = "easy-runners"
    runner_image: str = "easy-runners-runner:latest"
    runner_version: str = "2.336.0"
    docker_socket_path: Path = Path("/var/run/docker.sock")

    runner_min: int | None = None
    runner_max: int | None = None
    runner_labels: str | None = None
    runner_cpu_limit: float | None = None
    runner_memory_limit: str | None = None
    runner_job_timeout: int | None = None
    runner_docker_mode: str | None = None

    runner_pools: dict[str, RunnerPoolConfig] = Field(default_factory=dict)

    @field_validator("public_url", "github_api_url", "github_web_url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("instance_id")
    @classmethod
    def valid_instance(cls, value: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_.-]", "-", value).strip("-")
        if not normalized:
            raise ValueError("INSTANCE_ID must contain a valid character")
        return normalized[:24]

    @model_validator(mode="after")
    def validate_pool_definitions(self) -> Settings:
        if (
            self.notification_webhook_url
            and not self.allow_insecure_public_url
            and not self.notification_webhook_url.get_secret_value().startswith("https://")
        ):
            raise ValueError(
                "NOTIFICATION_WEBHOOK_URL must use HTTPS "
                "(or set ALLOW_INSECURE_PUBLIC_URL=true for development)"
            )
        fingerprints: dict[frozenset[str], str] = {}
        for name, pool in self.runner_pools.items():
            if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
                raise ValueError(f"invalid pool name: {name}")
            fingerprint = frozenset(pool.effective_labels)
            previous = fingerprints.get(fingerprint)
            if previous:
                raise ValueError(f"pools {previous!r} and {name!r} have identical labels")
            fingerprints[fingerprint] = name
        return self

    def assert_production_safe(self) -> None:
        if not self.allow_insecure_public_url and not self.public_url.startswith("https://"):
            raise ValueError("PUBLIC_URL must use HTTPS (or set ALLOW_INSECURE_PUBLIC_URL=true)")

    def image_for_pool(self, pool: RunnerPoolConfig) -> str:
        return pool.image or self.runner_image


def _apply_yaml(settings: Settings, raw: dict[str, Any]) -> Settings:
    updates: dict[str, Any] = {}
    manager = raw.get("manager", {})
    for key, value in manager.items():
        env_key = key.upper()
        if env_key not in os.environ and hasattr(settings, key):
            updates[key] = value
    github = raw.get("github", {})
    for key, value in github.items():
        field = f"github_{key}"
        if field.upper() not in os.environ and hasattr(settings, field):
            updates[field] = value
    if "runner_pools" in raw:
        updates["runner_pools"] = {
            name: RunnerPoolConfig.model_validate(pool)
            for name, pool in raw["runner_pools"].items()
        }
    return settings.model_copy(update=updates)


def _default_pool(settings: Settings) -> RunnerPoolConfig:
    return RunnerPoolConfig(image=settings.runner_image)


def load_settings() -> Settings:
    settings = Settings()
    path = settings.config_path
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must contain a YAML mapping")
        settings = _apply_yaml(settings, raw)

    pools = dict(settings.runner_pools) or {"default": _default_pool(settings)}
    default = pools.get("default", _default_pool(settings)).model_copy(deep=True)
    overrides: dict[str, Any] = {}
    if settings.runner_min is not None:
        overrides["min"] = settings.runner_min
    if settings.runner_max is not None:
        overrides["max"] = settings.runner_max
    if settings.runner_labels:
        overrides["labels"] = settings.runner_labels.split(",")
    if settings.runner_cpu_limit is not None:
        overrides["cpu"] = settings.runner_cpu_limit
    if settings.runner_memory_limit:
        overrides["memory"] = settings.runner_memory_limit
    if settings.runner_job_timeout is not None:
        overrides["job_timeout"] = settings.runner_job_timeout
    if settings.runner_docker_mode:
        overrides["docker_mode"] = settings.runner_docker_mode
    if os.getenv("RUNNER_IMAGE"):
        overrides["image"] = settings.runner_image
    pools["default"] = RunnerPoolConfig.model_validate({**default.model_dump(), **overrides})
    result = settings.model_copy(update={"runner_pools": pools})
    return Settings.model_validate(result.model_dump())
