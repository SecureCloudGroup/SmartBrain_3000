#!/usr/bin/env python3
"""Generate the AUTHENTIC publisher .sbvault files clip 11 (subscribe -> update) serves.

Run inside the dev image (it needs the real smartbrain_3000.vault_format + cryptography):

    docker run --rm -v "$REPO/app:/app" -v "$OUT:/out" -w /app smartbrain_3000:dev \\
        python /gifdemo/make_publisher_vaults.py /out

These are REAL open-mode vaults, packed by the shipped `vault_format.pack` and signed by ONE
generated Ed25519 publisher key (reused for both versions, so v2 verifies against the key the
subscriber pinned from v1 — exactly what a genuine publisher update does). The recorder never
weakens verification; it only serves these bytes in place of a network fetch.

    publisher-v1.sbvault  seq 1  — Onboarding checklist, Deployment runbook, On-call rotation
    publisher-v2.sbvault  seq 2  — same vault_id / name_key / key; Onboarding + Deployment changed,
                                    On-call unchanged, Incident postmortem template ADDED

The subscriber (in the clip) edits "Onboarding checklist" locally before updating, so applying v2
detaches it as "kept (yours)" while Deployment updates and the postmortem template is added — a
single summary that exercises updated + added + kept-yours, all real.
"""
from __future__ import annotations

import sys
import uuid

from smartbrain_3000 import identity, kb, vault_format


class MemStore:
    """The minimal SecretStore identity.* needs: an in-memory get/put. Holding one instance across
    both packs makes _load_or_create generate the publisher key ONCE and reuse it — so v1 and v2
    are signed by the same identity."""

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._d.get(key)

    def put(self, key: str, value: str) -> None:
        self._d[key] = value


def _doc(vault_id: str, doc_id: str, title: str, content: str) -> dict:
    """One document in pack()'s shape, with the same stable uid the export route derives, so the
    SAME logical doc keeps its uid across v1 and v2 (which is what lets the diff recognise it)."""
    return {
        "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"sbvault:{vault_id}:{doc_id}")),
        "title": title,
        "content": content,
        "meta": {},
        "chunks": len(kb.chunk_text(title, content)),
    }


def main(out_dir: str) -> None:
    store = MemStore()
    vault_id = str(uuid.uuid4())
    name_key = __import__("os").urandom(32)  # a born-open vault mints a persistent K_name
    name = "Frontend Team Playbook"
    desc = "Shared onboarding, deploy, and on-call notes for the frontend team."

    onboarding_v1 = (
        "Welcome to the frontend team. Day one: get repo access, install the toolchain, and pair "
        "with your onboarding buddy. Read the design-system docs before your first ticket. Ask "
        "questions early — nobody expects you to know the codebase in week one."
    )
    deploy_v1 = (
        "Deploys run from main after CI is green. Tag the release, watch the canary for ten "
        "minutes, then promote to full. Roll back with the previous tag if error rates climb."
    )
    oncall = (
        "On-call rotates weekly, handed off every Monday at 10:00. The primary acks pages within "
        "five minutes; the secondary is backup. Log every incident in the tracker before you sleep."
    )

    # v1
    docs_v1 = [
        _doc(vault_id, "doc-onboarding", "Onboarding checklist", onboarding_v1),
        _doc(vault_id, "doc-deploy", "Deployment runbook", deploy_v1),
        _doc(vault_id, "doc-oncall", "On-call rotation", oncall),
    ]
    blob_v1 = vault_format.pack(
        store=store, vault_id=vault_id, name=name, description=desc, seq=1,
        docs=docs_v1, name_key=name_key, mode=vault_format.OPEN,
    )

    # v2: onboarding + deploy CHANGED, oncall unchanged (same bytes -> not re-downloaded), one ADDED.
    deploy_v2 = (
        "Deploys run from main after CI is green. Tag the release, watch the canary for ten "
        "minutes, then promote to full. NEW: promotion now requires a second approver in the deploy "
        "channel. Roll back with the previous tag if error rates climb."
    )
    onboarding_v2 = onboarding_v1 + " Updated: your first ticket is now assigned during orientation."
    postmortem = (
        "Incident postmortem template: timeline, impact, root cause, what went well, what to fix, "
        "and action items with owners and dates. Blameless — we fix systems, not people."
    )
    docs_v2 = [
        _doc(vault_id, "doc-onboarding", "Onboarding checklist", onboarding_v2),
        _doc(vault_id, "doc-deploy", "Deployment runbook", deploy_v2),
        _doc(vault_id, "doc-oncall", "On-call rotation", oncall),
        _doc(vault_id, "doc-postmortem", "Incident postmortem template", postmortem),
    ]
    blob_v2 = vault_format.pack(
        store=store, vault_id=vault_id, name=name, description=desc, seq=2,
        docs=docs_v2, name_key=name_key, mode=vault_format.OPEN,
    )

    import os

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "publisher-v1.sbvault"), "wb") as fh:
        fh.write(blob_v1)
    with open(os.path.join(out_dir, "publisher-v2.sbvault"), "wb") as fh:
        fh.write(blob_v2)
    # The recorder starts on v1; it flips this pointer to v2 to simulate the publisher pushing.
    with open(os.path.join(out_dir, "serve.txt"), "w", encoding="utf-8") as fh:
        fh.write("publisher-v1.sbvault\n")

    fp = vault_format.fingerprint(
        identity.public_key_b64(store, identity.VAULT_PUBLISHER_SECRET))
    print(f"vault_id={vault_id}")
    print(f"publisher_fingerprint={fp}")
    print(f"v1={len(blob_v1)}B (seq 1, 3 docs)  v2={len(blob_v2)}B (seq 2, 4 docs)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/out")
