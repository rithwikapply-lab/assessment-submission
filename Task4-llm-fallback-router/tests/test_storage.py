import pytest
import sqlite3
import os
from llm_router.storage import SqliteStore

@pytest.mark.asyncio
async def test_sqlite_store_lifecycle(tmp_path):
    db_file = str(tmp_path / "test.db")
    store = SqliteStore(db_file)
    await store.init()

    def get_version(conn: sqlite3.Connection):
        cur = conn.cursor()
        cur.execute("PRAGMA user_version;")
        return cur.fetchone()[0]

    version = await store.run(get_version)
    assert version == 1

    def test_insert(conn: sqlite3.Connection):
        conn.execute("INSERT INTO tenants (key_hash, tenant_id, tokens_per_minute) VALUES (?, ?, ?);",
                     ("hash1", "tenant1", 1000))
        cur = conn.cursor()
        cur.execute("SELECT * FROM tenants WHERE tenant_id = ?;", ("tenant1",))
        return dict(cur.fetchone())

    row = await store.run(test_insert)
    assert row["tenant_id"] == "tenant1"
    assert row["tokens_per_minute"] == 1000

    await store.aclose()
    assert os.path.exists(db_file)
