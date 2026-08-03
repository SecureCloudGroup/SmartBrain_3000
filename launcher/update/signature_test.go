// Authenticity tests. The positive case matters least here: what this file is for
// is proving every way of NOT being the release key is refused, since a verifier
// that returns nil on a path nobody checked is the same as having no verifier.
package update

import (
	"context"
	"encoding/base64"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const sidecar = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef  launcher.zip\n"

// errNotPublished stands in for the release simply not carrying a signature asset.
var errNotPublished = errors.New("404 not found")

func decodeB64(s string) ([]byte, error) { return base64.StdEncoding.DecodeString(s) }
func encodeB64(b []byte) string          { return base64.StdEncoding.EncodeToString(b) }

func readInstalled(root string) (string, error) {
	b, err := os.ReadFile(filepath.Join(root, "SmartBrain.exe"))
	return string(b), err
}

func signedFixture(t *testing.T) (pub, priv, sig string) {
	t.Helper()
	pub, priv, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	sig, err = SignDetached(priv, []byte(sidecar), "launcher.zip")
	if err != nil {
		t.Fatal(err)
	}
	return pub, priv, sig
}

func TestVerifyAcceptsOurOwnSignature(t *testing.T) {
	pub, _, sig := signedFixture(t)
	if err := verifySignature([]byte(sidecar), sig, pub); err != nil {
		t.Fatalf("a signature we just made must verify: %v", err)
	}
}

func TestVerifyRefusesADifferentKey(t *testing.T) {
	_, _, sig := signedFixture(t)
	other, _, err := GenerateKey() // the attacker signs with a key that is not ours
	if err != nil {
		t.Fatal(err)
	}
	if err := verifySignature([]byte(sidecar), sig, other); err == nil {
		t.Fatal("a signature by another key must be refused")
	}
}

func TestVerifyRefusesAlteredContent(t *testing.T) {
	pub, _, sig := signedFixture(t)
	altered := strings.Replace(sidecar, "0123", "dead", 1)
	if err := verifySignature([]byte(altered), sig, pub); err == nil {
		t.Fatal("a checksum file altered after signing must be refused")
	}
}

func TestVerifyRefusesSignatureFromAnotherFile(t *testing.T) {
	pub, priv, _ := signedFixture(t)
	// Valid signature, valid key — over the wrong message.
	elsewhere, err := SignDetached(priv, []byte("some other release\n"), "other.zip")
	if err != nil {
		t.Fatal(err)
	}
	if err := verifySignature([]byte(sidecar), elsewhere, pub); err == nil {
		t.Fatal("a signature over different content must be refused")
	}
}

func TestVerifyFailsClosedWithoutAKey(t *testing.T) {
	_, _, sig := signedFixture(t)
	if err := verifySignature([]byte(sidecar), sig, ""); err == nil {
		t.Fatal("a build with no compiled-in key must refuse to verify anything")
	}
}

func TestVerifyRefusesMalformedInput(t *testing.T) {
	pub, _, sig := signedFixture(t)
	cases := []struct{ name, sig, pub string }{
		{"empty signature", "", pub},
		{"not base64", "untrusted comment: x\n!!!!not base64!!!!\n", pub},
		{"truncated signature", "untrusted comment: x\nRWQ=\n", pub},
		{"comments only", "untrusted comment: x\ntrusted comment: y\n", pub},
		{"public key not base64", sig, "!!!!"},
		{"public key wrong length", sig, "RWQ="},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if err := verifySignature([]byte(sidecar), c.sig, c.pub); err == nil {
				t.Fatal("malformed input must be refused, not accepted by default")
			}
		})
	}
}

