from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast


class Database:
    def __init__(self, path: Path, history_limit: int = 500) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        self.path = path
        self.history_limit = history_limit
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        path.chmod(0o600)

    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS api_tokens (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'manage',
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_history (
                    job_id INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS setup_states (
                    state TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(api_tokens)").fetchall()
            }
            if "scope" not in columns:
                self._conn.execute(
                    "ALTER TABLE api_tokens ADD COLUMN scope TEXT NOT NULL DEFAULT 'manage'"
                )
            if "expires_at" not in columns:
                self._conn.execute("ALTER TABLE api_tokens ADD COLUMN expires_at TEXT")
            self._conn.execute("PRAGMA user_version=2")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def delete_setting(self, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def create_api_token(
        self,
        token_id: str,
        name: str,
        digest: str,
        scope: str,
        expires_at: str | None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO api_tokens(id, name, digest, scope, created_at, expires_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (token_id, name, digest, scope, now, expires_at),
            )

    def get_api_token(self, token_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_tokens WHERE id = ?", (token_id,)
            ).fetchone()
            return dict(row) if row else None

    def touch_api_token(self, token_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), token_id),
            )

    def list_api_tokens(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, scope, created_at, last_used_at, expires_at "
                "FROM api_tokens ORDER BY created_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_api_token(self, token_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
            return cursor.rowcount > 0

    def claim_delivery(self, delivery_id: str, retention_hours: int = 24) -> bool:
        now = datetime.now(UTC)
        cutoff = (now - timedelta(hours=retention_hours)).isoformat()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM webhook_deliveries WHERE received_at < ?", (cutoff,))
            try:
                self._conn.execute(
                    "INSERT INTO webhook_deliveries(delivery_id, received_at) VALUES(?, ?)",
                    (delivery_id, now.isoformat()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def add_history(self, job_id: int, payload: dict[str, Any]) -> None:
        completed_at = str(payload.get("completed_at") or datetime.now(UTC).isoformat())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO job_history(job_id, payload, completed_at) VALUES(?, ?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET payload=excluded.payload, "
                "completed_at=excluded.completed_at",
                (job_id, json.dumps(payload, default=str), completed_at),
            )
            self._conn.execute(
                "DELETE FROM job_history WHERE job_id NOT IN "
                "(SELECT job_id FROM job_history ORDER BY completed_at DESC LIMIT ?)",
                (self.history_limit,),
            )

    def list_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM job_history ORDER BY completed_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [json.loads(row["payload"]) for row in rows]

    def usage_summary(self, now: datetime | None = None) -> dict[str, dict[str, Any]]:
        current = now or datetime.now(UTC)
        jobs = self.list_history(self.history_limit)
        windows = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}
        failure_conclusions = {"failure", "timed_out", "cancelled", "startup_failure"}
        result: dict[str, dict[str, Any]] = {}
        for name, window in windows.items():
            cutoff = current - window
            selected = [
                job
                for job in jobs
                if (completed := _parse_datetime(job.get("completed_at"))) and completed >= cutoff
            ]
            runtimes: list[float] = []
            queue_times: list[float] = []
            for job in selected:
                queued = _parse_datetime(job.get("queued_at"))
                started = _parse_datetime(job.get("started_at"))
                completed = _parse_datetime(job.get("completed_at"))
                if started and completed and completed >= started:
                    runtimes.append((completed - started).total_seconds())
                if queued and started and started >= queued:
                    queue_times.append((started - queued).total_seconds())
            failures = sum(
                str(job.get("conclusion") or "").lower() in failure_conclusions
                for job in selected
            )
            result[name] = {
                "jobs": len(selected),
                "runner_minutes": round(sum(runtimes) / 60, 1),
                "average_queue_seconds": (
                    round(sum(queue_times) / len(queue_times)) if queue_times else None
                ),
                "failure_rate": round(failures * 100 / len(selected), 1) if selected else 0.0,
            }
        return result

    def create_setup_state(
        self, state: str, payload: dict[str, Any], ttl_seconds: int = 3600
    ) -> None:
        expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO setup_states(state, payload, expires_at) VALUES(?, ?, ?)",
                (state, json.dumps(payload), expires.isoformat()),
            )

    def consume_setup_state(self, state: str) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT payload, expires_at FROM setup_states WHERE state = ?", (state,)
            ).fetchone()
            self._conn.execute("DELETE FROM setup_states WHERE state = ?", (state,))
            if not row or datetime.fromisoformat(row["expires_at"]) < now:
                return None
            return cast(dict[str, Any], json.loads(row["payload"]))


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
