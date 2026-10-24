import asyncio
import hashlib
import hmac
import json
import os
from uuid import uuid4

import asyncpg
import httpx


def signed_webhook(body: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
    secret = os.getenv("WEBHOOK_SECRET", "test-secret").encode("utf-8")
    signature = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return raw_body, {
        "Content-Type": "application/json",
        "X-Signature": signature,
    }


async def create_funded_task(
    service_client: httpx.AsyncClient,
    user_id: str,
    tokens: int,
    model_slug: str,
    prompt: str,
) -> str:
    payment_id = str(uuid4())
    payment_body = {
        "payment_id": payment_id,
        "user_id": user_id,
        "tokens": tokens,
        "status": "succeeded",
    }
    payment_raw_body, payment_headers = signed_webhook(payment_body)
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
            "model_slug": model_slug,
            "prompt": prompt,
            "ref_images": [],
        },
    )
    assert generate_response.status_code == 202, generate_response.text
    return generate_response.json()["task_id"]


async def wait_for_attempt(
    connection: asyncpg.Connection,
    task_id: str,
    attempt_number: int,
) -> str:
    for _ in range(200):
        external_id = await connection.fetchval(
            """
            SELECT external_id
            FROM task_attempts
            WHERE task_id = $1 AND attempt_number = $2 AND status = 'waiting'
            """,
            task_id,
            attempt_number,
        )
        if external_id is not None:
            return external_id
        await asyncio.sleep(0.025)
    raise AssertionError(f"Attempt {attempt_number} was not accepted")


async def post_provider_failure(
    service_client: httpx.AsyncClient,
    provider: str,
    external_id: str,
    error_type: str,
) -> httpx.Response:
    raw_body, headers = signed_webhook(
        {
            "external_id": external_id,
            "status": "failed",
            "error_type": error_type,
        }
    )
    return await service_client.post(
        f"/webhooks/providers/{provider}",
        content=raw_body,
        headers=headers,
    )


async def read_task(
    service_client: httpx.AsyncClient,
    task_id: str,
) -> dict[str, object]:
    response = await service_client.get(f"/tasks/{task_id}")
    assert response.is_success, response.text
    return response.json()


async def test_concurrent_failures_on_last_leg_refund_once(
    service_client: httpx.AsyncClient,
    database_connection: asyncpg.Connection,
    user_id: str,
) -> None:
    task_user_id = f"Last-leg-refund-user-{user_id}"
    task_id = await create_funded_task(
        service_client,
        task_user_id,
        2,
        "image-fast",
        "Concurrent last-leg refund test",
    )
    alpha_external_id = await wait_for_attempt(database_connection, task_id, 1)
    alpha_response = await post_provider_failure(
        service_client,
        "alpha",
        alpha_external_id,
        "technical",
    )
    alpha_response.raise_for_status()

    beta_external_id = await wait_for_attempt(database_connection, task_id, 2)
    responses = await asyncio.gather(
        *(
            post_provider_failure(
                service_client,
                "beta",
                beta_external_id,
                "technical",
            )
            for _ in range(10)
        )
    )
    assert all(response.is_success for response in responses)

    task = await read_task(service_client, task_id)
    assert task["status"] == "failed"
    assert [attempt["provider"] for attempt in task["attempts"]] == [
        "alpha",
        "beta",
    ]
    refunds = await database_connection.fetchval(
        """
        SELECT COUNT(*)
        FROM ledger_entries
        WHERE task_id = $1 AND operation = 'refund'
        """,
        task_id,
    )
    assert refunds == 1
    balance = await database_connection.fetchval(
        "SELECT balance FROM users WHERE user_id = $1",
        task_user_id,
    )
    assert balance == 2


async def test_moderation_skip_and_payload_failure_refund(
    service_client: httpx.AsyncClient,
    database_connection: asyncpg.Connection,
    user_id: str,
) -> None:
    moderation_user_id = f"Moderation-refund-user-{user_id}"
    moderation_task_id = await create_funded_task(
        service_client,
        moderation_user_id,
        25,
        "video-long",
        "Moderation skip test",
    )
    beta_external_id = await wait_for_attempt(
        database_connection,
        moderation_task_id,
        1,
    )
    moderation_response = await post_provider_failure(
        service_client,
        "beta",
        beta_external_id,
        "moderation",
    )
    moderation_response.raise_for_status()

    payload_user_id = f"Payload-refund-user-{user_id}"
    payload_task_id = await create_funded_task(
        service_client,
        payload_user_id,
        2,
        "image-fast",
        "Payload failure test",
    )
    alpha_external_id = await wait_for_attempt(
        database_connection,
        payload_task_id,
        1,
    )
    payload_response = await post_provider_failure(
        service_client,
        "alpha",
        alpha_external_id,
        "payload",
    )
    payload_response.raise_for_status()

    moderation_task = await read_task(service_client, moderation_task_id)
    payload_task = await read_task(service_client, payload_task_id)
    assert moderation_task["status"] == "failed"
    assert [attempt["provider"] for attempt in moderation_task["attempts"]] == [
        "beta",
    ]
    assert payload_task["status"] == "failed"
    assert [attempt["provider"] for attempt in payload_task["attempts"]] == [
        "alpha",
    ]

    for task_id in (moderation_task_id, payload_task_id):
        refund_count = await database_connection.fetchval(
            """
            SELECT COUNT(*)
            FROM ledger_entries
            WHERE task_id = $1 AND operation = 'refund'
            """,
            task_id,
        )
        assert refund_count == 1


async def test_synchronous_503_advances_to_next_leg(
    service_client: httpx.AsyncClient,
    user_id: str,
) -> None:
    task_user_id = f"Synchronous-503-user-{user_id}"
    task_id = await create_funded_task(
        service_client,
        task_user_id,
        2,
        "image-fast",
        "Provider synchronous failure test",
    )

    for _ in range(200):
        task = await read_task(service_client, task_id)
        if task["status"] in {"completed", "failed"}:
            break
        await asyncio.sleep(0.025)
    else:
        raise AssertionError("Task did not finish after synchronous provider error")

    assert task["status"] == "completed"
    assert task["current_leg"] == "beta"
    assert [attempt["provider"] for attempt in task["attempts"]] == [
        "alpha",
        "beta",
    ]
    assert [attempt["status"] for attempt in task["attempts"]] == [
        "failed",
        "completed",
    ]
    assert task["attempts"][0]["error_type"] == "technical"
