"""Shared test configuration.

TrustedHostMiddleware (main.py) validates the Host header against an allow-list
(loopback only in production, to block DNS rebinding). Starlette's TestClient
sends ``Host: testserver``, so the suite must permit it. Setting the env before
any app is created keeps every create_app() in the suite consistent.
"""

import os

os.environ.setdefault("SMARTBRAIN_ALLOWED_HOSTS", "testserver,localhost,127.0.0.1")
