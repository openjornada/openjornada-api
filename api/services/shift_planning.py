"""
Shift planning core: cell writing, roster reading, publish and copy-week.

Storage model (design D4/D8), collection ``PlannedShifts`` — one document per
worker/day/company cell::

    {
      company_id: str, worker_id: str, date: "YYYY-MM-DD",
      shifts:   [CellShift]  # draft, what the admin edits
      published: None | [CellShift]  # last published snapshot; None = never
      removed: bool  # draft emptied while a published version existed
      updated_at: iso str
    }

A cell has *pending changes* when the multiset of its draft shifts differs
from the published one (an identical repaint is not a change). Workers only
ever see ``published`` (including removed cells, until the next publish).
"""
import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from bson.objectid import ObjectId
from fastapi import status
from pymongo.errors import DuplicateKeyError

from ..database import db
from ..utils.errors import raise_api_error
from .shift_planning_time import (
    absolute_interval,
    cell_signature,
    planned_minutes,
    rest_warnings,
    validate_shift_timing,
    week_dates,
)
from .shift_planning_gating import ensure_shift_planning_module_enabled

logger = logging.getLogger(__name__)

MAX_RANGE_DAYS = 42
MAX_SHIFTS_PER_CELL = 10

# Shared response shapes -----------------------------------------------------


def shift_dict(item) -> dict:
    """Pydantic model or stored dict -> plain storage dict."""
    if hasattr(item, "model_dump"):
        return item.model_dump()
    return dict(item)


# Validation helpers ----------------------------------------------------------


def _require_monday(week_start: str) -> None:
    if date.fromisoformat(week_start).weekday() != 0:
        raise_api_error(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="shift_plan.week_not_monday",
            message="La semana debe empezar en lunes",
        )


def check_range(start_date: str, end_date: str) -> None:
    """Shared 42-day range rule for roster, publish and week queries."""
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError:
        raise_api_error(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="shift_plan.invalid_range",
            message="La fecha no es un día válido",
        )
    if end < start:
        raise_api_error(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="shift_plan.invalid_range",
            message="end_date no puede ser anterior a start_date",
        )
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise_api_error(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="shift_plan.range_too_long",
            message=f"El rango no puede superar {MAX_RANGE_DAYS} días",
        )


async def find_worker(worker_id: str) -> Optional[dict]:
    """Workers._id is an ObjectId in production; planned shifts store it as str."""
    ids = [worker_id]
    try:
        ids.append(ObjectId(worker_id))
    except Exception:
        pass
    return await db.Workers.find_one({"_id": {"$in": ids}})


async def ensure_worker_in_company(company_id: str, worker_id: str) -> dict:
    """Active worker that belongs to the company (spec: reject otherwise)."""
    worker = await find_worker(worker_id)
    if (
        worker is None
        or worker.get("deleted_at") is not None
        or company_id not in (worker.get("company_ids") or [])
    ):
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="shift_plan.worker_not_found",
            message="El trabajador no pertenece a la empresa o está dado de baja",
        )
    return worker


# Cell resolution -------------------------------------------------------------


async def resolve_cell_items(company_id: str, items: Sequence) -> List[dict]:
    """
    Turn CellWriteItem list into stored CellShift dicts, copying the shift
    type's values for type-based shifts (D2) and validating each shift.
    """
    if len(items) > MAX_SHIFTS_PER_CELL:
        raise_api_error(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="shift_plan.too_many_shifts",
            message=f"Una celda no puede tener más de {MAX_SHIFTS_PER_CELL} turnos",
        )

    resolved: List[dict] = []
    for item in items:
        data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        base: dict = {}
        type_id = data.get("shift_type_id")
        if type_id:
            try:
                oid = ObjectId(type_id)
            except Exception:
                oid = None
            shift_type = await db.ShiftTypes.find_one(
                {"_id": oid, "company_id": company_id, "deleted_at": None}
            ) if oid else None
            if shift_type is None:
                raise_api_error(
                    status_code=status.HTTP_404_NOT_FOUND,
                    error_code="shift_plan.shift_type_not_found",
                    message="Tipo de turno no encontrado para esta empresa",
                )
            base = {
                "name": shift_type["name"],
                "short_code": shift_type["short_code"],
                "color": shift_type["color"],
                "start_time": shift_type["start_time"],
                "end_time": shift_type["end_time"],
                "break_minutes": int(shift_type.get("break_minutes", 0)),
            }

        shift = {"shift_type_id": type_id}
        for key in ("name", "short_code", "color", "start_time", "end_time", "break_minutes"):
            value = data.get(key)
            if value is None:
                value = base.get(key)
            shift[key] = value
        shift["break_minutes"] = int(shift.get("break_minutes") or 0)

        if not shift.get("start_time") or not shift.get("end_time"):
            raise_api_error(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                error_code="shift_plan.incomplete_shift",
                message="Un turno sin tipo debe indicar start_time y end_time",
            )
        validate_shift_timing(shift["start_time"], shift["end_time"], int(shift.get("break_minutes", 0)))
        resolved.append(shift)
    return resolved


