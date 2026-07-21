#!/usr/bin/env bash
# Regenerate a quickstart GIF (or all of them) from the REAL app, deterministically.
#
#   ./run.sh 03          # record + encode clip 03
#   ./run.sh all         # all 10
#
# Prereqs (kept out of the repo on purpose):
#   - docker, with the smartbrain_3000:dev image built (cd .. ; docker build ...).
#   - node + playwright:   npm i playwright && npx playwright install chromium
#   - ffmpeg + gifsicle + python3 on the host.
# Output: ./out/<NN-name>.gif . The mock gateway + a throwaway demo container are
# started for you; nothing touches a real provider, Bifrost, or your data.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PORT=33096; GW=38099; U="http://127.0.0.1:$PORT"; PASS="correct-horse-battery"
# A case map, not `declare -A`: macOS ships bash 3.2, where -A degrades to an indexed array and
# the octal-invalid subscript 08 aborts the assignment list — 08/09/10 silently never existed.
name_of(){ case "$1" in
  01) echo 01-install-to-unlocked;; 02) echo 02-connect-a-model;; 03) echo 03-first-chat;;
  04) echo 04-add-knowledge;;       05) echo 05-approve-an-action;; 06) echo 06-planner;;
  07) echo 07-schedule-a-prompt;;   08) echo 08-pair-a-phone;;      09) echo 09-backup-recovery;;
  10) echo 10-vaults;; 11) echo 11-vault-subscribe;; esac; }

# Extra `docker run` args, empty except for clip 11 (the public-vault subscribe/update demo), which
# mounts the RECORDER-ONLY netguard shim + the served publisher files. The shim serves a real .sbvault
# in place of the network fetch so the localhost URL a demo needs never has to defeat the SSRF guard;
# it lives in tools/gifgen only, is never COPY'd into the image, and only activates under SB_GIFDEMO=1.
SHIM=()
mock_up(){ pgrep -f "mock_gateway.py $GW" >/dev/null 2>&1 || { nohup python3 "$HERE/mock_gateway.py" $GW >/tmp/sb_mockgw.log 2>&1 & sleep 1; }; }
# Build the two authentic publisher vaults once (v1 = 3 docs, v2 = one changed + one added, same key).
gen_publisher(){ local state="$HERE/out/gifdemo"; mkdir -p "$state"
  { [ -f "$state/publisher-v1.sbvault" ] && [ -f "$state/publisher-v2.sbvault" ]; } || \
    docker run --rm -v "$REPO/app:/app" -v "$HERE:/gifdemo" -v "$state:/out" -w /app \
      smartbrain_3000:dev python /gifdemo/make_publisher_vaults.py /out >/dev/null
  printf 'publisher-v1.sbvault\n' > "$state/serve.txt"; }
reset_demo(){ docker rm -f sb_gifdemo >/dev/null 2>&1 || true
  docker run -d --name sb_gifdemo -p 127.0.0.1:$PORT:33000 --add-host host.docker.internal:host-gateway \
    -v "$REPO/app:/app" ${SHIM[@]+"${SHIM[@]}"} -e SMARTBRAIN_DB_PATH=/tmp/demo.duckdb -e SMARTBRAIN_HOST=0.0.0.0 \
    -e SMARTBRAIN_WEBRTC_ENABLED=0 -e SMARTBRAIN_LLM_GATEWAY_URL=http://host.docker.internal:$GW \
    smartbrain_3000:dev >/dev/null
  for i in $(seq 1 40); do curl -fsS $U/api/health >/dev/null 2>&1 && break; sleep 1; done; }
