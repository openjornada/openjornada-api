"""
Unit tests for the SMS i18n change (tasks 4.1-4.7).

Covers: GSM-7 helpers, per-locale default templates, the localized
_build_reminder_message with fallback chain + lazy migration + single-segment
guarantee, and the /sms/template endpoints (map contract, per-locale
PUT/DELETE isolation, segment validation).

Pure unit tests — MongoDB is mocked, no HTTP server required.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth.auth_handler import get_current_active_user
from api.models.auth import APIUser
from api.models.sms import (
    AVAILABLE_TAGS,
    DEFAULT_SMS_TEMPLATE,
    DEFAULT_SMS_TEMPLATES,
    LEGACY_SMS_TEMPLATE_FIELD,
    SMS_MARKER_TAGS,
    SMS_TEMPLATES_FIELD,
    SmsTemplateUpdate,
    resolve_reminder_template,
    resolve_sms_reminder_templates,
)
from api.services.sms_service import SmsService
from api.utils.sms_length import (
    GSM7_FOLD_OVERRIDES,
    fits_single_segment,
    fold_to_gsm7,
    is_gsm7,
    sms_length,
    sms_segment_limit,
    truncate_to_single_segment,
)


# ===========================================================================
# 4.3 — GSM-7 helpers
# ===========================================================================

class TestIsGsm7:
    def test_pure_ascii_is_gsm7(self):
        assert is_gsm7("Hello worker, your shift is open. Reminder 2.") is True

    def test_gsm7_accented_chars_are_accepted(self):
        # à è é ì ò ù ç ñ ü are GSM-7 basic characters.
        assert is_gsm7("àèéìòùçñüÄÖÑÜ§¿¡ØøÅåÆßÉ") is True

    def test_currency_and_symbols(self):
        assert is_gsm7("£$¥€¤§@") is True  # € is an extension code, still GSM-7

    @pytest.mark.parametrize("char", "áíóúÁÍÓÚ")
    def test_acute_accents_force_ucs2(self, char):
        assert is_gsm7(f"jornada {char} abierta") is False

    def test_emoji_and_cjk_force_ucs2(self):
        assert is_gsm7("hola 😀") is False
        assert is_gsm7("こんにちは") is False

    def test_empty_string_is_gsm7(self):
        assert is_gsm7("") is True


class TestSegmentLimit:
    def test_gsm7_limit_160(self):
        assert sms_segment_limit("Hola, jornada abierta") == 160

    def test_ucs2_limit_70(self):
        assert sms_segment_limit("Jornada á abierta") == 70

    def test_extension_chars_count_double(self):
        assert sms_length("abc") == 3
        assert sms_length("a{b}c") == 7  # { } are extension codes
        assert sms_length("€") == 2

    def test_fits_single_segment(self):
        assert fits_single_segment("x" * 160) is True
        assert fits_single_segment("x" * 161) is False
        assert fits_single_segment("á" * 70) is True
        assert fits_single_segment("á" * 71) is False


class TestTruncateToSingleSegment:
    def test_fits_unchanged(self):
        text = "Hola, tu jornada está abierta."
        assert truncate_to_single_segment(text) == text

    def test_long_gsm7_over_limit_stays_gsm7(self):
        # A GSM-7 message over 160 keeps GSM-7: a "..." ellipsis is appended and the
        # body trimmed so the result still fits ONE 160-septet segment.
        result = truncate_to_single_segment("x" * 200)
        assert result.endswith("...")
        assert is_gsm7(result) is True
        assert result == "x" * 157 + "..."
        assert fits_single_segment(result) is True

    def test_long_ucs2_truncated_to_70(self):
        result = truncate_to_single_segment("á" * 100)
        assert result.endswith("…")
        assert len(result) == 70
        assert fits_single_segment(result) is True

    def test_boundary_161_gsm7(self):
        result = truncate_to_single_segment("a" * 161)
        assert fits_single_segment(result) is True
        assert result == "a" * 157 + "..."


# ===========================================================================
# fold_to_gsm7 — BUG A: accents must not force needless UCS-2 truncation
# ===========================================================================

class TestFoldToGsm7:
    def test_gsm7_text_is_identity(self):
        text = "Hola ñàèéìòùçüÄÖÑÜ§¿¡€_@£$¥"
        assert fold_to_gsm7(text) == text

    def test_empty_string(self):
        assert fold_to_gsm7("") == ""

    def test_acute_accents_fold_and_result_is_gsm7(self):
        assert fold_to_gsm7("García Rodríguez") == "Garcia Rodriguez"
        assert is_gsm7(fold_to_gsm7("María López ÁÍÓÚ")) is True

    def test_gsm7_accented_chars_are_not_folded_away(self):
        # ñ, à, ü, è, é, ç ARE GSM-7: folding must preserve them verbatim.
        assert fold_to_gsm7("Iñaki àü èé ç") == "Iñaki àü èé ç"

    def test_typographic_punctuation_folds(self):
        assert fold_to_gsm7("\u201chola\u201d\u2014\u2026") == '"hola"-...'
        assert fold_to_gsm7("Jos\u00e9\u2019s") == "Jos\u00e9's"  # é survives, ’ folds

    def test_indecomposable_latin_letters_fold(self):
        assert fold_to_gsm7("ł Ł đ Đ ı") == "l L d D i"

    def test_exotic_chars_kept_and_still_ucs2(self):
        # No compatible form → kept as-is so the UCS-2 truncation fallback applies.
        assert fold_to_gsm7("hola 😀 中") == "hola 😀 中"
        assert is_gsm7(fold_to_gsm7("hola 😀")) is False

    def test_all_overrides_are_gsm7(self):
        for target in GSM7_FOLD_OVERRIDES.values():
            assert is_gsm7(target), target


# ===========================================================================
# 4.1 — default templates per locale
# ===========================================================================

class TestDefaultSmsTemplates:
    def test_all_supported_locales_present(self):
        assert set(DEFAULT_SMS_TEMPLATES) == {"es", "en", "ca"}

    def test_es_text_is_verbatim(self):
        assert DEFAULT_SMS_TEMPLATES["es"] == (
            "OpenJornada: Hola {%worker_name%}, llevas {%hours_open%}h con jornada "
            "abierta en {%company_name%}. Registra tu salida. "
            "Aviso {%reminder_number%}."
        )
        assert DEFAULT_SMS_TEMPLATE == DEFAULT_SMS_TEMPLATES["es"]

    @pytest.mark.parametrize("locale", sorted(DEFAULT_SMS_TEMPLATES))
    def test_markers_present_in_every_locale(self, locale):
        for marker in SMS_MARKER_TAGS:
            assert marker in DEFAULT_SMS_TEMPLATES[locale], (locale, marker)

    @pytest.mark.parametrize("locale", sorted(DEFAULT_SMS_TEMPLATES))
    def test_defaults_are_pure_gsm7(self, locale):
        assert is_gsm7(DEFAULT_SMS_TEMPLATES[locale]) is True


# ===========================================================================
# 4.2 — lazy migration + resolution chain helpers
# ===========================================================================

class TestResolveSmsReminderTemplates:
    def test_no_settings(self):
        assert resolve_sms_reminder_templates(None) == {}

    def test_map_returned_as_is(self):
        doc = {SMS_TEMPLATES_FIELD: {"ca": "text"}}
        assert resolve_sms_reminder_templates(doc) == {"ca": "text"}

    def test_legacy_string_migrates_to_es(self):
        doc = {LEGACY_SMS_TEMPLATE_FIELD: "recordatorio antiguo"}
        assert resolve_sms_reminder_templates(doc) == {"es": "recordatorio antiguo"}

    def test_map_wins_over_legacy_for_es(self):
        doc = {
            LEGACY_SMS_TEMPLATE_FIELD: "legacy",
            SMS_TEMPLATES_FIELD: {"es": "modern"},
        }
        assert resolve_sms_reminder_templates(doc) == {"es": "modern"}


class TestResolveReminderTemplate:
    def test_custom_locale_wins(self):
        custom = {"ca": "personalitzat"}
        assert resolve_reminder_template(custom, "ca") == "personalitzat"

    def test_default_for_locale(self):
        assert resolve_reminder_template({}, "en") == DEFAULT_SMS_TEMPLATES["en"]

    def test_unknown_locale_falls_back_to_es_default(self):
        assert resolve_reminder_template({}, "eu") == DEFAULT_SMS_TEMPLATES["es"]

    def test_none_locale_falls_back_to_es_default(self):
        assert resolve_reminder_template({}, None) == DEFAULT_SMS_TEMPLATES["es"]


# ===========================================================================
# 4.4 — _build_reminder_message
# ===========================================================================

@pytest.fixture()
def with_settings():
    """Factory: SmsService whose module-level db is a fake Settings document."""
    started = []

    def _make(settings_doc):
        service = SmsService()
        mock_db = MagicMock()
        mock_db.Settings.find_one = AsyncMock(return_value=settings_doc)
        patcher = patch("api.services.sms_service.db", mock_db)
        patcher.start()
        started.append(patcher)
        return service, mock_db

    yield _make
    for patcher in started:
        patcher.stop()


async def _build(service, locale):
    return await service._build_reminder_message(
        worker_name="Ana",
        company_name="Acme",
        hours_open=4.5,
        reminder_number=2,
        notification_locale=locale,
    )


class TestBuildReminderMessage:
    async def test_no_settings_uses_es_default(self, with_settings):
        service, _ = with_settings(None)
        message = await _build(service, "es")
        assert message == (
            "OpenJornada: Hola Ana, llevas 4.5h con jornada abierta en Acme. "
            "Registra tu salida. Aviso 2."
        )

    async def test_company_ca_gets_catalan_default(self, with_settings):
        service, _ = with_settings(None)
        message = await _build(service, "ca")
        assert "portes 4.5h de jornada oberta a Acme" in message
        assert "Registra la sortida" in message

    async def test_company_en_gets_english_default(self, with_settings):
        service, _ = with_settings(None)
        message = await _build(service, "en")
        assert "your shift at Acme is open 4.5h" in message
        assert "Reminder 2." in message

    async def test_custom_template_for_locale_used(self, with_settings):
        service, _ = with_settings({
            SMS_TEMPLATES_FIELD: {"en": "Hi {%worker_name%}, {%reminder_number%} of 5"}
        })
        message = await _build(service, "en")
        assert message == "Hi Ana, 2 of 5"

    async def test_fallback_chain_custom_en_missing_for_ca(self, with_settings):
        service, _ = with_settings({
            SMS_TEMPLATES_FIELD: {"en": "custom english"}
        })
        # ca has no custom → default ca (not the custom en, not es)
        message = await _build(service, "ca")
        assert message.startswith("OpenJornada: Hola Ana, portes")

    async def test_unknown_locale_falls_to_es(self, with_settings):
        service, _ = with_settings({
            SMS_TEMPLATES_FIELD: {"gl": "galego custom"}
        })
        message = await _build(service, "gl")  # custom exists for gl → used
        assert message == "galego custom"
        message = await _build(service, "eu")  # no custom, no default → es
        assert "llevas 4.5h" in message

    async def test_lazy_migration_legacy_string_is_es(self, with_settings):
        service, _ = with_settings({LEGACY_SMS_TEMPLATE_FIELD: "legacy {%worker_name%}!"})
        assert await _build(service, "es") == "legacy Ana!"
        # other locales unaffected by the legacy es text
        assert "portes" in await _build(service, "ca")

    async def test_marker_substitution_all_four(self, with_settings):
        service, _ = with_settings({
            SMS_TEMPLATES_FIELD: {"es": "{%worker_name%}|{%company_name%}|{%hours_open%}|{%reminder_number%}"}
        })
        assert await _build(service, "es") == "Ana|Acme|4.5|2"

    async def test_long_worker_name_truncated_to_single_segment(self, with_settings):
        long_name = "Christopher Alexander von Habsburg-Lothringen y Borbón"  # 57 chars
        service, _ = with_settings(None)
        message = await service._build_reminder_message(
            worker_name=long_name,
            company_name="Internacional de Servicios Temporales y Consultoria SL",
            hours_open=12.75,
            reminder_number=3,
            notification_locale="es",
        )
        assert fits_single_segment(message) is True
        assert message.endswith("...")

    async def test_truncation_logs_warning(self, with_settings):
        service, _ = with_settings(None)
        with patch("api.services.sms_service.logger") as mock_logger:
            await service._build_reminder_message(
                worker_name="Christopher Alexander von Habsburg-Lothringen y Borbón",
                company_name="Una Empresa Bastante Larga De Jornada Abierta SL",
                hours_open=12.75,
                reminder_number=3,
                notification_locale="es",
            )
        assert any("truncat" in str(call).lower() for call in mock_logger.warning.call_args_list)

    async def test_db_error_degrades_to_default(self):
        service = SmsService()
        mock_db = MagicMock()
        mock_db.Settings.find_one = AsyncMock(side_effect=RuntimeError("mongo down"))
        with patch("api.services.sms_service.db", mock_db):
            message = await service._build_reminder_message(
                worker_name="Ana", company_name="Acme", hours_open=5.0,
                reminder_number=1, notification_locale="en",
            )
        assert "your shift at Acme" in message


class TestAccentFoldingInReminder:
    """BUG A regression: á/í/ó/ú in names must not force UCS-2 truncation."""

    async def test_acute_accented_names_stay_gsm7_and_untruncated(self, with_settings):
        service, _ = with_settings(None)
        message = await service._build_reminder_message(
            worker_name="García Rodríguez",
            company_name="Panadería López",
            hours_open=4.5,
            reminder_number=2,
            notification_locale="es",
        )
        assert message == (
            "OpenJornada: Hola Garcia Rodriguez, llevas 4.5h con jornada abierta "
            "en Panaderia Lopez. Registra tu salida. Aviso 2."
        )
        assert is_gsm7(message) is True
        assert sms_length(message) <= 160
        assert fits_single_segment(message) is True
        assert "…" not in message  # instruction text fully preserved

    async def test_accented_names_do_not_log_truncation_warning(self, with_settings):
        service, _ = with_settings(None)
        with patch("api.services.sms_service.logger") as mock_logger:
            await service._build_reminder_message(
                worker_name="María José Núñez",
                company_name="Pilar SL",
                hours_open=3.0,
                reminder_number=1,
                notification_locale="es",
            )
        assert not mock_logger.warning.called

    async def test_gsm7_accented_names_preserved_in_message(self, with_settings):
        service, _ = with_settings(None)
        message = await service._build_reminder_message(
            worker_name="Iñaki",
            company_name="Cañada & Co.",
            hours_open=2.0,
            reminder_number=1,
            notification_locale="es",
        )
        assert "Iñaki" in message  # ñ is GSM-7 → not folded
        assert "Cañada" in message
        assert is_gsm7(message) is True

    async def test_exotic_char_still_uses_ucs2_truncation_fallback(self, with_settings):
        service, _ = with_settings(None)
        message = await service._build_reminder_message(
            worker_name="Ana 😀",
            company_name="Panadería López",
            hours_open=4.5,
            reminder_number=2,
            notification_locale="es",
        )
        assert "😀" in message  # no GSM-7-compatible form → kept
        assert is_gsm7(message) is False
        assert fits_single_segment(message) is True
        assert message.endswith("…")
        assert sms_length(message) <= 70

    async def test_long_accented_names_still_truncate_safely(self, with_settings):
        service, _ = with_settings(None)
        message = await service._build_reminder_message(
            worker_name="María Fernanda García-Pérez Rodríguez de la Vega",
            company_name="Comercial Internacional de Servicios Temporales y Consultoría SL",
            hours_open=12.75,
            reminder_number=3,
            notification_locale="es",
        )
        # Folding happens BEFORE measuring/truncating…
        assert "á" not in message
        # …and the over-long result still degrades to a safe single segment.
        assert fits_single_segment(message) is True
        assert message.endswith("...")
        assert sms_length(message) <= 160

    async def test_folding_applies_to_custom_templates_too(self, with_settings):
        # Curly quotes pasted into a custom template are folded as well,
        # since folding runs on the final rendered message.
        service, _ = with_settings({
            SMS_TEMPLATES_FIELD: {"es": "Hola \u201c{%worker_name%}\u201d \u2014 revisa tu jornada \u2026"}
        })
        message = await service._build_reminder_message(
            worker_name="Rafael",
            company_name="Acme",
            hours_open=1.0,
            reminder_number=1,
            notification_locale="es",
        )
        assert message == 'Hola "Rafael" - revisa tu jornada ...'
        assert is_gsm7(message) is True


class TestSendShiftReminderLocalePlumbing:
    async def test_locale_forwarded_to_builder(self):
        service = SmsService()
        service._enabled = True
        service._unlimited_balance = True
        provider = AsyncMock()
        provider.send_sms = AsyncMock(return_value=(True, "id", None))
        service._provider = provider

        with patch("api.services.sms_service.db") as mock_db:
            mock_db.Settings.find_one = AsyncMock(return_value=None)
            mock_db.SmsLogs.insert_one = AsyncMock()
            ok = await service.send_shift_reminder(
                worker_id="w1", company_id="c1", time_record_entry_id="t1",
                phone_number="+34600000001", worker_name="Ana",
                company_name="Acme", hours_open=5.0, reminder_number=1,
                notification_locale="ca",
            )
        assert ok is True
        sent_message = provider.send_sms.call_args.kwargs["message"]
        assert "portes" in sent_message  # Catalan default
        assert service._unlimited_balance


# ===========================================================================
# 4.5/4.6 — /sms/template endpoints (map contract)
# ===========================================================================

_admin = APIUser(username="admin", email="admin@example.com", role="admin")


@pytest.fixture()
def client_and_db():
    """TestClient for the sms router + a fake Settings collection.

    PermissionChecker depends on get_current_active_user; overriding that with
    an admin (who holds manage_sms_config) is enough to pass authorization.
    """
    from api.routers import sms as sms_router

    app = FastAPI()
    app.include_router(sms_router.router, prefix="/api")
    app.dependency_overrides[get_current_active_user] = lambda: _admin

    settings_doc: dict = {"_id": "settings1"}

    def _apply(settings_doc, key, value):
        if "." in key:
            field, sub_key = key.split(".", 1)
            settings_doc.setdefault(field, {})[sub_key] = value
        else:
            settings_doc[key] = value

    def _unapply(settings_doc, key):
        if "." in key:
            field, sub_key = key.split(".", 1)
            settings_doc.get(field, {}).pop(sub_key, None)
        else:
            settings_doc.pop(key, None)

    async def _update_one(query, update):
        for key, value in (update.get("$set") or {}).items():
            _apply(settings_doc, key, value)
        for key in (update.get("$unset") or {}):
            _unapply(settings_doc, key)

    fake_db = MagicMock()
    fake_db.Settings.find_one = AsyncMock(side_effect=lambda *a, **k: dict(settings_doc))
    fake_db.Settings.update_one = AsyncMock(side_effect=_update_one)
    fake_db.Settings.insert_one = AsyncMock()

    with patch.object(sms_router, "db", fake_db):
        yield TestClient(app), settings_doc


class TestTemplateEndpoints:
    def test_get_returns_map_contract(self, client_and_db):
        client, _ = client_and_db
        resp = client.get("/api/sms/template")
        assert resp.status_code == 200
        body = resp.json()
        assert body["templates"] == {}
        assert body["default_templates"] == DEFAULT_SMS_TEMPLATES
        assert body["supported_locales"] == ["es", "en", "ca"]
        assert body["available_tags"] == AVAILABLE_TAGS

    def test_get_surfaces_legacy_as_es(self, client_and_db):
        client, settings_doc = client_and_db
        settings_doc[LEGACY_SMS_TEMPLATE_FIELD] = "legacy text"
        body = client.get("/api/sms/template").json()
        assert body["templates"] == {"es": "legacy text"}

    def test_put_without_locale_targets_es(self, client_and_db):
        client, settings_doc = client_and_db
        resp = client.put("/api/sms/template", json={"template": "Recordatorio personalizado de la jornada."})
        assert resp.status_code == 200
        assert settings_doc[SMS_TEMPLATES_FIELD] == {"es": "Recordatorio personalizado de la jornada."}

    def test_put_edits_one_locale_only(self, client_and_db):
        client, settings_doc = client_and_db
        client.put("/api/sms/template", json={"locale": "ca", "template": "Recordatori personalitzat de la jornada."})
        client.put("/api/sms/template", json={"locale": "en", "template": "Your personalized shift reminder text."})
        assert settings_doc[SMS_TEMPLATES_FIELD] == {
            "ca": "Recordatori personalitzat de la jornada.",
            "en": "Your personalized shift reminder text.",
        }
        # Editing ca/en never touches the es default; GET keeps both maps.
        body = client.get("/api/sms/template").json()
        assert "es" not in body["templates"]
        assert body["default_templates"]["es"] == DEFAULT_SMS_TEMPLATES["es"]

    def test_put_supersedes_legacy_es_string(self, client_and_db):
        client, settings_doc = client_and_db
        settings_doc[LEGACY_SMS_TEMPLATE_FIELD] = "legacy text"
        resp = client.put("/api/sms/template", json={"locale": "es", "template": "Nuevo texto de recordatorio es."})
        assert resp.status_code == 200
        assert LEGACY_SMS_TEMPLATE_FIELD not in settings_doc
        assert settings_doc[SMS_TEMPLATES_FIELD]["es"] == "Nuevo texto de recordatorio es."

    def test_reset_single_locale(self, client_and_db):
        client, settings_doc = client_and_db
        client.put("/api/sms/template", json={"locale": "ca", "template": "Text personalitzat en catala."})
        client.put("/api/sms/template", json={"locale": "en", "template": "Custom english reminder text."})
        resp = client.delete("/api/sms/template", params={"locale": "ca"})
        assert resp.status_code == 200
        assert settings_doc[SMS_TEMPLATES_FIELD] == {"en": "Custom english reminder text."}
        body = resp.json()
        assert "ca" not in body["templates"]
        assert body["default_templates"]["ca"] == DEFAULT_SMS_TEMPLATES["ca"]

    def test_reset_es_also_drops_legacy(self, client_and_db):
        client, settings_doc = client_and_db
        settings_doc[LEGACY_SMS_TEMPLATE_FIELD] = "legacy text"
        resp = client.delete("/api/sms/template")  # no locale → es
        assert resp.status_code == 200
        assert LEGACY_SMS_TEMPLATE_FIELD not in settings_doc
        assert client.get("/api/sms/template").json()["templates"] == {}

    def test_put_rejects_invalid_locale(self, client_and_db):
        client, _ = client_and_db
        resp = client.put("/api/sms/template", json={"locale": "fr", "template": "Texte en francais valide."})
        assert resp.status_code == 422
        assert resp.json()["detail"]["error_code"] == "sms.invalid_locale"

    def test_put_rejects_over_160_gsm7(self, client_and_db):
        client, settings_doc = client_and_db
        too_long = "a" * 161
        resp = client.put("/api/sms/template", json={"locale": "en", "template": too_long})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error_code"] == "sms.template_exceeds_segment"
        assert "160" in detail["message"]
        assert SMS_TEMPLATES_FIELD not in settings_doc  # nothing persisted

    def test_put_rejects_over_70_with_ucs2_char(self, client_and_db):
        client, _ = client_and_db
        # 80 chars but one 'í' (acute) forces UCS-2 → over the 70 limit.
        resp = client.put("/api/sms/template", json={"locale": "es", "template": "Jornada í abierta " + "x" * 60})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error_code"] == "sms.template_exceeds_segment"
        assert "70" in detail["message"]

    def test_put_accepts_70_with_ucs2_char(self, client_and_db):
        client, settings_doc = client_and_db
        text = "á" * 69 + "."  # 70 chars, UCS-2 → exactly one segment
        resp = client.put("/api/sms/template", json={"locale": "es", "template": text})
        assert resp.status_code == 200
        assert settings_doc[SMS_TEMPLATES_FIELD]["es"] == text

    def test_put_accepts_160_gsm7(self, client_and_db):
        client, settings_doc = client_and_db
        text = "x" * 159 + "."  # exactly 160 GSM-7 septets
        assert sms_length(text) == 160
        resp = client.put("/api/sms/template", json={"locale": "en", "template": text})
        assert resp.status_code == 200

    def test_extension_chars_count_double(self, client_and_db):
        # 160 chars but with { } extension codes → 164 septets → rejected.
        client, _ = client_and_db
        text = "{%worker_name%} " + "x" * 143 + "."
        assert len(text) == 160 and sms_length(text) > 160
        resp = client.put("/api/sms/template", json={"locale": "en", "template": text})
        assert resp.status_code == 400

    def test_delete_rejects_invalid_locale(self, client_and_db):
        client, _ = client_and_db
        resp = client.delete("/api/sms/template", params={"locale": "de"})
        assert resp.status_code == 422
        assert resp.json()["detail"]["error_code"] == "sms.invalid_locale"


class TestSmsTemplateUpdateModel:
    def test_max_length_480_removed(self):
        # Long texts now pass schema validation; the router applies the
        # dynamic segment rule instead.
        model = SmsTemplateUpdate(template="x" * 400)
        assert model.locale is None

    def test_locale_optional(self):
        assert SmsTemplateUpdate(template="1234567890").locale is None
        assert SmsTemplateUpdate(template="1234567890", locale="ca").locale == "ca"
