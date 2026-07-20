"""
shift_state.py - Atomic CAS guard for worker shift state transitions.

Implements a compare-and-swap on the WorkerShiftStates collection so that
concurrent clock-in requests from multiple API replicas or double-taps cannot
both succeed. The denormalized `entry_time` and `open_pause` fields eliminate
any subsequent read of TimeRecords on the critical path (see plan §7).
"""
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException, status
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from ..database import db

# (expected_state, new_state) per action
_TRANSITIONS: dict[str, tuple[str, str]] = {
    "entry":       ("logged_out", "logged_in"),
    "pause_start": ("logged_in",  "on_pause"),
    "pause_end":   ("on_pause",   "logged_in"),
    "exit":        ("logged_in",  "logged_out"),
}


async def transition_shift_state(
    worker_id: str,
    company_id: str,
    action: str,
    now: datetime,
    pause_info: dict | None = None,
) -> tuple[str, dict]:
    """
    Atomic CAS on shift state with denormalization.

    - Writes entry_time (entry) or open_pause (pause_start) into the state doc.
    - Returns (version, prev_doc) where prev_doc is the image BEFORE the CAS
      (used to read entry_time / open_pause atomically, without touching the log).

    Raises HTTPException(409) if the transition is invalid or the CAS is lost
    (another concurrent request already advanced the state).
    """
    expected, new_state = _TRANSITIONS[action]
    version = str(uuid4())

    set_fields: dict = {"state": new_state, "version": version, "updated_at": now}
    unset_fields: dict = {}

    if action == "entry":
        set_fields["entry_time"] = now
    elif action == "pause_start":
        assert pause_info is not None  # guaranteed by caller
        set_fields["open_pause"] = {
            "pause_start_time":     now,
            "pause_type_id":        pause_info["pause_type_id"],
            "pause_type_name":      pause_info["pause_type_name"],
            "pause_counts_as_work": pause_info["pause_counts_as_work"],
        }
    elif action == "pause_end":
        unset_fields["open_pause"] = ""
    elif action == "exit":
        unset_fields["entry_time"] = ""
        unset_fields["open_pause"] = ""

    update: dict = {"$set": set_fields}
    if unset_fields:
        update["$unset"] = unset_fields

    # Step 1 (entry only): ensure the doc exists with state="logged_out".
    # The $setOnInsert upsert is deterministic: it only writes if the document
    # does not yet exist, so two concurrent first-clock-ins race on the unique
    # index — the loser gets DuplicateKeyError (ignored); the CAS below then
    # resolves the 409 for that loser.
    if expected == "logged_out":
        try:
            await db.WorkerShiftStates.update_one(
                {"worker_id": worker_id, "company_id": company_id},
                {"$setOnInsert": {"state": "logged_out", "updated_at": now}},
                upsert=True,
            )
        except DuplicateKeyError:
            pass

    # Step 2: atomic CAS. ReturnDocument.BEFORE so we read entry_time / open_pause
    # from the same atomic operation — no subsequent log read needed.
    prev = await db.WorkerShiftStates.find_one_and_update(
        {"worker_id": worker_id, "company_id": company_id, "state": expected},
        update,
        upsert=False,
        return_document=ReturnDocument.BEFORE,
    )

    if prev is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=await _conflict_message(worker_id, company_id, action),
        )

    return version, prev


async def _conflict_message(worker_id: str, company_id: str, action: str) -> str:
    """
    Compose a human-readable 409 message based on the CURRENT state.

    This read is safe for messaging — it is never used for logic decisions.
    """
    doc = await db.WorkerShiftStates.find_one(
        {"worker_id": worker_id, "company_id": company_id}
    )
    current = doc["state"] if doc else "logged_out"

    table: dict[tuple[str, str], str] = {
        ("entry",       "logged_in"):   "Ya tienes una jornada activa. Ciérrala antes de registrar una nueva entrada.",
        ("entry",       "on_pause"):    "Tienes una pausa abierta. Finalízala y cierra la jornada antes de una nueva entrada.",
        ("pause_start", "logged_out"):  "No puedes iniciar una pausa: no tienes una jornada activa.",
        ("pause_start", "on_pause"):    "Ya tienes una pausa en curso. Finalízala antes de iniciar otra.",
        ("pause_end",   "logged_in"):   "No hay ninguna pausa en curso que finalizar.",
        ("pause_end",   "logged_out"):  "No puedes finalizar una pausa: no tienes una jornada activa.",
        ("exit",        "logged_out"):  "No puedes registrar una salida: no tienes una jornada activa.",
        ("exit",        "on_pause"):    "Tienes una pausa abierta. Finalízala antes de cerrar la jornada.",
    }
    return table.get((action, current), "Acción no válida para el estado actual de la jornada.")


async def _revert_shift_state(
    worker_id: str,
    company_id: str,
    prev_doc: dict,
    version: str,
) -> None:
    """
    Restore the state document to its pre-CAS image after a failed insert_one.

    Filters by `version` (fencing token): if another request has already
    transitioned (version changed), the filter does not match → no-op → correct
    (we must not overwrite a legitimately advanced state).

    Does not re-raise; the caller logs CRITICAL if modified_count == 0 and
    the insert also failed (double-failure case).
    """
    restore = {k: v for k, v in prev_doc.items() if k != "_id"}
    await db.WorkerShiftStates.replace_one(
        {"worker_id": worker_id, "company_id": company_id, "version": version},
        restore,
    )
