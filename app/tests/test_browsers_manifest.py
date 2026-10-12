"""Browser component manifests (§35): the shipped pins, the strict loader, and the golden
argv/env the builder renders — including the proof that no URL or UA string can smuggle a
forbidden token into the engine's command line."""

from __future__ import annotations

import copy
import json

import pytest

from smartbrain_3000 import netguard
from smartbrain_3000.browsers import cmdline
from smartbrain_3000.browsers import manifest as mf
from tests import _browsers as hb

# The v0.2.4 release digests the lead verified, and the member digests computed from the
# same tarballs. A change here is a pin bump and must be reviewed as one.
SHIPPED = {
    ("obscura", "darwin-arm64"): ("obscura-aarch64-macos.tar.gz",
                                  "456210fe48e77324a064477fe450452a58709940a50e71678ae6638a3e5095ec", 66840143),
    ("obscura", "linux-x86_64"): ("obscura-x86_64-linux.tar.gz",
                                  "757e7b597ba5cdd53af9fc701e0a9b80f0a8d788f545d4f82ec9ad32db77f000", 71241984),
    ("obscura", "linux-aarch64"): ("obscura-aarch64-linux.tar.gz",
                                   "d9ba3719c2442a227567d7664b5505b38ab68236f3f5388884eee2e27f55db28", 69167573),
    ("obscura-stealth", "darwin-arm64"): ("obscura-aarch64-macos-stealth.tar.gz",
                                          "183ce21fc9caf421c4e027cd8043970224a24d9921d8bf27105153ca74f0e3ae", 69917203),
    ("obscura-stealth", "linux-x86_64"): ("obscura-x86_64-linux-stealth.tar.gz",
                                          "49b53f74a509764c42a35c8e73a37301399e1500d43564fad4cb641721efc753", 74629613),
    ("obscura-stealth", "linux-aarch64"): ("obscura-aarch64-linux-stealth.tar.gz",
                                           "56eacea66e4a5b0ab0f39343183c308816b426f604259b9cc41402488bf998f7", 72381172),
}
UA = "SmartBrain/1.2.3 (+https://smartbrain.securecloudgroup.com)"
GOLDEN_HTML = [
    "/data/browsers/obscura/0.2.4/obscura",
    "--v8-flags", "--max-old-space-size=512",
    "--proxy", "http://127.0.0.1:40123",
    "--storage-dir", "/tmp/run/storage",
    "fetch", "https://example.com/a?b=1",
    "--timeout", "20", "--wait-until", "load",
    "--dump", "html", "--output", "/tmp/run/out/page.html",
    "--user-agent", UA,
]
GOLDEN_ASSETS_NO_UA = [
    "/data/browsers/obscura/0.2.4/obscura",
    "--v8-flags", "--max-old-space-size=512",
    "--proxy", "http://127.0.0.1:40123",
    "--storage-dir", "/tmp/run/storage",
    "fetch", "https://example.com/a?b=1",
    "--timeout", "7", "--wait-until", "load",
    "--dump", "assets", "--output", "/tmp/run/out/assets.ndjson",
]


def _render(m: mf.Manifest, url: str = "https://example.com/a?b=1", ua: str | None = UA,
            **kw: object) -> list[str]:
    args = {"engine_path": "/data/browsers/obscura/0.2.4/obscura", "url": url,
            "run_dir": "/tmp/run", "proxy_port": 40123, "timeout_s": 20,
            "output_path": "/tmp/run/out/page.html", "user_agent": ua, "want_assets": False}
    args.update(kw)
    return cmdline.render_argv(m, **args)


def _forbidden_hits(m: mf.Manifest, argv: list[str]) -> list[str]:
    return [t for t in argv if t in m.forbidden or t.split("=", 1)[0] in m.forbidden]


def test_shipped_manifests_load_and_pin_every_platform() -> None:
    manifests = {m.name: m for m in mf.load_all()}
    assert set(manifests) == {"obscura", "obscura-stealth"}
    for (name, plat), (asset, sha, size) in SHIPPED.items():
        pinned = manifests[name].platforms[plat]
        assert (pinned.asset, pinned.sha256, pinned.size) == (asset, sha, size)
        assert [mname for mname, _, _ in pinned.members] == ["obscura"]
    for m in manifests.values():
        assert m.version == "0.2.4" and m.executable == "obscura"
        assert m.limits.html_cap_bytes == 8_000_000 <= netguard._MAX_BYTES
        assert m.limits.rlimit_data_mb == 0  # V8 reserves ~450 GB of address space: off
        assert (m.identity_mode, m.pool_size) == ("mimic", 8)  # 8 built-in profiles, 0-7
        assert {"--quiet", "-q", "--eval", "--allow-private-network"} <= m.forbidden
        assert mf.source_url(m, "linux-x86_64").startswith(
            "https://github.com/h4ckf0r0day/obscura/releases/download/v0.2.4/")
        assert {f.name: (f.install, oct(f.mode)) for f in m.files} == {
            "obscura": (True, "0o755"), "obscura-worker": (False, "0o644")}
    assert "stealth" not in manifests["obscura"].capabilities
    assert "stealth" in manifests["obscura-stealth"].capabilities
    assert mf.SELECTABLE == frozenset({"obscura"})  # the stealth tier is listed, not selectable


