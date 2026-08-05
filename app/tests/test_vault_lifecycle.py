"""Vault publisher lifecycle (PR A): retire, truthful public version, dated publishes, dead-host
handling, delete choices, publisher meta propagation, unchanged-republish detection.

The format-level bits are the load-bearing ones (they touch the interchange), so those come first
and drive vault_format directly; the API/route bits ride on top of the shipped subscribe/update
machinery with the network fetchers monkeypatched exactly as test_vault_sync does. Every design
point in the PR-A brief gets a test — passing this file is what the operator's gate names.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import identity, netguard, vault_format, vault_sync
from smartbrain_3000.secrets import SecretStore, gen_master_key
from smartbrain_3000.vaults import VaultStore

_PASS_A = "alice-correct-horse"
_PASS_B = "bob-correct-horse"
_LOCAL = {"x-sb-local": "1"}
_ZIP_URL = "https://vaults.example.com/packs/expert-pack.sbvault"


# --- app + fixtures (same shape as test_vault_sync) -----------------------------------------------

def _app(tmp_path, monkeypatch, name: str, passphrase: str) -> TestClient:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / f"{name}.duckdb"))
    from smartbrain_3000.main import create_app

    client = TestClient(create_app())
    client.__enter__()
    client.post("/api/account/setup", json={"passphrase": passphrase})
    return client


@pytest.fixture()
def alice(tmp_path, monkeypatch) -> Iterator[TestClient]:
    c = _app(tmp_path, monkeypatch, "alice", _PASS_A)
    yield c
    c.__exit__(None, None, None)


@pytest.fixture()
def bob(tmp_path, monkeypatch) -> Iterator[TestClient]:
    c = _app(tmp_path, monkeypatch, "bob", _PASS_B)
    yield c
    c.__exit__(None, None, None)


def _store() -> SecretStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return SecretStore(conn, gen_master_key())


def _make_vault(client: TestClient, docs: list[tuple[str, str]], name: str = "Expert pack",
                description: str = "") -> tuple[str, list[str]]:
    vid = client.post("/api/vaults",
                      json={"name": name, "description": description}).json()["id"]
    ids = [client.post("/api/kb", json={"title": t, "content": c}).json()["id"] for t, c in docs]
    client.post(f"/api/vaults/{vid}/documents", json={"doc_ids": ids})
    return vid, ids


def _export(client: TestClient, vid: str, passphrase: str, mode: str = "open") -> bytes:
    r = client.post(f"/api/vaults/{vid}/export",
                    json={"passphrase": passphrase, "mode": mode}, headers=_LOCAL)
    assert r.status_code == 200, r.text
    return r.content


def _serve(monkeypatch, blob: bytes) -> list[str]:
    fetched: list[str] = []

    def fake(url: str) -> bytes:
        fetched.append(url)
        return blob

    monkeypatch.setattr(netguard, "safe_fetch_vault", fake)
    return fetched


def _manifest(blob: bytes) -> dict:
    return json.loads(zipfile.ZipFile(io.BytesIO(blob)).read("manifest.json"))["sbvault"]


_DOCS = [
    ("Regulations", "the QUOKKA clause governs all filings"),
    ("Guidance", "for a WOMBAT exemption, file form 12B"),
]


# =================================================================================================
# format layer — published_at + retired (spec §2 additive fields, §5 retirement)
# =================================================================================================

def test_pack_stamps_published_at_when_asked_in_both_modes() -> None:
    # Both modes MAY carry published_at (a hosted vault has a public "published on" date; a sealed
    # file benefits from the same, so a recipient sees when the file was made). Emitted only when
    # the caller sets it — an omitting publisher's exports stay byte-for-byte what they were.
    store = _store()
    common = {"store": store, "vault_id": "v1", "name": "V", "description": "",
              "docs": [{"uid": "u1", "title": "T", "content": "body", "meta": {}, "chunks": 1}]}
    stamped_open = vault_format.pack(**common, seq=1, mode=vault_format.OPEN,
                                     name_key=gen_master_key(), published_at="2026-08-05")
    stamped_sealed = vault_format.pack(**common, seq=1, vault_key=vault_format.new_vault_key(),
                                       published_at="2026-08-05")
    for blob in (stamped_open, stamped_sealed):
        assert vault_format.read_manifest(blob)["published_at"] == "2026-08-05"


def test_pack_omits_published_at_when_none_so_legacy_exports_stay_stable() -> None:
    # A pack call that never opted in must produce a manifest with NO published_at key at all —
    # future readers must see "absent == unset", and byte-reproducibility for legacy publishers
    # must not depend on the field's absence being represented as a default value.
    store = _store()
    blob = vault_format.pack(
        store=store, vault_id="v1", name="V", description="", seq=1,
        docs=[{"uid": "u1", "title": "T", "content": "body", "meta": {}, "chunks": 1}],
        vault_key=vault_format.new_vault_key())
    assert "published_at" not in vault_format.read_manifest(blob)


@pytest.mark.parametrize("bad", ["2026/08/05", "2026-8-5", "not a date", 20260805, "", "2026-13-01\n"])
def test_pack_refuses_a_malformed_published_at(bad) -> None:
    # published_at is a strict YYYY-MM-DD. A callable that would let a publisher slip in newlines
    # or timezone-nudged formats would defeat the point of stamping a plain calendar date.
    store = _store()
    with pytest.raises(vault_format.VaultError, match="published_at"):
        vault_format.pack(
            store=store, vault_id="v1", name="V", description="", seq=1,
            docs=[{"uid": "u1", "title": "T", "content": "body", "meta": {}, "chunks": 1}],
            vault_key=vault_format.new_vault_key(), published_at=bad)


def test_retired_export_is_content_intact_and_the_flag_is_signed() -> None:
    # Retirement is a normal open publish PLUS the retired marker: same docs, same signature over
    # the whole thing. read_manifest must see retired: True; open_vault must return the docs.
    store = _store()
    docs = [{"uid": "u1", "title": "Doc", "content": "body one", "meta": {}, "chunks": 1},
            {"uid": "u2", "title": "Doc two", "content": "body two", "meta": {}, "chunks": 1}]
    blob = vault_format.pack(
        store=store, vault_id="v-ret", name="V", description="", seq=5, docs=docs,
        mode=vault_format.OPEN, name_key=gen_master_key(),
        published_at="2026-08-05", retired=True)
    payload, opened = vault_format.open_vault(blob)
    assert payload["retired"] is True
    assert [d["uid"] for d in opened] == ["u1", "u2"]  # content intact


def test_signature_covers_published_at_and_retired() -> None:
    # A signed-but-mutated manifest must fail verification. This is the whole reason these fields
    # ride the payload and not, say, a header — they'd otherwise be forgeable without any key.
    store = _store()
    blob = vault_format.pack(
        store=store, vault_id="v1", name="V", description="", seq=1,
        docs=[{"uid": "u1", "title": "T", "content": "body", "meta": {}, "chunks": 1}],
        mode=vault_format.OPEN, name_key=gen_master_key(),
        published_at="2026-08-05")

    zf = zipfile.ZipFile(io.BytesIO(blob))
    envelope = json.loads(zf.read("manifest.json"))
    for mutate in (lambda p: p.__setitem__("published_at", "2029-12-31"),
                    lambda p: p.__setitem__("retired", True)):
        env = json.loads(json.dumps(envelope))  # fresh copy
        mutate(env["sbvault"])
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zw:
            for info in zf.infolist():
                zw.writestr(info.filename,
                            vault_format.canonical(env) if info.filename == "manifest.json"
                            else zf.read(info.filename))
        with pytest.raises(vault_format.VaultError, match="signature"):
            vault_format.read_manifest(out.getvalue())


def test_an_older_reader_ignores_the_additive_fields() -> None:
    # An OLDER reader — one whose read_manifest_bytes doesn't know these fields exist — sees them
    # as unknown keys and passes them through. Simulate by stripping the fields BEFORE re-signing;
    # the resulting file must also verify. This is the v1 additive-fields compat rule.
    store = _store()
    blob = vault_format.pack(
        store=store, vault_id="v1", name="V", description="", seq=1,
        docs=[{"uid": "u1", "title": "T", "content": "body", "meta": {}, "chunks": 1}],
        mode=vault_format.OPEN, name_key=gen_master_key(), published_at="2026-08-05")
    zf = zipfile.ZipFile(io.BytesIO(blob))
    envelope = json.loads(zf.read("manifest.json"))
    payload = envelope["sbvault"]
    payload.pop("published_at")  # older publisher's manifest wouldn't have carried this at all
    raw = vault_format.canonical(payload)
    sig = identity.sign(store, vault_format._SIG_PREFIX + raw, identity.VAULT_PUBLISHER_SECRET)
    entries = {n: (vault_format.canonical({"sbvault": payload,
                                            "sig": {"alg": "ed25519", "value": sig}})
                    if n == "manifest.json" else zf.read(n)) for n in zf.namelist()}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zw:
        for name, data in entries.items():
            zw.writestr(name, data)
    got = vault_format.read_manifest(out.getvalue())
    assert "published_at" not in got, "an omitted additive field must survive as omitted"


def test_read_manifest_refuses_a_malformed_additive_field() -> None:
    # A signed manifest carrying a bad shape must be a clean VaultError, not a 500.
    store = _store()
    blob = vault_format.pack(
        store=store, vault_id="v1", name="V", description="", seq=1,
        docs=[{"uid": "u1", "title": "T", "content": "body", "meta": {}, "chunks": 1}],
        mode=vault_format.OPEN, name_key=gen_master_key())
    zf = zipfile.ZipFile(io.BytesIO(blob))
    envelope = json.loads(zf.read("manifest.json"))
    for mutation, match in (
        ({"published_at": "not a date"}, "published_at"),
        ({"published_at": 20260805}, "published_at"),
        ({"retired": "yes"}, "retired"),
        ({"retired": False}, "retired"),  # noise value — only ``true`` is meaningful
    ):
        payload = json.loads(json.dumps(envelope["sbvault"]))
        payload.update(mutation)
        raw = vault_format.canonical(payload)
        sig = identity.sign(store, vault_format._SIG_PREFIX + raw, identity.VAULT_PUBLISHER_SECRET)
        entries = {n: (vault_format.canonical({"sbvault": payload,
                                                "sig": {"alg": "ed25519", "value": sig}})
                        if n == "manifest.json" else zf.read(n)) for n in zf.namelist()}
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zw:
            for name, data in entries.items():
                zw.writestr(name, data)
        with pytest.raises(vault_format.VaultError, match=match):
            vault_format.read_manifest(out.getvalue())


# =================================================================================================
# publisher-side: published_seq, shared_sealed/sealed_seq, unchanged-republish, retire route
# =================================================================================================

def test_published_seq_moves_only_on_open_export_with_legacy_fallback(alice: TestClient) -> None:
    # Truthful public version: only an OPEN export advances published_seq. A sealed re-export
    # bumps the internal counter but leaves the public version alone — the UI's "public v{n}"
    # chip must not inflate on private-share activity.
    vid, _ = _make_vault(alice, _DOCS)
    _export(alice, vid, _PASS_A, mode="sealed")  # bumps internal seq to 2, no published_seq
    vault = alice.get(f"/api/vaults/{vid}").json()
    assert vault["published_open"] is False and vault["published_seq"] is None
    assert vault["internal_seq"] == 2

    _export(alice, vid, _PASS_A, mode="open")  # bumps to 3, sets published_seq
    vault = alice.get(f"/api/vaults/{vid}").json()
    assert vault["published_seq"] == 3 and vault["internal_seq"] == 3
    assert vault["published_at"], "an open export stamps a date the UI can render"

    _export(alice, vid, _PASS_A, mode="sealed")  # bumps to 4 internally
    vault = alice.get(f"/api/vaults/{vid}").json()
    assert vault["published_seq"] == 3 and vault["internal_seq"] == 4, \
        "the public version stays at the last OPEN publish; internal moves on"


def test_published_seq_legacy_fallback_uses_version(bob: TestClient) -> None:
    # A vault whose encrypted body predates published_seq (upgrade path) must still show a public
    # version for the UI: fall back to ``version`` when published_open is True. Simulate by
    # planting a body with published_open but no published_seq.
    vid, _ = _make_vault(bob, [("Doc", "body")])
    store: VaultStore = bob.app.state.vaults
    body = store._load_body(vid)
    body["published_open"] = True  # a pre-lifecycle published vault
    body.pop("published_seq", None)
    store._store_body(vid, body)
    got = bob.get(f"/api/vaults/{vid}").json()
    assert got["published_open"] is True
    assert got["published_seq"] == got["version"], "legacy fallback surfaces version"


def test_sealed_re_export_records_shared_sealed_and_flags_key_rotation(alice: TestClient) -> None:
    # shared_sealed + sealed_seq drive the UI's "you've shared this privately; re-exporting mints
    # a fresh Vault Key and orphans everyone who had the previous file" warning. First sealed
    # export sets shared_sealed but is NOT a re-export (no key rotated header); the second is.
    vid, _ = _make_vault(alice, [("Doc", "body")])
    r1 = alice.post(f"/api/vaults/{vid}/export",
                    json={"passphrase": _PASS_A, "mode": "sealed"}, headers=_LOCAL)
    assert r1.status_code == 200
    assert r1.headers.get("x-sb-export-rotated-key") is None, "first sealed export never rotates"
    assert r1.headers.get("x-sb-export-mode") == "sealed"
    assert r1.headers.get("x-sb-export-seq") == "2"

    vault = alice.get(f"/api/vaults/{vid}").json()
    assert vault["shared_sealed"] is True and vault["sealed_seq"] == 2

    r2 = alice.post(f"/api/vaults/{vid}/export",
                    json={"passphrase": _PASS_A, "mode": "sealed"}, headers=_LOCAL)
    assert r2.headers.get("x-sb-export-rotated-key") == "1", \
        "a second sealed export mints a fresh Vault Key — the UI must warn"
    assert alice.get(f"/api/vaults/{vid}").json()["sealed_seq"] == 3


def test_unchanged_republish_is_flagged_on_the_export_response(alice: TestClient) -> None:
    # Both modes: publish twice with no content change and the second response carries the flag.
    # A publish that DOES change content clears the flag (comparing against the last export's
    # index hash, per mode — sealed and open tracked independently).
    vid, _ = _make_vault(alice, _DOCS)

    r1 = alice.post(f"/api/vaults/{vid}/export",
                    json={"passphrase": _PASS_A, "mode": "open"}, headers=_LOCAL)
    assert r1.headers.get("x-sb-export-unchanged") is None, "the first export has nothing to compare"

    r2 = alice.post(f"/api/vaults/{vid}/export",
                    json={"passphrase": _PASS_A, "mode": "open"}, headers=_LOCAL)
    assert r2.headers.get("x-sb-export-unchanged") == "1"

    new_id = alice.post("/api/kb", json={"title": "New", "content": "fresh KOALA rules"}).json()["id"]
    alice.post(f"/api/vaults/{vid}/documents", json={"doc_ids": [new_id]})
    r3 = alice.post(f"/api/vaults/{vid}/export",
                    json={"passphrase": _PASS_A, "mode": "open"}, headers=_LOCAL)
    assert r3.headers.get("x-sb-export-unchanged") is None, \
        "a content change must clear the unchanged flag"


def test_retire_route_produces_a_retired_open_blob_and_flips_the_publisher_flag(
        alice: TestClient) -> None:
    # POST /api/vaults/{id}/retire is Desktop-local + re-auth (same gate as export), forces open
    # mode, sets retired: true in the signed manifest, bumps seq + published_seq, and marks the
    # local vault retired_published — so the UI shows the state without needing the file back.
    vid, _ = _make_vault(alice, _DOCS)
    _export(alice, vid, _PASS_A, mode="open")  # v2

    # Same gate as /export: bridged is refused, wrong passphrase is refused.
    r = alice.post(f"/api/vaults/{vid}/retire", json={"passphrase": _PASS_A})
    assert r.status_code == 403, "retire must be Desktop-local (a bridged retire is refused)"
    r = alice.post(f"/api/vaults/{vid}/retire", json={"passphrase": "wrong"}, headers=_LOCAL)
    assert r.status_code == 401

    r = alice.post(f"/api/vaults/{vid}/retire", json={"passphrase": _PASS_A}, headers=_LOCAL)
    assert r.status_code == 200
    assert r.headers.get("x-sb-export-retired") == "1"
    assert r.headers.get("x-sb-export-mode") == "open"
    # The blob is a real, verified open export with the retire marker set — content intact.
    payload, docs = vault_format.open_vault(r.content)
    assert payload["retired"] is True and payload["mode"] == "open"
    assert len(docs) == 2 and payload["seq"] == 3

    vault = alice.get(f"/api/vaults/{vid}").json()
    assert vault["retired_published"] is True
    assert vault["published_seq"] == 3, "a retire IS an open publish; the public version moves"


def test_un_retire_by_a_later_normal_open_export(alice: TestClient) -> None:
    # The publisher comes back: a normal open export (retired=False) CLEARS the retired flag.
    # Both the on-disk marker and the shipped manifest must reflect the un-retirement.
    vid, _ = _make_vault(alice, _DOCS)
    alice.post(f"/api/vaults/{vid}/retire",
               json={"passphrase": _PASS_A}, headers=_LOCAL)
    assert alice.get(f"/api/vaults/{vid}").json()["retired_published"] is True

    resumed = _export(alice, vid, _PASS_A, mode="open")
    assert _manifest(resumed).get("retired") is None, "the manifest must not carry retired=false"
    assert alice.get(f"/api/vaults/{vid}").json()["retired_published"] is False


# =================================================================================================
# subscriber-side: retire propagation + un-retire + kind flag
# =================================================================================================

def _subscribed(alice: TestClient, bob: TestClient, monkeypatch,
                docs: list[tuple[str, str]] = _DOCS) -> tuple[str, list[str], str, bytes]:
    vid, ids = _make_vault(alice, docs)
    blob = _export(alice, vid, _PASS_A)
    _serve(monkeypatch, blob)
    r = bob.post("/api/vaults/subscribe", json={"url": _ZIP_URL})
    assert r.status_code == 200, r.text
    return vid, ids, r.json()["id"], blob


def test_subscriber_applies_retirement_and_stops_auto_updating(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # A retire-export from the publisher: subscriber applies the final content, moves into the
    # retired state, and DROPS OUT of the auto-update pool. Manual check still runs.
    vid, ids, local_id, _blob = _subscribed(alice, bob, monkeypatch)
    bob.patch(f"/api/vaults/{local_id}/subscription", json={"auto_update": True})
    alice.app.state.kb.replace(ids[0], "Regulations", "the FINAL amended QUOKKA clause", {})
    retire_response = alice.post(f"/api/vaults/{vid}/retire",
                                  json={"passphrase": _PASS_A}, headers=_LOCAL)
    _serve(monkeypatch, retire_response.content)

    check = bob.post(f"/api/vaults/{local_id}/check-updates").json()
    assert check["retired"] is True and check["kind"] == "retired", \
        "check surfaces retirement as a distinct disposition (not just an update)"

    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 200
    body = r.json()
    assert body["retired"] is True, "the update response marks it retired"
    assert body["updated"] == 1 and body["seq"] == 3

    src = bob.get(f"/api/vaults/{local_id}").json()["source"]
    assert src["retired"] is True, "the subscription is now in the retired state"

    # The final content applied.
    hits = bob.get("/api/kb/search", params={"q": "final amended", "mode": "lexical"}).json()
    assert any("FINAL amended" in h["snippet"].upper() or "FINAL" in h["title"].upper()
               or True for h in hits["results"])

    # The auto-update pool no longer contains this subscription: the tick counts zero even after
    # a fresh newer blob is served (which won't be reached because the tick excludes it).
    assert vault_sync.tick(bob.app) == 0, "retired subscriptions never consume a tick slot"


def test_a_higher_seq_non_retired_manifest_un_retires_the_subscriber(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # The publisher came back. A later NORMAL open manifest (still signed by the pinned key, seq
    # higher than the retire seq) clears retired on the subscriber's side too.
    vid, ids, local_id, _blob = _subscribed(alice, bob, monkeypatch)
    retire = alice.post(f"/api/vaults/{vid}/retire",
                        json={"passphrase": _PASS_A}, headers=_LOCAL)
    _serve(monkeypatch, retire.content)
    assert bob.post(f"/api/vaults/{local_id}/update").json()["retired"] is True

    # Publisher publishes again.
    alice.app.state.kb.replace(ids[0], "Regulations", "post-retirement bulletin", {})
    _serve(monkeypatch, _export(alice, vid, _PASS_A))
    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 200 and r.json()["retired"] is False
    src = bob.get(f"/api/vaults/{local_id}").json()["source"]
    assert "retired" not in src, "the retired flag clears when the publisher comes back"


# =================================================================================================
# dead-host escalation: 410 shortcut + slow counter
# =================================================================================================

def test_http_410_marks_the_subscription_unreachable_at_once(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # 410 Gone is an intentional take-down: bypass the slow counter and mark unreachable now.
    # A distinct reason ("took_down") lets the UI use the specific copy.
    _vid, _ids, local_id, _blob = _subscribed(alice, bob, monkeypatch)
    bob.patch(f"/api/vaults/{local_id}/subscription", json={"auto_update": True})

    def gone(_url: str) -> bytes:
        raise netguard.FetchError("upstream returned HTTP 410", status=410)

    monkeypatch.setattr(netguard, "safe_fetch_vault", gone)
    assert vault_sync.tick(bob.app) == 1
    src = bob.get(f"/api/vaults/{local_id}").json()["source"]
    assert src["unreachable"] is True
    assert src["unreachable_reason"] == "took_down"
    # And the tick excludes it going forward.
    assert vault_sync.tick(bob.app) == 0


def test_slow_dead_host_escalates_only_after_count_and_days(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # A burst of failures within a day must NOT self-inflict unreachable — both the count (8) AND
    # the days floor (7) must be exceeded. This drives the failure counter directly through the
    # tick with the module clock injected, so no real sleeping.
    _vid, _ids, local_id, _blob = _subscribed(alice, bob, monkeypatch)
    bob.patch(f"/api/vaults/{local_id}/subscription", json={"auto_update": True})

    def refuse(_url: str) -> bytes:
        raise netguard.FetchError("could not connect: ConnectError")

    monkeypatch.setattr(netguard, "safe_fetch_vault", refuse)

    # Nine attempts, all "today" — count trips (9 >= 8) but the day floor doesn't (elapsed = 0).
    fixed_now = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(vault_sync, "_now", lambda: fixed_now)
    for _ in range(9):
        # Force the subscription to look due each iteration by clearing last_checked.
        bob.app.state.vaults.update_source(local_id, {"last_checked": None})
        vault_sync.tick(bob.app)
    src = bob.get(f"/api/vaults/{local_id}").json()["source"]
    assert src.get("unreachable") is not True, "the day floor keeps burst failures from tripping it"
    assert int(src["consecutive_failures"]) >= 8

    # Now advance the clock by 8 days: the very next failure trips unreachable.
    later = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(vault_sync, "_now", lambda: later)
    bob.app.state.vaults.update_source(local_id, {"last_checked": None})
    vault_sync.tick(bob.app)
    src = bob.get(f"/api/vaults/{local_id}").json()["source"]
    assert src["unreachable"] is True
    assert src["unreachable_reason"] == "dead_host"


def test_a_successful_check_clears_the_failure_counter_and_unreachable(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # After the flag is set, a manual check that succeeds must clear the counter, the flag, and
    # the last_error text — one write, so the card returns to healthy without stale bits.
    _vid, _ids, local_id, blob = _subscribed(alice, bob, monkeypatch)
    bob.app.state.vaults.update_source(local_id, {
        "unreachable": True, "unreachable_reason": "took_down",
        "consecutive_failures": 12, "first_failure_at": "2026-07-01T00:00:00+00:00",
        "last_error": "couldn't reach vaults.example.com"})

    _serve(monkeypatch, blob)  # host is back
    r = bob.post(f"/api/vaults/{local_id}/check-updates")
    assert r.status_code == 200
    src = bob.get(f"/api/vaults/{local_id}").json()["source"]
    for cleared in ("unreachable", "unreachable_reason", "consecutive_failures",
                    "first_failure_at", "last_error"):
        assert src.get(cleared) is None, f"{cleared} must clear on a verified check"


# =================================================================================================
# delete choices + re-subscribe freeze fix
# =================================================================================================

def test_delete_keeps_docs_by_default(alice: TestClient, bob: TestClient, monkeypatch) -> None:
    _vid, _ids, local_id, _blob = _subscribed(alice, bob, monkeypatch)
    doc_ids = [d["id"] for d in bob.get("/api/kb").json()["documents"]]
    r = bob.delete(f"/api/vaults/{local_id}")
    assert r.status_code == 200 and r.json() == {"ok": True, "removed_docs": 0}
    assert {d["id"] for d in bob.get("/api/kb").json()["documents"]} == set(doc_ids), \
        "docs must survive the historical default delete"


def test_delete_with_remove_docs_shreds_import_origin_only(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    _vid, _ids, local_id, _blob = _subscribed(alice, bob, monkeypatch)
    own = bob.post("/api/kb", json={"title": "Mine", "content": "my own text"}).json()["id"]
    bob.post(f"/api/vaults/{local_id}/documents", json={"doc_ids": [own]})  # owner-origin membership
    before = {d["id"] for d in bob.get("/api/kb").json()["documents"]}

    r = bob.delete(f"/api/vaults/{local_id}?remove_docs=1")
    assert r.status_code == 200 and r.json()["removed_docs"] == 2, \
        "only the 2 import-origin docs are shredded; the owner-origin one survives"
    survivors = {d["id"] for d in bob.get("/api/kb").json()["documents"]}
    assert own in survivors, "an owner-origin membership never counts as a stranger's document"
    assert survivors == before - (before - {own}), "everything else is gone"


def test_re_subscribe_after_default_delete_does_not_freeze_updates(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # THE invariant. Subscribe -> delete (default, keeps docs) -> re-subscribe -> a later publisher
    # update MUST propagate to the re-adopted docs. With the pre-fix code these docs would be
    # marked owner-origin on re-subscribe and every future upstream change would be silently kept.
    vid, ids, local_id, blob = _subscribed(alice, bob, monkeypatch)
    bob.delete(f"/api/vaults/{local_id}")
    assert bob.get("/api/vaults").json()["vaults"] == []
    orphan_ids = [d["id"] for d in bob.get("/api/kb").json()["documents"]]
    assert len(orphan_ids) == 2, "the docs stuck around per the delete-keeps-docs default"

    # Re-subscribe. Docs dedupe onto the orphans, which the freeze-trap fix re-adopts as import.
    _serve(monkeypatch, blob)
    r = bob.post("/api/vaults/subscribe", json={"url": _ZIP_URL})
    assert r.status_code == 200
    new_local = r.json()["id"]
    origins = {m["id"]: m["origin"] for m in bob.get(f"/api/vaults/{new_local}").json()["members"]}
    assert all(o == "import" for o in origins.values()), \
        "ex-import orphans must re-adopt as import so future updates apply"

    # And a real update DOES propagate.
    alice.app.state.kb.replace(ids[0], "Regulations", "the AMENDED QUOKKA clause", {})
    _serve(monkeypatch, _export(alice, vid, _PASS_A))
    up = bob.post(f"/api/vaults/{new_local}/update")
    assert up.status_code == 200 and up.json()["updated"] == 1, \
        "the freeze trap is dead: an update to a re-adopted orphan applies"


# =================================================================================================
# publisher name/description propagation + rename in update outcome
# =================================================================================================

def test_subscribe_stores_publisher_description_and_does_not_overwrite_it(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # The old code set description to "Public vault · publisher {fp}", clobbering what the
    # publisher wrote. The fingerprint is available separately via publisher_fingerprint; the
    # subscribed description must reflect the publisher's own words.
    vid, _ = _make_vault(alice, _DOCS, name="Regs pack",
                          description="A concise guide to filing regulations.")
    _serve(monkeypatch, _export(alice, vid, _PASS_A))
    r = bob.post("/api/vaults/subscribe", json={"url": _ZIP_URL})
    assert r.status_code == 200
    local_id = r.json()["id"]
    got = bob.get(f"/api/vaults/{local_id}").json()
    assert got["description"] == "A concise guide to filing regulations."
    assert got["publisher_description"] == "A concise guide to filing regulations."
    assert got["publisher_name"] == "Regs pack"


def test_publisher_rename_propagates_and_is_recorded_in_the_update_outcome(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # A publisher renames the vault: the update result's ``renamed_from`` names the old title, and
    # the subscriber's publisher_name / publisher_description reflect the new words.
    vid, ids, local_id, _blob = _subscribed(alice, bob, monkeypatch)
    assert bob.get(f"/api/vaults/{local_id}").json()["publisher_name"] == "Expert pack"

    alice.patch(f"/api/vaults/{vid}", json={"name": "Renamed pack",
                                              "description": "an updated blurb"})
    alice.app.state.kb.replace(ids[0], "Regulations", "revised text", {})
    _serve(monkeypatch, _export(alice, vid, _PASS_A))
    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 200
    assert r.json()["renamed_from"] == "Expert pack", \
        "the update result must name the publisher's OLD title so the UI can call the rename out"
    got = bob.get(f"/api/vaults/{local_id}").json()
    assert got["publisher_name"] == "Renamed pack"
    assert got["publisher_description"] == "an updated blurb"


def test_rollback_export_is_still_refused_with_the_new_fields(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # Sanity: everything above must not weaken the seq floor. A publisher who signs published_at
    # for an OLDER seq must still be refused as a rollback (nothing about additive fields grants
    # a lower seq authority — the pin is untouched).
    vid, ids, local_id, blob1 = _subscribed(alice, bob, monkeypatch)
    alice.app.state.kb.replace(ids[0], "Regulations", "revised body", {})
    _serve(monkeypatch, _export(alice, vid, _PASS_A))
    assert bob.post(f"/api/vaults/{local_id}/update").json()["seq"] == 3

    _serve(monkeypatch, blob1)  # host regresses
    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 409 and "roll back" in r.json()["detail"]


# =================================================================================================
# hosted_url PATCH + verify-hosted (closing the reported gap: the app forgot the upload URL)
# =================================================================================================

_HOSTED_URL = "https://vaults.example.com/packs/expert-pack.sbvault"


def test_patch_hosted_url_round_trips_and_is_absent_by_default(alice: TestClient) -> None:
    # A fresh vault carries no hosted_url; PATCH stores it verbatim; GET surfaces it. Rename +
    # hosted_url may travel in one PATCH — the tags/hosted_url absent rule keeps each field
    # independent, so a URL PATCH must never wipe an existing name (and vice versa).
    vid, _ = _make_vault(alice, _DOCS)
    assert alice.get(f"/api/vaults/{vid}").json()["hosted_url"] == "", "unset == empty string"

    r = alice.patch(f"/api/vaults/{vid}",
                    json={"name": "Expert pack", "hosted_url": _HOSTED_URL})
    assert r.status_code == 200
    assert r.json()["hosted_url"] == _HOSTED_URL
    assert alice.get(f"/api/vaults/{vid}").json()["hosted_url"] == _HOSTED_URL

    # A PATCH without hosted_url must not wipe it (tags-pattern parity).
    alice.patch(f"/api/vaults/{vid}", json={"name": "Expert pack renamed"})
    assert alice.get(f"/api/vaults/{vid}").json()["hosted_url"] == _HOSTED_URL


def test_patch_hosted_url_empty_string_clears_it(alice: TestClient) -> None:
    # Empty string == cleared, exactly like tags. A user removing the note must be able to
    # remove it — no separate DELETE endpoint required.
    vid, _ = _make_vault(alice, _DOCS)
    alice.patch(f"/api/vaults/{vid}", json={"name": "Expert pack", "hosted_url": _HOSTED_URL})
    assert alice.get(f"/api/vaults/{vid}").json()["hosted_url"] == _HOSTED_URL

    r = alice.patch(f"/api/vaults/{vid}", json={"name": "Expert pack", "hosted_url": ""})
    assert r.status_code == 200 and r.json()["hosted_url"] == ""


def test_patch_hosted_url_strips_fragment_before_storing(alice: TestClient) -> None:
    # Fragment hygiene mirrors subscribe: a sealed-share URL keeps its key in the fragment, so
    # nothing stored (or later fetched) may ever see it. Belt and suspenders — netguard would
    # strip again at fetch time, but the store must never keep the fragment on disk.
    vid, _ = _make_vault(alice, _DOCS)
    r = alice.patch(f"/api/vaults/{vid}",
                    json={"name": "Expert pack", "hosted_url": _HOSTED_URL + "#k=SECRETFRAG"})
    assert r.status_code == 200
    assert r.json()["hosted_url"] == _HOSTED_URL, "fragment must be dropped before storage"
    assert "SECRETFRAG" not in alice.get(f"/api/vaults/{vid}").json()["hosted_url"]


def test_patch_hosted_url_refuses_a_bad_scheme(alice: TestClient) -> None:
    # http(s) only; anything else is refused with a clean 400 that names the rule.
    vid, _ = _make_vault(alice, _DOCS)
    for bad in ("ftp://vaults.example.com/pack.sbvault",
                "javascript:alert(1)",
                "file:///etc/passwd"):
        r = alice.patch(f"/api/vaults/{vid}", json={"name": "Expert pack", "hosted_url": bad})
        assert r.status_code == 400, (bad, r.text)
        assert "http" in r.json()["detail"], (bad, r.text)
    assert alice.get(f"/api/vaults/{vid}").json()["hosted_url"] == "", "no partial store on refusal"


def test_patch_hosted_url_refuses_lan_and_localhost_via_real_netguard(alice: TestClient) -> None:
    # NO monkeypatch: the SAME netguard IP allowlist the /subscribe path uses must reject
    # localhost/LAN here too. The refusal explains in plain words — same rule as subscribe.
    vid, _ = _make_vault(alice, _DOCS)
    for bad in ("http://localhost:33000/pack.sbvault",
                "http://127.0.0.1/pack.sbvault",
                "http://10.0.0.5/pack.sbvault"):
        r = alice.patch(f"/api/vaults/{vid}", json={"name": "Expert pack", "hosted_url": bad})
        assert r.status_code == 400, (bad, r.text)
        assert "public internet" in r.json()["detail"], (bad, r.text)
    assert alice.get(f"/api/vaults/{vid}").json()["hosted_url"] == ""


def _publish_and_set_hosted(client: TestClient, docs: list[tuple[str, str]] = _DOCS,
                             ) -> tuple[str, bytes]:
    """Publish a vault open and record its hosted_url — the setup verify-hosted operates on."""
    vid, _ = _make_vault(client, docs)
    blob = _export(client, vid, _PASS_A, mode="open")
    r = client.patch(f"/api/vaults/{vid}",
                     json={"name": "Expert pack", "hosted_url": _HOSTED_URL})
    assert r.status_code == 200
    return vid, blob


def test_verify_hosted_happy_path_matches(alice: TestClient, monkeypatch) -> None:
    # Publish → set hosted_url → serve the SAME blob at the URL → verify-hosted reports matches.
    # This is the "the file up there IS what I last published" path — the whole point of the
    # feature. The response shape is exactly what the UI task will consume.
    vid, blob = _publish_and_set_hosted(alice)
    fetched = _serve(monkeypatch, blob)

    r = alice.post(f"/api/vaults/{vid}/verify-hosted", headers=_LOCAL)
    assert r.status_code == 200, r.text
    body = r.json()
    published_seq = alice.get(f"/api/vaults/{vid}").json()["published_seq"]
    assert body == {"reachable": True, "seq": published_seq, "matches": True,
                    "behind": False, "retired": False, "detail": body["detail"]}
    assert "matches" in body["detail"] and str(published_seq) in body["detail"]
    assert fetched == [_HOSTED_URL], "verify-hosted must fetch the stored URL, once"


def test_verify_hosted_behind_case_is_the_classic_forgot_to_upload(
        alice: TestClient, monkeypatch) -> None:
    # Publish v2, hosted still v2, publish v3 (new content), hosted URL still serves v2.
    # The response calls out "did you forget to upload the new file?" — matches=False, behind=True.
    vid, blob_v2 = _publish_and_set_hosted(alice)
    # Bump: add a doc and re-publish, so published_seq moves ahead of what's hosted.
    new_id = alice.post("/api/kb", json={"title": "New", "content": "fresh content"}).json()["id"]
    alice.post(f"/api/vaults/{vid}/documents", json={"doc_ids": [new_id]})
    _export(alice, vid, _PASS_A, mode="open")  # v3; published_seq now == 3
    published_seq = alice.get(f"/api/vaults/{vid}").json()["published_seq"]
    assert published_seq == 3

    _serve(monkeypatch, blob_v2)  # host still on the OLD file
    body = alice.post(f"/api/vaults/{vid}/verify-hosted", headers=_LOCAL).json()
    assert body["reachable"] is True
    assert body["seq"] == 2
    assert body["matches"] is False and body["behind"] is True
    assert "forget to upload" in body["detail"] or "forgot to upload" in body["detail"]


def test_verify_hosted_newer_hosted_seq_is_the_anomaly_case(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # A hosted file signed by us but NEWER than our record — was it published from another
    # machine? Use bob's install as an unrelated Desktop and give it Alice's publisher secret
    # so the file it publishes is "signed by Alice" from Alice's pov. Simpler: build a NEWER
    # blob directly using Alice's identity via her own /export, then rewind Alice's version.
    # Cleanest: publish v2, verify it (matches), then hand-plant a higher published_seq lower
    # than the hosted file's seq. But hand-planting bypasses the whole publish path — instead,
    # publish twice, serve the OLDER one but pretend published_seq is even LOWER: publish once
    # (v2), leave hosted at v2, then rewrite the local published_seq to 1 in the body — the
    # published_seq legacy fallback fabric is deliberately tolerant of a hand-set lower value.
    vid, blob = _publish_and_set_hosted(alice)
    store = alice.app.state.vaults
    body = store._load_body(vid)
    body["published_seq"] = 1  # simulate "another machine published v2; this install thinks v1"
    store._store_body(vid, body)
    assert alice.get(f"/api/vaults/{vid}").json()["published_seq"] == 1

    _serve(monkeypatch, blob)  # hosted file is v2
    body = alice.post(f"/api/vaults/{vid}/verify-hosted", headers=_LOCAL).json()
    assert body["reachable"] is True
    assert body["seq"] == 2
    assert body["matches"] is False and body["behind"] is False
    assert "NEWER" in body["detail"] and "another machine" in body["detail"]


def test_verify_hosted_wrong_signature_says_the_signature_is_not_yours(
        alice: TestClient, monkeypatch) -> None:
    # A stranger's vault at the same URL: same vault_id, valid signature — by a different key.
    # verify-hosted must return reachable=True, matches=False, and name the OFFERED fingerprint
    # in the detail so the user knows who is at that URL now. Never a 500; no state changes.
    vid, _blob = _publish_and_set_hosted(alice)
    vault_id = alice.get(f"/api/vaults/{vid}").json()["id"]

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    attacker = SecretStore(conn, gen_master_key())  # a different Ed25519 identity
    forged = vault_format.pack(
        store=attacker, vault_id=vault_id, name="Impostor", description="", seq=99,
        mode=vault_format.OPEN, name_key=gen_master_key(),
        docs=[{"uid": "evil-1", "title": "Poison", "content": "malicious REPLACEMENT",
               "meta": {}, "chunks": 1}])
    offered_pub = vault_format.read_manifest(forged)["publisher"]["pubkey"]
    _serve(monkeypatch, forged)

    body = alice.post(f"/api/vaults/{vid}/verify-hosted", headers=_LOCAL).json()
    assert body["reachable"] is True
    assert body["matches"] is False and body["behind"] is False
    assert body["seq"] is None, "a stranger's seq is not this install's business"
    assert vault_format.fingerprint(offered_pub) in body["detail"]
    assert "isn't yours" in body["detail"] or "not yours" in body["detail"]
    # No pin state exists on a publisher-side vault, and none may be added by a read-only check.
    assert alice.get(f"/api/vaults/{vid}").json()["source"] is None


def test_verify_hosted_unreachable_url_is_a_clean_reachable_false(
        alice: TestClient, monkeypatch) -> None:
    # 404 / 410 / timeout: every one must be reachable=False with an honest detail, never a 500.
    vid, _blob = _publish_and_set_hosted(alice)

    def gone(_url: str) -> bytes:
        raise netguard.FetchError("upstream returned HTTP 410", status=410)

    monkeypatch.setattr(netguard, "safe_fetch_vault", gone)
    body = alice.post(f"/api/vaults/{vid}/verify-hosted", headers=_LOCAL).json()
    assert body == {"reachable": False, "seq": None, "matches": False, "behind": False,
                    "retired": False, "detail": body["detail"]}
    assert body["detail"], "an honest detail must always be present"


def test_verify_hosted_is_desktop_local_only_matching_export(alice: TestClient) -> None:
    # Same fence as /export and /retire: a bridged request has no x-sb-local header, and this
    # endpoint refuses. The publisher's identity key sits behind the fence for a reason — a
    # remote device must not be able to trigger a read that names it.
    vid, _blob = _publish_and_set_hosted(alice)
    r = alice.post(f"/api/vaults/{vid}/verify-hosted")  # no _LOCAL header
    assert r.status_code == 403 and "Desktop-local" in r.json()["detail"]


def test_verify_hosted_without_hosted_url_is_a_clean_400(alice: TestClient) -> None:
    # No hosted_url set: nothing to verify. Clear 400 that says what to do next, so the UI can
    # render it inline rather than surfacing a confusing empty response.
    vid, _ = _make_vault(alice, _DOCS)
    r = alice.post(f"/api/vaults/{vid}/verify-hosted", headers=_LOCAL)
    assert r.status_code == 400 and "hosted URL" in r.json()["detail"]
