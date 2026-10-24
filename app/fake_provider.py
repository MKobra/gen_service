import asyncio
import hashlib
import hmac
import json
import os
import random
from typing import Literal
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


class TaskPayload(BaseModel):
    task_id: str
    prompt: str
    ref_images: list[str]


FailureType = Literal["technical", "moderation", "payload"]


app = FastAPI()


def post_webhook(url: str, body: bytes, signature: str) -> None:
    request = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": signature,
        },
        method="POST",
    )
    with urlopen(request, timeout=10):
        pass


def provider_settings() -> tuple[float, bool, FailureType]:
    failure_rate = float(os.getenv("FAKE_PROVIDER_FAILURE_RATE", "0"))
    if not 0 <= failure_rate <= 1:
        raise ValueError("FAKE_PROVIDER_FAILURE_RATE must be between 0 and 1")

    duplicate_setting = os.getenv("FAKE_PROVIDER_DUPLICATE_WEBHOOK", "false").lower()
    if duplicate_setting not in {"true", "false"}:
        raise ValueError("FAKE_PROVIDER_DUPLICATE_WEBHOOK must be true or false")

    configured_failure_type = os.getenv("FAKE_PROVIDER_FAILURE_TYPE", "technical")
    if configured_failure_type == "technical":
        failure_type: FailureType = "technical"
    elif configured_failure_type == "moderation":
        failure_type = "moderation"
    elif configured_failure_type == "payload":
        failure_type = "payload"
    else:
        raise ValueError("FAKE_PROVIDER_FAILURE_TYPE is invalid")

    return failure_rate, duplicate_setting == "true", failure_type


async def send_completion(
    external_id: str,
    prompt: str,
    failure_rate: float,
    duplicate_webhook: bool,
    failure_type: FailureType,
) -> None:
    if prompt == os.getenv("FAKE_PROVIDER_SILENT_PROMPT"):
        return

    delay = float(os.getenv("FAKE_PROVIDER_DELAY_SECONDS", "1"))
    await asyncio.sleep(delay)
    provider_name = os.environ["PROVIDER_NAME"]
    failure_prompt = os.getenv("FAKE_PROVIDER_FAILURE_PROMPT")
    if prompt == failure_prompt or random.random() < failure_rate:
        callback = {
            "external_id": external_id,
            "status": "failed",
            "error_type": failure_type,
        }
    else:
        callback = {
            "external_id": external_id,
            "status": "completed",
            "result_url": f"https://files.example/{external_id}",
        }
    raw_body = json.dumps(callback, separators=(",", ":")).encode("utf-8")
    secret = os.environ["WEBHOOK_SECRET"].encode("utf-8")
    signature = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    callback_url = (
        f"{os.environ['APP_CALLBACK_URL']}/webhooks/providers/{provider_name}"
    )
    try:
        deliveries = 2 if duplicate_webhook else 1
        await asyncio.gather(
            *(
                asyncio.to_thread(post_webhook, callback_url, raw_body, signature)
                for _ in range(deliveries)
            )
        )
    except URLError:
        return


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tasks", status_code=202)
async def create_task(payload: TaskPayload) -> dict[str, str]:
    try:
        failure_rate, duplicate_webhook, failure_type = provider_settings()
    except ValueError as error:
        raise HTTPException(
            status_code=500,
            detail="Invalid fake-provider settings",
        ) from error

    sync_failure_prompt = os.getenv("FAKE_PROVIDER_SYNC_FAILURE_PROMPT")
    if sync_failure_prompt is not None and payload.prompt == sync_failure_prompt:
        raise HTTPException(status_code=503, detail="Configured synchronous failure")

    external_id = str(uuid4())
    asyncio.create_task(
        send_completion(
            external_id,
            payload.prompt,
            failure_rate,
            duplicate_webhook,
            failure_type,
        )
    )
    return {"external_id": external_id}
