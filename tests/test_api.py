import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import runner_manager.main as main_module
from runner_manager.database import Database
from runner_manager.main import create_app
from runner_manager.models import NATIVE_ARCHITECTURE, GitHubSetupRequest, ManagedRunner


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

    async def resource_inventory(self, *, refresh: bool = False):
        return {
            "counts": {
                "networks": 4,
                "containers": 2,
                "stopped_containers": 1,
                "volumes": 3,
                "suspected_leftovers": 1,
                "eligible_leftovers": 1,
            },
            "warning": False,
            "network_warning_threshold": 24,
            "cleanup_enabled": True,
            "cleanup_volumes": False,
            "grace_seconds": 300,
            "targets": [{"kind": "network", "name": "owned", "eligible": True}],
        }

    async def cleanup_orphans(
        self, *, dry_run: bool, include_volumes: bool, target_keys=None
    ):
        return {
            "dry_run": dry_run,
            "include_volumes": include_volumes,
            "target_keys": target_keys,
            "targets": [{"kind": "network", "name": "owned", "eligible": True}],
            "removed": [],
            "errors": [],
        }


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
    assert test_client.get("/api/docker/resources").status_code == 401


def test_docker_cleanup_preview_requires_manage_auth(client) -> None:
    test_client, app = client
    _, csrf = login(test_client, app)
    inventory = test_client.get("/api/docker/resources")
    assert inventory.status_code == 200
    assert inventory.json()["counts"]["networks"] == 4
    assert (
        test_client.post(
            "/api/docker/resources/cleanup",
            json={"dry_run": True, "include_volumes": False},
        ).status_code
        == 403
    )
    preview = test_client.post(
        "/api/docker/resources/cleanup",
        headers={"X-CSRF-Token": csrf},
        json={"dry_run": True, "include_volumes": False, "target_keys": None},
    )
    assert preview.status_code == 200
    assert preview.json()["targets"][0]["name"] == "owned"


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
    assert "Diagnostic log retention" in dashboard.text
    assert "Runner Docker cleanup" in dashboard.text
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

    def repositories(connection_id):
        return ["peer/one", "peer/two"]

    monkeypatch.setattr(app.state.github, "cached_repositories", repositories)
    response = test_client.get("/api/github")
    assert response.status_code == 200
    assert response.json()["repositories"] == ["peer/one", "peer/two"]
    assert response.json()["repository_bound"] is True
    assert response.json()["configure_url"].endswith("/settings/installations/2")


def test_github_status_is_cache_only(
    client, monkeypatch
) -> None:
    test_client, app = client
    _, _ = login(test_client, app)
    app.state.github_store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer"),
        {"id": 1, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "secret"},
    )
    app.state.github_store.save_installation(2, repository_selection="selected")

    def repositories(connection_id):
        return ["peer/one", "peer/two"]

    async def remote_call(*args, **kwargs):
        raise AssertionError("dashboard refresh must not call GitHub")

    monkeypatch.setattr(app.state.github, "cached_repositories", repositories)
    monkeypatch.setattr(app.state.github, "refresh_installation_metadata", remote_call)
    monkeypatch.setattr(app.state.github, "list_repositories", remote_call)

    response = test_client.get("/api/github")

    assert response.status_code == 200
    assert response.json()["repositories"] == ["peer/one", "peer/two"]
    assert response.json()["metadata_error"] is None
    assert response.json()["repositories_error"] is None


