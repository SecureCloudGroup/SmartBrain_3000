# Packaging — install SmartBrain without a code-signing certificate

These are the manifests for the three package managers that let a user install the desktop launcher
with **no security warning**, even though the app is unsigned. That works because none of these
channels applies the macOS quarantine flag or Windows Mark-of-the-Web — the two things that trigger
Gatekeeper and SmartScreen. A browser-downloaded `.dmg`/`.exe` is the one channel that *would* warn,
so we don't lead with it.

| Channel | File(s) | User runs | Where it's published |
|---|---|---|---|
| Homebrew (macOS) | `homebrew/Casks/smartbrain.rb` | `brew install --cask securecloudgroup/tap/smartbrain` | your tap repo `SecureCloudGroup/homebrew-tap` |
| winget (Windows) | `winget/SecureCloudGroup.SmartBrain.*.yaml` | `winget install SecureCloudGroup.SmartBrain` | `microsoft/winget-pkgs` (PR, they review) |
| Scoop (Windows) | `scoop/smartbrain.json` | `scoop install securecloudgroup/smartbrain` | your bucket repo `SecureCloudGroup/scoop-bucket` |

All three are pinned to the current release and its SHA-256. They only work once the matching
GitHub Release exists (its assets are what they download).

## First-time publish

**Homebrew** — create a public repo named exactly `SecureCloudGroup/homebrew-tap`, and put
`homebrew/Casks/smartbrain.rb` at `Casks/smartbrain.rb` in it. That's it — `securecloudgroup/tap`
resolves to that repo. The cask strips the quarantine flag in a `postflight` (the app is ad-hoc
signed, so this is all it needs to open on first click).

**winget** — the easiest route is Microsoft's `wingetcreate`:
```
wingetcreate submit packaging/winget --token <gh-token>
```
or fork `microsoft/winget-pkgs` and open a PR placing the three files under
`manifests/s/SecureCloudGroup/SmartBrain/0.4.0/`. Their bot validates (installs it in a sandbox) and
a maintainer merges. No code-signing is required for a `.zip`/`.exe` — only MSIX must be signed.

**Scoop** — create a public repo `SecureCloudGroup/scoop-bucket` with `smartbrain.json` at
`bucket/smartbrain.json`. Users add it with `scoop bucket add securecloudgroup https://github.com/SecureCloudGroup/scoop-bucket`.

## Updating for a new release — automatic

Pushing a `v*` tag now updates everything on its own. The `packages` job in
`.github/workflows/launcher.yml` runs after the app builds, hashes the two release zips, rewrites all
five manifests here with `refresh.py`, and then:

- **pushes** the new cask to `SecureCloudGroup/homebrew-tap` and the new manifest to
  `SecureCloudGroup/scoop-bucket` — so `brew`/`scoop` users get the update immediately;
- **opens a PR** bumping the manifests in this `packaging/` folder (main is branch-protected, so it
  can't push directly) — merge it to keep the source current.

**winget is not auto-submitted** (that PR goes through Microsoft's review) — the winget files here are
kept current so the next `wingetcreate submit packaging/winget` is a one-liner.

### One-time setup: the `PACKAGES_TOKEN` secret
The tap and bucket are separate repos, so the workflow's default token can't write to them. Create a
**fine-grained PAT** with **Contents: read/write** on `homebrew-tap` and `scoop-bucket`, and add it as
an Actions secret named `PACKAGES_TOKEN` in the `SmartBrain_3000` repo. Until that secret exists, the
tap/bucket push steps are skipped and only the in-repo PR is opened (so nothing breaks).

### Doing it by hand
```
gh release download vX.Y.Z -R SecureCloudGroup/SmartBrain_3000 -D /tmp/rel
python3 packaging/refresh.py --version X.Y.Z \
    --macos /tmp/rel/SmartBrain-macos.zip --windows /tmp/rel/SmartBrain-windows.zip
```
Then copy `homebrew/Casks/smartbrain.rb` to the tap and `scoop/smartbrain.json` to the bucket.

> The winget `PackageIdentifier` (`SecureCloudGroup.SmartBrain`) is effectively permanent once
> published — worth confirming the product name before the first winget submission.
