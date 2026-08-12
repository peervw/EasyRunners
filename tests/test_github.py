import asyncio
from pathlib import Path

import httpx
import pytest

from runner_manager.config import Settings
from runner_manager.database import Database
from runner_manager.github import GitHubClient, GitHubConnectionStore, GitHubRateLimitError
from runner_manager.models import (
    DockerMode,
    GitHubConnectRequest,
    GitHubSetupRequest,
    RunnerPoolConfig,
)


def make_stack(tmp_path: Path, handler) -> tuple[GitHubClient, GitHubConnectionStore, Database]:
    settings = Settings(
        public_url="https://runners.example.com",
        data_dir=tmp_path,
        config_path=tmp_path / "none",
        github_auth_mode="onboarding",
        runner_pools={"default": RunnerPoolConfig(labels=["docker"])},
    )
    database = Database(tmp_path / "state.sqlite3")
    store = GitHubConnectionStore(settings, database)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GitHubClient(settings, store, http), store, database


def test_app_jwt_uses_short_lived_rs256_claims(settings, tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "state.sqlite3")
    store = GitHubConnectionStore(settings, database)
    client = GitHubClient(
        settings,
        store,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
    )
    captured = {}

    def encode(payload, key, algorithm):
        captured.update({"payload": payload, "key": key, "algorithm": algorithm})
        return "encoded"

    monkeypatch.setattr("runner_manager.github.jwt.encode", encode)
    assert client.auth.app_jwt(123, "private", now=1000) == "encoded"
    assert captured == {
        "payload": {"iat": 940, "exp": 1540, "iss": "123"},
        "key": "private",
        "algorithm": "RS256",
    }
    database.close()


