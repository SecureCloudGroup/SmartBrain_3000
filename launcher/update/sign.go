package update

// The signing half of release authenticity. It lives beside the verifier on
// purpose: one file defines the wire format, so the two can never drift into
// disagreeing about it.
//
// Keys are generated and used by our own tooling rather than the minisign CLI,
// because CI wants one unencrypted secret rather than a scrypt-wrapped key file
// plus a password to pipe into a prompt. What we EMIT is minisign's format, which
// is the half that has to interoperate: anyone can verify a release with the
// stock tool without trusting anything of ours.

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"strings"
)

// privKeyLen is our private-key blob: the key id the signature must carry,
// followed by the Ed25519 seed.
const privKeyLen = minisignKeyID + ed25519.SeedSize

// GenerateKey returns a new (publicKey, privateKey) pair, both base64.
//
// The public key is minisign's format and belongs in releasePublicKey and the
// documentation. The private key is a CI secret and must never be committed,
// logged, or printed by a workflow.
func GenerateKey() (pub string, priv string, err error) {
	keyID := make([]byte, minisignKeyID)
	if _, err := rand.Read(keyID); err != nil {
		return "", "", err
	}
	pubKey, privKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return "", "", err
	}
	pubBlob := append(append([]byte(algEd25519), keyID...), pubKey...)
	privBlob := append(append([]byte{}, keyID...), privKey.Seed()...)
	return base64.StdEncoding.EncodeToString(pubBlob),
		base64.StdEncoding.EncodeToString(privBlob), nil
}

// PublicKeyFor returns the public key matching a private key, so a workflow can
// assert it is signing with the key the launcher actually verifies against
// instead of discovering the mismatch after publishing.
func PublicKeyFor(priv string) (string, error) {
	keyID, key, err := parsePrivateKey(priv)
	if err != nil {
		return "", err
	}
	pub := key.Public().(ed25519.PublicKey)
	return base64.StdEncoding.EncodeToString(append(append([]byte(algEd25519), keyID...), pub...)), nil
}

// SignDetached returns the contents of a .minisig file for msg.
//
// The trusted comment and the global signature over it are emitted because the
// stock minisign refuses a signature without them; our own verifier ignores the
// comment, and nothing downstream reads it.
func SignDetached(priv string, msg []byte, trustedComment string) (string, error) {
	keyID, key, err := parsePrivateKey(priv)
	if err != nil {
		return "", err
	}
	if strings.ContainsAny(trustedComment, "\r\n") {
		return "", fmt.Errorf("trusted comment must be a single line")
	}
	sig := ed25519.Sign(key, msg)
	blob := append(append([]byte(algEd25519), keyID...), sig...)
	// minisign binds the comment by signing it together with the signature above.
	global := ed25519.Sign(key, append(append([]byte{}, sig...), []byte(trustedComment)...))
	return fmt.Sprintf(
		"untrusted comment: signature from SmartBrain release signing key\n%s\ntrusted comment: %s\n%s\n",
		base64.StdEncoding.EncodeToString(blob),
		trustedComment,
		base64.StdEncoding.EncodeToString(global),
	), nil
}

func parsePrivateKey(priv string) (keyID []byte, key ed25519.PrivateKey, err error) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(priv))
	if err != nil {
		return nil, nil, fmt.Errorf("malformed private key: %w", err)
	}
	if len(raw) != privKeyLen {
		return nil, nil, fmt.Errorf("malformed private key: got %d bytes, want %d", len(raw), privKeyLen)
	}
	return raw[:minisignKeyID], ed25519.NewKeyFromSeed(raw[minisignKeyID:]), nil
}
