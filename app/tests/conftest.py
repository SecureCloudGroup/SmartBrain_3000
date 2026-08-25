"""Shared test configuration.

TrustedHostMiddleware (main.py) validates the Host header against an allow-list
(loopback only in production, to block DNS rebinding). Starlette's TestClient
sends ``Host: testserver``, so the suite must permit it. Setting the env before
any app is created keeps every create_app() in the suite consistent.
"""

import os

os.environ.setdefault("SMARTBRAIN_ALLOWED_HOSTS", "testserver,localhost,127.0.0.1")

# The app's lifespan arms the voice-model background download at boot. In tests that
# means a REAL 236 MB fetch racing monkeypatched module state (the adversarial review
# caught it) — the explicit opt-out keeps every TestClient hermetic. moonshine tests
# drive the state machine directly.
os.environ.setdefault("SMARTBRAIN_NO_VOICE_PREFETCH", "1")
