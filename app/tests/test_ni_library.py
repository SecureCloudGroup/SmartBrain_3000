"""Neural Interface Global Library (§19/§20/§21): pack format, verification, TOFU
LibraryStore, install flow, board template_update flag, apply-template-update,
export-as-template, and the tools/ni-library build+validate scripts.

All crypto is REAL (not mocked): pack bytes are signed by a per-test SecretStore
using the ``ni:publisher_ed25519`` mint, so every test walks the shipped code path
end-to-end. The only monkeypatch is ``netguard.safe_fetch_ni_pack`` — the same
place ``test_vault_sync`` patches ``safe_fetch_vault``.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import identity, netguard, ni, ni_library, vault_format
from smartbrain_3000.secrets import SecretStore, gen_master_key

_LOCAL = {"x-sb-local": "1"}


# --- helpers --------------------------------------------------------------

def _stores() -> tuple[SecretStore, ni.NIStore, duckdb.DuckDBPyConnection, bytes]:
    """A hermetic Secret+NI store pair over a fresh in-memory DuckDB."""
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return SecretStore(conn, key), ni.NIStore(conn, key), conn, key


def _template(**over) -> dict:
    """A minimal §19 template that passes parse_pack (spec + preview both valid).

    Deliberately OMITS ``contract`` / ``_c2_ok`` / ``_l1_*`` / ``_template`` from
    ``spec_template`` — LOW#6 (audit 2026-09-09) makes parse_pack REFUSE any
    template that ships those engine/install-owned keys. Phase 4b D2c (audit
    2026-09-11) extends the forbidden set to ``_l2_*`` and ``repair_policy``
    (installer's local choice; the install path forces the safe default).
    """
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "{{title}}", "role": "title",
         "tone": "default", "size": "md"},
    ]}
    spec_template = {
        "version": 1, "title": "Weather", "goal": "show the weather",
        "params": {"zip": {"label": "ZIP", "kind": "string", "value": ""}},
        "source": {"type": "model", "instruction": "hi"},
        "pipeline": [], "scene": scene, "display": {"size": "small"},
        "model": None,
    }
    template = {
        "id": "weather-basic",
        "title": "Weather basic",
        "goal": "show weather at a ZIP",
        "category": "weather",
        "tags": ["nws"],
        "spec_template": spec_template,
        "preview_payload": {"title": "preview title"},
        "notes": "example",
    }
    template.update(over)
    return template


def _sign_pack(secrets_store: SecretStore, payload: dict) -> bytes:
    """Return canonical envelope bytes signed by the publisher key."""
    signed = ni_library.PACK_SIG_PREFIX + vault_format.canonical(payload)
    signature = identity.sign(secrets_store, signed, identity.NI_PUBLISHER_SECRET)
    envelope = {"sb_ni_pack": payload,
                "sig": {"alg": "ed25519", "value": signature}}
    return vault_format.canonical(envelope)


def _pack_payload(pubkey: str, *, seq: int = 1, pack_id: str = "pack-1",
                   templates: list[dict] | None = None) -> dict:
    """Assemble a valid §19 payload dict — used as input to _sign_pack."""
    return {
        "version": 1,
        "pack_id": pack_id,
        "seq": int(seq),
        "published_at": "2026-09-09",
        "publisher": {"label": "SmartBrain project", "pubkey": pubkey},
        "templates": templates if templates is not None else [_template()],
    }


def _build_pack(secrets_store: SecretStore, *, seq: int = 1,
                pack_id: str = "pack-1",
                templates: list[dict] | None = None) -> tuple[bytes, dict]:
    """Return (envelope_bytes, payload_dict) signed by ``secrets_store``'s publisher key."""
    pubkey = identity.public_key_b64(secrets_store, identity.NI_PUBLISHER_SECRET)
    payload = _pack_payload(pubkey, seq=seq, pack_id=pack_id, templates=templates)
    return _sign_pack(secrets_store, payload), payload


# --- parse_pack: bounds + shape + duplicate ids -----------------------------

def test_parse_pack_valid_roundtrip() -> None:
    secrets, _ni_store, _, _ = _stores()
    raw, payload = _build_pack(secrets)
    parsed = ni_library.parse_pack(raw)
    assert parsed["pack_id"] == payload["pack_id"]
    assert parsed["seq"] == payload["seq"]
    assert parsed["templates"][0]["id"] == "weather-basic"


def test_parse_pack_refuses_over_size_cap() -> None:
    with pytest.raises(ni_library.LibraryError):
        ni_library.parse_pack(b"x" * (ni_library.MAX_PACK_BYTES + 1))


def test_parse_pack_refuses_over_template_count() -> None:
    secrets, _, _, _ = _stores()
    # Force MAX_TEMPLATES + 1 by fabricating templates with distinct ids
    templates = [_template(id=f"t-{i}") for i in range(ni_library.MAX_TEMPLATES + 1)]
    payload = _pack_payload(identity.public_key_b64(secrets, identity.NI_PUBLISHER_SECRET),
                            templates=templates)
    raw = _sign_pack(secrets, payload)
    with pytest.raises(ni_library.LibraryError, match="more than"):
        ni_library.parse_pack(raw)


def test_parse_pack_refuses_duplicate_template_ids() -> None:
    secrets, _, _, _ = _stores()
    dup = [_template(id="dup"), _template(id="dup")]
    payload = _pack_payload(identity.public_key_b64(secrets, identity.NI_PUBLISHER_SECRET),
                            templates=dup)
    raw = _sign_pack(secrets, payload)
    with pytest.raises(ni_library.LibraryError, match="duplicate"):
        ni_library.parse_pack(raw)


def test_parse_pack_refuses_bad_spec_template() -> None:
    secrets, _, _, _ = _stores()
    bad = _template()
    bad["spec_template"]["source"] = {"type": "carrier-pigeon"}  # unknown source type
    payload = _pack_payload(identity.public_key_b64(secrets, identity.NI_PUBLISHER_SECRET),
                            templates=[bad])
    raw = _sign_pack(secrets, payload)
    with pytest.raises(ni_library.LibraryError):
        ni_library.parse_pack(raw)


def test_parse_pack_refuses_bad_preview_payload() -> None:
    secrets, _, _, _ = _stores()
    bad = _template()
    bad["preview_payload"] = {"unrelated": "field"}  # scene binds {{title}}, missing
    payload = _pack_payload(identity.public_key_b64(secrets, identity.NI_PUBLISHER_SECRET),
                            templates=[bad])
    raw = _sign_pack(secrets, payload)
    with pytest.raises(ni_library.LibraryError, match="preview_payload"):
        ni_library.parse_pack(raw)


def test_parse_pack_accepts_image_node_template_and_leaves_others_untouched() -> None:
    """Phase 4c audit 2026-09-11 (finding #1): a template whose scene contains an
    image node must parse — the per-template preview bind at parse time needs a
    preview-style image_ref threaded in, or ``_bind_image_src`` raises
    ``image_missing`` and the whole pack is refused. A non-image template in the
    SAME pack must still parse (regression net: the fix does not accidentally
    require image_ref for non-image scenes)."""
    secrets, _, _, _ = _stores()
    image_scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "image", "alt": "radar frame"},
    ]}
    image_tpl = _template(
        id="radar-image",
        title="Radar image",
        goal="show a radar frame",
        spec_template={
            "version": 1, "title": "Radar", "goal": "show a radar frame",
            "params": {}, "pipeline": [],
            "source": {"type": "http_image",
                        "url": "https://cdn.example.com/radar.png", "headers": {}},
            "scene": image_scene, "display": {"size": "small"}, "model": None,
        },
        preview_payload={"image": {"bytes_len": 0, "format": "png"}},
    )
    payload = _pack_payload(identity.public_key_b64(secrets, identity.NI_PUBLISHER_SECRET),
                            templates=[image_tpl, _template(id="weather-basic")])
    raw = _sign_pack(secrets, payload)
    parsed = ni_library.parse_pack(raw)
    ids = [t["id"] for t in parsed["templates"]]
    assert ids == ["radar-image", "weather-basic"]


