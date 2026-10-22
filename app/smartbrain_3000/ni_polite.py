"""S5a politeness for every Neural Interface fetch (plan D9).

One shared object sits behind ``netguard``'s guarded request while an NI fetch runs
(``netguard.set_politeness``); every other caller of netguard is untouched. It keeps:

* a per-host token bucket (``HOST_BURST`` tokens, one back every ``HOST_REFILL_S``). A host over
  budget DEFERS the card: ``FetchError(kind="host_busy")``, which the engine maps to the
  ``host_busy`` capacity status (``ni.CAPACITY_KINDS``): the card stays due, is never marked failed.
* a per-host pause from 429 / 503 answers, honouring ``Retry-After`` (seconds or HTTP date), capped
  at ``RETRY_AFTER_CAP_S``.
* fetch dedupe on the tick: cards on one URL reuse one response for ``DEDUPE_TTL_S`` (third-party
  hosts: the cadence floor) so 50 cards on one URL make one request per interval.
* the 15 minute cadence floor for third-party hosts (``cadence_floor_minutes``).
* the NI port policy (80 / 443, or the one port a Library row declared in ``source.port``).
* robots.txt for page reads (``safe_fetch_page``), and for EVERY read of a source that is not a Library row
  (``engaged(robots=True)``: research on the open web, and a kept card ``robots_for`` names), whichever
  fetcher runs; fetched through netguard and cached.

"Own" hosts are exempt from the cadence floor and use a short dedupe window: loopback / private /
link-local address literals, single-label names, ``localhost``, ``.local`` ``.lan`` ``.home.arpa``
``.internal`` ``.localdomain`` names, and anything the user lists in ``SB_NI_OWN_HOSTS`` (comma
separated hosts; a listed host covers its subdomains).
"""

from __future__ import annotations

import contextlib
import functools
import ipaddress
import os
import threading
import time
from collections import OrderedDict
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib import robotparser
from urllib.parse import urlparse

from . import netguard

CADENCE_FLOOR_MINUTES = 15     # D9: third-party API / page cadence floor
HOST_BURST = 5                 # requests a host may take at once
HOST_REFILL_S = 30.0           # one more request allowed per this many seconds
RETRY_AFTER_CAP_S = 3600.0     # longest pause one answer can impose on a host
RETRY_AFTER_MIN_S = 1.0
RETRY_DEFAULT_S = 300.0        # 429 / 503 with no usable Retry-After
OWN_DEDUPE_TTL_S = 60.0        # response reuse window for an own host
ROBOTS_TTL_S = 24 * 3600.0
ROBOTS_FAIL_TTL_S = 600.0
ROBOTS_MAX_BYTES = 512_000
ALLOWED_PORTS = (80, 443)
_MAX_HOSTS = 512
_MAX_CACHE = 16
_MAX_CACHED_BYTES = 1_000_000
_MAX_ROBOTS = 256
_OWN_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".localdomain", ".localhost")
_FLOORED_SOURCES = ("http_json", "http_page", "http_image")
_clock = time.monotonic        # module attributes so a test can move time
_wall = time.time


def is_own_host(host: str) -> bool:
    """True for a local or user-owned host (see the module docstring); False for a third party."""
    assert isinstance(host, str), "host must be a string"
    h = host.strip().lower().rstrip(".").strip("[]")
    if not h:
        return False
    try:
        return not ipaddress.ip_address(h).is_global
    except ValueError:
        pass
    if "." not in h or h.endswith(_OWN_SUFFIXES):
        return True
    mine = [x.strip().lower().rstrip(".") for x in os.environ.get("SB_NI_OWN_HOSTS", "").split(",") if x.strip()]
    return any(h == x or h.endswith("." + x) for x in mine)


def cadence_floor_minutes(spec: object) -> int:
    """The floor (minutes) a spec's interval is clamped to at run time: 15 for an http source on a
    third-party host, else 0. A parameterised host counts as third-party."""
    if not isinstance(spec, dict) or not isinstance(spec.get("source"), dict):
        return 0
    source = spec["source"]
    if source.get("type") not in _FLOORED_SOURCES:
        return 0
    parsed = urlparse(str(source.get("url") or ""))
    if "{" in parsed.netloc:
        return CADENCE_FLOOR_MINUTES
    host = parsed.hostname or ""
    return 0 if (not host or is_own_host(host)) else CADENCE_FLOOR_MINUTES