def test_github_connection_refresh_is_explicit_and_csrf_protected(
    client, monkeypatch
) -> None:
    test_client, app = client
    _, csrf = login(test_client, app)
    app.state.github_store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer"),
        {"id": 1, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "secret"},
    )
    connection = app.state.github_store.save_installation(
        2, repository_selection="selected"
    )
    calls: list[str] = []

    async def metadata(connection_id, *, refresh=False):
        assert connection_id == connection.id
        assert refresh is True
        calls.append("metadata")
        return connection

    async def repositories(*, connection_id=None, refresh=False):
        assert connection_id == connection.id
        assert refresh is True
        calls.append("repositories")
        return ["peer/one", "peer/two"]

    monkeypatch.setattr(app.state.github, "refresh_installation_metadata", metadata)
    monkeypatch.setattr(app.state.github, "list_repositories", repositories)

    path = f"/api/github/connections/{connection.id}/refresh"
    assert test_client.post(path).status_code == 403
    response = test_client.post(path, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert response.json()["repositories"] == ["peer/one", "peer/two"]
    assert calls == ["metadata", "repositories"]


def test_github_status_and_disconnect_are_connection_scoped(client, monkeypatch) -> None:
    test_client, app = client
    _, csrf = login(test_client, app)
    peer = GitHubSetupRequest(connection_id="a" * 32, scope="repo", owner="peer")
    acme = GitHubSetupRequest(
        connection_id="b" * 32,
        scope="org",
        owner="acme",
        app_owner_kind="organization",
    )
    for setup, app_id, installation in ((peer, 1, 11), (acme, 2, 22)):
        app.state.github_store.save_manifest_result(
            setup,
            {
                "id": app_id,
                "slug": f"app-{app_id}",
                "pem": f"KEY-{app_id}",
                "webhook_secret": f"secret-{app_id}",
            },
        )
        app.state.github_store.save_installation(setup.connection_id, installation)

    def repositories(connection_id):
        return {
            peer.connection_id: ["peer/one"],
            acme.connection_id: ["acme/service"],
        }[connection_id]

    monkeypatch.setattr(app.state.github, "cached_repositories", repositories)
    response = test_client.get("/api/github")
    assert response.status_code == 200
    body = response.json()
    assert [item["connection"]["owner"] for item in body["connections"]] == [
        "peer",
        "acme",
    ]
    assert body["repositories"] == ["acme/service", "peer/one"]
    assert body["connection"] is None
    assert body["configure_url"] is None

    app.state.scheduler._runners = [
        ManagedRunner(
            runner_id="active",
            name="er-test-default-active",
            pool="default",
            container_id="container",
            container_status="running",
            created_at=datetime.now(UTC),
            connection_id=peer.connection_id,
        )
    ]
    blocked = test_client.post(
        f"/api/github/connections/{peer.connection_id}/disconnect",
        headers={"X-CSRF-Token": csrf},
    )
    assert blocked.status_code == 409
    app.state.scheduler._runners = []
    disconnected = test_client.post(
        f"/api/github/connections/{peer.connection_id}/disconnect",
        headers={"X-CSRF-Token": csrf},
    )
    assert disconnected.status_code == 204
    assert app.state.github_store.connection(peer.connection_id) is None
    assert app.state.github_store.connection(acme.connection_id) is not None


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

    refreshes: list[bool] = []

    async def adoption(pools, *, refresh=False, wait=True):
        assert "default" in pools
        assert wait is False
        refreshes.append(refresh)
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

    response = test_client.get("/api/repositories/adoption")
    assert response.status_code == 200
    assert response.json()["repositories"][0]["status"] == "needs_migration"
    scanned = test_client.post(
        "/api/repositories/adoption/scan", headers={"X-CSRF-Token": csrf}
    )
    assert scanned.status_code == 200
    assert refreshes == [False, True]
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

    async def test_runner(pool=None, repository=None, connection_id=None):
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


def test_webhooks_use_the_matching_installation_secret(client) -> None:
    test_client, app = client
    peer = GitHubSetupRequest(connection_id="a" * 32, scope="repo", owner="peer")
    acme = GitHubSetupRequest(connection_id="b" * 32, scope="repo", owner="acme")
    for setup, app_id, installation, secret in (
        (peer, 1, 11, "peer-secret"),
        (acme, 2, 22, "acme-secret"),
    ):
        app.state.github_store.save_manifest_result(
            setup,
            {
                "id": app_id,
                "slug": f"app-{app_id}",
                "pem": f"KEY-{app_id}",
                "webhook_secret": secret,
            },
        )
        app.state.github_store.save_installation(setup.connection_id, installation)

    def delivery(owner, installation, job_id, secret):
        payload = {
            "action": "queued",
            "installation": {"id": installation},
            "repository": {"full_name": f"{owner}/project"},
            "workflow_job": {
                "id": job_id,
                "name": "test",
                "labels": ["self-hosted", "linux", NATIVE_ARCHITECTURE, "docker"],
            },
        }
        raw = json.dumps(payload).encode()
        return raw, {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256="
            + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
            "X-GitHub-Delivery": "same-delivery-id",
            "X-GitHub-Event": "workflow_job",
        }

    peer_raw, peer_headers = delivery("peer", 11, 101, "peer-secret")
    acme_raw, acme_headers = delivery("acme", 22, 202, "acme-secret")
    assert test_client.post(
        "/webhooks/github", content=peer_raw, headers=peer_headers
    ).status_code == 200
    assert test_client.post(
        "/webhooks/github", content=acme_raw, headers=acme_headers
    ).status_code == 200
    jobs = {job.id: job for job in app.state.demand._jobs.values()}
    assert jobs[101].connection_id == peer.connection_id
    assert jobs[202].connection_id == acme.connection_id

    wrong_raw, wrong_headers = delivery("acme", 22, 303, "peer-secret")
    wrong_headers["X-GitHub-Delivery"] = "wrong-secret"
    assert (
        test_client.post(
            "/webhooks/github", content=wrong_raw, headers=wrong_headers
        ).status_code
        == 401
    )


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
