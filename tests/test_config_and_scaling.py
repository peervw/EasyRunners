from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from runner_manager.config import Settings
from runner_manager.demand import match_pool
from runner_manager.models import ManagedRunner, RunnerPoolConfig
from runner_manager.scheduler import calculate_scale


def test_pool_normalizes_effective_and_custom_labels() -> None:
    pool = RunnerPoolConfig(labels=["Docker", "linux", "DOCKER"])
    assert pool.effective_labels == {"self-hosted", "linux", "x64", "docker"}
    assert pool.custom_labels == ["docker"]


def test_pool_rejects_invalid_limits() -> None:
    with pytest.raises(ValidationError, match="min must not exceed max"):
        RunnerPoolConfig(min=3, max=2)
    with pytest.raises(ValidationError, match="max_lifetime"):
        RunnerPoolConfig(job_timeout=1000, max_lifetime=999)


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
