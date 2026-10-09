"""Browser component manifests (ni-format §35): the strict loader and the argv/env builder.

A manifest (``engines/<name>.json``, shipped inside this package) is the ONLY description
of an engine the app trusts: where its release lives, the exact bytes (the tarball and
every installed member, sha256 + size), the flags the app may pass, and the limits it runs
under. Loading is strict — an unknown key, a placeholder outside the closed set, a flag
outside the allowlist or a forbidden token is a ``ValueError`` — so a bad manifest can
never reach a launch.

The builder fills ONLY the closed placeholders (url, run_dir, proxy_port, timeout_s,
heap_mb, output_path, user_agent) into the manifest's templates. Every value is validated
first (https URL without whitespace, printable-ASCII UA, absolute paths, bounded ints) and
the rendered argv is re-walked token by token, so no caller-controlled string can become a
flag (the Crawl4AI flag-injection lesson). ``--eval``, ``--allow-private-network`` and the
``serve``/``mcp``/``scrape`` subcommands can never be emitted; ``--stealth`` only by an
engine whose manifest declares the ``stealth`` capability; ``--v8-flags`` only as
``--max-old-space-size=<heap_mb>``. The value checks and the builder itself live in
``cmdline``; this module owns the schema and the token grammar both of them use.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .. import netguard

ENGINES_DIR = Path(__file__).resolve().parent / "engines"  # tests point this at their own
PLATFORMS = ("darwin-arm64", "linux-x86_64", "linux-aarch64")
PLACEHOLDERS = frozenset({"url", "run_dir", "proxy_port", "timeout_s", "heap_mb",
                          "output_path", "user_agent"})
CAPABILITIES = frozenset({"render", "js", "assets", "screenshot", "stealth"})
IDENTITY_MODES = ("honest", "mimic", "rotate")
# Engines a caller may run in this release. The stealth tier ships its manifest (so it can
# be installed, hashed and shown) but is not selectable until a later step enables it.
SELECTABLE = frozenset({"obscura"})
# Never emitted for any engine, whatever its manifest says (golden-tested). ``-p``/``--port``
# open serve's CDP listener, ``--file`` makes fetch read a local file of URLs, ``-s`` is the
# screenshot flag (no screenshots in this contract), ``-e`` is the short form of --eval.
# ``--quiet``/``-q`` would hide the only sign of a heap-limit or watchdog kill (both exit 0
# and announce themselves on stderr only — measured, B0 spike §E).
ALWAYS_FORBIDDEN = frozenset({"--allow-private-network", "--host", "--port", "-p", "--eval",
                              "-e", "--file", "--screenshot", "-s", "--quiet", "-q", "serve",
                              "mcp", "scrape"})
STEALTH_FLAG = "--stealth"
# The flag grammar a template may use (an ALLOWLIST: anything else is refused at load).
_VALUE_FLAGS = frozenset({"--proxy", "--storage-dir", "--timeout", "--dump", "--output",
                          "--wait-until", "--v8-flags", "--user-agent"})
_BOOL_FLAGS = frozenset({STEALTH_FLAG})
# Global flags clap only accepts BEFORE the subcommand (measured: ``--v8-flags`` after
# ``fetch`` is "unexpected argument", and without it the heap is unbounded).
_GLOBAL_ONLY = frozenset({"--v8-flags"})
_WAIT_UNTIL = frozenset({"load", "domcontentloaded", "networkidle0"})
# Every fetch/assets template carries these: the egress route, the run-dir output and
# state, the engine's own deadline, the heap cap and the dump kind.
_REQUIRED_FLAGS = frozenset({"--proxy", "--output", "--timeout", "--storage-dir", "--v8-flags",
                             "--dump"})
_KEYS = frozenset({"name", "version", "released", "source_url_template", "platforms", "files",
                   "executable", "capabilities", "argv", "forbidden_flags", "limits",
                   "identity", "min_glibc"})
_LIMIT_RANGES = {"timeout_s": (1, 120), "script_deadline_ms": (1000, 120_000),
                 "heap_mb": (64, 4096), "rss_mb": (64, 8192), "rlimit_data_mb": (0, 65536)}
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_MANIFESTS = 16
_MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
_MAX_TEMPLATE_TOKENS = 48
_MAX_TOKEN = 200
MAX_POOL = 8  # Obscura has exactly 8 built-in profiles; any other index silently means 0
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
_ASSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.tar\.gz$")
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FORBIDDEN_RE = re.compile(r"^(?:-{1,2})?[a-z][a-z0-9-]{0,40}$")
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
BAD_CHARS_RE = re.compile(r"[\s\x00-\x1f\x7f]")
_GLIBC_RE = re.compile(r"^(\d{1,2})\.(\d{1,3})$")
_V8_TEMPLATE = "--max-old-space-size={heap_mb}"
_V8_RENDERED_RE = re.compile(r"^--max-old-space-size=\d{2,5}$")
_PROXY_TEMPLATE = "http://127.0.0.1:{proxy_port}"
_PROXY_RENDERED_RE = re.compile(r"^http://127\.0\.0\.1:\d{1,5}$")


@dataclass(frozen=True)
class Asset:
    """One platform's release tarball and the members the installer writes from it."""

    asset: str
    sha256: str
    size: int
    members: tuple[tuple[str, str, int], ...]  # (name, sha256, size) of each installed file


