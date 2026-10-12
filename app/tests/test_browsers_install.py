"""Browser component installer (§35): a REAL local release server over real sockets (only
the host part of each URL is rewritten to 127.0.0.1 by the test transport), so the
redirect allowlist, the size cap, the hash check, the safe untar, the atomic commit, the
prune and the launch-time re-hash all run on the real code paths."""

from __future__ import annotations

import io
import json
import os
import tarfile
import threading

import pytest

from smartbrain_3000.browsers import install, release, walls
from smartbrain_3000.browsers import manifest as mf
from tests import _browsers as hb

PINNED = f"/github.com/h4ckf0r0day/obscura/releases/download/v{hb.FAKE_VERSION}/{hb.ASSET}"
CDN = f"/objects.githubusercontent.com/release-asset/{hb.ASSET}"


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """Test manifests + a private data dir; returns (root, server, manifest, tarball)."""
    tarball = hb.fake_tarball()
    root = hb.use_test_engines(monkeypatch, tmp_path, [hb.manifest_doc(tarball)])
    server = hb.LocalServer(hb.release_routes(tarball))
    yield root, server, mf.load("obscura"), tarball
    server.close()


def _install(server, m, **kw):
    return install.install(m, install.platform_key(), transport=hb.transport_for(server), **kw)


def _vdir(root):
    return root / "obscura" / hb.FAKE_VERSION


def test_install_follows_one_allowlisted_redirect_and_commits(rig) -> None:
    root, server, m, _ = rig
    got = _install(server, m)
    assert got["ok"] and got["served_by"] == "objects.githubusercontent.com", got
    assert server.requests == [PINNED, CDN]
    assert sorted(os.listdir(_vdir(root))) == ["INSTALLED.json", "obscura"]  # worker never written
    assert oct(os.stat(_vdir(root) / "obscura").st_mode & 0o777) == "0o755"
    meta = json.loads((_vdir(root) / "INSTALLED.json").read_text())
    assert set(meta) == {"name", "version", "sha256", "installed_at", "platform"}
    assert meta["sha256"] == m.platforms[install.platform_key()].sha256
    assert install.verify_installed("obscura") == (True, "")
    assert sorted(os.listdir(root / "obscura")) == [hb.FAKE_VERSION]  # no .part / .partial left


def test_release_assets_host_is_allowlisted_too(tmp_path, monkeypatch) -> None:
    tarball = hb.fake_tarball()
    hb.use_test_engines(monkeypatch, tmp_path, [hb.manifest_doc(tarball)])
    server = hb.LocalServer(hb.release_routes(tarball, redirect_to="release-assets.githubusercontent.com"))
    try:
        got = _install(server, mf.load("obscura"))
    finally:
        server.close()
    assert got["ok"] and got["served_by"] == "release-assets.githubusercontent.com"


@pytest.mark.parametrize("location", [
    "https://evil.example/release-asset/x.tar.gz",
    "http://objects.githubusercontent.com/release-asset/x.tar.gz",
    "https://objects.githubusercontent.com:8443/release-asset/x.tar.gz",
    "https://user@objects.githubusercontent.com/release-asset/x.tar.gz",
    "https://objects.githubusercontent.com.evil.example/x.tar.gz",
    "",
])
def test_redirect_off_the_allowlist_is_refused(rig, location: str) -> None:
    root, server, m, _ = rig
    server.routes[PINNED] = (302, {"Location": location} if location else {}, b"")
    got = _install(server, m)
    assert (got["ok"], got["code"]) == (False, "redirect")
    assert server.requests == [PINNED]  # the bad hop is never contacted
    assert not _vdir(root).exists() and os.listdir(root / "obscura") == []


def test_a_second_redirect_is_refused(rig) -> None:
    _root, server, m, tarball = rig
    server.routes[CDN] = (302, {"Location": "https://objects.githubusercontent.com/again"}, b"")
    assert _install(server, m)["code"] == "redirect"


def test_download_aborts_past_the_pinned_size(rig) -> None:
    root, server, m, tarball = rig
    server.routes[CDN] = (200, {}, tarball + b"\0" * 100)
    got = _install(server, m)
    assert got["code"] == "oversize"
    assert os.listdir(root / "obscura") == []  # the .part is gone


