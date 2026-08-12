import hashlib
import hmac
import json

import httpx
import pytest
from pydantic import SecretStr

from runner_manager.notifications import NotificationManager


@pytest.mark.asyncio
async def test_notification_is_signed_and_throttled(settings) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    configured = settings.model_copy(
        update={
            "notification_webhook_url": SecretStr(
                "https://hooks.example.test/easy-runners"
            ),
            "notification_webhook_secret": SecretStr("signing-secret"),
            "notification_cooldown_seconds": 600,
        }
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager = NotificationManager(configured, http)

    assert await manager.send("job_stuck", "Stuck", "A job is waiting", key="job:1")
    assert not await manager.send("job_stuck", "Stuck", "A job is waiting", key="job:1")
    assert len(requests) == 1
    body = requests[0].content
    payload = json.loads(body)
    assert payload["event"] == "job_stuck"
    assert payload["instance_id"] == "test"
    expected = hmac.new(b"signing-secret", body, hashlib.sha256).hexdigest()
    assert requests[0].headers["X-EasyRunners-Signature"] == f"sha256={expected}"
    await http.aclose()


@pytest.mark.asyncio
async def test_notification_delivery_failure_is_nonfatal(settings) -> None:
    configured = settings.model_copy(
        update={
            "notification_webhook_url": SecretStr("https://hooks.example.test/fail")
        }
    )
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    )
    manager = NotificationManager(configured, http)
    assert not await manager.send("runner_startup_failure", "Failed", "Runner failed")
    await http.aclose()