def test_manifest_has_exact_scope_permissions(settings, tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    store = GitHubConnectionStore(settings, database)
    client = GitHubClient(
        settings,
        store,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
    )
    repo = client.build_manifest(
        GitHubSetupRequest(scope="repo", owner="peervw", repository="project")
    )
    org = client.build_manifest(GitHubSetupRequest(scope="org", owner="peervw"))
    assert repo["default_permissions"] == {"actions": "read", "administration": "write"}
    assert org["default_permissions"] == {
        "actions": "read",
        "organization_self_hosted_runners": "write",
    }
    assert repo["default_events"] == ["workflow_job"]
    database.close()


@pytest.mark.asyncio
async def test_one_url_setup_detects_owner_kind_and_polling_mode(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/users/peervw"
        return httpx.Response(200, json={"type": "Organization"})

    client, _, database = make_stack(tmp_path, handler)
    setup = await client.resolve_setup(
        GitHubConnectRequest(
            target_url="https://github.com/peervw",
            webhook_enabled=False,
        )
    )
    assert setup.owner == "peervw"
    assert setup.repository is None
    assert setup.app_owner_kind == "organization"
    assert setup.webhook_enabled is False
    assert client.build_manifest(setup)["hook_attributes"]["active"] is False
    await client.close()
    database.close()


@pytest.mark.asyncio
async def test_convert_manifest_persists_secrets(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app-manifests/code/conversions"
        return httpx.Response(
            201,
            json={"id": 12, "slug": "easy-test", "pem": "PRIVATE", "webhook_secret": "hook"},
        )

    client, store, database = make_stack(tmp_path, handler)
    connection = await client.convert_manifest(
        "code", GitHubSetupRequest(scope="repo", owner="peer", repository="repo")
    )
    assert connection.app_id == 12
    assert (tmp_path / "github/app.pem").read_text() == "PRIVATE"
    assert (tmp_path / "github/app.pem").stat().st_mode & 0o777 == 0o600
    assert store.credentials(require_installation=False).webhook_secret == "ho" + "ok"
    await client.close()
    database.close()


@pytest.mark.asyncio
async def test_installation_token_cached_and_registration_path(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/app/installations/99/access_tokens":
            return httpx.Response(
                201,
                json={"token": "ghs_token", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path == "/repos/peer/repo/actions/runners/registration-token":
            return httpx.Response(201, json={"token": "registration"})
        raise AssertionError(request.url)

    client, store, database = make_stack(tmp_path, handler)
    store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer", repository="repo"),
        {"id": 12, "slug": "easy-test", "pem": "PRIVATE", "webhook_secret": "hook"},
    )
    store.save_installation(99)
    monkeypatch.setattr(client.auth, "app_jwt", lambda *args: "jwt")
    assert await client.registration_token() == "registration"
    assert await client.registration_token() == "registration"
    assert calls.count("/app/installations/99/access_tokens") == 1
    await client.close()
    database.close()


@pytest.mark.asyncio
async def test_installation_account_is_validated(tmp_path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 99, "account": {"login": "attacker"}})

    client, store, database = make_stack(tmp_path, handler)
    store.save_manifest_result(
        GitHubSetupRequest(scope="org", owner="trusted"),
        {"id": 12, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "hook"},
    )
    monkeypatch.setattr(client.auth, "app_jwt", lambda *args: "jwt")
    with pytest.raises(ValueError, match="does not match"):
        await client.validate_installation(99)
    assert store.credentials(require_installation=False).connection.installation_id is None
    await client.close()
    database.close()


@pytest.mark.asyncio
async def test_installation_repository_selection_is_saved(tmp_path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/99":
            return httpx.Response(
                200,
                json={
                    "id": 99,
                    "account": {"login": "peer"},
                    "repository_selection": "all",
                },
            )
        if request.url.path == "/app/installations/99/access_tokens":
            return httpx.Response(
                201,
                json={"token": "ghs", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                200,
                json={"repositories": [{"full_name": "peer/repo"}]},
            )
        raise AssertionError(request.url)

    client, store, database = make_stack(tmp_path, handler)
    store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer"),
        {"id": 12, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "hook"},
    )
    monkeypatch.setattr(client.auth, "app_jwt", lambda *args: "jwt")
    await client.validate_installation(99)
    assert store.credentials().connection.repository_selection == "all"
    await client.close()
    database.close()


@pytest.mark.asyncio
async def test_account_installation_requires_at_least_one_repository(
    tmp_path, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/99":
            return httpx.Response(
                200,
                json={
                    "id": 99,
                    "account": {"login": "peer"},
                    "repository_selection": "selected",
                },
            )
        if request.url.path == "/app/installations/99/access_tokens":
            return httpx.Response(
                201,
                json={"token": "ghs", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path == "/installation/repositories":
            return httpx.Response(200, json={"repositories": []})
        raise AssertionError(request.url)

    client, store, database = make_stack(tmp_path, handler)
    store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer"),
        {"id": 12, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "hook"},
    )
    monkeypatch.setattr(client.auth, "app_jwt", lambda *args: "jwt")
    with pytest.raises(ValueError, match="at least one repository"):
        await client.validate_installation(99)
    assert store.credentials(require_installation=False).connection.installation_id is None
    await client.close()
    database.close()


@pytest.mark.asyncio
async def test_installation_must_include_target_repository(tmp_path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/99":
            return httpx.Response(200, json={"id": 99, "account": {"login": "peer"}})
        if request.url.path == "/app/installations/99/access_tokens":
            return httpx.Response(
                201,
                json={"token": "ghs", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path == "/repos/peer/repo":
            return httpx.Response(404, json={"message": "Not Found"})
        raise AssertionError(request.url)

    client, store, database = make_stack(tmp_path, handler)
    store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer", repository="repo"),
        {"id": 12, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "hook"},
    )
    monkeypatch.setattr(client.auth, "app_jwt", lambda *args: "jwt")
    with pytest.raises(ValueError, match="not granted access"):
        await client.validate_installation(99)
    assert store.credentials(require_installation=False).connection.installation_id is None
    await client.close()
    database.close()


@pytest.mark.asyncio
async def test_repository_poll_reconstructs_queued_job(tmp_path, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/app/installations/99/access_tokens":
            return httpx.Response(201, json={"token": "ghs", "expires_at": "2099-01-01T00:00:00Z"})
        if path == "/installation/repositories":
            return httpx.Response(
                200,
                json={"repositories": [{"full_name": "peer/repo"}]},
            )
        if path == "/repos/peer/repo/actions/runs":
            status = request.url.params["status"]
            return httpx.Response(
                200,
                json={"workflow_runs": [{"id": 42}] if status == "queued" else []},
            )
        if path == "/repos/peer/repo/actions/runs/42/jobs":
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 7,
                            "name": "test",
                            "status": "queued",
                            "labels": ["self-hosted", "docker"],
                        }
                    ]
                },
            )
        raise AssertionError(request.url)

    client, store, database = make_stack(tmp_path, handler)
    store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer", repository="repo"),
        {"id": 12, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "hook"},
    )
    store.save_installation(99)
    monkeypatch.setattr(client.auth, "app_jwt", lambda *args: "jwt")
    jobs = await client.queued_jobs()
    assert [(job.id, job.repository, job.labels) for job in jobs] == [
        (7, "peer/repo", ["docker", "self-hosted"])
    ]
    await client.close()
    database.close()


@pytest.mark.asyncio
async def test_app_installation_discovers_multiple_repositories_and_targets_registration(
    tmp_path, monkeypatch
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/app/installations/99/access_tokens":
            return httpx.Response(
                201,
                json={"token": "ghs", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {"full_name": "peer/one"},
                        {"full_name": "peer/two"},
                        {"full_name": "someone/ignored"},
                    ]
                },
            )
        if request.url.path == "/repos/peer/two/actions/runners/registration-token":
            return httpx.Response(201, json={"token": "registration"})
        if request.url.path == "/repos/peer/one/actions/runners":
            return httpx.Response(
                200,
                json={"runners": [{"id": 11, "name": "one", "status": "online"}]},
            )
        if request.url.path == "/repos/peer/two/actions/runners":
            return httpx.Response(
                200,
                json={"runners": [{"id": 22, "name": "two", "status": "online"}]},
            )
        if (
            request.method == "DELETE"
            and request.url.path == "/repos/peer/two/actions/runners/22"
        ):
            return httpx.Response(204)
        raise AssertionError(request.url)

    client, store, database = make_stack(tmp_path, handler)
    store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer", repository="one"),
        {"id": 12, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "hook"},
    )
    store.save_installation(99, repository_selection="selected")
    monkeypatch.setattr(client.auth, "app_jwt", lambda *args: "jwt")
    assert await client.list_repositories() == ["peer/one", "peer/two"]
    assert await client.registration_token("peer/two") == "registration"
    assert await client.list_runners() == [
        {"id": 11, "name": "one", "status": "online", "repository": "peer/one"},
        {"id": 22, "name": "two", "status": "online", "repository": "peer/two"},
    ]
    await client.delete_runner(22, "peer/two")
    with pytest.raises(ValueError, match="does not belong"):
        await client.registration_token("peer/../attacker")
    assert calls.count("/installation/repositories") == 1
    assert store.credentials().connection.repositories_count == 2
    await client.close()
    database.close()


@pytest.mark.asyncio
async def test_rate_limit_stops_repeat_requests_until_retry_window(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/app/installations/99/access_tokens":
            return httpx.Response(
                201,
                json={"token": "ghs", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                429,
                headers={"Retry-After": "120", "X-RateLimit-Remaining": "0"},
                json={"message": "rate limited"},
            )
        raise AssertionError(request.url)

    client, store, database = make_stack(tmp_path, handler)
    store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer"),
        {"id": 12, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "hook"},
    )
    store.save_installation(99)
    monkeypatch.setattr(client.auth, "app_jwt", lambda *args: "jwt")
    with pytest.raises(GitHubRateLimitError):
        await client.list_repositories(refresh=True)
    first_count = len(calls)
    with pytest.raises(GitHubRateLimitError):
        await client.list_repositories(refresh=True)
    assert len(calls) == first_count
    assert client.rate_limit_status()["limited_until"] is not None
    await client.close()
    database.close()


@pytest.mark.asyncio
async def test_server_errors_are_retried_with_backoff(tmp_path, monkeypatch) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/app/installations/99/access_tokens":
            return httpx.Response(
                201,
                json={"token": "ghs", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.url.path == "/installation/repositories":
            attempts += 1
            if attempts < 3:
                return httpx.Response(503, json={"message": "unavailable"})
            return httpx.Response(
                200,
                json={"repositories": [{"full_name": "peer/repo"}]},
            )
        raise AssertionError(request.url)

    async def no_sleep(_seconds: float) -> None:
        return None

    client, store, database = make_stack(tmp_path, handler)
    store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer"),
        {"id": 12, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "hook"},
    )
    store.save_installation(99)
    monkeypatch.setattr(client.auth, "app_jwt", lambda *args: "jwt")
    monkeypatch.setattr("runner_manager.github.asyncio.sleep", no_sleep)
    assert await client.list_repositories(refresh=True) == ["peer/repo"]
    assert attempts == 3
    await client.close()
    database.close()


@pytest.mark.asyncio
async def test_repository_adoption_finds_hosted_jobs_and_exact_replacement(
    tmp_path, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/app/installations/99/access_tokens":
            return httpx.Response(
                201,
                json={"token": "ghs", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {"full_name": "peer/hosted"},
                        {"full_name": "peer/self-hosted"},
                    ]
                },
            )
        if path.endswith("/actions/runs"):
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 42,
                            "name": "CI",
                            "path": ".github/workflows/ci.yml",
                        }
                    ]
                },
            )
        if path.endswith("/actions/runs/42/jobs"):
            label = "ubuntu-latest" if "/hosted/" in path else "self-hosted"
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 7,
                            "name": "checks",
                            "labels": [label, "linux"],
                            "html_url": "https://github.com/peer/repo/actions/runs/42/job/7",
                        }
                    ]
                },
            )
        raise AssertionError(request.url)

    client, store, database = make_stack(tmp_path, handler)
    store.save_manifest_result(
        GitHubSetupRequest(scope="repo", owner="peer"),
        {"id": 12, "slug": "easy", "pem": "PRIVATE", "webhook_secret": "hook"},
    )
    store.save_installation(99)
    monkeypatch.setattr(client.auth, "app_jwt", lambda *args: "jwt")
    pools = {
        "ci": RunnerPoolConfig(labels=["ci"], docker_mode=DockerMode.NONE),
        "docker": RunnerPoolConfig(labels=["docker"], docker_mode=DockerMode.SOCKET),
    }
    adoption = await client.repository_adoption(pools)
    by_repository = {item["repository"]: item for item in adoption["repositories"]}
    assert by_repository["peer/hosted"]["status"] == "needs_migration"
    assert (
        by_repository["peer/hosted"]["examples"][0]["workflow_path"]
        == ".github/workflows/ci.yml"
    )
    assert by_repository["peer/self-hosted"]["status"] == "using_easy_runners"
    assert adoption["repository_count_total"] == 2
    assert adoption["repository_count_scanned"] == 2
    assert adoption["scan"]["scanning"] is False
    assert adoption["scan"]["completed"] == 2
    assert adoption["recommended_pool"] == "ci"
    assert adoption["recommended_runs_on"] == "runs-on: [self-hosted, linux, ci]"
    assert adoption["replacements"]["docker"]["runs_on"].endswith("linux, docker]")
    cached = await client.repository_adoption(
        {"safe": RunnerPoolConfig(labels=["safe"], docker_mode=DockerMode.NONE)}
    )
    assert cached["recommended_pool"] == "safe"
    assert cached["recommended_runs_on"] == "runs-on: [self-hosted, linux, safe]"
    await client.close()
    database.close()


