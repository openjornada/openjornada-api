# Scripts de Utilidad

Esta carpeta contiene scripts de verificación y utilidad para testing manual de la API.

## 📝 Scripts Disponibles

### test_incidents.py
Script para verificar el funcionamiento del sistema de incidencias.

**Uso:**
```bash
python scripts/test_incidents.py
```

**Funcionalidad:**
- Prueba la creación de incidencias
- Verifica la autenticación de trabajadores
- Valida el flujo completo de reportes

### verify_password_reset.py
Script para verificar el flujo de recuperación de contraseña.

**Uso:**
```bash
python scripts/verify_password_reset.py
```

**Funcionalidad:**
- Prueba la solicitud de reset de contraseña
- Verifica el envío de emails
- Valida la generación de tokens
- Comprueba el restablecimiento de contraseña

## ⚙️ Configuración

Estos scripts requieren que la API esté corriendo:

```bash
# Asegúrate de que la API está activa
docker-compose up -d

# O en desarrollo local
python -m api.main
```

## 🔧 Requisitos

- API corriendo en http://localhost:8000
- MongoDB conectado
- Variables de entorno configuradas
- Datos de test (trabajadores, empresas, etc.)

## 📚 Añadir Nuevos Scripts

Para añadir nuevos scripts de verificación:

1. Crea el archivo en esta carpeta
2. Nombra descriptivamente (ej: `test_time_records.py`)
3. Documenta su uso en este README
4. Incluye docstrings en el código

## 🚨 Nota Importante

Estos scripts son para **testing manual y verificación**, no son tests automatizados. Para tests automatizados usa pytest en la carpeta `tests/`.