setup(){ curl -fsS -X POST $U/api/account/setup -H 'content-type: application/json' -d "{\"passphrase\":\"$PASS\"}"; }
connect(){ curl -fsS -X PUT $U/api/local-models/ollama -H 'content-type: application/json' -d '{"url":"http://host.docker.internal:11434"}' >/dev/null; }
task(){ curl -fsS -X POST $U/api/tasks -H 'content-type: application/json' -d "$1" >/dev/null; }
doc(){ curl -fsS -X POST $U/api/kb -H 'content-type: application/json' -d "$1" >/dev/null; }
enc(){ local out="$1" fps="${2:-12}" scale="${3:-960}" lossy="${4:-55}" colors="${5:-120}" webm; cd "$HERE"; webm=$(ls -t video/*.webm|head -1)
  ffmpeg -y -loglevel error -i "$webm" -vf "fps=$fps,scale=$scale:-1:flags=lanczos,palettegen=max_colors=120:stats_mode=diff" /tmp/sbpal.png
  ffmpeg -y -loglevel error -i "$webm" -i /tmp/sbpal.png -lavfi "fps=$fps,scale=$scale:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=none" /tmp/sbraw.gif
  gifsicle -O3 --lossy=$lossy --colors $colors /tmp/sbraw.gif -o "out/$out.gif"; echo "  -> out/$out.gif ($(du -h out/$out.gif|cut -f1))"; }

record(){ local n="$1"; mock_up; mkdir -p "$HERE/out"; rm -rf "$HERE/video"; mkdir -p "$HERE/video"; cd "$HERE"; local RK=""
  case "$n" in
    01) reset_demo ;;                                   # fresh, NOT set up (records first-run)
    02) curl -fsS "http://127.0.0.1:$GW/reset" >/dev/null; reset_demo; setup >/dev/null ;;  # detected, not connected
    03|04|05) reset_demo; setup >/dev/null; connect ;;  # model connected
    06) reset_demo; setup >/dev/null
        task "{\"title\":\"Call the dentist\",\"due_date\":\"$(python3 -c 'import datetime;print(datetime.date.today())')\"}"
        task "{\"title\":\"Submit expense report\",\"due_date\":\"$(python3 -c 'import datetime;print(datetime.date.today()+datetime.timedelta(days=3))')\"}" ;;
    07) reset_demo; setup >/dev/null; connect
        task '{"title":"Call the dentist"}'; task '{"title":"Submit expense report"}' ;;
    08) reset_demo; setup >/dev/null ;;
    09) reset_demo; RK=$(setup | python3 -c 'import sys,json;print(json.load(sys.stdin)["recovery_key"])') ;;
    10) reset_demo; setup >/dev/null; connect
        doc '{"title":"Apartment Lease","content":"Lease term 12 months, rent $1,800/mo due on the 1st, 60-day notice to vacate, landlord Pat Rivera."}'
        doc '{"title":"Renters insurance policy","content":"Policy RE-2210 with Homestead Mutual. Coverage: $30,000 personal property, $100,000 liability. Premium $14/mo, renews March 1."}'
        doc '{"title":"Moving-day checklist","content":"Transfer utilities, forward mail at USPS, photograph every room at move-in, return the old keys by the 30th."}' ;;
    11) gen_publisher
        SHIM=(-v "$HERE/gifdemo_shim:/gifdemo:ro" -v "$HERE/out/gifdemo:/gifdemo-state:ro"
              -e SB_GIFDEMO=1 -e PYTHONPATH=/gifdemo -e SB_GIFDEMO_STATE=/gifdemo-state)
        reset_demo; setup >/dev/null; connect ;;  # subscriber instance; the shim serves the publisher
  esac
  RECOVERY_KEY="$RK" DEMO_PASS="$PASS" node clips.js "$n"
  # Clip 10 (the public-vault publish story) is content-heavy; a firmer lossy/color budget keeps it
  # under the ~2.6 MB target at the same 10fps/900px as 06/08.
  # 10 (publish) and 11 (subscribe/update) are content- and scroll-heavy; a firmer lossy/color budget
  # keeps them under the ~2.6 MB target at 10fps/900px. 11 is the tighter of the two.
  case "$n" in 06|08) enc "$(name_of "$n")" 10 900 ;; 10) enc "$(name_of "$n")" 10 900 100 100 ;; 11) enc "$(name_of "$n")" 10 900 180 60 ;; *) enc "$(name_of "$n")" ;; esac
}

shots(){ # deterministic screenshot grid of every route (design-PR before/after evidence)
  mock_up; reset_demo; setup >/dev/null; connect
  task '{"title":"Call the dentist"}'; task '{"title":"Submit expense report"}'
  doc '{"title":"Apartment Lease","content":"Lease term 12 months, rent $1,800/mo due on the 1st, 60-day notice to vacate, landlord Pat Rivera."}'
  doc '{"title":"Renters insurance policy","content":"Policy RE-2210 with Homestead Mutual. Coverage: $30,000 personal property, $100,000 liability. Premium $14/mo, renews March 1."}'
  cd "$HERE"; node shots.js
}

if [ "${1:-}" = "all" ]; then for n in 01 02 03 04 05 06 07 08 09 10 11; do echo "### $n"; record "$n"; done
elif [ "${1:-}" = "shots" ]; then shots
elif [ -n "$(name_of "${1:-}")" ]; then record "$1"
else echo "usage: $0 <01..10|all|shots>"; exit 1; fi
echo "done. (cleanup: docker rm -f sb_gifdemo; pkill -f mock_gateway.py)"