@dataclass(frozen=True)
class FileSpec:
    """An expected tarball member: its installed mode, and whether it is written at all."""

    name: str
    mode: int
    install: bool


@dataclass(frozen=True)
class Limits:
    timeout_s: int
    script_deadline_ms: int
    heap_mb: int
    rss_mb: int
    rlimit_data_mb: int  # 0 = off: V8 reserves ~450 GB of address space (measured)
    html_cap_bytes: int


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str
    released: _dt.date
    source_url_template: str
    platforms: dict[str, Asset]
    files: tuple[FileSpec, ...]
    executable: str
    capabilities: frozenset[str]
    argv: dict[str, tuple[str, ...]]
    forbidden: frozenset[str]
    limits: Limits
    identity_mode: str
    pool_size: int
    min_glibc: tuple[int, int]


def load(name: str) -> Manifest:
    """The shipped manifest for engine ``name`` (read and validated on every call)."""
    assert isinstance(name, str), "engine name must be a string"
    if not _NAME_RE.fullmatch(name):
        raise ValueError("manifest: bad engine name")
    path = ENGINES_DIR / f"{name}.json"
    try:
        raw = path.read_bytes()
    except OSError:
        raise ValueError("manifest: unknown engine") from None
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ValueError("manifest: file too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("manifest: not JSON") from None
    manifest = parse(data, expect_name=name)
    assert manifest.name == name, "manifest name must match its file"
    return manifest


def load_all() -> list[Manifest]:
    """Every shipped manifest, sorted by name (bounded)."""
    names = sorted(p.stem for p in ENGINES_DIR.glob("*.json"))
    assert isinstance(names, list), "listing must be a list"
    if len(names) > _MAX_MANIFESTS:
        raise ValueError("manifest: too many engines")
    manifests = [load(n) for n in names]
    assert [m.name for m in manifests] == names, "one manifest per file, in order"
    return manifests


def _closed(obj: object, keys: frozenset[str] | set[str], where: str) -> dict:
    """``obj`` must be a dict carrying exactly ``keys``."""
    assert keys, "key set required"
    assert where, "location required"
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise ValueError(f"manifest: {where} must have exactly {sorted(keys)}")
    return obj


def _int_in(value: object, low: int, high: int, where: str) -> int:
    assert low <= high, "range must be ordered"
    assert where, "location required"
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"manifest: {where} must be an integer in [{low}, {high}]")
    return value


