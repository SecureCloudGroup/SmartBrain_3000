"""SmartBrain_3000 — a fully-local, single-user personal AI assistant.

Foundation package. Features are added one component at a time; this module
exposes only the version string for now.
"""

import os

# Stamped into the image at build time (Dockerfile ARG/ENV SMARTBRAIN_VERSION). Falls back to a
# dev marker for local runs and tests, so /api/health always reports a non-empty version.
__version__ = os.environ.get("SMARTBRAIN_VERSION") or "0.0.0-dev"
