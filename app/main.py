import hashlib
import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import asyncpg
from fastapi import FastAPI, Header, HTTPException, Path, Request
from pydantic import BaseModel, Field, ValidationError

ExternalIdentifier = Annotated[str, Field(strict=True, min_length=1, max_length=128)]


class PaymentPayload(BaseModel):
    payment_id: ExternalIdentifier
    user_id: ExternalIdentifier
    tokens: Annotated[int, Field(strict=True, gt=0)]
    status: str


def create_app() -> FastAPI:
    pool: asyncpg.Pool | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal pool
        pool = await asyncpg.create_pool(
            os.environ["APP_DATABASE_URL"],
            min_size=1,
            max_size=10,
        )
        try:
            yield
        finally:
            await pool.close()
            pool = None

    def get_pool() -> asyncpg.Pool:
        if pool is None:
            raise RuntimeError("Database pool is not initialized")
        return pool

    application = FastAPI(lifespan=lifespan)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/webhooks/payments")
    async def receive_payment(
        request: Request,
        x_signature: Annotated[str | None, Header()] = None,
    ) -> dict[str, str]:
        raw_body = await request.body()
        secret = os.environ["WEBHOOK_SECRET"].encode("utf-8")
        expected_signature = hmac.new(
            secret,
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        if x_signature is None or not hmac.compare_digest(
            expected_signature,
            x_signature,
        ):
            raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            payload = PaymentPayload.model_validate_json(raw_body)
        except ValidationError as error:
            invalid_json = any(
                detail["type"] == "json_invalid" for detail in error.errors()
            )
            raise HTTPException(
                status_code=400 if invalid_json else 422,
                detail="Invalid payment payload",
            ) from error

        if payload.status != "succeeded":
            return {"status": "ignored"}

        async with get_pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO users (user_id, balance)
                    VALUES ($1, 0)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    payload.user_id,
                )
                inserted_payment_id = await connection.fetchval(
                    """
                    INSERT INTO payments (payment_id, user_id, tokens)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (payment_id) DO NOTHING
                    RETURNING payment_id
                    """,
                    payload.payment_id,
                    payload.user_id,
                    payload.tokens,
                )
                if inserted_payment_id is None:
                    existing_payment = await connection.fetchrow(
                        """
                        SELECT user_id, tokens
                        FROM payments
                        WHERE payment_id = $1
                        """,
                        payload.payment_id,
                    )
                    if existing_payment is None:
                        raise RuntimeError("Conflicting payment disappeared")
                    if (
                        existing_payment["user_id"] != payload.user_id
                        or existing_payment["tokens"] != payload.tokens
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="Payment ID has conflicting data",
                        )
                    return {"status": "duplicate"}

                await connection.execute(
                    """
                    UPDATE users
                    SET balance = balance + $2
                    WHERE user_id = $1
                    """,
                    payload.user_id,
                    payload.tokens,
                )
                await connection.execute(
                    """
                    INSERT INTO ledger_entries (
                        entry_id, user_id, delta, operation, payment_id
                    )
                    VALUES ($1, $2, $3, 'payment', $4)
                    """,
                    uuid4(),
                    payload.user_id,
                    payload.tokens,
                    payload.payment_id,
                )

        return {"status": "succeeded"}

    @application.get("/users/{user_id}/balance")
    async def get_balance(
        user_id: Annotated[
            str,
            Path(strict=True, min_length=1, max_length=128),
        ],
    ) -> dict[str, object]:
        async with get_pool().acquire() as connection:
            user = await connection.fetchrow(
                "SELECT balance FROM users WHERE user_id = $1",
                user_id,
            )
            if user is None:
                raise HTTPException(status_code=404, detail="User not found")

            journal = await connection.fetch(
                """
                SELECT entry_id, delta, operation, payment_id, created_at
                FROM ledger_entries
                WHERE user_id = $1
                ORDER BY created_at DESC, entry_id DESC
                LIMIT 20
                """,
                user_id,
            )

        return {
            "user_id": str(user_id),
            "balance": user["balance"],
            "journal": [
                {
                    "entry_id": str(entry["entry_id"]),
                    "delta": entry["delta"],
                    "operation": entry["operation"],
                    "payment_id": entry["payment_id"],
                    "created_at": entry["created_at"],
                }
                for entry in journal
            ],
        }

    return application


app = create_app()
