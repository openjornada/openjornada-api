"""
Server-side resolution of the timezone that delimits a worker's month.

The window of the month a worker views and signs must not depend on the device
placing the call: two consecutive months signed from different zones would
leave a boundary punch covered by no signature at all. Both surfaces therefore
resolve the zone from the worker's own record instead of trusting the request.
"""

import logging

import pytz

logger = logging.getLogger(__name__)

DEFAULT_REPORT_TIMEZONE = "Europe/Madrid"


def resolve_worker_timezone(worker: dict) -> str:
    """
    IANA timezone delimiting the local calendar month of a worker's own
    monthly report, export and signature.

    Args:
        worker: Raw MongoDB worker document.

    Returns:
        The worker's ``default_timezone`` when it is a deliberate choice this
        pytz build knows about, ``DEFAULT_REPORT_TIMEZONE`` otherwise.
    """
    stored = (worker.get("default_timezone") or "").strip()

    # "UTC" is the WorkerModel default (api/models/workers.py:15), not a
    # choice: it is only set on purpose by the bulk import
    # (api/routers/workers.py:303) or by editing the worker. Honouring it would
    # move the month window of every existing worker away from the
    # Europe/Madrid the reports have always used.
    if not stored or stored == "UTC":
        return DEFAULT_REPORT_TIMEZONE

    if stored not in pytz.all_timezones_set:
        # Zone from a tzdata newer than the bundled pytz (or a typo): degrade
        # instead of leaving the worker unable to view or sign their month.
        logger.warning(
            "Unknown default_timezone %r on worker %s; falling back to %s",
            stored, worker.get("_id"), DEFAULT_REPORT_TIMEZONE,
        )
        return DEFAULT_REPORT_TIMEZONE

    return stored