def test_hash_mismatch_is_refused_and_the_part_removed(rig) -> None:
    root, server, m, tarball = rig
    flipped = bytearray(tarball)
    flipped[len(flipped) // 2] ^= 0xFF
    server.routes[CDN] = (200, {}, bytes(flipped))
    got = _install(server, m)
    assert got["code"] == "hash"
    assert os.listdir(root / "obscura") == []
    row = install.status_row(m)
    assert row["phase"] == "error" and "github.com" in row["error"] and "://" not in row["error"]


def test_truncated_downloads_retry_a_bounded_number_of_times(rig) -> None:
    _root, server, m, tarball = rig
    server.routes[CDN] = (200, {}, tarball[:-10])
    assert _install(server, m)["code"] == "truncated"
    assert server.requests.count(PINNED) == release.MAX_ATTEMPTS


def test_server_errors_retry_and_refusals_do_not(rig) -> None:
    _root, server, m, _ = rig
    server.routes[CDN] = (503, {}, b"")
    assert _install(server, m)["code"] == "server"
    assert server.requests.count(CDN) == release.MAX_ATTEMPTS
    server.requests.clear()
    server.routes[CDN] = (404, {}, b"")
    assert _install(server, m)["code"] == "refused"
    assert server.requests.count(CDN) == 1


def _raw(name: str, kind: bytes, *, size: int = 0, linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.size, info.linkname, info.mode = kind, size, linkname, 0o755
    return info


ENGINE = hb.engine_bytes()
WORKER = ("obscura-worker", b"never run\n", 0o755)
UNSAFE = {
    "absolute": ([("/tmp/sb_b1_abs", ENGINE, 0o755), WORKER], []),
    "dotdot": ([("../obscura", ENGINE, 0o755), WORKER], []),
    "nested": ([("bin/obscura", ENGINE, 0o755), WORKER], []),
    "symlink": ([WORKER], [_raw("obscura", tarfile.SYMTYPE, linkname="/etc/passwd")]),
    "hardlink": ([WORKER], [_raw("obscura", tarfile.LNKTYPE, linkname="obscura-worker")]),
    "chardev": ([WORKER], [_raw("obscura", tarfile.CHRTYPE)]),
    "fifo": ([WORKER], [_raw("obscura", tarfile.FIFOTYPE)]),
    "directory": ([WORKER], [_raw("obscura", tarfile.DIRTYPE)]),
    # a link or special file is refused even where it names a member that is never written
    # (the size pin alone would not catch these: only the type check does)
    "symlink_unwritten": ([("obscura", ENGINE, 0o755)],
                          [_raw("obscura-worker", tarfile.SYMTYPE, linkname="/etc/passwd")]),
    "hardlink_unwritten": ([("obscura", ENGINE, 0o755)],
                           [_raw("obscura-worker", tarfile.LNKTYPE, linkname="obscura")]),
    "fifo_unwritten": ([("obscura", ENGINE, 0o755)], [_raw("obscura-worker", tarfile.FIFOTYPE)]),
    "stranger": ([("obscura", ENGINE, 0o755), WORKER, ("evil.sh", b"#!/bin/sh\n", 0o755)], []),
    "duplicate": ([("obscura", ENGINE, 0o755), WORKER, ("obscura", ENGINE, 0o755)], []),
    "missing_member": ([("obscura", ENGINE, 0o755)], []),
    "size_mismatch": ([("obscura", ENGINE + b"#", 0o755), WORKER], []),
    "many_strangers": ([("obscura", ENGINE, 0o755), WORKER]
                 + [(f"f{i}", b"x", 0o644) for i in range(20)], []),
}


@pytest.mark.parametrize("case", sorted(UNSAFE))
def test_unsafe_tar_members_are_refused_one_by_one(tmp_path, monkeypatch, case: str) -> None:
    members, extra = UNSAFE[case]
    tarball = hb.tar_bytes(members, extra=extra)
    root = hb.use_test_engines(monkeypatch, tmp_path, [hb.manifest_doc(tarball)])
    server = hb.LocalServer(hb.release_routes(tarball))
    try:
        got = _install(server, mf.load("obscura"))
    finally:
        server.close()
    assert (got["ok"], got["code"]) == (False, "unsafe_tar"), (case, got)
    assert os.listdir(root / "obscura") == []  # no .partial, no version dir, no escaped file
    assert not os.path.exists("/tmp/sb_b1_abs")


def test_member_hash_must_match_the_manifest(tmp_path, monkeypatch) -> None:
    tampered = bytearray(ENGINE)
    tampered[-2] ^= 0x01  # same size, different bytes
    tarball = hb.tar_bytes([("obscura", bytes(tampered), 0o755), WORKER])
    root = hb.use_test_engines(monkeypatch, tmp_path, [hb.manifest_doc(tarball, member=ENGINE)])
    server = hb.LocalServer(hb.release_routes(tarball))
    try:
        assert _install(server, mf.load("obscura"))["code"] == "hash"
    finally:
        server.close()
    assert os.listdir(root / "obscura") == []


def test_install_is_visible_only_after_the_atomic_rename(rig, monkeypatch) -> None:
    root, server, m, _ = rig

    def refuse_rename(src, dst):
        assert (src / "INSTALLED.json").is_file()  # committed metadata exists before the rename
        assert not _vdir(root).exists()            # ...and nothing is visible yet
        raise OSError("simulated crash at the commit")

    monkeypatch.setattr(install, "_rename", refuse_rename)
    got = _install(server, m)
    assert got["ok"] is False
    assert install.verify_installed("obscura") == (False, "absent")
    assert os.listdir(root / "obscura") == []
    monkeypatch.setattr(install, "_rename", os.rename)
    seen: list[tuple[bool, str]] = []

    def self_test(manifest, exe):
        seen.append(install.verify_installed(manifest.name))  # runs after the rename
        return "ok"

    assert _install(server, m, self_test=self_test)["ok"]
    assert seen == [(True, "")]


def test_prune_keeps_the_current_version_only(rig) -> None:
    root, server, m, _ = rig
    older = root / "obscura" / "0.0.1"
    older.mkdir(parents=True)
    (older / "INSTALLED.json").write_text("{}")
    (root / "obscura" / "0.0.2.partial").mkdir()
    (root / "obscura" / "0.0.3.tar.gz.part").write_bytes(b"x")
    os.symlink("/etc", root / "obscura" / "link")
    assert _install(server, m)["ok"]
    assert os.listdir(root / "obscura") == [hb.FAKE_VERSION]
    assert os.path.isdir("/etc")  # a link is removed, never followed


def test_verify_installed_detects_every_change(rig) -> None:
    root, server, m, _ = rig
    assert _install(server, m)["ok"]
    exe = _vdir(root) / "obscura"
    original = exe.read_bytes()
    flipped = bytearray(original)
    flipped[100] ^= 0x01
    exe.write_bytes(bytes(flipped))  # same size, one bit
    assert install.verify_installed("obscura") == (False, "modified")
    exe.write_bytes(original)
    assert install.verify_installed("obscura") == (True, "")
    os.chmod(exe, 0o644)
    assert install.verify_installed("obscura") == (False, "modified")
    os.chmod(exe, 0o775)
    assert install.verify_installed("obscura") == (False, "modified")  # group-writable
    os.chmod(exe, 0o755)
    (_vdir(root) / "libinject.so").write_bytes(b"x")
    assert install.verify_installed("obscura") == (False, "extra_files")
    os.remove(_vdir(root) / "libinject.so")
    os.remove(exe)
    os.symlink("/bin/sh", exe)
    assert install.verify_installed("obscura") == (False, "modified")
    meta = json.loads((_vdir(root) / "INSTALLED.json").read_text())
    (_vdir(root) / "INSTALLED.json").write_text(json.dumps({**meta, "sha256": "0" * 64}))
    assert install.verify_installed("obscura") == (False, "stale")
    os.remove(_vdir(root) / "INSTALLED.json")
    assert install.verify_installed("obscura") == (False, "absent")


def test_ensure_replaces_a_modified_install(rig) -> None:
    root, server, m, _ = rig
    assert _install(server, m)["ok"]
    (_vdir(root) / "obscura").write_bytes(b"#!/bin/sh\necho owned\n")
    server.requests.clear()
    row = install.ensure("obscura", transport=hb.transport_for(server))
    assert server.requests == [PINNED, CDN]
    assert install.verify_installed("obscura") == (True, "")
    assert row["phase"] == "ready" and row["sandbox"] == "null"
    server.requests.clear()
    install.ensure("obscura", transport=hb.transport_for(server))
    assert server.requests == []  # a clean install is never fetched again


def test_disabled_never_downloads(rig, monkeypatch) -> None:
    _root, server, m, _ = rig
    monkeypatch.setenv(install.DISABLE_ENV, "1")
    row = install.ensure("obscura", transport=hb.transport_for(server))
    assert (row["phase"], row["eligible"], row["reason"]) == ("disabled", False, "disabled")
    assert _install(server, m)["code"] == "disabled"
    assert server.requests == []


def test_an_engine_that_cannot_run_is_never_fetched(rig, monkeypatch) -> None:
    _root, server, _m, _ = rig
    real = install.platform_key()
    monkeypatch.setattr(install, "platform_key", lambda: None)
    row = install.ensure("obscura", transport=hb.transport_for(server))
    assert (row["phase"], row["reason"]) == ("unavailable", "platform")
    monkeypatch.setattr(install, "platform_key", lambda: real)
    monkeypatch.setattr(install, "glibc_version", lambda: (2, 16))  # below the manifest's 2.17
    row = install.ensure("obscura", transport=hb.transport_for(server))
    assert (row["phase"], row["reason"]) == ("unavailable", "missing_libs")
    assert server.requests == []


def test_self_test_runs_under_the_wall_or_records_the_skip(rig, monkeypatch) -> None:
    _root, server, m, _ = rig
    assert _install(server, m)["self_test"] == "ok"  # NullWall + the fake engine's --version
    row = install.status_row(m)
    assert (row["phase"], row["last_self_test"], row["eligible"]) == ("ready", "ok", True)
    monkeypatch.delenv(walls.NULLWALL_ENV)
    assert _install(server, m)["self_test"] == "skipped(wall_unavailable)"
    row = install.status_row(m)
    assert row["phase"] == "unavailable" and row["reason"] in walls.REASONS
    assert row["last_self_test"] == "skipped(wall_unavailable)" and row["sandbox"] == "none"


def test_a_failed_self_test_is_an_error_with_a_fixed_sentence(rig) -> None:
    _root, server, m, _ = rig
    got = _install(server, m, self_test=lambda manifest, exe: "failed:exit")
    assert got["self_test"] == "failed:exit"
    row = install.status_row(m)
    assert row["phase"] == "error" and row["error"] == install._SENTENCES["self_test"]


def test_install_is_single_flight(rig) -> None:
    _root, server, m, _ = rig
    gate = threading.Event()
    inside = threading.Event()
    transport = hb.transport_for(server)

    def slow(url):
        inside.set()
        gate.wait(10)
        return transport(url)

    first: dict = {}
    worker = threading.Thread(
        target=lambda: first.update(install.install(m, install.platform_key(), transport=slow)))
    worker.start()
    assert inside.wait(10)
    assert install.install(m, install.platform_key(), transport=transport)["code"] == "busy"
    assert install.status_row(m)["phase"] == "downloading"
    gate.set()
    worker.join(30)
    assert first["ok"]


def test_download_deadline_is_enforced(rig, monkeypatch) -> None:
    _root, server, m, _ = rig
    ticks = iter(range(0, 10_000_000, 1000))
    monkeypatch.setattr(release, "_monotonic", lambda: float(next(ticks)))
    assert _install(server, m)["code"] == "timeout"


def test_error_sentences_are_fixed_and_name_github() -> None:
    for code, sentence in install._SENTENCES.items():
        assert "/" not in sentence and "{" not in sentence, code  # no URL, path or template
        if code in ("network", "timeout", "truncated", "server", "refused", "redirect",
                    "oversize", "hash", "unsafe_tar"):
            assert "github.com" in sentence, code


def test_status_rows_are_closed_and_cheap(rig) -> None:
    _root, _server, m, _ = rig
    row = install.status_row(m)
    assert set(row) == {"name", "phase", "pct", "version", "age_days", "eligible", "reason",
                        "sandbox", "last_self_test", "error"}
    assert row["phase"] == "unavailable" and row["reason"] in walls.REASONS  # no engine yet
    assert row["age_days"] >= 0 and row["version"] == hb.FAKE_VERSION


def test_progress_is_visible_while_downloading(rig) -> None:
    _root, server, m, _ = rig
    transport = hb.transport_for(server)
    phases: list[tuple[str, int]] = []

    class Spy:
        def __init__(self, url):
            self._inner = transport(url)
            self.status_code, self.headers = self._inner.status_code, self._inner.headers

        def iter_bytes(self, size):
            for chunk in self._inner.iter_bytes(size):
                row = install.status_row(m)
                phases.append((row["phase"], row["pct"]))
                yield chunk

        def close(self):
            self._inner.close()

    assert install.install(m, install.platform_key(), transport=Spy)["ok"]
    assert phases and all(phase == "downloading" for phase, _ in phases)
    assert all(0 <= pct <= 98 for _, pct in phases)


def test_tar_helper_builds_what_the_cases_claim() -> None:
    data = hb.tar_bytes([("a", b"1", 0o644)], extra=[_raw("b", tarfile.SYMTYPE, linkname="/x")])
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        kinds = {m.name: m.type for m in tf}
    assert kinds == {"a": tarfile.REGTYPE, "b": tarfile.SYMTYPE}
