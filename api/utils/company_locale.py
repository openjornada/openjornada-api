import logging
from typing import Optional
from bson.objectid import ObjectId
from ..database import db
from ..models.i18n import resolve_notification_locale

logger = logging.getLogger(__name__)


async def resolve_company_locale(company_id: Optional[str]) -> str:
    """Notification locale of a company, tolerating missing/legacy documents."""
    company = None
    if company_id:
        try:
            company = await db.Companies.find_one({"_id": ObjectId(company_id)})
        except Exception as e:
            logger.warning(
                f"[COMPANY-LOCALE] Error resolving company {company_id}: "
                f"{type(e).__name__}: {e}. Falling back to default locale."
            )
            company = None
    return resolve_notification_locale(company)
