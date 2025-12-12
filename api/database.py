import motor.motor_asyncio
import os
from dotenv import load_dotenv
from bson import ObjectId

load_dotenv()

MONGO_URL = os.getenv("MONGODB_URL") or os.getenv("MONGO_URL", "mongodb://mongodb:27017")
DB_NAME = os.getenv("DATABASE_NAME") or os.getenv("DB_NAME", "time_tracking_db")

client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

def convert_id(obj):
    """Convert MongoDB _id to string id field"""
    if obj and "_id" in obj:
        obj["id"] = str(obj["_id"])
        del obj["_id"]
    return obj

async def init_db():
    try:
        # Create indexes for Workers
        await db.Workers.create_index("email", unique=True)
        await db.Workers.create_index("id_number", unique=True)
        await db.Workers.create_index("reset_token")  # For password reset lookup

        # Create indexes for APIUsers
        await db.APIUsers.create_index("username", unique=True)
        await db.APIUsers.create_index("email", unique=True)

        # Create indexes for Incidents (for performance)
        await db.Incidents.create_index("worker_id")
        await db.Incidents.create_index("status")
        await db.Incidents.create_index("created_at")

        # Create indexes for Companies
        await db.Companies.create_index("name", unique=True)

        # Create indexes for TimeRecords
        await db.TimeRecords.create_index("worker_id")
        await db.TimeRecords.create_index("company_id")
        await db.TimeRecords.create_index("created_at")
        await db.TimeRecords.create_index([("worker_id", 1), ("company_id", 1)])
        await db.TimeRecords.create_index([("worker_id", 1), ("company_id", 1), ("created_at", 1)])

        # Create indexes for ChangeRequests
        await db.ChangeRequests.create_index("worker_id")
        await db.ChangeRequests.create_index("status")
        await db.ChangeRequests.create_index("created_at")
        await db.ChangeRequests.create_index(
            [("worker_id", 1), ("status", 1)],
            unique=True,
            partialFilterExpression={"status": "pending"}
        )

    except Exception as e:
        print(f"Error initializing database: {e}")


async def init_default_settings():
    """Create default settings if they don't exist"""
    try:
        existing = await db.Settings.find_one()
        if not existing:
            default_settings = {
                "contact_email": "support@opentracker.local",
                "webapp_url": "http://localhost:5173"
            }
            await db.Settings.insert_one(default_settings)
            print("Default settings created")
    except Exception as e:
        print(f"Error initializing default settings: {e}")
