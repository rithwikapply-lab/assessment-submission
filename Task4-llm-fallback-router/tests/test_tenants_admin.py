import pytest
import os
import subprocess
import sys
from llm_router.storage import SqliteStore
from llm_router.tenants import add_tenant, get_tenant_by_key, list_tenants, hash_key

@pytest.mark.asyncio
async def test_tenants_crud(tmp_path):
    db_file = str(tmp_path / "tenants.db")
    store = SqliteStore(db_file)
    await store.init()

    # Add tenant with default limit
    await add_tenant(store, "test-tenant", "secret-key-1")
    res = await get_tenant_by_key(store, "secret-key-1")
    assert res is not None
    assert res["tenant_id"] == "test-tenant"
    assert res["tokens_per_minute"] == 50000

    # Add tenant with custom limit
    await add_tenant(store, "custom-tenant", "secret-key-2", 100000)
    res2 = await get_tenant_by_key(store, "secret-key-2")
    assert res2 is not None
    assert res2["tenant_id"] == "custom-tenant"
    assert res2["tokens_per_minute"] == 100000

    # Get non-existent tenant
    assert await get_tenant_by_key(store, "unknown-key") is None

    all_tenants = await list_tenants(store)
    assert len(all_tenants) == 2
    assert all_tenants[0]["tenant_id"] == "custom-tenant"
    assert all_tenants[1]["tenant_id"] == "test-tenant"

    await store.aclose()

def test_admin_cli(tmp_path):
    db_file = str(tmp_path / "cli_test.db")
    # Test add-tenant
    cmd = [
        sys.executable, "-m", "llm_router.admin",
        "add-tenant", "--id", "tenant_a", "--key", "keyA", "--limit", "20000", "--db", db_file
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "Tenant 'tenant_a' added successfully." in res.stdout
    assert "keyA" not in res.stdout

    # Test list-tenants
    cmd = [
        sys.executable, "-m", "llm_router.admin",
        "list-tenants", "--db", db_file
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "tenant_a: 20000" in res.stdout
    assert "keyA" not in res.stdout
