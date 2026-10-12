package stack

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"time"
)

// The page browser's renderer sidecar (ni-format §35) is the compose service `render`,
// declared under a profile of the same name in docker-compose.release.yml. The app
// container never runs a browser engine; on the Docker stack the sidecar does.
const (
	RenderProfile = "render"
	renderService = "render"
)

// renderUp is the one command that starts the sidecar. Naming the SERVICE is
// load-bearing: a bare `--profile render up -d` converges every service, and the app's
// `pull_policy: always` then recreates the app container onto an update the background
// pull staged — installing it with no click and dropping the open session (measured,
// Compose v5.1.2). Named, it never touches the app or the gateway, and a sidecar that is
// already running is left as it is.
var renderUp = []string{"--profile", RenderProfile, "up", "-d", renderService}

// RenderUpArgs is the exact `docker` argument vector StartRender runs.
func (s Stack) RenderUpArgs() []string { return s.composeArgs(renderUp...) }

// StartRender starts the renderer sidecar. Idempotent; its pull_policy makes the first
// start download the image.
func (s Stack) StartRender(ctx context.Context) error { return s.compose(ctx, renderUp...) }

// FetchStatus reads the app's aggregate status (GET /api/status/overview). The app answers
// it while locked too, but only to a credential (R14), so the launcher's token rides it.
// Bounded and forgiving like FetchNotices: the app being down, a refused credential, any
// status but 200 or an oversized body all read as "no news".
func FetchStatus(ctx context.Context, port int) ([]byte, bool) {
	url := fmt.Sprintf("http://127.0.0.1:%d/api/status/overview", port)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, false
	}
	authorize(req)
	client := http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, false // 401/423 (no accepted credential), 5xx: no news
	}
	const maxStatus = 64 << 10
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxStatus+1))
	if err != nil || len(body) > maxStatus {
		return nil, false
	}
	return body, true
}
