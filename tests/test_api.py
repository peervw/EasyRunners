import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import runner_manager.main as main_module
from runner_manager.database import Database
from runner_manager.main import create_app
from runner_manager.models import NATIVE_ARCHITECTURE, GitHubSetupRequest


class NoopDocker:
    def __init__(self, settings) -> None:
        self.settings = settings

    async def close(self) -> None:
        return None

    async def ping(self) -> bool:
        return True

    async def image_exists(self, image: str) -> bool:
        return True

    async def list_managed(self):
        return []

    async def prune_logs(self):
        return None


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr(main_module, "DockerRunnerManager", NoopDocker)
    app = create_app(settings, start_scheduler=False)
    with TestClient(app) as test_client:
        yield test_client, app


def login(client: TestClient, app: Any) -> tuple[str, str]:
    password = app.state.auth.bootstrap_password
    assert password
    response = client.post("/auth/login", data={"password": password}, follow_redirects=False)
    assert response.status_code == 303
    session = app.state.auth.verify_session(client.cookies.get(app.state.auth.cookie_name))
    assert session
    assert client.get("/api/status").status_code == 403
    replacement = "a-new-dashboard-password"
    response = client.post(
        "/auth/password",
        data={
            "current_password": password,
            "new_password": replacement,
            "csrf_token": session["csrf"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client.post("/auth/login", data={"password": replacement}, follow_redirects=False)
    assert response.status_code == 303
    session = app.state.auth.verify_session(client.cookies.get(app.state.auth.cookie_name))
    assert session
    return replacement, session["csrf"]


def test_health_is_public_but_api_is_protected(client) -> None:
    test_client, _ = client
    assert test_client.get("/health").json() == {"status": "ok"}
    assert test_client.get("/api/status").status_code == 401
    assert test_client.get("/metrics").status_code == 401


def test_login_dashboard_csrf_and_api_token(client) -> None:
    test_client, app = client
    _, csrf = login(test_client, app)
    dashboard = test_client.get("/")
    assert dashboard.status_code == 200
    assert dashboard.headers["Cache-Control"] == "no-store"
    assert "GitHub account or organization URL" in dashboard.text
    assert 'aria-label="Main navigation"' in dashboard.text
    assert "Runner pools" in dashboard.text
    assert "GitHub integration" in dashboard.text
    assert 'id="repository-browser"' in dashboard.text
    assert 'placeholder="Search repositories…"' in dashboard.text
    assert 'id="migration-drawer"' in dashboard.text
    assert 'class="card adoption-card"' not in dashboard.text
    assert 'data-activity-tab="diagnostics"' in dashboard.text
    assert "Diagnostic retention" in dashboard.text
    asset_version = app.state.templates.env.globals["asset_version"]
    assert f'/static/app.js?v={asset_version}' in dashboard.text
    versioned_asset = test_client.get(f"/static/app.js?v={asset_version}")
    assert versioned_asset.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert test_client.get("/static/app.js").headers["Cache-Control"] == "no-cache"
    assert test_client.post("/api/reconcile").status_code == 403
    response = test_client.post(
        "/api/auth/tokens",
        headers={"X-CSRF-Token": csrf},
        json={"name": "automation", "scope": "manage", "expires_in_days": 30},
    )
    assert response.status_code == 200
    token = response.json()["token"]
    assert (
        test_client.get("/api/status", headers={"Authorization": f"Bearer {token}"}).status_code
        == 200
    )
    read_response = test_client.post(
        "/api/auth/tokens",
        headers={"X-CSRF-Token": csrf},
        json={"name": "dashboard", "scope": "read"},
    )
    read_token = read_response.json()["token"]
    assert (
        test_client.get(
            "/api/status", headers={"Authorization": f"Bearer {read_token}"}
        ).status_code
        == 200
    )
    assert (
        test_client.post(
            "/api/reconcile", headers={"Authorization": f"Bearer {read_token}"}
        ).status_code
        == 403
    )
    metrics_response = test_client.post(
        "/api/auth/tokens",
        headers={"X-CSRF-Token": csrf},
        json={"name": "prometheus", "scope": "metrics"},
    )
    metrics_token = metrics_response.json()["token"]
    metrics_headers = {"Authorization": f"Bearer {metrics_token}"}
    assert test_client.get("/metrics", headers=metrics_headers).status_code == 200
    assert test_client.get("/api/status", headers=metrics_headers).status_code == 403
    assert (
        test_client.post(
            "/api/pools/default/scale",
            headers={"Authorization": f"Bearer {token}"},
            json={"desired": 0, "ttl_seconds": 600},
        ).status_code
        == 200
    )


def test_manifest_endpoint_builds_external_post(client) -> None:
    test_client, app = client
    _, csrf = login(test_client, app)
    response = test_client.post(
        "/api/github/setup/manifest",
        headers={"X-CSRF-Token": csrf},
        json={
            "scope": "repo",
            "owner": "peer",
            "repository": "repo",
            "app_owner_kind": "user",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["action"].startswith("https://github.com/settings/apps/new?state=")
    assert body["manifest"]["default_permissions"] == {
        "actions": "read",
        "administration": "write",
    }


def test_one_url_setup_and_callback_login_recovery(client, monkeypatch) -> None:
    test_client, app = client
    _, csrf = login(test_client, app)

    async def resolve(setup):
        assert setup.target_url == "https://github.com/peer"
        return GitHubSetupRequest(scope="repo", owner="peer")

    monkeypatch.setattr(app.state.github, "resolve_setup", resolve)
    response = test_client.post(
        "/api/github/setup/manifest",
        headers={"X-CSRF-Token": csrf},
        json={"target_url": "https://github.com/peer"},
    )
    assert response.status_code == 200
    test_client.cookies.clear()
    callback = test_client.get(
        "/setup/github/callback?code=code&state=state", follow_redirects=False
    )
    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/auth/login?next=")


def test_pool_crud_yaml_and_readiness(client) -> None:
    test_client, app = client
    _, csrf = login(test_client, app)
    response = test_client.put(
        "/api/pools/build",
        headers={"X-CSRF-Token": csrf},
        json={"labels": ["build"], "min": 0, "max": 2, "docker_mode": "none"},
    )
    assert response.status_code == 200
    assert "build" in response.json()["pools"]
    exported = test_client.get("/api/pools/config.yaml")
    assert "runner_pools:" in exported.text
    readiness = test_client.get("/api/readiness").json()
    assert readiness["checks"]["docker"]["ok"] is True
    assert readiness["checks"]["runner_images"]["ok"] is True

    imported = test_client.put(
        "/api/pools/config",
        headers={"X-CSRF-Token": csrf},
        json={
            "yaml": (
                "runner_pools:\n  isolated:\n    labels: [isolated]\n"
                "    max: 1\n    docker_mode: none\n"
            )
        },
    )
    assert imported.status_code == 200
    assert set(imported.json()["pools"]) == {"isolated"}
    assert app.state.database.get_setting("runner_pools_override")
    assert test_client.get("/api/pools/default/workflow").status_code == 404


def test_github_status_lists_selected_repositories(client, monkeypatch) -> None:
    test_client, app = client
    _, _ = login(test_client, app)
    app.state.github_store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer"),
        {"id": 1, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "secret"},
    )
    app.state.github_store.save_installation(2, repository_selection="selected")

    async def metadata(*, refresh=False):
        return app.state.github_store.credentials().connection

    async def repositories(*, refresh=False):
        return ["peer/one", "peer/two"]

    monkeypatch.setattr(app.state.github, "refresh_installation_metadata", metadata)
    monkeypatch.setattr(app.state.github, "list_repositories", repositories)
    response = test_client.get("/api/github")
    assert response.status_code == 200
    assert response.json()["repositories"] == ["peer/one", "peer/two"]
    assert response.json()["repository_bound"] is True
    assert response.json()["configure_url"].endswith("/settings/installations/2")


def test_github_status_lists_repositories_when_metadata_refresh_fails(
    client, monkeypatch
) -> None:
    test_client, app = client
    _, _ = login(test_client, app)
    app.state.github_store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer"),
        {"id": 1, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "secret"},
    )
    app.state.github_store.save_installation(2, repository_selection="selected")

    async def metadata(*, refresh=False):
        raise httpx.HTTPError("installation metadata unavailable")

    async def repositories(*, refresh=False):
        return ["peer/one", "peer/two"]

    monkeypatch.setattr(app.state.github, "refresh_installation_metadata", metadata)
    monkeypatch.setattr(app.state.github, "list_repositories", repositories)

    response = test_client.get("/api/github")

    assert response.status_code == 200
    assert response.json()["repositories"] == ["peer/one", "peer/two"]
    assert response.json()["metadata_error"] == "installation metadata unavailable"
    assert response.json()["repositories_error"] is None


def test_diagnostic_archives_are_authenticated_and_downloadable(client) -> None:
    test_client, app = client
    directory = app.state.settings.data_dir / "runner-logs"
    directory.mkdir()
    (directory / "runner-one.tar").write_bytes(b"archive")
    (directory / "manager-secret.txt").write_text("not a diagnostic")
    (directory / "linked.log").symlink_to(directory / "runner-one.tar")
    assert test_client.get("/api/diagnostics").status_code == 401

    _, csrf = login(test_client, app)
    response = test_client.get("/api/diagnostics")
    assert [item["name"] for item in response.json()] == ["runner-one.tar"]
    settings_response = test_client.get("/api/settings/diagnostics")
    settings_body = settings_response.json()
    assert settings_body.pop("oldest_at")
    assert settings_body == {
        "capture_enabled": True,
        "cleanup_enabled": True,
        "retention_days": 7,
        "file_count": 1,
        "total_size": 7,
    }
    updated = test_client.put(
        "/api/settings/diagnostics",
        headers={"X-CSRF-Token": csrf},
        json={"capture_enabled": False, "cleanup_enabled": False, "retention_days": 14},
    )
    assert updated.status_code == 200
    assert updated.json()["capture_enabled"] is False
    assert updated.json()["cleanup_enabled"] is False
    assert app.state.database.get_setting("diagnostic_settings")
    assert test_client.get("/api/diagnostics/runner-one.tar").content == b"archive"
    assert test_client.get("/api/diagnostics/manager-secret.txt").status_code == 404
    assert test_client.get("/api/diagnostics/linked.log").status_code == 404
    assert test_client.get("/api/diagnostics/%2E%2E%2Fstate.sqlite3").status_code == 404
    cleared = test_client.delete(
        "/api/diagnostics", headers={"X-CSRF-Token": csrf}
    )
    assert cleared.json() == {"deleted": 1, "released_bytes": 7}
    assert (directory / "manager-secret.txt").exists()
    assert (directory / "linked.log").is_symlink()


def test_usage_and_update_status(client, monkeypatch) -> None:
    test_client, app = client
    _, _ = login(test_client, app)

    async def latest_runner():
        return "9.0.0"

    async def latest_manager_release():
        return {
            "tag": "v9.0.0",
            "version": "9.0.0",
            "name": "9.0.0",
            "published_at": "2026-08-12T00:00:00Z",
            "url": "https://github.com/peervw/EasyRunners/releases/tag/v9.0.0",
        }

    monkeypatch.setattr(app.state.github, "latest_runner_version", latest_runner)
    monkeypatch.setattr(app.state.github, "latest_manager_release", latest_manager_release)
    assert set(test_client.get("/api/usage").json()) == {"24h", "7d"}
    versions = test_client.get("/api/version").json()
    assert versions["runner_update_available"] is True
    assert versions["manager_update_available"] is True
    assert versions["latest_manager_release"]["tag"] == "v9.0.0"
    assert versions["source_update_command"].startswith("git pull --ff-only")


def test_repository_adoption_and_notification_endpoints(client, monkeypatch) -> None:
    test_client, app = client
    _, csrf = login(test_client, app)
    app.state.github_store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer"),
        {"id": 1, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "secret"},
    )
    app.state.github_store.save_installation(2)

    async def adoption(pools, *, refresh=False, wait=True):
        assert "default" in pools
        assert refresh is True
        assert wait is False
        return {
            "repositories": [
                {"repository": "peer/repo", "status": "needs_migration"}
            ],
            "recommended_pool": "default",
            "recommended_runs_on": "runs-on: [self-hosted, linux, docker]",
            "replacements": {},
        }

    delivered: list[str] = []

    async def send(event, title, message, **kwargs):
        delivered.append(event)
        return True

    app.state.settings.notification_webhook_url = SecretStr(
        "https://hooks.example.test/easy-runners"
    )
    monkeypatch.setattr(app.state.github, "repository_adoption", adoption)
    monkeypatch.setattr(app.state.notifications, "send", send)

    response = test_client.get("/api/repositories/adoption?refresh=true")
    assert response.status_code == 200
    assert response.json()["repositories"][0]["status"] == "needs_migration"
    assert test_client.get("/api/notifications").json()["configured"] is True
    tested = test_client.post(
        "/api/notifications/test", headers={"X-CSRF-Token": csrf}
    )
    assert tested.json() == {"delivered": True}
    assert delivered == ["test"]


def test_saved_diagnostic_settings_load_on_restart(settings, monkeypatch) -> None:
    database = Database(settings.data_dir / "easyrunners.sqlite3")
    database.set_setting(
        "diagnostic_settings",
        json.dumps(
            {"capture_enabled": False, "cleanup_enabled": False, "retention_days": 30}
        ),
    )
    database.close()
    monkeypatch.setattr(main_module, "DockerRunnerManager", NoopDocker)
    app = create_app(settings, start_scheduler=False)
    with TestClient(app):
        assert app.state.settings.runner_log_capture_enabled is False
        assert app.state.settings.runner_log_cleanup_enabled is False
        assert app.state.settings.runner_log_retention_days == 30


def test_saved_builtin_pools_migrate_on_restart(settings, monkeypatch) -> None:
    database = Database(settings.data_dir / "easyrunners.sqlite3")
    legacy = json.dumps(
        {
            "default": {"labels": ["docker"], "docker_mode": "socket"},
            "rust": {"labels": ["rust"], "docker_mode": "none"},
        }
    )
    database.set_setting("runner_pools_override", legacy)
    database.close()
    monkeypatch.setattr(main_module, "DockerRunnerManager", NoopDocker)
    app = create_app(settings, start_scheduler=False)
    with TestClient(app):
        assert set(app.state.settings.runner_pools) == {"standard", "docker"}
        assert app.state.settings.runner_pools["standard"].aliases == ["ci", "rust"]
        assert (
            app.state.database.get_setting("runner_pools_override_pre_standard")
            == legacy
        )


def test_runner_request_carries_repository_and_pool(client, monkeypatch) -> None:
    test_client, app = client
    _, csrf = login(test_client, app)
    app.state.github_store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer"),
        {"id": 1, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "secret"},
    )
    app.state.github_store.save_installation(2, repository_selection="selected")

    async def test_runner(pool=None, repository=None):
        assert pool == "standard"
        assert repository == "peer/two"
        return {"pool": pool, "repository": repository}

    monkeypatch.setattr(app.state.scheduler, "test_runner", test_runner)
    response = test_client.post(
        "/api/readiness/test-runner?pool=standard&repository=peer/two",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json() == {"pool": "standard", "repository": "peer/two"}


def test_webhook_signature_replay_and_demand(client) -> None:
    test_client, app = client
    app.state.github_store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer", repository="repo"),
        {"id": 1, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "secret"},
    )
    app.state.github_store.save_installation(2)
    payload = {
        "action": "queued",
        "installation": {"id": 2},
        "repository": {"full_name": "peer/repo"},
        "workflow_job": {
            "id": 7,
            "run_id": 9,
            "name": "test",
            "labels": ["self-hosted", "linux", NATIVE_ARCHITECTURE, "docker"],
        },
    }
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": "delivery-1",
        "X-GitHub-Event": "workflow_job",
    }
    assert test_client.post("/webhooks/github", content=raw, headers=headers).json() == {
        "accepted": True,
        "matched_pool": "default",
    }
    assert test_client.post("/webhooks/github", content=raw, headers=headers).json()["duplicate"]
    bad = {**headers, "X-GitHub-Delivery": "delivery-2", "X-Hub-Signature-256": "sha256=bad"}
    assert test_client.post("/webhooks/github", content=raw, headers=bad).status_code == 401


