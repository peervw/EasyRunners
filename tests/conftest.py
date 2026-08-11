from pathlib import Path

import pytest

from runner_manager.config import Settings
from runner_manager.models import DockerMode, RunnerPoolConfig


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        public_url="http://testserver",
        allow_insecure_public_url=True,
        data_dir=tmp_path / "data",
        config_path=tmp_path / "missing.yaml",
        instance_id="test",
        runner_network=None,
        runner_pools={
            "default": RunnerPoolConfig(
                labels=["self-hosted", "linux", "x64", "docker"],
                min=0,
                max=5,
                docker_mode=DockerMode.NONE,
                idle_timeout=0,
                job_timeout=3600,
                max_lifetime=3900,
            )
        },
    )
