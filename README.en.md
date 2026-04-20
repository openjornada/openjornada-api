> 🇪🇸 [Leer en español](README.md)

# OpenJornada API

Backend API for the OpenJornada system, built with FastAPI and MongoDB.

## 🚀 Features

- **FastAPI**: Modern and fast framework for building APIs with Python
- **MongoDB + Motor**: NoSQL database with async driver for maximum performance
- **JWT Authentication**: For admin users and trackers
- **Request-based Authentication**: For workers registering their work hours
- **Soft Delete**: Logical deletion to maintain data integrity
- **Permission System**: Granular role-based access control
- **Pydantic Validation**: Robust data validation
- **Automatic Timezone**: Correct timezone handling in records
- **Company System**: Multi-company support with associated workers
- **Email Sending**: Password recovery via SMTP
- **Incident Management**: Complete reporting and tracking system
- **Backup System**: Automatic backups with multiple backends (S3, SFTP, Local)
- **Reports and Export**: Monthly reports, overtime and export to CSV/XLSX/PDF
- **Labor Inspection Access**: Endpoints for access compliant with Spanish labor law (art. 34.9 Workers' Statute and RD-Ley 8/2019)
- **Integrity Verification**: SHA-256 hash for records and exports
- **Monthly Worker Signature**: Digital signature of monthly records by the worker
- **SMS Reminders**: Automatic SMS sending to workers who forget to clock out (LabsMobile provider)

## 📋 Requirements

- Python 3.11+
- MongoDB 7.0+
- Docker and Docker Compose (recommended)
- Additional dependencies for reports: openpyxl 3.1.2, reportlab 4.1.0

## 🛠️ Installation

### With Docker (Recommended)

```bash
# Clone the repository
cd openjornada-api

# Set up environment variables
cp .env.example .env
# Edit .env with your settings

# Start services
docker-compose up -d

# View logs
docker-compose logs -f api
```

### Without Docker

```bash
# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env
# Edit .env with your settings

# Make sure MongoDB is running
# mongodb://localhost:27017

# Start the API
python -m api.main
```

## 🔧 Configuration

### Environment Variables

Create a `.env` file based on `.env.example`:

```env
# API Configuration
API_PORT=8000
API_HOST=0.0.0.0
DEBUG=True

# Security
SECRET_KEY=your_secret_key_here
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30

# Database
MONGO_URL=mongodb://mongodb:27017
DB_NAME=time_tracking_db

# SMTP Configuration
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=your_email@example.com
SMTP_PASSWORD=your_password
SMTP_FROM_EMAIL=noreply@example.com
SMTP_FROM_NAME=OpenJornada
EMAIL_APP_NAME=OpenJornada

# SMS Configuration (LabsMobile)
# SMS_ENABLED=false
# SMS_PROVIDER=labsmobile
# SMS_LABSMOBILE_API_TOKEN=  # Base64(username:api_key)
# SMS_SENDER_ID=OpenJornada
# SMS_UNLIMITED_BALANCE=0
```

## 👥 API User Management

The API includes a CLI script for managing admin users:

```bash
# Create user
python -m api.manage_api_users create <username> <email> <role>
# Roles: admin, tracker

# List users
python -m api.manage_api_users list

# Show user details
python -m api.manage_api_users show <username>

# Change role
python -m api.manage_api_users role <username> <new_role>

# Change password
python -m api.manage_api_users password <username>

# Enable/disable user
python -m api.manage_api_users toggle <username>

# Delete user
python -m api.manage_api_users delete <username>
```

### Example: Create admin user

```bash
python -m api.manage_api_users create admin admin@example.com admin
```

## 📚 API Documentation

Once the API is running, you can access:

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

## 🏗️ Project Structure

```
openjornada-api/
├── api/
│   ├── auth/              # Authentication and permissions
│   ├── models/            # Pydantic models
│   │   ├── reports.py     # Report and export models
│   │   ├── sms.py          # SMS models (logs, config, templates)
│   │   └── ...
│   ├── routers/           # API endpoints
│   │   ├── reports.py     # Reports, export and Labor Inspection
│   │   ├── sms.py          # SMS endpoints
│   │   └── ...
│   ├── services/          # Services
│   │   ├── report_service.py     # Monthly and overtime report generation
│   │   ├── export_service.py     # Export to CSV, XLSX and PDF
│   │   ├── integrity_service.py  # SHA-256 integrity verification
│   │   ├── sms_service.py        # SMS service (LabsMobile)
│   │   └── ...
│   ├── database.py        # MongoDB configuration
│   ├── main.py           # Application entry point
│   └── manage_api_users.py # CLI for user management
├── docker/               # Dockerfiles
├── docs/                 # Additional documentation
├── scripts/              # Verification and utility scripts
├── tests/                # Tests
│   └── unit/
│       ├── test_reports.py       # Report unit tests (68 tests)
│       ├── test_sms_service.py   # SMS service tests (24 tests)
│       └── test_scheduler_sms.py # SMS scheduler tests (17 tests)
├── requirements.txt      # Python dependencies
├── docker-compose.yml    # Local Docker configuration
└── README.md            # This file
```

## 🔐 Permission System

### Available Roles

- **admin**: Full access to all endpoints
- **inspector**: Read-only access to reports and companies (Labor Inspection)
- **tracker**: Can only create time records

### Permissions by Role

**Admin**:
- create_users, view_users
- create_workers, view_workers, update_workers, delete_workers
- create_time_records, view_all_time_records, view_worker_time_records
- manage_pause_types, view_pause_types
- view_change_requests, manage_change_requests, create_change_requests
- create_companies, view_companies, update_companies, delete_companies
- view_incidents, manage_incidents
- view_settings, update_settings
- view_backups, manage_backups
- view_reports, export_reports, manage_inspection
- manage_sms_config, view_sms_logs, view_sms_dashboard

**Inspector**:
- view_reports, export_reports, view_companies

**Tracker**:
- create_time_records, create_change_requests, view_pause_types

## 📊 MongoDB Collections

### Workers
Workers who register their work hours:
- Fields: first_name, last_name, email, phone_number, id_number, hashed_password
- company_ids: Array of associated company IDs
- Soft delete: deleted_at, deleted_by

### TimeRecords
Clock-in/clock-out records:
- Automatic type based on last record
- Stores UTC + local time with timezone
- Fields: worker_id, company_id, company_name, type, timestamp
- Automatically calculates duration

### Companies
System companies:
- Fields: name, created_at, updated_at
- Soft delete: deleted_at, deleted_by

### APIUsers
Admin users:
- Fields: username, email, hashed_password, role, is_active
- Roles: admin, tracker, inspector

### Incidents
Incidents reported by workers:
- Fields: worker_id, description, status
- Statuses: pending, in_review, resolved

### MonthlySignatures
Monthly worker signatures:
- Fields: worker_id, company_id, year, month, signed_at
- Unique index: (worker_id, company_id, year, month)
- Validation: A worker can only sign once per month/company

### Settings
Global configuration:
- contact_email: Support contact email
- webapp_url: Web application URL
- backup_config: Automatic backup configuration

### SmsLogs
Sent SMS log:
- Fields: worker_id, company_id, phone_number, message, status, provider_message_id, cost, reminder_number
- Statuses: pending, sent, delivered, failed
- Indexes: (company_id, created_at), (worker_id, time_record_entry_id, reminder_number)

### Backups
Backup records:
- Fields: filename, storage_path, storage_type, size_bytes, status, trigger
- Statuses: in_progress, completed, failed
- Trigger: scheduled, manual, pre_restore

## 🔄 Main Flows

### Work Hours Registration

1. Worker authenticates with email/password
2. System validates credentials
3. Verifies associated company
4. Checks last record
5. If last is "exit" or none exists → creates "entry"
6. If last is "entry" → creates "exit" with duration
7. **Critical validation**: Does not allow simultaneous clock-in at multiple companies

### Password Recovery

1. Worker requests reset via email
2. System generates secure token (valid for 1 hour)
3. Sends email with recovery link
4. Worker uses token to set new password
5. Rate limit: maximum 3 attempts per hour

## 📝 Main Endpoints

### Authentication
- `POST /api/token` - Get JWT token

### Companies (Admin only)
- `GET /api/companies/` - List companies
- `POST /api/companies/` - Create company
- `PATCH /api/companies/{id}` - Update company
- `DELETE /api/companies/{id}` - Delete company

### Workers (Admin)
- `GET /api/workers/` - List workers
- `POST /api/workers/` - Create worker
- `PUT /api/workers/{id}` - Update worker
- `DELETE /api/workers/{id}` - Delete worker

### Workers (Public)
- `POST /api/workers/my-companies` - Get worker's companies
- `PATCH /api/workers/change-password` - Change password
- `POST /api/workers/forgot-password` - Request password reset
- `POST /api/workers/reset-password` - Reset password

### Time Records
- `POST /api/time-records/` - Create record (public with auth)
- `GET /api/time-records/` - List all (admin)
- `GET /api/time-records/{worker_id}/latest` - Latest record

### Incidents
- `POST /api/incidents/` - Create incident (public with auth)
- `GET /api/incidents/` - List incidents (admin)
- `PATCH /api/incidents/{id}` - Update incident (admin)

### Reports and Export (Admin/Inspector)
- `GET /api/reports/monthly` - Company monthly summary
- `GET /api/reports/monthly/worker/{worker_id}` - Worker monthly summary
- `GET /api/reports/overtime` - Overtime report
- `GET /api/reports/export/monthly` - Export monthly report (CSV/XLSX/PDF)
- `GET /api/reports/export/overtime` - Export overtime report (CSV/XLSX/PDF)
- `GET /api/reports/integrity/{record_id}` - Verify record integrity

### Worker Reports (Request-based auth)
- `POST /api/reports/worker/monthly` - View own monthly summary
- `POST /api/reports/worker/monthly/sign` - Sign monthly records
- `POST /api/reports/worker/signatures/status` - Signature status (last 12 months)

### Settings (Admin only)
- `GET /api/settings/` - Get settings
- `PATCH /api/settings/` - Update settings

### Backups (Admin only)
- `GET /api/backups/` - List backups
- `POST /api/backups/trigger` - Create manual backup
- `GET /api/backups/{id}` - Backup details
- `DELETE /api/backups/{id}` - Delete backup
- `POST /api/backups/{id}/restore` - Restore from backup
- `GET /api/backups/{id}/download-url` - Download URL
- `POST /api/backups/test-connection` - Test storage connection
- `GET /api/backups/schedule/status` - Scheduler status

### SMS (Admin only)
- `GET /api/sms/credits` - SMS provider credits and status
- `GET /api/sms/config` - Get company SMS configuration
- `PATCH /api/sms/config` - Update SMS configuration
- `GET /api/sms/template` - Get SMS message template
- `PUT /api/sms/template` - Update template
- `POST /api/sms/template/reset` - Restore default template
- `GET /api/sms/stats` - Sending statistics
- `GET /api/sms/history` - Sent SMS history
- `DELETE /api/sms/history` - Clear history
- `POST /api/workers/{id}/sms/send` - Send manual SMS to worker

## 💾 Backup System

The API includes a complete MongoDB backup system:

### Features

- **Automatic scheduling**: Daily, weekly or monthly backups via APScheduler
- **Multiple storage backends**:
  - **S3-compatible**: AWS S3, Backblaze B2, MinIO, DigitalOcean Spaces
  - **SFTP**: Servers with SFTP access
  - **Local**: Server storage (bind mount)
- **Configurable retention**: Default 730 days (2 years)
- **Safe restore**: Automatic pre-restore backup
- **Encrypted credentials**: Fernet encryption using SECRET_KEY

### Configuration from Admin UI

1. Go to **Settings → Backups**
2. Enable scheduled backups
3. Configure frequency (daily/weekly/monthly)
4. Select UTC time
5. Choose storage backend
6. Configure storage credentials
7. Test connection
8. Save

### Docker Configuration for Local Backups

For local storage, the backup directory must be a **bind mount**:

```yaml
# docker-compose.yml
services:
  api:
    volumes:
      - ./backups:/app/backups
```

```bash
# On the server, create directory before deployment
sudo mkdir -p /opt/openjornada/backups
sudo chown 1000:1000 /opt/openjornada/backups
```

### Note on Replicas

For local backups, use `API_REPLICAS=1` to avoid conflicts. With S3/SFTP, multiple replicas can be used.

## 📱 SMS Reminder System

The API includes an SMS reminder system for workers who forget to clock out:

### Features

- **Automatic sending**: The scheduler checks for open shifts every 5 minutes
- **LabsMobile provider**: Integration via REST API with HTTP Basic authentication
- **Customizable template**: Dynamic tags ({%worker_name%}, {%company_name%}, {%hours_open%}, {%reminder_number%})
- **Configurable active hours**: Only sends within the defined schedule (default 08:00-23:00)
- **Frequency control**: First reminder, interval between reminders and maximum per day
- **Per-worker opt-out**: Each worker can disable SMS from their profile
- **Encrypted credentials**: Fernet encryption using SECRET_KEY
- **Unlimited balance**: Development mode without consuming real credits

### Configuration

1. Configure SMS environment variables (see Environment Variables section)
2. Go to **Admin → SMS Reminders**
3. Configure active hours and reminder frequency
4. Customize the message template
5. Enable the service

## 📊 Reports and Export System

The API includes a complete reporting system for labor law compliance:

### Available Reports

- **Monthly Summary per Worker**: Daily detail with clock-in, clock-out, minutes worked, breaks and open session status
- **Monthly Summary per Company**: Aggregates all active workers with records in the month
- **Overtime Report**: Detects workers exceeding expected hours (8h/day by default)

### Export Formats

| Format | Features |
|--------|----------|
| **CSV** | `;` separator, UTF-8 encoding with BOM (Spanish Excel compatibility) |
| **XLSX** | 2 sheets: Summary + Daily Detail, professional styling |
| **PDF** | Landscape A4, formatted tables, legal compliance footer |

### Integrity and Compliance

- **SHA-256 hash** on each time record (`integrity_hash` field)
- **Export hash** returned in HTTP header `X-Report-Hash`
- **Monthly worker signature**: Workers can sign their monthly records; status queryable (last 12 months)
- **Legal footer**: "Generated by OpenJornada. Record compliant with Spanish labor law (art. 34.9 Workers' Statute and RD-Ley 8/2019)."

### Timezone

All timestamps are stored in UTC. Reports group by calendar day in local timezone (default `Europe/Madrid`). The `timezone` parameter allows adjustment to any IANA timezone.

## 🧪 Testing

### Integration Tests

The project includes end-to-end integration tests that validate the complete flow:

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
pytest

# Run with verbose output
pytest -v -s

# Run only integration tests
pytest tests/integration/ -v
```

### Available Tests

**Integration Tests** (`tests/integration/`):

| Test | Description |
|------|-------------|
| `test_01_create_company` | Creates company and verifies in DB |
| `test_02_create_worker` | Creates associated worker |
| `test_03_create_entry_record` | Registers clock-in |
| `test_04_create_exit_record` | Registers clock-out with duration |
| `test_05_create_change_request` | Creates change request |
| `test_06_approve_change_request` | Approves request |
| `test_07_verify_final_state` | Verifies API ↔ DB consistency |
| `test_99_cleanup` | Cleans up test data |

**Unit Tests** (`tests/unit/`):

| Module | Tests | Description |
|--------|-------|-------------|
| `test_reports.py` | 68 | IntegrityService, report models, ReportService (process_day_records, group_records_by_day), ExportService (CSV/XLSX/PDF), permissions |
| `test_sms_service.py` | 24 | SmsService: initialization (env, DB, fallback), sending (disabled, no balance, success, failure), reload |
| `test_scheduler_sms.py` | 17 | SMS Scheduler: active hours, intervals, max reminders, opt-out, no phone, thresholds |

```bash
# Run unit tests
pytest tests/unit/ -v

# Run report tests specifically
pytest tests/unit/test_reports.py -v
```

For complete testing documentation, see [`docs/TESTING.md`](./docs/TESTING.md).

### With Docker

```bash
# Run tests in container
docker-compose exec api pytest tests/integration/ -v
```

### Manual Verification Scripts

The `scripts/` folder contains manual verification scripts:

```bash
# Verify incident system
python scripts/test_incidents.py

# Verify password recovery
python scripts/verify_password_reset.py
```

For more information about available scripts, see [`scripts/README.md`](./scripts/README.md).

## 📖 Additional Documentation

Check the [`docs/`](./docs/) folder for more information:

- [TESTING.md](./docs/TESTING.md) - Integration tests
- [TESTING_STRATEGY.md](./docs/TESTING_STRATEGY.md) - Testing strategy
- [PERMISSIONS_IMPLEMENTATION.md](./docs/PERMISSIONS_IMPLEMENTATION.md) - Permission system
- [INCIDENTS_API.md](./docs/INCIDENTS_API.md) - Incident system
- [PASSWORD_RESET_IMPLEMENTATION.md](./docs/PASSWORD_RESET_IMPLEMENTATION.md) - Password recovery

## 🐛 Debugging

### View logs in real time

```bash
docker-compose logs -f api
```

### Access the container

```bash
docker-compose exec api bash
```

### Verify MongoDB connection

```bash
docker-compose exec mongodb mongosh
```

## 🐳 Docker Image

The official image is available on GitHub Container Registry:

```bash
# Latest version
docker pull ghcr.io/openjornada/openjornada-api:latest

# Specific version
docker pull ghcr.io/openjornada/openjornada-api:1.0.0
```

**Supported platforms:** linux/amd64, linux/arm64

## 🚀 Production Deployment

For production deployment:

1. Use `docker-compose.prod.yml`
2. Configure secure environment variables
3. Use a strong SECRET_KEY
4. Configure real SMTP
5. Disable DEBUG
6. Configure CORS appropriately
7. Use HTTPS
8. Configure MongoDB backups

## 📄 License

GNU Affero General Public License v3.0 (AGPL-3.0) - See LICENSE file in the project root.

## 👨‍💻 Author

OpenJornada is a project developed by **[HappyAndroids](https://happyandroids.com)**.

## 🤝 Contributing

Contributions are welcome. Please open an issue before making large changes.

## 🔗 Links

- **Website**: [www.openjornada.es](https://www.openjornada.es)
- **Developed by**: [HappyAndroids](https://happyandroids.com)
- **Email**: info@openjornada.es

---

A project by [HappyAndroids](https://happyandroids.com) | [OpenJornada](https://www.openjornada.es)
