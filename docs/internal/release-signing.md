# Signing releases (and why the checksum alone was not enough)

The launcher self-updates automatically, with no user click, and the distribution
deliberately avoids Gatekeeper and SmartScreen (the Homebrew cask clears the
quarantine attribute; a Go HTTP download sets no Mark-of-the-Web). Nothing else
inspects what arrives.

Until now the only control on that path was a SHA-256 published beside each zip.
A checksum answers *"did these bytes arrive intact"* — it cannot answer *"did they
come from us"*, because it is fetched from the same release as the file it
describes. Anyone able to publish one asset can publish both and make them agree.
So the realistic threat was never a network attacker (TLS covers that); it was a
leaked `PACKAGES_TOKEN`, a compromised CI job, or a hijacked maintainer account —
any one of which converted directly into silent code execution on every install.

**Now:** the checksum file is signed with an Ed25519 key, and the *public* half is
compiled into the launcher. Replacing a release asset is no longer sufficient,
because the key an attacker would have to match ships inside the binary they are
trying to displace.

What is signed is the sidecar, not the zip — the `SHA256SUMS`-plus-detached-signature
construction distributions have used for decades. The signature proves the sidecar
is ours; the sidecar's digest proves the zip matches it.

- `launcher/update/signature.go` — verification, and `releasePublicKey`
- `launcher/update/sign.go` — key generation and signing (one file defines the format)
- `launcher/cmd/sbsign` — the CLI CI uses
- `.github/workflows/launcher.yml`, job `sign`

## One-time setup

**Do this before tagging a release with this code in it.** The launcher refuses
unsigned updates, so the order matters.

1. Generate a keypair (locally, not in CI):

   ```sh
   go -C launcher run ./cmd/sbsign gen
   ```

   It prints a public key and a private key. The private key is printed once and
   written nowhere — that is deliberate.

2. Put the **private** key in the repository secret `SMARTBRAIN_SIGNING_KEY`
   (Settings → Secrets and variables → Actions). It must not be committed, pasted
   into an issue, or echoed by a workflow. Anyone holding it can publish an update
   that every install will accept.

3. Put the **public** key in `releasePublicKey` in `launcher/update/signature.go`,
   and commit it. It is public by design: it belongs in git, where anyone can see
   which key releases are expected to carry.

4. Tag a release as usual. The `sign` job refuses to run without the secret, and
   refuses to run if the secret does not match the compiled-in public key — so a
   half-finished rotation is a red build rather than a release nobody can install.

## Anyone can check a release themselves

The signature format is minisign's, so this needs none of our tooling:

```sh
# once: save the public key from launcher/update/signature.go
printf 'untrusted comment: smartbrain release key\n<PUBLIC KEY>\n' > minisign.pub

minisign -Vm SmartBrain-macos.zip.sha256 -p minisign.pub   # is the checksum ours?
shasum -c SmartBrain-macos.zip.sha256                      # does the zip match it?
```

Verified against minisign 0.11, including its comment signature.

## Rotating the key

Same as setup: generate, replace the secret, replace `releasePublicKey`, ship a
release. Because the public key is compiled in, **a launcher only ever trusts the
key it was built with** — installs running an older binary keep verifying against
the old key until they update to a build carrying the new one. So never retire a
key and immediately sign with a new one in the same release; publish the build that
knows the new key first, then rotate.

If the private key is believed to be compromised, rotating is not enough on its own:
anything already signed with it stays verifiable to launchers built before the
rotation. Treat it as a security incident, publish the new key, and say so.

## Not covered by this

This proves **who produced a release**. It does not make the app *signed software*
in the operating system's eyes: macOS and Windows still see an unsigned binary,
which is why the cask clears quarantine and why a browser download shows a warning.

Fixing that is a separate, paid step — an Apple Developer certificate plus
notarization, and an EV certificate for Windows — and it is on the roadmap rather
than done. It would remove the quarantine-stripping and the SmartScreen warning
entirely, and it supersedes nothing here: signing the release and signing the
binaries answer different questions and both are worth having.