def test_golden_argv_honest_and_assets() -> None:
    m = mf.load("obscura")
    assert _render(m) == GOLDEN_HTML
    # --v8-flags is global-only: it must precede the subcommand or the heap is unbounded
    assert GOLDEN_HTML.index("--v8-flags") < GOLDEN_HTML.index("fetch")
    assert "--quiet" not in GOLDEN_HTML  # a heap/watchdog kill is only visible on stderr
    assets = _render(m, ua=None, timeout_s=7, output_path="/tmp/run/out/assets.ndjson",
                     want_assets=True)
    assert assets == GOLDEN_ASSETS_NO_UA
    stealth = _render(mf.load("obscura-stealth"))
    assert "--stealth" in stealth and "--stealth" not in GOLDEN_HTML
    assert stealth.index("--stealth") < stealth.index("fetch")


def test_golden_env_is_closed_and_inside_the_run_dir() -> None:
    m = mf.load("obscura")
    env = cmdline.render_env(m, run_dir="/tmp/run", timezone="America/New_York", profile=None)
    assert env == {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "HOME": "/tmp/run/home", "TMPDIR": "/tmp/run/tmp",
        "XDG_CACHE_HOME": "/tmp/run/home/.cache", "XDG_CONFIG_HOME": "/tmp/run/home/.config",
        "XDG_DATA_HOME": "/tmp/run/home/.local/share", "OBSCURA_TIMEZONE": "America/New_York",
        "OBSCURA_SCRIPT_DEADLINE_MS": "15000"}
    for profile in range(8):
        assert cmdline.render_env(m, run_dir="/tmp/run", timezone="UTC",
                             profile=profile)["OBSCURA_PROFILE"] == str(profile)
    for bad in (8, -1, 99):  # out of range silently means profile 0 in the engine: refuse
        with pytest.raises(ValueError):
            cmdline.render_env(m, run_dir="/tmp/run", timezone="UTC", profile=bad)
    for bad_env in ({**env, "OBSCURA_ALLOW_PRIVATE_NETWORK": "1"}, {**env, "HTTPS_PROXY": "x"},
                    {**env, "LD_PRELOAD": "/x.so"}, {**env, "HOME": "/tmp/a\nb"}):
        with pytest.raises(ValueError):
            cmdline.check_env(bad_env)


@pytest.mark.parametrize("url", [
    "https://example.com/ --allow-private-network", "--allow-private-network", "--eval",
    "https://example.com/\n--eval=1", "https://example.com/\t-e", " https://example.com/",
    "http://example.com/", "file:///etc/passwd", "data:text/html,<p>x</p>", "about:blank",
    "javascript:alert(1)", "https://127.0.0.1/", "https://[::1]/", "https://169.254.169.254/",
    "https://2130706433/", "https://user:pw@example.com/", "https://example.com:99999/",
    "https://example.com/\x00", "https://ex ample.com/", "https://" + "a" * 2050 + ".com/",
])
def test_hostile_urls_are_refused(url: str) -> None:
    m = mf.load("obscura")
    with pytest.raises(ValueError):
        _render(m, url=url)


@pytest.mark.parametrize("url", [
    "https://example.com/?x=--eval&y=--allow-private-network", "https://serve.example/mcp",
    "https://example.com/%20--host", "https://example.com/{run_dir}/{user_agent}",
    "https://example.com:8443/scrape?serve=1",
])
def test_accepted_urls_never_become_flags(url: str) -> None:
    m = mf.load("obscura")
    argv = _render(m, url=url)
    assert argv[argv.index("fetch") + 1] == url  # a URL is inserted verbatim, never re-scanned
    assert _forbidden_hits(m, argv) == []


@pytest.mark.parametrize("ua", [
    "--eval", "-e", "--allow-private-network", "--host=0.0.0.0", "serve", "mcp", "scrape",
    "UA\n--eval", "UA\x00", "Mozilla é", "x" * 201, "",
])
def test_hostile_user_agents_are_refused(ua: str) -> None:
    with pytest.raises(ValueError):
        _render(mf.load("obscura"), ua=ua)


