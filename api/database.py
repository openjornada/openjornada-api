import motor.motor_asyncio
import os
from dotenv import load_dotenv

load_dotenv()

MONGO_URL = os.getenv("MONGODB_URL") or os.getenv("MONGO_URL", "mongodb://mongodb:27017")
DB_NAME = os.getenv("DATABASE_NAME") or os.getenv("DB_NAME", "time_tracking_db")

# Retención del outbox de notificaciones (segundos). Las notificaciones son
# avisos efímeros: se purgan automáticamente vía índice TTL. 90 días por defecto.
NOTIFICATIONS_RETENTION_SECONDS = 90 * 24 * 60 * 60

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

        # Indexes for Reports
        await db.TimeRecords.create_index([("company_id", 1), ("timestamp", 1)])
        await db.TimeRecords.create_index([("worker_id", 1), ("company_id", 1), ("timestamp", 1)])

        # Indexes for MonthlySignatures
        await db.MonthlySignatures.create_index(
            [("worker_id", 1), ("company_id", 1), ("year", 1), ("month", 1)],
            unique=True,
        )

        # Create indexes for ChangeRequests
        await db.ChangeRequests.create_index("worker_id")
        await db.ChangeRequests.create_index("status")
        await db.ChangeRequests.create_index("created_at")
        await db.ChangeRequests.create_index(
            [("worker_id", 1), ("status", 1)],
            unique=True,
            partialFilterExpression={"status": "pending"}
        )

        # Create indexes for SmsLogs
        await db.SmsLogs.create_index("worker_id")
        await db.SmsLogs.create_index("company_id")
        await db.SmsLogs.create_index("time_record_entry_id")
        await db.SmsLogs.create_index("status")
        await db.SmsLogs.create_index("created_at")
        await db.SmsLogs.create_index([("company_id", 1), ("created_at", 1)])
        await db.SmsLogs.create_index([("worker_id", 1), ("time_record_entry_id", 1), ("reminder_number", 1)])

        # Create indexes for Absences (no "one-pending" partial index: a worker
        # can have several pending requests at once)
        await db.Absences.create_index([("worker_id", 1), ("status", 1)])
        await db.Absences.create_index([("company_id", 1), ("start_date", 1), ("end_date", 1)])

        # Create index for AbsencePolicies (exactly one policy per company)
        await db.AbsencePolicies.create_index("company_id", unique=True)
        # Indexes for Notifications (real-time outbox).
        # NOTA: los antiguos índices con prefijo company_id (company_id_created_at,
        # company_id_read) están obsoletos; pueden borrarse a mano en despliegues
        # existentes (dropIndex). No se hace drop automático: fallaría en BD nuevas.
        # Notificaciones (tiempo real). Las consultas nunca filtran por company_id
        # (aislamiento por tenant = BD separada), así que los índices NO deben
        # prefixear company_id. Ver design.md "Riesgos".
        #  - sort/últimas N:  find({}).sort(created_at, -1)  -> índice creado_at
        #    (un índice único sobre created_at sirve también para el sort descendente
        #     porque Mongo lo recorre en reversa). Añadimos TTL para acotar el outbox.
        #  - no-leídas:       find({"read": False}).sort(created_at, -1) y
        #                     count_documents({"read": False}) -> índice compuesto.
        await db.notifications.create_index(
            [("created_at", 1)],
            name="notifications_created_ttl",
            expireAfterSeconds=NOTIFICATIONS_RETENTION_SECONDS,
        )
        await db.notifications.create_index(
            [("read", 1), ("created_at", -1)],
            name="notifications_unread_created",
        )

        # Index for WorkerShiftStates (CAS guard — must be unique per worker+company)
        await db.WorkerShiftStates.create_index(
            [("worker_id", 1), ("company_id", 1)],
            unique=True,
            name="worker_company_unique",
        )

    except Exception as e:
        # El guard atómico de fichaje depende del índice único de WorkerShiftStates.
        # Si no se crea, la API no debe arrancar: fallaría en abierto (vuelve la carrera).
        raise RuntimeError(f"Fallo crítico inicializando índices: {e}") from e


async def init_default_settings():
    """Create default settings if they don't exist"""
    try:
        existing = await db.Settings.find_one()
        if not existing:
            default_settings = {
                "contact_email": "support@openjornada.es",
                "webapp_url": "http://localhost:5173"
            }
            await db.Settings.insert_one(default_settings)
            print("Default settings created")
    except Exception as e:
        print(f"Error initializing default settings: {e}")