def test_parse_pack_allows_empty_and_placeholder_secrets() -> None:
    """§19: string params ship empty, secret params ship 'ni:self:<name>'."""
    secrets, _, _, _ = _stores()
    template = _template()
    template["spec_template"]["params"]["api_key"] = {
        "label": "API key", "kind": "secret", "value": "ni:self:api_key"}
    template["spec_template"]["params"]["zip"]["value"] = ""
    payload = _pack_payload(identity.public_key_b64(secrets, identity.NI_PUBLISHER_SECRET),
                            templates=[template])
    raw = _sign_pack(secrets, payload)
    parsed = ni_library.parse_pack(raw)
    assert parsed["templates"][0]["spec_template"]["params"]["api_key"]["value"] \
        == "ni:self:api_key"


# --- verify_pack: order (pack_id → sig → seq) ------------------------------

def test_verify_pack_up_to_date_when_seq_equal() -> None:
    secrets, _, _, _ = _stores()
    raw, payload = _build_pack(secrets, seq=1)
    pubkey = payload["publisher"]["pubkey"]
    verdict = ni_library.verify_pack(raw, pubkey, "pack-1", 1)
    assert verdict["up_to_date"] and not verdict["behind"]


def test_verify_pack_behind_when_seq_higher() -> None:
    secrets, _, _, _ = _stores()
    raw, payload = _build_pack(secrets, seq=5)
    verdict = ni_library.verify_pack(raw, payload["publisher"]["pubkey"], "pack-1", 3)
    assert verdict["behind"] and verdict["remote_seq"] == 5


def test_verify_pack_refuses_wrong_pack_id() -> None:
    secrets, _, _, _ = _stores()
    raw, payload = _build_pack(secrets, pack_id="a")
    with pytest.raises(ni_library.LibraryError, match="DIFFERENT"):
        ni_library.verify_pack(raw, payload["publisher"]["pubkey"], "b", 0)


def test_verify_pack_refuses_rollback() -> None:
    secrets, _, _, _ = _stores()
    raw, payload = _build_pack(secrets, seq=2)
    with pytest.raises(ni_library.RollbackError):
        ni_library.verify_pack(raw, payload["publisher"]["pubkey"], "pack-1", 5)


def test_verify_pack_raises_key_changed_with_offered_key() -> None:
    """A pack signed by key B, but the pin is key A → KeyChanged carries B."""
    secrets_a, _, _, _ = _stores()
    secrets_b, _, _, _ = _stores()
    pin_pubkey = identity.public_key_b64(secrets_a, identity.NI_PUBLISHER_SECRET)
    raw, payload = _build_pack(secrets_b)  # signed by B, carries B's pubkey inside
    with pytest.raises(ni_library.KeyChanged) as exc:
        ni_library.verify_pack(raw, pin_pubkey, "pack-1", 0)
    assert exc.value.offered_pubkey == payload["publisher"]["pubkey"]
    assert exc.value.offered_pubkey != pin_pubkey


def test_verify_pack_refuses_tampered_bytes() -> None:
    """H1 (audit 2026-09-09): a one-byte tamper fails against BOTH the pinned key AND
    the embedded key → plain ``LibraryError('pack signature does not verify')``, NEVER
    a ``KeyChanged`` (which subclasses LibraryError and would present the same bytes
    to the user as if they were a legitimate publisher rotation).

    Asserts the EXACT class — the pre-audit implementation raised ``KeyChanged`` here
    (because the pinned-key check failed and it didn't verify against the embedded
    key first), so a byte-tamper looked identical to a rotation in the UI. This test
    would fail against the pre-audit code.
    """
    secrets, _, _, _ = _stores()
    raw, payload = _build_pack(secrets)
    # Flip a byte in the middle (avoid the JSON structure — mutate a title char).
    tampered = raw.replace(b"Weather", b"Tampered", 1)
    assert tampered != raw
    with pytest.raises(ni_library.LibraryError) as exc_info:
        ni_library.verify_pack(tampered, payload["publisher"]["pubkey"], "pack-1", 0)
    assert not isinstance(exc_info.value, ni_library.KeyChanged), (
        "H1: tampering must NOT masquerade as a rotation")
    assert "signature does not verify" in str(exc_info.value)


def test_verify_pack_attacker_pubkey_garbage_sig_is_libraryerror() -> None:
    """H1: an attacker who ships their own pubkey in ``publisher.pubkey`` but signs
    with a DIFFERENT key (or garbage) must land as LibraryError, NOT KeyChanged —
    the pre-audit code would present the attacker's pubkey to the user as a
    "trust the new fingerprint" affordance.
    """
    # Attacker A owns key A; publisher B is the real (unrelated) publisher.
    secrets_attacker, _, _, _ = _stores()
    secrets_wrong, _, _, _ = _stores()
    secrets_pinned, _, _, _ = _stores()
    attacker_pubkey = identity.public_key_b64(secrets_attacker, identity.NI_PUBLISHER_SECRET)
    pinned_pubkey = identity.public_key_b64(secrets_pinned, identity.NI_PUBLISHER_SECRET)
    assert attacker_pubkey != pinned_pubkey
    # Build a payload that EMBEDS the attacker's pubkey but is signed by a DIFFERENT
    # key (wrong) — so neither the pinned key nor the embedded key verifies the sig.
    payload = _pack_payload(attacker_pubkey)
    raw = _sign_pack(secrets_wrong, payload)  # signed by wrong, embedded=attacker
    with pytest.raises(ni_library.LibraryError) as exc_info:
        ni_library.verify_pack(raw, pinned_pubkey, "pack-1", 0)
    assert not isinstance(exc_info.value, ni_library.KeyChanged), (
        "H1: an attacker-authored pubkey with a garbage sig is tampering, not rotation")


def test_verify_pack_real_rotation_is_keychanged_with_new_key() -> None:
    """H1: a pack SIGNED by the new key AND embedding that key inside is a
    legitimate publisher rotation — KeyChanged is raised carrying the NEW key so
    the user can compare fingerprints and re-trust out-of-band."""
    secrets_old, _, _, _ = _stores()
    secrets_new, _, _, _ = _stores()
    pinned = identity.public_key_b64(secrets_old, identity.NI_PUBLISHER_SECRET)
    raw, payload = _build_pack(secrets_new)  # signed by new, embedded=new
    with pytest.raises(ni_library.KeyChanged) as exc_info:
        ni_library.verify_pack(raw, pinned, "pack-1", 0)
    assert exc_info.value.offered_pubkey == payload["publisher"]["pubkey"]
    assert exc_info.value.offered_pubkey != pinned


# --- LibraryStore: TOFU connect, source pin sealed at rest -----------------

def _fake_fetcher(raw: bytes):
    """A stand-in netguard module that serves ``raw`` regardless of URL/cap."""

    class _Guard:
        @staticmethod
        def safe_fetch_ni_pack(url: str, cap: int) -> bytes:
            assert url and cap > 0, "fake_fetcher args"
            return raw

    return _Guard()


def test_library_store_connect_tofu_pins_publisher_key() -> None:
    secrets, ni_store, _, _ = _stores()
    raw, payload = _build_pack(secrets)
    library = ni_library.LibraryStore(ni_store, netguard_mod=_fake_fetcher(raw))
    pin = library.connect("https://library.example.com/pack.json")
    assert pin["publisher_pubkey"] == payload["publisher"]["pubkey"]
    assert pin["pack_id"] == "pack-1" and pin["seq"] == 1
    # Round-trip through the store
    stored = library.source()
    assert stored is not None and stored["url"].startswith("https://library.example.com/")


