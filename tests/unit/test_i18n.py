"""
Unit tests for the shared i18n primitives and the data-model fields added by
the add-multilanguage-i18n change (tasks 1.1-1.4, 2.3 validation).

Pure unit tests — no MongoDB or HTTP server required.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from api.models.auth import APIUser, APIUserCreate
from api.models.companies import Company, CompanyCreate, CompanyResponse, CompanyUpdate
from api.models.i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    LanguagePreferenceUpdate,
    is_supported_locale,
    resolve_admin_ui_locale,
    resolve_notification_locale,
    resolve_worker_ui_locale,
)
from api.models.workers import WorkerModel, WorkerResponse, WorkerUpdateModel


# ===========================================================================
# 1.1 — shared locale type
# ===========================================================================

class TestLocaleConstants:
    def test_supported_locales(self):
        assert SUPPORTED_LOCALES == ["es", "en", "ca"]

    def test_default_locale_is_es(self):
        assert DEFAULT_LOCALE == "es"

    def test_is_supported_locale(self):
        assert is_supported_locale("es") is True
        assert is_supported_locale("en") is True
        assert is_supported_locale("ca") is True
        assert is_supported_locale("fr") is False
        assert is_supported_locale(None) is False
        assert is_supported_locale(42) is False


# ===========================================================================
# 1.2 — Company.notification_language
# ===========================================================================

def _company_kwargs():
    return {"name": "Acme", "created_at": datetime.now(timezone.utc)}


class TestCompanyLanguage:
    def test_create_defaults_to_es(self):
        company = CompanyCreate(name="Acme")
        assert company.notification_language == "es"

    def test_create_accepts_supported_locales(self):
        for locale in SUPPORTED_LOCALES:
            assert CompanyCreate(name="Acme", notification_language=locale).notification_language == locale

    def test_create_rejects_invalid_locale(self):
        with pytest.raises(ValidationError):
            CompanyCreate(name="Acme", notification_language="fr")

    def test_db_model_defaults_to_es(self):
        assert Company(**_company_kwargs()).notification_language == "es"

    def test_db_model_rejects_invalid_locale(self):
        with pytest.raises(ValidationError):
            Company(**_company_kwargs(), notification_language="de")

    def test_response_defaults_to_es(self):
        response = CompanyResponse(id="abc", **_company_kwargs())
        assert response.notification_language == "es"

    def test_update_field_is_optional(self):
        # Absent → None (do not touch); present → must be a supported locale.
        assert CompanyUpdate(name="x").notification_language is None
        assert CompanyUpdate(notification_language="ca").notification_language == "ca"
        with pytest.raises(ValidationError):
            CompanyUpdate(notification_language="xx")


# ===========================================================================
# 1.3 — language on admin + worker models
# ===========================================================================

class TestAdminUserLanguage:
    def _user_kwargs(self):
        return {"username": "admin", "email": "admin@example.com"}

    def test_defaults_to_none(self):
        assert APIUser(**self._user_kwargs()).language is None

    def test_accepts_supported(self):
        assert APIUser(**self._user_kwargs(), language="en").language == "en"

    def test_rejects_invalid(self):
        with pytest.raises(ValidationError):
            APIUser(**self._user_kwargs(), language="de")

    def test_create_model_accepts_optional_language(self):
        kwargs = self._user_kwargs() | {"password": "secret123"}
        assert APIUserCreate(**kwargs).language is None
        with pytest.raises(ValidationError):
            APIUserCreate(**kwargs, language="pt")


class TestWorkerLanguage:
    def _worker_kwargs(self):
        return {
            "first_name": "Ana", "last_name": "García", "email": "ana@example.com",
            "phone_number": "+34600000001", "id_number": "12345678A",
            "password": "secret123", "company_ids": ["c1"],
        }

    def test_model_defaults_to_none(self):
        assert WorkerModel(**self._worker_kwargs()).language is None

    def test_model_accepts_supported(self):
        assert WorkerModel(**self._worker_kwargs(), language="ca").language == "ca"

    def test_model_rejects_invalid(self):
        with pytest.raises(ValidationError):
            WorkerModel(**self._worker_kwargs(), language="fr")

    def test_update_field_is_optional(self):
        assert WorkerUpdateModel().language is None
        assert WorkerUpdateModel(language="en").language == "en"
        with pytest.raises(ValidationError):
            WorkerUpdateModel(language="de")

    def test_response_defaults_to_none(self):
        kwargs = {
            "id": "w1", "first_name": "Ana", "last_name": "García",
            "email": "ana@example.com", "phone_number": "+34600000001", "id_number": "12345678A",
        }
        assert WorkerResponse(**kwargs).language is None


# ===========================================================================
# 1.4 — locale resolution helpers
# ===========================================================================

class TestResolveNotificationLocale:
    def test_company_value_wins(self):
        assert resolve_notification_locale({"notification_language": "ca"}) == "ca"

    def test_model_object_supported(self):
        company = CompanyCreate(name="Acme", notification_language="en")
        assert resolve_notification_locale(company) == "en"

    def test_missing_field_falls_back_to_es(self):
        # Documents created before the field existed.
        assert resolve_notification_locale({"name": "Acme"}) == "es"

    def test_none_value_falls_back_to_es(self):
        assert resolve_notification_locale({"notification_language": None}) == "es"

    def test_none_company_falls_back_to_es(self):
        assert resolve_notification_locale(None) == "es"

    def test_invalid_value_falls_back_to_es(self):
        assert resolve_notification_locale({"notification_language": "fr"}) == "es"


class TestResolveWorkerUiLocale:
    def test_worker_language_wins(self):
        worker = {"language": "en"}
        company = {"notification_language": "ca"}
        assert resolve_worker_ui_locale(worker, company) == "en"

    def test_inherits_company_language(self):
        assert resolve_worker_ui_locale({"language": None}, {"notification_language": "ca"}) == "ca"

    def test_falls_back_to_es_without_company(self):
        assert resolve_worker_ui_locale({}, None) == "es"

    def test_invalid_worker_language_falls_to_company(self):
        assert resolve_worker_ui_locale({"language": "de"}, {"notification_language": "en"}) == "en"

    def test_models_accepted(self):
        worker = WorkerUpdateModel(language="ca")
        company = CompanyCreate(name="Acme", notification_language="en")
        assert resolve_worker_ui_locale(worker, company) == "ca"


class TestResolveAdminUiLocale:
    def test_returns_stored_value(self):
        assert resolve_admin_ui_locale({"language": "en"}) == "en"

    def test_none_when_unset(self):
        assert resolve_admin_ui_locale({"language": None}) is None
        assert resolve_admin_ui_locale({}) is None
        assert resolve_admin_ui_locale(None) is None

    def test_invalid_value_treated_as_unset(self):
        assert resolve_admin_ui_locale({"language": "de"}) is None


# ===========================================================================
# 2.1 — shared preference body model
# ===========================================================================

class TestLanguagePreferenceUpdate:
    def test_language_optional_str(self):
        assert LanguagePreferenceUpdate().language is None
        # Plain str on purpose: the endpoints emit error_code'd 422s instead
        # of raw schema errors, so any value is accepted at model level.
        assert LanguagePreferenceUpdate(language="fr").language == "fr"
