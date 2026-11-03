import asyncio
import json
import pytest
from httpx import AsyncClient, ASGITransport

from llm_router.app import create_app, lifespan
from llm_router.config import Settings
from llm_router.errors import ERROR_MESSAGES
from llm_router.storage import SqliteStore
from llm_router.tenants import add_tenant
from tests.helpers import FakeProvider

@pytest.fixture
async def app_fixture(tmp_path):
    db_file = str(tmp_path / "http_test.db")
    store = SqliteStore(db_file)
    await store.init()
    await add_tenant(store, tenant_id="t-test", api_key="valid-key", tokens_per_minute=1000)
    await store.aclose()

    p1 = FakeProvider(name="primary", status=200, body={
        "id": "chatcmpl-1",
        "choices": [{"message": {"role": "assistant", "content": "hello from primary"}}],
        "usage": {"total_tokens": 42}
    })
    p2 = FakeProvider(name="secondary", status=200, body={
        "id": "chatcmpl-2",
        "choices": [{"message": {"role": "assistant", "content": "hello from secondary"}}],
        "usage": {"total_tokens": 35}
    })

    settings = Settings(
        gateway_db_path=db_file,
        rate_limit_tokens_per_minute=1000,
        rate_limit_window_seconds=60,
        primary_timeout_ms=300,
        secondary_timeout_ms=300,
        purge_interval_seconds=60
    )

    app = create_app(settings, providers=(p1, p2))

    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            yield client, app, p1, p2

@pytest.mark.asyncio
async def test_auth_missing_malformed_unknown(app_fixture):
    client, app, p1, p2 = app_fixture
    body = {"messages": [{"role": "user", "content": "hi"}]}

    # Missing auth
    res = await client.post("/v1/chat/completions", json=body)
    assert res.status_code == 401
    assert "x-request-id" in res.headers
    data = res.json()
    assert data["error"]["code"] == "unauthorized"
    assert data["error"]["message"] == ERROR_MESSAGES["unauthorized"]
    assert data["error"]["retry_after_seconds"] is None

    # Malformed auth
    for bad_auth in ["Bearer", "Bearer ", "Basic 1234", "Token secret", "   "]:
        res = await client.post("/v1/chat/completions", json=body, headers={"Authorization": bad_auth})
        assert res.status_code == 401
        assert res.json()["error"]["code"] == "unauthorized"

    # Unknown key
    res = await client.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer non-existent-key"})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthorized"

@pytest.mark.asyncio
async def test_bad_request_bodies(app_fixture):
    client, app, p1, p2 = app_fixture
    auth = {"Authorization": "Bearer valid-key"}

    # Non-json / invalid JSON
    res = await client.post("/v1/chat/completions", content=b"invalid json{[", headers={**auth, "Content-Type": "application/json"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_request"

    # JSON not an object
    res = await client.post("/v1/chat/completions", json=["array"], headers=auth)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_request"

    # Empty object or missing messages
    res = await client.post("/v1/chat/completions", json={}, headers=auth)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_request"

    # Messages not a list
    res = await client.post("/v1/chat/completions", json={"messages": "string"}, headers=auth)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_request"

    # Messages empty list
    res = await client.post("/v1/chat/completions", json={"messages": []}, headers=auth)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_request"

    # Message missing role or content
    res = await client.post("/v1/chat/completions", json={"messages": [{"role": "user"}]}, headers=auth)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_request"

    # Message role/content not string
    res = await client.post("/v1/chat/completions", json={"messages": [{"role": 123, "content": "hi"}]}, headers=auth)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_request"

    # stream is True
    res = await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}], "stream": True}, headers=auth)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_request"

    # Bad max_tokens (string, boolean, negative, 0, > 8192)
    for bad_tok in ["100", True, False, -5, 0, 99999]:
        res = await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": bad_tok}, headers=auth)
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "invalid_request"

@pytest.mark.asyncio
async def test_standardized_404_and_405(app_fixture):
    client, app, p1, p2 = app_fixture

    # 404
    res = await client.get("/non-existent-endpoint")
    assert res.status_code == 404
    assert "x-request-id" in res.headers
    data = res.json()
    assert data == {
        "error": {
            "type": "gateway_error",
            "code": "not_found",
            "message": ERROR_MESSAGES["not_found"],
            "request_id": res.headers["x-request-id"],
            "retry_after_seconds": None
        }
    }

    # 405
    res = await client.get("/v1/chat/completions")
    assert res.status_code == 405
    assert "x-request-id" in res.headers
    data = res.json()
    assert data == {
        "error": {
            "type": "gateway_error",
            "code": "method_not_allowed",
            "message": ERROR_MESSAGES["method_not_allowed"],
            "request_id": res.headers["x-request-id"],
            "retry_after_seconds": None
        }
    }

@pytest.mark.asyncio
async def test_request_too_large_413(app_fixture):
    client, app, p1, p2 = app_fixture
    auth = {"Authorization": "Bearer valid-key"}
    # Tenant limit is 1000 tokens. Setting max_tokens=1500 exceeds limit.
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1500
    }
    res = await client.post("/v1/chat/completions", json=body, headers=auth)
    assert res.status_code == 413
    assert "x-request-id" in res.headers
    data = res.json()
    assert data["error"]["code"] == "request_too_large"
    assert data["error"]["message"] == ERROR_MESSAGES["request_too_large"]