def test_library_source_url_is_sealed_in_ni_snapshots() -> None:
    """The source pin holds the URL; at-rest bytes must NOT reveal it (feed/vault law)."""
    secrets, ni_store, conn, _ = _stores()
    raw, _ = _build_pack(secrets)
    library = ni_library.LibraryStore(ni_store, netguard_mod=_fake_fetcher(raw))
    library.connect("https://library.example.com/pack.json")
    row = conn.execute("SELECT ciphertext FROM ni_snapshots WHERE item_id = ? AND slot = ?;",
                       [ni_library.LIBRARY_RESERVED_ID, ni_library.LIBRARY_SLOT_SOURCE]).fetchone()
    assert row is not None
    ciphertext = bytes(row[0])
    assert b"library.example.com" not in ciphertext
    assert b"pack-1" not in ciphertext


def test_library_store_refuses_second_connect() -> None:
    secrets, ni_store, _, _ = _stores()
    raw, _ = _build_pack(secrets)
    library = ni_library.LibraryStore(ni_store, netguard_mod=_fake_fetcher(raw))
    library.connect("https://a.example.com/pack.json")
    with pytest.raises(ni_library.LibraryError, match="already connected"):
        library.connect("https://b.example.com/pack.json")


def test_library_disconnect_wipes_slots() -> None:
    secrets, ni_store, _, _ = _stores()
    raw, _ = _build_pack(secrets)
    library = ni_library.LibraryStore(ni_store, netguard_mod=_fake_fetcher(raw))
    library.connect("https://a.example.com/pack.json")
    library.disconnect()
    assert library.source() is None and library.pack() is None


# --- LibraryStore: due cadence + dead-host escalation ---------------------

def test_is_due_defaults_to_true_on_never_checked() -> None:
    secrets, ni_store, _, _ = _stores()
    raw, _ = _build_pack(secrets)
    library = ni_library.LibraryStore(ni_store, netguard_mod=_fake_fetcher(raw))
    library.connect("https://a.example.com/pack.json")
    # Just connected → last_checked stamped in the connect. Test the "never" branch
    # by clearing the stamp manually.
    pin = library.source()
    pin["last_checked"] = None
    library._write_source(pin)
    assert library.is_due()


def test_is_due_false_while_within_interval() -> None:
    secrets, ni_store, _, _ = _stores()
    raw, _ = _build_pack(secrets)
    library = ni_library.LibraryStore(ni_store, netguard_mod=_fake_fetcher(raw))
    library.connect("https://a.example.com/pack.json")
    # Fresh connect stamps last_checked now → NOT due.
    assert not library.is_due()


def test_record_failure_advances_counter_and_escalates() -> None:
    """Vault_sync mirror: 8 consecutive failures AND ≥7 elapsed days → unreachable."""
    import datetime as _dt
    secrets, ni_store, _, _ = _stores()
    raw, _ = _build_pack(secrets)
    library = ni_library.LibraryStore(ni_store, netguard_mod=_fake_fetcher(raw))
    library.connect("https://a.example.com/pack.json")
    epoch = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    for i in range(8):
        stamp = epoch + _dt.timedelta(days=i + 1)  # 8 days spread
        library.record_failure(RuntimeError("boom"), now=stamp)
    pin = library.source()
    assert pin["consecutive_failures"] == 8
    assert pin["unreachable"] is True


def test_check_update_clears_failure_counter_on_success() -> None:
    secrets, ni_store, _, _ = _stores()
    raw, _ = _build_pack(secrets)
    library = ni_library.LibraryStore(ni_store, netguard_mod=_fake_fetcher(raw))
    library.connect("https://a.example.com/pack.json")
    library.record_failure(RuntimeError("boom"))
    pin_after_fail = library.source()
    assert pin_after_fail["consecutive_failures"] == 1
    library.check_update()
    pin_after_ok = library.source()
    assert pin_after_ok["consecutive_failures"] == 0


# --- install: params fill, secrets refused, draft state, provenance -------

def _install_context() -> tuple[SecretStore, ni.NIStore, ni_library.LibraryStore, dict]:
    """Return (secrets, ni_store, library, pin) with one pack ready to install from."""
    secrets, ni_store, _, _ = _stores()
    template = _template()
    template["spec_template"]["params"]["api_key"] = {
        "label": "API key", "kind": "secret", "value": "ni:self:api_key"}
    raw, _ = _build_pack(secrets, templates=[template])
    library = ni_library.LibraryStore(ni_store, netguard_mod=_fake_fetcher(raw))
    pin = library.connect("https://a.example.com/pack.json")
    return secrets, ni_store, library, pin


def test_build_installed_spec_fills_string_params() -> None:
    _, _, library, _ = _install_context()
    template = library.get_template("weather-basic")
    spec = ni_library.build_installed_spec(template, {"zip": "94103"})
    assert spec["params"]["zip"]["value"] == "94103"
    # Secret placeholder kept — install BODY doesn't take secrets
    assert spec["params"]["api_key"]["value"] == "ni:self:api_key"


def test_build_installed_spec_refuses_secret_in_body() -> None:
    _, _, library, _ = _install_context()
    template = library.get_template("weather-basic")
    with pytest.raises(ni_library.LibraryError, match="secret param"):
        ni_library.build_installed_spec(template, {"api_key": "leak"})


def test_install_creates_draft_with_sealed_provenance() -> None:
    secrets, ni_store, library, pin = _install_context()
    template = library.get_template("weather-basic")
    spec = ni_library.build_installed_spec(template, {"zip": "94103"})
    spec["_template"] = ni_library.provenance_for(
        pin["pack_id"], template["id"], int(pin["seq"]), template["spec_template"])
    item_id = ni_store.add_item(spec, template["preview_payload"], origin="template")
    item = ni_store.get_item(item_id)
    assert item["state"] == "draft"
    assert item["spec"]["_template"]["pack_id"] == pin["pack_id"]
    assert item["spec"]["_template"]["template_id"] == template["id"]
    assert item["spec"]["_template"]["spec_hash"] \
        == ni_library.spec_hash(template["spec_template"])


# --- board template_update flag -------------------------------------------

