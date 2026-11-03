import pytest
import sqlite3
import math
from llm_router.clock import ManualClock
from llm_router.storage import SqliteStore
from llm_router.limiter import SlidingWindowLimiter, RateLimited, RequestTooLarge
from llm_router.tenants import add_tenant

@pytest.fixture
async def store(tmp_path):
    db_file = str(tmp_path / "test_limiter.db")
    s = SqliteStore(db_file)
    await s.init()
    yield s
    await s.aclose()

@pytest.mark.asyncio
async def test_admit_up_to_limit_and_reject(store):
    clock = ManualClock(1000)
    limiter = SlidingWindowLimiter(store, clock, default_limit=50000, window_ms=60000)

    # Reserve 30k
    r1 = await limiter.reserve("tenant-1", 30000)
    assert r1 is not None

    # Reserve another 20k -> exactly at 50k
    r2 = await limiter.reserve("tenant-1", 20000)
    assert r2 is not None

    # Next 1k should be rejected
    with pytest.raises(RateLimited) as exc_info:
        await limiter.reserve("tenant-1", 1000)
    assert exc_info.value.retry_after_ms > 0

@pytest.mark.asyncio
async def test_per_tenant_isolation(store):
    clock = ManualClock(1000)
    limiter = SlidingWindowLimiter(store, clock, default_limit=50000, window_ms=60000)

    # Fill tenant-1
    await limiter.reserve("tenant-1", 50000)
    with pytest.raises(RateLimited):
        await limiter.reserve("tenant-1", 100)

    # tenant-2 should still have full quota
    r_t2 = await limiter.reserve("tenant-2", 40000)
    assert r_t2 is not None

    snap1 = await limiter.usage("tenant-1")
    assert snap1.used == 50000
    assert snap1.remaining == 0

    snap2 = await limiter.usage("tenant-2")
    assert snap2.used == 40000
    assert snap2.remaining == 10000

@pytest.mark.asyncio
async def test_per_tenant_override_limit(store):
    clock = ManualClock(1000)
    limiter = SlidingWindowLimiter(store, clock, default_limit=50000, window_ms=60000)

    # Custom limit 100k
    r1 = await limiter.reserve("vip-tenant", 60000, limit=100000)
    assert r1 is not None
    # Custom lower limit 20k
    with pytest.raises(RequestTooLarge):
        await limiter.reserve("budget-tenant", 25000, limit=20000)

@pytest.mark.asyncio
async def test_exact_boundary_conditions(store):
    # reserve 30_000 at t=0 and 20_000 at t=30_000 (limit 50_000, window 60_000)
    # At t=59_999 a 1_000 request is rejected with retry_after_ms == 1
    # At t=60_000 it is admitted (first row expired, boundary is expired)
    clock = ManualClock(0)
    limiter = SlidingWindowLimiter(store, clock, default_limit=50000, window_ms=60000)

    await limiter.reserve("t1", 30000)
    
    clock.set(30000)
    await limiter.reserve("t1", 20000)

    clock.set(59999)
    with pytest.raises(RateLimited) as exc:
        await limiter.reserve("t1", 1000)
    assert exc.value.retry_after_ms == 1

    clock.set(60000)
    # At t=60000, row at t=0 has ts_ms <= 60000 - 60000 (0 <= 0), so it's expired!
    # Remaining used is 20000, 20000 + 1000 <= 50000 -> Admitted
    r = await limiter.reserve("t1", 1000)
    assert r is not None

@pytest.mark.asyncio
async def test_retry_after_walk_logic(store):
    clock = ManualClock(1000)
    limiter = SlidingWindowLimiter(store, clock, default_limit=100, window_ms=60000)

    # Insert multiple rows: t=1000: 40 tokens; t=2000: 30 tokens; t=3000: 30 tokens (total 100)
    await limiter.reserve("t1", 40)
    clock.set(2000)
    await limiter.reserve("t1", 30)
    clock.set(3000)
    await limiter.reserve("t1", 30)

    # Now need 50 tokens at t=4000: total currently 100, 100 + 50 - 100 = need 50.
    # Walking: row1(40) -> 40 < 50; row2(30) -> 70 >= 50.
    # Expiry of row2 is 2000 + 60000 = 62000.
    # retry_after_ms = 62000 - 4000 = 58000.
    clock.set(4000)
    with pytest.raises(RateLimited) as exc:
        await limiter.reserve("t1", 50)
    assert exc.value.retry_after_ms == 58000

