#!/usr/bin/env python3
import asyncio
import os
import sys
import tempfile
import time
import logging
logging.getLogger("llm_router.service").setLevel(logging.WARNING)

# Ensure src is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from llm_router.clock import SystemClock
from llm_router.config import Settings
from llm_router.errors import GatewayError
from llm_router.limiter import SlidingWindowLimiter
from llm_router.router import ModelRouter
from llm_router.service import GatewayService
from llm_router.storage import SqliteStore
from llm_router.tenants import add_tenant
from tests.helpers import FakeProvider
logging.getLogger("llm_router.service").setLevel(logging.WARNING)

async def run_demo():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "demo_gateway.db")
        store = SqliteStore(db_path)
        await store.init()

        tenant_id = "demo-tenant"
        api_key = "demo-key"
        rate_limit = 200
        await add_tenant(store, tenant_id=tenant_id, api_key=api_key, tokens_per_minute=rate_limit)

        clock = SystemClock()
        limiter = SlidingWindowLimiter(store, clock, default_limit=rate_limit, window_ms=60_000)

        config = Settings(
            gateway_db_path=db_path,
            rate_limit_tokens_per_minute=rate_limit,
            primary_timeout_ms=300,
            secondary_timeout_ms=300
        )

        rows = []

        # 1. Normal request
        p1 = FakeProvider(
            name="primary",
            status=200,
            body={"choices": [{"message": {"role": "assistant", "content": "Primary ok"}}], "usage": {"total_tokens": 25}}
        )
        p2 = FakeProvider(name="secondary", status=200)
        router = ModelRouter(p1, p2, config)
        service = GatewayService(store, limiter, router, config)

        t0 = time.perf_counter()
        status, body, headers = await service.handle(f"Bearer {api_key}", {"messages": [{"role": "user", "content": "Hi"}], "max_tokens": 20}, "req-1")
        ms = round((time.perf_counter() - t0) * 1000, 1)
        snap = await limiter.usage(tenant_id, rate_limit)
        rows.append((1, "Normal request", f"{status} OK", headers["X-Gateway-Fallback"], 25, f"{snap.used} / {rate_limit}", f"{ms}ms", "Primary served"))
        await router.aclose()

        # 2. Primary 429 -> fallback
        p1 = FakeProvider(name="primary", status=429, body={"error": "rate limited"})
        p2 = FakeProvider(
            name="secondary",
            status=200,
            body={"choices": [{"message": {"role": "assistant", "content": "Secondary ok"}}], "usage": {"total_tokens": 25}}
        )
        router = ModelRouter(p1, p2, config)
        service = GatewayService(store, limiter, router, config)

        t0 = time.perf_counter()
        status, body, headers = await service.handle(f"Bearer {api_key}", {"messages": [{"role": "user", "content": "Hi"}], "max_tokens": 20}, "req-2")
        ms = round((time.perf_counter() - t0) * 1000, 1)
        snap = await limiter.usage(tenant_id, rate_limit)
        rows.append((2, "Primary 429 -> fallback", f"{status} OK", headers["X-Gateway-Fallback"], 25, f"{snap.used} / {rate_limit}", f"{ms}ms", "Secondary served"))
        await router.aclose()

        # 3. Primary timeout -> fallback
        p1 = FakeProvider(name="primary", delay=0.6)
        p2 = FakeProvider(
            name="secondary",
            status=200,
            body={"choices": [{"message": {"role": "assistant", "content": "Secondary ok"}}], "usage": {"total_tokens": 25}}
        )
        router = ModelRouter(p1, p2, config)
        service = GatewayService(store, limiter, router, config)

        t0 = time.perf_counter()
        status, body, headers = await service.handle(f"Bearer {api_key}", {"messages": [{"role": "user", "content": "Hi"}], "max_tokens": 20}, "req-3")
        ms = round((time.perf_counter() - t0) * 1000, 1)
        snap = await limiter.usage(tenant_id, rate_limit)
        rows.append((3, "Primary timeout -> fallback", f"{status} OK", headers["X-Gateway-Fallback"], 25, f"{snap.used} / {rate_limit}", f"{ms}ms", "Timeout 300ms, Sec served"))
        await router.aclose()

        # 4. Both failing -> 502
        p1 = FakeProvider(name="primary", status=500)
        p2 = FakeProvider(name="secondary", status=503)
        router = ModelRouter(p1, p2, config)
        service = GatewayService(store, limiter, router, config)

        t0 = time.perf_counter()
        status_text = "500"
        try:
            await service.handle(f"Bearer {api_key}", {"messages": [{"role": "user", "content": "Hi"}], "max_tokens": 20}, "req-4")
        except GatewayError as e:
            status_text = f"{e.http_status} Error"
        ms = round((time.perf_counter() - t0) * 1000, 1)
        snap = await limiter.usage(tenant_id, rate_limit)
        rows.append((4, "Both failing -> 502", status_text, "N/A", 0, f"{snap.used} / {rate_limit}", f"{ms}ms", "Upstream unavailable"))
        await router.aclose()

        # 5. Rate limit hit -> 429 with retry-after
        p1 = FakeProvider(name="primary", status=200)
        p2 = FakeProvider(name="secondary", status=200)
        router = ModelRouter(p1, p2, config)
        service = GatewayService(store, limiter, router, config)

        t0 = time.perf_counter()
        status_text = "200"
        retry_msg = ""
        try:
            await service.handle(f"Bearer {api_key}", {"messages": [{"role": "user", "content": "Hi"}], "max_tokens": 150}, "req-5")
        except GatewayError as e:
            status_text = f"{e.http_status} Error"
            retry_msg = f"Retry-After: {e.retry_after_s}s"
        ms = round((time.perf_counter() - t0) * 1000, 1)
        snap = await limiter.usage(tenant_id, rate_limit)
        rows.append((5, "Rate limit hit -> 429", status_text, "N/A", 0, f"{snap.used} / {rate_limit}", f"{ms}ms", retry_msg))
        await router.aclose()

        await store.aclose()

        print("")
        print("=== LLM Gateway Router Scenario Timeline ===")
        line = "+----+-----------------------------+-----------+----------+--------+------------+---------+---------------------------+"
        print(line)
        print(f"| {"#":<2} | {"Scenario":<27} | {"Status":<9} | {"Fallback":<8} | {"Tokens":<6} | {"Total Used":<10} | {"Latency":<7} | {"Detail":<25} |")
        print(line)
        for r in rows:
            print(f"| {r[0]:<2} | {r[1]:<27} | {r[2]:<9} | {r[3]:<8} | {r[4]:<6} | {r[5]:<10} | {r[6]:<7} | {r[7]:<25} |")
        print(line)
        print("Demo completed successfully against temporary on-disk SQLite.\n")

if __name__ == "__main__":
    asyncio.run(run_demo())
