import asyncio
import logging
import sqlite3
import pytest
from httpx import AsyncClient, ASGITransport

from llm_router.app import create_app, lifespan
from llm_router.config import Settings
from llm_router.errors import ERROR_MESSAGES
from llm_router.providers import ProviderConnectionError
from llm_router.storage import SqliteStore
from llm_router.tenants import add_tenant
from tests.helpers import FakeProvider

FORBIDDEN = [
    "Traceback (most recent call last)",
    "/Users/",
    "sk-secret-key",
    "sqlite3.OperationalError",
    "httpx",
    "primary.internal.example",
    "SELECT * FROM",
]

LEAKY_TEXT = (
    "Traceback (most recent call last): File /Users/test/app.py sk-secret-key "
    "sqlite3.OperationalError: SELECT * FROM tenants httpx.ConnectError at primary.internal.example"
)

def assert_sanitized(res, expected_status: int, expected_code: str):
    assert res.status_code == expected_status
    assert "x-request-id" in res.headers

    data = res.json()
    assert set(data.keys()) == {"error"}
    err = data["error"]
    assert set(err.keys()) == {"type", "code", "message", "request_id", "retry_after_seconds"}
    assert err["type"] == "gateway_error"
    assert err["code"] == expected_code
    assert err["message"] == ERROR_MESSAGES[expected_code]
    assert err["request_id"] == res.headers["x-request-id"]

    # Check that forbidden strings are nowhere in the body or headers
    text = res.text
    for forbidden in FORBIDDEN:
        assert forbidden not in text, f"Forbidden '{forbidden}' leaked in body: {text}"
        for k, v in res.headers.items():
            assert forbidden not in k, f"Forbidden '{forbidden}' in header key {k}"
            assert forbidden not in v, f"Forbidden '{forbidden}' in header val {v}"

@pytest.fixture
async def setup_app(tmp_path):
    db_file = str(tmp_path / "sanitize_test.db")
    store = SqliteStore(db_file)
    await store.init()
    await add_tenant(store, tenant_id="t-san", api_key="valid-secret-key", tokens_per_minute=1000)
    await store.aclose()

    settings = Settings(
        gateway_db_path=db_file,
        rate_limit_tokens_per_minute=1000,
        rate_limit_window_seconds=60,
        primary_timeout_ms=100,
        secondary_timeout_ms=100
    )

    def _make_app(p1, p2):
        return create_app(settings, providers=(p1, p2))

    return _make_app

@pytest.mark.asyncio
@pytest.mark.parametrize("error_case", [
    "provider_500_leak",
    "provider_400_leak",
    "provider_401_leak",
    "provider_429_leak",
    "provider_conn_error_leak",
    "provider_sql_error_leak",
    "limiter_sql_error_leak",
    "bad_request_shape",
    "unauthorized_key_leak",
    "request_too_large",
    "not_found_404",
    "method_not_allowed_405",
])
async def test_error_sanitization_all_paths(setup_app, error_case):
    make_app = setup_app

    p1 = FakeProvider("primary")
    p2 = FakeProvider("secondary")

    auth_header = "Bearer valid-secret-key"
    req_body = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 50}
    path = "/v1/chat/completions"
    method = "POST"

    expected_status = 502
    expected_code = "upstream_unavailable"

    if error_case == "provider_500_leak":
        p1.status = 500
        p1.body = {"detail": LEAKY_TEXT}
        p2.status = 500
        p2.body = {"detail": LEAKY_TEXT}
    elif error_case == "provider_400_leak":
        p1.status = 400
        p1.body = {"leak": LEAKY_TEXT}
        expected_status = 400
        expected_code = "invalid_request"
    elif error_case == "provider_401_leak":
        p1.status = 401
        p1.body = {"leak": LEAKY_TEXT}
        expected_status = 502
        expected_code = "upstream_unavailable"
    elif error_case == "provider_429_leak":
        p1.status = 429
        p1.body = {"leak": LEAKY_TEXT}
        p2.status = 429
        p2.body = {"leak": LEAKY_TEXT}
        expected_status = 502
        expected_code = "upstream_unavailable"
    elif error_case == "provider_conn_error_leak":
        p1.exception = ProviderConnectionError(LEAKY_TEXT)
        p2.exception = ProviderConnectionError(LEAKY_TEXT)
        expected_status = 502
        expected_code = "upstream_unavailable"
    elif error_case == "provider_sql_error_leak":
        p1.exception = sqlite3.OperationalError(LEAKY_TEXT)
        p2.exception = sqlite3.OperationalError(LEAKY_TEXT)
        expected_status = 502
        expected_code = "upstream_unavailable"
    elif error_case == "bad_request_shape":
        req_body = {"invalid": LEAKY_TEXT}
        expected_status = 400
        expected_code = "invalid_request"
    elif error_case == "unauthorized_key_leak":
        auth_header = f"Bearer {LEAKY_TEXT}"
        expected_status = 401
        expected_code = "unauthorized"
    elif error_case == "request_too_large":
        req_body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 5000}
        expected_status = 413
        expected_code = "request_too_large"
    elif error_case == "not_found_404":
        path = f"/non-existent/{LEAKY_TEXT[:20]}"
        method = "GET"
        expected_status = 404
        expected_code = "not_found"
    elif error_case == "method_not_allowed_405":
        method = "GET"
        expected_status = 405
        expected_code = "method_not_allowed"

    app = make_app(p1, p2)

    if error_case == "limiter_sql_error_leak":
        async def mock_reserve(*args, **kwargs):
            raise sqlite3.OperationalError(LEAKY_TEXT)
        app.state.limiter = type("MockLimiter", (), {"reserve": mock_reserve})()
        expected_status = 500
        expected_code = "internal_error"

    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers = {}
            if auth_header:
                headers["Authorization"] = auth_header

            if method == "POST":
                res = await client.post(path, json=req_body, headers=headers)
            else:
                res = await client.get(path, headers=headers)

            assert_sanitized(res, expected_status, expected_code)

@pytest.mark.asyncio
async def test_unexpected_exception_logging_no_api_key(setup_app, caplog):
    make_app = setup_app
    p1 = FakeProvider("primary")
    p2 = FakeProvider("secondary")
    app = make_app(p1, p2)

    secret_key = "sk-secret-key-12345"

    async def explode(*args, **kwargs):
        raise RuntimeError("unexpected internal failure in processing")

    async with lifespan(app):
        await add_tenant(app.state.store, tenant_id="t-key", api_key=secret_key, tokens_per_minute=1000)
        app.state.service.router.route = explode

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            with caplog.at_level(logging.ERROR):
                res = await client.post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": f"Bearer {secret_key}"}
                )

            assert res.status_code == 500
            req_id = res.headers["x-request-id"]
            data = res.json()
            assert data["error"]["code"] == "internal_error"
            assert data["error"]["request_id"] == req_id

            # Verify caplog contains traceback and request_id, but NEVER secret_key
            found_req_id = False
            for record in caplog.records:
                assert secret_key not in record.message
                if req_id in record.message:
                    found_req_id = True
                if record.exc_info:
                    formatted_tb = logging.Formatter().formatException(record.exc_info)
                    assert secret_key not in formatted_tb

            assert found_req_id, "request_id not found in server error logs"
