package update

// Release authenticity.
//
// The sha256 sidecar answers "did these bytes arrive intact"; it cannot answer
// "did they come from us", because it is fetched from the same release as the zip
// it describes — anyone able to publish one asset can publish both and make them
// agree. Combined with a fully automatic update and a distribution that
// deliberately clears quarantine, a single leaked publishing token would have
// meant silent code execution on every install. So the sidecar is signed, and the
// PUBLIC key is compiled into this binary: replacing an asset is no longer enough,
// because the key an attacker would have to match ships inside the very program
// they are trying to displace.
//
// What is signed is the sidecar, not the zip — the SHA256SUMS-plus-detached-signature
// construction distributions have used for decades. The signature proves the sidecar
// is ours; the sidecar's digest proves the zip matches it. Signing a 64-byte file
// instead of a 100 MB one keeps verification O(1) in memory and lets Ed25519 sign the
// message directly, with no prehash variant to negotiate.
//
// The format is minisign's, so releases stay verifiable with a tool nobody has to
// take our word for:
//
//	minisign -Vm SmartBrain-macos.zip.sha256 -P <public key>
//	shasum -c SmartBrain-macos.zip.sha256
//
// Only the legacy "Ed" algorithm (Ed25519 over the file itself) is accepted.
// Prehashed "ED" signatures are refused rather than half-supported: verifying them
// needs BLAKE2b, which is not in the standard library, and this package ships with
// no cryptographic dependencies. sign_release.go writes what this reads.

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"fmt"
	"strings"
)

// releasePublicKey is the minisign public key releases are signed with, in the
// same base64 form `minisign -G` prints (2-byte algorithm, 8-byte key id, 32-byte
// Ed25519 key).
//
// EMPTY MEANS NO UPDATES. A build without a key refuses to install anything rather
// than falling back to the checksum alone — an unauthenticated update is the exact
// thing this exists to prevent, and failing open would silently restore it. See
// docs/internal/release-signing.md to generate the keypair and fill this in.
const releasePublicKey = ""

const (
	// "Ed": Ed25519 over the file contents. "ED" is minisign's prehashed variant.
	algEd25519    = "Ed"
	minisignKeyID = 8
	rawPubKeyLen  = 2 + minisignKeyID + ed25519.PublicKeySize
	rawSigLen     = 2 + minisignKeyID + ed25519.SignatureSize
)

// publicKey returns the key this Updater verifies with: the injected one when set
// (tests), otherwise the compiled-in constant.
func (u Updater) publicKey() string {
	if u.PubKey != "" {
		return u.PubKey
	}
	return releasePublicKey
}

// verifySignature reports whether sig is a valid minisign signature over signed,
// made by pubKey. Every failure is an error; there is no path that returns nil for
// an unverified input.
func verifySignature(signed []byte, sig, pubKey string) error {
	if pubKey == "" {
		return fmt.Errorf("no release public key is compiled into this build — refusing to update")
	}
	alg, keyID, key, err := parsePublicKey(pubKey)
	if err != nil {
		return err
	}
	if alg != algEd25519 {
		return fmt.Errorf("unsupported public key algorithm %q", alg)
	}
	sigAlg, sigKeyID, rawSig, err := parseSignature(sig)
	if err != nil {
		return err
	}
	if sigAlg != algEd25519 {
		// "ED" lands here: a real minisign signature we deliberately do not accept.
		return fmt.Errorf("unsupported signature algorithm %q (expected %q)", sigAlg, algEd25519)
	}
	if !bytes.Equal(keyID, sigKeyID) {
		// Not a security boundary on its own — the Verify below is — but it turns
		// "signed by the wrong key" into a clear message instead of a bare failure.
		return fmt.Errorf("signature was made by a different key")
	}
	if !ed25519.Verify(key, signed, rawSig) {
		return fmt.Errorf("signature does not match the downloaded checksum file")
	}
	return nil
}

// parsePublicKey decodes minisign's base64 public key: algorithm, key id, key.
func parsePublicKey(pubKey string) (alg string, keyID []byte, key ed25519.PublicKey, err error) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(lastLine(pubKey)))
	if err != nil {
		return "", nil, nil, fmt.Errorf("malformed public key: %w", err)
	}
	if len(raw) != rawPubKeyLen {
		return "", nil, nil, fmt.Errorf("malformed public key: got %d bytes, want %d", len(raw), rawPubKeyLen)
	}
	return string(raw[:2]), raw[2 : 2+minisignKeyID], ed25519.PublicKey(raw[2+minisignKeyID:]), nil
}

// parseSignature decodes a .minisig file. Only the first base64 line matters here:
// the trusted comment and its global signature bind the comment, not the artifact,
// and this format carries no comment we act on.
func parseSignature(sig string) (alg string, keyID, rawSig []byte, err error) {
	line := ""
	for _, l := range strings.Split(sig, "\n") {
		l = strings.TrimSpace(l)
		if l == "" || strings.HasPrefix(l, "untrusted comment:") || strings.HasPrefix(l, "trusted comment:") {
			continue
		}
		line = l
		break
	}
	if line == "" {
		return "", nil, nil, fmt.Errorf("malformed signature: no signature line")
	}
	raw, err := base64.StdEncoding.DecodeString(line)
	if err != nil {
		return "", nil, nil, fmt.Errorf("malformed signature: %w", err)
	}
	if len(raw) != rawSigLen {
		return "", nil, nil, fmt.Errorf("malformed signature: got %d bytes, want %d", len(raw), rawSigLen)
	}
	return string(raw[:2]), raw[2 : 2+minisignKeyID], raw[2+minisignKeyID:], nil
}

// lastLine returns the final non-empty line, so a public key may be pasted either
// bare or with minisign's "untrusted comment:" header above it.
func lastLine(s string) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if l := strings.TrimSpace(lines[i]); l != "" {
			return l
		}
	}
	return ""
}
