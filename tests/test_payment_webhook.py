import hashlib
import hmac
import json
import os
from uuid import uuid4

import asyncpg
import httpx
from helpers import assert_balance_matches_ledger, read_journal


async def test_successful_payment_creates_user_and_journals_credit(
    service_client: httpx.AsyncClient,
    database_connection: asyncpg.Connection,
    user_id: str,
    payment_id: str,
) -> None:
    user_id = f"User-{user_id}"
    payment_id = f"Payment-{payment_id}"
    tokens = 17
    body = {
        "payment_id": payment_id,
        "user_id": user_id,
        "tokens": tokens,
        "status": "succeeded",
    }
    raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
    secret = os.getenv("WEBHOOK_SECRET", "test-secret").encode("utf-8")
    signature = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()

    response = await service_client.post(
        "/webhooks/payments",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": signature,
        },
    )

    response.raise_for_status()

    balance_response = await service_client.get(f"/users/{user_id}/balance")
    assert balance_response.is_success
    balance = balance_response.json()
    assert balance["balance"] == tokens
    assert balance["journal"][0]["delta"] == tokens

    await assert_balance_matches_ledger(database_connection, user_id)
    journal = await read_journal(database_connection, user_id)
    assert len(journal) == 1
    assert journal[0]["delta"] == tokens
    assert journal[0]["payment_id"] == payment_id


async def test_missing_or_invalid_payment_signature_does_not_create_user(
    service_client: httpx.AsyncClient,
    database_connection: asyncpg.Connection,
    user_id: str,
    payment_id: str,
) -> None:
    body = {
        "payment_id": payment_id,
        "user_id": user_id,
        "tokens": 17,
        "status": "succeeded",
    }
    raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
    invalid_signature = hmac.new(
        b"wrong-secret",
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    missing_signature_response = await service_client.post(
        "/webhooks/payments",
        content=raw_body,
        headers={"Content-Type": "application/json"},
    )
    assert missing_signature_response.status_code == 401

    response = await service_client.post(
        "/webhooks/payments",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": invalid_signature,
        },
    )

    assert response.status_code == 401
    user_exists = await database_connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM users WHERE user_id = $1)",
        user_id,
    )
    assert user_exists is False


async def test_non_success_payment_and_empty_user_id_do_not_change_state(
    service_client: httpx.AsyncClient,
    database_connection: asyncpg.Connection,
) -> None:
    pending_user_id = f"Pending-user-{uuid4()}"
    pending_payment_id = f"Pending-payment-{uuid4()}"
    pending_body = {
        "payment_id": pending_payment_id,
        "user_id": pending_user_id,
        "tokens": 17,
        "status": "pending",
    }
    pending_raw_body = json.dumps(
        pending_body,
        separators=(",", ":"),
    ).encode("utf-8")
    secret = os.getenv("WEBHOOK_SECRET", "test-secret").encode("utf-8")
    pending_signature = hmac.new(
        secret,
        pending_raw_body,
        hashlib.sha256,
    ).hexdigest()

    pending_response = await service_client.post(
        "/webhooks/payments",
        content=pending_raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": pending_signature,
        },
    )
    assert pending_response.is_success

    pending_user_exists = await database_connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM users WHERE user_id = $1)",
        pending_user_id,
    )
    pending_payment_exists = await database_connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM payments WHERE payment_id = $1)",
        pending_payment_id,
    )
    assert pending_user_exists is False
    assert pending_payment_exists is False

    empty_user_payment_id = f"Empty-user-payment-{uuid4()}"
    empty_user_body = {
        "payment_id": empty_user_payment_id,
        "user_id": "",
        "tokens": 17,
        "status": "succeeded",
    }
    empty_user_raw_body = json.dumps(
        empty_user_body,
        separators=(",", ":"),
    ).encode("utf-8")
    empty_user_signature = hmac.new(
        secret,
        empty_user_raw_body,
        hashlib.sha256,
    ).hexdigest()

    empty_user_response = await service_client.post(
        "/webhooks/payments",
        content=empty_user_raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": empty_user_signature,
        },
    )
    assert empty_user_response.status_code == 422

    empty_user_exists = await database_connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM users WHERE user_id = '')",
    )
    empty_user_payment_exists = await database_connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM payments WHERE payment_id = $1)",
        empty_user_payment_id,
    )
    assert empty_user_exists is False
    assert empty_user_payment_exists is False
