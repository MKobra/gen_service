import httpx


async def test_health_endpoint_returns_ok(service_client: httpx.AsyncClient) -> None:
    response = await service_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
