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

## Updating for a new release

Each release changes the version and the asset hashes. Get the new hashes:
```
gh release download vX.Y.Z -R SecureCloudGroup/SmartBrain_3000 -D /tmp/rel
shasum -a 256 /tmp/rel/SmartBrain-macos.zip /tmp/rel/SmartBrain-windows.zip
```
then bump `version`/`PackageVersion` and the `sha256`/`InstallerSha256`/`hash` in each file. Scoop's
`autoupdate` + `checkver` mean its bucket can self-bump; `wingetcreate update` does the same for
winget. Automating the tap + winget bump from a release workflow is a reasonable follow-up (it needs
a token with access to the tap repo).

> The winget `PackageIdentifier` (`SecureCloudGroup.SmartBrain`) is effectively permanent once
> published — worth confirming the product name before the first winget submission.
