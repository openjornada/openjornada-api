"""
Roster planning endpoints: cell write, grid read, publish (+emails), copy
week, and the worker's own published shifts.

Every operation is gated by ``shift_planning_gating`` (404 unknown company,
403 module disabled). Admin routes need ``view_shift_plans`` (reads) or
``manage_shift_plans`` (writes/publish/copy); ``/shift-plans/me`` is the
worker self-service route (``view_own_shift_plans``, worker credentials in the
body, same pattern as the worker absence endpoints).
"""
import logging
import os
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status

from ..auth.permissions import PermissionChecker
from ..database import db
from ..models.auth import APIUser
from ..models.shift_planning import (
    CellShift,
    CellWriteRequest,
    CopyWeekRequest,
    CopyWeekResult,
    MyShiftPlanRequest,
    MyShiftPlanResponse,
    RosterResponse,
    ShiftPlanPublishRequest,
)
from ..services import shift_planning
from ..services.email_service import EmailService
from ..services.shift_planning_gating import ensure_shift_planning_module_enabled
from ..utils.company_locale import resolve_company_locale
from ..utils.worker_auth import _authenticate_worker, _verify_worker_company_access

logger = logging.getLogger(__name__)
router = APIRouter()


@router.put("/cell", response_model=List[dict], status_code=status.HTTP_200_OK)
async def replace_cell(
    body: CellWriteRequest,
    _user: APIUser = Depends(PermissionChecker("manage_shift_plans")),
):
    """
    Replace the full shift list of one worker/day cell (D3).

    Returns the stored (draft) shifts; the grid re-reads the roster itself.
    """
    await ensure_shift_planning_module_enabled(body.company_id)
    return await shift_planning.write_cell(body.company_id, body.worker_id, body.date, body.shifts)


@router.get("", response_model=RosterResponse)
async def get_roster(
    company_id: str = Query(...),
    start_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    work_center_id: Optional[str] = Query(None),
    _user: APIUser = Depends(PermissionChecker("view_shift_plans")),
):
    await ensure_shift_planning_module_enabled(company_id)
    return await shift_planning.get_roster(company_id, start_date, end_date, work_center_id)


@router.post("/publish")
async def publish_roster(
    body: ShiftPlanPublishRequest,
    background_tasks: BackgroundTasks,
    current_user: APIUser = Depends(PermissionChecker("manage_shift_plans")),
):
    """
    Publish drafts for the whole company in the range. Emails (one per
    affected worker) are queued outside the request cycle and a send failure
    never reverts the publish (D10).
    """
    await ensure_shift_planning_module_enabled(body.company_id)

    result = await shift_planning.publish(
        company_id=body.company_id,
        start_date=body.start_date,
        end_date=body.end_date,
        acknowledge_warnings=body.acknowledge_warnings,
        notify=body.notify,
        published_by=current_user.username,
    )

    if body.notify and result["publication_id"]:
        background_tasks.add_task(
            _send_publication_emails,
            body.company_id,
            sorted({change["worker_id"] for change in result["changes"]}),
            body.start_date,
            body.end_date,
        )

    result.pop("changes", None)
    return result


@router.post("/copy-week", response_model=CopyWeekResult)
async def copy_week(
    body: CopyWeekRequest,
    _user: APIUser = Depends(PermissionChecker("manage_shift_plans")),
):
    await ensure_shift_planning_module_enabled(body.company_id)
    return await shift_planning.copy_week(
        body.company_id, body.source_week_start, body.target_week_start, body.conflict
    )


@router.post("/me", response_model=MyShiftPlanResponse)
async def my_shift_plan(
    body: MyShiftPlanRequest,
    _user: APIUser = Depends(PermissionChecker("view_own_shift_plans")),
):
    """Worker self-query: published shifts only, for one of their companies."""
    worker = await _authenticate_worker(body.email, body.password)
    _verify_worker_company_access(worker, body.company_id)
    company = await ensure_shift_planning_module_enabled(body.company_id)
    shift_planning.check_range(body.start_date, body.end_date)

    docs = await db.PlannedShifts.find(
        {
            "company_id": body.company_id,
            "worker_id": str(worker["_id"]),
            "date": {"$gte": body.start_date, "$lte": body.end_date},
        }
    ).to_list(length=None)

    days = [
        {"date": doc["date"], "shifts": doc["published"]}
        for doc in sorted(docs, key=lambda d: d["date"])
        if doc.get("published")
    ]
    return MyShiftPlanResponse(
        company_id=body.company_id,
        company_name=company.get("name", ""),
        start_date=body.start_date,
        end_date=body.end_date,
        days=days,
    )


# Helpers ----------------------------------------------------------------------


async def _send_publication_emails(
    company_id: str,
    worker_ids: list,
    start_date: str,
    end_date: str,
) -> None:
    """
    One email per affected worker listing their PUBLISHED shifts in the range
    (not only the changed cells; a worker whose cells were all removed gets
    an empty list). Any failure is logged only: the publication stands.
    """
    try:
        company = await ensure_shift_planning_module_enabled(company_id)
        locale = await resolve_company_locale(company_id)
        contact_email = os.getenv("SMTP_FROM_EMAIL", "support@openjornada.es")
        email_service = EmailService()
        for worker_id in worker_ids:
            worker = await shift_planning.find_worker(worker_id)
            if worker is None or worker.get("deleted_at") is not None or not worker.get("email"):
                continue
            docs = await db.PlannedShifts.find(
                {
                    "company_id": company_id,
                    "worker_id": worker_id,
                    "date": {"$gte": start_date, "$lte": end_date},
                }
            ).sort("date", 1).to_list(length=None)
            await email_service.send_shift_plan_published_email(
                to_email=worker["email"],
                worker_name=worker.get("first_name", ""),
                company_name=company.get("name", ""),
                days=[{"date": d["date"], "shifts": d["published"]} for d in docs if d.get("published")],
                start_date=start_date,
                end_date=end_date,
                contact_email=contact_email,
                locale=locale,
            )
    except Exception:
        logger.exception("Shift plan publication emails failed for company %s", company_id)
