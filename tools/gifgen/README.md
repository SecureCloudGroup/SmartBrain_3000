# gifgen — quickstart GIF recorder

Regenerates the twelve animated quickstart clips in `docs/assets/gifs/` (also copied to
`web/static/assets/gifs/` → built into `app/smartbrain_3000/web/assets/gifs/`). The clips are
**build artifacts**: recorded from the real app driving a throwaway demo container against a tiny
**mock OpenAI gateway**, so chat / tool-calls / model lists are deterministic and 100% synthetic —
no real provider key, no real Bifrost, no user data.

## Why a mock gateway

The app routes models through Bifrost (`SMARTBRAIN_LLM_GATEWAY_URL`). `mock_gateway.py` stands in for
it **and** for a host Ollama on `:11434`, so the chat clips render canned replies + the `add_task` /
`kb_search` tool-calls deterministically, in isolation. Pointing the demo at a real Bifrost would leak
your providers and a demo "Save key" would pollute your real gateway — hence the mock.

## Prereqs (intentionally not committed)

```sh
npm i playwright && npx playwright install chromium     # recorder browser
# host tools: ffmpeg, gifsicle, python3, docker
cd .. && docker build -t smartbrain_3000:dev -f ../Dockerfile ..   # the app image (if not built)
```

## Run

```sh
./run.sh 03        # record + encode clip 03 -> out/03-first-chat.gif
./run.sh all       # all twelve
./run.sh docshots  # seven of the eight guide screenshots -> out/docshots/*.png
                   # (04-neural-board comes from ./run.sh 12 — it needs that take's board)
# cleanup:
docker rm -f sb_gifdemo ; pkill -f mock_gateway.py
```

`run.sh` starts the mock + a throwaway `sb_gifdemo` container (host `app/` bind-mounted so it runs the
current code), sets the per-clip state via the API, records with `clips.js`, and encodes with
ffmpeg + gifsicle. Copy `out/*.gif` into `docs/assets/gifs/` + `web/static/assets/gifs/` and
`cd web && npm run build` to refresh the in-app Help.

## Files

- `lib.js` — recorder: a DOM overlay with a synthetic eased cursor, lower-third caption band, step
  pill, click ripple, focus ring, and full-screen title/terminal cards (captured in the video).
- `mock_gateway.py` — stdlib mock OpenAI gateway (`/v1/models`, streaming + non-stream
  `/v1/chat/completions` with tool-calls and the Neural Interface's canned JSON turns, `/v1/embeddings`,
  Bifrost admin, Ollama `/api/tags`, `/reset`).
- `clips.js` — the twelve storyboards (`node clips.js 01`…`12`).
- `gifdemo_shim/` + `gifdemo_state/ni/` — the RECORDER-ONLY fetch shim (clips 11 and 12) and clip 12's
  recorded source responses (see the clip 12 notes below).
- `docshots.js` — the eight committed guide screenshots (docs/assets + web/static/assets); the
  update-banner shot plays the launcher with its own handshake (the launcher header plus the
  local token `run.sh` exports as `SB_LOCAL_TOKEN`, so run it through `run.sh`) — recorder-only,
  nothing in the app is faked.
- `run.sh` — per-clip demo state + record + encode.

## Conventions (see the planning spec in git history of c7c2112)

960px / 12fps, ≤2 MB target (≤3 MB hard for QR-heavy clips), captions = intent not mechanics,
synthetic data only (fake `DEMO-XXXX` Recovery Key, `DEMO42` pairing code, masked `sk-ant-DEMO` key).
Encoding uses a **dither-free** palette — flat UI compresses far better; `dither=bayer` bloats it.

## Gotchas baked into the scripts

- `setup` unlocks globally, so pre-seeding via the API persists into the recorded browser session.
- Emergency-Kit "I've saved it" checkbox is **disabled until Download/Copy** is clicked.
- The Knowledge "write a note" form is **collapsed** — click "…write a note" first.
- Planner's undated group is labeled **"No date"**, not "Later".
- Chat: tapping a suggestion **chip fills the box but doesn't send** — click Send after.
- Clip 02 needs the mock at empty state — `curl :38099/reset` before recording.
- Clip 08: the pairing panel is taller than the space above the caption band, so the band is hidden
  (`opacity:0`) for the QR + `DEMO42` reveal, then restored.
