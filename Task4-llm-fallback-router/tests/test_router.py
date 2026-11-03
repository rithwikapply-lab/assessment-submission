import asyncio
import time
import pytest
from llm_router.config import Settings
from llm_router.errors import GatewayError
from llm_router.providers import ProviderRequest, ProviderResult, ProviderConnectionError
from llm_router.router import ModelRouter
from tests.helpers import FakeProvider

@pytest.fixture
def default_config():
    return Settings(
        primary_timeout_ms=100,
        secondary_timeout_ms=100
    )

@pytest.mark.asyncio
async def test_primary_success(default_config):
    p1 = FakeProvider(name="primary", status=200, body={"choices": ["test1"]})
    p2 = FakeProvider(name="secondary", status=200, body={"choices": ["test2"]})
    router = ModelRouter(p1, p2, default_config)

    req = ProviderRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=10)
    res = await router.route(req)

    assert res.provider_name == "primary"
    assert res.body == {"choices": ["test1"]}
    assert not res.fallback_used
    assert p1.call_count == 1
    assert p2.call_count == 0
    await router.aclose()

@pytest.mark.asyncio
async def test_primary_429_fallback(default_config):
    p1 = FakeProvider(name="primary", status=429, body={"error": "rate limit"})
    p2 = FakeProvider(name="secondary", status=200, body={"choices": ["from secondary"]})
    router = ModelRouter(p1, p2, default_config)

    req = ProviderRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=10)
    res = await router.route(req)

    assert res.provider_name == "secondary"
    assert res.fallback_used
    assert p1.call_count == 1
    assert p2.call_count == 1
    await router.aclose()

@pytest.mark.asyncio
async def test_primary_timeout_fallback(default_config):
    # Primary takes 0.5s, deadline is 0.1s (100ms)
    p1 = FakeProvider(name="primary", delay=0.5, body={"choices": ["slow"]})
    p2 = FakeProvider(name="secondary", delay=0.01, body={"choices": ["fast secondary"]})
    router = ModelRouter(p1, p2, default_config)

    start = time.perf_counter()
    req = ProviderRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=10)
    res = await router.route(req)
    elapsed = time.perf_counter() - start

    assert res.provider_name == "secondary"
    assert res.fallback_used
    assert elapsed < 0.35  # Should not wait for p1's 0.5s
    assert p1.cancelled is True
    await router.aclose()

@pytest.mark.asyncio
async def test_server_error_and_connection_error_fallback(default_config):
    # 500 error
    p1 = FakeProvider(name="primary", status=500)
    p2 = FakeProvider(name="secondary", status=200, body={"ok": 1})
    router = ModelRouter(p1, p2, default_config)
    req = ProviderRequest(messages=[], max_tokens=10)
    res = await router.route(req)
    assert res.provider_name == "secondary"
    await router.aclose()

    # Connection error
    p1 = FakeProvider(name="primary", exception=ProviderConnectionError("network down"))
    p2 = FakeProvider(name="secondary", status=200, body={"ok": 2})
    router = ModelRouter(p1, p2, default_config)
    res = await router.route(req)
    assert res.provider_name == "secondary"
    await router.aclose()

@pytest.mark.asyncio
async def test_both_fail_gives_502(default_config):
    p1 = FakeProvider(name="primary", status=500)
    p2 = FakeProvider(name="secondary", status=503)
    router = ModelRouter(p1, p2, default_config)
    req = ProviderRequest(messages=[], max_tokens=10)
    with pytest.raises(GatewayError) as exc:
        await router.route(req)
    assert exc.value.http_status == 502
    assert exc.value.code == "upstream_unavailable"
    await router.aclose()

@pytest.mark.asyncio
async def test_both_timeout_gives_504(default_config):
    p1 = FakeProvider(name="primary", delay=0.2)
    p2 = FakeProvider(name="secondary", delay=0.2)
    router = ModelRouter(p1, p2, default_config)
    req = ProviderRequest(messages=[], max_tokens=10)
    with pytest.raises(GatewayError) as exc:
        await router.route(req)
    assert exc.value.http_status == 504
    assert exc.value.code == "upstream_timeout"
    await router.aclose()