def test_board_template_update_flips_when_pack_bumps_spec_hash(tmp_path, monkeypatch) -> None:
    """Install v1 → pack ships v2 with a changed spec → board shows template_update=true."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "board.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        assert client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        # Publisher key lives inside the app's SecretStore — use it to sign packs.
        state = client.app.state
        secrets_store = state.secret_store
        assert secrets_store is not None
        template = _template()
        # Fetch v1
        raw_v1, _ = _build_pack(secrets_store, seq=1, templates=[template])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack",
                            lambda url, cap: raw_v1)
        r = client.post("/api/ni/library/connect", headers=_LOCAL,
                        json={"url": "https://a.example.com/pack.json"})
        assert r.status_code == 200, r.text
        # Install from v1
        r = client.post("/api/ni/library/install",
                        json={"template_id": "weather-basic",
                              "params": {"zip": "94103"}})
        assert r.status_code == 200, r.text
        # Board initially says NO update
        rows = client.get("/api/ni/board").json()["items"]
        assert len(rows) == 1
        assert rows[0]["template_update"] is False
        assert rows[0]["template_gone"] is False
        # Publisher ships v2 with a mutated spec
        mutated = _template()
        mutated["spec_template"]["goal"] = "show the temperature and the forecast"
        raw_v2, _ = _build_pack(secrets_store, seq=2, templates=[mutated])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack",
                            lambda url, cap: raw_v2)
        r = client.post("/api/ni/library/check")
        assert r.status_code == 200, r.text
        rows = client.get("/api/ni/board").json()["items"]
        assert rows[0]["template_update"] is True
        assert rows[0]["template_gone"] is False


def test_board_template_gone_flag(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "gone.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        assert client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        secrets_store = client.app.state.secret_store
        raw_v1, _ = _build_pack(secrets_store, seq=1, templates=[_template()])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack",
                            lambda url, cap: raw_v1)
        client.post("/api/ni/library/connect", headers=_LOCAL,
                    json={"url": "https://a.example.com/pack.json"})
        client.post("/api/ni/library/install",
                    json={"template_id": "weather-basic", "params": {"zip": "94103"}})
        # v2 REMOVES the template entirely (registry-repo template deletion)
        raw_v2, _ = _build_pack(secrets_store, seq=2,
                                templates=[_template(id="other-thing")])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack",
                            lambda url, cap: raw_v2)
        client.post("/api/ni/library/check")
        rows = client.get("/api/ni/board").json()["items"]
        assert rows[0]["template_gone"] is True
        assert rows[0]["template_update"] is False


# --- apply-template-update: draft reset + provenance bump -----------------

def test_apply_template_update_carries_over_params_and_resets_to_draft(
        tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "apply.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        assert client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        secrets_store = client.app.state.secret_store
        template = _template()
        raw_v1, _ = _build_pack(secrets_store, seq=1, templates=[template])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack",
                            lambda url, cap: raw_v1)
        client.post("/api/ni/library/connect", headers=_LOCAL,
                    json={"url": "https://a.example.com/pack.json"})
        r = client.post("/api/ni/library/install",
                        json={"template_id": "weather-basic",
                              "params": {"zip": "94103"}})
        item_id = r.json()["item_id"]
        # Force out of draft so the reset is observable
        client.app.state.ni.set_state(item_id, "live")
        # v2 with a different goal (spec_hash flips)
        mutated = _template()
        mutated["spec_template"]["goal"] = "different goal"
        raw_v2, _ = _build_pack(secrets_store, seq=2, templates=[mutated])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack",
                            lambda url, cap: raw_v2)
        client.post("/api/ni/library/check")
        # Apply the template update for our item
        r = client.post(f"/api/ni/items/{item_id}/apply-template-update")
        assert r.status_code == 200, r.text
        # State reset to draft; param value carried; provenance stamped to v2
        item = client.get(f"/api/ni/items/{item_id}").json()
        assert item["state"] == "draft"
        assert item["spec"]["params"]["zip"]["value"] == "94103"
        assert item["spec"]["_template"]["seq"] == 2


# --- export-as-template (§21) --------------------------------------------

def test_export_template_strips_secrets_and_empties_params(
        tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "export.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        assert client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        secrets_store = client.app.state.secret_store
        template = _template()
        template["spec_template"]["params"]["api_key"] = {
            "label": "API key", "kind": "secret", "value": "ni:self:api_key"}
        raw, _ = _build_pack(secrets_store, templates=[template])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack", lambda url, cap: raw)
        client.post("/api/ni/library/connect", headers=_LOCAL,
                    json={"url": "https://a.example.com/pack.json"})
        r = client.post("/api/ni/library/install",
                        json={"template_id": "weather-basic",
                              "params": {"zip": "94103"}})
        item_id = r.json()["item_id"]
        # PUT a credential so the export sanitizer has a value to check for
        r = client.put(f"/api/ni/items/{item_id}/credential",
                       json={"name": "api_key", "value": "super-secret",
                              "host": "example.com"},
                       headers=_LOCAL)
        assert r.status_code == 200, r.text
        # Export
        r = client.get(f"/api/ni/items/{item_id}/export-template", headers=_LOCAL)
        assert r.status_code == 200, r.text
        body = r.json()
        spec = body["spec_template"]
        # Params emptied; secret placeholder preserved
        assert spec["params"]["zip"]["value"] == ""
        assert spec["params"]["api_key"]["value"] == "ni:self:api_key"
        # System fields stripped
        for key in ("contract", "_c2_ok", "_l1_last_attempt", "_l1_trial", "_template"):
            assert key not in spec


def test_export_refuses_credential_value_in_header_literal(
        tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "leak.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        assert client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        secrets_store = client.app.state.secret_store
        # Template with an http_json source that has NO secret ref — but the user
        # accidentally pastes their credential as a plain literal in a header.
        template = _template()
        template["spec_template"]["params"]["api_key"] = {
            "label": "API key", "kind": "secret", "value": "ni:self:api_key"}
        template["spec_template"]["source"] = {
            "type": "http_json",
            "url": "https://example.com/api?zip=94103",
            "headers": {"x-Static": "bearer super-secret-value"},
        }
        raw, _ = _build_pack(secrets_store, templates=[template])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack", lambda url, cap: raw)
        client.post("/api/ni/library/connect", headers=_LOCAL,
                    json={"url": "https://a.example.com/pack.json"})
        r = client.post("/api/ni/library/install",
                        json={"template_id": "weather-basic", "params": {}})
        item_id = r.json()["item_id"]
        client.put(f"/api/ni/items/{item_id}/credential",
                   json={"name": "api_key", "value": "super-secret-value",
                          "host": "example.com"},
                   headers=_LOCAL)
        r = client.get(f"/api/ni/items/{item_id}/export-template", headers=_LOCAL)
        assert r.status_code == 400
        assert "credential" in r.json()["detail"].lower()


def _image_template() -> dict:
    """Phase 4c audit 2026-09-11 (finding #1): an image-node template used by the
    image-card roundtrip + apply-update tests below. Mirrors the _template()
    shape but with an http_image source + image-node scene + preview metadata."""
    image_scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "image", "alt": "radar frame"},
    ]}
    return {
        "id": "radar-image", "title": "Radar image",
        "goal": "show a radar frame", "category": "weather",
        "tags": ["radar"],
        "spec_template": {
            "version": 1, "title": "Radar", "goal": "show a radar frame",
            "params": {}, "pipeline": [],
            "source": {"type": "http_image",
                        "url": "https://cdn.example.com/radar.png",
                        "headers": {}},
            "scene": image_scene, "display": {"size": "small"}, "model": None,
        },
        "preview_payload": {"image": {"bytes_len": 0, "format": "png"}},
        "notes": "example image template",
    }


def test_library_install_refuses_composite_depth_violation(
        tmp_path, monkeypatch) -> None:
    """Phase 4c audit 2026-09-11 (finding #4): library_install must run the §25
    depth guard BEFORE add_item. A composite template whose alias points at an
    installed internal.ni item lands with the runtime guard as the sole defence
    otherwise — every tick refuses fresh. Guard fires here as a clean 400."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "install-depth.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        assert client.post("/api/account/setup",
                            json={"passphrase": "correct-horse"}).status_code == 200
        secrets_store = client.app.state.secret_store
        # An existing internal.ni item on the desktop — no library needed for this row.
        inner_id = client.app.state.ni.add_item(
            _template()["spec_template"], {"title": "seed"})
        composite_scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
            {"type": "text", "value": "{{stock.title}}", "role": "title",
             "tone": "default", "size": "md"},
        ]}
        composite_outer_id = client.app.state.ni.add_item(
            {"version": 1, "title": "Aggregate", "goal": "aggregate a stock",
             "params": {}, "pipeline": [], "scene": composite_scene,
             "display": {"size": "small"}, "model": None,
             "source": {"type": "internal.ni", "items": {"stock": inner_id}}},
            {"stock": {"title": "T", "state": "live",
                       "payload_at": None, "history": {}}},
        )
        # Publisher ships a composite template that (once install rewrites refs)
        # would reference composite_outer_id — a composite-of-composite.
        composite_tpl = {
            "id": "composite-deep", "title": "Deep composite",
            "goal": "aggregate an aggregate", "category": "weather", "tags": [],
            "spec_template": {
                "version": 1, "title": "Deep", "goal": "aggregate an aggregate",
                "params": {}, "pipeline": [], "scene": composite_scene,
                "display": {"size": "small"}, "model": None,
                "source": {"type": "internal.ni",
                            "items": {"stock": composite_outer_id}},
            },
            "preview_payload": {"stock": {"title": "T", "state": "live",
                                            "payload_at": None, "history": {}}},
            "notes": "installs into a composite-of-composite",
        }
        raw, _ = _build_pack(secrets_store, templates=[composite_tpl])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack", lambda url, cap: raw)
        client.post("/api/ni/library/connect", headers=_LOCAL,
                    json={"url": "https://a.example.com/pack.json"})
        r = client.post("/api/ni/library/install",
                        json={"template_id": "composite-deep", "params": {}})
        assert r.status_code == 400, r.text
        assert "composite" in r.json()["detail"].lower()


