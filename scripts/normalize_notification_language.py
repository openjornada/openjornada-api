#!/usr/bin/env python3
"""
Idempotent normalization of ``Company.notification_language`` (task 1.5).

Optional: the Pydantic default already covers documents created before the
field existed, so the system works without running this. The script simply
materializes ``notification_language = "es"`` on companies that lack it, so
raw DB reads (and DB-level tooling) see an explicit value.

Safe to run repeatedly: only documents without the field are touched, and
existing values are never overwritten.

Usage:
    python scripts/normalize_notification_language.py [--dry-run]

Requires MONGO_URL / DB_NAME (or .env) as the API does.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.database import db  # noqa: E402
from api.models.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES  # noqa: E402


async def normalize(dry_run: bool) -> int:
    """Set notification_language=es on companies missing it. Returns count."""
    query = {
        "notification_language": {"$not": {"$in": SUPPORTED_LOCALES}},
    }
    count = 0
    async for company in db.Companies.find(query, {"_id": 1, "name": 1, "notification_language": 1}):
        count += 1
        print(f"{'[dry-run] would set' if dry_run else 'setting'} "
              f"notification_language={DEFAULT_LOCALE!r} on company "
              f"{company['_id']} ({company.get('name')!r}, current={company.get('notification_language')!r})")
        if not dry_run:
            await db.Companies.update_one({"_id": company["_id"]}, {"$set": {"notification_language": DEFAULT_LOCALE}})
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    args = parser.parse_args()

    async def run() -> None:
        total = await normalize(args.dry_run)
        if total == 0:
            print("All companies already have a valid notification_language. Nothing to do.")
        else:
            verb = "would be normalized" if args.dry_run else "normalized"
            print(f"{total} company document(s) {verb}.")

    asyncio.run(run())


if __name__ == "__main__":
    main()