@pytest.mark.asyncio
async def test_repository_adoption_runs_in_background_and_reports_progress(
    tmp_path, monkeypatch
) -> None:
    client, _, database = make_stack(
        tmp_path,
        lambda request: httpx.Response(500, request=request),
    )
    repositories = ["peer/one", "peer/two", "peer/three"]
    release = asyncio.Event()
    started = asyncio.Event()

    async def list_repositories(*, refresh=False):
        return repositories

    async def scan(repository):
        started.set()
        await release.wait()
        return {
            "repository": repository,
            "status": "using_easy_runners",
            "hosted_jobs": 0,
            "self_hosted_jobs": 1,
            "examples": [],
            "error": None,
        }

    monkeypatch.setattr(client, "list_repositories", list_repositories)
    monkeypatch.setattr(client, "_repository_adoption", scan)
    client.settings.poll_concurrency = 2
    pools = {"ci": RunnerPoolConfig(labels=["ci"], docker_mode=DockerMode.NONE)}

    initial = await client.repository_adoption(pools, wait=False)
    assert initial["scan"]["scanning"] is True
    assert initial["scan"]["total"] == 3
    await started.wait()
    progress = await client.repository_adoption(pools, wait=False)
    assert progress["repository_count_scanned"] == 0

    release.set()
    assert client._adoption_task
    await client._adoption_task
    completed = await client.repository_adoption(pools, wait=False)
    assert completed["scan"]["scanning"] is False
    assert completed["repository_count_scanned"] == 3
    assert completed["scan"]["completed"] == 3
    await client.close()
    database.close()


