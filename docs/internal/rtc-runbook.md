# RTC node runbook (rtc.securecloudgroup.com)

The node is one VPS running three containers under `~/sb-node/compose/docker-compose.signaling.yml`:
`sb_caddy` (TLS, WS routing, the phone app shell, the landing page), `sb_signaling` (the broker,
`signaling/server.py`), `sb_coturn` (STUN/TURN). Phones load their app from it and every remote
session is brokered through it, so an outage reads to users as "SmartBrain is broken".

## What watches it

- `.github/workflows/rtc-probe.yml` every 30 min: TLS expiry, STUN, and a full synthetic
  pairing through the real node (registration with proof-of-possession, offer/answer,
  DataChannel round trip; once natural, once forcing TURN). Failure opens/updates an issue
  labeled `rtc-outage`; recovery closes it.
- The broker's own app-level healthcheck (`signaling/health.py`, every 30 s). Docker only
  *marks* unhealthy; `deploy/sb-autoheal.timer` (user systemd, every 2 min) restarts
  containers that report unhealthy. Install once: copy `deploy/sb-autoheal.*` to
  `~/.config/systemd/user/` and `systemctl --user enable --now sb-autoheal.timer`.
- `deploy/sb-web-sync.timer` (hourly) pulls the latest `v*` tag's phone shell and landing page.

## When the probe fails

1. **Read the issue.** The probe names the stage: `tls`, `stun`, `pair (natural)`,
   `pair (relay)`.
2. **From your machine** (no SSH needed): `curl -sI https://rtc.securecloudgroup.com/`
   (expect `HTTP/2 200`, `server: Caddy`); the STUN check in `tools/rtc_probe/probe.py`.
3. **On the node** (`ssh sb-signaling`, needs the operator VPN):
   - `docker ps` — all three `Up`; a `(unhealthy)` broker means the handler layer wedged
     (seen once after 7 weeks): `docker restart sb_signaling`.
   - `docker logs --tail 100 sb_signaling` — the broker logs no ids or addresses; look for
     `global phone cap reached`, `refusing duplicate desktop registration`, tracebacks.
   - `docker logs --tail 50 sb_coturn` — allocation errors point at `TURN_SECRET` drift
     between `compose/.env` and the broker's `SIGNALING_TURN_SECRET`.
   - Certificate: Caddy renews automatically; if `tls` fails, `docker logs sb_caddy | grep -i acme`
     and confirm ports 80/443 are reachable.
   - Full restart: `cd ~/sb-node && docker compose -f compose/docker-compose.signaling.yml -f compose/docker-compose.landing.yml up -d`.
   - Desktop-id bindings live in the `sb_signaling_state` volume (`/state/bindings.json`);
     deleting the volume forgets every binding (any Desktop then re-binds on its next
     registration — safe, but do it only when a user is locked out of their own id).
4. **Users during an outage:** Desktops reconnect on their own (1→30 s backoff); phones retry
   six times then show "Desktop unreachable" — they reconnect on the next open. Nothing is
   lost; nothing needs re-pairing.

## Rollout of the proof-of-possession broker (once)

Desktops on releases before the one that signs registrations send a legacy hello. Deploy the
new broker with `SIGNALING_ALLOW_LEGACY=1` in `compose/.env` first (unsigned hellos admitted
only for ids that have no binding yet), wait for users to update, then set it to `0` and
`up -d`. The `sb_signaling_state` volume holds the id→key bindings — keep it across
redeploys. Install the autoheal units once: `cp deploy/sb-autoheal.* ~/.config/systemd/user/ &&
systemctl --user daemon-reload && systemctl --user enable --now sb-autoheal.timer`.

## Deploying a broker change

Files are rsynced from a release tag, never from a branch, and CI never touches the node.
After merging: on the node, `cd ~/sb-node/src/SmartBrain_3000 && git fetch --tags && git
checkout vX.Y.Z`, rsync `signaling/` and `compose/` into `~/sb-node/`, then `docker compose
... up -d --build signaling`. Check the probe passes on the next run (or dispatch it).
