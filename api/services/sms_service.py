"""
SMS Service for sending shift reminder notifications.

Supports LabsMobile provider via REST JSON API.
Credentials are encrypted at rest using the same mechanism as backup_config.
"""

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

import httpx

from ..database import db
from ..models.sms import DEFAULT_SMS_TEMPLATE
from ..utils.encryption import credential_encryption

logger = logging.getLogger(__name__)


def _mask_phone(phone: str) -> str:
    """Mask phone number for logging, showing only last 4 digits."""
    return f"***{phone[-4:]}" if len(phone) >= 4 else "***"


# ============================================================================
# Abstract Provider
# ============================================================================

class SmsProvider(ABC):
    """Abstract base class for SMS providers."""

    @abstractmethod
    async def send_sms(self, phone_number: str, message: str) -> tuple[bool, Optional[str], Optional[str]]:
        """
        Send an SMS message.

        Returns:
            Tuple of (success, provider_message_id, error_message)
        """

    @abstractmethod
    async def close(self) -> None:
        """Close any underlying HTTP connections."""


# ============================================================================
# LabsMobile Provider
# ============================================================================

class LabsMobileProvider(SmsProvider):
    """
    LabsMobile SMS provider implementation.

    API: https://api.labsmobile.com/json/send
    Auth: HTTP Basic with base64(username:api_key) token
    """

    _API_ENDPOINT = "https://api.labsmobile.com/json/send"

    def __init__(self, api_token: str, sender_id: str = "OpenJornada"):
        """
        Initialize LabsMobile provider.

        Args:
            api_token: Base64-encoded "username:api_key" token for Basic auth
            sender_id: SMS sender identifier (tpoa)
        """
        self._api_token = api_token
        self._sender_id = sender_id
        self._client = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def send_sms(self, phone_number: str, message: str) -> tuple[bool, Optional[str], Optional[str]]:
        """Send SMS via LabsMobile REST API."""
        payload = {
            "message": message,
            "tpoa": self._sender_id,
            "recipient": [{"msisdn": phone_number}]
        }
        headers = {
            "Authorization": f"Basic {self._api_token}",
            "Content-Type": "application/json"
        }

        try:
            response = await self._client.post(
                self._API_ENDPOINT,
                json=payload,
                headers=headers
            )
            response.raise_for_status()
            data = response.json()

            # LabsMobile returns {"code": "0", "message": "..."} on success
            code = str(data.get("code", ""))
            if code == "0":
                message_id = data.get("subid") or data.get("message", "")
                logger.info(f"[SMS] LabsMobile sent to {_mask_phone(phone_number)}, subid={message_id}")
                return True, message_id, None
            else:
                error_msg = data.get("message", f"Provider error code {code}")
                logger.warning(f"[SMS] LabsMobile rejected send to {_mask_phone(phone_number)}: {error_msg}")
                return False, None, error_msg

        except httpx.HTTPStatusError as e:
            error_msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            logger.error(f"[SMS] LabsMobile HTTP error for {_mask_phone(phone_number)}: {error_msg}")
            return False, None, error_msg
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            logger.error(f"[SMS] LabsMobile unexpected error for {_mask_phone(phone_number)}: {error_msg}")
            return False, None, error_msg


# ============================================================================
# SMS Service
# ============================================================================

