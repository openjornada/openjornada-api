# OpenJornada API

Backend API para el sistema OpenJornada, construido con FastAPI y MongoDB.

## 🚀 Características

- **FastAPI**: Framework moderno y rápido para construir APIs con Python
- **MongoDB + Motor**: Base de datos NoSQL con driver async para máximo rendimiento
- **Autenticación JWT**: Para usuarios administradores y trackers
- **Autenticación por Request**: Para trabajadores que registran jornada
- **Soft Delete**: Eliminación lógica para mantener integridad de datos
- **Sistema de Permisos**: Control de acceso granular basado en roles
- **Validación Pydantic**: Validación de datos robusta
- **Zona Horaria Automática**: Manejo correcto de zonas horarias en registros
- **Sistema de Empresas**: Soporte multi-empresa con trabajadores asociados
- **Envío de Emails**: Recuperación de contraseña vía SMTP
- **Gestión de Incidencias**: Sistema completo de reportes y seguimiento
- **Sistema de Backups**: Copias de seguridad automáticas con múltiples backends (S3, SFTP, Local)
- **Informes y Exportación**: Informes mensuales, horas extra y exportación a CSV/XLSX/PDF
- **Acceso Inspección de Trabajo**: Endpoints para acceso conforme al art. 34.9 ET y RD-Ley 8/2019
- **Verificación de Integridad**: Hash SHA-256 para registros y exportaciones
- **Firma Mensual de Trabajadores**: Firma digital de registros mensuales por el trabajador

## 📋 Requisitos

- Python 3.11+
- MongoDB 7.0+
- Docker y Docker Compose (recomendado)
- Dependencias adicionales para informes: openpyxl 3.1.2, reportlab 4.1.0

## 🛠️ Instalación

### Con Docker (Recomendado)

```bash
# Clonar el repositorio
cd openjornada-api

# Configurar variables de entorno
cp .env.example .env
# Editar .env con tus configuraciones

# Iniciar servicios
docker-compose up -d

# Ver logs
docker-compose logs -f api
```

### Sin Docker

```bash
# Instalar dependencias
pip install -r requirements.txt

# Configurar variables de entorno
cp .env.example .env
# Editar .env con tus configuraciones

# Asegurarse de que MongoDB está corriendo
# mongodb://localhost:27017

# Iniciar la API
python -m api.main
```

## 🔧 Configuración

### Variables de Entorno

Crea un archivo `.env` basado en `.env.example`:

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
```

## 👥 Gestión de Usuarios API

La API incluye un script CLI para gestionar usuarios administradores:

```bash
# Crear usuario
python -m api.manage_api_users create <username> <email> <role>
# Roles: admin, tracker

# Listar usuarios
python -m api.manage_api_users list

# Ver detalles de usuario
python -m api.manage_api_users show <username>

# Cambiar rol
python -m api.manage_api_users role <username> <new_role>

# Cambiar contraseña
python -m api.manage_api_users password <username>

# Activar/desactivar usuario
python -m api.manage_api_users toggle <username>

