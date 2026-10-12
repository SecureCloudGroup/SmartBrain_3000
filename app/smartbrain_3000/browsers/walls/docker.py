"""The Docker wall (ni-format §35; P3 design §2): the renderer sidecar, app side.

The app container never runs the engine. The ``render`` compose profile adds a sidecar
(``network_mode: none``, read-only root, ``cap_drop: [ALL]``, no-new-privileges, init,
pid/memory limits, NO app data volume) that holds the pinned Linux engine (installed and
hash-verified at image build) and runs ``browsers.render_agent``. The two containers share
exactly one thing: the named volume ``smartbrain_render`` (a tmpfs; mode 1770, owned by the
app's uid/gid; the sidecar runs as 65532 with the app's gid) mounted at ``/render`` in both.

A render's run dir is created ON the volume, so every path the app renders into the argv
(``--storage-dir``, ``--output``, HOME, TMPDIR) means the same file in both containers, and
the egress child's Unix socket sits in it. ``SidecarWall`` opens the run dir to the shared
group (1770, socket 0660), writes ``job.json`` (closed keys; argv/env rendered by the app's
own ``cmdline``), waits for ``result.json`` with the render's deadline, and reads it and the
output file with ``O_NOFOLLOW`` on every path component (the sidecar is untrusted). The
agent re-validates the argv against the manifest, re-hashes its engine, starts the loopback
relay 127.0.0.1:``RELAY_PORT`` → the egress socket, runs the engine with the same
spawn/watch/kill code as a native run, kills every stray process, and removes the engine's
state before it answers.

``check`` (cheap, no spawn, every call): ``READY.json`` — written once per agent boot after
its own canary (connects to a public IP, the Docker gateways and the metadata address must
fail; only ``lo`` is up; the root is read-only; it runs as non-root; its engine re-hashes
clean) — must be valid and ``ok``, and ``HEARTBEAT.json`` from the same boot must be fresh.
Otherwise ``docker_no_sidecar``.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import tempfile
import time
from pathlib import Path

from .. import egress
from .. import manifest as mf
from .base import Unavailable, Wall, row, verdict

RENDER_ROOT = Path("/render")
READY_NAME, HEARTBEAT_NAME = "READY.json", "HEARTBEAT.json"
JOB_NAME, RESULT_NAME = "job.json", "result.json"
RUN_PREFIX = "smartbrain-render-"
RELAY_PORT = 3128
HEARTBEAT_MAX_AGE_S = 10.0
PICKUP_S = 15.0
POLL_S = 0.05
SHARED_DIR_MODE, SHARED_SOCKET_MODE = 0o1770, 0o660
READY_KEYS = frozenset({"v", "boot", "at", "uid", "gid", "interfaces", "canaries", "netns",
                        "readonly_root", "engine", "ok"})
ENGINE_KEYS = frozenset({"name", "version", "platform", "sha256", "verified"})
CANARY_KEYS = frozenset({"target", "outcome"})
HEARTBEAT_KEYS = frozenset({"v", "boot", "ts"})
JOB_KEYS = frozenset({"v", "kind", "run_id", "argv", "env", "out", "deadline_s", "rss_mb",
                      "cpu_s", "data_mb", "relay_port"})
RESULT_KEYS = frozenset({"v", "run_id", "spawned", "rc", "verdict", "signals", "refused", "rows"})
VERDICTS = ("", "exited", "timeout", "memory", "wall", "children")
REFUSALS = ("", "job", "argv", "env", "engine", "busy")
JSON_CAP = 64 * 1024
_MAX_SIGNALS, _MAX_SIGNAL_LEN = 64, 2048
_SUBDIRS = ("home", "tmp", "storage", "out")
_REQUIRED = ("ready", "interfaces", "network", "readonly_root", "uid", "engine_verified",
             "proxy", "data_absent", "dns", "engine_version", "census")


def root_ready() -> bool:
    """The shared volume is mounted here and writable by this process."""
    assert isinstance(RENDER_ROOT, Path), "the root is a path"
    ok = RENDER_ROOT.is_dir() and not RENDER_ROOT.is_symlink() and os.access(RENDER_ROOT, os.W_OK)
    assert isinstance(ok, bool), "a flag"
    return ok


def read_json(directory: str | Path, name: str, keys: frozenset[str],
              cap: int = JSON_CAP) -> dict | None:
    """A closed-key JSON object from ``directory/name``, never through a link; else None."""
    assert name and "/" not in name, "a plain file name"
    assert keys, "a closed key set"
    data = read_file(directory, name, cap)[0]
    if data is None:
        return None
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) and set(obj) == keys else None


def read_file(directory: str | Path, rel: str, cap: int) -> tuple[bytes | None, bool]:
    """``(bytes, False)`` | ``(None, True)`` over the cap | ``(None, False)`` missing, not
    regular, or reached through a link at ANY component (O_NOFOLLOW + dir fds)."""
    assert cap > 0, "a positive cap"
    parts = rel.split("/")
    assert all(p and p not in (".", "..") for p in parts), "a plain relative path"
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        fd = os.open(directory, flags | os.O_DIRECTORY)
    except OSError:
        return None, False
    try:
        for part in parts[:-1]:  # bounded by the path
            nxt = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(parts[-1], flags, dir_fd=fd)
    except OSError:
        return None, False
    finally:
        os.close(fd)
    with os.fdopen(leaf, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None, False
        if info.st_size > cap:
            return None, True
        data = fh.read(cap + 1)
    return (None, True) if len(data) > cap else (data, False)


def write_json(directory: str | Path, name: str, obj: dict) -> None:
    """Write ``obj`` as ``directory/name`` atomically: a fresh O_EXCL|O_NOFOLLOW temp file,
    then a rename (a planted file or link at either name is never written through)."""
    assert name and "/" not in name, "a plain file name"
    assert isinstance(obj, dict), "a JSON object"
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    assert len(body) <= JSON_CAP, "bounded JSON"
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    tmp = f".{name}.{secrets.token_hex(6)}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660, dir_fd=dfd)
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    finally:
        os.close(dfd)


def ready_facts() -> dict | None:
    """READY.json + a fresh HEARTBEAT.json from the same boot, both valid; else None."""
    assert isinstance(RENDER_ROOT, Path), "the volume root is a path"
    ready = read_json(RENDER_ROOT, READY_NAME, READY_KEYS)
    beat = read_json(RENDER_ROOT, HEARTBEAT_NAME, HEARTBEAT_KEYS)
    if ready is None or beat is None or not valid_ready(ready):
        return None
    if beat["v"] != 1 or beat["boot"] != ready["boot"] or not isinstance(beat["ts"], (int, float)):
        return None
    fresh = abs(time.time() - float(beat["ts"])) <= HEARTBEAT_MAX_AGE_S
    assert isinstance(fresh, bool), "a flag"
    return ready if fresh else None


def valid_ready(ready: dict) -> bool:
    """The agent's boot facts prove the wall: no route out, only ``lo`` up, read-only root,
    non-root, a verified engine — and it says ``ok``."""
    assert set(ready) == READY_KEYS, "closed READY keys"
    canaries = ready["canaries"]
    engine = ready["engine"]
    if ready["v"] != 1 or ready["ok"] is not True or not isinstance(canaries, list):
        return False
    if not isinstance(engine, dict) or set(engine) != ENGINE_KEYS or engine["verified"] is not True:
        return False
    if not 3 <= len(canaries) <= 16 or not all(
            isinstance(c, dict) and set(c) == CANARY_KEYS and isinstance(c["outcome"], str)
            and c["outcome"] != "connected" for c in canaries):
        return False
    uid = ready["uid"]
    assert isinstance(canaries, list) and isinstance(engine, dict), "shapes checked above"
    return (ready["interfaces"] == ["lo"] and ready["readonly_root"] is True
            and isinstance(uid, int) and not isinstance(uid, bool) and uid > 0)


def check() -> Unavailable | None:
    """``None`` when a ready sidecar answers on the shared volume (no spawn, no cache)."""
    if not root_ready() or ready_facts() is None:
        return Unavailable("docker_no_sidecar")
    assert RENDER_ROOT.is_dir(), "the volume is mounted"
    assert root_ready(), "the volume is writable"
    return None


def engine_ready(m: mf.Manifest, platform: str | None) -> bool:
    """The ready sidecar holds exactly this manifest's pinned build for this platform."""
    assert isinstance(m, mf.Manifest), "manifest required"
    assert platform is None or isinstance(platform, str), "a platform key"
    facts = ready_facts() if root_ready() else None
    if facts is None or platform is None or platform not in m.platforms:
        return False
    engine = facts["engine"]
    want = (m.name, m.version, platform, m.platforms[platform].sha256)
    return (engine["name"], engine["version"], engine["platform"], engine["sha256"]) == want


