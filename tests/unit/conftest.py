"""
Minimal conftest for unit tests.

Unit tests in this directory are fully self-contained and do NOT require
a running MongoDB instance or a live FastAPI application.  This conftest
intentionally avoids importing api.main so that missing infrastructure
(MongoDB, APScheduler, etc.) does not prevent the unit test suite from
running.
"""
