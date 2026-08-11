from __future__ import annotations

import re
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _apt_cache_mounts(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if "--mount=type=cache" in line and "target=/var/" in line
    ]


def _cache_ids(mounts: list[str]) -> set[str]:
    ids: set[str] = set()
    for mount in mounts:
        match = re.search(r"id=([^,]+)", mount)
        assert match is not None
        ids.add(match.group(1))
    return ids


def test_apt_cache_mounts_are_locked_and_isolated_by_image() -> None:
    manager_mounts = _apt_cache_mounts(REPOSITORY_ROOT / "Dockerfile")
    runner_mounts = _apt_cache_mounts(REPOSITORY_ROOT / "runner" / "Dockerfile")

    assert manager_mounts
    assert runner_mounts
    assert all("sharing=locked" in mount for mount in manager_mounts + runner_mounts)

    manager_ids = _cache_ids(manager_mounts)
    runner_ids = _cache_ids(runner_mounts)
    assert manager_ids.isdisjoint(runner_ids)


def test_one_compose_supports_source_builds_and_published_images() -> None:
    assert not (REPOSITORY_ROOT / "compose.prebuilt.yaml").exists()
    compose = yaml.safe_load((REPOSITORY_ROOT / "compose.yaml").read_text())
    services = compose["services"]
    for name in ("manager", "runner-image", "rust-runner-image"):
        assert services[name]["build"]
        assert services[name]["image"].startswith("${")
        assert "ghcr.io/peervw/" in services[name]["image"]
    manager_volumes = services["manager"]["volumes"]
    assert "easy-runners-data:/data" in manager_volumes
    assert all(not volume.startswith("./") for volume in manager_volumes)
    assert "COPY config.yaml ./config.yaml" in (REPOSITORY_ROOT / "Dockerfile").read_text()


def test_release_images_use_the_self_hosted_docker_pool() -> None:
    workflow = yaml.safe_load(
        (REPOSITORY_ROOT / ".github/workflows/release-images.yml").read_text()
    )
    assert workflow["jobs"]["publish"]["runs-on"] == [
        "self-hosted",
        "linux",
        "x64",
        "docker",
    ]