@pytest.mark.asyncio
async def test_rate_limit_exceeded_429(app_fixture):
    client, app, p1, p2 = app_fixture
    auth = {"Authorization": "Bearer valid-key"}

    # First request: 600 max_tokens -> admitted (leaves remaining ~390)
    res1 = await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 600}, headers=auth)
    # p1 returns usage 42, but wait! After commit, used is 42, so 958 remaining.
    # To hit 429, let's make max_tokens 970:
    res2 = await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 970}, headers=auth)
    assert res2.status_code == 429
    assert "retry-after" in res2.headers
    retry_s = int(res2.headers["retry-after"])
    assert retry_s >= 1
    data = res2.json()
    assert data == {
        "error": {
            "type": "gateway_error",
            "code": "rate_limit_exceeded",
            "message": ERROR_MESSAGES["rate_limit_exceeded"],
            "request_id": res2.headers["x-request-id"],
            "retry_after_seconds": retry_s
        }
    }

@pytest.mark.asyncio
async def test_success_body_and_headers(app_fixture):
    client, app, p1, p2 = app_fixture
    auth = {"Authorization": "Bearer valid-key"}

    # Primary success
    body = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 100}
    res = await client.post("/v1/chat/completions", json=body, headers=auth)
    assert res.status_code == 200
    assert res.json() == p1.body
    assert "x-request-id" in res.headers
    assert res.headers["x-ratelimit-limit"] == "1000"
    assert res.headers["x-ratelimit-remaining"] == str(1000 - 42)
    assert res.headers["x-gateway-fallback"] == "false"

    # Primary fails with 500, secondary succeeds -> fallback is true
    p1.status = 500
    res2 = await client.post("/v1/chat/completions", json=body, headers=auth)
    assert res2.status_code == 200
    assert res2.json() == p2.body
    assert res2.headers["x-gateway-fallback"] == "true"
    usage_res = await client.get("/v1/usage", headers=auth)
    assert usage_res.json()["used"] == 77  # 42 from primary earlier + 35 from secondary, not both

@pytest.mark.asyncio
async def test_usage_endpoint(app_fixture):
    client, app, p1, p2 = app_fixture
    auth = {"Authorization": "Bearer valid-key"}

    # Check initial usage
    res = await client.get("/v1/usage", headers=auth)
    assert res.status_code == 200
    data = res.json()
    assert data["used"] == 0
    assert data["limit"] == 1000
    assert data["remaining"] == 1000
    assert data["reset_after_seconds"] == 0

    # Make a request charging 42 tokens
    await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 50}, headers=auth)

    res2 = await client.get("/v1/usage", headers=auth)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["used"] == 42
    assert data2["limit"] == 1000
    assert data2["remaining"] == 958
    assert data2["reset_after_seconds"] > 0

@pytest.mark.asyncio
async def test_missing_usage_falls_back_to_estimate(app_fixture):
    client, app, p1, p2 = app_fixture
    auth = {"Authorization": "Bearer valid-key"}

    # Provider returns body without usage field
    p1.body = {
        "id": "no-usage",
        "choices": [{"message": {"role": "assistant", "content": "1234"}}]  # 1 token
    }
    # prompt: 'hi' (1 token) + 4 = 5 tokens. Response: 1 token. Total: 6 tokens.
    res = await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 50}, headers=auth)
    assert res.status_code == 200

    usage_res = await client.get("/v1/usage", headers=auth)
    assert usage_res.json()["used"] == 6

@pytest.mark.asyncio
async def test_total_failure_refunds_tokens(app_fixture):
    client, app, p1, p2 = app_fixture
    auth = {"Authorization": "Bearer valid-key"}

    # Usage before
    u_before = (await client.get("/v1/usage", headers=auth)).json()["used"]

    # Both fail
    p1.status = 502
    p2.status = 502

    res = await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}, headers=auth)
    assert res.status_code == 502

    # Usage after should equal usage before
    u_after = (await client.get("/v1/usage", headers=auth)).json()["used"]
    assert u_after == u_before

@pytest.mark.asyncio
async def test_client_cancellation_releases_reservation(app_fixture):
    client, app, p1, p2 = app_fixture
    auth = {"Authorization": "Bearer valid-key"}

    p1.delay = 1.0
    p2.delay = 1.0

    task = asyncio.create_task(
        client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}, headers=auth)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    # Bounded polling loop to check that reservation was released
    store = app.state.store
    released = False
    for _ in range(50):
        def _check(conn):
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM usage WHERE state = 'reserved';")
            return cur.fetchone()[0]
        count = await store.run(_check)
        if count == 0:
            released = True
            break
        await asyncio.sleep(0.02)

    assert released, "Reservation was not released within timeout"
    usage_res = await client.get("/v1/usage", headers=auth)
    assert usage_res.json()["used"] == 0
