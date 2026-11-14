"""
Unit tests for the shift planning module (services + time helpers).

Uses the in-memory Mongo double pattern from tests/unit/test_absences.py:
collections live inside a FakeDB monkeypatched over the module-level ``db``.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import HTTPException

from api.services import shift_planning
from api.services.shift_planning_time import (
    cell_signature,
    find_overlap,
    planned_minutes,
    rest_warnings,
    shift_duration_minutes,
    validate_shift_timing,
    week_dates,
)


# ---------------------------------------------------------------------------
# Fake Mongo (same contract as tests/unit/test_absences.py)
# ---------------------------------------------------------------------------


def _matches(doc: dict, query: dict) -> bool:
    for key, expected in query.items():
        actual = doc.get(key)
        if isinstance(expected, dict) and any(op.startswith("$") for op in expected):
            for op, val in expected.items():
                if op == "$in" and actual not in val:
                    return False
                elif op == "$gte" and not (actual is not None and actual >= val):
                    return False
                elif op == "$lte" and not (actual is not None and actual <= val):
                    return False
                elif op == "$gt" and not (actual is not None and actual > val):
                    return False
        else:
            if isinstance(actual, list):
                # Mongo matches a scalar against an array field by membership
                if expected not in actual:
                    return False
            elif actual != expected:
                return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def to_list(self, length=None):
        return list(self._docs)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs if docs is not None else []

    async def find_one(self, query=None):
        query = query or {}
        for doc in self.docs:
            if _matches(doc, query):
                return doc
        return None

    def find(self, query=None, projection=None):
        query = query or {}
        return FakeCursor([d for d in self.docs if _matches(d, query)])

    async def insert_one(self, doc):
        doc = dict(doc)
        doc["_id"] = doc.get("_id", ObjectId())
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def update_one(self, query, update):
        for doc in self.docs:
            if _matches(doc, query):
                doc.update(update.get("$set", {}))
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def delete_one(self, query):
        for i, doc in enumerate(self.docs):
            if _matches(doc, query):
                return self.docs.pop(i) and SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def delete_many(self, query):
        keep = [d for d in self.docs if not _matches(d, query)]
        removed = len(self.docs) - len(keep)
        self.docs[:] = keep
        return SimpleNamespace(deleted_count=removed)


class FakeDB:
    def __init__(self):
        self.ShiftTypes = FakeCollection()
        self.PlannedShifts = FakeCollection()
        self.ShiftPlanPublications = FakeCollection()
        self.Companies = FakeCollection()
        self.Workers = FakeCollection()
        self.Absences = FakeCollection()


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(shift_planning, "db", db)
    return db


# ---------------------------------------------------------------------------
# Helpers to build data
# ---------------------------------------------------------------------------

COMPANY = "665f00000000000000000001"
WORKER = "665f00000000000000000002"
TYPE_ID = "665f0000000000000000000a"


def active_worker(worker_id=WORKER, company_id=COMPANY, deleted=False):
    return {
        "_id": worker_id,
        "first_name": "Ana",
        "last_name": "García",
        "email": "ana@example.com",
        "company_ids": [company_id],
        "deleted_at": datetime(2026, 1, 1) if deleted else None,
    }


def shift_type(**over):
    doc = {
        "_id": ObjectId(TYPE_ID),
        "company_id": COMPANY,
        "name": "Mañana",
        "short_code": "M",
        "color": "#22c55e",
        "start_time": "06:00",
        "end_time": "14:00",
        "break_minutes": 30,
        "deleted_at": None,
    }
    doc.update(over)
    return doc


def item(shift_type_id=TYPE_ID, **over):
    base = {
        "shift_type_id": shift_type_id,
        "name": None,
        "short_code": None,
        "color": None,
        "start_time": None,
        "end_time": None,
        "break_minutes": None,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Time helpers (task 2.1)
# ---------------------------------------------------------------------------


class TestTimeHelpers:
    def test_normal_duration(self):
        assert shift_duration_minutes("06:00", "14:00") == 480

    def test_midnight_crossing_duration(self):
        assert shift_duration_minutes("22:00", "06:00") == 480

    def test_end_midnight_counts_24(self):
        assert shift_duration_minutes("20:00", "00:00") == 240

    def test_planned_minutes_subtracts_break(self):
        assert planned_minutes("13:00", "17:00", 30) == 210

    def test_week_dates(self):
        assert week_dates("2026-11-02")[0] == "2026-11-02"
        assert week_dates("2026-11-02")[-1] == "2026-11-08"

    def test_find_overlap(self):
        assert find_overlap([(0, 100), (90, 150)])
        assert not find_overlap([(0, 100), (100, 150)])  # touching is not overlap

    def test_validate_equal_hours_rejected(self):
        with pytest.raises(HTTPException) as exc:
            validate_shift_timing("08:00", "08:00", 0)
        assert exc.value.detail["error_code"] == "shift_plan.invalid_time_range"

    def test_validate_break_too_big(self):
        with pytest.raises(HTTPException) as exc:
            validate_shift_timing("08:00", "09:00", 60)
        assert exc.value.detail["error_code"] == "shift_plan.invalid_break"

    def test_validate_night_shift_break_ok(self):
        validate_shift_timing("22:00", "06:00", 30)  # duration 480 > break


# ---------------------------------------------------------------------------
# Cell resolution and write (task 2.2/2.3)
# ---------------------------------------------------------------------------


class TestResolveCellItems:
    async def test_copies_type_values(self, fake_db):
        fake_db.ShiftTypes.docs.append(shift_type())
        resolved = await shift_planning.resolve_cell_items(COMPANY, [item()])
        assert resolved == [
            {
                "shift_type_id": TYPE_ID,
                "name": "Mañana",
                "short_code": "M",
                "color": "#22c55e",
                "start_time": "06:00",
                "end_time": "14:00",
                "break_minutes": 30,
            }
        ]

    async def test_custom_without_type(self, fake_db):
        resolved = await shift_planning.resolve_cell_items(
            COMPANY, [item(shift_type_id=None, start_time="09:30", end_time="13:00")]
        )
        assert resolved[0]["start_time"] == "09:30"
        assert resolved[0]["name"] is None
        assert resolved[0]["short_code"] is None

    async def test_custom_without_hours_rejected(self, fake_db):
        with pytest.raises(HTTPException) as exc:
            await shift_planning.resolve_cell_items(COMPANY, [item(shift_type_id=None)])
        assert exc.value.detail["error_code"] == "shift_plan.incomplete_shift"

    async def test_override_hours_wins(self, fake_db):
        fake_db.ShiftTypes.docs.append(shift_type())
        resolved = await shift_planning.resolve_cell_items(
            COMPANY, [item(start_time="07:00", end_time="15:30")]
        )
        assert resolved[0]["start_time"] == "07:00"
        assert resolved[0]["end_time"] == "15:30"
        assert resolved[0]["name"] == "Mañana"  # copy of the type survives

    async def test_unknown_type_rejected(self, fake_db):
        with pytest.raises(HTTPException) as exc:
            await shift_planning.resolve_cell_items(
                COMPANY, [item(shift_type_id="665f000000000000000000zz")]
            )
        assert exc.value.detail["error_code"] == "shift_plan.shift_type_not_found"

    async def test_too_many_shifts_rejected(self, fake_db):
        fake_db.ShiftTypes.docs.append(shift_type())
        items = [item() for _ in range(11)]
        with pytest.raises(HTTPException) as exc:
            await shift_planning.resolve_cell_items(COMPANY, items)
        assert exc.value.detail["error_code"] == "shift_plan.too_many_shifts"


class TestWriteCell:
    async def test_insert_new_cell(self, fake_db):
        fake_db.Workers.docs.append(active_worker())
        fake_db.ShiftTypes.docs.append(shift_type())
        stored = await shift_planning.write_cell(COMPANY, WORKER, "2026-11-02", [item()])
        assert len(stored) == 1
        doc = fake_db.PlannedShifts.docs[0]
        assert doc["published"] is None and doc["removed"] is False

    async def test_worker_not_in_company_rejected(self, fake_db):
        fake_db.Workers.docs.append(active_worker(company_id="665f00000000000000000099"))
        with pytest.raises(HTTPException) as exc:
            await shift_planning.write_cell(COMPANY, WORKER, "2026-11-02", [item()])
        assert exc.value.detail["error_code"] == "shift_plan.worker_not_found"

    async def test_deactivated_worker_rejected(self, fake_db):
        fake_db.Workers.docs.append(active_worker(deleted=True))
        with pytest.raises(HTTPException) as exc:
            await shift_planning.write_cell(COMPANY, WORKER, "2026-11-02", [item()])
        assert exc.value.detail["error_code"] == "shift_plan.worker_not_found"

    async def test_overlap_same_day_rejected(self, fake_db):
        fake_db.Workers.docs.append(active_worker())
        with pytest.raises(HTTPException) as exc:
            await shift_planning.write_cell(
                COMPANY,
                WORKER,
                "2026-11-02",
                [
                    item(shift_type_id=None, start_time="08:00", end_time="14:00"),
                    item(shift_type_id=None, start_time="13:00", end_time="17:00"),
                ],
            )
        assert exc.value.detail["error_code"] == "shift_plan.overlap"
        assert fake_db.PlannedShifts.docs == []  # cell untouched

    async def test_overlap_with_previous_night_rejected(self, fake_db):
        fake_db.Workers.docs.append(active_worker())
        fake_db.PlannedShifts.docs.append(
            {
                "_id": ObjectId(),
                "company_id": COMPANY,
                "worker_id": WORKER,
                "date": "2026-11-01",
                "shifts": [
                    {
                        "shift_type_id": None,
                        "name": None,
                        "short_code": None,
                        "color": None,
                        "start_time": "22:00",
                        "end_time": "06:00",
                        "break_minutes": 0,
                    }
                ],
                "published": None,
                "removed": False,
            }
        )
        with pytest.raises(HTTPException) as exc:
            await shift_planning.write_cell(
                COMPANY,
                WORKER,
                "2026-11-02",
                [item(shift_type_id=None, start_time="05:00", end_time="13:00")],
            )
        assert exc.value.detail["error_code"] == "shift_plan.overlap"

    async def test_no_overlap_when_touching_boundaries(self, fake_db):
        fake_db.Workers.docs.append(active_worker())
        fake_db.PlannedShifts.docs.append(
            {
                "_id": ObjectId(),
                "company_id": COMPANY,
                "worker_id": WORKER,
                "date": "2026-11-01",
                "shifts": [
                    {
                        "shift_type_id": None,
                        "name": None,
                        "short_code": None,
                        "color": None,
                        "start_time": "22:00",
                        "end_time": "06:00",
                        "break_minutes": 0,
                    }
                ],
                "published": None,
                "removed": False,
            }
        )
        stored = await shift_planning.write_cell(
            COMPANY,
            WORKER,
            "2026-11-02",
            [item(shift_type_id=None, start_time="06:00", end_time="14:00")],
        )
        assert len(stored) == 1

    async def test_clear_never_published_deletes_doc(self, fake_db):
        oid = ObjectId()
        fake_db.Workers.docs.append(active_worker())
        fake_db.PlannedShifts.docs.append(
            {"_id": oid, "company_id": COMPANY, "worker_id": WORKER, "date": "2026-11-02",
             "shifts": [{"shift_type_id": None, "name": "X", "short_code": None, "color": None,
                         "start_time": "06:00", "end_time": "14:00", "break_minutes": 0}],
             "published": None, "removed": False}
        )
        await shift_planning.write_cell(COMPANY, WORKER, "2026-11-02", [])
        assert fake_db.PlannedShifts.docs == []

    async def test_clear_published_marks_removed(self, fake_db):
        oid = ObjectId()
        published = [{"shift_type_id": None, "name": "X", "short_code": None, "color": None,
                      "start_time": "06:00", "end_time": "14:00", "break_minutes": 0}]
        fake_db.Workers.docs.append(active_worker())
        fake_db.PlannedShifts.docs.append(
            {"_id": oid, "company_id": COMPANY, "worker_id": WORKER, "date": "2026-11-02",
             "shifts": list(published), "published": list(published), "removed": False}
        )
        await shift_planning.write_cell(COMPANY, WORKER, "2026-11-02", [])
        doc = fake_db.PlannedShifts.docs[0]
        assert doc["removed"] is True
        assert doc["published"] == published  # worker still sees it until publish

    async def test_store_cell_updates_when_insert_races(self, fake_db, monkeypatch):
        from pymongo.errors import DuplicateKeyError

        winner_id = ObjectId()
        calls = {"n": 0}
        real_find = fake_db.PlannedShifts.find_one

        async def find_one(query=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return await real_find(query)

        async def insert_one(doc):
            fake_db.PlannedShifts.docs.append(
                {"_id": winner_id, "company_id": COMPANY, "worker_id": WORKER,
                 "date": "2026-11-02", "shifts": [_stored("T", "15:00", "22:00")],
                 "published": [_stored("T", "15:00", "22:00")], "removed": False}
            )
            raise DuplicateKeyError("dup")

        monkeypatch.setattr(fake_db.PlannedShifts, "find_one", find_one)
        monkeypatch.setattr(fake_db.PlannedShifts, "insert_one", insert_one)
        await shift_planning.store_cell(COMPANY, WORKER, "2026-11-02", [_stored("M", "06:00", "14:00")])
        assert len(fake_db.PlannedShifts.docs) == 1
        doc = fake_db.PlannedShifts.docs[0]
        assert doc["_id"] == winner_id
        assert doc["shifts"][0]["name"] == "M"
        assert doc["published"][0]["name"] == "T"


# ---------------------------------------------------------------------------
# Warnings (task 2.3)
# ---------------------------------------------------------------------------


class TestWarnings:
    def _days(self, *pairs):
        # (date, [(start, end)]) -> absolute intervals
        return [
            (day, shift_planning._days_with_intervals(
                day, [{"start_time": s, "end_time": e} for s, e in shifts]
            )[1])
            for day, shifts in pairs
        ]

    def test_short_rest_warns_next_day(self):
        days = self._days(("2026-11-02", [("14:00", "22:00")]),
                          ("2026-11-03", [("06:00", "14:00")]))
        warns = shift_planning.compute_warnings({"w1": days}, {}, "2026-11-02", "2026-11-03")
        assert [w["date"] for w in warns] == ["2026-11-03"]
        assert warns[0]["code"] == "rest_under_12h"

    def test_night_rotation_no_warning(self):
        days = self._days(("2026-11-02", [("22:00", "06:00")]),
                          ("2026-11-03", [("22:00", "06:00")]))
        warns = shift_planning.compute_warnings({"w1": days}, {}, "2026-11-02", "2026-11-03")
        assert warns == []

    def test_split_day_no_warning(self):
        days = self._days(("2026-11-02", [("13:00", "16:00"), ("20:00", "23:59")]))
        warns = shift_planning.compute_warnings({"w1": days}, {}, "2026-11-02", "2026-11-02")
        assert warns == []

    def test_previous_day_before_range_warns_first_day(self):
        days = self._days(("2026-11-01", [("14:00", "22:00")]),
                          ("2026-11-02", [("06:00", "14:00")]))
        warns = shift_planning.compute_warnings({"w1": days}, {}, "2026-11-02", "2026-11-08")
        assert [w["date"] for w in warns] == ["2026-11-02"]

    def test_warning_outside_range_dropped(self):
        # only the prev-day pair: nothing inside the range to warn about
        days = self._days(("2026-11-01", [("14:00", "22:00")]))
        warns = shift_planning.compute_warnings({"w1": days}, {}, "2026-11-02", "2026-11-08")
        assert warns == []

    def test_absence_overlap(self):
        days = self._days(("2026-11-02", [("09:00", "17:00")]))
        absences = {"w1": {"2026-11-02"}}
        warns = shift_planning.compute_warnings({"w1": days}, absences, "2026-11-02", "2026-11-08")
        assert [w["code"] for w in warns] == ["absence_overlap"]


# ---------------------------------------------------------------------------
# Pending-change detection and roster (task 2.3)
# ---------------------------------------------------------------------------


def _stored(name, start, end, brk=0):
    return {
        "shift_type_id": None,
        "name": name,
        "short_code": None,
        "color": None,
        "start_time": start,
        "end_time": end,
        "break_minutes": brk,
    }


class TestCellSignature:
    def test_repaint_identical_not_pending(self):
        assert cell_signature([_stored("M", "06:00", "14:00")]) == cell_signature(
            [_stored("M", "06:00", "14:00")]
        )

    def test_order_is_multiset(self):
        a = [_stored("A", "06:00", "14:00"), _stored("B", "20:00", "23:00")]
        b = [_stored("B", "20:00", "23:00"), _stored("A", "06:00", "14:00")]
        assert cell_signature(a) == cell_signature(b)

    def test_hour_change_is_pending(self):
        assert cell_signature([_stored("M", "06:00", "14:00")]) != cell_signature(
            [_stored("M", "06:30", "14:00")]
        )


class TestRoster:
    async def test_roster_counts_and_minutes(self, fake_db):
        fake_db.Companies.docs.append({"_id": ObjectId(COMPANY), "name": "Acme",
                                       "shift_planning_enabled": True})
        fake_db.Workers.docs.append(active_worker())
        fake_db.PlannedShifts.docs.append(
            {"_id": ObjectId(), "company_id": COMPANY, "worker_id": WORKER,
             "date": "2026-11-02",
             "shifts": [_stored("M", "06:00", "14:00", 30)],
             "published": None, "removed": False}
        )
        roster = await shift_planning.get_roster(COMPANY, "2026-11-02", "2026-11-08")
        assert roster["pending_changes"] == 1
        worker_row = roster["workers"][0]
        assert worker_row["planned_minutes"] == 450
        assert worker_row["cells"]["2026-11-02"][0]["start_time"] == "06:00"

    def test_impossible_date_is_422(self):
        with pytest.raises(HTTPException) as exc:
            shift_planning.check_range("2026-02-31", "2026-03-02")
        assert exc.value.status_code == 422
        assert exc.value.detail["error_code"] == "shift_plan.invalid_range"

    async def test_range_too_long(self, fake_db):
        with pytest.raises(HTTPException) as exc:
            await shift_planning.get_roster(COMPANY, "2026-01-01", "2026-03-01")
        assert exc.value.detail["error_code"] == "shift_plan.range_too_long"

    async def test_work_center_filter(self, fake_db):
        fake_db.Workers.docs.append(active_worker())
        roster = await shift_planning.get_roster(COMPANY, "2026-11-02", "2026-11-02", "center-x")
        assert roster["workers"] == []


# ---------------------------------------------------------------------------
# Publish (task 2.3)
# ---------------------------------------------------------------------------


class TestPublish:
    async def _cell(self, fake_db, date_, shifts, published=None):
        fake_db.PlannedShifts.docs.append(
            {"_id": ObjectId(), "company_id": COMPANY, "worker_id": WORKER,
             "date": date_, "shifts": shifts,
             "published": published, "removed": published is not None and len(shifts) == 0}
        )

    async def test_publish_first_time(self, fake_db):
        await self._cell(fake_db, "2026-11-02", [_stored("M", "06:00", "14:00")])
        result = await shift_planning.publish(COMPANY, "2026-11-02", "2026-11-08",
                                              acknowledge_warnings=False, notify=True,
                                              published_by="admin")
        assert result["changed_cells"] == 1
        assert result["publication_id"] is not None
        doc = fake_db.PlannedShifts.docs[0]
        assert doc["published"] == doc["shifts"]
        pub = fake_db.ShiftPlanPublications.docs[0]
        assert pub["notified"] is True
        assert pub["changes"][0]["before"] == []
        assert pub["published_by"] == "admin"

    async def test_publish_no_changes_creates_nothing(self, fake_db):
        shifts = [_stored("M", "06:00", "14:00")]
        await self._cell(fake_db, "2026-11-02", list(shifts), published=list(shifts))
        result = await shift_planning.publish(COMPANY, "2026-11-02", "2026-11-08",
                                              acknowledge_warnings=False, notify=True,
                                              published_by="admin")
        assert result["publication_id"] is None
        assert fake_db.ShiftPlanPublications.docs == []

    async def test_repaint_not_pending_on_publish(self, fake_db):
        shifts = [_stored("M", "06:00", "14:00")]
        await self._cell(fake_db, "2026-11-02", list(shifts), published=list(shifts))
        result = await shift_planning.publish(COMPANY, "2026-11-02", "2026-11-02",
                                              acknowledge_warnings=False, notify=False,
                                              published_by="admin")
        assert result["changed_cells"] == 0

    async def test_removed_cell_deleted_at_publish(self, fake_db):
        published = [_stored("M", "06:00", "14:00")]
        fake_db.PlannedShifts.docs.append(
            {"_id": ObjectId(), "company_id": COMPANY, "worker_id": WORKER,
             "date": "2026-11-02", "shifts": [], "published": published, "removed": True}
        )
        result = await shift_planning.publish(COMPANY, "2026-11-02", "2026-11-02",
                                              acknowledge_warnings=False, notify=True,
                                              published_by="admin")
        assert fake_db.PlannedShifts.docs == []
        change = result["changes"][0]
        assert change["before"] == published and change["after"] == []

    async def test_warnings_block_until_ack(self, fake_db):
        await self._cell(fake_db, "2026-11-02", [_stored("A", "14:00", "22:00")])
        await self._cell(fake_db, "2026-11-03", [_stored("B", "06:00", "14:00")])
        with pytest.raises(HTTPException) as exc:
            await shift_planning.publish(COMPANY, "2026-11-02", "2026-11-08",
                                         acknowledge_warnings=False, notify=True,
                                         published_by="admin")
        assert exc.value.detail["error_code"] == "shift_plan.warnings_not_acknowledged"
        assert fake_db.ShiftPlanPublications.docs == []

        result = await shift_planning.publish(COMPANY, "2026-11-02", "2026-11-08",
                                              acknowledge_warnings=True, notify=True,
                                              published_by="admin")
        assert result["changed_cells"] == 2
        pub = fake_db.ShiftPlanPublications.docs[0]
        assert [w["code"] for w in pub["acknowledged_warnings"]] == ["rest_under_12h"]

    async def test_rest_warning_uses_published_day_before_range(self, fake_db):
        fake_db.PlannedShifts.docs.append(
            {"_id": ObjectId(), "company_id": COMPANY, "worker_id": WORKER,
             "date": "2026-11-02", "shifts": [],
             "published": [_stored("N", "22:00", "06:00")], "removed": True}
        )
        await self._cell(fake_db, "2026-11-03", [_stored("M", "08:00", "16:00")])
        with pytest.raises(HTTPException) as exc:
            await shift_planning.publish(COMPANY, "2026-11-03", "2026-11-03",
                                         acknowledge_warnings=False, notify=False,
                                         published_by="admin")
        assert exc.value.detail["error_code"] == "shift_plan.warnings_not_acknowledged"

    async def test_unpublished_draft_before_range_does_not_warn(self, fake_db):
        await self._cell(fake_db, "2026-11-02", [_stored("N", "22:00", "06:00")])
        await self._cell(fake_db, "2026-11-03", [_stored("M", "08:00", "16:00")])
        result = await shift_planning.publish(COMPANY, "2026-11-03", "2026-11-03",
                                              acknowledge_warnings=False, notify=False,
                                              published_by="admin")
        assert result["changed_cells"] == 1


# ---------------------------------------------------------------------------
# Copy week (task 2.4)
# ---------------------------------------------------------------------------


class TestCopyWeek:
    async def _source(self, fake_db, date_, shifts, worker_id=WORKER):
        fake_db.PlannedShifts.docs.append(
            {"_id": ObjectId(), "company_id": COMPANY, "worker_id": worker_id,
             "date": date_, "shifts": shifts, "published": None, "removed": False}
        )

    async def test_copy_to_empty_week(self, fake_db):
        fake_db.Workers.docs.append(active_worker())
        await self._source(fake_db, "2026-11-02", [_stored("M", "06:00", "14:00")])
        result = await shift_planning.copy_week(COMPANY, "2026-11-02", "2026-11-09", "skip")
        assert result == {"copied": 1, "skipped": 0}
        target = next(d for d in fake_db.PlannedShifts.docs if d["date"] == "2026-11-09")
        assert target["shifts"][0]["name"] == "M"
        assert target["published"] is None  # results stay draft

    async def test_skip_keeps_occupied_target(self, fake_db):
        fake_db.Workers.docs.append(active_worker())
        await self._source(fake_db, "2026-11-02", [_stored("M", "06:00", "14:00")])
        await self._source(fake_db, "2026-11-09", [_stored("T", "15:00", "22:00")])
        result = await shift_planning.copy_week(COMPANY, "2026-11-02", "2026-11-09", "skip")
        assert result == {"copied": 0, "skipped": 1}
        target = next(d for d in fake_db.PlannedShifts.docs if d["date"] == "2026-11-09")
        assert target["shifts"][0]["name"] == "T"

    async def test_overwrite_replaces_target(self, fake_db):
        fake_db.Workers.docs.append(active_worker())
        await self._source(fake_db, "2026-11-02", [_stored("M", "06:00", "14:00")])
        await self._source(fake_db, "2026-11-09", [_stored("T", "15:00", "22:00")])
        result = await shift_planning.copy_week(COMPANY, "2026-11-02", "2026-11-09", "overwrite")
        assert result == {"copied": 1, "skipped": 0}
        target = next(d for d in fake_db.PlannedShifts.docs if d["date"] == "2026-11-09")
        assert target["shifts"][0]["name"] == "M"

    async def test_deactivated_worker_not_copied(self, fake_db):
        fake_db.Workers.docs.append(active_worker(deleted=True))
        await self._source(fake_db, "2026-11-02", [_stored("M", "06:00", "14:00")])
        result = await shift_planning.copy_week(COMPANY, "2026-11-02", "2026-11-09", "skip")
        assert result == {"copied": 0, "skipped": 0}
        assert len(fake_db.PlannedShifts.docs) == 1

    async def test_copy_keeps_shifts_when_type_was_deleted(self, fake_db):
        fake_db.Workers.docs.append(active_worker())
        stored = _stored("M", "06:00", "14:00")
        stored["shift_type_id"] = TYPE_ID
        await self._source(fake_db, "2026-11-02", [stored])
        result = await shift_planning.copy_week(COMPANY, "2026-11-02", "2026-11-09", "skip")
        assert result == {"copied": 1, "skipped": 0}
        target = next(d for d in fake_db.PlannedShifts.docs if d["date"] == "2026-11-09")
        assert target["shifts"][0]["start_time"] == "06:00"
        assert target["shifts"][0]["shift_type_id"] == TYPE_ID

    async def test_non_monday_rejected(self, fake_db):
        with pytest.raises(HTTPException) as exc:
            await shift_planning.copy_week(COMPANY, "2026-11-02", "2026-11-05", "skip")
        assert exc.value.detail["error_code"] == "shift_plan.week_not_monday"

    async def test_same_week_rejected(self, fake_db):
        with pytest.raises(HTTPException) as exc:
            await shift_planning.copy_week(COMPANY, "2026-11-02", "2026-11-02", "skip")
        assert exc.value.detail["error_code"] == "shift_plan.overlapping_weeks"


# ---------------------------------------------------------------------------
# Gating + company flag (task 1.6)
# ---------------------------------------------------------------------------


class TestShiftPlanningGating:
    @pytest.fixture
    def gating_db(self, monkeypatch):
        from api.services import shift_planning_gating

        db = FakeDB()
        monkeypatch.setattr(shift_planning_gating, "db", db)
        return db, shift_planning_gating.ensure_shift_planning_module_enabled

    async def test_invalid_company_id_rejected(self, gating_db):
        _, ensure = gating_db
        with pytest.raises(HTTPException) as exc:
            await ensure("not-an-objectid")
        assert exc.value.status_code == 404
        assert exc.value.detail["error_code"] == "shift_plan.company_not_found"

    async def test_missing_company_rejected(self, gating_db):
        _, ensure = gating_db
        with pytest.raises(HTTPException) as exc:
            await ensure(COMPANY)
        assert exc.value.status_code == 404

    async def test_deleted_company_rejected(self, gating_db):
        db, ensure = gating_db
        db.Companies.docs.append(
            {"_id": ObjectId(COMPANY), "deleted_at": datetime(2026, 1, 1),
             "shift_planning_enabled": True}
        )
        with pytest.raises(HTTPException) as exc:
            await ensure(COMPANY)
        assert exc.value.status_code == 404

    async def test_module_disabled_rejected(self, gating_db):
        db, ensure = gating_db
        db.Companies.docs.append(
            {"_id": ObjectId(COMPANY), "deleted_at": None}
        )
        with pytest.raises(HTTPException) as exc:
            await ensure(COMPANY)
        assert exc.value.status_code == 403
        assert exc.value.detail["error_code"] == "shift_plan.module_disabled"

    async def test_enabled_returns_company(self, gating_db):
        db, ensure = gating_db
        doc = {"_id": ObjectId(COMPANY), "deleted_at": None,
               "shift_planning_enabled": True, "name": "Test"}
        db.Companies.docs.append(doc)
        assert await ensure(COMPANY) is doc


class TestCompanyFlagDefaults:
    def test_company_model_defaults_to_disabled(self):
        from api.models.companies import Company, CompanyUpdate

        now = datetime(2026, 1, 1)
        assert Company(name="X", created_at=now, updated_at=now).shift_planning_enabled is False
        assert CompanyUpdate(name="X").shift_planning_enabled is None
        assert CompanyUpdate(shift_planning_enabled=True).shift_planning_enabled is True

    def test_company_response_exposes_flag(self):
        from api.models.companies import CompanyResponse

        fields = CompanyResponse.model_fields
        assert "shift_planning_enabled" in fields
        assert fields["shift_planning_enabled"].default is False

