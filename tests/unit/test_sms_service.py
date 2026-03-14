"""
Unit tests for SmsService.

All tests are pure unit tests — no MongoDB or HTTP server required.
External dependencies (database, encryption, HTTP) are fully mocked.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.sms_service import SmsService


# ===========================================================================
# Helpers
# ===========================================================================

def _make_service() -> SmsService:
    """Return a fresh, uninitialised SmsService instance."""
    return SmsService()


def _db_settings_with_sms(enabled: bool = True, encrypted_token: str = "enc_tok") -> dict:
    """Return a minimal Settings document with sms_provider_config."""
    return {
        "sms_provider_config": {
            "enabled": enabled,
            "provider": "labsmobile",
            "api_token_encrypted": encrypted_token,
            "sender_id": "TestSender",
        }
    }


# ===========================================================================
# initialize() — environment-variable path
# ===========================================================================

class TestInitializeFromEnv:
    """initialize() reads SMS config from env vars when they are present."""

    async def test_enabled_with_token_sets_enabled_true(self):
        """SMS_ENABLED=true + token in env → is_enabled() returns True."""
        service = _make_service()

        env = {
            "SMS_ENABLED": "true",
            "SMS_LABSMOBILE_API_TOKEN": "base64token==",
            "SMS_SENDER_ID": "OpenJornada",
            "SMS_UNLIMITED_BALANCE": "0",
        }
        with patch("api.services.sms_service.os.getenv", side_effect=lambda k, d="": env.get(k, d)):
            await service.initialize()

        assert service.is_enabled() is True

    async def test_enabled_flag_false_sets_enabled_false(self):
        """SMS_ENABLED=false → is_enabled() returns False even with a token."""
        service = _make_service()

        env = {
            "SMS_ENABLED": "false",
            "SMS_LABSMOBILE_API_TOKEN": "base64token==",
            "SMS_SENDER_ID": "OpenJornada",
            "SMS_UNLIMITED_BALANCE": "0",
        }
        # DB returns nothing so it does not accidentally enable the service.
        with patch("api.services.sms_service.os.getenv", side_effect=lambda k, d="": env.get(k, d)), \
             patch("api.services.sms_service.db") as mock_db:
            mock_db.Settings.find_one = AsyncMock(return_value=None)
            await service.initialize()

        assert service.is_enabled() is False

    async def test_env_var_not_set_defaults_to_disabled(self):
        """
        When SMS_ENABLED is not set at all os.getenv returns the default "false",
        so is_enabled() must return False.  This is the regression test for the
        bug where an unset variable was treated as enabled.
        """
        service = _make_service()

        # Simulate a completely empty environment — no SMS vars present.
        with patch("api.services.sms_service.os.getenv", side_effect=lambda k, d="": d), \
             patch("api.services.sms_service.db") as mock_db:
            mock_db.Settings.find_one = AsyncMock(return_value=None)
            await service.initialize()

        assert service.is_enabled() is False

    async def test_enabled_true_without_token_falls_through_to_db(self):
        """SMS_ENABLED=true but empty token → env branch skipped, falls to DB."""
        service = _make_service()

        env = {
            "SMS_ENABLED": "true",
            "SMS_LABSMOBILE_API_TOKEN": "",  # no token
            "SMS_SENDER_ID": "OpenJornada",
            "SMS_UNLIMITED_BALANCE": "0",
        }
        with patch("api.services.sms_service.os.getenv", side_effect=lambda k, d="": env.get(k, d)), \
             patch("api.services.sms_service.db") as mock_db:
            mock_db.Settings.find_one = AsyncMock(return_value=None)
            await service.initialize()

        # No DB config either, so must end up disabled.
        assert service.is_enabled() is False


# ===========================================================================
# initialize() — database fallback path
# ===========================================================================

class TestInitializeFromDb:
    """initialize() falls back to DB when env vars are absent or insufficient."""

    async def test_db_config_enabled_sets_enabled_true(self):
        """Valid encrypted config in DB with enabled=true → is_enabled() True."""
        service = _make_service()

        settings_doc = _db_settings_with_sms(enabled=True, encrypted_token="enc_tok")

        with patch("api.services.sms_service.os.getenv", side_effect=lambda k, d="": d), \
             patch("api.services.sms_service.db") as mock_db, \
             patch("api.services.sms_service.credential_encryption") as mock_enc:

            mock_db.Settings.find_one = AsyncMock(return_value=settings_doc)
            mock_enc.decrypt.return_value = "decrypted_api_token"

            await service.initialize()

        assert service.is_enabled() is True
        mock_enc.decrypt.assert_called_once_with("enc_tok")

    async def test_db_config_disabled_sets_enabled_false(self):
        """DB config with enabled=false → is_enabled() returns False."""
        service = _make_service()

        settings_doc = _db_settings_with_sms(enabled=False)

        with patch("api.services.sms_service.os.getenv", side_effect=lambda k, d="": d), \
             patch("api.services.sms_service.db") as mock_db, \
             patch("api.services.sms_service.credential_encryption") as mock_enc:

            mock_db.Settings.find_one = AsyncMock(return_value=settings_doc)
            await service.initialize()

        assert service.is_enabled() is False
        mock_enc.decrypt.assert_not_called()

    async def test_db_returns_none_leaves_service_disabled(self):
        """No Settings document in DB → service stays disabled."""
        service = _make_service()

        with patch("api.services.sms_service.os.getenv", side_effect=lambda k, d="": d), \
             patch("api.services.sms_service.db") as mock_db:

            mock_db.Settings.find_one = AsyncMock(return_value=None)
            await service.initialize()

        assert service.is_enabled() is False

    async def test_db_exception_leaves_service_disabled(self):
        """If DB raises an exception, service degrades gracefully to disabled."""
        service = _make_service()

        with patch("api.services.sms_service.os.getenv", side_effect=lambda k, d="": d), \
             patch("api.services.sms_service.db") as mock_db:

            mock_db.Settings.find_one = AsyncMock(side_effect=RuntimeError("mongo down"))
            await service.initialize()

        assert service.is_enabled() is False


# ===========================================================================
# is_enabled()
# ===========================================================================

class TestIsEnabled:
    """is_enabled() requires both _enabled=True AND a non-None provider."""

    def test_returns_false_when_enabled_flag_true_but_no_provider(self):
        """Guard: if somehow _enabled is True but provider is None → False."""
        service = _make_service()
        service._enabled = True
        service._provider = None

        assert service.is_enabled() is False

    def test_returns_true_when_both_flag_and_provider_set(self):
        """Happy path: flag True + provider object → True."""
        service = _make_service()
        service._enabled = True
        service._provider = MagicMock()

        assert service.is_enabled() is True

    def test_returns_false_when_both_unset(self):
        """Default state after construction → disabled."""
        service = _make_service()

        assert service.is_enabled() is False


# ===========================================================================
# is_unlimited_balance()
# ===========================================================================

class TestIsUnlimitedBalance:
    """is_unlimited_balance() reflects the SMS_UNLIMITED_BALANCE env var."""

    async def test_unlimited_balance_env_var_one(self):
        """SMS_UNLIMITED_BALANCE=1 → is_unlimited_balance() True."""
        service = _make_service()

        env = {"SMS_UNLIMITED_BALANCE": "1"}
        with patch("api.services.sms_service.os.getenv", side_effect=lambda k, d="": env.get(k, d)), \
             patch("api.services.sms_service.db") as mock_db:
            mock_db.Settings.find_one = AsyncMock(return_value=None)
            await service.initialize()

        assert service.is_unlimited_balance() is True

    async def test_unlimited_balance_env_var_zero(self):
        """SMS_UNLIMITED_BALANCE=0 (default) → is_unlimited_balance() False."""
        service = _make_service()

        env = {"SMS_UNLIMITED_BALANCE": "0"}
        with patch("api.services.sms_service.os.getenv", side_effect=lambda k, d="": env.get(k, d)), \
             patch("api.services.sms_service.db") as mock_db:
            mock_db.Settings.find_one = AsyncMock(return_value=None)
            await service.initialize()

        assert service.is_unlimited_balance() is False

    async def test_unlimited_balance_not_set_defaults_false(self):
        """Absent env var → default '0' → False."""
        service = _make_service()

        with patch("api.services.sms_service.os.getenv", side_effect=lambda k, d="": d), \
             patch("api.services.sms_service.db") as mock_db:
            mock_db.Settings.find_one = AsyncMock(return_value=None)
            await service.initialize()

        assert service.is_unlimited_balance() is False


# ===========================================================================
# send_shift_reminder()
# ===========================================================================

SHIFT_REMINDER_DEFAULTS = dict(
    worker_id="w1",
    company_id="c1",
    time_record_entry_id="tr1",
    phone_number="+34600000001",
    worker_name="Ana García",
    company_name="Empresa SL",
    hours_open=5.0,
    reminder_number=1,
    worker_id_number="12345678A",
)


class TestSendShiftReminder:
    """send_shift_reminder() guards and happy path."""

    async def test_returns_false_when_service_disabled(self):
        """Disabled service → False, provider never called."""
        service = _make_service()
        mock_provider = AsyncMock()
        service._provider = mock_provider
        service._enabled = False  # is_enabled() → False because flag is False

        result = await service.send_shift_reminder(**SHIFT_REMINDER_DEFAULTS)

        assert result is False
        mock_provider.send_sms.assert_not_called()

    async def test_returns_false_when_no_unlimited_balance(self):
        """Enabled service but balance not unlimited → False, provider not called."""
        service = _make_service()
        mock_provider = AsyncMock()
        service._enabled = True
        service._provider = mock_provider
        service._unlimited_balance = False

        result = await service.send_shift_reminder(**SHIFT_REMINDER_DEFAULTS)

        assert result is False
        mock_provider.send_sms.assert_not_called()

    async def test_sends_sms_and_logs_when_enabled_and_unlimited(self):
        """
        Enabled + unlimited balance → provider.send_sms is called once, result
        logged to SmsLogs, and the method returns True.
        """
        service = _make_service()
        mock_provider = AsyncMock()
        mock_provider.send_sms.return_value = (True, "msg_id_123", None)
        service._enabled = True
        service._provider = mock_provider
        service._unlimited_balance = True

        with patch("api.services.sms_service.db") as mock_db:
            mock_db.Settings.find_one = AsyncMock(return_value=None)
            mock_db.SmsLogs.insert_one = AsyncMock()

            result = await service.send_shift_reminder(**SHIFT_REMINDER_DEFAULTS)

        assert result is True
        mock_provider.send_sms.assert_awaited_once()
        mock_db.SmsLogs.insert_one.assert_awaited_once()

        # Inspect the logged document.
        log_doc = mock_db.SmsLogs.insert_one.call_args[0][0]
        assert log_doc["status"] == "sent"
        assert log_doc["provider_message_id"] == "msg_id_123"
        assert log_doc["message_type"] == "shift_reminder"
        assert log_doc["worker_id"] == "w1"
        assert log_doc["company_id"] == "c1"
        assert log_doc["cost_credits"] == 1.0

    async def test_logs_failed_status_when_provider_returns_error(self):
        """Provider failure → returns False, log entry has status 'failed'."""
        service = _make_service()
        mock_provider = AsyncMock()
        mock_provider.send_sms.return_value = (False, None, "Network error")
        service._enabled = True
        service._provider = mock_provider
        service._unlimited_balance = True

        with patch("api.services.sms_service.db") as mock_db:
            mock_db.Settings.find_one = AsyncMock(return_value=None)
            mock_db.SmsLogs.insert_one = AsyncMock()

            result = await service.send_shift_reminder(**SHIFT_REMINDER_DEFAULTS)

        assert result is False
        log_doc = mock_db.SmsLogs.insert_one.call_args[0][0]
        assert log_doc["status"] == "failed"
        assert log_doc["error_message"] == "Network error"
        assert log_doc["cost_credits"] == 0.0


# ===========================================================================
# send_custom_sms()
# ===========================================================================

CUSTOM_SMS_DEFAULTS = dict(
    worker_id="w2",
    company_id="c2",
    phone_number="+34600000002",
    message="Hola, este es un mensaje personalizado.",
    worker_name="Luis López",
    worker_id_number="87654321B",
)


class TestSendCustomSms:
    """send_custom_sms() guards and happy path."""

    async def test_returns_false_and_error_when_service_disabled(self):
        """Disabled service → (False, error_message), provider never called."""
        service = _make_service()
        mock_provider = AsyncMock()
        service._enabled = False
        service._provider = None

        success, error = await service.send_custom_sms(**CUSTOM_SMS_DEFAULTS)

        assert success is False
        assert error is not None
        assert len(error) > 0
        mock_provider.send_sms.assert_not_called()

    async def test_returns_false_when_no_unlimited_balance(self):
        """Enabled but balance not unlimited → (False, error_message)."""
        service = _make_service()
        mock_provider = AsyncMock()
        service._enabled = True
        service._provider = mock_provider
        service._unlimited_balance = False

        success, error = await service.send_custom_sms(**CUSTOM_SMS_DEFAULTS)

        assert success is False
        assert error is not None
        mock_provider.send_sms.assert_not_called()

    async def test_sends_sms_and_logs_when_enabled_and_unlimited(self):
        """
        Enabled + unlimited → provider.send_sms called, log inserted,
        returns (True, None).
        """
        service = _make_service()
        mock_provider = AsyncMock()
        mock_provider.send_sms.return_value = (True, "custom_id_456", None)
        service._enabled = True
        service._provider = mock_provider
        service._unlimited_balance = True

        with patch("api.services.sms_service.db") as mock_db:
            mock_db.SmsLogs.insert_one = AsyncMock()

            success, error = await service.send_custom_sms(**CUSTOM_SMS_DEFAULTS)

        assert success is True
        assert error is None
        mock_provider.send_sms.assert_awaited_once_with(
            phone_number="+34600000002",
            message=CUSTOM_SMS_DEFAULTS["message"],
        )
        mock_db.SmsLogs.insert_one.assert_awaited_once()

        log_doc = mock_db.SmsLogs.insert_one.call_args[0][0]
        assert log_doc["message_type"] == "custom"
        assert log_doc["status"] == "sent"
        assert log_doc["worker_id"] == "w2"

    async def test_logs_failed_status_when_provider_returns_error(self):
        """Provider failure on custom SMS → (False, error), log status='failed'."""
        service = _make_service()
        mock_provider = AsyncMock()
        mock_provider.send_sms.return_value = (False, None, "Auth error")
        service._enabled = True
        service._provider = mock_provider
        service._unlimited_balance = True

        with patch("api.services.sms_service.db") as mock_db:
            mock_db.SmsLogs.insert_one = AsyncMock()

            success, error = await service.send_custom_sms(**CUSTOM_SMS_DEFAULTS)

        assert success is False
        assert error == "Auth error"
        log_doc = mock_db.SmsLogs.insert_one.call_args[0][0]
        assert log_doc["status"] == "failed"
        assert log_doc["cost_credits"] == 0.0


# ===========================================================================
# reload()
# ===========================================================================

class TestReload:
    """reload() must reset state before delegating to initialize()."""

    async def test_reload_resets_state_and_calls_initialize(self):
        """
        After reload(), a previously-enabled service becomes disabled when
        initialize() finds no valid configuration.
        """
        service = _make_service()
        # Simulate a previously initialised (enabled) state.
        service._enabled = True
        service._provider = MagicMock()
        service._unlimited_balance = True

        with patch("api.services.sms_service.os.getenv", side_effect=lambda k, d="": d), \
             patch("api.services.sms_service.db") as mock_db:
            mock_db.Settings.find_one = AsyncMock(return_value=None)
            await service.reload()

        # State must have been reset and re-evaluated.
        assert service.is_enabled() is False
        assert service._provider is None

    async def test_reload_enables_service_when_db_now_has_config(self):
        """
        reload() picks up a newly-added DB config after a settings update.
        """
        service = _make_service()
        # Start disabled.
        service._enabled = False
        service._provider = None

        settings_doc = _db_settings_with_sms(enabled=True, encrypted_token="enc_tok")

        with patch("api.services.sms_service.os.getenv", side_effect=lambda k, d="": d), \
             patch("api.services.sms_service.db") as mock_db, \
             patch("api.services.sms_service.credential_encryption") as mock_enc:

            mock_db.Settings.find_one = AsyncMock(return_value=settings_doc)
            mock_enc.decrypt.return_value = "fresh_token"

            await service.reload()

        assert service.is_enabled() is True
