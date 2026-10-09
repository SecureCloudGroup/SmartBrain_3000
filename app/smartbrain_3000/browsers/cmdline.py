"""The engine command line for browser components (ni-format §35): every value checked,
then filled into the manifest's templates, then re-walked token by token.

- ``validate_url``: https only, no whitespace/control chars, no userinfo, a hostname normalised
  to lowercase IDNA ASCII and never an IP literal (the engine reads local files when handed
  ``file://`` — measured). Returns the normalised URL; callers use that one everywhere.
- ``validate_user_agent`` / ``validate_timezone`` (zoneinfo must load it: a bogus zone is
  silently GMT inside the engine, and its default is Europe/Berlin — measured).
- ``render_argv``: single-pass placeholder fill (values are never re-scanned), then
  ``check_argv`` re-walks the result with the manifest's grammar — no forbidden token can
  appear anywhere, not even as a value.
- ``render_env``: the engine's whole environment, a closed key set; no proxy variable ever.
- ``card_profile``: the stable per-card identity profile (sha256(card id) mod pool).
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import zoneinfo
from urllib.parse import urlsplit

from .manifest import (
    BAD_CHARS_RE,
    MAX_POOL,
    PLACEHOLDER_RE,
    STEALTH_FLAG,
    Manifest,
    walk_argv,
)

ENV_KEYS = frozenset({"PATH", "LANG", "LC_ALL", "HOME", "TMPDIR", "XDG_CACHE_HOME",
                      "XDG_CONFIG_HOME", "XDG_DATA_HOME", "OBSCURA_TIMEZONE",
                      "OBSCURA_SCRIPT_DEADLINE_MS", "OBSCURA_PROFILE"})
FORBIDDEN_ENV = frozenset({"OBSCURA_ALLOW_PRIVATE_NETWORK"})
_MINIMAL_PATH = "/usr/local/bin:/usr/bin:/bin"
_MAX_UA = 200
_MAX_URL = 2048
_TZ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]{0,31}(?:/[A-Za-z0-9_+-]{1,32}){0,2}$")


def validate_url(url: object) -> tuple[str, str, int]:
    """A URL the engine may be given: https only, no whitespace/control chars, no userinfo,
    a hostname (never an IP literal) normalised to lowercase IDNA ASCII. Returns
    ``(normalised_url, host, port)``; refusals are ``ValueError(code)``. ``file://`` and
    every other scheme are refused — the engine reads local files when handed one."""
    if not isinstance(url, str) or not 9 <= len(url) <= _MAX_URL:
        raise ValueError("bad_url")
    if BAD_CHARS_RE.search(url) or url[:8].lower() != "https://":
        raise ValueError("bad_url")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password or "@" in parts.netloc:
        raise ValueError("bad_url")
    try:
        port = parts.port
        host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError):
        raise ValueError("bad_url") from None
    if not host or is_ip_literal(host):
        raise ValueError("ip_literal" if host else "bad_url")
    if not re.fullmatch(r"[a-z0-9.-]{1,253}", host) or host.rsplit(".", 1)[-1].isdigit():
        raise ValueError("bad_url")
    netloc = host if port in (None, 443) else f"{host}:{port}"
    normal = parts._replace(scheme="https", netloc=netloc).geturl()
    assert normal.startswith("https://"), "normalised URLs stay https"
    assert host == host.lower() and host.isascii(), "hosts are lowercase IDNA ASCII"
    return normal, host, port or 443


def card_profile(card_id: str, pool_size: int) -> int:
    """The stable per-card identity profile: sha256(card id) mod pool size."""
    assert isinstance(card_id, str) and card_id, "card id required"
    assert 1 <= pool_size <= MAX_POOL, "a profile pool is required"
    digest = hashlib.sha256(card_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % pool_size


def is_ip_literal(host: str) -> bool:
    """True for any address literal ``ipaddress`` accepts (v4, v6, scoped v6)."""
    assert isinstance(host, str), "host must be a string"
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    assert host, "an address literal is never empty"
    return True


def validate_user_agent(ua: object) -> str:
    """Printable ASCII, ≤ 200 chars, never starting with a dash."""
    if not isinstance(ua, str) or not 1 <= len(ua) <= _MAX_UA:
        raise ValueError("bad_user_agent")
    if ua.startswith("-") or any(not 0x20 <= ord(c) <= 0x7E for c in ua):
        raise ValueError("bad_user_agent")
    assert ua.isascii(), "user agent must be ASCII"
    assert len(ua) <= _MAX_UA, "user agent is capped"
    return ua


def validate_timezone(tz: object) -> str:
    """An IANA zone name the runtime can load (``UTC`` included)."""
    if not isinstance(tz, str) or not _TZ_RE.fullmatch(tz):
        raise ValueError("bad_timezone")
    try:
        zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError("bad_timezone") from None
    assert tz, "timezone must be non-empty"
    assert _TZ_RE.fullmatch(tz), "timezone shape was checked"
    return tz


def _safe_path(path: object) -> str:
    if (not isinstance(path, str) or not os.path.isabs(path) or len(path) > 1024
            or BAD_CHARS_RE.search(path.replace(" ", "_"))):
        raise ValueError("bad_path")
    assert not path.startswith("-"), "absolute paths never start with a dash"
    assert "\x00" not in path, "no NUL in a path"
    return path


def render_argv(m: Manifest, *, engine_path: str, url: str, run_dir: str, proxy_port: int,
                timeout_s: int, output_path: str, user_agent: str | None,
                want_assets: bool) -> list[str]:
    """The full argv for one engine run (executable first). ``user_agent`` None = the
    engine's own identity (mimic/rotate); otherwise the honest section is appended."""
    assert isinstance(m, Manifest), "manifest required"
    assert isinstance(want_assets, bool), "want_assets is a flag"
    if validate_url(url)[0] != url:
        raise ValueError("bad_url")  # callers pass the normalised URL
    if not isinstance(proxy_port, int) or not 1 <= proxy_port <= 65535:
        raise ValueError("bad_port")
    if not isinstance(timeout_s, int) or not 1 <= timeout_s <= m.limits.timeout_s:
        raise ValueError("bad_timeout")
    values = {"url": url, "run_dir": _safe_path(run_dir), "proxy_port": str(proxy_port),
              "timeout_s": str(timeout_s), "heap_mb": str(m.limits.heap_mb),
              "output_path": _safe_path(output_path), "user_agent": ""}
    tokens = list(m.argv["assets" if want_assets else "fetch"])
    if user_agent is not None:
        values["user_agent"] = validate_user_agent(user_agent)
        tokens += list(m.argv["honest"])
    argv = [_safe_path(engine_path)]
    argv += [PLACEHOLDER_RE.sub(lambda mt: values[mt.group(1)], tok) for tok in tokens]
    check_argv(m, argv, url)
    assert argv[0] == engine_path, "the executable leads"
    return argv


