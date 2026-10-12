"""The test-only NullWall (ni-format §35): confines nothing.

It is built ONLY when ``SMARTBRAIN_BROWSER_TEST_NULLWALL=1`` AND every executable in the
engine directory is the fake engine fixture (a Python script carrying
``FAKE_ENGINE_MARKER``) — both checked, both asserted. A real engine binary can never run
under it, and (symmetrically) the real walls never run the fake engine.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import manifest as mf
from .base import NULLWALL_ENV, Wall, is_fake_engine


class NullWall(Wall):
    """TEST ONLY: confines nothing. The fake engine dials the egress port directly."""

    kind = "null"
    endpoint_kind = "tcp"
    expects_children = False

    def __init__(self, engine_dir: Path) -> None:
        assert os.environ.get(NULLWALL_ENV) == "1", "NullWall needs the test env flag"
        assert is_fake_engine(engine_dir), "NullWall only ever runs the fake engine"
        self._engine_dir = engine_dir

    def wrap(self, argv: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        assert argv and argv[0].startswith(str(self._engine_dir)), "argv runs the engine"
        assert isinstance(env, dict), "env must be a dict"
        return list(argv), dict(env)

    def verify(self, pid_tree: list[int]) -> bool:
        assert isinstance(pid_tree, list), "pid tree must be a list"
        assert all(isinstance(p, int) for p in pid_tree), "pids are ints"
        return all(p > 1 for p in pid_tree)

    def teardown(self) -> None:
        assert self._engine_dir is not None, "wall was prepared"
        assert self.kind == "null", "only the null wall tears down this way"


def self_test(m: mf.Manifest, engine_dir: Path) -> tuple[str, list[dict]]:
    """The fake engine's ``--version`` under the NullWall (B1's probe)."""
    assert isinstance(engine_dir, Path), "engine dir required"
    assert is_fake_engine(engine_dir), "the null self-test runs the fake engine only"
    from .. import runner  # lazy: runner imports walls

    result = runner.probe_version(m, engine_dir)
    return result, [{"row": "engine_version", "ok": result == "ok", "detail": result}]