class SmsService:
    """
    Main SMS service managing provider, sending, and credit accounting.

    Singleton initialized on startup. Provider configuration is loaded
    from Settings collection (encrypted credentials).
    """

    def __init__(self):
        self._provider: Optional[SmsProvider] = None
        self._enabled: bool = False
        self._sender_id: str = "OpenJornada"
        self._unlimited_balance: bool = False

    async def initialize(self):
        """Load provider configuration from DB settings and environment."""
        # SMS balance mode
        self._unlimited_balance = os.getenv("SMS_UNLIMITED_BALANCE", "0") == "1"

        # Environment variables take priority over DB config for backwards compat
        env_enabled = os.getenv("SMS_ENABLED", "false").lower() == "true"
        env_token = os.getenv("SMS_LABSMOBILE_API_TOKEN", "")
        env_sender = os.getenv("SMS_SENDER_ID", "OpenJornada")

        if env_enabled and env_token:
            self._provider = LabsMobileProvider(
                api_token=env_token,
                sender_id=env_sender
            )
            self._enabled = True
            self._sender_id = env_sender
            logger.info("[SMS] Initialized from environment variables")
            return

        # Try to load from DB settings
        try:
            settings = await db.Settings.find_one()
            if settings:
                sms_config = settings.get("sms_provider_config")
                if sms_config and sms_config.get("enabled"):
                    encrypted_token = sms_config.get("api_token_encrypted", "")
                    if encrypted_token:
                        api_token = credential_encryption.decrypt(encrypted_token)
                        sender_id = sms_config.get("sender_id", "OpenJornada")
                        provider_name = sms_config.get("provider", "labsmobile")
                        if provider_name == "labsmobile":
                            self._provider = LabsMobileProvider(
                                api_token=api_token,
                                sender_id=sender_id
                            )
                            self._enabled = True
                            self._sender_id = sender_id
                            logger.info("[SMS] Initialized from DB settings")
                            return
        except Exception as e:
            logger.error(f"[SMS] Error loading config from DB: {e}")

        logger.info("[SMS] SMS service disabled (no valid configuration)")
        self._enabled = False
        self._provider = None

    async def close(self):
        """Close the underlying provider HTTP client, if any."""
        if self._provider is not None:
            await self._provider.close()

    async def reload(self):
        """Reload provider configuration (call after settings update)."""
        await self.close()
        self._provider = None
        self._enabled = False
        await self.initialize()

    def is_enabled(self) -> bool:
        return self._enabled and self._provider is not None

    def is_unlimited_balance(self) -> bool:
        return self._unlimited_balance

    async def _build_reminder_message(
        self,
        worker_name: str,
        company_name: str,
        hours_open: float,
        reminder_number: int
    ) -> str:
        """Build the SMS text from the DB template or use default."""
        template = DEFAULT_SMS_TEMPLATE
        try:
            settings = await db.Settings.find_one()
            if settings and "sms_reminder_template" in settings:
                template = settings["sms_reminder_template"]
        except Exception as e:
            logger.error(f"[SMS] Error loading template from DB: {e}")

        message = template
        message = message.replace("{%worker_name%}", worker_name)
        message = message.replace("{%company_name%}", company_name)
        message = message.replace("{%hours_open%}", f"{hours_open:.1f}")
        message = message.replace("{%reminder_number%}", str(reminder_number))
        return message

    async def send_custom_sms(
        self,
        worker_id: str,
        company_id: str,
        phone_number: str,
        message: str,
        worker_name: Optional[str] = None,
        worker_id_number: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Send a custom/manual SMS to a worker.

        Returns:
            Tuple of (success, error_message)
        """
        if not self.is_enabled():
            return False, "El servicio SMS no está habilitado"

        if not self._unlimited_balance:
            return False, "SMS no disponible: saldo no configurado como ilimitado"

        success, provider_message_id, error_message = await self._provider.send_sms(
            phone_number=phone_number,
            message=message
        )

        now = datetime.now(timezone.utc)
        sms_status = "sent" if success else "failed"

        log_entry = {
            "worker_id": worker_id,
            "company_id": company_id,
            "phone_number": phone_number,
            "time_record_entry_id": "",
            "message_type": "custom",
            "reminder_number": 0,
            "status": sms_status,
            "provider": "labsmobile",
            "provider_message_id": provider_message_id,
            "error_message": error_message,
            "cost_credits": 1.0 if success else 0.0,
            "worker_name": worker_name,
            "worker_id_number": worker_id_number,
            "message": message,
            "created_at": now,
            "delivered_at": None,
        }
        await db.SmsLogs.insert_one(log_entry)

        return success, error_message

    async def send_shift_reminder(
        self,
        worker_id: str,
        company_id: str,
        time_record_entry_id: str,
        phone_number: str,
        worker_name: str,
        company_name: str,
        hours_open: float,
        reminder_number: int,
        worker_id_number: Optional[str] = None,
    ) -> bool:
        """
        Send a shift reminder SMS and record it in SmsLogs / SmsCredits.

        Returns True if sent successfully, False otherwise.
        """
        if not self.is_enabled():
            logger.debug("[SMS] Service disabled, skipping send_shift_reminder")
            return False

        # 1. Check balance mode
        if not self._unlimited_balance:
            logger.warning(
                f"[SMS] SMS credits via Stripe not configured yet. "
                f"Set SMS_UNLIMITED_BALANCE=1 to enable unlimited sending."
            )
            return False

        # 2. Build message
        message = await self._build_reminder_message(
            worker_name=worker_name,
            company_name=company_name,
            hours_open=hours_open,
            reminder_number=reminder_number
        )

        # 3. Send via provider
        success, provider_message_id, error_message = await self._provider.send_sms(
            phone_number=phone_number,
            message=message
        )

        now = datetime.now(timezone.utc)
        sms_status = "sent" if success else "failed"

        # 4. Record in SmsLogs — denormalize worker fields for history display
        log_entry = {
            "worker_id": worker_id,
            "company_id": company_id,
            "phone_number": phone_number,
            "time_record_entry_id": time_record_entry_id,
            "message_type": "shift_reminder",
            "reminder_number": reminder_number,
            "status": sms_status,
            "provider": "labsmobile",
            "provider_message_id": provider_message_id,
            "error_message": error_message,
            "cost_credits": 1.0 if success else 0.0,
            # Denormalized fields for frontend history display
            "worker_name": worker_name,
            "worker_id_number": worker_id_number,
            "message": message,
            "created_at": now,
            "delivered_at": None
        }
        await db.SmsLogs.insert_one(log_entry)

        return success


# Singleton
sms_service = SmsService()