def test_rendered_argv_recheck_catches_tampering() -> None:
    m = mf.load("obscura")
    good = _render(m)
    for bad in (good + ["--eval", "1"], good + ["--allow-private-network"],
                good + ["--stealth"], good[:1] + ["serve"] + good[1:],
                [t if t != "--max-old-space-size=512" else "--expose-gc" for t in good],
                good + ["-qs", "shot.png"], good + ["--port", "9222"]):
        with pytest.raises(ValueError):
            cmdline.check_argv(m, bad, "https://example.com/a?b=1")
    with pytest.raises(ValueError):  # the positional must be the validated URL
        cmdline.check_argv(m, good, "https://other.example/")


def _doc() -> dict:
    return copy.deepcopy(hb.manifest_doc(hb.fake_tarball()))


def _mutated(path: list, value: object, delete: bool = False) -> dict:
    doc = _doc()
    node = doc
    for key in path[:-1]:
        node = node[key]
    if delete:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return doc


def test_the_test_manifest_itself_is_valid() -> None:
    m = mf.parse(_doc())
    assert m.name == "obscura" and m.version == hb.FAKE_VERSION


@pytest.mark.parametrize(("path", "value"), [
    (["extra"], 1),
    (["limits", "extra"], 1),
    (["identity", "extra"], 1),
    (["argv", "screenshot"], ["--screenshot", "x"]),
])
def test_unknown_keys_are_refused(path: list, value: object) -> None:
    with pytest.raises(ValueError):
        mf.parse(_mutated(path, value))


def test_nested_unknown_keys_are_refused() -> None:
    doc = _doc()
    plat = next(iter(doc["platforms"]))
    doc["platforms"][plat]["mirror"] = "https://elsewhere.example/"
    with pytest.raises(ValueError):
        mf.parse(doc)
    doc = _doc()
    doc["files"][0]["setuid"] = True
    with pytest.raises(ValueError):
        mf.parse(doc)
    doc = _doc()
    doc["platforms"]["windows-x86_64"] = doc["platforms"][plat]
    with pytest.raises(ValueError):
        mf.parse(doc)


@pytest.mark.parametrize("token", [
    "--eval", "-e", "--allow-private-network", "--host", "--port", "-p", "--file", "--screenshot",
    "-s", "serve", "mcp", "scrape", "--obey-robots", "--selector", "--stealth", "-q",
    "--quiet", "{home}", "{url", "--wait=5",
])
def test_templates_only_use_allowlisted_flags_and_closed_placeholders(token: str) -> None:
    doc = _doc()
    doc["argv"]["fetch"] = doc["argv"]["fetch"] + [token]
    with pytest.raises(ValueError):
        mf.parse(doc)


@pytest.mark.parametrize("value", ["--expose-gc", "--max-old-space-size=99999",
                                   "--max-old-space-size={heap_mb} --expose-gc", "{heap_mb}"])
def test_v8_flags_only_carry_the_heap_cap(value: str) -> None:
    doc = _doc()
    fetch = doc["argv"]["fetch"]
    fetch[fetch.index("--v8-flags") + 1] = value
    with pytest.raises(ValueError):
        mf.parse(doc)


def test_required_template_flags_cannot_be_dropped() -> None:
    for flag in ("--proxy", "--output", "--timeout", "--storage-dir", "--v8-flags", "--dump"):
        doc = _doc()
        fetch = doc["argv"]["fetch"]
        i = fetch.index(flag)
        del fetch[i: i + 2]
        with pytest.raises(ValueError):
            mf.parse(doc)


def test_v8_flags_must_precede_the_subcommand() -> None:
    doc = _doc()
    fetch = doc["argv"]["fetch"]
    i = fetch.index("--v8-flags")
    moved = fetch[:i] + fetch[i + 2:] + fetch[i: i + 2]  # same tokens, after fetch
    doc["argv"]["fetch"] = moved
    with pytest.raises(ValueError):
        mf.parse(doc)
    good = _render(mf.load("obscura"))
    j = good.index("--v8-flags")
    with pytest.raises(ValueError):
        cmdline.check_argv(mf.load("obscura"), good[:j] + good[j + 2:] + good[j: j + 2],
                      "https://example.com/a?b=1")


def test_stealth_flag_requires_the_capability_both_ways() -> None:
    doc = _doc()
    doc["argv"]["fetch"] = ["--stealth"] + doc["argv"]["fetch"]
    with pytest.raises(ValueError):  # a non-stealth engine never emits --stealth
        mf.parse(doc)
    stealth = hb.manifest_doc(hb.fake_tarball(), name="obscura-stealth")
    assert "--stealth" in mf.parse(stealth).argv["fetch"]
    stealth["argv"]["fetch"].remove("--stealth")
    with pytest.raises(ValueError):  # a stealth engine must declare its flag
        mf.parse(stealth)
    stealth = hb.manifest_doc(hb.fake_tarball(), name="obscura-stealth")
    stealth["forbidden_flags"].append("--stealth")
    with pytest.raises(ValueError):
        mf.parse(stealth)


