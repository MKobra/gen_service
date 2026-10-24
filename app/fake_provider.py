import asyncio
import hashlib
import hmac
import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import FastAPI
from pydantic import BaseModel


class TaskPayload(BaseModel):
    task_id: str
    prompt: str
    ref_images: list[str]


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


async def send_completion(external_id: str) -> None:
    delay = float(os.getenv("FAKE_PROVIDER_DELAY_SECONDS", "1"))
    await asyncio.sleep(delay)
    provider_name = os.environ["PROVIDER_NAME"]
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
        await asyncio.to_thread(post_webhook, callback_url, raw_body, signature)
    except URLError:
        return


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tasks", status_code=202)
async def create_task(payload: TaskPayload) -> dict[str, str]:
    external_id = str(uuid4())
    asyncio.create_task(send_completion(external_id))
    return {"external_id": external_id}