func TestVerifyRefusesPrehashedAlgorithm(t *testing.T) {
	// "ED" is a real minisign signature we deliberately do not support; it must be
	// refused explicitly rather than silently treated as the algorithm we do check.
	pub, _, sig := signedFixture(t)
	lines := strings.Split(sig, "\n")
	raw := lines[1]
	decoded, err := decodeB64(raw)
	if err != nil {
		t.Fatal(err)
	}
	decoded[1] = 'D' // "Ed" -> "ED"
	lines[1] = encodeB64(decoded)
	if err := verifySignature([]byte(sidecar), strings.Join(lines, "\n"), pub); err == nil ||
		!strings.Contains(err.Error(), "unsupported signature algorithm") {
		t.Fatalf("prehashed signatures must be refused explicitly, got: %v", err)
	}
}

func TestPublicKeyForMatchesTheSigningKey(t *testing.T) {
	pub, priv, _ := signedFixture(t)
	got, err := PublicKeyFor(priv)
	if err != nil {
		t.Fatal(err)
	}
	if got != pub {
		t.Fatalf("PublicKeyFor must reproduce the published key\n got %q\nwant %q", got, pub)
	}
}

func TestCompiledInKeyIsUsedWhenNoneInjected(t *testing.T) {
	// Guards the wiring: an Updater with no PubKey must fall back to the constant,
	// so a real build cannot accidentally verify against an empty key.
	if got := (Updater{}).publicKey(); got != releasePublicKey {
		t.Fatalf("an Updater with no injected key must use the compiled-in one, got %q", got)
	}
	if got := (Updater{PubKey: "injected"}).publicKey(); got != "injected" {
		t.Fatalf("an injected key must win, got %q", got)
	}
}

func TestCompiledInKeyIsWellFormed(t *testing.T) {
	// A mistyped or truncated paste would otherwise surface as every launcher
	// rejecting a release that was signed perfectly well. Catch it at build time.
	// Empty is allowed and means "this build never self-updates" — see
	// TestVerifyFailsClosedWithoutAKey for that path.
	if releasePublicKey == "" {
		t.Skip("no release key compiled in; updates are disabled for this build")
	}
	alg, keyID, key, err := parsePublicKey(releasePublicKey)
	if err != nil {
		t.Fatalf("releasePublicKey does not parse: %v", err)
	}
	if alg != algEd25519 {
		t.Fatalf("releasePublicKey algorithm = %q, want %q", alg, algEd25519)
	}
	if len(keyID) != minisignKeyID || len(key) != 32 {
		t.Fatalf("releasePublicKey has key id %d bytes and key %d bytes", len(keyID), len(key))
	}
}

func TestApplyRefusesWhenTheSignatureIsMissing(t *testing.T) {
	u, started, _, _ := harness(t, "flat")
	u.FetchBody = func(_ context.Context, url string) ([]byte, error) {
		if strings.HasSuffix(url, ".sha256.minisig") {
			return nil, errNotPublished
		}
		return []byte(sidecar), nil
	}
	if _, err := u.Apply(context.Background(), "9.9.9"); err == nil ||
		!strings.Contains(err.Error(), "signature") {
		t.Fatalf("an unsigned release must not install, got: %v", err)
	}
	if len(*started) != 0 {
		t.Fatal("a refused update must not relaunch anything")
	}
}

func TestApplyRefusesASignatureFromAForeignKey(t *testing.T) {
	u, started, root, _ := harness(t, "flat")
	_, foreign, err := GenerateKey() // attacker publishes a consistent zip + sidecar + signature
	if err != nil {
		t.Fatal(err)
	}
	u.FetchBody = func(_ context.Context, url string) ([]byte, error) {
		return releaseBody(t, url, sidecar, foreign)
	}
	if _, err := u.Apply(context.Background(), "9.9.9"); err == nil ||
		!strings.Contains(err.Error(), "signature") {
		t.Fatalf("a release signed by anyone else must be refused, got: %v", err)
	}
	body, _ := readInstalled(root)
	if body != "OLD" {
		t.Fatal("a refused update must leave the running install untouched")
	}
	if len(*started) != 0 {
		t.Fatal("a refused update must not relaunch anything")
	}
}
