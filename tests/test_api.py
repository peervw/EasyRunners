import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import runner_manager.main as main_module
from runner_manager.main import create_app
from runner_manager.models import GitHubSetupRequest


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
    assert test_client.get("/").status_code == 200
    assert test_client.post("/api/reconcile").status_code == 403
    response = test_client.post(
        "/api/auth/tokens",
        headers={"X-CSRF-Token": csrf},
        json={"name": "automation"},
    )
    assert response.status_code == 200
    token = response.json()["token"]
    assert (
        test_client.get("/api/status", headers={"Authorization": f"Bearer {token}"}).status_code
        == 200
    )
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
        assert setup.target_url == "https://github.com/peer/repo"
        return GitHubSetupRequest(scope="repo", owner="peer", repository="repo")

    monkeypatch.setattr(app.state.github, "resolve_setup", resolve)
    response = test_client.post(
        "/api/github/setup/manifest",
        headers={"X-CSRF-Token": csrf},
        json={"target_url": "https://github.com/peer/repo"},
    )
    assert response.status_code == 200
    test_client.cookies.clear()
    callback = test_client.get(
        "/setup/github/callback?code=code&state=state", follow_redirects=False
    )
    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/auth/login?next=")


def test_pool_crud_yaml_workflow_and_readiness(client) -> None:
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
    workflow = test_client.get("/api/pools/build/workflow?template=python")
    assert workflow.status_code == 200
    assert "runs-on: [self-hosted, linux, x64, build]" in workflow.json()["yaml"]
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
            "labels": ["self-hosted", "linux", "x64", "docker"],
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
