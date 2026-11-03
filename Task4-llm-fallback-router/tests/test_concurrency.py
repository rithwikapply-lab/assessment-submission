import asyncio
import multiprocessing as mp
import pytest

from llm_router.clock import SystemClock
from llm_router.config import Settings
from llm_router.limiter import RateLimited, SlidingWindowLimiter
from llm_router.router import ModelRouter
from llm_router.service import GatewayService
from llm_router.storage import SqliteStore
from llm_router.tenants import add_tenant
from tests.helpers import FakeProvider, mp_reserve_worker

@pytest.mark.asyncio
async def test_200_concurrent_reserves_admit_exactly_50(tmp_path):
    db_file = str(tmp_path / "concurrent_reserve.db")
    store = SqliteStore(db_file)
    await store.init()

    clock = SystemClock()
    limiter = SlidingWindowLimiter(store, clock, default_limit=50_000, window_ms=60_000)

    tenant_id = "t-conc"
    tokens_per_req = 1000
    limit = 50_000
    num_requests = 200

    async def _try_reserve():
        try:
            res = await limiter.reserve(tenant_id, tokens_per_req, limit=limit)
            return True, res
        except RateLimited:
            return False, None

    results = await asyncio.gather(*[_try_reserve() for _ in range(num_requests)])

    admitted = [r for success, r in results if success]
    rate_limited = [r for success, r in results if not success]

    assert len(admitted) == 50
    assert len(rate_limited) == 150

    def _get_sum(conn):
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE tenant_id = ?;", (tenant_id,))
        return cur.fetchone()[0]

    total_tokens_in_db = await store.run(_get_sum)
    assert total_tokens_in_db == 50_000

    await store.aclose()

@pytest.mark.asyncio
async def test_multiprocess_concurrency(tmp_path):
    db_file = str(tmp_path / "mp_conc.db")
    store = SqliteStore(db_file)
    await store.init()
    await store.aclose()

    tenant_id = "t-mp"
    limit = 50_000
    tokens_per_attempt = 1000
    attempts_per_proc = 25
    num_procs = 4

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = []

    for _ in range(num_procs):
        p = ctx.Process(
            target=mp_reserve_worker,
            args=(db_file, tenant_id, limit, attempts_per_proc, tokens_per_attempt, q)
        )
        p.start()
        procs.append(p)

    for p in procs:
        p.join(timeout=30)
        assert not p.is_alive(), "Process did not terminate in time"

    worker_results = [q.get(timeout=5) for _ in range(num_procs)]
    total_admitted = sum(r[0] for r in worker_results)
    total_limited = sum(r[1] for r in worker_results)

    assert total_admitted == 50
    assert total_limited == 50

    store2 = SqliteStore(db_file)
    await store2.init()

    def _get_sum(conn):
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE tenant_id = ?;", (tenant_id,))
        return cur.fetchone()[0]

    sum_in_db = await store2.run(_get_sum)
    assert sum_in_db == 50_000
    await store2.aclose()

@pytest.mark.asyncio
async def test_100_concurrent_requests_through_service(tmp_path):
    db_file = str(tmp_path / "svc_100_conc.db")
    store = SqliteStore(db_file)
    await store.init()

    tenant_id = "t-svc"
    api_key = "svc-key"
    limit = 500_000  # High limit so all 100 are admitted
    await add_tenant(store, tenant_id=tenant_id, api_key=api_key, tokens_per_minute=limit)

    clock = SystemClock()
    limiter = SlidingWindowLimiter(store, clock, default_limit=limit, window_ms=60_000)

    # Fake provider charges exactly 10 tokens in usage
    tokens_per_response = 10
    p1 = FakeProvider(
        name="primary",
        status=200,
        body={"choices": [{"message": {"role": "assistant", "content": "fast"}}], "usage": {"total_tokens": tokens_per_response}}
    )
    p2 = FakeProvider(name="secondary", status=200)

    config = Settings(
        gateway_db_path=db_file,
        rate_limit_tokens_per_minute=limit,
        primary_timeout_ms=500,
        secondary_timeout_ms=500
    )
    router = ModelRouter(p1, p2, config)
    service = GatewayService(store, limiter, router, config)

    num_requests = 100
    body = {"messages": [{"role": "user", "content": "hello concurrency"}], "max_tokens": 50}

    async def _send_req(i: int):
        status, resp, headers = await service.handle(f"Bearer {api_key}", body, f"req-conc-{i}")
        assert status == 200
        return status

    statuses = await asyncio.gather(*[_send_req(i) for i in range(num_requests)])
    assert len(statuses) == num_requests
    assert all(s == 200 for s in statuses)

    # Verify usage afterwards equals the sum of actual tokens
    expected_usage = num_requests * tokens_per_response
    usage_snap = await limiter.usage(tenant_id, limit)
    assert usage_snap.used == expected_usage

    # Verify no row is left in 'reserved'
    def _check_no_reserved(conn):
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM usage WHERE tenant_id = ? AND state = 'reserved';", (tenant_id,))
        reserved = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM usage WHERE tenant_id = ? AND state = 'committed';", (tenant_id,))
        committed = cur.fetchone()[0]
        return reserved, committed

    reserved_count, committed_count = await store.run(_check_no_reserved)
    assert reserved_count == 0
    assert committed_count == num_requests

    await router.aclose()
    await store.aclose()
