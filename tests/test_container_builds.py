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


def test_one_compose_builds_every_image_from_source() -> None:
    assert not (REPOSITORY_ROOT / "compose.prebuilt.yaml").exists()
    compose = yaml.safe_load((REPOSITORY_ROOT / "compose.yaml").read_text())
    services = compose["services"]
    for name in ("manager", "runner-image"):
        assert services[name]["build"]
        assert services[name]["image"].startswith("${")
        assert "easy-runners-" in services[name]["image"]
    assert "rust-runner-image" not in services
    assert services["runner-image"]["build"]["target"] == "runner"
    assert "RUST_TOOLCHAIN" in services["runner-image"]["build"]["args"]
    assert set(services["manager"]["depends_on"]) == {"runner-image"}
    runner_dockerfile = (REPOSITORY_ROOT / "runner" / "Dockerfile").read_text()
    assert len(re.findall(r"^FROM ", runner_dockerfile, flags=re.MULTILINE)) == 1
    assert "rustup toolchain install" in runner_dockerfile
    manager_volumes = services["manager"]["volumes"]
    assert "easy-runners-data:/data" in manager_volumes
    assert all(not volume.startswith("./") for volume in manager_volumes)
    assert "COPY config.yaml ./config.yaml" in (REPOSITORY_ROOT / "Dockerfile").read_text()
    assert not (REPOSITORY_ROOT / ".github/workflows/release-images.yml").exists()
    assert "NOTIFICATION_WEBHOOK_URL" in services["manager"]["environment"]


def test_ci_and_examples_pin_actions_to_immutable_commits() -> None:
    workflow_files = [
        *sorted((REPOSITORY_ROOT / ".github/workflows").glob("*.yml")),
        *sorted((REPOSITORY_ROOT / "examples").glob("*.yaml")),
    ]
    for path in workflow_files:
        references = re.findall(r"uses:\s+[^\s@]+@([^\s#]+)", path.read_text())
        assert references, f"{path} has no action references"
        assert all(re.fullmatch(r"[0-9a-f]{40}", reference) for reference in references)

    ci = (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
    assert "uv run ruff check ." in ci
    assert "uv run mypy src" in ci
    assert "linux/amd64,linux/arm64" in ci


def test_dependency_and_release_automation_is_configured() -> None:
    dependabot = yaml.safe_load(
        (REPOSITORY_ROOT / ".github/dependabot.yml").read_text()
    )
    ecosystems = {item["package-ecosystem"] for item in dependabot["updates"]}
    assert ecosystems == {"uv", "github-actions"}

    release = yaml.safe_load(
        (REPOSITORY_ROOT / "release-please-config.json").read_text()
    )
    assert release["release-type"] == "simple"
    assert set(release["extra-files"]) == {"pyproject.toml", "uv.lock"}
    assert "x-release-please-version" in (REPOSITORY_ROOT / "pyproject.toml").read_text()
    assert "x-release-please-version" in (REPOSITORY_ROOT / "uv.lock").read_text()


def test_default_config_offers_a_no_socket_ci_pool() -> None:
    config = yaml.safe_load((REPOSITORY_ROOT / "config.yaml").read_text())
    ci = config["runner_pools"]["ci"]
    assert ci["docker_mode"] == "none"
    assert "ci" in ci["labels"]
    assert config["runner_pools"]["default"]["docker_mode"] == "socket"
