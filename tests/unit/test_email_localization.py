"""
Unit tests for email localization (tasks 3.1-3.4).

Covers the locale-dynamic EmailRenderer (per-locale ChoiceLoader with es
fallback), the translated template sets, and the send points passing the
recipient company's notification_language.  No MongoDB or SMTP server is
required: DB and email sending are mocked.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

from api.services.email_renderer import EmailRenderer
from api.services.email_service import _localized_subject


# ===========================================================================
# 3.1/3.2 — renderer: per-locale environments + translations
# ===========================================================================

_CONTEXT = {
    "app_name": "OpenJornada",
    "worker_name": "Juan",
    "username": "admin",
    "reset_link": "https://example.com/reset/token",
    "contact_email": "support@example.com",
    "company_name": "Acme SL",
    "absence_type_name": "Vacaciones",
    "start_date": date(2026, 1, 5),
    "end_date": date(2026, 1, 9),
    "days_computed": 3.0,
    "worker_comment": "viaje",
    "admin_public_comment": "ok",
    "record_type": "Entrada",
    "original_datetime": datetime(2026, 1, 2, 9, 0),
    "new_datetime": datetime(2026, 1, 2, 9, 30),
    "reason": "me equivoqué",
    "admin_url": "https://admin.example.com",
    "webapp_url": "https://app.example.com",
}

_ALL_TEMPLATES = [
    "welcome_worker.html",
    "welcome_admin.html",
    "password_reset_worker.html",
    "password_reset_admin.html",
    "absence_approved.html",
    "absence_rejected.html",
    "change_request_accepted.html",
    "change_request_rejected.html",
]


@pytest.fixture()
def renderer():
    return EmailRenderer()


class TestEmailRendererLocales:
    def test_renders_every_template_in_all_locales(self, renderer):
        for locale in ("es", "en", "ca"):
            for template in _ALL_TEMPLATES:
                html, text = renderer.render(template, _CONTEXT, locale=locale)
                assert template != "absence_approved.html" or html  # smoke: no exception, non-empty
                assert "example.com" in html

    def test_english_template_content(self, renderer):
        html, _ = renderer.render("welcome_worker.html", _CONTEXT, locale="en")
        assert "Welcome to OpenJornada" in html
        assert "Create password" in html
        assert "Hola" not in html

    def test_catalan_template_content(self, renderer):
        html, _ = renderer.render("welcome_worker.html", _CONTEXT, locale="ca")
        assert "Benvingut a OpenJornada" in html
        assert "Crear contrasenya" in html

    def test_absence_templates_localized(self, renderer):
        html_en, _ = renderer.render("absence_approved.html", _CONTEXT, locale="en")
        html_ca, _ = renderer.render("absence_approved.html", _CONTEXT, locale="ca")
        assert "Absence Request Approved" in html_en
        assert "Sol·licitud d'absència aprovada" in html_ca

    def test_base_inheritance_per_locale(self, renderer):
        # Each locale resolves its own base.html (lang attribute + footer).
        html_en, _ = renderer.render("welcome_worker.html", _CONTEXT, locale="en")
        html_ca, _ = renderer.render("welcome_worker.html", _CONTEXT, locale="ca")
        html_es, _ = renderer.render("welcome_worker.html", _CONTEXT, locale="es")
        assert 'lang="en"' in html_en
        assert 'lang="ca"' in html_ca
        assert 'lang="es"' in html_es
        assert "This is an automatic email" in html_en
        assert "Aquest és un correu automàtic" in html_ca

    def test_fallback_to_es_when_locale_dir_missing(self, renderer):
        # Unknown locale → search path [<locale>/, es/, emails/] → es.
        html, _ = renderer.render("welcome_worker.html", _CONTEXT, locale="fr")
        assert "Bienvenido" in html

    def test_fallback_when_locale_template_missing(self, renderer, tmp_path):
        # A locale dir that exists but lacks the template must fall back to es.
        locale_dir = tmp_path / "emails" / "xx"
        locale_dir.mkdir(parents=True)
        (tmp_path / "emails" / "es").mkdir()
        (tmp_path / "emails" / "es" / "only_es.html").write_text("<p>Hola {{ worker_name }}</p>")
        r = EmailRenderer.__new__(EmailRenderer)
        r._emails_dir = str(tmp_path / "emails")
        r._envs = {}
        html, _ = r.render("only_es.html", {"worker_name": "Ana"}, locale="xx")
        assert "Hola Ana" in html

    def test_environments_cached_per_locale(self, renderer):
        env_en_1 = renderer._get_env("en")
        env_en_2 = renderer._get_env("en")
        env_ca = renderer._get_env("ca")
        assert env_en_1 is env_en_2
        assert env_en_1 is not env_ca

    def test_none_locale_uses_default(self, renderer):
        html, _ = renderer.render("welcome_worker.html", _CONTEXT, locale=None)
        assert "Bienvenido" in html

    def test_plain_text_generated(self, renderer):
        _, text = renderer.render("welcome_worker.html", _CONTEXT, locale="en")
        assert "Welcome to OpenJornada" in text
        assert "<" not in text.split("http")[0]  # tags stripped


# ===========================================================================
# 3.3 — localized subjects
# ===========================================================================

class TestLocalizedSubjects:
    def test_subject_per_locale(self):
        assert _localized_subject("welcome_worker", "en", "OpenJornada") == "Welcome to OpenJornada"
        assert _localized_subject("welcome_worker", "ca", "OpenJornada") == "Benvingut a OpenJornada"
        assert _localized_subject("welcome_worker", "es", "OpenJornada") == "Bienvenido a OpenJornada"

    def test_unknown_locale_falls_back_to_es(self):
        assert _localized_subject("absence_approved", "fr", "X") == \
            "Tu solicitud de ausencia ha sido aprobada - X"

    def test_es_subjects_match_legacy_text(self):
        # The Spanish subjects must be byte-identical to the pre-i18n ones.
        assert _localized_subject("password_reset_worker", "es", "OpenJornada") == "Recuperación de contraseña - OpenJornada"
        assert _localized_subject("welcome_admin", "es", "OpenJornada") == "Bienvenido a OpenJornada - Panel de Administracion"
        assert _localized_subject("change_request_accepted", "es", "X") == "Tu petición de cambio ha sido aceptada - X"
        assert _localized_subject("change_request_rejected", "es", "X") == "Tu petición de cambio ha sido rechazada - X"
        assert _localized_subject("absence_approved", "es", "X") == "Tu solicitud de ausencia ha sido aprobada - X"
        assert _localized_subject("absence_rejected", "es", "X") == "Tu solicitud de ausencia ha sido rechazada - X"


# ===========================================================================
# 3.3 — send points pass the company's notification_language
# ===========================================================================

class TestWelcomeEmailUsesCompanyLocale:
    async def test_company_language_propagates(self):
        from api.routers import workers as workers_router

        company_oid = ObjectId()
        worker_doc = {
            "_id": ObjectId(), "email": "w@example.com", "first_name": "Ana",
            "last_name": "García", "company_ids": [str(company_oid)],
        }
        company_doc = {"_id": company_oid, "name": "Acme", "notification_language": "ca"}

        with patch("api.routers.workers.db") as mock_db, \
             patch("api.utils.company_locale.db", mock_db), \
             patch("api.routers.workers.email_service") as mock_email:
            mock_db.Workers.update_one = AsyncMock()
            mock_db.Settings.find_one = AsyncMock(return_value=None)
            mock_db.Companies.find_one = AsyncMock(return_value=company_doc)
            mock_email.send_welcome_email = AsyncMock(return_value=True)

            await workers_router._send_welcome_email_to_worker(worker_doc)

        kwargs = mock_email.send_welcome_email.call_args.kwargs
        assert kwargs["locale"] == "ca"

    async def test_legacy_company_without_field_falls_back_to_es(self):
        from api.routers import workers as workers_router

        company_oid = ObjectId()
        worker_doc = {
            "_id": ObjectId(), "email": "w@example.com", "first_name": "Ana",
            "last_name": "García", "company_ids": [str(company_oid)],
        }

        with patch("api.routers.workers.db") as mock_db, \
             patch("api.utils.company_locale.db", mock_db), \
             patch("api.routers.workers.email_service") as mock_email:
            mock_db.Workers.update_one = AsyncMock()
            mock_db.Settings.find_one = AsyncMock(return_value=None)
            mock_db.Companies.find_one = AsyncMock(return_value={"_id": company_oid, "name": "Acme"})
            mock_email.send_welcome_email = AsyncMock(return_value=True)

            await workers_router._send_welcome_email_to_worker(worker_doc)

        assert mock_email.send_welcome_email.call_args.kwargs["locale"] == "es"


class TestAbsenceEmailUsesCompanyLocale:
    async def test_review_sends_in_company_language(self):
        """An admin (any UI language) approving for a 'en' company renders 'en'."""
        from api.models.absences import AbsenceStatus, AbsenceUpdate
        from api.routers import absences as absences_router

        company_oid = ObjectId()
        admin = SimpleNamespace(id="adm", email="admin@example.com", username="admin")
        absence_doc = {
            "_id": ObjectId(),
            "status": AbsenceStatus.PENDING.value,
            "company_id": str(company_oid),
            "company_name": "Acme",
            "worker_id": str(ObjectId()),
            "worker_email": "w@example.com",
            "worker_first_name": "Ana",
            "worker_last_name": "García",
            "absence_type_name": "Vacaciones",
            "start_date": datetime(2026, 1, 5, tzinfo=timezone.utc),
            "end_date": datetime(2026, 1, 9, tzinfo=timezone.utc),
            "days_computed": 3.0,
            "worker_comment": "",
            "absence_type_code": "vacations",
            "deducts_balance": False,
            "is_partial": False,
            "day_portion": "full",
            "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
            "reviewed_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        }

        fake_email_service = SimpleNamespace(
            send_absence_approved_email=AsyncMock(),
            send_absence_rejected_email=AsyncMock(),
        )

        with patch.object(absences_router, "db") as mock_db, \
             patch("api.utils.company_locale.db", mock_db), \
             patch.object(absences_router, "ensure_absence_module_enabled",
                          AsyncMock(return_value={"_id": company_oid})), \
             patch.object(absences_router, "EmailService", return_value=fake_email_service), \
             patch.object(absences_router.validator, "validate",
                          AsyncMock(return_value=(True, []))):
            mock_db.Absences.find_one = AsyncMock(return_value=absence_doc)
            mock_db.Absences.find_one_and_update = AsyncMock(
                return_value=dict(absence_doc, status=AbsenceStatus.ACCEPTED.value)
            )
            mock_db.Companies.find_one = AsyncMock(
                return_value={"_id": company_oid, "notification_language": "en"}
            )
            mock_db.AbsencePolicies.find_one = AsyncMock(return_value={"company_id": str(company_oid)})
            mock_db.Workers.find_one = AsyncMock(return_value={"_id": ObjectId()})
            result = await absences_router.update_absence(
                str(absence_doc["_id"]),
                AbsenceUpdate(status=AbsenceStatus.ACCEPTED),
                current_user=admin,
            )

        assert result.status == AbsenceStatus.ACCEPTED.value
        kwargs = fake_email_service.send_absence_approved_email.call_args.kwargs
        assert kwargs["locale"] == "en"


class TestChangeRequestEmailLocale:
    async def test_record_type_display_localized(self):
        from api.routers.change_requests import _record_type_display

        assert _record_type_display("entry", "es") == "Entrada"
        assert _record_type_display("exit", "es") == "Salida"
        assert _record_type_display("entry", "en") == "Clock-in"
        assert _record_type_display("exit", "en") == "Clock-out"
        assert _record_type_display("entry", "ca") == "Entrada"
        assert _record_type_display("exit", "ca") == "Sortida"
        # Unknown locale → es
        assert _record_type_display("exit", "fr") == "Salida"

    async def test_resolve_company_locale_tolerates_missing_company(self):
        from api.utils.company_locale import resolve_company_locale

        with patch("api.utils.company_locale.db") as mock_db:
            mock_db.Companies.find_one = AsyncMock(return_value=None)
            assert await resolve_company_locale(str(ObjectId())) == "es"
            assert await resolve_company_locale(None) == "es"
            assert await resolve_company_locale("not-an-id") == "es"
