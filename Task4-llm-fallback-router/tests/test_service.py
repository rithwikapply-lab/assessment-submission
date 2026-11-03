import asyncio
import logging
import pytest
from llm_router.clock import SystemClock
from llm_router.config import Settings
from llm_router.errors import GatewayError
from llm_router.limiter import SlidingWindowLimiter
from llm_router.router import ModelRouter
from llm_router.service import GatewayService
from llm_router.storage import SqliteStore
from llm_router.tenants import add_tenant
from tests.helpers import FakeProvider

@pytest.fixture
async def service_setup(tmp_path):
    db_file = str(tmp_path / "service_test.db")
    store = SqliteStore(db_file)
    await store.init()

    clock = SystemClock()
    limiter = SlidingWindowLimiter(store, clock, default_limit=50_000, window_ms=60_000)
    config = Settings(
        gateway_db_path=db_file,
        primary_timeout_ms=200,
        secondary_timeout_ms=200
    )

    await add_tenant(store, tenant_id="t-1", api_key="test-key", tokens_per_minute=50_000)

    yield store, limiter, config

    await store.aclose()

@pytest.mark.asyncio
async def test_cancel_handle_mid_route_releases_reservation(service_setup):
    store, limiter, config = service_setup

    # Provider delays long enough for us to cancel mid-route
    p1 = FakeProvider(name="primary", delay=0.5)
    p2 = FakeProvider(name="secondary", delay=0.5)
    router = ModelRouter(p1, p2, config)
    service = GatewayService(store, limiter, router, config)

    body = {
        "messages": [{"role": "user", "content": "hello world"}],
        "max_tokens": 100
    }

    task = asyncio.create_task(service.handle("Bearer test-key", body, "req-cancel-1"))
    await asyncio.sleep(0.05)

    # Verify reservation exists before cancel
    def _check_reserved(conn):
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM usage WHERE tenant_id = 't-1' AND state = 'reserved';")
        return cur.fetchone()[0]

    reserved_before = await store.run(_check_reserved)
    assert reserved_before == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Verify reservation was cleaned up
    reserved_after = await store.run(_check_reserved)
    assert reserved_after == 0

    usage_snap = await limiter.usage("t-1")
    assert usage_snap.used == 0

    await router.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["both_500", "both_timeout", "unexpected_exc"])
async def test_reservation_never_stays_reserved_after_failure(service_setup, failure_mode):
    store, limiter, config = service_setup

    if failure_mode == "both_500":
        p1 = FakeProvider(name="primary", status=500)
        p2 = FakeProvider(name="secondary", status=500)
    elif failure_mode == "both_timeout":
        p1 = FakeProvider(name="primary", delay=0.3)
        p2 = FakeProvider(name="secondary", delay=0.3)
    else:
        p1 = FakeProvider(name="primary", exception=RuntimeError("unexpected boom"))
        p2 = FakeProvider(name="secondary", exception=RuntimeError("unexpected boom"))

    router = ModelRouter(p1, p2, config)
    service = GatewayService(store, limiter, router, config)

    body = {
        "messages": [{"role": "user", "content": "hello test failure"}],
        "max_tokens": 100
    }

    with pytest.raises(GatewayError):
        await service.handle("Bearer test-key", body, f"req-fail-{failure_mode}")

    def _check_no_reserved(conn):
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM usage WHERE state = 'reserved';")
        return cur.fetchone()[0]

    reserved_count = await store.run(_check_no_reserved)
    assert reserved_count == 0

    usage_snap = await limiter.usage("t-1")
    assert usage_snap.used == 0

    await router.aclose()

@pytest.mark.asyncio
async def test_key_parsing_and_unauthorized(service_setup):
    store, limiter, config = service_setup
    p1 = FakeProvider(name="primary", status=200)
    p2 = FakeProvider(name="secondary", status=200)
    router = ModelRouter(p1, p2, config)
    service = GatewayService(store, limiter, router, config)

    body = {"messages": [{"role": "user", "content": "hi"}]}

    # Missing / None
    with pytest.raises(GatewayError) as exc:
        await service.handle(None, body, "req-none")
    assert exc.value.http_status == 401

    # Empty string
    with pytest.raises(GatewayError) as exc:
        await service.handle("   ", body, "req-empty")
    assert exc.value.http_status == 401

    # Just "Bearer" or "Bearer "
    with pytest.raises(GatewayError) as exc:
        await service.handle("Bearer", body, "req-bearer")
    assert exc.value.http_status == 401

    with pytest.raises(GatewayError) as exc:
        await service.handle("Bearer   ", body, "req-bearer-space")
    assert exc.value.http_status == 401

    # Unknown key
    with pytest.raises(GatewayError) as exc:
        await service.handle("Bearer wrong-key", body, "req-wrong")
    assert exc.value.http_status == 401

    # Valid key with leading/trailing spaces around Bearer
    status, resp, headers = await service.handle("  Bearer   test-key  ", body, "req-valid")
    assert status == 200

    await router.aclose()

@pytest.mark.asyncio
async def test_second_cancel_does_not_skip_logging(service_setup, caplog):
    store, limiter, config = service_setup

    p1 = FakeProvider(name="primary", delay=0.5)
    p2 = FakeProvider(name="secondary", delay=0.5)
    router = ModelRouter(p1, p2, config)
    service = GatewayService(store, limiter, router, config)

    # Wrap limiter.release to introduce a slight delay during release
    orig_release = limiter.release
    async def slow_release(res):
        await asyncio.sleep(0.05)
        return await orig_release(res)
    limiter.release = slow_release

    body = {"messages": [{"role": "user", "content": "test double cancel"}], "max_tokens": 50}

    with caplog.at_level(logging.INFO, logger="llm_router.service"):
        task = asyncio.create_task(service.handle("Bearer test-key", body, "req-double-cancel"))
        await asyncio.sleep(0.02)
        task.cancel()  # First cancel while in route
        await asyncio.sleep(0.01)
        task.cancel()  # Second cancel while in shielded release

        with pytest.raises(asyncio.CancelledError):
            await task

    # Check that logger.info was still called despite second cancel
    log_messages = [r.message for r in caplog.records if "req-double-cancel" in r.message]
    assert len(log_messages) == 1
    assert '"outcome": "cancelled"' in log_messages[0]

    await router.aclose()