# Eliminar usuario
python -m api.manage_api_users delete <username>
```

### Ejemplo: Crear usuario admin

```bash
python -m api.manage_api_users create admin admin@example.com admin
```

## 📚 Documentación API

Una vez que la API esté corriendo, puedes acceder a:

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

## 🏗️ Estructura del Proyecto

```
openjornada-api/
├── api/
│   ├── auth/              # Autenticación y permisos
│   ├── models/            # Modelos Pydantic
│   │   ├── reports.py     # Modelos de informes y exportación
│   │   └── ...
│   ├── routers/           # Endpoints de la API
│   │   ├── reports.py     # Informes, exportación e Inspección de Trabajo
│   │   └── ...
│   ├── services/          # Servicios
│   │   ├── report_service.py     # Generación de informes mensuales y horas extra
│   │   ├── export_service.py     # Exportación a CSV, XLSX y PDF
│   │   ├── integrity_service.py  # Verificación de integridad SHA-256
│   │   └── ...
│   ├── database.py        # Configuración de MongoDB
│   ├── main.py           # Punto de entrada de la aplicación
│   └── manage_api_users.py # CLI para gestión de usuarios
├── docker/               # Dockerfiles
├── docs/                 # Documentación adicional
├── scripts/              # Scripts de verificación y utilidad
├── tests/                # Tests
│   └── unit/
│       └── test_reports.py # Tests unitarios de informes (68 tests)
├── requirements.txt      # Dependencias Python
├── docker-compose.yml    # Configuración Docker local
└── README.md            # Este archivo
```

## 🔐 Sistema de Permisos

### Roles Disponibles

- **admin**: Acceso completo a todos los endpoints
- **inspector**: Acceso de solo lectura a informes y empresas (Inspección de Trabajo)
- **tracker**: Solo puede crear registros de tiempo

### Permisos por Rol

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

**Inspector**:
- view_reports, export_reports, view_companies

**Tracker**:
- create_time_records, create_change_requests, view_pause_types

## 📊 Colecciones de MongoDB

### Workers
Trabajadores que registran jornada:
- Campos: first_name, last_name, email, phone_number, id_number, hashed_password
- company_ids: Array de IDs de empresas asociadas
- Soft delete: deleted_at, deleted_by

### TimeRecords
Registros de entrada/salida:
- Tipo automático basado en último registro
- Almacena UTC + hora local con zona horaria
- Campos: worker_id, company_id, company_name, type, timestamp
- Calcula duración automáticamente

### Companies
Empresas del sistema:
- Campos: name, created_at, updated_at
- Soft delete: deleted_at, deleted_by

### APIUsers
Usuarios administradores:
- Campos: username, email, hashed_password, role, is_active
- Roles: admin, tracker, inspector

### Incidents
Incidencias reportadas por trabajadores:
- Campos: worker_id, description, status
- Estados: pending, in_review, resolved

### MonthlySignatures
Firmas mensuales de trabajadores:
- Campos: worker_id, company_id, year, month, signed_at
- Índice único: (worker_id, company_id, year, month)
- Validación: Un trabajador solo puede firmar una vez por mes/empresa

### Settings
Configuración global:
- contact_email: Email de contacto para soporte
- webapp_url: URL de la aplicación web
- backup_config: Configuración de backups automáticos

### Backups
Registros de copias de seguridad:
- Campos: filename, storage_path, storage_type, size_bytes, status, trigger
- Estados: in_progress, completed, failed
- Trigger: scheduled, manual, pre_restore

## 🔄 Flujos Principales

### Registro de Jornada

1. Trabajador se autentica con email/password
2. Sistema valida credenciales
3. Verifica empresa asociada
4. Comprueba último registro
5. Si último es "exit" o no existe → crea "entry"
6. Si último es "entry" → crea "exit" con duración
7. **Validación crítica**: No permite entrada simultánea en múltiples empresas

### Recuperación de Contraseña

1. Trabajador solicita reset vía email
2. Sistema genera token seguro (válido 1 hora)
3. Envía email con enlace de recuperación
4. Trabajador usa token para establecer nueva contraseña
5. Rate limit: máximo 3 intentos por hora

## 📝 Endpoints Principales

### Autenticación
- `POST /api/token` - Obtener JWT token

### Empresas (Admin only)
- `GET /api/companies/` - Listar empresas
- `POST /api/companies/` - Crear empresa
- `PATCH /api/companies/{id}` - Actualizar empresa
- `DELETE /api/companies/{id}` - Eliminar empresa

### Trabajadores (Admin)
- `GET /api/workers/` - Listar trabajadores
- `POST /api/workers/` - Crear trabajador
- `PUT /api/workers/{id}` - Actualizar trabajador
- `DELETE /api/workers/{id}` - Eliminar trabajador

### Trabajadores (Público)
- `POST /api/workers/my-companies` - Obtener empresas del trabajador
- `PATCH /api/workers/change-password` - Cambiar contraseña
- `POST /api/workers/forgot-password` - Solicitar reset de contraseña
- `POST /api/workers/reset-password` - Restablecer contraseña

### Registros de Jornada
- `POST /api/time-records/` - Crear registro (público con auth)
- `GET /api/time-records/` - Listar todos (admin)
- `GET /api/time-records/{worker_id}/latest` - Último registro

### Incidencias
- `POST /api/incidents/` - Crear incidencia (público con auth)
- `GET /api/incidents/` - Listar incidencias (admin)
- `PATCH /api/incidents/{id}` - Actualizar incidencia (admin)

### Informes y Exportación (Admin/Inspector)
- `GET /api/reports/monthly` - Resumen mensual de empresa
- `GET /api/reports/monthly/worker/{worker_id}` - Resumen mensual de trabajador
- `GET /api/reports/overtime` - Informe de horas extra
- `GET /api/reports/export/monthly` - Exportar informe mensual (CSV/XLSX/PDF)
- `GET /api/reports/export/overtime` - Exportar informe de horas extra (CSV/XLSX/PDF)
- `GET /api/reports/integrity/{record_id}` - Verificar integridad de registro

### Informes del Trabajador (Auth por request)
- `POST /api/reports/worker/monthly` - Ver resumen mensual propio
- `POST /api/reports/worker/monthly/sign` - Firmar registros mensuales
- `POST /api/reports/worker/signatures/status` - Estado de firmas (últimos 12 meses)

### Configuración (Admin only)
- `GET /api/settings/` - Obtener configuración
- `PATCH /api/settings/` - Actualizar configuración

### Backups (Admin only)
- `GET /api/backups/` - Listar backups
- `POST /api/backups/trigger` - Crear backup manual
- `GET /api/backups/{id}` - Detalle de backup
- `DELETE /api/backups/{id}` - Eliminar backup
- `POST /api/backups/{id}/restore` - Restaurar desde backup
- `GET /api/backups/{id}/download-url` - URL de descarga
- `POST /api/backups/test-connection` - Probar conexión storage
- `GET /api/backups/schedule/status` - Estado del scheduler

## 💾 Sistema de Backups

La API incluye un sistema completo de copias de seguridad de MongoDB:

### Características

- **Programación automática**: Backups diarios, semanales o mensuales via APScheduler
- **Múltiples backends de almacenamiento**:
  - **S3-compatible**: AWS S3, Backblaze B2, MinIO, DigitalOcean Spaces
  - **SFTP**: Servidores con acceso SFTP
  - **Local**: Almacenamiento en el servidor (bind mount)
- **Retención configurable**: Por defecto 730 días (2 años)
- **Restauración segura**: Backup automático pre-restore
- **Credenciales encriptadas**: Fernet encryption usando SECRET_KEY

### Configuración desde Admin UI

1. Ir a **Settings → Backups**
2. Activar backups programados
3. Configurar frecuencia (diario/semanal/mensual)
4. Seleccionar hora UTC
5. Elegir backend de almacenamiento
6. Configurar credenciales del storage
7. Probar conexión
8. Guardar

### Configuración Docker para Backups Locales

Para almacenamiento local, el directorio de backups debe ser un **bind mount**:

```yaml
# docker-compose.yml
services:
  api:
    volumes:
      - ./backups:/app/backups
