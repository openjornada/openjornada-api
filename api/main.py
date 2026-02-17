from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import os
import logging
from dotenv import load_dotenv

from .database import init_db, init_default_settings
from .routers import workers, time_records, auth, incidents, settings, companies, pause_types, change_requests, gdpr, backups, reports
from .services.scheduler_service import scheduler_service

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s: %(message)s'
)

app = FastAPI(
    title="Time Tracking API",
    description="API for tracking workers' time entries",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    root_path=os.getenv("ROOT_PATH", "")
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


@app.on_event("startup")
async def startup():
    await init_db()
    await init_default_settings()


@app.get("/", tags=["Health"])
async def health_check():
    return {"status": "healthy"}


if __name__ == "__main__":
    uvicorn.run("app.main:app", 
                host=os.getenv("API_HOST", "0.0.0.0"), 
                port=int(os.getenv("API_PORT", 8000)), 
                reload=os.getenv("DEBUG", "False").lower() == "true")
