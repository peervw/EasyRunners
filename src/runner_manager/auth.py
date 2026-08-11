from __future__ import annotations

import hashlib
import hmac
import secrets
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from runner_manager.config import Settings
from runner_manager.database import Database

log = structlog.get_logger()


class AuthManager:
    cookie_name = "easyrunners_session"

    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.password_hasher = PasswordHasher()
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self.bootstrap_password: str | None = None
        secret = self._load_or_create_secret(settings.data_dir / "session.key")
        self._serializer = URLSafeTimedSerializer(secret, salt="easyrunners-session-v1")
        self._initialize_admin()

    @staticmethod
    def _load_or_create_secret(path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        value = secrets.token_urlsafe(48)
        fd = path.open("x", encoding="utf-8")
        try:
            fd.write(value)
        finally:
            fd.close()
        path.chmod(0o600)
        return value

    def _initialize_admin(self) -> None:
        if self.database.get_setting("admin_password_hash"):
            return
        password = secrets.token_urlsafe(18)
        self.database.set_setting("admin_password_hash", self.password_hasher.hash(password))
        self.database.set_setting("admin_must_change", "1")
        self.database.set_setting("auth_epoch", "1")
        self.bootstrap_password = password
        log.warning(
            "auth.bootstrap_password",
            password=password,
            warning="shown once; sign in and change it immediately",
        )

    @property
    def must_change_password(self) -> bool:
        return self.database.get_setting("admin_must_change") == "1"

    def verify_password(self, password: str) -> bool:
        digest = self.database.get_setting("admin_password_hash") or ""
        try:
            valid = self.password_hasher.verify(digest, password)
        except (VerifyMismatchError, ValueError):
            return False
        if valid and self.password_hasher.check_needs_rehash(digest):
            self.database.set_setting("admin_password_hash", self.password_hasher.hash(password))
        return valid

    def change_password(self, current: str | None, new: str, *, force: bool = False) -> None:
        if len(new) < 14:
            raise ValueError("new password must be at least 14 characters")
        if not force and (not current or not self.verify_password(current)):
            raise ValueError("current password is incorrect")
        self.database.set_setting("admin_password_hash", self.password_hasher.hash(new))
        self.database.set_setting("admin_must_change", "0")
        epoch = int(self.database.get_setting("auth_epoch") or "1") + 1
        self.database.set_setting("auth_epoch", str(epoch))

    def reset_password(self) -> str:
        password = secrets.token_urlsafe(18)
        self.database.set_setting("admin_password_hash", self.password_hasher.hash(password))
        self.database.set_setting("admin_must_change", "1")
        epoch = int(self.database.get_setting("auth_epoch") or "1") + 1
        self.database.set_setting("auth_epoch", str(epoch))
        return password

    def create_session(self) -> tuple[str, str]:
        csrf = secrets.token_urlsafe(24)
        payload = {
            "sub": "admin",
            "csrf": csrf,
            "epoch": int(self.database.get_setting("auth_epoch") or "1"),
            "iat": datetime.now(UTC).isoformat(),
        }
        return self._serializer.dumps(payload), csrf

    def verify_session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        try:
            payload = self._serializer.loads(token, max_age=self.settings.session_ttl_seconds)
        except (BadSignature, SignatureExpired):
            return None
        epoch = int(self.database.get_setting("auth_epoch") or "1")
        if payload.get("epoch") != epoch or payload.get("sub") != "admin":
            return None
        return dict(payload)

    @staticmethod
    def verify_csrf(session: dict[str, Any], provided: str | None) -> bool:
        expected = str(session.get("csrf", ""))
        return bool(provided and hmac.compare_digest(expected, provided))

    def login_allowed(self, client: str, now: float) -> bool:
        attempts = self._attempts[client]
        cutoff = now - self.settings.login_window_seconds
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        return len(attempts) < self.settings.login_attempts

    def record_login_failure(self, client: str, now: float) -> None:
        self._attempts[client].append(now)

    def clear_login_failures(self, client: str) -> None:
        self._attempts.pop(client, None)

    @staticmethod
    def _token_digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create_api_token(self, name: str) -> tuple[str, dict[str, Any]]:
        token_id = secrets.token_hex(6)
        token = f"ert_{token_id}_{secrets.token_urlsafe(32)}"
        self.database.create_api_token(token_id, name, self._token_digest(token))
        record = next(item for item in self.database.list_api_tokens() if item["id"] == token_id)
        return token, record

    def verify_api_token(self, token: str) -> bool:
        parts = token.split("_", 2)
        if len(parts) != 3 or parts[0] != "ert":
            return False
        record = self.database.get_api_token(parts[1])
        if not record:
            return False
        valid = hmac.compare_digest(str(record["digest"]), self._token_digest(token))
        if valid:
            self.database.touch_api_token(parts[1])
        return valid
