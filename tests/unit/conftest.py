"""
Minimal conftest for unit tests.

Unit tests in this directory are fully self-contained and do NOT require
a running MongoDB instance or a live FastAPI application.  This conftest
intentionally avoids importing api.main so that missing infrastructure
(MongoDB, APScheduler, etc.) does not prevent the unit test suite from
running.

Run unit tests in isolation with:

    SECRET_KEY=test_secret_key_for_testing_only python -m pytest tests/unit/ --noconftest -v

The --noconftest flag is required on Python 3.9 because the parent
tests/conftest.py imports api.main, which uses the X|Y union syntax
(PEP 604) that is only valid on Python 3.10+. Because --noconftest also
skips this file, SECRET_KEY must be exported manually: several unit tests
import api.auth/api.utils.encryption modules that require it at import
time (see api/utils/secrets.py). The CI workflow sets it as a step env var.
"""
