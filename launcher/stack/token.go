package stack

import (
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

// LocalAPIToken (set by main at startup) is this install's credential for the local
// app's API (R14, 2026-09-23). The launcher OWNS it: it lives in the launcher's 0700
// config dir as a 0600 file, rides Handshake and FetchNotices as a bearer, and is handed
// to the app the launcher starts (TokenEnv) — the native child's environment or the
// compose environment — so both sides agree. Empty = present nothing: the app then
// refuses the credentialed calls, which the launcher already reads as "no news".
//
// Deliberately NOT sent by Healthy: that probe carries the launcher header without the
// staged version, and a credentialed launcher probe with no staged version is how the
// app learns the launcher WITHDREW an update — a liveness poll must never say that.
var LocalAPIToken = ""

// TokenEnv is the variable the app reads the launcher's token from.
const TokenEnv = "SMARTBRAIN_LOCAL_TOKEN"

const (
	tokenFile     = "local-api.token"
	minTokenChars = 32
)

// EnsureLocalToken returns the token stored in dir, minting it (32 random bytes,
// base64url, 0600) on first use. A file with unusable content is replaced.
func EnsureLocalToken(dir string) (string, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", fmt.Errorf("create app dir: %w", err)
	}
	path := filepath.Join(dir, tokenFile)
	if b, err := os.ReadFile(path); err == nil {
		if tok := strings.TrimSpace(string(b)); validToken(tok) {
			return tok, nil
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return "", fmt.Errorf("read token: %w", err)
	}
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", fmt.Errorf("mint token: %w", err)
	}
	tok := base64.RawURLEncoding.EncodeToString(raw)
	tmp := path + ".tmp"
	_ = os.Remove(tmp) // a stale tmp must not keep a looser mode than the fresh 0600 create
	if err := os.WriteFile(tmp, []byte(tok+"\n"), 0o600); err != nil {
		return "", fmt.Errorf("write token: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return "", fmt.Errorf("install token: %w", err)
	}
	return tok, nil
}

func validToken(tok string) bool {
	if len(tok) < minTokenChars {
		return false
	}
	for _, r := range tok { // printable ASCII, no spaces
		if r <= ' ' || r > '~' {
			return false
		}
	}
	return true
}

// authorize presents the local token when one is configured.
func authorize(req *http.Request) {
	if LocalAPIToken != "" {
		req.Header.Set("Authorization", "Bearer "+LocalAPIToken)
	}
}

// composeEnv is the environment `docker compose` runs with: the caller's, plus the
// token the compose file hands to the app container.
func composeEnv() []string {
	env := os.Environ()
	if LocalAPIToken != "" {
		env = append(env, TokenEnv+"="+LocalAPIToken)
	}
	return env
}
