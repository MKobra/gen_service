import asyncio
import hashlib
import hmac
import json
import os

import httpx


async def test_generate_completes_through_provider_webhook(
    service_client: httpx.AsyncClient,
    user_id: str,
    payment_id: str,
) -> None:
    user_id = f"Provider-completion-user-{user_id}"
    payment_id = f"Provider-completion-payment-{payment_id}"
    payment_body = {
        "payment_id": payment_id,
        "user_id": user_id,
        "tokens": 2,
        "status": "succeeded",
    }
    payment_raw_body = json.dumps(
        payment_body,
        separators=(",", ":"),
    ).encode("utf-8")
    secret = os.getenv("WEBHOOK_SECRET", "test-secret").encode("utf-8")
    payment_signature = hmac.new(
        secret,
        payment_raw_body,
        hashlib.sha256,
    ).hexdigest()

    payment_response = await service_client.post(
        "/webhooks/payments",
        content=payment_raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": payment_signature,
        },
    )
    payment_response.raise_for_status()

    generate_response = await service_client.post(
        "/generate",
        json={
            "user_id": user_id,
            "model_slug": "image-fast",
            "prompt": "Provider completion test",
            "ref_images": [],
        },
    )
    assert generate_response.status_code == 202
    task_id = generate_response.json()["task_id"]

    for _ in range(40):
        task_response = await service_client.get(f"/tasks/{task_id}")
        assert task_response.is_success, task_response.text
        task = task_response.json()
        if task["status"] == "completed":
            break
        await asyncio.sleep(0.25)
    else:
        raise AssertionError("Task did not complete through the provider webhook")

    assert task["result_url"]
    assert task["current_leg"] == "alpha"
    assert len(task["attempts"]) == 1
    assert task["attempts"][0]["provider"] == "alpha"
    assert task["attempts"][0]["status"] == "completed"
    assert task["attempts"][0]["error_type"] is None