def parse_retry_after(value: object, now: float | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` value (delta-seconds or HTTP date), clamped to
    ``[RETRY_AFTER_MIN_S, RETRY_AFTER_CAP_S]``; None when it is absent or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.isascii() and text.isdigit() and len(text) <= 9:
        seconds = float(int(text))
    else:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = when.timestamp() - (_wall() if now is None else now)
    return min(RETRY_AFTER_CAP_S, max(RETRY_AFTER_MIN_S, seconds))


class _Host:
    __slots__ = ("paused_until", "stamp", "tokens")

    def __init__(self, now: float) -> None:
        self.tokens = float(HOST_BURST)
        self.stamp = now
        self.paused_until = 0.0


class Politeness:
    """The shared state: host buckets and pauses, the response cache, the robots cache."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hosts: dict[str, _Host] = {}
        self._cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
        self._robots: OrderedDict[str, tuple[float, robotparser.RobotFileParser | None]] = OrderedDict()
        self._local = threading.local()

    # --- budget -------------------------------------------------------------------------
    def admit(self, host: str) -> None:
        """Take one request from ``host``'s bucket, or raise ``FetchError(kind="host_busy")``."""
        assert host, "host required"
        now = _clock()
        with self._lock:
            state = self._hosts.get(host)
            if state is None:
                if len(self._hosts) >= _MAX_HOSTS:
                    self._prune(now)
                state = self._hosts[host] = _Host(now)
            state.tokens = min(float(HOST_BURST), state.tokens + (now - state.stamp) / HOST_REFILL_S)
            state.stamp = now
            if state.paused_until > now or state.tokens < 1.0:
                raise netguard.FetchError("host is over its request budget", kind="host_busy")
            state.tokens -= 1.0

    def pause(self, host: str, seconds: float) -> None:
        """Pause ``host`` for ``seconds`` (already clamped by the caller)."""
        assert host and seconds > 0, "host and a positive pause required"
        with self._lock:
            state = self._hosts.setdefault(host, _Host(_clock()))
            state.paused_until = max(state.paused_until, _clock() + seconds)

    def _prune(self, now: float) -> None:
        idle = [h for h, s in self._hosts.items() if s.paused_until <= now and s.tokens >= HOST_BURST - 0.5]
        for h in idle or list(self._hosts)[: _MAX_HOSTS // 4]:
            self._hosts.pop(h, None)

    # --- the netguard hook --------------------------------------------------------------
    def call(self, url: str, method: str, has_headers: bool, shape: tuple, go) -> dict:
        """Run ``go()`` (one guarded request) under the budget, the pause and the dedupe cache."""
        assert callable(go) and url, "a request and its url are required"
        host = (urlparse(url).hostname or "").lower()
        if not host:
            raise netguard.FetchError("no host in URL")
        if getattr(self._local, "robots", False) and not is_own_host(host) and not self.robots_ok(url):
            raise netguard.FetchError("robots.txt asks automated readers to stay out", kind="robots")
        key = (url, method, shape)
        reuse = method == "GET" and not has_headers and getattr(self._local, "dedupe", False)
        if reuse:
            hit = self._cache_get(key)
            if hit is not None:
                return hit
        self.admit(host)
        try:
            got = go()
        except netguard.FetchError as exc:
            if exc.status in (429, 503):
                wait = parse_retry_after(exc.retry_after)
                self.pause(host, wait if wait is not None else RETRY_DEFAULT_S)
                if exc.status == 429 or wait is not None:  # asked to slow down: defer, not fail
                    raise netguard.FetchError("host asked us to wait", status=exc.status,
                                              kind="host_busy") from None
            raise
        if reuse:
            self._cache_put(key, host, got)
        return got

    def _cache_get(self, key: tuple) -> dict | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None or entry[0] <= _clock():
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return dict(entry[1])

    def _cache_put(self, key: tuple, host: str, got: dict) -> None:
        if len(got.get("content") or b"") > _MAX_CACHED_BYTES:
            return
        ttl = OWN_DEDUPE_TTL_S if is_own_host(host) else CADENCE_FLOOR_MINUTES * 60.0
        with self._lock:
            self._cache[key] = (_clock() + ttl, dict(got))
            while len(self._cache) > _MAX_CACHE:
                self._cache.popitem(last=False)

    def port_ok(self, port: int | None, host: str = "") -> bool:
        """80 / 443 (or no explicit port), the one port the sealed source declared, or any port on a host
        of the user's own (the same own-host rule as the cadence floor and the dedupe window)."""
        assert port is None or isinstance(port, int), "port is an int or absent"
        assert isinstance(host, str), "host must be a string"
        if port is None or port in ALLOWED_PORTS or port == getattr(self._local, "port", None):
            return True
        return bool(host) and is_own_host(host)

    def robots_ok(self, url: str) -> bool:
        """May an automated reader fetch ``url`` per its host's robots.txt (cached)?"""
        parsed = urlparse(url)
        if not parsed.hostname or parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
            return True  # netguard refuses this URL itself, with the real reason
        try:
            if not self.port_ok(parsed.port, parsed.hostname):
                return True  # likewise: netguard reports the refused port
        except ValueError:
            return True  # a malformed port: netguard's "invalid port in URL"
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            entry = self._robots.get(origin)
        if entry is None or entry[0] <= _clock():
            entry = self._load_robots(origin)
        rules = entry[1]
        return rules is None or rules.can_fetch(netguard.USER_AGENT, url)

    def _load_robots(self, origin: str) -> tuple[float, robotparser.RobotFileParser | None]:
        """Fetch ``/robots.txt`` through netguard. A 4xx or a non-text answer means no rules; any
        other failure keeps readers out for ``ROBOTS_FAIL_TTL_S`` (RFC 9309: unavailable = disallow)."""
        deny = robotparser.RobotFileParser()
        deny.parse(["User-agent: *", "Disallow: /"])
        try:
            got = netguard._guarded_get_once(origin + "/robots.txt", ("text/",), ROBOTS_MAX_BYTES)
            rules = robotparser.RobotFileParser()
            rules.parse(netguard.decode_body(got["content"], got["content_type"]).splitlines())
            entry = (_clock() + ROBOTS_TTL_S, rules)
        except netguard.FetchError as exc:
            free = (exc.status is not None and 400 <= exc.status < 500 and exc.status != 429) \
                or exc.kind == "not_json"
            entry = (_clock() + (ROBOTS_TTL_S if free else ROBOTS_FAIL_TTL_S), None if free else deny)
        with self._lock:
            self._robots[origin] = entry
            while len(self._robots) > _MAX_ROBOTS:
                self._robots.popitem(last=False)
        return entry

    def reset(self) -> None:
        """Forget every bucket, pause, cached response and robots answer (tests, a lock)."""
        with self._lock:
            self._hosts.clear()
            self._cache.clear()
            self._robots.clear()


SHARED = Politeness()


@contextlib.contextmanager
def engaged(port: int | None = None, robots: bool | None = None):
    """Run the body with the shared politeness installed on this thread (re-entrant). ``port`` is
    the one extra port a Library row declared for this source. ``robots``: every request in the body
    obeys robots.txt (a source that is not a Library row); None keeps the enclosing answer."""
    previous = netguard.set_politeness(SHARED)
    saved_port = getattr(SHARED._local, "port", None)
    saved_robots = getattr(SHARED._local, "robots", False)
    SHARED._local.port = port if isinstance(port, int) and 0 < port < 65536 else saved_port
    SHARED._local.robots = bool(robots) if robots is not None else saved_robots
    try:
        yield SHARED
    finally:
        SHARED._local.port = saved_port
        SHARED._local.robots = saved_robots
        netguard.set_politeness(previous)


def robots_for(spec: object) -> bool:
    """True when a card's source came from outside the Library (a researcher recipe naming any source kind
    but ``library``): its refreshes obey robots.txt like the research that found it."""
    recipe = spec.get("recipe") if isinstance(spec, dict) else None
    sources = recipe.get("sources") if isinstance(recipe, dict) else None
    if not isinstance(sources, list):
        return False
    return any(isinstance(s, dict) and s.get("kind") != "library" for s in sources[:8])


def polite(fn):
    """Decorator: run ``fn`` inside ``engaged()`` (an NI fetch entry point)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with engaged():
            return fn(*args, **kwargs)
    return wrapper


def declared_port(source: object) -> int | None:
    """The extra port a sealed source may use: its Library-declared ``port``, else the explicit port in
    its own ``url`` (the user sealed that address, so its port is part of what they kept)."""
    if not isinstance(source, dict):
        return None
    port = source.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
        return port
    url = source.get("url")
    try:
        return urlparse(url).port if isinstance(url, str) else None
    except ValueError:
        return None  # a malformed port: netguard refuses the URL itself


def set_dedupe(on: bool) -> None:
    """Turn response reuse on or off for this thread (the tick: on; a manual run: off)."""
    SHARED._local.dedupe = bool(on)


def admit_page(url: str) -> None:
    """Budget + pause + robots for a read that bypasses netguard (the page browser's render)."""
    assert url, "url required"
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise netguard.FetchError("no host in URL")
    SHARED.admit(host)
    if not SHARED.robots_ok(url):
        raise netguard.FetchError("robots.txt asks automated readers to stay out", kind="robots")
