import asyncio
import hashlib
import hmac
import json
import os

import httpx


async def test_first_provider_failure_completes_on_second_leg(
    service_client: httpx.AsyncClient,
    user_id: str,
    payment_id: str,
) -> None:
    """Отказ первого провайдера приводит к успешному завершению на втором провайдере."""
    user_id = f"Provider-fallback-user-{user_id}"
    payment_id = f"Provider-fallback-payment-{payment_id}"
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
            "prompt": "Provider fallback test",
            "ref_images": [],
        },
    )
    assert generate_response.status_code == 202
    task_id = generate_response.json()["task_id"]

    for _ in range(40):
        task_response = await service_client.get(f"/tasks/{task_id}")
        assert task_response.is_success, task_response.text
        task = task_response.json()
        if task["status"] in {"completed", "failed"}:
            break
        await asyncio.sleep(0.25)
    else:
        raise AssertionError("Task did not finish after provider fallback")

    assert task["status"] == "completed", f"статус: {task['status']}"
    assert task["current_leg"] == "beta", f"этап: {task['current_leg']}"
    assert task["result_url"], f"результат: {task['result_url']}"
    assert [attempt["provider"] for attempt in task["attempts"]] == [
        "alpha",
        "beta",
    ], f"провайдеры: {[a['provider'] for a in task['attempts']]}"

    attempts_chain = [f"{a['provider']}:{a['status']}" for a in task["attempts"]]
    balance_response = await service_client.get(f"/users/{user_id}/balance")
    balance_response.raise_for_status()
    balance = balance_response.json()["balance"]
    print(f"цепочка: {' → '.join(attempts_chain)}, статус: {task['status']}, "
          f"попыток: {len(task['attempts'])}, баланс: {balance}")
    
    assert [attempt["status"] for attempt in task["attempts"]] == [
        "failed",
        "completed",
    ], f"статусы: {[a['status'] for a in task['attempts']]}"

    assert task["attempts"][0]["error_type"] == "technical", \
        f"ошибка: {task['attempts'][0]['error_type']}"
    assert task["attempts"][1]["error_type"] is None, \
        f"ошибка: {task['attempts'][1]['error_type']}"
