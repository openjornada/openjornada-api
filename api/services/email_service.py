"""
Email Service for sending emails via SMTP.

Refactored to use Jinja2 templates for email content.
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .email_renderer import email_renderer

# Configure logging
logger = logging.getLogger(__name__)

# Default timezone for email display (Spain)
EMAIL_DISPLAY_TIMEZONE = "Europe/Madrid"


def convert_to_local_timezone(dt: datetime, tz_name: str = EMAIL_DISPLAY_TIMEZONE) -> datetime:
    """
    Convert a datetime to the specified timezone for display in emails.

    Args:
        dt: datetime object (should be UTC aware)
        tz_name: timezone name (default: Europe/Madrid)

    Returns:
        datetime in the specified timezone
    """
    if dt is None:
        return None

    # If datetime is naive, assume it's UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    # Convert to target timezone
    target_tz = ZoneInfo(tz_name)
    return dt.astimezone(target_tz)


class EmailService:
    """
    Service for sending emails via SMTP.

    Uses EmailRenderer for template rendering and handles SMTP communication.
    """

    def __init__(self):
        """Initialize email service with SMTP configuration from environment."""
        self.smtp_host = os.getenv("SMTP_HOST", "localhost")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.smtp_from_email = os.getenv("SMTP_FROM_EMAIL", "noreply@openjornada.local")
        self.smtp_from_name = os.getenv("SMTP_FROM_NAME", "OpenJornada")
        self.app_name = os.getenv("EMAIL_APP_NAME", "OpenJornada")
        self._executor = ThreadPoolExecutor(max_workers=2)

        logger.info(f"EmailService initialized - SMTP: {self.smtp_host}:{self.smtp_port}")

    def _send_email_sync(
        self,
        to_email: str,
        subject: str,
        text_body: str,
        html_body: str
    ) -> bool:
        """
        Synchronous email sending function.

        Args:
            to_email: Recipient email address
            subject: Email subject
            text_body: Plain text body
            html_body: HTML body

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            logger.info(f"[EMAIL] Starting email send to: {to_email}")
            logger.info(f"[EMAIL] SMTP Config - Host: {self.smtp_host}, Port: {self.smtp_port}, User: {self.smtp_user}")

            # Create message
            message = MIMEMultipart("alternative")
            message["Subject"] = subject
            message["From"] = f"{self.smtp_from_name} <{self.smtp_from_email}>"
            message["To"] = to_email
            logger.info(f"[EMAIL] Message created - From: {self.smtp_from_email}, To: {to_email}, Subject: {subject}")

            # Attach text and HTML parts
            part1 = MIMEText(text_body, "plain")
            part2 = MIMEText(html_body, "html")
            message.attach(part1)
            message.attach(part2)
            logger.info("[EMAIL] Message parts attached")

            # Send email
            logger.info(f"[EMAIL] Connecting to SMTP server {self.smtp_host}:{self.smtp_port}...")
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                logger.info("[EMAIL] Connected to SMTP server")

                logger.info("[EMAIL] Starting TLS...")
                server.starttls()
                logger.info("[EMAIL] TLS started successfully")

                if self.smtp_user and self.smtp_password:
                    logger.info(f"[EMAIL] Logging in as {self.smtp_user}...")
                    server.login(self.smtp_user, self.smtp_password)
                    logger.info("[EMAIL] Login successful")
                else:
                    logger.info("[EMAIL] No authentication credentials provided, skipping login")

                logger.info("[EMAIL] Sending message...")
                server.send_message(message)
                logger.info("[EMAIL] Message sent successfully!")

            return True
        except Exception as e:
            logger.error(f"[EMAIL] Error sending email: {type(e).__name__}: {e}")
            import traceback
            logger.error(f"[EMAIL] Traceback: {traceback.format_exc()}")
            return False

    async def send_password_reset_email(
        self,
        to_email: str,
        worker_name: str,
        reset_token: str,
        webapp_url: str,
        contact_email: str,
        locale: str = 'es'
    ) -> bool:
        """
        Send password reset email to worker.

        Args:
            to_email: Worker's email address
            worker_name: Worker's full name
            reset_token: Password reset token
            webapp_url: Base URL of the webapp
            contact_email: Contact email for support
            locale: Language code for email template (default: 'es')

        Returns:
            True if sent successfully, False otherwise
        """
        logger.info(f"[EMAIL] send_password_reset_email called for: {to_email}")
        logger.info(f"[EMAIL] Worker: {worker_name}, WebApp URL: {webapp_url}, Contact: {contact_email}")

        # Build reset link
        reset_link = f"{webapp_url}/reset-password/{reset_token}"
        logger.info(f"[EMAIL] Reset link generated: {reset_link}")

        try:
            # Render email template
            html_body, text_body = email_renderer.render(
                template_name='password_reset_worker.html',
                context={
                    'app_name': self.app_name,
                    'worker_name': worker_name,
                    'reset_link': reset_link,
                    'contact_email': contact_email
                },
                locale=locale
            )

            # Email subject
            subject = f"Recuperación de contraseña - {self.app_name}"

            # Run sync email sending in thread pool
            logger.info("[EMAIL] Executing email send in thread pool...")
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self._executor,
                self._send_email_sync,
                to_email,
                subject,
                text_body,
                html_body
            )
            logger.info(f"[EMAIL] Email send result: {result}")
            return result

        except Exception as e:
            logger.error(f"[EMAIL] Error preparing email: {e}")
            import traceback
            logger.error(f"[EMAIL] Traceback: {traceback.format_exc()}")
            return False
        
    async def send_welcome_email(
        self,
        to_email: str,
        worker_name: str,
        reset_token: str,
        webapp_url: str,
        contact_email: str,
        locale: str = 'es'
    ) -> bool:
        """
        Send welcome email to new worker with password reset link.

        Args:
            to_email: Worker's email address
            worker_name: Worker's full name
            reset_token: Password reset token
            webapp_url: Base URL of the webapp
            contact_email: Contact email for support
            locale: Language code for email template (default: 'es')

        Returns:
            True if sent successfully, False otherwise
        """
        logger.info(f"[EMAIL] send_password_reset_email called for: {to_email}")
        logger.info(f"[EMAIL] Worker: {worker_name}, WebApp URL: {webapp_url}, Contact: {contact_email}")

        # Build reset link
        reset_link = f"{webapp_url}/reset-password/{reset_token}"
        logger.info(f"[EMAIL] Welcome reset link generated: {reset_link}")

        try:
            # Render email template
            html_body, text_body = email_renderer.render(
                template_name='welcome_worker.html',
                context={
                    'app_name': self.app_name,
                    'worker_name': worker_name,
                    'reset_link': reset_link,
                    'contact_email': contact_email
                },
                locale=locale
            )

            # Email subject
            subject = f"Bienvenido a {self.app_name}"

            # Run sync email sending in thread pool
            logger.info("[EMAIL] Executing email send in thread pool...")
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self._executor,
                self._send_email_sync,
                to_email,
                subject,
                text_body,
                html_body
            )
            logger.info(f"[EMAIL] Welcome email send result: {result}")
            return result

        except Exception as e:
            logger.error(f"[EMAIL] Error preparing welcome email: {e}")
            import traceback
            logger.error(f"[EMAIL] Traceback: {traceback.format_exc()}")
            return False


    async def send_admin_password_reset_email(
        self,
        to_email: str,
        username: str,
        reset_token: str,
        admin_url: str,
        contact_email: str,
        locale: str = 'es'
    ) -> bool:
        """
        Send password reset email to admin user.

        Args:
            to_email: Admin's email address
            username: Admin's username
            reset_token: Password reset token
            admin_url: Base URL of the admin panel
            contact_email: Contact email for support
            locale: Language code for email template (default: 'es')

        Returns:
            True if sent successfully, False otherwise
        """
        logger.info(f"[EMAIL] send_admin_password_reset_email called for: {to_email}")
        logger.info(f"[EMAIL] Username: {username}, Admin URL: {admin_url}, Contact: {contact_email}")

        # Build reset link
        reset_link = f"{admin_url}/reset-password/{reset_token}"
        logger.info(f"[EMAIL] Reset link generated: {reset_link}")

        try:
            # Render email template
            html_body, text_body = email_renderer.render(
                template_name='password_reset_admin.html',
                context={
                    'app_name': self.app_name,
                    'username': username,
                    'reset_link': reset_link,
                    'contact_email': contact_email
                },
                locale=locale
            )

            # Email subject
            subject = f"Recuperación de contraseña - {self.app_name}"

            # Run sync email sending in thread pool
            logger.info("[EMAIL] Executing email send in thread pool...")
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self._executor,
                self._send_email_sync,
                to_email,
                subject,
                text_body,
                html_body
            )
            logger.info(f"[EMAIL] Email send result: {result}")
            return result

        except Exception as e:
            logger.error(f"[EMAIL] Error preparing email: {e}")
            import traceback
            logger.error(f"[EMAIL] Traceback: {traceback.format_exc()}")
            return False

    async def send_admin_welcome_email(
        self,
        to_email: str,
        username: str,
        reset_token: str,
        admin_url: str,
        webapp_url: str,
        contact_email: str,
        locale: str = 'es'
    ) -> bool:
        """
        Send welcome email to new admin user with password setup link.

        Args:
            to_email: Admin's email address
            username: Admin's username
            reset_token: Password reset token for initial password setup
            admin_url: Base URL of the admin panel
            webapp_url: Base URL of the webapp (for workers)
            contact_email: Contact email for support
            locale: Language code for email template (default: 'es')

        Returns:
            True if sent successfully, False otherwise
        """
        logger.info(f"[EMAIL] send_admin_welcome_email called for: {to_email}")
        logger.info(f"[EMAIL] Username: {username}, Admin URL: {admin_url}, Webapp URL: {webapp_url}")

        # Build reset link (using admin URL for admin users)
        reset_link = f"{admin_url}/reset-password/{reset_token}"
        logger.info(f"[EMAIL] Welcome reset link generated: {reset_link}")

        try:
            # Render email template
            html_body, text_body = email_renderer.render(
                template_name='welcome_admin.html',
                context={
                    'app_name': self.app_name,
                    'username': username,
                    'reset_link': reset_link,
                    'admin_url': admin_url,
                    'webapp_url': webapp_url,
                    'contact_email': contact_email
                },
                locale=locale
            )

            # Email subject
            subject = f"Bienvenido a {self.app_name} - Panel de Administracion"

            # Run sync email sending in thread pool
            logger.info("[EMAIL] Executing admin welcome email send in thread pool...")
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self._executor,
                self._send_email_sync,
                to_email,
                subject,
                text_body,
                html_body
            )
            logger.info(f"[EMAIL] Admin welcome email send result: {result}")
            return result

        except Exception as e:
            logger.error(f"[EMAIL] Error preparing admin welcome email: {e}")
            import traceback
            logger.error(f"[EMAIL] Traceback: {traceback.format_exc()}")
            return False

    async def send_change_request_rejected_email(
        self,
        to_email: str,
        worker_name: str,
        company_name: str,
        record_type: str,
        original_datetime,
        new_datetime,
        reason: str,
        admin_public_comment: str,
        contact_email: str,
        locale: str = 'es'
    ) -> bool:
        """
        Send change request rejection email to worker.

        Args:
            to_email: Worker's email address
            worker_name: Worker's full name
            company_name: Company name
            record_type: "Entrada" or "Salida"
            original_datetime: Original datetime of the record
            new_datetime: Requested new datetime
            reason: Worker's reason for the change request
            admin_public_comment: Admin's public comment (can be empty)
            contact_email: Contact email for support
            locale: Language code for email template (default: 'es')

        Returns:
            True if sent successfully, False otherwise
        """
        logger.info(f"[EMAIL] send_change_request_rejected_email called for: {to_email}")

        try:
            # Convert datetimes to local timezone for display
            original_datetime_local = convert_to_local_timezone(original_datetime)
            new_datetime_local = convert_to_local_timezone(new_datetime)

            # Render email template
            html_body, text_body = email_renderer.render(
                template_name='change_request_rejected.html',
                context={
                    'app_name': self.app_name,
                    'worker_name': worker_name,
                    'company_name': company_name,
                    'record_type': record_type,
                    'original_datetime': original_datetime_local,
                    'new_datetime': new_datetime_local,
                    'reason': reason,
                    'admin_public_comment': admin_public_comment,
                    'contact_email': contact_email
                },
                locale=locale
            )

            # Email subject
            subject = f"Tu petición de cambio ha sido rechazada - {self.app_name}"

            # Run sync email sending in thread pool
            logger.info("[EMAIL] Executing change request rejection email send in thread pool...")
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self._executor,
                self._send_email_sync,
                to_email,
                subject,
                text_body,
                html_body
            )
            logger.info(f"[EMAIL] Change request rejection email send result: {result}")
            return result

        except Exception as e:
            logger.error(f"[EMAIL] Error preparing change request rejection email: {e}")
            import traceback
            logger.error(f"[EMAIL] Traceback: {traceback.format_exc()}")
            return False

    async def send_change_request_accepted_email(
        self,
        to_email: str,
        worker_name: str,
        company_name: str,
        record_type: str,
        original_datetime,
        new_datetime,
        reason: str,
        admin_public_comment: str,
        contact_email: str,
        locale: str = 'es'
    ) -> bool:
        """
        Send change request acceptance email to worker.

        Args:
            to_email: Worker's email address
            worker_name: Worker's full name
            company_name: Company name
            record_type: "Entrada" or "Salida"
            original_datetime: Original datetime of the record
            new_datetime: Requested new datetime (now applied)
            reason: Worker's reason for the change request
            admin_public_comment: Admin's public comment (can be empty)
            contact_email: Contact email for support
            locale: Language code for email template (default: 'es')

        Returns:
            True if sent successfully, False otherwise
        """
        logger.info(f"[EMAIL] send_change_request_accepted_email called for: {to_email}")

        try:
            # Convert datetimes to local timezone for display
            original_datetime_local = convert_to_local_timezone(original_datetime)
            new_datetime_local = convert_to_local_timezone(new_datetime)

            # Render email template
            html_body, text_body = email_renderer.render(
                template_name='change_request_accepted.html',
                context={
                    'app_name': self.app_name,
                    'worker_name': worker_name,
                    'company_name': company_name,
                    'record_type': record_type,
                    'original_datetime': original_datetime_local,
                    'new_datetime': new_datetime_local,
                    'reason': reason,
                    'admin_public_comment': admin_public_comment,
                    'contact_email': contact_email
                },
                locale=locale
            )

            # Email subject
            subject = f"Tu petición de cambio ha sido aceptada - {self.app_name}"

            # Send email
            logger.info("[EMAIL] Executing change request acceptance email send in thread pool...")
            import asyncio
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self._executor,
                self._send_email_sync,
                to_email,
                subject,
                text_body,
                html_body
            )
            logger.info(f"[EMAIL] Change request acceptance email send result: {result}")
            return result

        except Exception as e:
            logger.error(f"[EMAIL] Error preparing change request acceptance email: {e}")
            import traceback
            logger.error(f"[EMAIL] Traceback: {traceback.format_exc()}")
            return False


# Singleton instance
email_service = EmailService()
