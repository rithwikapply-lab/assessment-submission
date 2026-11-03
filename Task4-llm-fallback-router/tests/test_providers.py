import pytest
import httpx
from llm_router.providers import HttpProvider, ProviderRequest, ProviderResult, ProviderConnectionError

@pytest.mark.asyncio
async def test_http_provider_success():
    def handler(request: httpx.Request):
        assert request.headers["authorization"] == "Bearer test-key"
        assert request.headers["content-type"] == "application/json"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": {"total_tokens": 8}},
            headers={"retry-after": "5"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = HttpProvider("test-p", "https://api.example.com/v1", "test-key", "gpt-4o", client=client)
        req = ProviderRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=100)
        res = await provider.complete(req)
        assert res.status == 200
        assert res.body["usage"]["total_tokens"] == 8
        assert res.retry_after_s == 5.0

@pytest.mark.asyncio
async def test_http_provider_connection_error():
    def handler(request: httpx.Request):
        raise httpx.ConnectError("Connection refused")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = HttpProvider("test-p", "https://api.example.com/v1", "test-key", "gpt-4o", client=client)
        req = ProviderRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=100)
        with pytest.raises(ProviderConnectionError):
            await provider.complete(req)
