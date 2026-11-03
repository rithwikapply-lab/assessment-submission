import argparse
import asyncio
import os
import sys
from llm_router.storage import SqliteStore
from llm_router.tenants import add_tenant, list_tenants

def main():
    parser = argparse.ArgumentParser(description="LLM Gateway Tenant Admin CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add-tenant", help="Add or update a tenant")
    add_parser.add_argument("--id", required=True, help="Tenant ID")
    add_parser.add_argument("--key", required=True, help="Tenant API key")
    add_parser.add_argument("--limit", type=int, default=None, help="Tokens per minute limit")
    add_parser.add_argument("--db", default=os.getenv("GATEWAY_DB_PATH", "./data/gateway.db"), help="Path to sqlite DB")

    list_parser = subparsers.add_parser("list-tenants", help="List all tenants")
    list_parser.add_argument("--db", default=os.getenv("GATEWAY_DB_PATH", "./data/gateway.db"), help="Path to sqlite DB")

    args = parser.parse_args()

    async def run_command():
        store = SqliteStore(args.db)
        await store.init()
        try:
            if args.command == "add-tenant":
                await add_tenant(store, args.id, args.key, args.limit)
                print(f"Tenant '{args.id}' added successfully.")
            elif args.command == "list-tenants":
                tenants = await list_tenants(store)
                for t in tenants:
                    print(f"{t['tenant_id']}: {t['tokens_per_minute']}")
        finally:
            await store.aclose()

    asyncio.run(run_command())

if __name__ == "__main__":
    main()
