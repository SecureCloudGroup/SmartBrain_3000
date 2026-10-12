"""Pinned release download + safe unpack for browser components (ni-format §35).

- ``fetch_verified``: the exact pinned URL from the manifest, through netguard's address
  validation + IP pin, never auto-redirected; exactly ONE redirect hop is allowed and only
  to https on a closed GitHub allowlist (release downloads answer 302 to a CDN host). The
  body streams to a ``.part`` file hashed as it arrives; the download aborts as soon as
  more than the pinned size has arrived, has a wall-clock deadline, and the sha256 must
  match before anything is unpacked. Transient failures retry a bounded number of times; a
  refusal never does.
- ``unpack``: by hand, never ``extract``/``extractall``. Top-level regular files only, each
  named in the manifest; absolute paths, ``..``, links (also for members that are never
  written), devices, FIFOs, directories, sparse files, duplicates and strangers are refused;
  member count and total size are capped; every written member must match its pinned size
  and sha256, is created O_EXCL|O_NOFOLLOW and gets exactly the manifest's mode.

Failures are ``ReleaseError(code)``; ``install`` turns the code into a fixed sentence.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tarfile
import time
import zlib
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from .. import netguard
from . import manifest as mf

log = logging.getLogger("smartbrain.browsers")

# GitHub release downloads 302 from github.com to one of these CDN hosts; nothing else.
REDIRECT_HOSTS = frozenset({"github.com", "objects.githubusercontent.com",
                            "release-assets.githubusercontent.com"})
CODES = frozenset({"network", "timeout", "truncated", "server", "refused", "redirect",
                   "oversize", "hash", "unsafe_tar", "disk"})
RETRYABLE = frozenset({"network", "truncated", "timeout", "server"})
MAX_ATTEMPTS = 3
_CHUNK = 1 << 20
_MAX_CHUNKS = 1_000_000
_DOWNLOAD_DEADLINE_S = 900.0
_MAX_MEMBERS = 16
_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_monotonic = time.monotonic  # module attribute so a test can drive the download deadline


class ReleaseError(Exception):
    """A refused or failed download/unpack step; ``code`` is from the closed ``CODES``."""

    def __init__(self, code: str) -> None:
        assert code in CODES, f"unknown release error code: {code}"
        assert isinstance(code, str), "code must be a string"
        super().__init__(code)
        self.code = code


class Stream(Protocol):
    """What the downloader reads: one HTTP response, never auto-redirected."""

    status_code: int
    headers: Mapping[str, str]

    def iter_bytes(self, chunk_size: int) -> Iterator[bytes]: ...

    def close(self) -> None: ...


Transport = Callable[[str], Stream]
Progress = Callable[[str, int], None]  # (phase, pct)


class PinnedStream:
    """One GET of an exact allowlisted URL through netguard's validation + IP pin."""

    def __init__(self, url: str) -> None:
        parts = urlsplit(url)
        host = parts.hostname or ""
        assert parts.scheme == "https", "downloads are https only"
        assert host in REDIRECT_HOSTS, "downloads only reach the GitHub allowlist"
        self._client = httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False,
                                    headers={"User-Agent": netguard.USER_AGENT})
        try:
            ip = netguard._validated_ip(host)
            self._resp = netguard._send_pinned(self._client, url, host, ip)
        except BaseException:
            self._client.close()
            raise
        self.status_code = self._resp.status_code
        self.headers = self._resp.headers

    def iter_bytes(self, chunk_size: int) -> Iterator[bytes]:
        assert chunk_size > 0, "chunk size must be positive"
        assert self._resp is not None, "response required"
        return self._resp.iter_bytes(chunk_size)

    def close(self) -> None:
        assert self._client is not None, "client required"
        assert self._resp is not None, "response required"
        self._resp.close()
        self._client.close()


def fetch_verified(m: mf.Manifest, platform: str, part: Path, transport: Transport,
                   progress: Progress) -> str:
    """Download with bounded retries for transient failures; returns the serving host."""
    assert callable(transport) and callable(progress), "transport + progress required"
    assert part.suffix == ".part", "downloads land in a .part file"
    for attempt in range(MAX_ATTEMPTS):  # fixed bound
        try:
            return _download_once(m, platform, part, transport, progress)
        except ReleaseError as exc:
            part.unlink(missing_ok=True)
            if exc.code not in RETRYABLE or attempt == MAX_ATTEMPTS - 1:
                raise
            log.info("browsers: %s download attempt %d failed (%s)", m.name, attempt + 1, exc.code)
    raise ReleaseError("network")


def _download_once(m: mf.Manifest, platform: str, part: Path, transport: Transport,
                   progress: Progress) -> str:
    """The pinned URL, plus at most ONE allowlisted redirect. Returns the serving host."""
    url = mf.source_url(m, platform)
    asset = m.platforms[platform]
    assert asset.size > 0, "pinned size required"
    assert callable(transport), "transport required"
    progress("downloading", 0)
    for hop in range(2):  # the pinned URL + one redirect, never more
        try:
            stream = transport(url)
        except (httpx.HTTPError, netguard.FetchError, OSError):
            raise ReleaseError("network") from None
        try:
            code = stream.status_code
            if code in _REDIRECT_CODES:
                if hop:
                    raise ReleaseError("redirect")
                url = _redirect_target(url, str(stream.headers.get("location") or ""))
                continue
            if code != 200:
                raise ReleaseError("server" if code >= 500 else "refused")
            digest = _stream_to_part(stream, part, asset.size, progress)
        finally:
            stream.close()
        progress("verifying", 99)
        if digest != asset.sha256:
            raise ReleaseError("hash")
        host = urlsplit(url).hostname or ""
        log.info("browsers: %s %s fetched from %s", m.name, m.version, host)
        return host
    raise ReleaseError("redirect")


