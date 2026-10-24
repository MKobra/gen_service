import asyncio
import hashlib
import hmac
import json
import os

import asyncpg
import httpx
import pytest


def signed_body(body: dict[str, object]) -> tuple[bytes, dict[str, str]]:
    raw_body = json.dumps(body, separators=(",", ":")).encode("utf-8")
    secret = os.getenv("WEBHOOK_SECRET", "test-secret").encode("utf-8")
    signature = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return raw_body, {
        "Content-Type": "application/json",
        "X-Signature": signature,
    }


@pytest.mark.parametrize("scenario", ["fallback", "exhaustion"])
async def test_timeout_falls_back_and_ignores_late_completion(
    scenario: str,
    service_client: httpx.AsyncClient,
    database_connection: asyncpg.Connection,
    user_id: str,
    payment_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Тайм-аут первого провайдера приводит к завершению на втором
    с игнорированием поздних ответов."""
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

    if scenario == "fallback":
        # Сценарий (а): сторож перевёл задачу на вторую ногу, она завершилась
        # Настройки для fallback: второй провайдер работает нормально
        monkeypatch.setenv("FAKE_PROVIDER_FAILURE_RATE", "0")  # beta всегда успешен
        monkeypatch.setenv("FAKE_PROVIDER_FAILURE_PROMPT", "forced failure prompt")
        
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
        
    else:  # scenario == "exhaustion"
        # Сценарий (б): сторож исчерпал все ноги
        # Настройки для exhaustion: второй провайдер тоже не справляется
        monkeypatch.setenv("FAKE_PROVIDER_FAILURE_RATE", "0")  # \
            # отключаем случайные провалы
        monkeypatch.setenv("FAKE_PROVIDER_FAILURE_PROMPT", "forced failure prompt")
        monkeypatch.setenv("FAKE_PROVIDER_FAILURE_TYPE", "technical")
        
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

        # Ждем, когда alpha закончится с таймаутом, и beta начнет обработку
        for _ in range(280):
            task_response = await service_client.get(f"/tasks/{task_id}")
            assert task_response.is_success, task_response.text
            task = task_response.json()
            if (len(task["attempts"]) == 2 and
                    task["attempts"][1]["status"] == "waiting"):
                break
            await asyncio.sleep(0.025)
        else:
            raise AssertionError("Beta provider did not start processing")

        # Получаем external_id для beta из базы данных
        beta_external_id = await database_connection.fetchval(
            """
            SELECT external_id
            FROM task_attempts
            WHERE task_id = $1 AND attempt_number = 2
            """,
            task_id,
        )
        assert beta_external_id is not None
        
        # Теперь вручную отправляем failure для beta
        beta_failure_body = {
            "external_id": beta_external_id,
            "status": "failed",
            "error_type": "technical",
        }
        beta_failure_raw_body, beta_failure_headers = signed_body(beta_failure_body)
        beta_failure_response = await service_client.post(
            "/webhooks/providers/beta",
            content=beta_failure_raw_body,
            headers=beta_failure_headers,
        )
        beta_failure_response.raise_for_status()

        # Ждем, когда задача станет failed
        for _ in range(280):
            task_response = await service_client.get(f"/tasks/{task_id}")
            assert task_response.is_success, task_response.text
            task = task_response.json()
            if task["status"] == "failed" and len(task["attempts"]) == 2:
                break
            await asyncio.sleep(0.025)
        else:
            raise AssertionError("Task did not fail after beta failure")

        assert task["current_leg"] == "beta"
        assert [attempt["provider"] for attempt in task["attempts"]] == [
            "alpha",
            "beta",
        ]
        assert task["attempts"][0]["status"] in {"timed_out", "late_ignored"}
        assert task["attempts"][1]["status"] == "failed"
        assert task["result_url"] is None

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
    assert all(response.status_code == 200 for response in late_responses), \
        f"статусы: {[r.status_code for r in late_responses]}"

    attempt_status = await database_connection.fetchval(
        """
        SELECT status
        FROM task_attempts
        WHERE task_id = $1 AND attempt_number = 1
        """,
        task_id,
    )
    assert attempt_status == "late_ignored", f"статус: {attempt_status}"
    final_task = await service_client.get(f"/tasks/{task_id}")
    # Для exhaustion задача должна остаться failed
    if scenario == "fallback":
        assert final_task.json()["status"] == "completed", \
            f"статус: {final_task.json()['status']}"
        assert final_task.json()["current_leg"] == "beta", \
            f"этап: {final_task.json()['current_leg']}"

        balance_response = await service_client.get(f"/users/{user_id}/balance")
        balance_response.raise_for_status()
        balance = balance_response.json()["balance"]
        journal = balance_response.json()["journal"]
        
        assert balance == 0, f"баланс: {balance}"
        assert all(
            entry["operation"] != "refund"
            for entry in journal
        ), f"возвратов: {len([e for e in journal if e['operation'] == 'refund'])}"
        
        result_exists = final_task.json()["result_url"] is not None
        print(f"поздний completed: 200x{len(late_responses)}, "
              f"статус: {final_task.json()['status']}, возвратов: 0, "
              f"попытка помечена late_ignored: да, "
              f"результат: {'есть' if result_exists else 'нет'}, "
              f"баланс: {balance}")
              
    else:  # scenario == "exhaustion"
        assert final_task.json()["status"] == "failed", \
            f"статус: {final_task.json()['status']}"
        assert final_task.json()["current_leg"] == "beta", \
            f"этап: {final_task.json()['current_leg']}"

        balance_response = await service_client.get(f"/users/{user_id}/balance")
        balance_response.raise_for_status()
        balance = balance_response.json()["balance"]
        journal = balance_response.json()["journal"]
        
        refund_count = len([e for e in journal if e['operation'] == 'refund'])
        assert refund_count == 1, f"возвратов: {refund_count}"
        assert balance == 2, f"баланс: {balance}"
        assert final_task.json()["result_url"] is None, "результат должен отсутствовать"
        
        print(f"поздний completed: 200x{len(late_responses)}, "
              f"статус: {final_task.json()['status']}, возвратов: {refund_count}, "
              f"попытка помечена late_ignored: да, результат: нет, баланс: {balance}")