def parse(data: object, *, expect_name: str | None = None) -> Manifest:
    """Validate a decoded manifest; any deviation is a ``ValueError``."""
    obj = _closed(data, _KEYS, "manifest")
    assert isinstance(obj, dict), "manifest must be a dict"
    name, version = obj["name"], obj["version"]
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError("manifest: bad name")
    if expect_name is not None and name != expect_name:
        raise ValueError("manifest: name does not match its file")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise ValueError("manifest: bad version")
    files = _parse_files(obj["files"])
    capabilities = _parse_capabilities(obj["capabilities"])
    forbidden = _parse_forbidden(obj["forbidden_flags"], capabilities)
    identity_mode, pool_size = _parse_identity(obj["identity"])
    manifest = Manifest(
        name=name, version=version, released=_parse_date(obj["released"]),
        source_url_template=_parse_source(obj["source_url_template"]),
        platforms=_parse_platforms(obj["platforms"], files), files=files,
        executable=_parse_executable(obj["executable"], files), capabilities=capabilities,
        argv=_parse_argv(obj["argv"], capabilities, forbidden), forbidden=forbidden,
        limits=_parse_limits(obj["limits"]), identity_mode=identity_mode,
        pool_size=pool_size, min_glibc=_parse_glibc(obj["min_glibc"]))
    for plat in manifest.platforms:  # every platform renders a sound download URL
        source_url(manifest, plat)
    assert manifest.executable in {f.name for f in manifest.files}, "executable is a file"
    return manifest


def _parse_date(value: object) -> _dt.date:
    assert value is not None, "released required"
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("manifest: released must be YYYY-MM-DD")
    try:
        parsed = _dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError("manifest: released must be YYYY-MM-DD") from None
    assert isinstance(parsed, _dt.date), "released must parse to a date"
    return parsed


def _parse_source(value: object) -> str:
    assert value is not None, "source_url_template required"
    if not isinstance(value, str) or len(value) > 300 or BAD_CHARS_RE.search(value):
        raise ValueError("manifest: bad source_url_template")
    if not value.startswith("https://github.com/") or "{asset}" not in value:
        raise ValueError("manifest: source_url_template must be a github.com release URL")
    if set(PLACEHOLDER_RE.findall(value)) - {"version", "asset"} or ".." in value:
        raise ValueError("manifest: source_url_template placeholders are {version} {asset}")
    assert "{asset}" in value, "the asset is part of the URL"
    return value


def _parse_files(value: object) -> tuple[FileSpec, ...]:
    """Expected tarball members: ``[{name, mode, install}]`` (1-8, unique, top level)."""
    assert value is not None, "files required"
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError("manifest: files must list 1-8 members")
    out: list[FileSpec] = []
    for item in value:  # bounded: ≤ 8
        entry = _closed(item, {"name", "mode", "install"}, "files[]")
        name, mode, install = entry["name"], entry["mode"], entry["install"]
        if not isinstance(name, str) or not _FILE_RE.fullmatch(name) or name == "INSTALLED.json":
            raise ValueError("manifest: bad file name")
        if not isinstance(mode, str) or not re.fullmatch(r"0[0-7]{3}", mode):
            raise ValueError("manifest: file mode must be an octal string like 0755")
        bits = int(mode, 8)
        if bits & 0o7022 or not bits & 0o400:  # no setuid/sticky, no group/other write
            raise ValueError("manifest: file mode not allowed")
        if type(install) is not bool:
            raise ValueError("manifest: install must be a boolean")
        out.append(FileSpec(name=name, mode=bits, install=install))
    if len({f.name for f in out}) != len(out):
        raise ValueError("manifest: duplicate file")
    assert out, "files must not be empty"
    return tuple(out)