```

```bash
# En servidor, crear directorio antes de deploy
sudo mkdir -p /opt/openjornada/backups
sudo chown 1000:1000 /opt/openjornada/backups
```

### Nota sobre Réplicas

Para backups locales, usar `API_REPLICAS=1` para evitar conflictos. Con S3/SFTP se pueden usar múltiples réplicas.

## 📊 Sistema de Informes y Exportación

La API incluye un sistema completo de informes para cumplimiento laboral:

### Informes Disponibles

- **Resumen Mensual por Trabajador**: Detalle diario con entrada, salida, minutos trabajados, pausas y estado de sesión abierta
- **Resumen Mensual por Empresa**: Agrega todos los trabajadores activos con registros en el mes
- **Informe de Horas Extra**: Detecta trabajadores que superan las horas esperadas (8h/día por defecto)

### Formatos de Exportación

| Formato | Características |
|---------|----------------|
| **CSV** | Separador `;`, codificación UTF-8 con BOM (compatibilidad Excel español) |
| **XLSX** | 2 hojas: Resumen + Detalle Diario, estilos profesionales |
| **PDF** | A4 apaisado, tablas formateadas, pie de cumplimiento legal |

### Integridad y Cumplimiento

- **Hash SHA-256** en cada registro de tiempo (campo `integrity_hash`)
- **Hash de exportaciones** devuelto en cabecera HTTP `X-Report-Hash`
- **Firma mensual del trabajador**: El trabajador puede firmar sus registros mensuales; estado consultable (últimos 12 meses)
- **Pie legal**: "Generado por OpenJornada. Registro conforme al art. 34.9 ET y RD-Ley 8/2019."

### Zona Horaria

Todos los timestamps se almacenan en UTC. Los informes agrupan por día natural en zona horaria local (por defecto `Europe/Madrid`). El parámetro `timezone` permite ajustar a cualquier zona IANA.

## 🧪 Testing

### Tests de Integración

El proyecto incluye tests de integración end-to-end que validan el flujo completo:

```bash
# Instalar dependencias
pip install -r requirements.txt

