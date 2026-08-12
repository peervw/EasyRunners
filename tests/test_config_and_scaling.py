from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from runner_manager.config import Settings, migrate_legacy_pools
from runner_manager.demand import match_pool
from runner_manager.models import (
    NATIVE_ARCHITECTURE,
    DockerMode,
    ManagedRunner,
    RunnerPoolConfig,
)
from runner_manager.scheduler import calculate_scale


def test_pool_normalizes_effective_and_custom_labels() -> None:
    pool = RunnerPoolConfig(labels=["Docker", "linux", "DOCKER"])
    assert pool.effective_labels == {
        "self-hosted",
        "linux",
        NATIVE_ARCHITECTURE,
        "docker",
    }
    assert pool.custom_labels == ["docker"]


def test_all_pools_use_universal_image_unless_overridden(settings) -> None:
    standard = RunnerPoolConfig(labels=["standard"], docker_mode="none")
    base = RunnerPoolConfig(labels=["docker"], docker_mode="none")
    custom = standard.model_copy(update={"image": "registry.example/custom-runner:1"})
    assert settings.image_for_pool(standard) == settings.runner_image
    assert settings.image_for_pool(base) == settings.runner_image
    assert settings.image_for_pool(custom) == "registry.example/custom-runner:1"


def test_runner_pool_defaults_to_no_docker_socket() -> None:
    assert RunnerPoolConfig().docker_mode == DockerMode.NONE


def test_legacy_builtin_pools_migrate_without_breaking_workflow_labels() -> None:
    common = {"min": 0, "max": 5, "cpu": 4, "memory": "8g"}
    pools, changes = migrate_legacy_pools(
        {
            "ci": RunnerPoolConfig(
                labels=["ci"], docker_mode=DockerMode.NONE, priority=20, **common
            ),
            "rust": RunnerPoolConfig(
                labels=["rust"], docker_mode=DockerMode.NONE, priority=10, **common
            ),
            "default": RunnerPoolConfig(
                labels=["docker"], docker_mode=DockerMode.SOCKET, **common
            ),
        }
    )
    assert set(pools) == {"standard", "docker"}
    assert pools["standard"].aliases == ["ci", "rust"]
    assert match_pool(["self-hosted", "linux", "standard"], pools) == "standard"
    assert match_pool(["self-hosted", "linux", "rust"], pools) == "standard"
    assert match_pool(["self-hosted", "linux", "ci"], pools) == "standard"
    assert match_pool(["self-hosted", "linux", "docker"], pools) == "docker"
    assert match_pool(["self-hosted", "linux"], pools) == "standard"
    assert changes


def test_legacy_migration_preserves_custom_pool_variants() -> None:
    pools, _ = migrate_legacy_pools(
        {
            "ci": RunnerPoolConfig(labels=["ci"], docker_mode=DockerMode.NONE),
            "rust": RunnerPoolConfig(
                labels=["rust"], docker_mode=DockerMode.NONE, max=9
            ),
        }
    )
    assert "standard" in pools
    assert "rust" in pools


def test_pool_rejects_invalid_limits() -> None:
    with pytest.raises(ValidationError, match="min must not exceed max"):
        RunnerPoolConfig(min=3, max=2)
    with pytest.raises(ValidationError, match="max_lifetime"):
        RunnerPoolConfig(job_timeout=1000, max_lifetime=999)


def test_pool_rejects_architecture_that_does_not_match_host() -> None:
    other = "arm64" if NATIVE_ARCHITECTURE == "x64" else "x64"
    with pytest.raises(ValidationError, match="this host"):
        RunnerPoolConfig(labels=["docker", other])


def test_settings_reject_identical_pool_label_sets(tmp_path) -> None:
    with pytest.raises(ValidationError, match="identical labels"):
        Settings(
            public_url="https://runners.example.com",
            config_path=tmp_path / "none",
            runner_pools={
                "one": RunnerPoolConfig(labels=["docker"]),
                "two": RunnerPoolConfig(labels=["docker"]),
            },
        )


def test_notification_webhook_requires_https_in_production(tmp_path) -> None:
    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(
            public_url="https://runners.example.com",
            config_path=tmp_path / "none",
            notification_webhook_url="http://alerts.example.com/hook",
        )
    settings = Settings(
        public_url="https://runners.example.com",
        config_path=tmp_path / "none",
        notification_webhook_url="https://alerts.example.com/hook",
    )
    assert settings.notification_webhook_url is not None


def test_match_pool_prefers_fewest_surplus_then_priority() -> None:
    pools = {
        "wide": RunnerPoolConfig(labels=["docker", "deploy", "gpu"], priority=100),
        "low": RunnerPoolConfig(labels=["docker", "deploy"], priority=1),
        "high": RunnerPoolConfig(labels=["docker", "deploy"], priority=2),
    }
    assert match_pool(["self-hosted", "deploy"], pools) == "high"
    assert match_pool(["ubuntu-latest"], pools) is None


@pytest.mark.parametrize(
    ("queued", "starting", "idle", "busy", "floor", "expected"),
    [
        (2, 0, 0, 0, 0, (2, 2, 0)),
        (2, 0, 1, 0, 0, (2, 1, 0)),
        (0, 0, 2, 1, 0, (1, 0, 2)),
        (0, 0, 0, 0, 3, (3, 3, 0)),
        (99, 0, 0, 0, 0, (5, 5, 0)),
    ],
)
def test_scaling_calculation(
    queued: int,
    starting: int,
    idle: int,
    busy: int,
    floor: int,
    expected: tuple[int, int, int],
) -> None:
    pool = RunnerPoolConfig(min=0, max=5)
    decision = calculate_scale(
        pool,
        queued=queued,
        starting=starting,
        idle=idle,
        busy=busy,
        manual_floor=floor,
    )
    assert (decision.target, decision.create, decision.excess_idle) == expected


def test_runner_uptime_is_nonnegative() -> None:
    now = datetime.now(UTC)
    runner = ManagedRunner(
        runner_id="r",
        name="name",
        pool="default",
        container_id="c",
        container_status="running",
        created_at=now,
    )
    assert runner.uptime_seconds(now) == 0