def _parse_executable(value: object, files: tuple[FileSpec, ...]) -> str:
    assert files, "files required"
    spec = next((f for f in files if f.name == value), None)
    if spec is None or not spec.install or not spec.mode & 0o100:
        raise ValueError("manifest: executable must be an installed file with an exec bit")
    assert isinstance(value, str), "executable must be a string"
    return value


def _parse_platforms(value: object, files: tuple[FileSpec, ...]) -> dict[str, Asset]:
    """Per-platform assets; each pins the tarball AND every installed member."""
    if not isinstance(value, dict) or not value or set(value) - set(PLATFORMS):
        raise ValueError(f"manifest: platforms must be a subset of {PLATFORMS}")
    installed = {f.name for f in files if f.install}
    assert installed, "at least one installed file required"
    out: dict[str, Asset] = {}
    for plat, item in value.items():  # bounded: ≤ 3 platforms
        entry = _closed(item, {"asset", "sha256", "size", "members"}, f"platforms.{plat}")
        if not isinstance(entry["asset"], str) or not _ASSET_RE.fullmatch(entry["asset"]):
            raise ValueError("manifest: bad asset name")
        if not isinstance(entry["sha256"], str) or not _SHA256_RE.fullmatch(entry["sha256"]):
            raise ValueError("manifest: bad sha256")
        size = _int_in(entry["size"], 1, _MAX_DOWNLOAD_BYTES, f"platforms.{plat}.size")
        members = entry["members"]
        if not isinstance(members, dict) or set(members) != installed:
            raise ValueError("manifest: members must pin exactly the installed files")
        pins = []
        for mname in sorted(members):  # bounded: ≤ 8 files
            pin = _closed(members[mname], {"sha256", "size"}, f"members.{mname}")
            if not isinstance(pin["sha256"], str) or not _SHA256_RE.fullmatch(pin["sha256"]):
                raise ValueError("manifest: bad member sha256")
            pins.append((mname, pin["sha256"],
                         _int_in(pin["size"], 1, _MAX_DOWNLOAD_BYTES * 4, "member size")))
        out[plat] = Asset(asset=entry["asset"], sha256=entry["sha256"], size=size,
                          members=tuple(pins))
    assert out and set(out) <= set(PLATFORMS), "at least one known platform"
    return out


def _parse_capabilities(value: object) -> frozenset[str]:
    assert value is not None, "capabilities required"
    if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
        raise ValueError("manifest: capabilities must be a list of strings")
    caps = frozenset(value)
    if len(caps) != len(value) or caps - CAPABILITIES or "render" not in caps:
        raise ValueError(f"manifest: capabilities must be unique, from {sorted(CAPABILITIES)}, "
                         "and include render")
    assert "render" in caps, "every engine renders"
    return caps


def _parse_forbidden(value: object, caps: frozenset[str]) -> frozenset[str]:
    """The manifest's own forbidden list, unioned with the floor no manifest can lower."""
    assert isinstance(caps, frozenset), "capabilities required"
    if not isinstance(value, list) or not value or len(value) > 64:
        raise ValueError("manifest: forbidden_flags must list 1-64 tokens")
    if not all(isinstance(t, str) and _FORBIDDEN_RE.fullmatch(t) for t in value):
        raise ValueError("manifest: bad forbidden flag")
    listed = frozenset(value)
    if "stealth" in caps and STEALTH_FLAG in listed:
        raise ValueError("manifest: a stealth engine cannot forbid its own --stealth")
    extra = frozenset() if "stealth" in caps else frozenset({STEALTH_FLAG})
    result = listed | ALWAYS_FORBIDDEN | extra
    assert ALWAYS_FORBIDDEN <= result, "no manifest lowers the floor"
    return result