def test_repo_app_accepts_another_selected_repository_webhook(client) -> None:
    test_client, app = client
    app.state.github_store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer", repository="first"),
        {"id": 1, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "secret"},
    )
    app.state.github_store.save_installation(2, repository_selection="selected")
    payload = {
        "action": "queued",
        "installation": {"id": 2},
        "repository": {"full_name": "peer/second"},
        "workflow_job": {
            "id": 8,
            "run_id": 10,
            "name": "build",
            "labels": ["self-hosted", "linux", NATIVE_ARCHITECTURE, "docker"],
        },
    }
    raw = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": "sha256="
        + hmac.new(b"secret", raw, hashlib.sha256).hexdigest(),
        "X-GitHub-Delivery": "delivery-multi-repo",
        "X-GitHub-Event": "workflow_job",
    }
    response = test_client.post("/webhooks/github", content=raw, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"accepted": True, "matched_pool": "default"}
    payload["repository"]["full_name"] = "attacker/second"
    rejected_raw = json.dumps(payload).encode()
    rejected_headers = {
        **headers,
        "X-GitHub-Delivery": "delivery-wrong-owner",
        "X-Hub-Signature-256": "sha256="
        + hmac.new(b"secret", rejected_raw, hashlib.sha256).hexdigest(),
    }
    assert (
        test_client.post(
            "/webhooks/github", content=rejected_raw, headers=rejected_headers
        ).status_code
        == 403
    )
