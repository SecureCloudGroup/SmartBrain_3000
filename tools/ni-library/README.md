# Neural Interface Global Library — publisher tools

Two scripts:

- **`build.py`** — the operator's publish tool. Reads one JSON per template from a
  directory, assembles the §19 pack, signs it with the `ni:publisher_ed25519` key
  from a publisher-instance SecretStore, round-trips it through the same
  `ni_library.parse_pack` subscribers run, and writes the signed envelope to
  `landing/ni/library-pack.json` (or wherever `--out` points). See
  [`docs/internal/ni-format.md`](../../docs/internal/ni-format.md) §19–§21.

- **`validate.py`** — the registry-repo CI helper. Runs `_validate_one_template`
  against every `*.json` in a directory; exits nonzero on any failure. Pin
  `smartbrain_3000` in the registry repo and run this in CI on every PR.

## build.py

Offline signing (the one supported publish path in v1):

```
# Feed the key over a here-string so the value never lands in shell history —
# prefer `printenv | grep -v SB_PUBLISHER_MASTER_KEY` friendly patterns
# (envrc/1Password CLI/`read -s`). NEVER paste the base64 key directly on the
# command line — every keystroke lives in `~/.zsh_history` / `~/.bash_history`.
read -rs SB_PUBLISHER_MASTER_KEY   # then paste; blank prompt hides input
export SB_PUBLISHER_MASTER_KEY
python tools/ni-library/build.py \
    --templates ./registry/templates \
    --pack-id  1c1e0f2a-…-official \
    --seq      3 \
    --publisher-data-dir ./sb_ni_publisher_data \
    --label    "SmartBrain project" \
    --out      landing/ni/library-pack.json
```

The `sb_ni_publisher_data` directory holds a SmartBrain DuckDB whose SecretStore
carries the `ni:publisher_ed25519` key. The FIRST build mints the key (idempotent —
`identity._load_or_create` persists it); every subscriber pins **that** key at
first fetch, so the directory must be preserved across builds. Deleting it orphans
every subscriber (updates read as a key change and block).

The master key rides `SB_PUBLISHER_MASTER_KEY` (base64-encoded 32 raw bytes). This
is the moral equivalent of a signing-key file: store it out of band, treat any
leak as a rotation event (mint a new NI publisher key, re-publish with a bumped
seq; subscribers will see a KeyChanged and confirm the new fingerprint out-of-band
before re-trusting).

Rotation domain is INDEPENDENT from the vault publisher key (`vault:publisher_ed25519`) —
a compromise or rotation of the library-pack identity does not force every vault
subscriber to re-trust their vault pins, and vice versa.

## Template file shape

Each template JSON is exactly one entry from `pack.templates[]`:

```json
{
  "id": "weather-basic",
  "title": "Weather basic",
  "goal": "show the current temperature at your ZIP",
  "category": "weather",
  "tags": ["nws", "us"],
  "spec_template": {
    "version": 1, "title": "Weather", "goal": "…",
    "params": { "zip": {"label": "ZIP", "kind": "string", "value": ""} },
    "source": { … },
    "pipeline": [ … ],
    "scene":   { … },
    "display": { "size": "small" },
    "contract": null,
    "model": null
  },
  "preview_payload": { … dummy data matching the scene’s binds … },
  "notes": "one honest sentence"
}
```

Params:

- String / number params ship with an EMPTY `value` (`""` or `0`) — the
  installing user fills them in the install sheet. The `value` key itself is
  required by the validator; leaving it out fails `parse_pack`.
- Secret params ship with the placeholder `"ni:self:<name>"`; the installed item
  writes the real value under `ni:<item_id>:<name>` via the credential PUT.
- `contract`, `_c2_ok`, `_l1_*`, `_l2_*`, `_template`, `repair_policy` are
  FORBIDDEN in a template — the install path stamps `_template` itself, the
  engine keys are engine-owned (see §20), and `repair_policy` is always the
  installer's LOCAL choice (Phase 4b D2c audit 2026-09-11 — the install path
  forces the safe default `{l1: true, l2_frontier: false}` regardless of what
  a template shipped). `parse_pack` refuses any template that carries them
  (belt AND suspenders alongside `build_installed_spec`'s strip).

## validate.py

Registry CI:

```
python tools/ni-library/validate.py --templates ./registry/templates
```

Exit code is 0 when every template passes, 1 otherwise. Runs the same
`_validate_one_template` (spec + preview-bind) the app runs on `parse_pack`, so a
green CI means every future subscriber will accept the file.
