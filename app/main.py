import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.request import Request as UrlRequest
from urllib.request import urlopen
from uuid import uuid4

import asyncpg
from fastapi import FastAPI, Header, HTTPException, Path, Request
from pydantic import AnyHttpUrl, BaseModel, Field, TypeAdapter, ValidationError

logger = logging.getLogger(__name__)

ExternalIdentifier = Annotated[str, Field(strict=True, min_length=1, max_length=128)]


class PaymentPayload(BaseModel):
    payment_id: ExternalIdentifier
    user_id: ExternalIdentifier
    tokens: Annotated[int, Field(strict=True, gt=0)]
    status: str


class GeneratePayload(BaseModel):
    user_id: ExternalIdentifier
    model_slug: ExternalIdentifier
    prompt: str
    ref_images: object = Field(
        default_factory=list,
        json_schema_extra={
            "type": "array",
            "items": {"type": "string", "format": "uri"},
        },
    )


class ProviderAccepted(BaseModel):
    external_id: ExternalIdentifier


class ProviderCallback(BaseModel):
    external_id: ExternalIdentifier
    status: Literal["completed", "failed"]
    result_url: str | None = None
    error_type: Literal["technical", "moderation", "payload"] | None = None


def create_app() -> FastAPI:
    pool: asyncpg.Pool | None = None

    def get_pool() -> asyncpg.Pool:
        if pool is None:
            raise RuntimeError("Database pool is not initialized")
        return pool

    def post_provider_task(
        url: str,
        task_id: str,
        prompt: str,
        ref_images: list[str],
    ) -> ProviderAccepted:
        body = json.dumps(
            {
                "task_id": task_id,
                "prompt": prompt,
                "ref_images": ref_images,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = UrlRequest(
            f"{url}/tasks",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            return ProviderAccepted.model_validate_json(response.read())

    async def dispatch_attempts() -> None:
        while True:
            try:
                async with get_pool().acquire() as connection:
                    async with connection.transaction():
                        attempt = await connection.fetchrow(
                            """
                            WITH candidate AS (
                                SELECT attempts.attempt_id, attempts.task_id
                                FROM task_attempts AS attempts
                                JOIN tasks ON tasks.task_id = attempts.task_id
                                WHERE tasks.status = 'processing'
                                  AND (
                                    attempts.status = 'pending'
                                    OR (
                                      attempts.status = 'dispatching'
                                      AND attempts.dispatch_lease_until < now()
                                    )
                                  )
                                ORDER BY attempts.created_at
                                FOR UPDATE OF attempts SKIP LOCKED
                                LIMIT 1
                            )
                            UPDATE task_attempts AS attempts
                            SET status = 'dispatching',
                                dispatch_lease_until = now() + interval '30 seconds'
                            FROM candidate
                            JOIN tasks ON tasks.task_id = candidate.task_id
                            WHERE attempts.attempt_id = candidate.attempt_id
                            RETURNING attempts.attempt_id, attempts.task_id,
                                      attempts.provider, tasks.prompt,
                                      tasks.ref_images
                            """
                        )
                if attempt is None:
                    await asyncio.sleep(0.1)
                    continue

                provider_url = os.environ[
                    f"PROVIDER_{attempt['provider'].upper()}_URL"
                ]
                ref_images_value = attempt["ref_images"]
                ref_images = (
                    json.loads(ref_images_value)
                    if isinstance(ref_images_value, str)
                    else ref_images_value
                )
                try:
                    accepted = await asyncio.to_thread(
                        post_provider_task,
                        provider_url,
                        str(attempt["task_id"]),
                        attempt["prompt"],
                        ref_images,
                    )
                except Exception:
                    logger.exception("Provider task dispatch failed")
                    async with get_pool().acquire() as connection:
                        await connection.execute(
                            """
                            UPDATE task_attempts
                            SET status = 'failed', error_type = 'technical',
                                dispatch_lease_until = NULL
                            WHERE attempt_id = $1 AND status = 'dispatching'
                            """,
                            attempt["attempt_id"],
                        )
                    continue

                async with get_pool().acquire() as connection:
                    await connection.execute(
                        """
                        UPDATE task_attempts
                        SET status = 'waiting', external_id = $2,
                            dispatch_lease_until = NULL
                        WHERE attempt_id = $1 AND status = 'dispatching'
                        """,
                        attempt["attempt_id"],
                        accepted.external_id,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Attempt dispatcher failed")
                await asyncio.sleep(0.5)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal pool
        pool = await asyncpg.create_pool(
            os.environ["APP_DATABASE_URL"],
            min_size=1,
            max_size=10,
        )
        dispatcher = asyncio.create_task(dispatch_attempts())
        try:
            yield
        finally:
            dispatcher.cancel()
            try:
                await dispatcher
            except asyncio.CancelledError:
                pass
            await pool.close()
            pool = None

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

    @application.post("/generate", status_code=202)
    async def generate(payload: GeneratePayload) -> dict[str, str]:
        if not payload.prompt.strip():
            raise HTTPException(status_code=400, detail="Prompt must not be empty")

        try:
            ref_images = TypeAdapter(list[AnyHttpUrl]).validate_python(
                payload.ref_images
            )
        except ValidationError as error:
            raise HTTPException(
                status_code=400,
                detail="Invalid reference images",
            ) from error

        async with get_pool().acquire() as connection:
            async with connection.transaction():
                model = await connection.fetchrow(
                    "SELECT price FROM models WHERE slug = $1",
                    payload.model_slug,
                )
                if model is None:
                    raise HTTPException(status_code=404, detail="Model not found")

                legs = await connection.fetch(
                    """
                    SELECT priority, provider, max_ref_images
                    FROM model_legs
                    WHERE model_slug = $1
                    ORDER BY priority
                    """,
                    payload.model_slug,
                )
                selected_leg = next(
                    (
                        leg
                        for leg in legs
                        if len(ref_images) <= leg["max_ref_images"]
                    ),
                    None,
                )
                if selected_leg is None:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "No provider leg accepts this number of reference "
                            "images"
                        ),
                    )

                user = await connection.fetchrow(
                    "SELECT balance FROM users WHERE user_id = $1 FOR UPDATE",
                    payload.user_id,
                )
                if user is None or user["balance"] < model["price"]:
                    raise HTTPException(
                        status_code=402,
                        detail="Insufficient balance",
                    )

                active_task_count = await connection.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM tasks
                    WHERE user_id = $1 AND status NOT IN ('completed', 'failed')
                    """,
                    payload.user_id,
                )
                if active_task_count >= 3:
                    raise HTTPException(
                        status_code=429,
                        detail="User has reached the active task limit",
                    )

                task_id = uuid4()
                debited_balance = await connection.fetchval(
                    """
                    UPDATE users
                    SET balance = balance - $2
                    WHERE user_id = $1 AND balance >= $2
                    RETURNING balance
                    """,
                    payload.user_id,
                    model["price"],
                )
                if debited_balance is None:
                    raise HTTPException(
                        status_code=402,
                        detail="Insufficient balance",
                    )

                await connection.execute(
                    """
                    INSERT INTO tasks (
                        task_id, user_id, model_slug, prompt, ref_images, price
                    )
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                    """,
                    task_id,
                    payload.user_id,
                    payload.model_slug,
                    payload.prompt,
                    json.dumps([str(image) for image in ref_images]),
                    model["price"],
                )
                await connection.execute(
                    """
                    INSERT INTO task_attempts (
                        attempt_id, task_id, attempt_number, provider, status
                    )
                    VALUES ($1, $2, 1, $3, 'pending')
                    """,
                    uuid4(),
                    task_id,
                    selected_leg["provider"],
                )
                await connection.execute(
                    """
                    INSERT INTO ledger_entries (
                        entry_id, user_id, delta, operation, payment_id, task_id
                    )
                    VALUES ($1, $2, $3, 'debit', NULL, $4)
                    """,
                    uuid4(),
                    payload.user_id,
                    -model["price"],
                    task_id,
                )

        return {"task_id": str(task_id)}

    @application.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, object]:
        async with get_pool().acquire() as connection:
            task = await connection.fetchrow(
                """
                SELECT task_id, status, result_url
                FROM tasks
                WHERE task_id = $1
                """,
                task_id,
            )
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")

            attempts = await connection.fetch(
                """
                SELECT provider, status, error_type, created_at
                FROM task_attempts
                WHERE task_id = $1
                ORDER BY attempt_number
                """,
                task_id,
            )

        return {
            "task_id": str(task["task_id"]),
            "status": task["status"],
            "current_leg": attempts[-1]["provider"] if attempts else None,
            "result_url": task["result_url"],
            "attempts": [
                {
                    "provider": attempt["provider"],
                    "status": attempt["status"],
                    "error_type": attempt["error_type"],
                    "created_at": attempt["created_at"],
                }
                for attempt in attempts
            ],
        }

    @application.post("/webhooks/providers/{provider_name}")
    async def receive_provider_webhook(
        provider_name: Annotated[str, Path(min_length=1, max_length=64)],
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
            payload = ProviderCallback.model_validate_json(raw_body)
        except ValidationError as error:
            invalid_json = any(
                detail["type"] == "json_invalid" for detail in error.errors()
            )
            raise HTTPException(
                status_code=400 if invalid_json else 422,
                detail="Invalid provider payload",
            ) from error

        if payload.status == "completed" and not payload.result_url:
            raise HTTPException(status_code=422, detail="Missing result_url")
        if payload.status == "failed" and payload.error_type is None:
            raise HTTPException(status_code=422, detail="Missing error_type")

        async with get_pool().acquire() as connection:
            async with connection.transaction():
                attempt = await connection.fetchrow(
                    """
                    SELECT attempt_id, task_id, status
                    FROM task_attempts
                    WHERE provider = $1 AND external_id = $2
                    FOR UPDATE
                    """,
                    provider_name,
                    payload.external_id,
                )
                if attempt is None:
                    raise HTTPException(status_code=404, detail="Attempt not found")
                if attempt["status"] != "waiting":
                    return {"status": "ignored"}

                if payload.status == "completed":
                    completed_task_id = await connection.fetchval(
                        """
                        UPDATE tasks
                        SET status = 'completed', result_url = $2
                        WHERE task_id = $1 AND status = 'processing'
                        RETURNING task_id
                        """,
                        attempt["task_id"],
                        payload.result_url,
                    )
                    if completed_task_id is None:
                        return {"status": "ignored"}
                    await connection.execute(
                        """
                        UPDATE task_attempts
                        SET status = 'completed'
                        WHERE attempt_id = $1
                        """,
                        attempt["attempt_id"],
                    )
                else:
                    await connection.execute(
                        """
                        UPDATE task_attempts
                        SET status = 'failed', error_type = $2
                        WHERE attempt_id = $1
                        """,
                        attempt["attempt_id"],
                        payload.error_type,
                    )

        return {"status": "accepted"}

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
