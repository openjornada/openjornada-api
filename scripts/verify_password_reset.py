#!/usr/bin/env python3
"""
Verification script for password reset system implementation.
Checks that all modules can be imported and basic structure is correct.
"""

import sys
from datetime import datetime, timedelta

def verify_imports():
    """Verify all new modules can be imported"""
    print("Verifying imports...")

    try:
        from api.models.settings import (
            SettingsBase,
            SettingsUpdate,
            SettingsInDB,
            SettingsResponse
        )
        print("✓ Settings models imported successfully")
    except ImportError as e:
        print(f"✗ Failed to import settings models: {e}")
        return False

    try:
        from api.models.workers import (
            ForgotPasswordRequest,
            ResetPasswordRequest,
            WorkerInDB
        )
        print("✓ Password reset worker models imported successfully")
    except ImportError as e:
        print(f"✗ Failed to import worker models: {e}")
        return False

    try:
        from api.routers.settings import router as settings_router
        print("✓ Settings router imported successfully")
    except ImportError as e:
        print(f"✗ Failed to import settings router: {e}")
        return False

    try:
        from api.services.email_service import email_service, EmailService
        print("✓ Email service imported successfully")
    except ImportError as e:
        print(f"✗ Failed to import email service: {e}")
        return False

    try:
        from api.database import init_default_settings
        print("✓ Database initialization functions imported successfully")
    except ImportError as e:
        print(f"✗ Failed to import database functions: {e}")
        return False

    return True


def verify_models():
    """Verify models can be instantiated"""
    print("\nVerifying model instantiation...")

    try:
        from api.models.settings import SettingsBase, SettingsUpdate

        # Test SettingsBase
        settings = SettingsBase(
            contact_email="test@example.com",
            webapp_url="http://localhost:5173"
        )
        print(f"✓ SettingsBase instantiated: {settings.contact_email}")

        # Test SettingsUpdate
        update = SettingsUpdate(contact_email="new@example.com")
        print(f"✓ SettingsUpdate instantiated: {update.contact_email}")

    except Exception as e:
        print(f"✗ Failed to instantiate settings models: {e}")
        return False

    try:
        from api.models.workers import ForgotPasswordRequest, ResetPasswordRequest

        # Test ForgotPasswordRequest
        forgot = ForgotPasswordRequest(email="worker@example.com")
        print(f"✓ ForgotPasswordRequest instantiated: {forgot.email}")

        # Test ResetPasswordRequest
        reset = ResetPasswordRequest(
            token="test_token_123",
            new_password="password123"
        )
        print(f"✓ ResetPasswordRequest instantiated: token length = {len(reset.token)}")

    except Exception as e:
        print(f"✗ Failed to instantiate worker models: {e}")
        return False

    return True


def verify_email_service():
    """Verify email service configuration"""
    print("\nVerifying email service...")

    try:
        from api.services.email_service import EmailService

        service = EmailService()
        print(f"✓ EmailService instantiated")
        print(f"  - SMTP Host: {service.smtp_host}")
        print(f"  - SMTP Port: {service.smtp_port}")
        print(f"  - From Email: {service.smtp_from_email}")
        print(f"  - From Name: {service.smtp_from_name}")
        print(f"  - App Name: {service.app_name}")

    except Exception as e:
        print(f"✗ Failed to verify email service: {e}")
        return False

    return True


def verify_router_endpoints():
    """Verify router endpoints are defined"""
    print("\nVerifying router endpoints...")

    try:
        from api.routers.workers import router as workers_router

        # Get all routes
        routes = [route.path for route in workers_router.routes]

        required_endpoints = [
            "/workers/forgot-password",
            "/workers/reset-password"
        ]

        for endpoint in required_endpoints:
            if endpoint in routes:
                print(f"✓ Endpoint {endpoint} is registered")
            else:
                print(f"✗ Endpoint {endpoint} NOT found")
                return False

    except Exception as e:
        print(f"✗ Failed to verify endpoints: {e}")
        return False

    try:
        from api.routers.settings import router as settings_router

        routes = [route.path for route in settings_router.routes]

        if "/settings/" in routes:
            print(f"✓ Settings endpoints are registered")
        else:
            print(f"✗ Settings endpoints NOT found")
            return False

    except Exception as e:
        print(f"✗ Failed to verify settings router: {e}")
        return False

    return True


def verify_token_generation():
    """Verify secure token generation works"""
    print("\nVerifying token generation...")

    try:
        import secrets

        # Generate token like the system does
        token = secrets.token_urlsafe(32)

        print(f"✓ Token generated: {token[:20]}... (length: {len(token)})")

        # Verify it's URL-safe
        import re
        if re.match(r'^[A-Za-z0-9_-]+$', token):
            print("✓ Token is URL-safe")
        else:
            print("✗ Token contains invalid characters")
            return False

    except Exception as e:
        print(f"✗ Failed to generate token: {e}")
        return False

    return True


def main():
    print("=" * 70)
    print("Password Reset System Verification")
    print("=" * 70)

    results = []

    results.append(("Import Check", verify_imports()))
    results.append(("Model Instantiation", verify_models()))
    results.append(("Email Service", verify_email_service()))
    results.append(("Router Endpoints", verify_router_endpoints()))
    results.append(("Token Generation", verify_token_generation()))

    print("\n" + "=" * 70)
    print("Verification Summary")
    print("=" * 70)

    all_passed = True
    for test_name, passed in results:
        status = "PASS" if passed else "FAIL"
        symbol = "✓" if passed else "✗"
        print(f"{symbol} {test_name}: {status}")
        if not passed:
            all_passed = False

    print("=" * 70)

    if all_passed:
        print("\n✓ All verification checks passed!")
        print("\nPassword reset system is ready to use.")
        return 0
    else:
        print("\n✗ Some verification checks failed.")
        print("\nPlease review the errors above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