@pytest.mark.asyncio
async def test_mixed_429_and_timeout(default_config):
    p1 = FakeProvider(name="primary", status=429)
    p2 = FakeProvider(name="secondary", delay=0.2)
    router = ModelRouter(p1, p2, default_config)
    req = ProviderRequest(messages=[], max_tokens=10)
    with pytest.raises(GatewayError) as exc:
        await router.route(req)
    assert exc.value.http_status == 502
    assert exc.value.code == "upstream_unavailable"
    await router.aclose()

@pytest.mark.asyncio
async def test_upstream_400_and_401_no_failover(default_config):
    # 400 Bad Request
    p1 = FakeProvider(name="primary", status=400, body={"secret": "leak"})
    p2 = FakeProvider(name="secondary", status=200)
    router = ModelRouter(p1, p2, default_config)
    req = ProviderRequest(messages=[], max_tokens=10)
    with pytest.raises(GatewayError) as exc:
        await router.route(req)
    assert exc.value.http_status == 400
    assert exc.value.code == "invalid_request"
    assert p2.call_count == 0

    # 401 Unauthorized from upstream
    p1_401 = FakeProvider(name="primary", status=401, body={"secret": "leak"})
    router2 = ModelRouter(p1_401, p2, default_config)
    with pytest.raises(GatewayError) as exc:
        await router2.route(req)
    assert exc.value.http_status == 502
    assert exc.value.code == "upstream_unavailable"
    assert p2.call_count == 0
    await router.aclose()
    await router2.aclose()

@pytest.mark.asyncio
async def test_provider_ignoring_cancellation(default_config):
    # Provider ignores cancel and sleeps longer
    p1 = FakeProvider(name="primary", delay=0.3, ignore_cancel=True)
    p2 = FakeProvider(name="secondary", delay=0.01, body={"msg": "success"})
    router = ModelRouter(p1, p2, default_config)

    start = time.perf_counter()
    req = ProviderRequest(messages=[], max_tokens=10)
    res = await router.route(req)
    elapsed = time.perf_counter() - start

    assert res.provider_name == "secondary"
    # Even though p1 was sleeping 0.3s ignoring cancel, router moved on at 0.1s
    assert elapsed < 0.25
    await router.aclose()

@pytest.mark.asyncio
async def test_late_result_discarded(default_config):
    # Primary finishes slightly after timeout
    p1 = FakeProvider(name="primary", delay=0.15, body={"msg": "late primary"})
    p2 = FakeProvider(name="secondary", delay=0.05, body={"msg": "secondary"})
    router = ModelRouter(p1, p2, default_config)

    req = ProviderRequest(messages=[], max_tokens=10)
    res = await router.route(req)

    assert res.provider_name == "secondary"
    assert res.body == {"msg": "secondary"}
    assert p1.call_count == 1
    assert p2.call_count == 1
    await asyncio.sleep(0.1) # Wait for p1 task to complete
    await router.aclose()

@pytest.mark.asyncio
async def test_cancelling_route_cancels_provider(default_config):
    p1 = FakeProvider(name="primary", delay=1.0)
    p2 = FakeProvider(name="secondary", delay=1.0)
    router = ModelRouter(p1, p2, default_config)

    req = ProviderRequest(messages=[], max_tokens=10)
    task = asyncio.create_task(router.route(req))
    await asyncio.sleep(0.02)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert p1.cancelled is True
    await router.aclose()
    assert len(router._abandoned) == 0

