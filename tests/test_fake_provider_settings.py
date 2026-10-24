import json
import random
from threading import Barrier, BrokenBarrierError
from uuid import uuid4

import pytest

from app import fake_provider


async def test_failure_rate_controls_provider_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_DELAY_SECONDS", "0")
    monkeypatch.setenv("FAKE_PROVIDER_FAILURE_TYPE", "technical")
    monkeypatch.setenv("FAKE_PROVIDER_FAILURE_PROMPT", "forced failure prompt")
    monkeypatch.setenv("FAKE_PROVIDER_SILENT_PROMPT", "silent prompt")
    monkeypatch.setenv("FAKE_PROVIDER_DUPLICATE_WEBHOOK", "false")
    monkeypatch.setenv("PROVIDER_NAME", "alpha")
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("APP_CALLBACK_URL", "http://app")

    draws = iter([0.0, 0.99, 0.49, 0.5])
    monkeypatch.setattr(random, "random", lambda: next(draws))
    callbacks: list[dict[str, str]] = []

    def capture_callback(_: str, body: bytes, __: str) -> None:
        callbacks.append(json.loads(body))

    monkeypatch.setattr(fake_provider, "post_webhook", capture_callback)

    for failure_rate in (0.0, 1.0, 0.5, 0.5):
        monkeypatch.setenv("FAKE_PROVIDER_FAILURE_RATE", str(failure_rate))
        rate, duplicate_webhook, failure_type = fake_provider.provider_settings()
        await fake_provider.send_completion(
            str(uuid4()),
            "ordinary prompt",
            rate,
            duplicate_webhook,
            failure_type,
        )

    assert [callback["status"] for callback in callbacks] == [
        "completed",
        "failed",
        "failed",
        "completed",
    ]
    for failure_rate in ("-0.1", "1.1"):
        monkeypatch.setenv("FAKE_PROVIDER_FAILURE_RATE", failure_rate)
        with pytest.raises(ValueError):
            fake_provider.provider_settings()


async def test_duplicate_setting_sends_identical_webhooks_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_DELAY_SECONDS", "0")
    monkeypatch.setenv("FAKE_PROVIDER_FAILURE_RATE", "0")
    monkeypatch.setenv("FAKE_PROVIDER_DUPLICATE_WEBHOOK", "true")
    monkeypatch.setenv("FAKE_PROVIDER_FAILURE_TYPE", "technical")
    monkeypatch.setenv("FAKE_PROVIDER_FAILURE_PROMPT", "forced failure prompt")
    monkeypatch.setenv("FAKE_PROVIDER_SILENT_PROMPT", "silent prompt")
    monkeypatch.setenv("PROVIDER_NAME", "alpha")
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("APP_CALLBACK_URL", "http://app")

    barrier = Barrier(2, timeout=2)
    callbacks: list[tuple[str, bytes, str]] = []
    concurrent_delivery_errors: list[bool] = []

    def capture_callback(url: str, body: bytes, signature: str) -> None:
        try:
            barrier.wait()
        except BrokenBarrierError:
            concurrent_delivery_errors.append(True)
        callbacks.append((url, body, signature))

    monkeypatch.setattr(fake_provider, "post_webhook", capture_callback)
    rate, duplicate_webhook, failure_type = fake_provider.provider_settings()
    await fake_provider.send_completion(
        str(uuid4()),
        "ordinary prompt",
        rate,
        duplicate_webhook,
        failure_type,
    )

    assert len(callbacks) == 2
    assert not concurrent_delivery_errors
    assert callbacks[0] == callbacks[1]
    assert json.loads(callbacks[0][1])["status"] == "completed"
