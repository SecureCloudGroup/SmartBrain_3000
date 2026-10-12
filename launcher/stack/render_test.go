package stack

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// statusServer answers /api/status/overview with code and body, recording the credential.
func statusServer(t *testing.T, code int, body string, auth *string) int {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/status/overview" {
			http.NotFound(w, r)
			return
		}
		*auth = r.Header.Get("Authorization")
		w.WriteHeader(code)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv.Listener.Addr().(*net.TCPAddr).Port
}

// The status read is credentialed (the route answers only a credential, R14) and forgiving:
// a refusal, a non-200 or an oversized body is "no news", never a half-read answer.
func TestFetchStatus(t *testing.T) {
	prev := LocalAPIToken
	LocalAPIToken = strings.Repeat("T", 40)
	t.Cleanup(func() { LocalAPIToken = prev })
	ctx := context.Background()
	var auth string

	port := statusServer(t, http.StatusOK, `{"browsers":[]}`, &auth)
	body, ok := FetchStatus(ctx, port)
	if !ok || string(body) != `{"browsers":[]}` {
		t.Fatalf("FetchStatus = %q, %v", body, ok)
	}
	if auth != "Bearer "+LocalAPIToken {
		t.Errorf("the status read must present the launcher token, got %q", auth)
	}
	for _, code := range []int{http.StatusLocked, http.StatusUnauthorized, http.StatusInternalServerError} {
		if body, ok := FetchStatus(ctx, statusServer(t, code, `{"browsers":[]}`, &auth)); ok || body != nil {
			t.Errorf("status %d must read as no news, got %q, %v", code, body, ok)
		}
	}
	big := `{"pad":"` + strings.Repeat("x", 64<<10) + `"}`
	if body, ok := FetchStatus(ctx, statusServer(t, http.StatusOK, big, &auth)); ok || body != nil {
		t.Errorf("an oversized answer must read as no news, got %d bytes, %v", len(body), ok)
	}
	srv := httptest.NewServer(http.NotFoundHandler())
	gone := srv.Listener.Addr().(*net.TCPAddr).Port
	srv.Close()
	if _, ok := FetchStatus(ctx, gone); ok {
		t.Error("an app that is not answering must read as no news")
	}
}
