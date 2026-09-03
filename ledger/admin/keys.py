"""API key admin CLI (SPEC.md §9 Phase 7): `python -m ledger.admin.keys
mint|list|revoke`.

Lives under `ledger.admin`, not `scripts/`, so it runs on a deployed
machine -- see `ledger/admin/__init__.py`. Deliberately thin, the same
shape `scripts/demo.py` takes around `dashboard.demo`: all the logic is in
`ledger.core.apikeys`.
"""

import argparse
import asyncio
import uuid

from ledger.core.apikeys import create_api_key, list_api_keys, revoke_api_key
from ledger.db.engine import engine
from ledger.db.session import async_session_factory


async def _mint(name: str) -> None:
    async with async_session_factory() as session:
        key_id, raw_key = await create_api_key(session, name=name)
        await session.commit()
    await engine.dispose()
    print(f"id: {key_id}")
    print(f"key: {raw_key}")
    print("This key will not be shown again -- store it now.")


async def _list() -> None:
    async with async_session_factory() as session:
        keys = await list_api_keys(session)
    await engine.dispose()
    if not keys:
        print("no API keys")
        return
    for key in keys:
        status = "active" if key.active else "revoked"
        print(f"{key.id}  {status:8}  {key.name}  created {key.created_at.isoformat()}")


async def _revoke(key_id: uuid.UUID) -> None:
    async with async_session_factory() as session:
        revoked = await revoke_api_key(session, key_id=key_id)
        await session.commit()
    await engine.dispose()
    if not revoked:
        print(f"no active key with id {key_id}")
        raise SystemExit(1)
    print(f"revoked {key_id}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    mint_parser = subparsers.add_parser("mint", help="Create a new active API key")
    mint_parser.add_argument("--name", required=True)

    subparsers.add_parser("list", help="List every API key")

    revoke_parser = subparsers.add_parser("revoke", help="Deactivate an API key")
    revoke_parser.add_argument("--id", required=True, dest="key_id", type=uuid.UUID)

    args = parser.parse_args(argv)

    if args.command == "mint":
        asyncio.run(_mint(args.name))
    elif args.command == "list":
        asyncio.run(_list())
    elif args.command == "revoke":
        asyncio.run(_revoke(args.key_id))


if __name__ == "__main__":
    main()
