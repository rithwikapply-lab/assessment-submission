import hashlib
import sqlite3
from typing import Optional, Dict, Any, List
from llm_router.storage import SqliteStore

DEFAULT_RATE_LIMIT = 50000

def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode('utf-8')).hexdigest()

async def add_tenant(store: SqliteStore, tenant_id: str, api_key: str, tokens_per_minute: Optional[int] = None) -> None:
    limit = tokens_per_minute if tokens_per_minute is not None else DEFAULT_RATE_LIMIT
    k_hash = hash_key(api_key)
    def _insert(conn: sqlite3.Connection):
        conn.execute('BEGIN IMMEDIATE;')
        try:
            conn.execute(
                """INSERT INTO tenants (key_hash, tenant_id, tokens_per_minute)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key_hash) DO UPDATE SET
                   tenant_id = excluded.tenant_id,
                   tokens_per_minute = excluded.tokens_per_minute;""",
                (k_hash, tenant_id, limit)
            )
            conn.execute('COMMIT;')
        except Exception:
            conn.execute('ROLLBACK;')
            raise
    await store.run(_insert)

async def get_tenant_by_key(store: SqliteStore, api_key: str) -> Optional[Dict[str, Any]]:
    k_hash = hash_key(api_key)
    def _select(conn: sqlite3.Connection):
        cur = conn.cursor()
        cur.execute('SELECT tenant_id, tokens_per_minute FROM tenants WHERE key_hash = ?;', (k_hash,))
        row = cur.fetchone()
        if row:
            return {'tenant_id': row['tenant_id'], 'tokens_per_minute': row['tokens_per_minute']}
        return None
    return await store.run(_select)

async def list_tenants(store: SqliteStore) -> List[Dict[str, Any]]:
    def _list(conn: sqlite3.Connection):
        cur = conn.cursor()
        cur.execute('SELECT tenant_id, tokens_per_minute FROM tenants ORDER BY tenant_id ASC;')
        return [{'tenant_id': row['tenant_id'], 'tokens_per_minute': row['tokens_per_minute']} for row in cur.fetchall()]
    return await store.run(_list)
