from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from runner_manager.config import Settings
from runner_manager.metrics import NOTIFICATION_FAILURES, NOTIFICATIONS_SENT

log = structlog.get_logger()


class NotificationManager:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.http = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self._owns_client = client is None
        self._last_sent: dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self.settings.notification_webhook_url)

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "signed": bool(self.settings.notification_webhook_secret),
            "stuck_job_seconds": self.settings.notification_stuck_job_seconds,
            "cooldown_seconds": self.settings.notification_cooldown_seconds,
        }

    async def close(self) -> None:
        if self._owns_client:
            await self.http.aclose()

    async def send(
        self,
        event: str,
        title: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        key: str | None = None,
        force: bool = False,
    ) -> bool:
        if not self.settings.notification_webhook_url:
            return False
        dedupe_key = key or event
        now = time.monotonic()
        last_sent = self._last_sent.get(dedupe_key)
        if (
            not force
            and last_sent is not None
            and now - last_sent < self.settings.notification_cooldown_seconds
        ):
            return False
        payload = {
            "event": event,
            "severity": "warning",
            "title": title,
            "message": message,
            "timestamp": datetime.now(UTC).isoformat(),
            "instance_id": self.settings.instance_id,
            "dashboard_url": self.settings.public_url,
            "details": details or {},
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "EasyRunners/0.3",
            "X-EasyRunners-Event": event,
        }
        if self.settings.notification_webhook_secret:
            secret = self.settings.notification_webhook_secret.get_secret_value().encode()
            signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
            headers["X-EasyRunners-Signature"] = f"sha256={signature}"
        try:
            response = await self.http.post(
                self.settings.notification_webhook_url.get_secret_value(),
                content=body,
                headers=headers,
            )
            response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            NOTIFICATION_FAILURES.labels(event=event).inc()
            log.warning(
                "notification.delivery_failed", notification_event=event, error=str(exc)
            )
            return False
        self._last_sent[dedupe_key] = now
        NOTIFICATIONS_SENT.labels(event=event).inc()
        log.info("notification.delivered", notification_event=event)
        return True