def check_argv(m: Manifest, argv: list[str], url: str) -> None:
    """Re-walk a rendered argv: no forbidden token anywhere, allowlisted flags only, the
    single positional is exactly ``url``. Raises ``ValueError``."""
    assert isinstance(argv, list) and len(argv) >= 2, "argv must hold the executable + args"
    assert isinstance(url, str), "url required"
    if any(not isinstance(t, str) or "\x00" in t for t in argv):
        raise ValueError("argv: bad token")
    flags = walk_argv(argv[1:], m.forbidden, rendered=True)
    if flags["fetch"] != url:
        raise ValueError("argv: the positional must be the validated URL")
    if STEALTH_FLAG in flags and "stealth" not in m.capabilities:
        raise ValueError("argv: --stealth on a non-stealth engine")


def render_env(m: Manifest, *, run_dir: str, timezone: str, profile: int | None) -> dict[str, str]:
    """The engine's whole environment: minimal PATH, every home/temp/XDG dir inside the run
    dir, the pinned zone and script deadline, and the identity profile (mimic/rotate)."""
    assert isinstance(m, Manifest), "manifest required"
    assert m.pool_size <= MAX_POOL, "profile pool is bounded"
    home = os.path.join(_safe_path(run_dir), "home")
    env = {"PATH": _MINIMAL_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": home,
           "TMPDIR": os.path.join(run_dir, "tmp"),
           "XDG_CACHE_HOME": os.path.join(home, ".cache"),
           "XDG_CONFIG_HOME": os.path.join(home, ".config"),
           "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
           "OBSCURA_TIMEZONE": validate_timezone(timezone),
           "OBSCURA_SCRIPT_DEADLINE_MS": str(m.limits.script_deadline_ms)}
    if profile is not None:
        if not isinstance(profile, int) or not 0 <= profile < m.pool_size:
            raise ValueError("bad_profile")
        env["OBSCURA_PROFILE"] = str(profile)
    check_env(env)
    return env


def check_env(env: dict[str, str]) -> None:
    """Closed key set, no forbidden key, no proxy variable, no control characters."""
    assert isinstance(env, dict), "env must be a dict"
    assert "PATH" in env, "env must carry PATH"
    if set(env) - ENV_KEYS or set(env) & FORBIDDEN_ENV:
        raise ValueError("env: key outside the closed set")
    if any("proxy" in k.lower() for k in env):
        raise ValueError("env: proxy variables are never passed")
    if any(not isinstance(v, str) or re.search(r"[\x00-\x1f\x7f]", v) for v in env.values()):
        raise ValueError("env: bad value")