def run_parent() -> str | None:
    """Run dirs live on the shared volume (the same paths in both containers)."""
    return str(RENDER_ROOT) if root_ready() else None


class SidecarWall(Wall):
    """One render handed to the sidecar agent."""

    kind = "sidecar"
    endpoint_kind = "unix"
    expects_children = False
    remote = True

    def __init__(self, run_dir: str, endpoint: dict) -> None:
        real = os.path.realpath(run_dir)
        if os.path.dirname(real) != str(RENDER_ROOT) or not os.path.basename(real).startswith(RUN_PREFIX):
            raise ValueError("the run dir lives on the shared volume")
        if endpoint.get("kind") != "unix" or endpoint.get("path") != os.path.join(real, "egress.sock"):
            raise ValueError("the egress socket lives in the run dir")
        self._run_dir, self._socket = real, endpoint["path"]
        for sub in ("", *_SUBDIRS):  # bounded: the run dir and its four subdirs
            os.chmod(os.path.join(real, sub) if sub else real, SHARED_DIR_MODE)
        os.chmod(self._socket, SHARED_SOCKET_MODE)
        assert self._run_dir.startswith(str(RENDER_ROOT) + "/"), "on the volume"
        assert os.path.isdir(self._run_dir), "the run dir exists"

    def proxy_port(self, endpoint: dict) -> int:
        assert endpoint.get("path") == self._socket, "the relay serves this run's socket"
        return RELAY_PORT

    def wrap(self, argv: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        assert argv and isinstance(env, dict), "argv and env required"
        assert self.remote, "the sidecar runs the engine; nothing is wrapped here"
        return list(argv), dict(env)

    def verify(self, pid_tree: list[int]) -> bool:
        assert isinstance(pid_tree, list), "pid tree must be a list"
        assert not pid_tree, "no engine process ever runs in the app container"
        return True

    def run(self, argv: list[str], env: dict[str, str], *, out_path: str, cap: int,
            deadline_s: float, rss_mb: int, cpu_s: int, data_mb: int) -> dict:
        """Hand the job over, wait for the result, read the output. Returns the runner's
        outcome fields ``{spawned, rc, verdict, signals, output, oversize}``."""
        assert cap > 0 and deadline_s > 0, "limits required"
        rel = os.path.relpath(os.path.realpath(out_path), self._run_dir)
        assert not rel.startswith(".."), "the output lands in the run dir"
        job = {"v": 1, "kind": "render", "run_id": os.path.basename(self._run_dir),
               "argv": list(argv), "env": dict(env), "out": rel, "deadline_s": float(deadline_s),
               "rss_mb": int(rss_mb), "cpu_s": int(cpu_s), "data_mb": int(data_mb),
               "relay_port": RELAY_PORT}
        result = self.submit(job, deadline_s)
        outcome = {"spawned": False, "rc": None, "verdict": "", "signals": [], "output": None,
                   "oversize": False}
        if result is None:
            return {**outcome, "spawned": True, "verdict": "timeout"}
        if result["refused"]:
            return outcome
        outcome.update(spawned=result["spawned"], rc=result["rc"], verdict=result["verdict"],
                       signals=list(result["signals"]))
        if result["rc"] == 0 and result["verdict"] == "exited":
            outcome["output"], outcome["oversize"] = read_file(self._run_dir, rel, cap)
        return outcome

    def submit(self, job: dict, deadline_s: float) -> dict | None:
        """Write ``job.json`` and wait (bounded) for a valid ``result.json``."""
        assert set(job) == JOB_KEYS, "closed job keys"
        assert job["run_id"] == os.path.basename(self._run_dir), "the job names its run dir"
        write_json(self._run_dir, JOB_NAME, job)
        limit = time.monotonic() + deadline_s + PICKUP_S
        for _ in range(int((deadline_s + PICKUP_S) / POLL_S) + 2):  # fixed bound
            result = read_json(self._run_dir, RESULT_NAME, RESULT_KEYS)
            if result is not None:
                return result if valid_result(result, job["run_id"]) else None
            if time.monotonic() >= limit:
                return None
            time.sleep(POLL_S)
        return None

    def teardown(self) -> None:
        assert self._run_dir, "wall was prepared"
        assert self.kind == "sidecar", "sidecar teardown"


def valid_result(result: dict, run_id: str) -> bool:
    """Closed keys and sane types; the signals are bounded text lines."""
    assert set(result) == RESULT_KEYS, "closed result keys"
    assert run_id.startswith(RUN_PREFIX), "a run id"
    rc, signals, rows = result["rc"], result["signals"], result["rows"]
    if result["v"] != 1 or result["run_id"] != run_id or not isinstance(result["spawned"], bool):
        return False
    if rc is not None and (not isinstance(rc, int) or isinstance(rc, bool) or not -64 <= rc <= 255):
        return False
    if result["verdict"] not in VERDICTS or result["refused"] not in REFUSALS:
        return False
    if not isinstance(signals, list) or len(signals) > _MAX_SIGNALS or not all(
            isinstance(s, str) and len(s) <= _MAX_SIGNAL_LEN for s in signals):
        return False
    return isinstance(rows, list) and len(rows) <= 16 and all(
        isinstance(r, dict) and set(r) == {"row", "ok", "detail"} and isinstance(r["ok"], bool)
        and isinstance(r["row"], str) and isinstance(r["detail"], str) for r in rows)


def self_test(m: mf.Manifest, engine_dir: Path) -> tuple[str, list[dict]]:
    """The agent's boot canary (READY facts) and a ``selftest`` job round trip: the relay
    reaches THIS egress child, no app data is visible, DNS fails, the engine starts."""
    assert isinstance(m, mf.Manifest), "manifest required"
    assert isinstance(engine_dir, Path), "engine dir required"
    facts = ready_facts() if root_ready() else None
    if facts is None:
        rows = [row("ready", False, "no ready sidecar")]
        return verdict(rows, _REQUIRED), rows
    rows = [row("ready", True), row("interfaces", facts["interfaces"] == ["lo"]),
            row("network", all(c["outcome"] != "connected" for c in facts["canaries"]),
                ",".join(c["outcome"] for c in facts["canaries"])[:100]),
            row("readonly_root", facts["readonly_root"] is True),
            row("uid", isinstance(facts["uid"], int) and facts["uid"] > 0, str(facts["uid"])),
            row("engine_verified", facts["engine"]["verified"] is True)]
    rows += _selftest_job(m, engine_dir)
    return verdict(rows, _REQUIRED), rows


def _selftest_job(m: mf.Manifest, engine_dir: Path) -> list[dict]:
    """One ``selftest`` job through a real egress child on the shared volume."""
    assert m.executable, "the engine's file name"
    run_dir = tempfile.mkdtemp(prefix=RUN_PREFIX, dir=str(RENDER_ROOT))
    assert run_dir.startswith(str(RENDER_ROOT)), "the selftest run dir is on the volume"
    handle = None
    try:
        for sub in _SUBDIRS:
            os.mkdir(os.path.join(run_dir, sub), 0o700)
        handle = egress.start(run_dir, kind="unix", policy="own", sites=[],
                              main_host="selftest.invalid", main_port=443, max_life_s=120)
        wall = SidecarWall(run_dir, handle.endpoint)
        job = {"v": 1, "kind": "selftest", "run_id": os.path.basename(os.path.realpath(run_dir)),
               "argv": [str(engine_dir / m.executable), "--version"], "env": {},
               "out": "out/version.txt", "deadline_s": 20.0, "rss_mb": 512, "cpu_s": 30,
               "data_mb": 0, "relay_port": RELAY_PORT}
        result = wall.submit(job, 20.0)
        rows = list(result["rows"]) if result is not None else [row("proxy", False, "no result")]
    except (OSError, ValueError, egress.EgressError) as exc:
        rows = [row("setup", False, exc.__class__.__name__)]
    finally:
        census = _census(handle)
        shutil.rmtree(run_dir, ignore_errors=True)
    return rows + [census]


def _census(handle: object) -> dict:
    """The agent's proxy row went through the relay to THIS egress child."""
    if handle is None:
        return row("census", False, "no egress")
    assert hasattr(handle, "stop"), "an egress handle"
    try:
        rows, _dropped = handle.stop()  # type: ignore[attr-defined]
    except egress.EgressError as exc:
        return row("census", False, exc.reason)
    seen = [r["verdict"] for r in rows]
    assert isinstance(seen, list), "verdict list"
    return row("census", "ip_literal" in seen, ",".join(seen)[:100])
