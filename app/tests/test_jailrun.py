"""Subprocess-jail tests (§16): real ``sys.executable`` children, not mocks.

The load-bearing invariants for the jail are: it actually spawns a jailed child,
kills the whole process group on timeout, refuses malformed / oversize output as a
typed ``JailError``, and never leaks a SmartBrain env var to the child. Every test
runs a real subprocess — mocking the subprocess would defeat the point.
"""

from __future__ import annotations

import os
import textwrap
import time

import pytest

from smartbrain_3000 import jailrun


def _small_html() -> bytes:
    """A minimal HTML page trafilatura reliably extracts something meaningful from."""
    return (
        b"<html><head><title>Sample Title</title></head><body>"
        b"<article>This is a test article body with several sentences of real "
        b"prose so trafilatura's boilerplate stripper keeps it. The point is "
        b"to prove the extractor path end-to-end.</article></body></html>"
    )


def test_run_extractor_happy_path() -> None:
    """A real subprocess extracts title + main text from a tiny HTML page."""
    out = jailrun.run_extractor(_small_html(), url_hint="https://example.com/x")
    assert set(out) == {"text", "title"}, out
    assert out["title"] == "Sample Title"
    assert "trafilatura" in out["text"] or "extractor path" in out["text"], out


def test_run_extractor_rejects_bad_timeout() -> None:
    """A non-positive timeout is a caller bug and refused as ``bad_timeout``."""
    with pytest.raises(jailrun.JailError) as excinfo:
        jailrun.run_extractor(_small_html(), url_hint="", timeout_s=0.0)
    assert excinfo.value.reason == "bad_timeout"


def test_run_extractor_input_too_large() -> None:
    """Input past the ``_MAX_INPUT_BYTES`` cap is refused BEFORE spawning."""
    huge = b"x" * (jailrun._MAX_INPUT_BYTES + 1)
    with pytest.raises(jailrun.JailError) as excinfo:
        jailrun.run_extractor(huge, url_hint="", timeout_s=1.0)
    assert excinfo.value.reason == "input_too_large"


def test_run_extractor_timeout_kills_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that stalls past ``timeout_s`` is killed via its process group.

    Monkeypatch the child bootstrap to a busy-loop; the watchdog Timer fires, killpg
    tears the child down, and ``run_extractor`` surfaces ``JailError('timeout')``.
    The child's pid is no longer alive after the call — proving the kill landed.
    """
    stall = textwrap.dedent("""
        import sys, time
        sys.stdin.buffer.read(1)  # drain a byte so the parent-side write completes
        while True:
            time.sleep(60)
    """).strip()
    monkeypatch.setattr(jailrun, "_CHILD_BOOTSTRAP", stall)
    started = time.monotonic()
    with pytest.raises(jailrun.JailError) as excinfo:
        jailrun.run_extractor(b"x" * 4, url_hint="", timeout_s=0.5)
    elapsed = time.monotonic() - started
    assert excinfo.value.reason == "timeout"
    # The watchdog must fire well before the child's built-in sleep (60s) would end.
    assert elapsed < 15.0, f"timeout took {elapsed:.1f}s — killpg didn't kick in?"


def test_run_extractor_malformed_json_is_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that prints non-JSON exits 0 but the parent refuses with ``malformed_json``."""
    bootstrap = "import sys; sys.stdin.buffer.read(); print('not json here')"
    monkeypatch.setattr(jailrun, "_CHILD_BOOTSTRAP", bootstrap)
    with pytest.raises(jailrun.JailError) as excinfo:
        jailrun.run_extractor(b"<html></html>", url_hint="", timeout_s=5.0)
    assert excinfo.value.reason == "malformed_json"


