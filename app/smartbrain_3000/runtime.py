"""Where am I running? (Docker-exit Phase 0.)

The app has always assumed a container: service-DNS gateway URL, host.docker.internal
for host-run model servers, /app/data for the database. Those stay EXACTLY as they are
inside containers — every current deployment keeps byte-identical behavior — but the
same build can now also run natively, where the right defaults are loopback URLs and a
per-OS user-data directory. Environment variables still override everything; these are
only the *defaults* behind them.

Detection is explicit-first: the image sets ``SMARTBRAIN_CONTAINER=1`` (Dockerfile), and
``/.dockerenv`` is the belt-and-suspenders for images built before that env existed.
Native is simply "not a container" — no sniffing beyond that, no surprises.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def in_container() -> bool:
    """True inside the Docker image (explicit env, with the /.dockerenv fallback)."""
    if os.environ.get("SMARTBRAIN_CONTAINER") == "1":
        return True
    return os.path.exists("/.dockerenv")


def default_data_dir() -> Path:
    """The native per-OS user-data directory (used only OUTSIDE containers).

    Mirrors where the launcher already keeps its own state, so the eventual native
    stack has one obvious home per platform. Nothing is created here — callers
    mkdir when they actually write.
    """
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "SmartBrain" / "data"
    if sys.platform.startswith("win") or os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "SmartBrain" / "data"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else home / ".local" / "share"
    return base / "smartbrain" / "data"


def version_from_file(path: Path | None = None) -> str | None:
    """The version stamped beside the package by a native assembly, or None.

    Containers stamp SMARTBRAIN_VERSION into the environment at build time; a native
    install has no baked env, so the assembler writes a VERSION file into the package
    directory instead. Absent/unreadable -> None (callers keep their env/dev fallbacks).
    ``path`` exists for tests; production callers use the package-directory default.
    """
    try:
        target = path if path is not None else Path(__file__).resolve().parent / "VERSION"
        text = target.read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None
