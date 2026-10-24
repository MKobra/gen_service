import asyncio
import hashlib
import hmac
import json
import os

import asyncpg
import httpx


def signed_body(body: dict[str, object]) -> tuple[bytes, dict[str, str]]:
    raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
    secret = os.getenv("WEBHOOK_SECRET", "test-secret").encode("utf-8")
    signature = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return raw_body, {
        "Content-Type": "application/json",
        "X-Signature": signature,
    }


async def test_timeout_falls_back_and_ignores_late_completion(
    service_client: httpx.AsyncClient,
    database_connection: asyncpg.Connection,
    user_id: str,
    payment_id: str,
) -> None:
    user_id = f"Provider-timeout-user-{user_id}"
    payment_id = f"Provider-timeout-payment-{payment_id}"
    payment_raw_body, payment_headers = signed_body(
        {
            "payment_id": payment_id,
            "user_id": user_id,
            "tokens": 2,
            "status": "succeeded",
        }
    )
    payment_response = await service_client.post(
        "/webhooks/payments",
        content=payment_raw_body,
        headers=payment_headers,
    )
    payment_response.raise_for_status()

    generate_response = await service_client.post(
        "/generate",
        json={
            "user_id": user_id,
            "model_slug": "image-fast",
            "prompt": "Provider timeout late callback test",
            "ref_images": [],
        },
    )
    assert generate_response.status_code == 202
    task_id = generate_response.json()["task_id"]

    for _ in range(280):
        task_response = await service_client.get(f"/tasks/{task_id}")
        assert task_response.is_success, task_response.text
        task = task_response.json()
        if task["status"] == "completed" and len(task["attempts"]) == 2:
            break
        await asyncio.sleep(0.025)
    else:
        raise AssertionError("Task did not complete after provider timeout")

    assert task["current_leg"] == "beta"
    assert [attempt["provider"] for attempt in task["attempts"]] == [
        "alpha",
        "beta",
    ]
    assert task["attempts"][0]["status"] in {"timed_out", "late_ignored"}
    assert task["attempts"][1]["status"] == "completed"
    assert task["result_url"]

    alpha_external_id = await database_connection.fetchval(
        """
        SELECT external_id
        FROM task_attempts
        WHERE task_id = $1 AND attempt_number = 1
        """,
        task_id,
    )
    assert alpha_external_id is not None
    late_raw_body, late_headers = signed_body(
        {
            "external_id": alpha_external_id,
            "status": "completed",
            "result_url": "https://files.example/late-result",
        }
    )
    late_responses = await asyncio.gather(
        *(
            service_client.post(
                "/webhooks/providers/alpha",
                content=late_raw_body,
                headers=late_headers,
            )
            for _ in range(10)
        )
    )
    assert all(response.status_code == 200 for response in late_responses)

    attempt_status = await database_connection.fetchval(
        """
        SELECT status
        FROM task_attempts
        WHERE task_id = $1 AND attempt_number = 1
        """,
        task_id,
    )
    assert attempt_status == "late_ignored"
    final_task = await service_client.get(f"/tasks/{task_id}")
    assert final_task.json()["status"] == "completed"
    assert final_task.json()["current_leg"] == "beta"

    balance_response = await service_client.get(f"/users/{user_id}/balance")
    balance_response.raise_for_status()
    assert balance_response.json()["balance"] == 0
    assert all(
        entry["operation"] != "refund"
        for entry in balance_response.json()["journal"]
    )
