package stack

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
)

func TestEnsureLocalTokenMintsAPrivateFileAndReusesIt(t *testing.T) {
	dir := t.TempDir()
	tok, err := EnsureLocalToken(dir)
	if err != nil || !validToken(tok) {
		t.Fatalf("EnsureLocalToken = %q, %v", tok, err)
	}
	info, err := os.Stat(filepath.Join(dir, tokenFile))
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
		t.Errorf("token file mode = %v, want 0600", info.Mode().Perm())
	}
	again, err := EnsureLocalToken(dir)
	if err != nil || again != tok {
		t.Errorf("second call = %q, %v; want the same token", again, err)
	}
}

func TestEnsureLocalTokenReplacesUnusableContent(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, tokenFile), []byte("short\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	tok, err := EnsureLocalToken(dir)
	if err != nil || tok == "short" || !validToken(tok) {
		t.Errorf("EnsureLocalToken = %q, %v; want a fresh valid token", tok, err)
	}
}

// captured records the headers each path saw.
type captured struct {
	mu   sync.Mutex
	seen map[string]http.Header
}

func (c *captured) get(path string) http.Header {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.seen[path]
}

func serve(t *testing.T) (int, *captured) {
	t.Helper()
	c := &captured{seen: map[string]http.Header{}}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c.mu.Lock()
		c.seen[r.URL.Path] = r.Header.Clone()
		c.mu.Unlock()
		if r.URL.Path == "/api/ni/notices" {
			_, _ = w.Write([]byte("[]"))
			return
		}
		_, _ = w.Write([]byte(`{"version":"1.0.0"}`))
	}))
	t.Cleanup(srv.Close)
	return srv.Listener.Addr().(*net.TCPAddr).Port, c
}

func TestCredentialedCallsPresentTheTokenAndTheLivenessProbeDoesNot(t *testing.T) {
	prev := LocalAPIToken
	LocalAPIToken = strings.Repeat("T", 40)
	t.Cleanup(func() { LocalAPIToken = prev })
	port, c := serve(t)
	ctx := context.Background()

	if _, _, ok := Handshake(ctx, port, "9.9.9"); !ok {
		t.Fatal("handshake failed")
	}
	h := c.get("/api/health")
	if h.Get("Authorization") != "Bearer "+LocalAPIToken || h.Get("X-SmartBrain-Update") != "9.9.9" {
		t.Errorf("handshake headers = %v", h)
	}

	if _, ok := FetchNotices(ctx, port); !ok {
		t.Fatal("notices failed")
	}
	n := c.get("/api/ni/notices")
	if n.Get("Authorization") != "Bearer "+LocalAPIToken {
		t.Errorf("notices must present the token, got %v", n)
	}
	if n.Get("X-SB-Local") != "1" {
		t.Error("notices must keep the old marker for an app from before the token")
	}

	if !(Stack{Port: port}).Healthy(ctx) {
		t.Fatal("healthy failed")
	}
	// A credentialed launcher probe WITHOUT a staged version withdraws the update offer,
	// so the liveness probe must stay anonymous.
	if got := c.get("/api/health").Get("Authorization"); got != "" {
		t.Errorf("Healthy must not present the token, got %q", got)
	}
}

func TestComposeEnvCarriesTheToken(t *testing.T) {
	prev := LocalAPIToken
	t.Cleanup(func() { LocalAPIToken = prev })
	LocalAPIToken = ""
	for _, kv := range composeEnv() {
		if strings.HasPrefix(kv, TokenEnv+"=") {
			t.Fatalf("no token configured, yet compose env carries %q", kv)
		}
	}
	LocalAPIToken = strings.Repeat("Q", 40)
	env := composeEnv()
	if env[len(env)-1] != TokenEnv+"="+LocalAPIToken {
		t.Errorf("compose env must end with the token, got %q", env[len(env)-1])
	}
}