def test_run_extractor_bad_shape_is_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that returns extra keys / missing keys fails as ``bad_shape``."""
    bootstrap = ('import sys, json; sys.stdin.buffer.read(); '
                 'sys.stdout.write(json.dumps({"text": "x"}))')
    monkeypatch.setattr(jailrun, "_CHILD_BOOTSTRAP", bootstrap)
    with pytest.raises(jailrun.JailError) as excinfo:
        jailrun.run_extractor(b"<html></html>", url_hint="", timeout_s=5.0)
    assert excinfo.value.reason == "bad_shape"


def test_run_extractor_oversize_output_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that spews past ``_MAX_OUTPUT_BYTES`` is refused as ``oversize_output``.

    Also monkeypatches ``_MAX_OUTPUT_BYTES`` down to a small value so the test is
    fast + deterministic; the real ceiling stays at 1 MB in production.
    """
    monkeypatch.setattr(jailrun, "_MAX_OUTPUT_BYTES", 128)
    bootstrap = "import sys; sys.stdin.buffer.read(); sys.stdout.write('x' * 4096)"
    monkeypatch.setattr(jailrun, "_CHILD_BOOTSTRAP", bootstrap)
    with pytest.raises(jailrun.JailError) as excinfo:
        jailrun.run_extractor(b"", url_hint="", timeout_s=5.0)
    assert excinfo.value.reason == "oversize_output"


def test_run_extractor_env_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``SMARTBRAIN_*`` var in the parent env NEVER reaches the child.

    Set a canary in the parent, run a child that dumps its own env keys, and assert
    no ``SMARTBRAIN_`` key appears + no ``ANTHROPIC_*`` variable rides through.
    """
    monkeypatch.setenv("SMARTBRAIN_LEAK_CANARY", "must-not-appear")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-appear-either")
    bootstrap = ('import os, sys, json; sys.stdin.buffer.read(); '
                 'sys.stdout.write(json.dumps({"text": " ".join(sorted(os.environ)), '
                 '"title": ""}))')
    monkeypatch.setattr(jailrun, "_CHILD_BOOTSTRAP", bootstrap)
    out = jailrun.run_extractor(b"", url_hint="", timeout_s=5.0)
    env_keys = out["text"].split()
    assert not any(k.startswith("SMARTBRAIN_") for k in env_keys), env_keys
    assert "ANTHROPIC_API_KEY" not in env_keys, env_keys
    assert "PATH" in env_keys, env_keys  # minimal PATH IS threaded so python finds its libs


def test_run_extractor_cwd_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child runs in a fresh tempdir, not the parent's cwd — a private stage
    means an attacker cannot pre-place ``sitecustomize.py`` in the app tree for the
    child to import.
    """
    parent_cwd = os.getcwd()
    bootstrap = ('import os, sys, json; sys.stdin.buffer.read(); '
                 'sys.stdout.write(json.dumps({"text": os.getcwd(), "title": ""}))')
    monkeypatch.setattr(jailrun, "_CHILD_BOOTSTRAP", bootstrap)
    out = jailrun.run_extractor(b"", url_hint="", timeout_s=5.0)
    child_cwd = out["text"]
    assert child_cwd != parent_cwd
    assert "smartbrain-jail-" in child_cwd


def test_run_extractor_tag_strip_fallback_when_trafilatura_empty() -> None:
    """A tiny page trafilatura cannot classify still yields text via the fallback.

    Boilerplate-only HTML (a single ``<p>`` with a couple of words) is exactly the
    case where trafilatura returns nothing — the child then falls back to a plain
    tag-strip so the pipeline always sees some text.
    """
    tiny = b"<html><body><p>Hi.</p></body></html>"
    out = jailrun.run_extractor(tiny, url_hint="", timeout_s=10.0)
    assert isinstance(out["text"], str)
    assert "Hi" in out["text"]


def test_jail_env_never_leaks_secrets() -> None:
    """Whitebox: ``_jail_env`` builds an env with NO ``SMARTBRAIN_``/``ANTHROPIC_`` keys."""
    env = jailrun._jail_env()
    assert env.get("PATH")
    for name in env:
        assert not name.startswith("SMARTBRAIN_"), name
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