# Ejecutar todos los tests
pytest

# Ejecutar con output detallado
pytest -v -s

# Ejecutar solo tests de integración
pytest tests/integration/ -v
```

### Tests Disponibles

**Tests de Integración** (`tests/integration/`):

| Test | Descripción |
|------|-------------|
| `test_01_create_company` | Crea empresa y verifica en BD |
| `test_02_create_worker` | Crea trabajador asociado |
| `test_03_create_entry_record` | Registra entrada |
| `test_04_create_exit_record` | Registra salida con duración |
| `test_05_create_change_request` | Crea petición de cambio |
| `test_06_approve_change_request` | Aprueba petición |
| `test_07_verify_final_state` | Verifica consistencia API ↔ BD |
| `test_99_cleanup` | Limpia datos de test |

**Tests Unitarios** (`tests/unit/`):

| Módulo | Tests | Descripción |
|--------|-------|-------------|
| `test_reports.py` | 68 | IntegrityService, modelos de informes, ReportService (process_day_records, group_records_by_day), ExportService (CSV/XLSX/PDF), permisos |

```bash
# Ejecutar tests unitarios
pytest tests/unit/ -v

# Ejecutar tests de informes específicamente
pytest tests/unit/test_reports.py -v
```

Para documentación completa de testing, ver [`docs/TESTING.md`](./docs/TESTING.md).

### Con Docker

```bash
# Ejecutar tests en contenedor
docker-compose exec api pytest tests/integration/ -v
```

### Scripts de Verificación Manual

La carpeta `scripts/` contiene scripts de verificación manual:

```bash
# Verificar sistema de incidencias
python scripts/test_incidents.py

# Verificar recuperación de contraseña
python scripts/verify_password_reset.py
```

Para más información sobre los scripts disponibles, consulta [`scripts/README.md`](./scripts/README.md).

## 📖 Documentación Adicional

Consulta la carpeta [`docs/`](./docs/) para más información:

- [TESTING.md](./docs/TESTING.md) - Tests de integración
- [TESTING_STRATEGY.md](./docs/TESTING_STRATEGY.md) - Estrategia de testing
- [PERMISSIONS_IMPLEMENTATION.md](./docs/PERMISSIONS_IMPLEMENTATION.md) - Sistema de permisos
- [INCIDENTS_API.md](./docs/INCIDENTS_API.md) - Sistema de incidencias
- [PASSWORD_RESET_IMPLEMENTATION.md](./docs/PASSWORD_RESET_IMPLEMENTATION.md) - Recuperación de contraseña

## 🐛 Debugging

### Ver logs en tiempo real

```bash
docker-compose logs -f api
```

### Acceder al contenedor

```bash
docker-compose exec api bash
```

### Verificar conexión a MongoDB

```bash
docker-compose exec mongodb mongosh
```

## 🐳 Imagen Docker

La imagen oficial está disponible en GitHub Container Registry:

```bash
# Última versión
docker pull ghcr.io/openjornada/openjornada-api:latest

# Versión específica
docker pull ghcr.io/openjornada/openjornada-api:1.0.0
```

**Plataformas soportadas:** linux/amd64, linux/arm64

## 🚀 Despliegue en Producción

Para despliegue en producción:

1. Usa `docker-compose.prod.yml`
2. Configura variables de entorno seguras
3. Usa un SECRET_KEY fuerte
4. Configura SMTP real
5. Deshabilita DEBUG
6. Configura CORS apropiadamente
7. Usa HTTPS
8. Configura backups de MongoDB

## 📄 Licencia

GNU Affero General Public License v3.0 (AGPL-3.0) - Ver archivo LICENSE en la raíz del proyecto.

## 👨‍💻 Autor

OpenJornada es un proyecto desarrollado por **[HappyAndroids](https://happyandroids.com)**.

## 🤝 Contribuciones

Las contribuciones son bienvenidas. Por favor abre un issue antes de hacer cambios grandes.

## 🔗 Enlaces

- **Sitio web**: [www.openjornada.es](https://www.openjornada.es)
- **Desarrollado por**: [HappyAndroids](https://happyandroids.com)
- **Email**: info@openjornada.es

---

Un proyecto de [HappyAndroids](https://happyandroids.com) | [OpenJornada](https://www.openjornada.es)
