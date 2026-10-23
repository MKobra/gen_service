import asyncio
import hashlib
import hmac
import json
import os
from uuid import UUID, uuid4

import asyncpg
import httpx
from helpers import assert_balance_matches_ledger, read_journal


async def test_unsupported_reference_count_does_not_change_balance_or_journal(
    service_client: httpx.AsyncClient,
    database_connection: asyncpg.Connection,
    user_id: str,
    payment_id: str,
) -> None:
    user_id = f"Generate-user-{user_id}"
    payment_id = f"Generate-payment-{payment_id}"
    tokens = 10

    payment_body = {
        "payment_id": payment_id,
        "user_id": user_id,
        "tokens": tokens,
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
    assert 200 <= payment_response.status_code < 300

    balance_before_response = await service_client.get(
        f"/users/{user_id}/balance",
    )
    assert balance_before_response.is_success
    balance_before = balance_before_response.json()["balance"]
    journal_before = await read_journal(database_connection, user_id)

    generate_response = await service_client.post(
        "/generate",
        json={
            "user_id": user_id,
            "model_slug": "image-fast",
            "prompt": "Test prompt",
            "ref_images": [
                f"https://example.test/{index}.png"
                for index in range(11)
            ],
        },
    )

    assert generate_response.status_code == 400

    balance_after_response = await service_client.get(
        f"/users/{user_id}/balance",
    )
    assert balance_after_response.is_success
    assert balance_after_response.json()["balance"] == balance_before

    journal_after = await read_journal(database_connection, user_id)
    assert [
        (
            row["entry_id"],
            row["delta"],
            row["operation"],
            row["payment_id"],
            row["created_at"],
        )
        for row in journal_after
    ] == [
        (
            row["entry_id"],
            row["delta"],
            row["operation"],
            row["payment_id"],
            row["created_at"],
        )
        for row in journal_before
    ]

    await assert_balance_matches_ledger(database_connection, user_id)


async def test_concurrent_generate_debits_once_at_exact_balance(
    service_client: httpx.AsyncClient,
    database_connection: asyncpg.Connection,
    user_id: str,
    payment_id: str,
) -> None:
    user_id = f"Generate-race-user-{user_id}"
    payment_id = f"Generate-race-payment-{payment_id}"
    tokens = 2
    secret = os.getenv("WEBHOOK_SECRET", "test-secret").encode("utf-8")

    payment_body = {
        "payment_id": payment_id,
        "user_id": user_id,
        "tokens": tokens,
        "status": "succeeded",
    }
    payment_raw_body = json.dumps(
        payment_body,
        separators=(",", ":"),
    ).encode("utf-8")
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
    assert 200 <= payment_response.status_code < 300, (
        "Could not fund the race-test user"
    )

    generate_payload = {
        "user_id": user_id,
        "model_slug": "image-fast",
        "prompt": "Concurrent test",
        "ref_images": [],
    }
    responses = await asyncio.gather(
        service_client.post("/generate", json=generate_payload),
        service_client.post("/generate", json=generate_payload),
    )

    assert sorted(response.status_code for response in responses) == [202, 402], (
        "Exactly one concurrent request must be accepted and one must get 402"
    )

    balance_response = await service_client.get(f"/users/{user_id}/balance")
    assert balance_response.is_success
    balance = balance_response.json()["balance"]
    assert balance >= 0, "Concurrent generation must not make balance negative"
    assert balance == 0, "One accepted generation must spend the full balance"

    task_count = await database_connection.fetchval(
        "SELECT COUNT(*) FROM tasks WHERE user_id = $1",
        user_id,
    )
    assert task_count == 1, "Exactly one concurrent request must create a task"

    debit_rows = await database_connection.fetch(
        """
        SELECT delta
        FROM ledger_entries
        WHERE user_id = $1 AND operation = 'debit'
        """,
        user_id,
    )
    assert len(debit_rows) == 1, "Exactly one debit must be journaled"
    assert debit_rows[0]["delta"] == -tokens, (
        "The debit must equal one model price"
    )

    await assert_balance_matches_ledger(database_connection, user_id)


async def test_generate_returns_402_without_charging_or_creating_user(
    service_client: httpx.AsyncClient,
    database_connection: asyncpg.Connection,
    user_id: str,
    payment_id: str,
) -> None:
    user_id = f"Generate-low-balance-{user_id}"
    payment_id = f"Generate-low-balance-payment-{payment_id}"
    tokens = 1
    secret = os.getenv("WEBHOOK_SECRET", "test-secret").encode("utf-8")

    payment_body = {
        "payment_id": payment_id,
        "user_id": user_id,
        "tokens": tokens,
        "status": "succeeded",
    }
    payment_raw_body = json.dumps(
        payment_body,
        separators=(",", ":"),
    ).encode("utf-8")
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
    assert 200 <= payment_response.status_code < 300, (
        "Could not seed the low-balance user"
    )

    balance_before_response = await service_client.get(
        f"/users/{user_id}/balance",
    )
    assert balance_before_response.is_success
    balance_before = balance_before_response.json()["balance"]
    journal_before = await read_journal(database_connection, user_id)
    task_count_before = await database_connection.fetchval(
        "SELECT COUNT(*) FROM tasks WHERE user_id = $1",
        user_id,
    )

    generate_payload = {
        "user_id": user_id,
        "model_slug": "image-fast",
        "prompt": "Test prompt",
        "ref_images": [],
    }
    low_balance_response = await service_client.post(
        "/generate",
        json=generate_payload,
    )
    assert low_balance_response.status_code == 402, (
        "A known user below the model price must receive 402"
    )

    balance_after_response = await service_client.get(
        f"/users/{user_id}/balance",
    )
    assert balance_after_response.is_success
    assert balance_after_response.json()["balance"] == balance_before, (
        "A 402 response must not change the user's balance"
    )

    journal_after = await read_journal(database_connection, user_id)
    assert [
        (row["entry_id"], row["delta"], row["operation"], row["payment_id"])
        for row in journal_after
    ] == [
        (row["entry_id"], row["delta"], row["operation"], row["payment_id"])
        for row in journal_before
    ], "A 402 response must not change the user's journal"

    task_count_after = await database_connection.fetchval(
        "SELECT COUNT(*) FROM tasks WHERE user_id = $1",
        user_id,
    )
    assert task_count_after == task_count_before, (
        "A 402 response must not create a task"
    )

    unknown_user_id = f"Generate-unknown-{uuid4()}"
    unknown_user_response = await service_client.post(
        "/generate",
        json={**generate_payload, "user_id": unknown_user_id},
    )
    assert unknown_user_response.status_code == 402, (
        "An unknown user must receive 402"
    )

    unknown_user_exists = await database_connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM users WHERE user_id = $1)",
        unknown_user_id,
    )
    assert unknown_user_exists is False, (
        "A 402 response for an unknown user must not create that user"
    )

    unknown_user_task_count = await database_connection.fetchval(
        "SELECT COUNT(*) FROM tasks WHERE user_id = $1",
        unknown_user_id,
    )
    assert unknown_user_task_count == 0, (
        "A 402 response for an unknown user must not create a task"
    )

    top_up_payment_id = f"Generate-top-up-{uuid4()}"
    top_up_body = {
        "payment_id": top_up_payment_id,
        "user_id": user_id,
        "tokens": 1,
        "status": "succeeded",
    }
    top_up_raw_body = json.dumps(
        top_up_body,
        separators=(",", ":"),
    ).encode("utf-8")
    top_up_signature = hmac.new(
        secret,
        top_up_raw_body,
        hashlib.sha256,
    ).hexdigest()
    top_up_response = await service_client.post(
        "/webhooks/payments",
        content=top_up_raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": top_up_signature,
        },
    )
    assert 200 <= top_up_response.status_code < 300, (
        "Could not bring the balance to exactly one model price"
    )

    accepted_response = await service_client.post(
        "/generate",
        json=generate_payload,
    )
    assert accepted_response.status_code == 202, (
        "A request covered by the balance must be accepted"
    )

    task_id = UUID(accepted_response.json()["task_id"])
    task = await database_connection.fetchrow(
        """
        SELECT user_id, model_slug, prompt, ref_images, price, status
        FROM tasks
        WHERE task_id = $1
        """,
        task_id,
    )
    assert task is not None, "Accepted request must create a task"
    assert task["user_id"] == user_id, "Task must belong to the requesting user"
    assert task["model_slug"] == "image-fast", "Task must retain its model"
    assert task["prompt"] == "Test prompt", "Task must retain its prompt"
    assert json.loads(task["ref_images"]) == [], "Task must retain its reference list"
    assert task["price"] == 2, "Task must snapshot the model price"
    assert task["status"] == "processing", "New task must be nonterminal"

    debit_rows = await database_connection.fetch(
        """
        SELECT delta
        FROM ledger_entries
        WHERE task_id = $1 AND operation = 'debit'
        """,
        task_id,
    )
    assert len(debit_rows) == 1, "Accepted task must have exactly one debit"
    assert debit_rows[0]["delta"] == -2, "Debit must equal the model price"

    accepted_balance = await service_client.get(f"/users/{user_id}/balance")
    assert accepted_balance.is_success
    assert accepted_balance.json()["balance"] == 0, (
        "Successful acceptance must leave a zero balance"
    )

    journal_before_second_post = await read_journal(
        database_connection,
        user_id,
    )
    task_count_before_second_post = await database_connection.fetchval(
        "SELECT COUNT(*) FROM tasks WHERE user_id = $1",
        user_id,
    )

    second_response = await service_client.post(
        "/generate",
        json=generate_payload,
    )
    assert second_response.status_code == 402, (
        "A second request without another price of balance must be rejected"
    )

    journal_after_second_post = await read_journal(
        database_connection,
        user_id,
    )
    assert [
        (row["entry_id"], row["delta"], row["operation"], row["payment_id"])
        for row in journal_after_second_post
    ] == [
        (row["entry_id"], row["delta"], row["operation"], row["payment_id"])
        for row in journal_before_second_post
    ], "Rejected second request must not change the journal"

    task_count_after_second_post = await database_connection.fetchval(
        "SELECT COUNT(*) FROM tasks WHERE user_id = $1",
        user_id,
    )
    assert task_count_after_second_post == task_count_before_second_post, (
        "Rejected second request must not create another task"
    )

    await assert_balance_matches_ledger(database_connection, user_id)