@pytest.mark.asyncio
async def test_repository_adoption_batches_and_prioritizes_unscanned_repositories(
    tmp_path, monkeypatch
) -> None:
    client, _, database = make_stack(
        tmp_path,
        lambda request: httpx.Response(500, request=request),
    )
    repositories = [
        "peer/new",
        "peer/unverified",
        "peer/problem",
        "peer/migrated",
    ]
    client._adoption_results = {
        "peer/unverified": {
            "repository": "peer/unverified",
            "status": "no_recent_jobs",
            "scanned_at": "2026-08-01T00:00:00+00:00",
        },
        "peer/problem": {
            "repository": "peer/problem",
            "status": "needs_migration",
            "scanned_at": "2026-08-01T00:00:00+00:00",
        },
        "peer/migrated": {
            "repository": "peer/migrated",
            "status": "using_easy_runners",
            "scanned_at": "2026-08-01T00:00:00+00:00",
        },
    }
    scanned: list[str] = []

    async def list_repositories(*, refresh=False):
        return repositories

    async def scan(repository):
        scanned.append(repository)
        return {
            "repository": repository,
            "status": "using_easy_runners",
            "hosted_jobs": 0,
            "self_hosted_jobs": 1,
            "examples": [],
            "error": None,
        }

    monkeypatch.setattr(client, "list_repositories", list_repositories)
    monkeypatch.setattr(client, "_repository_adoption", scan)
    client.settings.poll_concurrency = 1
    client.settings.adoption_max_repositories = 2
    pools = {"ci": RunnerPoolConfig(labels=["ci"], docker_mode=DockerMode.NONE)}

    result = await client.repository_adoption(pools, refresh=True)
    assert scanned == ["peer/new", "peer/unverified"]
    assert result["repository_count_total"] == 4
    assert result["repository_count_scanned"] == 4
    assert result["scan"]["completed"] == 2
    assert result["scan"]["total"] == 2
    await client.close()
    database.close()
