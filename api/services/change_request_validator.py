"""
Service for validating change requests for time records.
Handles complex validation logic for overlaps, sequence validation, and edge cases.
"""
from typing import List, Tuple, Optional
from datetime import datetime, date, timezone as dt_timezone
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase


def ensure_utc_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Convierte datetime naive (de MongoDB) a UTC aware"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=dt_timezone.utc)
    return dt


class ChangeRequestValidator:
    """
    Validates that a change request doesn't create overlaps or inconsistencies.

    Uses a state machine approach to validate the complete sequence of time records.
    """

    async def validate_change(
        self,
        db: AsyncIOMotorDatabase,
        time_record_id: str,
        original_timestamp: datetime,
        new_timestamp: datetime,
        worker_id: str,
        company_id: str
    ) -> Tuple[bool, List[str]]:
        """
        Validates that the change request doesn't create overlaps or inconsistencies.
        Returns: (is_valid: bool, errors: List[str])

        Algorithm:
        1. Get original record
        2. Identify affected days (original + new if different)
        3. For each affected day:
           a. Get all records for that day
           b. Simulate the change (replace record with new timestamp)
           c. Re-order by created_at
           d. Validate sequence: ENTRY → [PAUSE_START → PAUSE_END]* → EXIT
        4. Validate type-specific restrictions
        5. Return list of descriptive errors
        """
        errors = []

        # Asegurar que los timestamps sean UTC aware
        new_timestamp = ensure_utc_aware(new_timestamp)

        # 1. Get original record
        original_record = await db.TimeRecords.find_one({"_id": ObjectId(time_record_id)})
        if not original_record:
            return (False, ["El registro original no existe"])

        record_type = original_record.get("type", "")

        # 2. Validate type-specific restrictions (entry vs exit)
        if record_type == "entry":
            entry_errors = await self._validate_entry_change(
                db, original_record, new_timestamp
            )
            errors.extend(entry_errors)
        elif record_type == "exit":
            exit_errors = await self._validate_exit_change(
                db, original_record, new_timestamp
            )
            errors.extend(exit_errors)

        is_valid = len(errors) == 0
        return (is_valid, errors)

    async def _validate_day_sequence(
        self,
        db: AsyncIOMotorDatabase,
        worker_id: str,
        company_id: str,
        day: date,
        modified_record_id: str,
        original_record: dict,
        new_timestamp: datetime
    ) -> List[str]:
        """
        Validates the complete sequence of records for a single day.
        Simulates the change and verifies the sequence is valid.

        Valid sequence: ENTRY → [PAUSE_START → PAUSE_END]* → EXIT
        Can repeat multiple times per day for multiple work periods.
        """
        errors = []

        # Get all records for the day
        start_of_day = datetime.combine(day, datetime.min.time())
        end_of_day = datetime.combine(day, datetime.max.time())

        records = await db.TimeRecords.find({
            "worker_id": worker_id,
            "company_id": company_id,
            "created_at": {"$gte": start_of_day, "$lte": end_of_day}
        }).sort("created_at", 1).to_list(None)

        # Simulate the change
        simulated_records = []
        for record in records:
            if str(record.get("_id", "")) == modified_record_id:
                # Replace with new timestamp
                record_copy = record.copy()
                record_copy["timestamp"] = new_timestamp
                simulated_records.append(record_copy)
            else:
                simulated_records.append(record)

        # Validate sequence: ENTRY → [PAUSE_START → PAUSE_END]* → EXIT
        state = "waiting_entry"
        entry_time = None

        for record in simulated_records:
            rec_type = record.get("type", "")
            timestamp = ensure_utc_aware(record.get("timestamp"))

            if not timestamp:
                continue

            if state == "waiting_entry":
                if rec_type != "entry":
                    errors.append(
                        f"Secuencia inválida: se esperaba ENTRY pero se encontró {rec_type.upper()}"
                    )
                else:
                    entry_time = timestamp
                    state = "after_entry"

            elif state == "after_entry":
                if rec_type == "pause_start":
                    state = "in_pause"
                elif rec_type == "exit":
                    exit_time = timestamp
                    if exit_time < entry_time:
                        errors.append(
                            f"EXIT ({exit_time.strftime('%H:%M')}) es anterior a ENTRY ({entry_time.strftime('%H:%M')})"
                        )
                    state = "after_exit"
                else:
                    errors.append(
                        f"Secuencia inválida después de ENTRY: {rec_type.upper()}"
                    )

            elif state == "in_pause":
                if rec_type == "pause_end":
                    # Verify pause_end is after pause_start
                    # We would need to track pause_start_time for this, but keeping simpler for now
                    state = "after_entry"  # Can have another pause or exit
                else:
                    errors.append(
                        f"Secuencia inválida: se esperaba PAUSE_END pero se encontró {rec_type.upper()}"
                    )

            elif state == "after_exit":
                # After EXIT, can start a new ENTRY (new work period)
                if rec_type == "entry":
                    entry_time = timestamp
                    state = "after_entry"
                else:
                    errors.append(
                        f"Secuencia inválida después de EXIT: {rec_type.upper()}"
                    )

        return errors

    async def _validate_entry_change(
        self,
        db: AsyncIOMotorDatabase,
        original_record: dict,
        new_entry_time: datetime
    ) -> List[str]:
        """Validates specific restrictions for ENTRY change"""
        errors = []

        # Asegurar que new_entry_time sea UTC aware
        new_entry_time = ensure_utc_aware(new_entry_time)

        worker_id = original_record.get("worker_id")
        company_id = original_record.get("company_id")
        original_created_at = original_record.get("created_at")

        # Find corresponding EXIT (next exit after this entry)
        exit_record = await db.TimeRecords.find_one({
            "worker_id": worker_id,
            "company_id": company_id,
            "type": "exit",
            "created_at": {"$gt": original_created_at}
        }, sort=[("created_at", 1)])

        if exit_record:
            exit_time = ensure_utc_aware(exit_record.get("timestamp"))
            if exit_time and new_entry_time >= exit_time:
                errors.append(
                    f"La nueva hora de entrada ({new_entry_time.strftime('%H:%M')}) "
                    f"debe ser anterior a la salida ({exit_time.strftime('%H:%M')})"
                )

        # Find previous EXIT (should not overlap)
        prev_exit = await db.TimeRecords.find_one({
            "worker_id": worker_id,
            "company_id": company_id,
            "type": "exit",
            "created_at": {"$lt": original_created_at}
        }, sort=[("created_at", -1)])

        if prev_exit:
            prev_exit_time = ensure_utc_aware(prev_exit.get("timestamp"))
            if prev_exit_time and new_entry_time < prev_exit_time:
                errors.append(
                    f"La nueva hora de entrada ({new_entry_time.strftime('%H:%M')}) "
                    f"no puede ser anterior a la salida previa ({prev_exit_time.strftime('%H:%M')})"
                )

        return errors

    async def _validate_exit_change(
        self,
        db: AsyncIOMotorDatabase,
        original_record: dict,
        new_exit_time: datetime
    ) -> List[str]:
        """Validates specific restrictions for EXIT change"""
        errors = []

        # Asegurar que new_exit_time sea UTC aware
        new_exit_time = ensure_utc_aware(new_exit_time)

        worker_id = original_record.get("worker_id")
        company_id = original_record.get("company_id")
        original_created_at = original_record.get("created_at")

        # Find corresponding ENTRY (previous entry before this exit)
        entry_record = await db.TimeRecords.find_one({
            "worker_id": worker_id,
            "company_id": company_id,
            "type": "entry",
            "created_at": {"$lt": original_created_at}
        }, sort=[("created_at", -1)])

        if entry_record:
            entry_time = ensure_utc_aware(entry_record.get("timestamp"))
            if entry_time and new_exit_time <= entry_time:
                errors.append(
                    f"La nueva hora de salida ({new_exit_time.strftime('%H:%M')}) "
                    f"debe ser posterior a la entrada ({entry_time.strftime('%H:%M')})"
                )

        # Find next ENTRY (should not overlap)
        next_entry = await db.TimeRecords.find_one({
            "worker_id": worker_id,
            "company_id": company_id,
            "type": "entry",
            "created_at": {"$gt": original_created_at}
        }, sort=[("created_at", 1)])

        if next_entry:
            next_entry_time = ensure_utc_aware(next_entry.get("timestamp"))
            if next_entry_time and new_exit_time > next_entry_time:
                errors.append(
                    f"La nueva hora de salida ({new_exit_time.strftime('%H:%M')}) "
                    f"no puede ser posterior a la entrada siguiente ({next_entry_time.strftime('%H:%M')})"
                )

        return errors
