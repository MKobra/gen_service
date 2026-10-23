import os
from collections.abc import AsyncIterator
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio


@pytest.fixture
def service_base_url() -> str:
    return os.getenv("SERVICE_BASE_URL", "http://localhost:8000")


@pytest_asyncio.fixture
async def service_client(service_base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=service_base_url) as client:
        yield client


@pytest_asyncio.fixture
async def database_connection() -> AsyncIterator[asyncpg.Connection]:
    database_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://gen_service:gen_service@localhost:5432/gen_service",
    )
    connection = await asyncpg.connect(database_url)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
def user_id() -> str:
    return str(uuid4())


@pytest.fixture
def payment_id() -> str:
    return str(uuid4())
