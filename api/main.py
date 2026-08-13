from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import os
import logging
from dotenv import load_dotenv
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from .database import init_db, init_default_settings
from .routers import workers, time_records, auth, incidents, settings, companies, pause_types, change_requests, gdpr, backups, reports
from .routers import sms, subscription, absences, absence_policies
from .services.scheduler_service import scheduler_service
from .services.sms_service import sms_service
from .utils.rate_limit import limiter

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s: %(message)s'
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await init_default_settings()
    await sms_service.initialize()
    await scheduler_service.start()
    yield
    scheduler_service.stop()
    await sms_service.close()


DEFAULT_DEV_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:3000",
    "http://localhost:3001",
]


def get_cors_allowed_origins() -> list[str]:
    """Return the CORS allowlist from `CORS_ALLOWED_ORIGINS` (comma-separated),
    falling back to a local-development default. Never returns `["*"]`."""
    origins_env = os.getenv("CORS_ALLOWED_ORIGINS")
    if origins_env:
        return [origin.strip() for origin in origins_env.split(",") if origin.strip()]
    return DEFAULT_DEV_CORS_ORIGINS


app = FastAPI(
    title="Time Tracking API",
    description="API for tracking workers' time entries",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    root_path=os.getenv("ROOT_PATH", ""),
    lifespan=lifespan
)

# Rate limiting (login brute-force protection)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth.router, prefix="/api", tags=["Authentication"])
app.include_router(companies.router, prefix="/api", tags=["Companies"])
app.include_router(workers.router, prefix="/api", tags=["Workers"])
app.include_router(time_records.router, prefix="/api", tags=["Time Records"])
app.include_router(pause_types.router, prefix="/api", tags=["Pause Types"])
app.include_router(incidents.router, prefix="/api/incidents", tags=["Incidents"])
app.include_router(change_requests.router, prefix="/api/change-requests", tags=["Change Requests"])
app.include_router(settings.router, prefix="/api", tags=["Settings"])
app.include_router(backups.router, prefix="/api", tags=["Backups"])
app.include_router(reports.router, prefix="/api", tags=["Reports & Inspection"])
app.include_router(gdpr.router, tags=["GDPR"])
app.include_router(sms.router, prefix="/api", tags=["SMS"])
app.include_router(subscription.router, prefix="/api", tags=["Subscription"])
app.include_router(absences.router, prefix="/api/absences", tags=["Absences"])
app.include_router(absence_policies.router, prefix="/api/absence-policies", tags=["Absence Policies"])


@app.get("/", tags=["Health"])
async def health_check():
    return {"status": "healthy"}


if __name__ == "__main__":
    uvicorn.run("app.main:app",
                host=os.getenv("API_HOST", "0.0.0.0"),
                port=int(os.getenv("API_PORT", 8000)),
                reload=os.getenv("DEBUG", "False").lower() == "true")