def raise_if_overlap(intervals: List[Tuple[int, int]]) -> None:
    """Absolute intervals (minutes) of one worker must not overlap each other."""
    ordered = sorted(intervals)
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt[0] < prev[1]:
            raise_api_error(
                status_code=status.HTTP_409_CONFLICT,
                error_code="shift_plan.overlap",
                message="Los turnos del trabajador se solapan",
            )


async def ensure_no_overlap(company_id: str, worker_id: str, day: str, shifts: List[dict]) -> None:
    """Reject the write if the new shifts overlap each other or the neighbouring days' shifts."""
    intervals = [absolute_interval(day, s["start_time"], s["end_time"]) for s in shifts]
    # Previous day (night shift crossing midnight) and next day (new night
    # shift running into it); the same day is replaced by `shifts`.
    for offset in (-1, 1):
        neighbour = (date.fromisoformat(day) + timedelta(days=offset)).isoformat()
        doc = await db.PlannedShifts.find_one(
            {"company_id": company_id, "worker_id": worker_id, "date": neighbour}
        )
        intervals += [absolute_interval(neighbour, s["start_time"], s["end_time"]) for s in (doc or {}).get("shifts", [])]
    raise_if_overlap(intervals)


# Cell write (PUT semantics, D3) ----------------------------------------------


async def write_cell(
    company_id: str,
    worker_id: str,
    day: str,
    items: Sequence,
) -> List[dict]:
    """
    Replace a whole cell. Never-published docs with an empty new list are
    deleted; published ones are marked ``removed`` (the worker keeps seeing
    the published version until the next publish). Returns the stored shifts.
    """
    await ensure_worker_in_company(company_id, worker_id)
    shifts = await resolve_cell_items(company_id, items)
    await ensure_no_overlap(company_id, worker_id, day, shifts)
    return await store_cell(company_id, worker_id, day, shifts)


async def store_cell(company_id: str, worker_id: str, day: str, shifts: List[dict]) -> List[dict]:
    """Persist an already validated cell (see ``write_cell`` for the semantics)."""
    existing = await db.PlannedShifts.find_one(
        {"company_id": company_id, "worker_id": worker_id, "date": day}
    )
    now = datetime.utcnow().isoformat()

    if not shifts:
        if existing is None:
            return []
        if existing.get("published") is None:
            await db.PlannedShifts.delete_one({"_id": existing["_id"]})
        else:
            await db.PlannedShifts.update_one(
                {"_id": existing["_id"]},
                {"$set": {"shifts": [], "removed": True, "updated_at": now}},
            )
        return []

    if existing is None:
        try:
            await db.PlannedShifts.insert_one(
                {
                    "company_id": company_id,
                    "worker_id": worker_id,
                    "date": day,
                    "shifts": shifts,
                    "published": None,
                    "removed": False,
                    "updated_at": now,
                }
            )
        except DuplicateKeyError:
            # Unique (worker, company, date): the other writer won the insert.
            await db.PlannedShifts.update_one(
                {"company_id": company_id, "worker_id": worker_id, "date": day},
                {"$set": {"shifts": shifts, "removed": False, "updated_at": now}},
            )
    else:
        await db.PlannedShifts.update_one(
            {"_id": existing["_id"]},
            {"$set": {"shifts": shifts, "removed": False, "updated_at": now}},
        )
    return shifts


# Roster reading and warnings --------------------------------------------------