def _parse_identity(value: object) -> tuple[str, int]:
    entry = _closed(value, {"mode", "pool_size"}, "identity")
    assert isinstance(entry, dict), "identity must be a dict"
    mode = entry["mode"]
    if mode not in IDENTITY_MODES:
        raise ValueError(f"manifest: identity.mode must be one of {IDENTITY_MODES}")
    pool = _int_in(entry["pool_size"], 0, MAX_POOL, "identity.pool_size")
    if mode != "honest" and pool < 1:
        raise ValueError("manifest: mimic/rotate need a profile pool")
    assert 0 <= pool <= MAX_POOL, "pool is bounded"
    return mode, pool


def _parse_limits(value: object) -> Limits:
    entry = _closed(value, set(_LIMIT_RANGES) | {"html_cap_bytes"}, "limits")
    assert isinstance(entry, dict), "limits must be a dict"
    got = {k: _int_in(entry[k], lo, hi, f"limits.{k}") for k, (lo, hi) in _LIMIT_RANGES.items()}
    # The rendered page is handed to the same extractor the static tier feeds: it may never
    # exceed what the static fetch itself accepts.
    cap = _int_in(entry["html_cap_bytes"], 1024, netguard._MAX_BYTES, "limits.html_cap_bytes")
    if got["script_deadline_ms"] > got["timeout_s"] * 1000:
        raise ValueError("manifest: script deadline exceeds the render timeout")
    if 0 < got["rlimit_data_mb"] < 256:
        raise ValueError("manifest: rlimit_data_mb is 0 (off) or at least 256")
    assert cap <= netguard._MAX_BYTES, "never more than the static tier accepts"
    return Limits(html_cap_bytes=cap, **got)


def _parse_glibc(value: object) -> tuple[int, int]:
    assert value is not None, "min_glibc required"
    match = _GLIBC_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("manifest: min_glibc must look like 2.35")
    assert len(match.groups()) == 2, "major and minor"
    return int(match.group(1)), int(match.group(2))


def _parse_argv(value: object, caps: frozenset[str], forbidden: frozenset[str]) -> dict[str, tuple[str, ...]]:
    """Templates: ``fetch`` (HTML dump), ``assets`` (iff the capability), ``honest`` (UA)."""
    sections = {"fetch", "honest"} | ({"assets"} if "assets" in caps else set())
    entry = _closed(value, sections, "argv")
    assert isinstance(entry, dict), "argv must be a dict"
    out: dict[str, tuple[str, ...]] = {}
    for section in sorted(sections):  # bounded: ≤ 3 sections
        tokens = entry[section]
        if not isinstance(tokens, list) or not 1 <= len(tokens) <= _MAX_TEMPLATE_TOKENS:
            raise ValueError(f"manifest: argv.{section} must list 1-{_MAX_TEMPLATE_TOKENS} tokens")
        for tok in tokens:  # bounded by the token cap
            if (not isinstance(tok, str) or not tok or len(tok) > _MAX_TOKEN
                    or BAD_CHARS_RE.search(tok)):
                raise ValueError(f"manifest: argv.{section} has a bad token")
            if set(PLACEHOLDER_RE.findall(tok)) - PLACEHOLDERS:
                raise ValueError(f"manifest: argv.{section} uses an unknown placeholder")
            if "{" in PLACEHOLDER_RE.sub("", tok) or "}" in PLACEHOLDER_RE.sub("", tok):
                raise ValueError(f"manifest: argv.{section} has a stray brace")
        out[section] = tuple(tokens)
    if out["honest"] != ("--user-agent", "{user_agent}"):
        raise ValueError("manifest: argv.honest must be exactly --user-agent {user_agent}")
    for section, dump in (("fetch", "html"), ("assets", "assets")):
        if section in out:
            flags = walk_argv(out[section], forbidden, rendered=False)
            if flags.get("--dump") != dump or "{user_agent}" in " ".join(out[section]):
                raise ValueError(f"manifest: argv.{section} must dump {dump} and carry no UA")
            if _REQUIRED_FLAGS - set(flags):
                raise ValueError(f"manifest: argv.{section} lacks {sorted(_REQUIRED_FLAGS - set(flags))}")
            if (STEALTH_FLAG in flags) != ("stealth" in caps):
                raise ValueError("manifest: --stealth must appear exactly when declared")
    assert set(out) == sections, "every declared section validated"
    return out


