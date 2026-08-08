#!/usr/bin/env python3
"""
Integrity hash maintenance CLI.
Usage: python -m api.manage_integrity --help
"""

import asyncio
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.database import db, client
from api.services.integrity_service import IntegrityService


class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


def print_success(message: str):
    print(f"{Colors.GREEN}✓ {message}{Colors.RESET}")


def print_info(message: str):
    print(f"{Colors.BLUE}ℹ {message}{Colors.RESET}")


def print_warning(message: str):
    print(f"{Colors.YELLOW}⚠ {message}{Colors.RESET}")


async def backfill_integrity_hashes(dry_run: bool = False) -> None:
    """
    Compute and store ``integrity_hash`` for TimeRecords that have none.

    Idempotent: only records missing the field are touched, and no hashed
    field is modified — only ``integrity_hash`` is set. Safe to re-run; a
    second run finds nothing left to do.
    """
    query = {"integrity_hash": {"$exists": False}}
    total = await db.TimeRecords.count_documents(query)

    if total == 0:
        print_success("No legacy records found. Nothing to backfill.")
        return

    print_info(f"Found {total} record(s) without integrity_hash.")

    updated = 0
    async for record in db.TimeRecords.find(query):
        computed_hash = IntegrityService.compute_record_hash(record)
        if dry_run:
            updated += 1
            continue
        await db.TimeRecords.update_one(
            {"_id": record["_id"], "integrity_hash": {"$exists": False}},
            {"$set": {"integrity_hash": computed_hash}}
        )
        updated += 1

    if dry_run:
        print_warning(f"[dry-run] Would backfill {updated} record(s). No changes written.")
    else:
        print_success(f"Backfilled integrity_hash for {updated} record(s).")


async def main():
    parser = argparse.ArgumentParser(
        description='Manage TimeRecord integrity hashes',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    subparsers = parser.add_subparsers(dest='command', help='Commands')

    backfill_parser = subparsers.add_parser(
        'backfill', help='Compute and store integrity_hash for legacy records missing it'
    )
    backfill_parser.add_argument(
        '--dry-run', action='store_true',
        help='Report how many records would be updated without writing changes'
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    try:
        if args.command == 'backfill':
            await backfill_integrity_hashes(dry_run=args.dry_run)
    except Exception as e:
        print(f"{Colors.RED}✗ Error: {str(e)}{Colors.RESET}")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
