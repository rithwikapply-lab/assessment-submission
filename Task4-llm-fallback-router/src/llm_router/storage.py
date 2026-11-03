import asyncio
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Any

CURRENT_SCHEMA_VERSION = 1

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS tenants (
    key_hash TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL UNIQUE,
    tokens_per_minute INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    tokens INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('reserved', 'committed'))
);

CREATE INDEX IF NOT EXISTS idx_usage_tenant_ts ON usage(tenant_id, ts_ms);
"""

class SqliteStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._conn: sqlite3.Connection | None = None

    def _init_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                self.db_path,
                check_same_thread=True,
                isolation_level=None
            )
            self._conn.row_factory = sqlite3.Row
            # Configure PRAGMAs
            self._conn.execute('PRAGMA journal_mode = WAL;')
            self._conn.execute('PRAGMA synchronous = NORMAL;')
            self._conn.execute('PRAGMA busy_timeout = 5000;')
            self._conn.execute('PRAGMA foreign_keys = ON;')

            # Check and run migrations
            cur = self._conn.cursor()
            cur.execute('PRAGMA user_version;')
            version = cur.fetchone()[0]
            if version == 0:
                self._conn.executescript(CREATE_TABLES_SQL)
                self._conn.execute(f'PRAGMA user_version = {CURRENT_SCHEMA_VERSION};')
        return self._conn

    async def init(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._init_conn)

    async def run(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        loop = asyncio.get_running_loop()
        def wrapper():
            conn = self._init_conn()
            return fn(conn)
        return await loop.run_in_executor(self._executor, wrapper)

    async def aclose(self) -> None:
        def _close(conn: sqlite3.Connection):
            try:
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE);')
            except Exception:
                pass
            conn.close()

        if self._conn is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, _close, self._conn)
            self._conn = None
        self._executor.shutdown(wait=True)