def test_html_cap_cannot_exceed_netguard() -> None:
    with pytest.raises(ValueError):
        mf.parse(_mutated(["limits", "html_cap_bytes"], netguard._MAX_BYTES + 1))
    assert mf.parse(_mutated(["limits", "html_cap_bytes"], netguard._MAX_BYTES))


@pytest.mark.parametrize(("path", "value"), [
    (["identity", "pool_size"], -1),
    (["identity", "pool_size"], 9),
    (["identity", "pool_size"], 0),           # mimic (the default mode) needs a profile pool
    (["limits", "rlimit_data_mb"], 100),
    (["identity", "mode"], "stealthy"),
    (["source_url_template"], "https://evil.example/{asset}"),
    (["source_url_template"], "https://github.com/x/{asset}/{name}"),
    (["source_url_template"], "http://github.com/x/{asset}"),
    (["min_glibc"], "two"),
    (["version"], "../0.2.4"),
    (["released"], "yesterday"),
    (["executable"], "obscura-worker"),
    (["capabilities"], ["js"]),
    (["capabilities"], ["render", "render"]),
    (["limits", "timeout_s"], 0),
    (["limits", "script_deadline_ms"], 999_999),
])
def test_bad_values_are_refused(path: list, value: object) -> None:
    with pytest.raises(ValueError):
        mf.parse(_mutated(path, value))


@pytest.mark.parametrize(("index", "field", "value"), [
    (0, "mode", "4755"), (0, "mode", "0777"), (0, "mode", "0644"), (0, "name", "bin/obscura"),
    (0, "name", "../obscura"), (1, "install", "no"),
])
def test_file_specs_are_strict(index: int, field: str, value: object) -> None:
    doc = _doc()
    doc["files"][index][field] = value
    with pytest.raises(ValueError):
        mf.parse(doc)


def test_members_must_pin_exactly_the_installed_files() -> None:
    doc = _doc()
    plat = next(iter(doc["platforms"]))
    doc["platforms"][plat]["members"]["obscura-worker"] = {"sha256": "0" * 64, "size": 1}
    with pytest.raises(ValueError):
        mf.parse(doc)


def test_load_refuses_bad_names_and_mismatches(tmp_path, monkeypatch) -> None:
    for name in ("../obscura", "Obscura", "", "a" * 40):
        with pytest.raises(ValueError):
            mf.load(name)
    doc = hb.manifest_doc(hb.fake_tarball())
    (tmp_path / "other.json").write_text(json.dumps(doc))
    monkeypatch.setattr(mf, "ENGINES_DIR", tmp_path)
    with pytest.raises(ValueError):  # the file name and the manifest name must agree
        mf.load("other")


def test_urls_are_normalised_to_idna_https() -> None:
    assert cmdline.validate_url("https://Bücher.Example/x?y=1") == (
        "https://xn--bcher-kva.example/x?y=1", "xn--bcher-kva.example", 443)
    assert cmdline.validate_url("HTTPS://EXAMPLE.com:443/a")[0] == "https://example.com/a"
    assert cmdline.validate_url("https://example.com:8443/a")[1:] == ("example.com", 8443)
    with pytest.raises(ValueError):  # render_argv takes only the normalised form
        _render(mf.load("obscura"), url="https://EXAMPLE.com/a")


def test_card_profiles_are_stable_and_cover_the_pool() -> None:
    picks = {cmdline.card_profile(f"card-{i}", 8) for i in range(200)}
    assert picks == set(range(8))
    assert cmdline.card_profile("card-7", 8) == cmdline.card_profile("card-7", 8)
    assert 0 <= cmdline.card_profile("x", 1) == 0


def test_rlimit_data_is_opt_in() -> None:
    assert mf.parse(_mutated(["limits", "rlimit_data_mb"], 0)).limits.rlimit_data_mb == 0
    assert mf.parse(_mutated(["limits", "rlimit_data_mb"], 2048)).limits.rlimit_data_mb == 2048


def test_timezones_are_validated() -> None:
    assert cmdline.validate_timezone("UTC") == "UTC"
    assert cmdline.validate_timezone("America/Argentina/Buenos_Aires")
    for bad in ("../etc/passwd", "Mars/Olympus_Mons", "", "A" * 80, "UTC\n", 5):
        with pytest.raises(ValueError):
            cmdline.validate_timezone(bad)