@pytest.mark.asyncio
async def test_deadline_race_property():
    unhandled_exceptions = []
    loop = asyncio.get_running_loop()
    def custom_handler(loop, context):
        unhandled_exceptions.append(context)

    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(custom_handler)

    try:
        import random
        # 60 iterations with random jitter around deadline
        deadline_ms = 50
        for i in range(60):
            jitter = random.uniform(-0.015, 0.015)
            delay = (deadline_ms / 1000.0) + jitter
            p1 = FakeProvider(name="primary", delay=max(0.001, delay), body={"msg": "from p1"})
            p2 = FakeProvider(name="secondary", delay=0.01, body={"msg": "from p2"})
            cfg = Settings(primary_timeout_ms=deadline_ms, secondary_timeout_ms=deadline_ms)
            router = ModelRouter(p1, p2, cfg)

            req = ProviderRequest(messages=[], max_tokens=10)
            res = await router.route(req)

            assert res is not None
            assert res.provider_name in ("primary", "secondary")
            assert p1.call_count == 1
            if res.provider_name == "primary":
                assert res.fallback_used is False
                assert p2.call_count == 0
                assert res.attempts[0].outcome == "success"
            else:
                assert res.fallback_used is True
                assert p2.call_count == 1
                assert res.attempts[0].outcome == "timeout"
            await router.aclose()
            # Give brief tick for any pending callbacks
            await asyncio.sleep(0.005)

        assert len(unhandled_exceptions) == 0
    finally:
        loop.set_exception_handler(old_handler)


@pytest.mark.asyncio
async def test_primary_2xx_unreadable_body_fallback(default_config):
    p1 = FakeProvider(name="primary", status=200, body=None)
    p2 = FakeProvider(name="secondary", status=200, body={"choices": ["from secondary"]})
    router = ModelRouter(p1, p2, default_config)

    req = ProviderRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=10)
    res = await router.route(req)

    assert res.provider_name == "secondary"
    assert res.fallback_used is True
    assert res.body == {"choices": ["from secondary"]}
    assert p1.call_count == 1
    assert p2.call_count == 1
    await router.aclose()


@pytest.mark.parametrize("st", [429, 500, 502, 503, 504])
@pytest.mark.asyncio
async def test_explicit_failover_statuses(default_config, st):
    p1 = FakeProvider(name="primary", status=st, body={"error": f"fail_{st}"})
    p2 = FakeProvider(name="secondary", status=200, body={"choices": [f"fallback_{st}"]})
    router = ModelRouter(p1, p2, default_config)

    req = ProviderRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=10)
    res = await router.route(req)

    assert res.provider_name == "secondary"
    assert res.fallback_used is True
    assert res.body == {"choices": [f"fallback_{st}"]}
    assert p1.call_count == 1
    assert p2.call_count == 1
    await router.aclose()


@pytest.mark.parametrize("unknown_st", [301, 302, 401, 403, 404, 501])
@pytest.mark.asyncio
async def test_unknown_statuses_no_failover(default_config, unknown_st):
    p1 = FakeProvider(name="primary", status=unknown_st, body={"msg": "unknown"})
    p2 = FakeProvider(name="secondary", status=200, body={"choices": ["sec"]})
    router = ModelRouter(p1, p2, default_config)

    req = ProviderRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=10)
    with pytest.raises(GatewayError) as exc:
        await router.route(req)

    assert exc.value.http_status == 502
    assert exc.value.code == "upstream_unavailable"
    assert p1.call_count == 1
    assert p2.call_count == 0
    await router.aclose()


@pytest.mark.asyncio
async def test_primary_unexpected_exception_no_failover(default_config):
    p1 = FakeProvider(name="primary", exception=RuntimeError("catastrophic failure"))
    p2 = FakeProvider(name="secondary", status=200, body={"choices": ["sec"]})
    router = ModelRouter(p1, p2, default_config)

    req = ProviderRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=10)
    with pytest.raises(GatewayError) as exc:
        await router.route(req)

    assert exc.value.http_status == 502
    assert exc.value.code == "upstream_unavailable"
    assert p1.call_count == 1
    assert p2.call_count == 0
    await router.aclose()


@pytest.mark.asyncio
async def test_primary_422_no_failover(default_config):
    p1 = FakeProvider(name="primary", status=422, body={"detail": "unprocessable"})
    p2 = FakeProvider(name="secondary", status=200, body={"choices": ["sec"]})
    router = ModelRouter(p1, p2, default_config)

    req = ProviderRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=10)
    with pytest.raises(GatewayError) as exc:
        await router.route(req)

    assert exc.value.http_status == 400
    assert exc.value.code == "invalid_request"
    assert p1.call_count == 1
    assert p2.call_count == 0
    await router.aclose()
