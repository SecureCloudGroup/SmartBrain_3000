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
# caught it) — the explicit opt-out keeps every TestClient hermetic. engine tests
# drive the state machine directly.
os.environ.setdefault("SMARTBRAIN_NO_VOICE_PREFETCH", "1")
# Tests must NEVER reach the hosted signaling node. An absent URL defaults to
# wss://rtc.securecloudgroup.com, and any test that pairs a device or enables remote
# access would then register a fresh routing id there — every CI run left 4 dead
# bindings on the production broker before this guard. An EMPTY url means "remote
# access off" (the loop exits without dialing). Tests that need the default or a
# local broker set the variable themselves.
os.environ.setdefault("SMARTBRAIN_SIGNALING_URL", "")