def _redirect_target(current: str, location: str) -> str:
    """The single redirect hop: https, an allowlisted host, default port, no userinfo."""
    assert current.startswith("https://"), "the pinned URL is https"
    assert isinstance(location, str), "location must be a string"
    if not location or len(location) > 4096 or re.search(r"[\s\x00-\x1f]", location):
        raise ReleaseError("redirect")
    target = urljoin(current, location)
    parts = urlsplit(target)
    try:
        port = parts.port
    except ValueError:
        raise ReleaseError("redirect") from None
    if (parts.scheme != "https" or (parts.hostname or "") not in REDIRECT_HOSTS
            or parts.username or parts.password or port not in (None, 443)):
        raise ReleaseError("redirect")
    return target


def _stream_to_part(stream: Stream, part: Path, size: int, progress: Progress) -> str:
    """Write the body to ``part`` while hashing; abort once more than ``size`` arrived."""
    assert size > 0, "pinned size required"
    assert callable(progress), "progress callback required"
    hasher = hashlib.sha256()
    total = 0
    start = _monotonic()
    try:
        with open(part, "wb") as fh:
            for index, chunk in enumerate(stream.iter_bytes(_CHUNK)):  # bounded below
                total += len(chunk)
                if total > size or index >= _MAX_CHUNKS:
                    raise ReleaseError("oversize")
                hasher.update(chunk)
                fh.write(chunk)
                progress("downloading", min(98, total * 100 // size))
                if _monotonic() - start > _DOWNLOAD_DEADLINE_S:
                    raise ReleaseError("timeout")
    except httpx.HTTPError:
        raise ReleaseError("network") from None
    except OSError as exc:
        raise ReleaseError("disk" if exc.errno == 28 else "network") from None
    if total < size:
        raise ReleaseError("truncated")
    return hasher.hexdigest()


def _member_name(member: tarfile.TarInfo) -> str:
    """A member's name if it is a plain top-level regular file, else refuse."""
    assert isinstance(member, tarfile.TarInfo), "tar member required"
    name = member.name.removeprefix("./")
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ReleaseError("unsafe_tar")
    if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or member.linkname:
        raise ReleaseError("unsafe_tar")  # links, devices, FIFOs, dirs, sparse files
    assert not name.startswith("/"), "absolute names never pass"
    return name


def unpack(m: mf.Manifest, platform: str, tar_path: Path, partial: Path) -> None:
    """Safe untar of the verified tarball into the fresh directory ``partial``."""
    expected = {f.name: f for f in m.files}
    pins = {name: (sha, size) for name, sha, size in m.platforms[platform].members}
    assert set(pins) <= set(expected), "pins name expected files"
    assert not partial.exists(), "unpack goes into a fresh directory"
    partial.mkdir(mode=0o700)
    seen: set[str] = set()
    total = 0
    try:
        with tarfile.open(tar_path, mode="r:gz") as tf:
            for index, member in enumerate(tf):  # bounded below
                name = _member_name(member)
                total += member.size
                if (index >= _MAX_MEMBERS or name in seen or name not in expected
                        or total > _MAX_TOTAL_BYTES):
                    raise ReleaseError("unsafe_tar")
                seen.add(name)
                if expected[name].install:
                    sha, size = pins[name]
                    if member.size != size:
                        raise ReleaseError("unsafe_tar")
                    _extract_member(tf, member, partial / name, sha, expected[name].mode)
    except (tarfile.TarError, EOFError, zlib.error):
        raise ReleaseError("unsafe_tar") from None
    except OSError as exc:
        raise ReleaseError("disk" if exc.errno == 28 else "unsafe_tar") from None
    if seen != set(expected):
        raise ReleaseError("unsafe_tar")


def _extract_member(tf: tarfile.TarFile, member: tarfile.TarInfo, dest: Path, sha: str,
                    mode: int) -> None:
    """Stream one member to a fresh file (O_EXCL, no link follow), hash, then chmod."""
    assert len(sha) == 64 and 0 < mode <= 0o755, "pinned hash + mode required"
    src = tf.extractfile(member)
    if src is None:
        raise ReleaseError("unsafe_tar")
    hasher = hashlib.sha256()
    written = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(dest, flags, 0o600), "wb") as out:
        for _ in range(member.size // _CHUNK + 2):  # fixed bound from the header size
            block = src.read(_CHUNK)
            if not block:
                break
            written += len(block)
            hasher.update(block)
            out.write(block)
    if written != member.size or hasher.hexdigest() != sha:
        raise ReleaseError("hash")
    os.chmod(dest, mode)
    assert dest.is_file(), "member must be on disk"
