# Keeping the phone PWA (and landing) current — VPS pull

The phone loads the app **shell** from the always-on origin (`rtc.securecloudgroup.com`), not from the
Desktop. That shell is a **separate copy** from the Desktop's Docker image, so it has to be updated on
its own — otherwise it drifts (it once sat ~3 weeks stale, pre-vaults).

**Model: the VPS pulls itself.** A systemd timer on the VPS periodically fetches the latest release of
the public repo and rsyncs the committed, already-built shell + landing into the Caddy-served dirs. The
VPS only ever reaches **out** to GitHub — nothing reaches **in**. So this needs **no GitHub secrets and
no inbound SSH**, which is the whole point (CI must never touch the VPS).

- `deploy/vps-sync-web.sh` — the sync script (checks out the latest `v*` tag, guards against an empty
  build, rsyncs `app/smartbrain_3000/web/` → the served web dir and `landing/` → the landing dir).
- `deploy/sb-web-sync.{service,timer}` — a user-level systemd oneshot + hourly timer that runs it.

## One-time install (on the VPS, as the `smartbrain` user)

```sh
# 0. prereqs (skip any already present)
command -v git rsync >/dev/null || sudo apt-get install -y git rsync

# 1. clone the public repo where the units expect it
mkdir -p ~/sb-node/src
git clone https://github.com/SecureCloudGroup/SmartBrain_3000 ~/sb-node/src/SmartBrain_3000

# 2. install the user timer (no root needed)
mkdir -p ~/.config/systemd/user
cp ~/sb-node/src/SmartBrain_3000/deploy/sb-web-sync.service ~/.config/systemd/user/
cp ~/sb-node/src/SmartBrain_3000/deploy/sb-web-sync.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sb-web-sync.timer

# 3. let it run without an active login session (survives reboots/logout)
sudo loginctl enable-linger smartbrain
```

Confirm it works:

```sh
systemctl --user start sb-web-sync.service     # run once now
journalctl --user -u sb-web-sync -n 20 --no-pager   # should print "deployed vX.Y.Z ..."
systemctl --user list-timers sb-web-sync.timer      # next scheduled run
curl -s https://rtc.securecloudgroup.com/_app/version.json   # should match the latest release build
```

## Notes / assumptions
- The served dirs default to `~/sb-node/compose/web` and `~/sb-node/landing` (the current Caddy mounts).
  Override with `SB_WEB_DEST` / `SB_LANDING_DEST` / `SB_REPO_DIR` env in the service unit if the layout
  changes.
- It tracks the latest **release tag**, not `main`, so the phone shell always matches the released
  Desktop image — never an unreleased build.
- Cadence is hourly (`OnUnitActiveSec=1h`) + 2 min after boot, `Persistent=true` so a missed run catches
  up. Tighten/loosen in the `.timer` if you want.
- The script is idempotent (already-on-latest → no-op) and refuses to deploy an empty checkout (which
  the `rsync --delete` would otherwise use to wipe the live shell).
