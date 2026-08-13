import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from runner_manager.auth import AuthManager
from runner_manager.config import Settings
from runner_manager.database import Database
from runner_manager.github import GitHubConnection, GitHubConnectionStore
from runner_manager.models import GitHubScope, TokenScope


@pytest.fixture
def auth_stack(tmp_path: Path) -> tuple[AuthManager, Database]:
    settings = Settings(
        public_url="http://localhost",
        allow_insecure_public_url=True,
        data_dir=tmp_path,
        config_path=tmp_path / "missing",
    )
    database = Database(tmp_path / "state.sqlite3", history_limit=2)
    return AuthManager(settings, database), database


def test_bootstrap_password_session_and_forced_change(auth_stack) -> None:
    auth, database = auth_stack
    assert auth.bootstrap_password
    assert auth.verify_password(auth.bootstrap_password)
    assert auth.must_change_password
    token, csrf = auth.create_session()
    session = auth.verify_session(token)
    assert session and auth.verify_csrf(session, csrf)

    auth.change_password(auth.bootstrap_password, "a-new-secure-password")
    assert not auth.must_change_password
    assert auth.verify_password("a-new-secure-password")
    assert auth.verify_session(token) is None
    database.close()


def test_password_policy_and_recovery(auth_stack) -> None:
    auth, database = auth_stack
    with pytest.raises(ValueError, match="14"):
        auth.change_password(auth.bootstrap_password, "too-short")
    reset = auth.reset_password()
    assert auth.verify_password(reset)
    assert auth.must_change_password
    database.close()


def test_api_token_is_one_way_and_revocable(auth_stack) -> None:
    auth, database = auth_stack
    token, record = auth.create_api_token("prometheus", TokenScope.METRICS, 30)
    assert token.startswith(f"ert_{record['id']}_")
    assert token not in str(database.get_api_token(record["id"]))
    assert record["scope"] == "metrics"
    assert record["expires_at"]
    assert auth.verify_api_token(token)
    assert not auth.verify_api_token(token + "x")
    assert database.delete_api_token(record["id"])
    assert not auth.verify_api_token(token)
    database.close()


def test_expired_api_token_is_rejected(auth_stack) -> None:
    auth, database = auth_stack
    token, record = auth.create_api_token("short-lived", TokenScope.READ, 1)
    settings = auth.settings
    path = database.path
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE api_tokens SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", record["id"]),
        )
    reopened = Database(path)
    assert AuthManager(settings, reopened).authenticate_api_token(token) is None
    reopened.close()


def test_legacy_api_tokens_migrate_with_manage_scope(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE api_tokens ("
            "id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT NOT NULL, "
            "created_at TEXT NOT NULL, last_used_at TEXT)"
        )
        connection.execute(
            "INSERT INTO api_tokens VALUES (?, ?, ?, ?, ?)",
            ("old", "legacy", "digest", "2026-01-01T00:00:00+00:00", None),
        )

    database = Database(path)
    record = database.get_api_token("old")
    assert record and record["scope"] == "manage"
    assert record["expires_at"] is None
    database.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_login_rate_window(auth_stack) -> None:
    auth, database = auth_stack
    for value in range(auth.settings.login_attempts):
        assert auth.login_allowed("client", float(value))
        auth.record_login_failure("client", float(value))
    assert not auth.login_allowed("client", float(auth.settings.login_attempts))
    assert auth.login_allowed("client", float(auth.settings.login_window_seconds + 10))
    database.close()


def test_delivery_dedup_history_and_setup_state(auth_stack) -> None:
    _, database = auth_stack
    assert database.claim_delivery("delivery")
    assert not database.claim_delivery("delivery")
    database.add_history(1, {"id": 1, "completed_at": "2026-01-01T00:00:00+00:00"})
    database.add_history(2, {"id": 2, "completed_at": "2026-01-02T00:00:00+00:00"})
    database.add_history(3, {"id": 3, "completed_at": "2026-01-03T00:00:00+00:00"})
    assert [item["id"] for item in database.list_history()] == [3, 2]
    database.create_setup_state("state", {"owner": "peer"})
    assert database.consume_setup_state("state") == {"owner": "peer"}
    assert database.consume_setup_state("state") is None
    database.close()


def test_legacy_github_connection_migrates_without_reconnect(tmp_path: Path) -> None:
    settings = Settings(
        public_url="https://runners.example.com",
        data_dir=tmp_path,
        config_path=tmp_path / "missing",
        github_auth_mode="onboarding",
    )
    database = Database(tmp_path / "state.sqlite3")
    legacy = GitHubConnection(
        auth_type="app",
        scope=GitHubScope.REPO,
        owner="peer",
        app_id=1,
        installation_id=2,
        app_slug="easy-peer",
        source="onboarding",
    )
    database.set_setting("github_connection", legacy.model_dump_json())
    database.set_setting("webhook_last_received_at", "2026-08-13T12:00:00+00:00")
    github_dir = tmp_path / "github"
    github_dir.mkdir()
    (github_dir / "app.pem").write_text("PRIVATE")
    (github_dir / "webhook.secret").write_text("secret")

    store = GitHubConnectionStore(settings, database)
    connections = store.connections()
    assert len(connections) == 1
    connection = connections[0]
    assert connection.id
    assert store.credentials(connection_id=connection.id).private_key == "PRIVATE"
    assert database.get_setting("github_connection") is None
    assert database.get_setting(f"webhook_last_received_at:{connection.id}")
    assert (github_dir / connection.id / "app.pem").stat().st_mode & 0o777 == 0o600
    database.close()


def test_webhook_delivery_ids_are_scoped_to_the_connection(auth_stack) -> None:
    _, database = auth_stack
    assert database.claim_delivery("same", connection_id="one")
    assert database.claim_delivery("same", connection_id="two")
    assert not database.claim_delivery("same", connection_id="one")
    database.close()


def test_usage_summary_calculates_recent_runtime_queue_and_failures(tmp_path: Path) -> None:
    database = Database(tmp_path / "usage.sqlite3", history_limit=10)
    now = datetime(2026, 8, 12, 12, tzinfo=UTC)
    database.add_history(
        1,
        {
            "id": 1,
            "queued_at": (now - timedelta(minutes=31)).isoformat(),
            "started_at": (now - timedelta(minutes=30)).isoformat(),
            "completed_at": (now - timedelta(minutes=20)).isoformat(),
            "conclusion": "success",
        },
    )
    database.add_history(
        2,
        {
            "id": 2,
            "queued_at": (now - timedelta(days=2, minutes=5)).isoformat(),
            "started_at": (now - timedelta(days=2, minutes=4)).isoformat(),
            "completed_at": (now - timedelta(days=2)).isoformat(),
            "conclusion": "failure",
        },
    )

    usage = database.usage_summary(now)

    assert usage["24h"] == {
        "jobs": 1,
        "runner_minutes": 10.0,
        "average_queue_seconds": 60,
        "failure_rate": 0.0,
    }
    assert usage["7d"] == {
        "jobs": 2,
        "runner_minutes": 14.0,
        "average_queue_seconds": 60,
        "failure_rate": 50.0,
    }
    database.close()