def test_export_template_of_image_card_round_trips_200(
        tmp_path, monkeypatch) -> None:
    """Phase 4c audit 2026-09-11 (finding #1): the export path validates the
    exported template through parse_pack's per-template validator (P5). Without
    the image_ref fix a scene carrying an image node fails that round-trip and
    the route 400s. Success proves the fix reached the exporter too."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "export-image.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        assert client.post("/api/account/setup",
                            json={"passphrase": "correct-horse"}).status_code == 200
        secrets_store = client.app.state.secret_store
        raw, _ = _build_pack(secrets_store, templates=[_image_template()])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack", lambda url, cap: raw)
        client.post("/api/ni/library/connect", headers=_LOCAL,
                    json={"url": "https://a.example.com/pack.json"})
        r = client.post("/api/ni/library/install",
                        json={"template_id": "radar-image", "params": {}})
        assert r.status_code == 200, r.text
        item_id = r.json()["item_id"]
        r = client.get(f"/api/ni/items/{item_id}/export-template", headers=_LOCAL)
        assert r.status_code == 200, r.text
        assert r.json()["spec_template"]["source"]["type"] == "http_image"


def test_library_status_row_binds_image_preview(
        tmp_path, monkeypatch) -> None:
    """Phase 4c audit 2026-09-11 (finding #1): _template_row binds the image
    preview via bind_scene with a preview-style image_ref. Without the fix the
    listing quietly renders an empty-stack fallback (the safety net); with the
    fix the row carries the real bound image node."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "row-image.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        assert client.post("/api/account/setup",
                            json={"passphrase": "correct-horse"}).status_code == 200
        secrets_store = client.app.state.secret_store
        raw, _ = _build_pack(secrets_store, templates=[_image_template()])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack", lambda url, cap: raw)
        client.post("/api/ni/library/connect", headers=_LOCAL,
                    json={"url": "https://a.example.com/pack.json"})
        state = client.get("/api/ni/library").json()
        rows = [t for t in state["templates"] if t["id"] == "radar-image"]
        assert rows, state
        preview = rows[0]["preview_payload"]
        # Bound scene: outer stack contains the bound image node with an ?v=preview src.
        assert preview["type"] == "stack"
        image_node = preview["children"][0]
        assert image_node["type"] == "image"
        assert image_node["src"].endswith("?v=preview"), image_node


def test_apply_template_update_on_image_template_rebinds_preview(
        tmp_path, monkeypatch) -> None:
    """Phase 4c audit 2026-09-11 (finding #1): apply-template-update rebinds the
    preview snapshot via bind_scene — an image-node scene must pass through the
    image_ref threading here too. Without the fix the route 500s; with it the
    stored preview slot carries the rebound image src."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "apply-image.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        assert client.post("/api/account/setup",
                            json={"passphrase": "correct-horse"}).status_code == 200
        secrets_store = client.app.state.secret_store
        v1 = _image_template()
        raw_v1, _ = _build_pack(secrets_store, seq=1, templates=[v1])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack", lambda url, cap: raw_v1)
        client.post("/api/ni/library/connect", headers=_LOCAL,
                    json={"url": "https://a.example.com/pack.json"})
        r = client.post("/api/ni/library/install",
                        json={"template_id": "radar-image", "params": {}})
        assert r.status_code == 200, r.text
        item_id = r.json()["item_id"]
        # v2 bumps a metadata field so spec_hash flips + apply is meaningful.
        v2 = _image_template()
        v2["spec_template"]["goal"] = "show a radar frame with a caption"
        raw_v2, _ = _build_pack(secrets_store, seq=2, templates=[v2])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack", lambda url, cap: raw_v2)
        client.post("/api/ni/library/check")
        r = client.post(f"/api/ni/items/{item_id}/apply-template-update")
        assert r.status_code == 200, r.text
        preview = client.app.state.ni.read_snapshot(item_id, "preview")
        assert preview is not None
        # Rebound scene carries the real per-item image src (not the placeholder).
        image_node = preview["payload"]["children"][0]
        assert image_node["type"] == "image"
        assert image_node["src"].startswith(f"/api/ni/items/{item_id}/image?v=")


def test_export_refuses_internal_schedule_source(
        tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "sched.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        assert client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        # Create an item DIRECTLY via NIStore.add_item that uses internal.schedule
        # (the library install path doesn't ship such items — this is the DIY case).
        spec = {
            "version": 1, "title": "Sched", "goal": "read a schedule",
            "params": {},
            "source": {"type": "internal.schedule", "schedule_id": "sched-1"},
            "pipeline": [], "scene": {"type": "stack", "dir": "v", "gap": "sm",
                                       "children": [{"type": "text", "value": "x",
                                                     "role": "title", "tone": "default",
                                                     "size": "md"}]},
            "display": {"size": "small"}, "contract": None,
            "repair_policy": {"l1": True, "l2_frontier": False}, "model": None,
        }
        item_id = client.app.state.ni.add_item(spec, {}, origin="user")
        r = client.get(f"/api/ni/items/{item_id}/export-template", headers=_LOCAL)
        assert r.status_code == 400
        assert "internal.schedule" in r.json()["detail"]


# --- build.py + validate.py smoke ------------------------------------------

# The publisher/registry tooling lives in the repo's tools/ tree, which the SHIPPED
# app image deliberately does not include (it's operator/CI tooling, not app code).
# The docker-image CI job runs this suite inside that image, so these four
# subprocess-driven tests must skip cleanly there instead of failing on a missing path.
_TOOLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "tools" / "ni-library"
_needs_tooling = pytest.mark.skipif(
    not _TOOLS_DIR.is_dir(), reason="publisher tooling not shipped in the app image")


def _run_build(tmp_path: pathlib.Path, master_key_b64: str,
                pack_id: str = "pack-1", seq: int = 1) -> tuple[pathlib.Path, pathlib.Path]:
    """Invoke tools/ni-library/build.py in a subprocess; return (pack_path, publisher_dir)."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "weather.json").write_text(json.dumps(_template()), encoding="utf-8")
    publisher_dir = tmp_path / "publisher"
    publisher_dir.mkdir()
    pack_path = tmp_path / "pack.json"
    env = dict(os.environ)
    env["SB_PUBLISHER_MASTER_KEY"] = master_key_b64
    script = pathlib.Path(__file__).resolve().parents[2] / "tools" / "ni-library" / "build.py"
    result = subprocess.run(
        [sys.executable, str(script),
         "--templates", str(templates_dir),
         "--pack-id", pack_id,
         "--seq", str(seq),
         "--publisher-data-dir", str(publisher_dir),
         "--out", str(pack_path)],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"build failed: {result.stdout}\n{result.stderr}"
    return pack_path, publisher_dir


@_needs_tooling
def test_build_py_signs_a_pack_that_verifies(tmp_path) -> None:
    """Full loop: build.py signs → parse_pack/verify_pack accept the resulting bytes."""
    key = base64.b64encode(gen_master_key()).decode("ascii")
    pack_path, _ = _run_build(tmp_path, key)
    raw = pack_path.read_bytes()
    payload = ni_library.parse_pack(raw)
    verdict = ni_library.verify_pack(raw, payload["publisher"]["pubkey"], "pack-1", 0)
    assert verdict["behind"]


@_needs_tooling
def test_build_py_output_refuses_after_byte_flip(tmp_path) -> None:
    """A tampered byte in the built pack must be refused by verify_pack."""
    key = base64.b64encode(gen_master_key()).decode("ascii")
    pack_path, _ = _run_build(tmp_path, key)
    raw = pack_path.read_bytes()
    payload = ni_library.parse_pack(raw)
    tampered = raw.replace(b"Weather", b"Poisoned", 1)
    assert tampered != raw
    with pytest.raises(ni_library.LibraryError):
        ni_library.verify_pack(tampered, payload["publisher"]["pubkey"], "pack-1", 0)


@_needs_tooling
def test_validate_py_accepts_good_and_rejects_bad(tmp_path) -> None:
    """validate.py exits 0 on a good template dir, 1 on a bad one."""
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    (good_dir / "weather.json").write_text(json.dumps(_template()), encoding="utf-8")
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad = _template()
    bad["spec_template"]["source"] = {"type": "not-a-source"}
    (bad_dir / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    script = pathlib.Path(__file__).resolve().parents[2] / "tools" / "ni-library" / "validate.py"
    good = subprocess.run([sys.executable, str(script), "--templates", str(good_dir)],
                          capture_output=True, text=True, check=False)
    assert good.returncode == 0, good.stdout + good.stderr
    poisoned = subprocess.run([sys.executable, str(script), "--templates", str(bad_dir)],
                              capture_output=True, text=True, check=False)
    assert poisoned.returncode == 1


@_needs_tooling
def test_validate_py_empty_dir_exits_nonzero(tmp_path) -> None:
    """LOW#2 (audit 2026-09-09): an empty template directory is a broken PR, not a
    passing CI — validate.py must exit nonzero so registry-repo CI catches it."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    script = pathlib.Path(__file__).resolve().parents[2] / "tools" / "ni-library" / "validate.py"
    result = subprocess.run([sys.executable, str(script), "--templates", str(empty_dir)],
                             capture_output=True, text=True, check=False)
    assert result.returncode == 1, "empty dir must fail CI"
    assert "no *.json" in result.stderr.lower()


# --- LOW#6 (audit 2026-09-09): parse_pack REJECTS system-owned keys in templates ----

@pytest.mark.parametrize("forbidden_key", [
    "contract", "_c2_ok", "_l1_last_attempt", "_l1_trial", "_template",
])
def test_parse_pack_refuses_template_with_system_owned_keys(forbidden_key: str) -> None:
    """Templates ship the spec's user-authored surface only — engine/consent-owned
    keys must land at install/commissioning time from the app, not from a pack."""
    secrets, _, _, _ = _stores()
    template = _template()
    # A None VALUE is enough to trip the refusal — the check is "key present".
    template["spec_template"][forbidden_key] = None
    payload = _pack_payload(identity.public_key_b64(secrets, identity.NI_PUBLISHER_SECRET),
                            templates=[template])
    raw = _sign_pack(secrets, payload)
    with pytest.raises(ni_library.LibraryError, match="system-owned keys"):
        ni_library.parse_pack(raw)


# --- M3 (audit 2026-09-09): rollback stamps last_checked, doesn't escalate ----------

def test_record_rollback_stamps_last_checked_without_escalation() -> None:
    """Rollback is host-answered, not host-unreachable: last_checked advances (the sheet
    stops the 30s refetch loop), the failure counter stays at 0, and the persisted
    status names the version drift truthfully."""
    import datetime as _dt
    secrets, ni_store, _, _ = _stores()
    raw, _ = _build_pack(secrets)
    library = ni_library.LibraryStore(ni_store, netguard_mod=_fake_fetcher(raw))
    library.connect("https://a.example.com/pack.json")
    before = library.source()
    assert int(before["consecutive_failures"]) == 0
    stamp = _dt.datetime(2027, 1, 1, tzinfo=_dt.UTC)
    transitioned = library.record_rollback(3, 5, now=stamp)
    assert transitioned is True
    pin = library.source()
    assert int(pin["consecutive_failures"]) == 0, "rollback must NOT count as unreachable"
    assert pin.get("unreachable") is None
    assert pin["last_checked"] == stamp.isoformat()
    assert "v3" in pin["last_error"] and "v5" in pin["last_error"]
    # Second identical rollback → not a fresh transition (LOW#5 dedupe)
    again = library.record_rollback(3, 5, now=stamp)
    assert again is False


def test_is_due_false_immediately_after_rollback() -> None:
    """The stamped last_checked stops the 30s refetch loop; is_due is False until the
    normal interval elapses (M3)."""
    secrets, ni_store, _, _ = _stores()
    raw, _ = _build_pack(secrets)
    library = ni_library.LibraryStore(ni_store, netguard_mod=_fake_fetcher(raw))
    library.connect("https://a.example.com/pack.json")
    library.record_rollback(3, 5)
    assert not library.is_due()


# --- H2 (audit 2026-09-09): end-to-end install → credential → commission → run ------

def test_install_puts_credential_commissions_and_runs_end_to_end(
        tmp_path, monkeypatch) -> None:
    """The flagship authenticated-template path: install an http_json template whose
    header rides ``$secret: ni:self:api_key``, PUT the credential, commission, then
    run — the run's fetch MUST attach the credential.

    Fails against the pre-audit code: ``ni:self:api_key`` never got rewritten to the
    concrete ``ni:<item_id>:api_key`` key, so ``_load_credential`` raised
    ``secret_not_scoped`` on every fetch attempt.
    """
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "auth.duckdb"))
    from smartbrain_3000 import main as _main
    from smartbrain_3000 import netguard as _ng
    from smartbrain_3000 import ni as _ni
    with TestClient(_main.create_app()) as client:
        assert client.post("/api/account/setup",
                           json={"passphrase": "correct-horse"}).status_code == 200
        secrets_store = client.app.state.secret_store
        template = _template()
        template["spec_template"]["params"]["api_key"] = {
            "label": "API key", "kind": "secret", "value": "ni:self:api_key"}
        template["spec_template"]["source"] = {
            "type": "http_json",
            "url": "https://api.example.com/v1/weather",
            "headers": {"authorization": {"$secret": "ni:self:api_key"}},
        }
        raw, _ = _build_pack(secrets_store, templates=[template])
        monkeypatch.setattr(_ng, "safe_fetch_ni_pack", lambda url, cap: raw)
        client.post("/api/ni/library/connect", headers=_LOCAL,
                    json={"url": "https://a.example.com/pack.json"})
        r = client.post("/api/ni/library/install",
                        json={"template_id": "weather-basic", "params": {}})
        assert r.status_code == 200, r.text
        item_id = r.json()["item_id"]
        # Store the credential — bound to the source host
        r = client.put(f"/api/ni/items/{item_id}/credential",
                       json={"name": "api_key", "value": "sk-live-42",
                             "host": "api.example.com"},
                       headers=_LOCAL)
        assert r.status_code == 200, r.text
        r = client.post(f"/api/ni/items/{item_id}/commission")
        assert r.status_code == 200, r.text
        # Fake the fetch and record what headers arrived
        seen: dict = {}

        def _fake_fetch(url, headers=None, allow_redirects=True):
            seen["url"] = url
            seen["headers"] = dict(headers or {})
            return {"ok": True}
        monkeypatch.setattr(_ng, "safe_fetch_json", _fake_fetch)
        r = client.post(f"/api/ni/items/{item_id}/run")
        assert r.status_code == 200, r.text
        # Fetch actually attached the credential (the whole point of the rewrite)
        assert seen["headers"].get("authorization") == "sk-live-42", seen
        assert _ni._NI_SELF_PLACEHOLDER not in json.dumps(seen)


def test_install_seals_rewritten_ni_self_refs_in_item_spec(
        tmp_path, monkeypatch) -> None:
    """H2: the installed item's sealed spec carries the concrete ``ni:<item_id>:<name>``
    everywhere the template shipped ``ni:self:<name>`` — header refs AND secret
    param VALUES. This is the read-side counterpart to the run-time test above."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "seal.duckdb"))
    from smartbrain_3000 import main as _main
    with TestClient(_main.create_app()) as client:
        assert client.post("/api/account/setup",
                           json={"passphrase": "correct-horse"}).status_code == 200
        secrets_store = client.app.state.secret_store
        template = _template()
        template["spec_template"]["params"]["api_key"] = {
            "label": "API key", "kind": "secret", "value": "ni:self:api_key"}
        template["spec_template"]["source"] = {
            "type": "http_json",
            "url": "https://api.example.com/v1/x",
            "headers": {"authorization": {"$secret": "ni:self:api_key"}},
        }
        raw, _ = _build_pack(secrets_store, templates=[template])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack", lambda url, cap: raw)
        client.post("/api/ni/library/connect", headers=_LOCAL,
                    json={"url": "https://a.example.com/pack.json"})
        item_id = client.post("/api/ni/library/install",
                              json={"template_id": "weather-basic",
                                    "params": {}}).json()["item_id"]
        item = client.get(f"/api/ni/items/{item_id}").json()
        spec = item["spec"]
        expected = f"ni:{item_id}:api_key"
        assert spec["source"]["headers"]["authorization"]["$secret"] == expected
        assert spec["params"]["api_key"]["value"] == expected


# --- H2 export inverse: ni:<item_id>: -> ni:self: + no UUID in emitted JSON ---------

def test_export_template_rewrites_item_refs_back_to_self(
        tmp_path, monkeypatch) -> None:
    """§21: the exported template must round-trip through install (no UUID in the
    header ref OR the secret param value). Guarantees (a) subscribers can install
    the exported file cleanly, and (b) the item's UUID never leaks in the JSON."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "export2.duckdb"))
    from smartbrain_3000 import main as _main
    with TestClient(_main.create_app()) as client:
        assert client.post("/api/account/setup",
                           json={"passphrase": "correct-horse"}).status_code == 200
        secrets_store = client.app.state.secret_store
        template = _template()
        template["spec_template"]["params"]["api_key"] = {
            "label": "API key", "kind": "secret", "value": "ni:self:api_key"}
        template["spec_template"]["source"] = {
            "type": "http_json",
            "url": "https://api.example.com/v1/x",
            "headers": {"authorization": {"$secret": "ni:self:api_key"}},
        }
        raw, _ = _build_pack(secrets_store, templates=[template])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack", lambda url, cap: raw)
        client.post("/api/ni/library/connect", headers=_LOCAL,
                    json={"url": "https://a.example.com/pack.json"})
        item_id = client.post("/api/ni/library/install",
                              json={"template_id": "weather-basic",
                                    "params": {}}).json()["item_id"]
        r = client.get(f"/api/ni/items/{item_id}/export-template", headers=_LOCAL)
        assert r.status_code == 200, r.text
        body_text = r.text
        # The item id must not appear anywhere in the exported JSON
        assert item_id not in body_text, "H2 export: item UUID leaked"
        exported = r.json()
        spec = exported["spec_template"]
        assert spec["source"]["headers"]["authorization"]["$secret"] == "ni:self:api_key"
        assert spec["params"]["api_key"]["value"] == "ni:self:api_key"


# --- H3 (audit 2026-09-09): DELETE /api/ni/library disconnects ---------------------

def test_disconnect_via_delete_route(tmp_path, monkeypatch) -> None:
    """The frontend sends DELETE /api/ni/library — backend route must accept it."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "disc.duckdb"))
    from smartbrain_3000 import main as _main
    with TestClient(_main.create_app()) as client:
        assert client.post("/api/account/setup",
                           json={"passphrase": "correct-horse"}).status_code == 200
        secrets_store = client.app.state.secret_store
        raw, _ = _build_pack(secrets_store)
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack", lambda url, cap: raw)
        client.post("/api/ni/library/connect", headers=_LOCAL,
                    json={"url": "https://a.example.com/pack.json"})
        r = client.delete("/api/ni/library", headers=_LOCAL)
        assert r.status_code == 200, r.text
        # Second delete with no source → 409
        r = client.delete("/api/ni/library", headers=_LOCAL)
        assert r.status_code == 409


# --- M1 (audit 2026-09-09): template previews are the BOUND scene ------------------

def test_library_template_preview_payload_is_bound_scene(
        tmp_path, monkeypatch) -> None:
    """The library sheet's ``preview_payload`` is what the client renders under the
    template card. Pre-audit it was the raw dummy data (not bindable by the scene
    renderer); post-audit it is the bound scene tree — same shape the item's
    preview slot returns after add_item."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "preview.duckdb"))
    from smartbrain_3000 import main as _main
    with TestClient(_main.create_app()) as client:
        assert client.post("/api/account/setup",
                           json={"passphrase": "correct-horse"}).status_code == 200
        secrets_store = client.app.state.secret_store
        raw, _ = _build_pack(secrets_store)
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack", lambda url, cap: raw)
        r = client.post("/api/ni/library/connect", headers=_LOCAL,
                        json={"url": "https://a.example.com/pack.json"})
        assert r.status_code == 200, r.text
        state = r.json()
        row = state["templates"][0]
        # BOUND scene: keys "type" + "children" at root (a stack), not raw {"title": "preview title"}
        pv = row["preview_payload"]
        assert isinstance(pv, dict) and pv.get("type") == "stack"
        assert "title" not in pv


# --- M2 (audit 2026-09-09): apply-template-update handles param removal / re-kinding

def test_apply_template_update_drops_removed_and_re_kinded_params(
        tmp_path, monkeypatch) -> None:
    """A carried value whose name is absent from the new template (renamed / removed)
    or whose new kind is 'secret' must be DROPPED — not passed to build_installed_spec
    which would 400 the whole apply. Fleet healing survives publisher param renames."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "m2.duckdb"))
    from smartbrain_3000 import main as _main
    with TestClient(_main.create_app()) as client:
        assert client.post("/api/account/setup",
                           json={"passphrase": "correct-horse"}).status_code == 200
        secrets_store = client.app.state.secret_store
        # v1 template with two string params
        v1 = _template()
        v1["spec_template"]["params"]["region"] = {
            "label": "Region", "kind": "string", "value": ""}
        raw_v1, _ = _build_pack(secrets_store, seq=1, templates=[v1])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack", lambda url, cap: raw_v1)
        client.post("/api/ni/library/connect", headers=_LOCAL,
                    json={"url": "https://a.example.com/pack.json"})
        item_id = client.post("/api/ni/library/install",
                              json={"template_id": "weather-basic",
                                    "params": {"zip": "94103",
                                                "region": "us-west"}}).json()["item_id"]
        # v2 REMOVES `region` and PROMOTES `zip` to secret — both would 400 pre-audit
        v2 = _template()
        v2["spec_template"]["params"]["zip"] = {
            "label": "API key", "kind": "secret", "value": "ni:self:zip"}
        raw_v2, _ = _build_pack(secrets_store, seq=2, templates=[v2])
        monkeypatch.setattr(netguard, "safe_fetch_ni_pack", lambda url, cap: raw_v2)
        client.post("/api/ni/library/check")
        r = client.post(f"/api/ni/items/{item_id}/apply-template-update")
        assert r.status_code == 200, r.text
        item = client.get(f"/api/ni/items/{item_id}").json()
        # `region` removed; `zip` re-kinded → carried value dropped, placeholder rewritten
        assert "region" not in (item["spec"]["params"] or {})
        assert item["spec"]["params"]["zip"]["kind"] == "secret"
        assert item["spec"]["params"]["zip"]["value"] == f"ni:{item_id}:zip"


# --- M5 (audit 2026-09-09): export credential guard covers URL path, %-decode, llm ---

def _install_and_put_credential(client: TestClient, monkeypatch, template: dict,
                                cred_value: str, host: str) -> str:
    """Helper for the M5 tests: install a template + PUT one credential; return item_id."""
    secrets_store = client.app.state.secret_store
    raw, _ = _build_pack(secrets_store, templates=[template])
    monkeypatch.setattr(netguard, "safe_fetch_ni_pack", lambda url, cap: raw)
    client.post("/api/ni/library/connect", headers=_LOCAL,
                json={"url": "https://a.example.com/pack.json"})
    item_id = client.post("/api/ni/library/install",
                          json={"template_id": "weather-basic",
                                "params": {}}).json()["item_id"]
    client.put(f"/api/ni/items/{item_id}/credential",
               json={"name": "api_key", "value": cred_value, "host": host},
               headers=_LOCAL)
    return item_id


def test_export_refuses_credential_value_in_url_path_segment(
        tmp_path, monkeypatch) -> None:
    """M5: a credential pasted into a URL path segment (not just query) is refused."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "m5path.duckdb"))
    from smartbrain_3000 import main as _main
    with TestClient(_main.create_app()) as client:
        assert client.post("/api/account/setup",
                           json={"passphrase": "correct-horse"}).status_code == 200
        template = _template()
        template["spec_template"]["params"]["api_key"] = {
            "label": "API key", "kind": "secret", "value": "ni:self:api_key"}
        template["spec_template"]["source"] = {
            "type": "http_json",
            # Credential in the PATH — pre-audit code only checked the query.
            "url": "https://example.com/tokens/super-secret-value/refresh",
            "headers": {},
        }
        item_id = _install_and_put_credential(
            client, monkeypatch, template, "super-secret-value", "example.com")
        r = client.get(f"/api/ni/items/{item_id}/export-template", headers=_LOCAL)
        assert r.status_code == 400
        assert "URL" in r.json()["detail"]


def test_export_refuses_credential_value_percent_encoded_in_url(
        tmp_path, monkeypatch) -> None:
    """M5: percent-encoded credential in URL is caught via unquote()."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "m5pct.duckdb"))
    from smartbrain_3000 import main as _main
    with TestClient(_main.create_app()) as client:
        assert client.post("/api/account/setup",
                           json={"passphrase": "correct-horse"}).status_code == 200
        template = _template()
        template["spec_template"]["params"]["api_key"] = {
            "label": "API key", "kind": "secret", "value": "ni:self:api_key"}
        # "sk-secret" percent-encoded as "sk%2Dsecret" (safe chars unchanged; %2D = '-')
        template["spec_template"]["source"] = {
            "type": "http_json",
            "url": "https://example.com/api?token=sk%2Dsecret",
            "headers": {},
        }
        item_id = _install_and_put_credential(
            client, monkeypatch, template, "sk-secret", "example.com")
        r = client.get(f"/api/ni/items/{item_id}/export-template", headers=_LOCAL)
        assert r.status_code == 400
        assert "URL" in r.json()["detail"]


def test_export_refuses_credential_value_in_llm_instruction(
        tmp_path, monkeypatch) -> None:
    """M5: credential pasted into an llm-stage instruction is refused."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "m5llm.duckdb"))
    from smartbrain_3000 import main as _main
    with TestClient(_main.create_app()) as client:
        assert client.post("/api/account/setup",
                           json={"passphrase": "correct-horse"}).status_code == 200
        template = _template()
        template["spec_template"]["params"]["api_key"] = {
            "label": "API key", "kind": "secret", "value": "ni:self:api_key"}
        # Give the pipeline an extract stage that emits an output, then the llm stage
        # references it — extract stage populates outputs so llm's `output` isn't a
        # namespace collision. But we're only checking the sanitizer; the pipeline
        # must be parse-valid because parse_pack runs the full spec validator.
        template["spec_template"]["pipeline"] = [
            {"op": "extract", "paths": {"text": "content"}},
            {"op": "llm",
             "instruction": "Analyze this content — the API key is my-cred",
             "output": {"summary": "string"}},
        ]
        # Scene renders one of the outputs so the preview is valid; adjust scene
        template["spec_template"]["scene"] = {
            "type": "stack", "dir": "v", "gap": "sm", "children": [
                {"type": "text", "value": "{{summary}}", "role": "title",
                 "tone": "default", "size": "md"}]}
        template["preview_payload"] = {"content": "x", "summary": "s"}
        item_id = _install_and_put_credential(
            client, monkeypatch, template, "my-cred", "example.com")
        r = client.get(f"/api/ni/items/{item_id}/export-template", headers=_LOCAL)
        assert r.status_code == 400
        assert "llm" in r.json()["detail"].lower()


# --- M6 (audit 2026-09-09): export refuses internal.kb ------------------------------

def test_export_refuses_internal_kb_source(tmp_path, monkeypatch) -> None:
    """internal.kb items carry the user's personal search query — refuse export."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "m6.duckdb"))
    from smartbrain_3000 import main as _main
    with TestClient(_main.create_app()) as client:
        assert client.post("/api/account/setup",
                           json={"passphrase": "correct-horse"}).status_code == 200
        spec = {
            "version": 1, "title": "KB item", "goal": "search my knowledge base",
            "params": {},
            "source": {"type": "internal.kb", "query": "quarterly spending", "limit": 5},
            "pipeline": [{"op": "extract", "paths": {"count": "results[0].title"}}],
            "scene": {"type": "stack", "dir": "v", "gap": "sm", "children": [
                {"type": "text", "value": "{{count}}", "role": "title",
                 "tone": "default", "size": "md"}]},
            "display": {"size": "small"}, "contract": None,
            "repair_policy": {"l1": True, "l2_frontier": False}, "model": None,
        }
        # preview_payload is the POST-pipeline shape (what the scene binds against),
        # not the raw fetch payload — add_item calls bind_scene(scene, preview_payload).
        item_id = client.app.state.ni.add_item(
            spec, {"count": "x"}, origin="user")
        r = client.get(f"/api/ni/items/{item_id}/export-template", headers=_LOCAL)
        assert r.status_code == 400
        assert "internal.kb" in r.json()["detail"]


# --- LOW#7 (audit 2026-09-09): pre-phase-3 export message names the cause ----------

def test_export_pre_phase3_item_gives_named_error(tmp_path, monkeypatch) -> None:
    """Items whose ``preview_data`` slot was never written (pre-Phase-3) refuse with
    a clear "created before previews were stored" message — not the generic
    "preview_payload does not bind" from the round-trip validator."""
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "prep3.duckdb"))
    from smartbrain_3000 import main as _main
    with TestClient(_main.create_app()) as client:
        assert client.post("/api/account/setup",
                           json={"passphrase": "correct-horse"}).status_code == 200
        spec = {
            "version": 1, "title": "Legacy", "goal": "a legacy item",
            "params": {}, "source": {"type": "model", "instruction": "hi"},
            "pipeline": [],
            "scene": {"type": "stack", "dir": "v", "gap": "sm", "children": [
                {"type": "text", "value": "{{title}}", "role": "title",
                 "tone": "default", "size": "md"}]},
            "display": {"size": "small"}, "contract": None,
            "repair_policy": {"l1": True, "l2_frontier": False}, "model": None,
        }
        item_id = client.app.state.ni.add_item(spec, {"title": "hi"}, origin="user")
        # Simulate pre-Phase-3 storage: nuke the preview_data slot.
        client.app.state.db.execute(
            "DELETE FROM ni_snapshots WHERE item_id = ? AND slot = ?;",
            [item_id, "preview_data"])
        r = client.get(f"/api/ni/items/{item_id}/export-template", headers=_LOCAL)
        assert r.status_code == 400
        assert "before previews were stored" in r.json()["detail"]
