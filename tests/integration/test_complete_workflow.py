"""
Tests de integración end-to-end.

Este módulo prueba el flujo completo:
1. Crear empresa
2. Crear trabajador
3. Registrar entrada
4. Registrar salida
5. Crear petición de cambio
6. Aprobar petición de cambio
7. Verificar datos en BD
8. Limpiar datos

Cada paso verifica:
- Respuesta de la API (status code, estructura)
- Datos persistidos en MongoDB
"""
import pytest
from httpx import AsyncClient
from datetime import datetime, timezone, timedelta
from bson import ObjectId
from typing import Dict, Any


class TestCompleteWorkflow:
    """
    Test de integración que ejecuta el flujo completo de la aplicación.

    Es un único test que ejecuta todos los pasos secuencialmente,
    verificando cada operación y limpiando al final.
    """

    @pytest.mark.asyncio
    async def test_complete_workflow(
        self,
        async_client: AsyncClient,
        admin_headers: Dict[str, str],
        test_db
    ):
        """
        Test del flujo completo:
        1. Crear empresa
        2. Crear trabajador
        3. Registrar entrada
        4. Registrar salida
        5. Crear petición de cambio
        6. Aprobar petición de cambio
        7. Verificar estado final
        8. Limpiar datos
        """
        # Variables para almacenar IDs creados
        company_id = None
        worker_id = None
        worker_email = "test.worker.integration@test.com"
        worker_password = "WorkerPass123!"
        entry_record_id = None
        exit_record_id = None
        change_request_id = None

        try:
            # ================================================================
            # PASO 1: CREAR EMPRESA
            # ================================================================
            print("\n--- PASO 1: Crear empresa ---")

            company_data = {"name": "Test Company Integration"}

            response = await async_client.post(
                "/api/companies/",
                json=company_data,
                headers=admin_headers
            )

            assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"

            data = response.json()
            assert "id" in data
            assert data["name"] == company_data["name"]
            assert "created_at" in data

            company_id = data["id"]
            print(f"Empresa creada: {company_id}")

            # Verificar en MongoDB
            company_in_db = await test_db.Companies.find_one({"_id": ObjectId(company_id)})
            assert company_in_db is not None, "Company not found in database"
            assert company_in_db["name"] == company_data["name"]
            assert company_in_db.get("deleted_at") is None
            print("✓ Empresa verificada en BD")

            # ================================================================
            # PASO 2: CREAR TRABAJADOR
            # ================================================================
            print("\n--- PASO 2: Crear trabajador ---")

            worker_data = {
                "first_name": "Test",
                "last_name": "Worker Integration",
                "email": worker_email,
                "phone_number": "+34666777888",
                "id_number": "12345678Z",
                "password": worker_password,
                "company_ids": [company_id]
            }

            response = await async_client.post(
                "/api/workers/",
                json=worker_data,
                headers=admin_headers
            )

            assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"

            data = response.json()
            assert "id" in data
            assert data["email"] == worker_data["email"]
            assert data["first_name"] == worker_data["first_name"]
            assert company_id in data["company_ids"]

            worker_id = data["id"]
            print(f"Trabajador creado: {worker_id}")

            # Verificar en MongoDB
            worker_in_db = await test_db.Workers.find_one({"_id": ObjectId(worker_id)})
            assert worker_in_db is not None, "Worker not found in database"
            assert worker_in_db["email"] == worker_data["email"]
            assert "hashed_password" in worker_in_db
            assert worker_in_db["hashed_password"].startswith("$2b$")  # bcrypt
            print("✓ Trabajador verificado en BD (password hasheado)")

            # ================================================================
            # PASO 3: REGISTRAR ENTRADA
            # ================================================================
            print("\n--- PASO 3: Registrar entrada ---")

            entry_data = {
                "email": worker_email,
                "password": worker_password,
                "company_id": company_id,
                "action": "entry"
            }

            response = await async_client.post(
                "/api/time-records/",
                json=entry_data,
                headers=admin_headers
            )

            assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"

            data = response.json()
            assert "id" in data
            assert data["record_type"] == "entry"
            assert "timestamp" in data
            assert data["worker_id"] == worker_id

            entry_record_id = data["id"]
            print(f"Entrada registrada: {entry_record_id}")

            # Verificar en MongoDB
            record_in_db = await test_db.TimeRecords.find_one({"_id": ObjectId(entry_record_id)})
            assert record_in_db is not None, "Entry record not found in database"
            assert record_in_db["type"] == "entry"
            assert isinstance(record_in_db["timestamp"], datetime)
            print("✓ Registro de entrada verificado en BD")

            # ================================================================
            # PASO 4: REGISTRAR SALIDA
            # ================================================================
            print("\n--- PASO 4: Registrar salida ---")

            exit_data = {
                "email": worker_email,
                "password": worker_password,
                "company_id": company_id,
                "action": "exit"
            }

            response = await async_client.post(
                "/api/time-records/",
                json=exit_data,
                headers=admin_headers
            )

            assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"

            data = response.json()
            assert "id" in data
            assert data["record_type"] == "exit"
            assert "timestamp" in data
            assert "duration_minutes" in data
            assert data["duration_minutes"] is not None
            assert data["duration_minutes"] >= 0

            exit_record_id = data["id"]
            print(f"Salida registrada: {exit_record_id}, duración: {data['duration_minutes']:.2f} min")

            # Verificar en MongoDB
            exit_in_db = await test_db.TimeRecords.find_one({"_id": ObjectId(exit_record_id)})
            assert exit_in_db is not None, "Exit record not found in database"
            assert exit_in_db["type"] == "exit"

            entry_in_db = await test_db.TimeRecords.find_one({"_id": ObjectId(entry_record_id)})
            assert exit_in_db["timestamp"] > entry_in_db["timestamp"], "Exit should be after entry"
            print("✓ Registro de salida verificado en BD")

            # ================================================================
            # PASO 5: CREAR PETICIÓN DE CAMBIO
            # ================================================================
            print("\n--- PASO 5: Crear petición de cambio ---")

            # Obtener timestamp original
            entry_record = await test_db.TimeRecords.find_one({"_id": ObjectId(entry_record_id)})
            original_timestamp = entry_record["timestamp"]

            if original_timestamp.tzinfo is None:
                original_timestamp = original_timestamp.replace(tzinfo=timezone.utc)

            # Nuevo timestamp: 30 minutos antes
            new_timestamp = original_timestamp - timedelta(minutes=30)

            change_request_data = {
                "email": worker_email,
                "password": worker_password,
                "date": original_timestamp.strftime("%Y-%m-%d"),
                "company_id": company_id,
                "time_record_id": entry_record_id,
                "new_timestamp": new_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "reason": "Test de integracion: ajuste de hora de entrada por olvido de fichaje"
            }

            response = await async_client.post(
                "/api/change-requests/",
                json=change_request_data,
                headers=admin_headers
            )

            assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"

            data = response.json()
            assert "id" in data
            assert data["status"] == "pending"
            assert data["time_record_id"] == entry_record_id
            assert data["original_type"] == "entry"

            change_request_id = data["id"]
            print(f"Petición de cambio creada: {change_request_id}")

            # Verificar en MongoDB
            cr_in_db = await test_db.ChangeRequests.find_one({"_id": ObjectId(change_request_id)})
            assert cr_in_db is not None, "Change request not found in database"
            assert cr_in_db["status"] == "pending"
            print("✓ Petición de cambio verificada en BD")

            # ================================================================
            # PASO 6: APROBAR PETICIÓN DE CAMBIO
            # ================================================================
            print("\n--- PASO 6: Aprobar petición de cambio ---")

            # Guardar valores antes de aprobar
            cr_before = await test_db.ChangeRequests.find_one({"_id": ObjectId(change_request_id)})
            expected_new_timestamp = cr_before["new_timestamp"]
            original_timestamp_before = cr_before["original_timestamp"]

            approve_data = {
                "status": "accepted",
                "admin_public_comment": "Aprobado en test de integracion"
            }

            response = await async_client.patch(
                f"/api/change-requests/{change_request_id}",
                json=approve_data,
                headers=admin_headers
            )

            assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"

            data = response.json()
            assert data["status"] == "accepted"
            assert data["reviewed_by_admin_email"] is not None
            assert data["reviewed_at"] is not None
            print(f"Petición aprobada por: {data['reviewed_by_admin_email']}")

            # Verificar change request en MongoDB
            cr_in_db = await test_db.ChangeRequests.find_one({"_id": ObjectId(change_request_id)})
            assert cr_in_db["status"] == "accepted"
            assert cr_in_db["reviewed_by_admin_id"] is not None
            print("✓ Estado de petición actualizado en BD")

            # Verificar que el time record se actualizó
            entry_in_db = await test_db.TimeRecords.find_one({"_id": ObjectId(entry_record_id)})

            assert entry_in_db["timestamp"].replace(microsecond=0) == \
                   expected_new_timestamp.replace(microsecond=0), \
                   f"Timestamp not updated correctly"

            assert entry_in_db.get("modified_by_admin_id") is not None
            assert entry_in_db.get("original_timestamp") is not None
            print("✓ Time record actualizado con nuevo timestamp y campos de auditoría")

            # ================================================================
            # PASO 7: VERIFICACIÓN FINAL
            # ================================================================
            print("\n--- PASO 7: Verificación final ---")

            # Verificar empresa via API
            response = await async_client.get(
                f"/api/companies/{company_id}",
                headers=admin_headers
            )
            assert response.status_code == 200
            company_api = response.json()
            company_db = await test_db.Companies.find_one({"_id": ObjectId(company_id)})
            assert company_api["name"] == company_db["name"]
            print("✓ Empresa: API y BD coinciden")

            # Verificar trabajador via API
            response = await async_client.get(
                f"/api/workers/{worker_id}",
                headers=admin_headers
            )
            assert response.status_code == 200
            worker_api = response.json()
            assert company_id in worker_api["company_ids"]
            print("✓ Trabajador: API y BD coinciden")

            # Verificar registros de tiempo
            response = await async_client.get(
                f"/api/time-records/worker/{worker_id}",
                headers=admin_headers
            )
            assert response.status_code == 200
            records_api = response.json()
            assert len(records_api) >= 2
            print(f"✓ Registros de tiempo: {len(records_api)} encontrados")

            # Verificar change request via API
            response = await async_client.get(
                f"/api/change-requests/{change_request_id}",
                headers=admin_headers
            )
            assert response.status_code == 200
            cr_api = response.json()
            assert cr_api["status"] == "accepted"
            print("✓ Change request: estado correcto")

            print("\n" + "=" * 50)
            print("TODOS LOS TESTS PASARON CORRECTAMENTE")
            print("=" * 50)

        finally:
            # ================================================================
            # LIMPIEZA (siempre se ejecuta)
            # ================================================================
            print("\n--- LIMPIEZA ---")

            # Eliminar change requests
            if change_request_id:
                await test_db.ChangeRequests.delete_one({"_id": ObjectId(change_request_id)})
                print(f"✓ Change request eliminado: {change_request_id}")

            # Eliminar time records
            if worker_id:
                result = await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                print(f"✓ Time records eliminados: {result.deleted_count}")

            # Eliminar worker
            if worker_id:
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
                print(f"✓ Worker eliminado: {worker_id}")

            # Eliminar company
            if company_id:
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
                print(f"✓ Company eliminada: {company_id}")

            # Eliminar admin user de test
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})
            print("✓ Admin user de test eliminado")

            print("\n--- Limpieza completada ---")
