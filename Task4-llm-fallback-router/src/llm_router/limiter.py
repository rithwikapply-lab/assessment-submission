import math
import uuid
import sqlite3
from dataclasses import dataclass
from typing import Optional, Union
from llm_router.clock import Clock
from llm_router.storage import SqliteStore

class RateLimited(Exception):
    def __init__(self, retry_after_ms: int):
        self.retry_after_ms = max(1, retry_after_ms)
        super().__init__(f"Rate limit exceeded. Retry after {self.retry_after_ms}ms")

class RequestTooLarge(Exception):
    def __init__(self, message: str = "Request too large"):
        super().__init__(message)

@dataclass
class Reservation:
    id: str
    tenant_id: str
    tokens: int
    ts_ms: int

@dataclass
class UsageSnapshot:
    used: int
    limit: int
    remaining: int
    reset_after_ms: int

class SlidingWindowLimiter:
    def __init__(self, store: SqliteStore, clock: Clock, default_limit: int = 50_000, window_ms: int = 60_000):
        self.store = store
        self.clock = clock
        self.default_limit = default_limit
        self.window_ms = window_ms
        self._reservation_tenant_cache: dict[str, str] = {}

    async def reserve(self, tenant_id: str, estimated_tokens: int, limit: Optional[int] = None) -> Reservation:
        effective_limit = limit if limit is not None else self.default_limit

        def _tx(conn: sqlite3.Connection):
            conn.execute("BEGIN IMMEDIATE;")
            try:
                now = self.clock.now_ms()
                cutoff = now - self.window_ms
                # 2. Opportunistic eviction in the same transaction
                conn.execute(
                    "DELETE FROM usage WHERE tenant_id = ? AND ts_ms <= ?;",
                    (tenant_id, cutoff)
                )

                # 3. If estimated_tokens > limit: COMMIT and raise RequestTooLarge
                if estimated_tokens > effective_limit:
                    conn.execute("COMMIT;")
                    raise RequestTooLarge("Requested tokens exceed limit")

                # 4. used = SUM(tokens) for the tenant
                cur = conn.cursor()
                cur.execute(
                    "SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE tenant_id = ? AND ts_ms > ?;",
                    (tenant_id, cutoff)
                )
                used = cur.fetchone()[0]

                if used + estimated_tokens <= effective_limit:
                    res_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO usage (id, tenant_id, ts_ms, tokens, state) VALUES (?, ?, ?, ?, 'reserved');",
                        (res_id, tenant_id, now, estimated_tokens)
                    )
                    conn.execute("COMMIT;")
                    return Reservation(id=res_id, tenant_id=tenant_id, tokens=estimated_tokens, ts_ms=now)

                # 5. Otherwise compute retry_after_ms exactly
                need = (used + estimated_tokens) - effective_limit
                cur.execute(
                    "SELECT id, ts_ms, tokens FROM usage WHERE tenant_id = ? AND ts_ms > ? ORDER BY ts_ms ASC, id ASC;",
                    (tenant_id, cutoff)
                )
                rows = cur.fetchall()
                accumulated = 0
                retry_at = now + self.window_ms
                for r in rows:
                    accumulated += r["tokens"]
                    if accumulated >= need:
                        retry_at = r["ts_ms"] + self.window_ms
                        break

                retry_after_ms = max(1, retry_at - now)
                conn.execute("COMMIT;")
                raise RateLimited(retry_after_ms)
            except Exception:
                # If transaction was not committed, rollback
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
                raise

        res = await self.store.run(_tx)
        if isinstance(res, Reservation):
            self._reservation_tenant_cache[res.id] = res.tenant_id
        return res

    async def commit(self, reservation_or_id: Union[str, Reservation], actual_tokens: int) -> None:
        if isinstance(reservation_or_id, Reservation):
            res_id = reservation_or_id.id
            t_id = reservation_or_id.tenant_id
        else:
            res_id = reservation_or_id
            t_id = self._reservation_tenant_cache.get(res_id)

        now = self.clock.now_ms()

        def _tx(conn: sqlite3.Connection):
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur = conn.cursor()
                cur.execute("SELECT tenant_id, state, ts_ms FROM usage WHERE id = ?;", (res_id,))
                row = cur.fetchone()

                if row is not None:
                    if row["state"] == "reserved":
                        cur.execute(
                            "UPDATE usage SET tokens = ?, state = 'committed' WHERE id = ? AND state = 'reserved';",
                            (actual_tokens, res_id)
                        )
                    # If already 'committed', idempotent no-op
                else:
                    # Row was evicted
                    tenant = t_id
                    if tenant:
                        cur.execute(
                            "INSERT INTO usage (id, tenant_id, ts_ms, tokens, state) VALUES (?, ?, ?, ?, 'committed');",
                            (res_id, tenant, now, actual_tokens)
                        )
                conn.execute("COMMIT;")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
                raise

        await self.store.run(_tx)
        self._reservation_tenant_cache.pop(res_id, None)

    async def release(self, reservation_or_id: Union[str, Reservation]) -> None:
        if isinstance(reservation_or_id, Reservation):
            res_id = reservation_or_id.id
        else:
            res_id = reservation_or_id

        def _tx(conn: sqlite3.Connection):
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute("DELETE FROM usage WHERE id = ? AND state = 'reserved';", (res_id,))
                conn.execute("COMMIT;")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
                raise

        await self.store.run(_tx)
        self._reservation_tenant_cache.pop(res_id, None)

    async def usage(self, tenant_id: str, limit: Optional[int] = None) -> UsageSnapshot:
        effective_limit = limit if limit is not None else self.default_limit

        def _query(conn: sqlite3.Connection):
            conn.execute("BEGIN IMMEDIATE;")
            try:
                now = self.clock.now_ms()
                cutoff = now - self.window_ms
                conn.execute("DELETE FROM usage WHERE tenant_id = ? AND ts_ms <= ?;", (tenant_id, cutoff))
                cur = conn.cursor()
                cur.execute(
                    "SELECT COALESCE(SUM(tokens), 0), MIN(ts_ms) FROM usage WHERE tenant_id = ? AND ts_ms > ?;",
                    (tenant_id, cutoff)
                )
                row = cur.fetchone()
                used = row[0]
                oldest_ts = row[1]
                conn.execute("COMMIT;")

                remaining = max(0, effective_limit - used)
                if oldest_ts is None or used == 0:
                    reset_after_ms = 0
                else:
                    reset_after_ms = max(0, (oldest_ts + self.window_ms) - now)

                return UsageSnapshot(
                    used=used,
                    limit=effective_limit,
                    remaining=remaining,
                    reset_after_ms=reset_after_ms
                )
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
                raise

        return await self.store.run(_query)

    async def purge_expired(self) -> int:
        def _purge(conn: sqlite3.Connection):
            conn.execute("BEGIN IMMEDIATE;")
            try:
                now = self.clock.now_ms()
                cutoff = now - self.window_ms
                cur = conn.cursor()
                cur.execute("DELETE FROM usage WHERE ts_ms <= ?;", (cutoff,))
                deleted = cur.rowcount
                conn.execute("COMMIT;")
                return deleted
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
                raise

        return await self.store.run(_purge)
