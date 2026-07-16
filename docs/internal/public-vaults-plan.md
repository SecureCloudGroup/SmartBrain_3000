# Public/Hosted Vaults — staged implementation plan (W4.6/4.7)

> Generated 2026-07-15 by a planning pass over docs/internal/vault-format.md + the shipped sealed
> code. The sealed half (export/import/re-seal) is shipped and field-tested. This plans the OPEN
> ("there is no Vault Key") half: publish to a URL, subscribe, delta-update, auto-update.

## Precondition (must land first — silent security regression otherwise)
`VaultStore.update` / `remember_key` whitelist-rebuild the encrypted vault body (`{name, description,
source?, key?}`). Any new body field (the publisher pin!) is silently dropped on the next rename.
Refactor both to read-modify-write, preserving unknown keys, BEFORE Stage C.

## Stages (each independently shippable + testable)
- **A — Open mode in vault_format (pack+open)** [M]. Flag, not rewrite: open index = raw canonical
  JSON, plaintext objects, manifest carries name/description/name_key and NO crypto; every non-crypto
  check identical. Object names stay HMAC(K_name,…) so a sealed→open flip is byte-identical.
- **B — Publish flow** [S]. export mode=open (same desktop-local+reauth gate, no key minted);
  hosting is docs not an uploader (unzipped tree = the same files). "Public: anyone with the link
  reads everything, no key, no take-backs."
- **C — Add-vault-by-URL + TOFU pinning + netguard** [L]. safe_fetch_vault (zip content-types, 512MiB,
  fragment-strip + never log full URL); POST /api/vaults/subscribe; pin in encrypted vault body
  `source`; migration 23 = encrypted per-doc {uid,hash} on vault_documents; enforce vault-owned docs
  read-only + Detach; audit ingress; provenance banner on imported-doc tool output.
- **D — Check-for-updates + apply** [L, splittable D1 zip / D2 tree-delta]. Verify order §5: version →
  vault_id==pin → sig against the PINNED key → seq>pinned. All-or-nothing txn; kb.replace() in place
  (id-preserving, drop embeddings to re-index); owner/edited docs skipped+reported; key change → block
  + surface both fingerprints + trust-publisher to re-pin.
- **E — Scheduled auto-update opt-in** [M]. source.auto_update (default OFF), ≥1h floor, ≤2/tick,
  unlocked-only; NEVER auto-apply across a key change; results into the schedule_runs feed (carrier row).
- **F — UI surfaces** [M]. subscription card (fingerprint always beside any label), key-change dialog
  (the one interruption), publish/hosting panel, imported-content provenance in approvals.

## Safe deferrals
rotated_from key-rotation cert → v1.1 (manual confirm dialog is the baseline; reserve the branch).
Sealed-hosted `#k=` URL subscribe → defer entirely (v1 URL-subscribe = open only), but ship the
fragment hygiene now. Zip-host Range checks → v2.

## Decisions the spec doesn't make (baked into the stages)
1. Locally-edited imported docs at update time → hash mismatch ⇒ treat as owner-edited (skip, flip
   origin→owner, report kept_yours). 2. Vault-check results feed via a reserved carrier row (INNER
   JOINs stay valid). 3. Pin storage = encrypted vault body + migration-23 encrypted per-doc hash.
   4. Unsubscribe offers keep-docs vs remove-imported. 5. Refuse dup vault_id / pubkey collisions +
   add the missing duplicate-uid index check (Stage A). 6. A born-open vault must PERSIST its random
   name_key or every republish looks like a full rewrite. 7. Decide whether an unchanged "Export
   update" warns. 8. Zip-host "check" downloads the whole file in v1 (honest UI copy).

Full reasoning + per-stage critical tests + invariants: see the session transcript that generated this.
