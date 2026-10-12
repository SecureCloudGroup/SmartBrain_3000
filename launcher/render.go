// The page browser's renderer sidecar on the Docker stack (ni-format §35, step B3). The
// app container never runs a browser engine — the compose `render` profile's sidecar does,
// and nothing starts that profile on its own. So on the launcher's 30-second beat, read the
// app's status and start that one service when a card needs it and the sidecar is missing
// or left on an older image. The native stack never gets here: its wall is the OS's own.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"slices"
	"strings"
	"time"

	"github.com/SecureCloudGroup/SmartBrain_3000/launcher/stack"
)

// No start follows another sooner than this. It covers a sidecar still booting (the app
// still says missing), a failed start (offline, the image out of reach), and a stale sidecar
// that a start could not refresh yet (the release's renderer image not published yet).
const renderRetryDelay = 10 * time.Minute

// noRender is the --no-render opt-out, read from anywhere on the command line
// (`smartbrain --no-render`, `smartbrain run --no-render`): the launcher never starts the
// sidecar, and a Docker install then has no page browser (the app says so itself).
var noRender bool

func noRenderFlag(args []string) bool { return slices.Contains(args, "--no-render") }

// renderWanted is the pure decision (loops are not testable; JSON is) over
// /api/status/overview. It returns why the sidecar should be started — "missing" or
// "stale" — or "" when it should not. The app's `renderer` field decides: a card has needed
// a render since the app started (`wanted`) and the sidecar is `missing` or `stale` (it
// answers with another build than this app pins; the start recreates it on the newer
// image). An app from before that field falls back to its rows: a browsers[] row that says
// docker_no_sidecar. Either way never while the page browser is switched off (every row
// `disabled`), and anything unreadable is "": a garbled answer never starts a container.
func renderWanted(status []byte) string {
	var body struct {
		Renderer *struct {
			Wanted  bool   `json:"wanted"`
			Sidecar string `json:"sidecar"`
		} `json:"renderer"`
		Browsers []struct {
			Phase  string `json:"phase"`
			Reason string `json:"reason"`
		} `json:"browsers"`
	}
	if json.Unmarshal(status, &body) != nil {
		return ""
	}
	disabled, noSidecar := len(body.Browsers) > 0, false
	for _, row := range body.Browsers {
		disabled = disabled && row.Phase == "disabled"
		noSidecar = noSidecar || (row.Reason == "docker_no_sidecar" && row.Phase != "disabled")
	}
	if disabled {
		return "" // switched off: the app's `wanted` counts needs regardless (measured)
	}
	if r := body.Renderer; r != nil {
		if r.Wanted && (r.Sidecar == "missing" || r.Sidecar == "stale") {
			return r.Sidecar
		}
		return ""
	}
	if noSidecar {
		return "missing"
	}
	return ""
}

// renderGate is what one poll remembers for the next. Touched only by renderLoop.
type renderGate struct {
	nextTry time.Time // no start before this; zero = at once
}

// step is one poll — pure apart from the start it may call — and returns the ONE log line
// the poll owes ("" when it did nothing). A start, whatever its outcome, holds the next one
// off for renderRetryDelay, so a sidecar still booting is never asked for on every beat. The
// app not answering (stopped, restarting, updating) re-arms the gate: its next run decides
// at once, so a Stop and Start, or an update that left the sidecar stale, is met promptly.
func (g *renderGate) step(now time.Time, status []byte, ok bool, start func() error) string {
	if !ok {
		g.nextTry = time.Time{}
		return ""
	}
	why := renderWanted(status)
	if why == "" || now.Before(g.nextTry) {
		return ""
	}
	g.nextTry = now.Add(renderRetryDelay)
	if err := start(); err != nil {
		return fmt.Sprintf("page browser: the renderer sidecar is %s and did not start (trying again in %d minutes): %s",
			why, int(renderRetryDelay/time.Minute), oneLine(err.Error()))
	}
	return "page browser: the renderer sidecar was " + why + "; ran docker compose --profile render up -d render"
}

// oneLine squeezes an error onto one bounded log line: compose's combined output is many
// progress lines, and a failed start owes exactly one. A long one keeps both ends — the
// command and its exit status lead, and the daemon's reason comes last.
func oneLine(s string) string {
	r := []rune(strings.Join(strings.Fields(s), " "))
	if len(r) <= 300 {
		return string(r)
	}
	return string(r[:80]) + " … " + string(r[len(r)-220:])
}

// renderLoop runs the gate on the handshake's beat — same shape as its siblings: sleep,
// one bounded call, act. It stays off the stack while a start, stop or install holds the
// operation lock, but its own start runs outside that lock: a first start downloads the
// sidecar image, and holding the lock that long would drop every Stop or Restart click.
func renderLoop() {
	var gate renderGate
	for {
		time.Sleep(handshakeInterval)
		if nativeMode() {
			continue
		}
		if !mu.TryLock() {
			continue // a start, stop or install is in flight: look again next beat
		}
		mu.Unlock()
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		status, ok := stack.FetchStatus(ctx, sb.Port)
		cancel()
		if line := gate.step(time.Now(), status, ok, startRender); line != "" {
			log.Println(line)
		}
	}
}

// startRender is bounded like Up: a first start pulls the sidecar image.
func startRender() error {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	return sb.StartRender(ctx)
}
