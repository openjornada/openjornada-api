from datetime import datetime, timezone as dt_timezone
from typing import List, Dict, Optional
from ..database import db
from bson import ObjectId
import logging

logger = logging.getLogger(__name__)

def ensure_utc_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Convierte datetime naive (de MongoDB) a UTC aware"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=dt_timezone.utc)
    return dt

class TimeCalculationService:
    """
    Servicio para calcular duración de jornadas considerando pausas.
    """

    @staticmethod
    async def calculate_duration_with_pauses(
        worker_id: str,
        company_id: str,
        entry_time: datetime,
        exit_time: datetime
    ) -> float:
        """
        Calcula el tiempo trabajado descontando pausas outside_shift.

        Args:
            worker_id: ID del trabajador
            company_id: ID de la empresa
            entry_time: Hora de entrada (UTC aware)
            exit_time: Hora de salida (UTC aware)

        Returns:
            Minutos trabajados efectivos (descontando pausas fuera de jornada)
        """
        # Asegurar que los tiempos sean aware para comparación
        entry_time = ensure_utc_aware(entry_time)
        exit_time = ensure_utc_aware(exit_time)

        # Tiempo total
        total_minutes = (exit_time - entry_time).total_seconds() / 60

        # Buscar todas las pausas en este período
        pause_records = await db.TimeRecords.find({
            "worker_id": worker_id,
            "company_id": company_id,
            "created_at": {"$gte": entry_time, "$lte": exit_time},
            "type": {"$in": ["pause_start", "pause_end"]}
        }).sort("created_at", 1).to_list(1000)

        if not pause_records:
            # Sin pausas, todo el tiempo cuenta
            return total_minutes

        # Agrupar pausas en períodos (pause_start → pause_end)
        pause_periods = []
        pause_start = None

        for record in pause_records:
            if record["type"] == "pause_start":
                pause_start = record
            elif record["type"] == "pause_end" and pause_start:
                pause_periods.append({
                    "start": ensure_utc_aware(pause_start["created_at"]),
                    "end": ensure_utc_aware(record["created_at"]),
                    "counts_as_work": pause_start.get("pause_counts_as_work", False)
                })
                pause_start = None

        # Calcular minutos de pausas outside_shift
        outside_shift_minutes = sum(
            (period["end"] - period["start"]).total_seconds() / 60
            for period in pause_periods
            if not period["counts_as_work"]
        )

        effective_minutes = total_minutes - outside_shift_minutes

        logger.debug(
            f"Duration calculation: total={total_minutes:.2f}min, "
            f"outside_pauses={outside_shift_minutes:.2f}min, "
            f"effective={effective_minutes:.2f}min"
        )

        return max(0, effective_minutes)

    @staticmethod
    async def get_open_pause(worker_id: str, company_id: str) -> Optional[Dict]:
        """
        Verifica si hay una pausa abierta (pause_start sin pause_end).

        Returns:
            Dict con info de la pausa abierta, o None si no hay
        """
        last_record = await db.TimeRecords.find_one(
            {
                "worker_id": worker_id,
                "company_id": company_id
            },
            sort=[("created_at", -1)]
        )

        if last_record and last_record["type"] == "pause_start":
            return {
                "pause_type_id": last_record.get("pause_type_id"),
                "pause_type_name": last_record.get("pause_type_name"),
                "pause_counts_as_work": last_record.get("pause_counts_as_work"),
                "started_at": last_record["created_at"]
            }

        return None