def walk_argv(tokens: tuple[str, ...] | list[str], forbidden: frozenset[str], *,
          rendered: bool) -> dict[str, str]:
    """The flag grammar shared by template validation and the rendered-argv re-check.

    Returns ``{flag: value}`` plus ``{"fetch": url}``. Refuses: any forbidden token (also as
    ``flag=value`` or inside a value), a flag outside the allowlist, a repeated flag, a value
    that looks like a flag (except the pinned ``--v8-flags`` heap form), anything but exactly
    one ``fetch <url>``.
    """
    assert isinstance(forbidden, frozenset) and forbidden, "forbidden set required"
    assert len(tokens) <= _MAX_TEMPLATE_TOKENS * 2, "token count bounded"
    flags: dict[str, str] = {}
    pending = ""
    for tok in tokens:  # bounded by the assert above
        if tok in forbidden or tok.split("=", 1)[0] in forbidden:
            raise ValueError("argv: forbidden token")
        if pending:
            _check_value(pending, tok, rendered)
            flags[pending], pending = tok, ""
        elif tok in _VALUE_FLAGS or tok in _BOOL_FLAGS:
            if tok in flags:
                raise ValueError("argv: repeated flag")
            if tok in _GLOBAL_ONLY and "fetch" in flags:
                raise ValueError("argv: a global flag must precede fetch")
            flags[tok] = ""
            pending = tok if tok in _VALUE_FLAGS else ""
        elif tok.startswith("-"):
            raise ValueError("argv: flag outside the allowlist")
        elif tok == "fetch" and "fetch" not in flags:
            flags["fetch"] = ""
            pending = "fetch"
        else:
            raise ValueError("argv: unexpected positional")
    if pending or "fetch" not in flags or "--proxy" not in flags:
        raise ValueError("argv: needs fetch <url> and --proxy")
    return flags


def _check_value(flag: str, value: str, rendered: bool) -> None:
    """One flag's value: template placeholders before rendering, the real shape after."""
    assert flag, "flag required"
    assert isinstance(value, str), "value must be a string"
    if flag == "--v8-flags":
        ok = _V8_RENDERED_RE.fullmatch(value) if rendered else value == _V8_TEMPLATE
    elif value.startswith("-"):
        ok = False
    elif flag == "fetch":
        ok = value.startswith("https://") if rendered else value == "{url}"
    elif flag == "--proxy":
        ok = _PROXY_RENDERED_RE.fullmatch(value) if rendered else value == _PROXY_TEMPLATE
    elif flag == "--dump":
        ok = value in ("html", "assets")
    elif flag == "--wait-until":
        ok = value in _WAIT_UNTIL
    elif flag == "--timeout":
        ok = value.isdigit() if rendered else value == "{timeout_s}"
    elif flag == "--output":
        ok = os.path.isabs(value) if rendered else value == "{output_path}"
    elif flag == "--storage-dir":
        ok = os.path.isabs(value) if rendered else value.startswith("{run_dir}/")
    else:  # --user-agent (only reachable through the honest section)
        ok = bool(value) if rendered else value == "{user_agent}"
    if not ok:
        raise ValueError(f"argv: bad value for {flag}")


def source_url(m: Manifest, platform: str) -> str:
    """The exact pinned download URL for ``platform``."""
    assert platform in m.platforms, "platform must be pinned"
    asset = m.platforms[platform].asset
    url = m.source_url_template.replace("{version}", m.version).replace("{asset}", asset)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "github.com" or BAD_CHARS_RE.search(url):
        raise ValueError("manifest: download URL must be https://github.com/...")
    assert asset in url, "the pinned asset is in the URL"
    return url
