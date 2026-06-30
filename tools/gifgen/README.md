# gifgen — quickstart GIF recorder

Regenerates the nine animated quickstart clips in `docs/assets/gifs/` (also copied to
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
./run.sh all       # all nine
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
  `/v1/chat/completions` with tool-calls, `/v1/embeddings`, Bifrost admin, Ollama `/api/tags`, `/reset`).
- `clips.js` — the nine storyboards (`node clips.js 01`…`09`).
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
