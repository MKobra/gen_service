# gen_service

## Запуск и миграции

Запустить PostgreSQL, применить миграции и поднять API в четырёх рабочих процессах:

```bash
docker compose up --build
```

Compose ждёт готовности PostgreSQL, применяет миграции командой `alembic upgrade head`, затем запускает API. Чтобы создать следующую миграцию:

```bash
docker compose run --rm migrate alembic -c pyproject.toml revision -m "краткое описание изменения схемы"
```

Новая миграция применится при следующем `docker compose up`.

## Установка тестовых инструментов и запуск тестов

Сначала запустите Compose. Затем создайте виртуальное окружение и установите проект вместе с тестовыми инструментами.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest
```

### Linux и macOS

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest
```

По умолчанию тестовый клиент обращается к `http://localhost:8000`, а тесты, читающие состояние БД, подключаются к PostgreSQL Compose по адресу `localhost:5432`. Переопределить адреса можно переменными `SERVICE_BASE_URL` и `TEST_DATABASE_URL`.

## Четыре обязательных приёмочных теста

Эти тесты соответствуют четырём сценариям из `docs/TASK.md`:

1. 20 одновременных одинаковых вебхуков оплаты дают одно начисление: `tests/test_payment_webhook.py::test_concurrent_duplicate_payments_are_credited_once`.
2. 10 одновременных отказов на последней ноге дают один возврат: `tests/test_provider_exhaustion.py::test_concurrent_failures_on_last_leg_refund_once`.
3. Отказ первой ноги приводит к завершению на второй, в истории две попытки: `tests/test_provider_fallback.py::test_first_provider_failure_completes_on_second_leg`.
4. После таймаута поздний `completed` игнорируется: `tests/test_provider_timeout.py::test_timeout_falls_back_and_ignores_late_completion`.

Запустить только эти четыре теста из активированного виртуального окружения:

```bash
python -m pytest -q \
  tests/test_payment_webhook.py::test_concurrent_duplicate_payments_are_credited_once \
  tests/test_provider_exhaustion.py::test_concurrent_failures_on_last_leg_refund_once \
  tests/test_provider_fallback.py::test_first_provider_failure_completes_on_second_leg \
  tests/test_provider_timeout.py::test_timeout_falls_back_and_ignores_late_completion
```

В Windows PowerShell запустите те же тесты командой:

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_payment_webhook.py::test_concurrent_duplicate_payments_are_credited_once `
  tests/test_provider_exhaustion.py::test_concurrent_failures_on_last_leg_refund_once `
  tests/test_provider_fallback.py::test_first_provider_failure_completes_on_second_leg `
  tests/test_provider_timeout.py::test_timeout_falls_back_and_ignores_late_completion
```

## Статические проверки

Выполняйте команды из активированного виртуального окружения:

```bash
ruff check .
mypy --strict
```

В Windows PowerShell без активации виртуального окружения:

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe --strict
```

## Вопросы и ответы

### Как запустить сервис и тесты?

Запусти `docker compose up --build`. Compose поднимет PostgreSQL, применит миграции, запустит оба тестовых провайдера и API в четырёх рабочих процессах. Команды создания виртуального окружения и установки зависимостей для Windows PowerShell, Linux и macOS приведены выше. После установки тесты запускаются командой `python -m pytest` из активированного окружения; в PowerShell можно вызвать `.\.venv\Scripts\python.exe -m pytest`.

### Чем обеспечивается выполнение начисления и возврата ровно один раз?

Для начисления используется уникальный `payment_id`. В одной транзакции записываются платёж, увеличение баланса и строка журнала. Повторный вебхук не создаёт второе начисление.

Возврат выполняется в транзакции только при условном переходе задачи из `processing` в `failed`. Баланс и строка `refund` в журнале фиксируются вместе с этим переходом; при конкурентных отказах его выигрывает один обработчик.

### Что произойдёт, если процесс завершится во время обработки вебхука?

Если транзакция ещё не зафиксирована, PostgreSQL откатит её изменения. Попытка останется доступной для повторной доставки вебхука или обработки сторожем таймаутов. Если транзакция уже зафиксирована, но ответ клиенту потерян, повторный вебхук будет подтверждён без повторного изменения результата или баланса.

### Как обрабатывается `completed`, пришедший после таймаута?

Решение сторожа побеждает: поздний `completed` получает HTTP `200`, попытка помечается `late_ignored`, а статус задачи, результат и баланс не меняются. Поздний `failed` тоже получает `200`, но попытка остаётся `timed_out`. Срок ожидания хранится в PostgreSQL; сторож работает в каждом worker-процессе, а `FOR UPDATE SKIP LOCKED` не позволяет обработать одну просроченную попытку дважды. В Compose `PROVIDER_TIMEOUT` по умолчанию равен 3 секундам.

Сохранённая попытка отправки использует аренду в БД. После перезапуска процесса истёкшая попытка в состоянии `dispatching` отправляется повторно. Если провайдер успел принять `POST /tasks`, а процесс остановился до сохранения ответа, повторная отправка может создать у провайдера дубликат: контракт не предусматривает ключ идемпотентности.

## Настройки тестовых провайдеров

Для каждого тестового провайдера доступны переменные с префиксом `FAKE_PROVIDER_ALPHA_` или `FAKE_PROVIDER_BETA_`:

- `FAILURE_RATE` — вероятность отказа от `0.0` до `1.0`, по умолчанию `0.0`.
- `FAILURE_TYPE` — тип отказа: `technical`, `moderation` или `payload`; по умолчанию `technical`.
- `DUPLICATE_WEBHOOK=true` — отправить два одинаковых подписанных вебхука одновременно; по умолчанию `false`.
- `DELAY_SECONDS` — задержка перед отправкой вебхука, по умолчанию 2 секунды. Задержка больше `PROVIDER_TIMEOUT` позволяет проверить поздний `completed`.
- `FAILURE_PROMPT` — точный prompt для принудительного асинхронного отказа независимо от `FAILURE_RATE`.
- `SYNC_FAILURE_PROMPT` — точный prompt, для которого `POST /tasks` отвечает `503`.
- `SILENT_PROMPT` — точный prompt, для которого вебхук не отправляется.

Пример включения вероятности отказа и повторной доставки для `alpha`:

```powershell
$env:FAKE_PROVIDER_ALPHA_FAILURE_RATE = "0.25"
$env:FAKE_PROVIDER_ALPHA_DUPLICATE_WEBHOOK = "true"
docker compose up --build
```

В Linux и macOS задайте те же переменные через `export` перед запуском `docker compose up --build`.