- Clip 09: capture the real Recovery Key from `/api/account/setup` and pass it as `RECOVERY_KEY`.
- Clip 04: upload a FILE (only a file/URL carries a `source`, which is what citation chips show);
  uploads auto-index, so there is no Reindex beat.
- Clip 10: the PUBLIC-vault publish flow — Share → Public → the no-key/no-take-backs warning →
  Export → a Public badge + the real `SB-…` publisher fingerprint (a public identity, deliberately
  NOT redacted) + the version, then Export update to bump it. Needs only `acceptDownloads` (the
  `.sbvault` export); there is no key to redact (a public vault has none) and the passphrase field is
  `type=password` (browser-masked). A fresh vault starts at v1 and each publish increments, so the
  first published version already reads v2. Encoded at a firmer `--lossy=100 --colors 100` (this
  content-heavy clip) to stay under ~2.6 MB at the same 10fps/900px as 06/08.
- Clip 12 (the Neural Interface) is deterministic and offline end to end:
  - **Library pack**: the pack this app pins is mounted read-only at the demo's library dir
    (`/tmp/library`, beside `SMARTBRAIN_DB_PATH`) — `SB_LIBRARY_PACK=<dir with library.duckdb +
    installed.json>`, else `run.sh` installs the pinned pack once into `out/library` with the app's own
    sha256-checked installer. `SMARTBRAIN_LIBRARY_AUTOINSTALL=0` makes a wrong pack fail the take instead
    of downloading; `SMARTBRAIN_LIBRARY_API=` (empty) keeps a tap's Library vote off the real service.
  - **Sources**: `gifdemo_shim/sitecustomize.py` (`_install_ni`, gated on `SB_GIFDEMO=1` +
    `SB_GIFDEMO_NI`) serves `netguard.safe_fetch_json` / `safe_fetch_text` from `gifdemo_state/ni/` —
    `index.json` maps each source address (query sorted, clock-filled dates as `{date}`) to one body
    curl'd once from the keyless public API — and refuses every other netguard fetch (pages, feeds,
    images, search, POSTs); a missing fixture fails loudly (`[gifdemo-ni] MISS <url>` in
    `docker logs sb_gifdemo`). Dates move with the clock: `dates` shifts every `YYYY-MM-DD` so the
    recorded day is today in the source's zone; `epoch_ms` shifts USGS event times by whole days. To
    refresh: curl each `url` in `index.json` into its `file` and update `recorded_at` + the `anchor` /
    `anchor_ms` (the body's first `daily.time` / `metadata.generated`).
  - **Model turns**: `mock_gateway.py` `NI_ASKS` answers the flow's intent, query, PRESENT and FIT
    prompts for the three asks that become cards (the sunset seed, the earthquakes, the UV index); the
    honest-stop ask ("Building permits in Seattle") gets a plain intent, and the Library offers only a
    dataset link for it (web search is refused offline). `run.sh` routes the `ni` capability to the
    mock's local model — without it the cloud default route asks for consent first.
  - **Two waits compressed, never faked**: a new card's first run waits for the scheduler's 30 s tick,
    so the take fires it through the card's own Run now endpoint; and it asks the board to repaint the
    moment the engine reaches its next stop (the SPA's reload-on-visible hook) instead of on its poll.
  - The viewer zone is pinned (`America/Los_Angeles`, the take and the `neural` docshot), the take opens on
    a board that already holds the sunset card (built through the same API calls the taps make), and the
    GIF starts on its title card: the take writes `video/start.txt` and `enc` skips the frames before it.
  - One command, four outputs: `./run.sh 12` writes `out/12-neural-interface.gif` (10fps/900px,
    `--lossy=100 --colors 100`, ~1.7 MB for ~37 s), `out/posters/12-neural-interface.poster.png` (its
    first frame, the title card), an 8-frame contact sheet `out/sheets/12-neural-interface.png`, and the
    board guide shot `out/docshots/04-neural-board.png` (docshots.js `neural`, which waits for the three
    live cards rather than network idle).