@pytest.mark.asyncio
async def test_request_too_large(store):
    clock = ManualClock(0)
    limiter = SlidingWindowLimiter(store, clock, default_limit=50000, window_ms=60000)

    with pytest.raises(RequestTooLarge):
        await limiter.reserve("t1", 50001)

    # Check that nothing was written to db
    def check_count(conn):
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM usage WHERE tenant_id = 't1';")
        return cur.fetchone()[0]

    count = await store.run(check_count)
    assert count == 0

@pytest.mark.asyncio
async def test_eviction_mechanisms(store):
    clock = ManualClock(0)
    limiter = SlidingWindowLimiter(store, clock, default_limit=50000, window_ms=60000)

    await limiter.reserve("t1", 10000)
    await limiter.reserve("t2", 10000)

    # Advance clock past window
    clock.set(70000)

    # Opportunistic eviction on next reserve
    await limiter.reserve("t1", 5000)

    def check_t1(conn):
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM usage WHERE tenant_id = 't1';")
        return cur.fetchone()[0]

    count_t1 = await store.run(check_t1)
    assert count_t1 == 1  # Old t1 record deleted, only new 5000 remains

    # purge_expired should clean up other tenants as well
    deleted = await limiter.purge_expired()
    assert deleted == 1  # t2's record got deleted

    def check_all(conn):
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM usage;")
        return cur.fetchone()[0]

    total_count = await store.run(check_all)
    assert total_count == 1

@pytest.mark.asyncio
async def test_commit_and_release(store):
    clock = ManualClock(0)
    limiter = SlidingWindowLimiter(store, clock, default_limit=50000, window_ms=60000)

    # Commit reconciles down (refund difference)
    res1 = await limiter.reserve("t1", 10000)
    await limiter.commit(res1, 6000)
    snap = await limiter.usage("t1")
    assert snap.used == 6000

    # Commit is idempotent
    await limiter.commit(res1, 6000)
    snap = await limiter.usage("t1")
    assert snap.used == 6000

    # Release does not delete committed row
    await limiter.release(res1)
    snap = await limiter.usage("t1")
    assert snap.used == 6000

    # Commit reconciles up (overdraft)
    res2 = await limiter.reserve("t1", 40000)
    await limiter.commit(res2, 50000) # used is now 6000 + 50000 = 56000 > 50000
    snap = await limiter.usage("t1")
    assert snap.used == 56000
    assert snap.remaining == 0
    with pytest.raises(RateLimited):
        await limiter.reserve("t1", 1)

    # Release uncommitted reservation
    clock.set(100000) # Reset window
    res3 = await limiter.reserve("t1", 20000)
    snap = await limiter.usage("t1")
    assert snap.used == 20000
    await limiter.release(res3)
    snap = await limiter.usage("t1")
    assert snap.used == 0

    # Release is idempotent
    await limiter.release(res3)
    assert (await limiter.usage("t1")).used == 0

    # Commit after eviction inserts a committed row
    clock.set(200000)
    res4 = await limiter.reserve("t1", 10000)
    clock.set(300000) # Eviction time past window
    # Trigger eviction
    await limiter.purge_expired()
    # Now commit res4
    await limiter.commit(res4, 8000)
    snap = await limiter.usage("t1")
    assert snap.used == 8000

@pytest.mark.asyncio
async def test_persistence_and_wal(tmp_path):
    db_file = str(tmp_path / "wal_test.db")
    store = SqliteStore(db_file)
    await store.init()
    clock = ManualClock(1000)
    limiter = SlidingWindowLimiter(store, clock, default_limit=50000, window_ms=60000)

    res = await limiter.reserve("t1", 15000)
    await limiter.commit(res, 15000)
    await store.aclose()

    # Reopen store
    store2 = SqliteStore(db_file)
    await store2.init()
    limiter2 = SlidingWindowLimiter(store2, clock, default_limit=50000, window_ms=60000)
    snap = await limiter2.usage("t1")
    assert snap.used == 15000
    await store2.aclose()

@pytest.mark.asyncio
async def test_sql_injection_defense(store):
    clock = ManualClock(1000)
    limiter = SlidingWindowLimiter(store, clock)
    malicious_id = "tenant'; DROP TABLE usage; --"

    res = await limiter.reserve(malicious_id, 1000)
    await limiter.commit(res, 1000)
    snap = await limiter.usage(malicious_id)
    assert snap.used == 1000

    def check_table(conn):
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='usage';")
        return cur.fetchone()

    assert (await store.run(check_table)) is not None
