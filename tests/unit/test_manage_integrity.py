"""
Unit tests for the integrity_hash backfill CLI (api.manage_integrity).

Pure unit tests — MongoDB access is fully mocked; no live database required.
"""
from unittest.mock import AsyncMock, MagicMock, patch

from api.manage_integrity import backfill_integrity_hashes
from api.services.integrity_service import IntegrityService


def _async_iter(items):
    """Build an async generator yielding items, mimicking a Motor cursor."""
    async def _gen():
        for item in items:
            yield item
    return _gen()


class TestBackfillIntegrityHashes:

    async def test_backfill_skips_when_no_legacy_records(self):
        """No records missing integrity_hash -> nothing is updated."""
        with patch("api.manage_integrity.db") as mock_db:
            mock_db.TimeRecords.count_documents = AsyncMock(return_value=0)
            mock_db.TimeRecords.find = MagicMock(return_value=_async_iter([]))
            mock_db.TimeRecords.update_one = AsyncMock()

            await backfill_integrity_hashes()

            mock_db.TimeRecords.update_one.assert_not_called()

    async def test_backfill_stores_hash_without_touching_hashed_fields(self):
        """Legacy records get integrity_hash set; no other field is modified."""
        legacy_record = {
            "_id": "legacy_id_1",
            "worker_id": "w1",
            "company_id": "c1",
            "type": "entry",
            "timestamp": None,
            "duration_minutes": None,
            "created_at": None,
        }
        expected_hash = IntegrityService.compute_record_hash(legacy_record)

        with patch("api.manage_integrity.db") as mock_db:
            mock_db.TimeRecords.count_documents = AsyncMock(return_value=1)
            mock_db.TimeRecords.find = MagicMock(return_value=_async_iter([legacy_record]))
            mock_db.TimeRecords.update_one = AsyncMock()

            await backfill_integrity_hashes()

            mock_db.TimeRecords.update_one.assert_awaited_once_with(
                {"_id": "legacy_id_1", "integrity_hash": {"$exists": False}},
                {"$set": {"integrity_hash": expected_hash}},
            )

    async def test_backfill_dry_run_does_not_write(self):
        """--dry-run reports what would change but performs no writes."""
        legacy_record = {"_id": "legacy_id_2", "worker_id": "w2"}

        with patch("api.manage_integrity.db") as mock_db:
            mock_db.TimeRecords.count_documents = AsyncMock(return_value=1)
            mock_db.TimeRecords.find = MagicMock(return_value=_async_iter([legacy_record]))
            mock_db.TimeRecords.update_one = AsyncMock()

            await backfill_integrity_hashes(dry_run=True)

            mock_db.TimeRecords.update_one.assert_not_called()

    async def test_backfill_is_idempotent_on_second_run(self):
        """Re-running the backfill after all records are hashed makes no changes."""
        with patch("api.manage_integrity.db") as mock_db:
            # Second run: query for missing-hash records returns nothing.
            mock_db.TimeRecords.count_documents = AsyncMock(return_value=0)
            mock_db.TimeRecords.find = MagicMock(return_value=_async_iter([]))
            mock_db.TimeRecords.update_one = AsyncMock()

            await backfill_integrity_hashes()
            await backfill_integrity_hashes()

            mock_db.TimeRecords.update_one.assert_not_called()
            assert mock_db.TimeRecords.count_documents.await_count == 2