async def accepted_absence_dates(company_id: str, worker_ids: Sequence[str], start_date: str, end_date: str) -> Dict[str, set]:
    """worker_id -> set of ISO dates with an accepted absence; {} if the absences module is off."""
    try:
        company_oid = ObjectId(company_id)
    except Exception:
        return {}
    company = await db.Companies.find_one({"_id": company_oid})
    if not company or not company.get("absence_management_enabled", False):
        return {}

    cursor = db.Absences.find(
        {
            "company_id": company_id,
            "worker_id": {"$in": list(worker_ids)},
            "status": "accepted",
        }
    )
    by_worker: Dict[str, set] = {}
    for absence in await cursor.to_list(length=None):
        start = _absence_date(absence.get("start_date"))
        end = _absence_date(absence.get("end_date")) or start
        if not start:
            continue
        if end < start_date or start > end_date:
            continue
        worker_set = by_worker.setdefault(absence["worker_id"], set())
        cursor_day = max(start, start_date)
        last_day = min(end, end_date)
        while cursor_day <= last_day:
            worker_set.add(cursor_day)
            cursor_day = (date.fromisoformat(cursor_day) + timedelta(days=1)).isoformat()
    return by_worker


def _absence_date(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def compute_warnings(
    days_per_worker: Dict[str, List[Tuple[str, List[Tuple[int, int]]]]],
    absences_by_worker: Dict[str, set],
    start_date: str,
    end_date: str,
) -> List[dict]:
    """
    Non-blocking warnings over draft data (D7): ``rest_under_12h`` (lands on
    the day that opens the short rest) and ``absence_overlap`` (per shift day
    inside the requested range). ``days_per_worker`` values are chronologically
    sorted (date, intervals) lists and may include the day before the range.
    """
    warnings: List[dict] = []
    for worker_id, days in days_per_worker.items():
        for day in rest_warnings([(d, intervals) for d, intervals in days if intervals]):
            if day < start_date or day > end_date:
                continue
            warnings.append(
                {
                    "worker_id": worker_id,
                    "date": day,
                    "code": "rest_under_12h",
                    "message": "Descanso inferior a 12 horas desde el turno del día anterior",
                }
            )
        for day, intervals in days:
            if day < start_date or day > end_date or not intervals:
                continue
            if day in absences_by_worker.get(worker_id, set()):
                warnings.append(
                    {
                        "worker_id": worker_id,
                        "date": day,
                        "code": "absence_overlap",
                        "message": "Hay una ausencia aprobada este día",
                    }
                )
    return warnings


def _days_with_intervals(day: str, shifts: List[dict]) -> Tuple[str, List[Tuple[int, int]]]:
    return day, [absolute_interval(day, s["start_time"], s["end_time"]) for s in shifts]


async def get_roster(company_id: str, start_date: str, end_date: str, work_center_id: Optional[str] = None) -> dict:
    """Full roster payload: active workers, draft shifts, minutes, warnings and pending count."""
    check_range(start_date, end_date)

    workers = await db.Workers.find({"company_ids": company_id, "deleted_at": None}).to_list(length=None)
    if work_center_id:
        workers = [w for w in workers if (w.get("work_center_assignments") or {}).get(company_id) == work_center_id]

    prev_day = (date.fromisoformat(start_date) - timedelta(days=1)).isoformat()
    docs = await db.PlannedShifts.find(
        {"company_id": company_id, "date": {"$gte": prev_day, "$lte": end_date}}
    ).to_list(length=None)
    docs_by_worker: Dict[str, Dict[str, List[dict]]] = {}
    for doc in docs:
        docs_by_worker.setdefault(doc["worker_id"], {})[doc["date"]] = doc

    pending = 0
    for doc in docs:
        if doc["date"] < start_date:
            continue
        if cell_signature(doc.get("shifts", [])) != cell_signature(doc.get("published") or []):
            pending += 1

    roster_workers = []
    days_per_worker: Dict[str, List[Tuple[str, List[dict]]]] = {}
    for worker in workers:
        worker_days = docs_by_worker.get(str(worker["_id"]), {})
        ordered_days = sorted((d, doc) for d, doc in worker_days.items())
        planned_total = 0
        cells: Dict[str, List[dict]] = {}
        pending_dates: List[str] = []
        for day, doc in ordered_days:
            if day == prev_day:
                continue
            shifts = doc.get("shifts", [])
            cells[day] = shifts
            if cell_signature(shifts) != cell_signature(doc.get("published") or []):
                pending_dates.append(day)
            for s in shifts:
                planned_total += planned_minutes(s["start_time"], s["end_time"], int(s.get("break_minutes", 0)))
            days_per_worker.setdefault(str(worker["_id"]), []).append(_days_with_intervals(day, shifts))
        if prev_day in worker_days:
            days_per_worker.setdefault(str(worker["_id"]), []).insert(
                0, _days_with_intervals(prev_day, worker_days[prev_day].get("shifts", []))
            )
        roster_workers.append(
            {
                "id": str(worker["_id"]),
                "first_name": worker.get("first_name", ""),
                "last_name": worker.get("last_name", ""),
                "cells": {day: cells[day] for day in sorted(cells)},
                "planned_minutes": planned_total,
                "pending_dates": pending_dates,
            }
        )

    worker_ids = [str(w["_id"]) for w in workers]
    absences_by_worker = await accepted_absence_dates(company_id, worker_ids, start_date, end_date)
    warnings = compute_warnings(days_per_worker, absences_by_worker, start_date, end_date)

    return {
        "company_id": company_id,
        "start_date": start_date,
        "end_date": end_date,
        "pending_changes": pending,
        "workers": roster_workers,
        "warnings": warnings,
    }


# Publish -----------------------------------------------------------------------


def _affected_workers(changes: List[dict]) -> Dict[str, List[str]]:
    """worker_id -> sorted list of changed dates from a changes list."""
    by_worker: Dict[str, set] = {}
    for change in changes:
        by_worker.setdefault(change["worker_id"], set()).add(change["date"])
    return {wid: sorted(days) for wid, days in by_worker.items()}


async def publish(
    company_id: str,
    start_date: str,
    end_date: str,
    acknowledge_warnings: bool,
    notify: bool,
    published_by: str,
) -> dict:
    """
    Apply drafts to the published view for the whole company in the range.

    Returns ``{publication_id, changed_cells, changes}``; when there was
    nothing to publish, ``publication_id`` is None (spec: no record, no
    emails). Raises ``shift_plan.warnings_not_acknowledged`` when the range
    has warnings and they were not confirmed.
    """
    check_range(start_date, end_date)

    warnings = await compute_range_warnings(company_id, start_date, end_date)
    if warnings and not acknowledge_warnings:
        raise_api_error(
            status_code=status.HTTP_409_CONFLICT,
            error_code="shift_plan.warnings_not_acknowledged",
            message="El rango tiene avisos de planificación; confírmalos para publicar",
        )

    docs = await db.PlannedShifts.find(
        {"company_id": company_id, "date": {"$gte": start_date, "$lte": end_date}}
    ).to_list(length=None)

    changes: List[dict] = []
    to_delete = []
    for doc in docs:
        draft = doc.get("shifts", [])
        published_before = doc.get("published") or []
        if cell_signature(draft) == cell_signature(published_before):
            continue
        changes.append(
            {"worker_id": doc["worker_id"], "date": doc["date"], "before": published_before, "after": draft}
        )
        if not draft:
            to_delete.append(doc["_id"])
        else:
            await db.PlannedShifts.update_one(
                {"_id": doc["_id"]},
                {"$set": {"published": draft, "removed": False}},
            )

    if not changes:
        return {"publication_id": None, "changed_cells": 0, "changes": []}

    publication = {
        "company_id": company_id,
        "start_date": start_date,
        "end_date": end_date,
        "published_at": datetime.utcnow().isoformat(),
        "published_by": published_by,
        # Decided before inserting: the record is immutable once created
        "notified": notify,
        "changes": changes,
        "acknowledged_warnings": warnings if acknowledge_warnings else [],
    }
    result = await db.ShiftPlanPublications.insert_one(publication)

    for oid in to_delete:
        await db.PlannedShifts.delete_one({"_id": oid})

    return {
        "publication_id": str(result.inserted_id),
        "changed_cells": len(changes),
        "changes": changes,
    }


async def compute_range_warnings(company_id: str, start_date: str, end_date: str) -> List[dict]:
    """
    Warnings for a publish. Days inside the range use the draft (what is about
    to go live). The day before uses ``published``: a cleared draft must not
    hide a night shift the worker still has, and an unpublished draft must not
    invent a warning. The roster grid still warns from drafts.
    """
    prev_day = (date.fromisoformat(start_date) - timedelta(days=1)).isoformat()
    docs = await db.PlannedShifts.find(
        {"company_id": company_id, "date": {"$gte": prev_day, "$lte": end_date}}
    ).to_list(length=None)
    days_per_worker: Dict[str, Dict[str, List[dict]]] = {}
    for doc in docs:
        if doc["date"] < start_date:
            shifts = doc.get("published") or []
        else:
            shifts = doc.get("shifts") or []
        if shifts:
            days_per_worker.setdefault(doc["worker_id"], {})[doc["date"]] = shifts

    worker_ids = list(days_per_worker)
    absences_by_worker = await accepted_absence_dates(company_id, worker_ids, start_date, end_date)
    grouped = {
        wid: [_days_with_intervals(day, shifts) for day, shifts in sorted(days.items())]
        for wid, days in days_per_worker.items()
    }
    return compute_warnings(grouped, absences_by_worker, start_date, end_date)


# Copy week --------------------------------------------------------------------


async def copy_week(company_id: str, source_week_start: str, target_week_start: str, conflict: str) -> dict:
    """
    Cellwise copy of draft shifts from one Monday-anchored week to another.

    ``skip`` leaves target cells that already have drafts; ``overwrite``
    replaces them and also clears target cells whose source cell is empty
    (counted in ``copied``). The whole resulting week is validated for overlaps
    before anything is written. Deactivated or departed workers are excluded.
    Results stay draft (they only reach the worker on the next publish).
    """
    _require_monday(source_week_start)
    _require_monday(target_week_start)
    source_days = week_dates(source_week_start)
    target_days = week_dates(target_week_start)
    check_range(source_week_start, source_days[-1])
    if set(source_days) & set(target_days):
        raise_api_error(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="shift_plan.overlapping_weeks",
            message="La semana origen y la destino no pueden solaparse",
        )

    source_docs = await db.PlannedShifts.find(
        {"company_id": company_id, "date": {"$gte": source_days[0], "$lte": source_days[-1]}}
    ).to_list(length=None)
    target_docs = await db.PlannedShifts.find(
        {"company_id": company_id, "date": {"$gte": target_days[0], "$lte": target_days[-1]}},
        {"worker_id": 1, "date": 1, "shifts": 1},
    ).to_list(length=None)
    before_target = (date.fromisoformat(target_days[0]) - timedelta(days=1)).isoformat()
    after_target = (date.fromisoformat(target_days[-1]) + timedelta(days=1)).isoformat()
    edge_docs = await db.PlannedShifts.find(
        {"company_id": company_id, "date": {"$in": [before_target, after_target]}}
    ).to_list(length=None)

    source_by_cell = {(d["worker_id"], d["date"]): d.get("shifts", []) for d in source_docs}
    target_by_cell = {(d["worker_id"], d["date"]): d.get("shifts", []) for d in target_docs}
    edge_by_worker: Dict[str, List[Tuple[str, List[dict]]]] = {}
    for d in edge_docs:
        edge_by_worker.setdefault(d["worker_id"], []).append((d["date"], d.get("shifts", [])))

    # Plan every write first and validate each worker's resulting week
    # (plus the days around it) so a conflict leaves the target untouched.
    writes: List[Tuple[str, str, List[dict]]] = []
    copied = skipped = 0
    for worker_id in sorted({w for w, _ in source_by_cell} | {w for w, _ in target_by_cell}):
        worker = await find_worker(worker_id)
        if worker is None or worker.get("deleted_at") is not None or company_id not in (worker.get("company_ids") or []):
            continue
        final_week: Dict[str, List[dict]] = {}
        for source_day, target_day in zip(source_days, target_days):
            source_shifts = source_by_cell.get((worker_id, source_day), [])
            existing = target_by_cell.get((worker_id, target_day), [])
            if conflict == "skip" and existing:
                skipped += 1 if source_shifts else 0
                final_week[target_day] = existing
            elif source_shifts or (conflict == "overwrite" and existing):
                # Copy the stored dicts. Re-resolving the type fails once that
                # type is soft-deleted, and the cell already holds the hours (D2).
                resolved = [dict(s) for s in source_shifts]
                writes.append((worker_id, target_day, resolved))
                final_week[target_day] = resolved
                copied += 1
            else:
                final_week[target_day] = existing
        intervals = [
            absolute_interval(day, s["start_time"], s["end_time"])
            for day, shifts in list(final_week.items()) + edge_by_worker.get(worker_id, [])
            for s in shifts
        ]
        raise_if_overlap(intervals)

    for worker_id, target_day, shifts in writes:
        await store_cell(company_id, worker_id, target_day, shifts)

    return {"copied": copied, "skipped": skipped}
