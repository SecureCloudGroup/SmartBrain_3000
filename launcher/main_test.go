package main

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/SecureCloudGroup/SmartBrain_3000/launcher/stack"
	"github.com/SecureCloudGroup/SmartBrain_3000/launcher/update"
)

// The mode must survive launches that don't carry the env — reboots, Finder
// relaunches, self-update handovers. Observed live: a plain relaunch fell back to
// Docker, whose compose up then collided with the surviving native stack's ports.
func TestResolveNativeMode(t *testing.T) {
	cases := []struct {
		env       string
		marker    bool
		supported bool
		want      bool
		why       string
	}{
		{"1", false, true, true, "explicit opt-in works before any marker exists"},
		{"", true, true, true, "a marked machine stays Docker-free"},
		{"0", true, true, false, "env 0 forces Docker for this run (the escape hatch)"},
		{"", false, true, true, "DEFAULT: a fresh supported machine is Docker-free"},
		{"garbage", false, true, true, "unrecognized env falls through to the default"},
		// The one that protects real people: the macOS cask is a universal binary, so an
		// Intel Mac installs this same app. There is no runtime pinned for it, so defaulting
		// it into native would leave it unable to start anything at all.
		{"", false, false, false, "an unsupported platform keeps Docker"},
		{"garbage", false, false, false, "…and is not talked out of it by a junk env value"},
		{"1", false, false, true, "an explicit opt-in is still honoured (it will fail loudly, not silently)"},
		{"", true, false, true, "a machine that HAS run native keeps doing so"},
	}
	for _, c := range cases {
		if got := resolveNativeMode(c.env, c.marker, c.supported); got != c.want {
			t.Fatalf("resolveNativeMode(%q, marker=%v, supported=%v) = %v, want %v (%s)",
				c.env, c.marker, c.supported, got, c.want, c.why)
		}
	}
}

// The boot-version rule carries the whole native update model: `current` (what
// auto-update assembles) must win over a stale env pin, because relaunches inherit
// the environment — the self-update handover preserves it verbatim, so a pin from
// the night of the migration would otherwise downgrade every future update.
func TestNativeBootVersion(t *testing.T) {
	cases := []struct {
		current, pinned, launcher, want string
		why                             string
	}{
		// The case that makes Docker-free installable by ordinary people: nothing assembled,
		// nothing pinned, so assemble the app that ships with THIS launcher's release.
		{"", "", "0.8.11", "0.8.11", "a fresh install assembles its own release"},
		{"", "", "dev", "", "a local dev build matches no release; the pin is required"},
		{"", "", "", "", "an unstamped build likewise"},
		{"", "0.8.4", "0.8.11", "0.8.4", "an explicit pin still wins on a first run"},
		{"0.8.5", "", "0.8.11", "0.8.5", "normal operation boots what is assembled, NOT the launcher's version"},
		{"0.8.5", "0.8.5", "0.8.11", "0.8.5", "pin agreeing with current changes nothing"},
		{"0.8.5", "0.8.4", "0.8.11", "0.8.5", "STALE pin must never downgrade an updated install"},
		{"0.8.5", "0.9.0", "0.8.11", "0.9.0", "a deliberately newer pin is a manual upgrade"},
		{"0.8.5", "garbage", "0.8.11", "0.8.5", "an unparseable pin is ignored (fail-closed)"},
	}
	for _, c := range cases {
		if got := nativeBootVersion(c.current, c.pinned, c.launcher); got != c.want {
			t.Fatalf("nativeBootVersion(%q, %q, %q) = %q, want %q (%s)",
				c.current, c.pinned, c.launcher, got, c.want, c.why)
		}
	}
}

// A check that could not run must ask to be retried SOON. Getting this wrong is what
// makes "I restarted twice and nothing happened" a real user experience: the launcher
// is busy assembling on first launch, the update check skips, and the next look is six
// hours away.
func TestUpdateCheckAsksToRetryWhenItCouldNotLook(t *testing.T) {
	oldDir := sb.Dir
	sb.Dir = t.TempDir()
	defer func() { sb.Dir = oldDir }()

	upd := update.New("1.0.0")
	upd.FetchBody = func(context.Context, string) ([]byte, error) {
		return []byte(`{"tag_name":"v9.9.9"}`), nil
	}

	// 1. Nothing assembled yet (a first launch still building) -> retry soon.
	if checkNativeUpdate(context.Background(), upd) {
		t.Fatal("with nothing assembled the checker must ask for a prompt retry")
	}

	// Plant a completed assembly so the check gets past that gate.
	vdir := filepath.Join(sb.Dir, "native", "versions", "1.0.0")
	if err := os.MkdirAll(vdir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(vdir, ".complete"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sb.Dir, "native", "current"), []byte("1.0.0\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	// 2. An operation is in flight (start/stop/install holds the lock) -> retry soon,
	//    NOT in six hours. This is the case that stranded a real update.
	mu.Lock()
	got := checkNativeUpdate(context.Background(), upd)
	mu.Unlock()
	if got {
		t.Fatal("a check blocked by an in-flight operation must ask for a prompt retry")
	}

	// 3. A real look that finds nothing new -> the normal long interval, no hot loop.
	upd.FetchBody = func(context.Context, string) ([]byte, error) {
		return []byte(`{"tag_name":"v1.0.0"}`), nil
	}
	if !checkNativeUpdate(context.Background(), upd) {
		t.Fatal("an actual look that finds nothing new must not schedule a fast retry")
	}
}

func TestRetryDelayIsMuchShorterThanTheInterval(t *testing.T) {
	if updateRetryDelay >= updateInterval {
		t.Fatal("a retry must come sooner than the regular interval, or it is not a retry")
	}
	if updateFirstDelay > 60*time.Second {
		t.Fatal("the first look after launch should be prompt: users restart expecting an update")
	}
}

// The menu must answer "which version am I on?" — and must name BOTH numbers only when
// they disagree. They disagree exactly during an update, when the desktop app has been
// replaced but the SmartBrain it supervises has not, which is when a single number is
// actively misleading.
func TestVersionLabel(t *testing.T) {
	cases := []struct {
		app, launcher, want string
	}{
		{"0.8.8", "0.8.8", "Version 0.8.8"},
		{"0.8.7", "0.8.8", "SmartBrain 0.8.7 · desktop app 0.8.8"},
		{"0.8.8", "dev", "Version 0.8.8"}, // a dev build is not worth naming
		{"0.8.8", "", "Version 0.8.8"},    // nor an unstamped one
	}
	for _, c := range cases {
		if got := versionLabel(c.app, c.launcher); got != c.want {
			t.Fatalf("versionLabel(%q, %q) = %q, want %q", c.app, c.launcher, got, c.want)
		}
	}
}

// "Open logs" must land somewhere real on every install: natively that is run/, where
// app.log and bifrost.log live; a Docker install writes no log files (container logs
// live in the daemon), so the click shows the app-data folder rather than an empty
// folder invented for it.
func TestLogsDir(t *testing.T) {
	dir := filepath.Join("some", "data")
	if got, want := logsDir(true, dir), filepath.Join(dir, "native", "run"); got != want {
		t.Fatalf("logsDir(native) = %q, want %q", got, want)
	}
	if got := logsDir(false, dir); got != dir {
		t.Fatalf("logsDir(docker) = %q, want the data dir %q", got, dir)
	}
}

// A healthy answer on the app port is not proof that WE are serving it. Anyone still on
// the Docker stack publishes the same port with restart: unless-stopped, so it answers at
// handover. Adopting that would mark the machine "running natively" while nothing native
// exists, skip the data migration, and then wedge updates forever — the update check gives
// up when no version is assembled.
func TestShouldAdopt(t *testing.T) {
	cases := []struct {
		assembled string
		healthy   bool
		want      bool
		why       string
	}{
		{"0.8.12", true, true, "our own assembly is answering — adopt it, do not start a second"},
		{"", true, false, "SOMETHING answers but we assembled nothing: not ours"},
		{"0.8.12", false, false, "we have an assembly but nothing is running — start it"},
		{"", false, false, "nothing assembled, nothing running — a first install"},
	}
	for _, c := range cases {
		if got := shouldAdopt(c.assembled, c.healthy); got != c.want {
			t.Fatalf("shouldAdopt(%q, %v) = %v, want %v (%s)", c.assembled, c.healthy, got, c.want, c.why)
		}
	}
}

// The persona table: which face a bare launch wears. Only linux ever leaves the
// tray — and it must SAY so, because systray itself registers into the void
// silently when no SNI host is on the bus.
func TestPickPersona(t *testing.T) {
	cases := []struct {
		goos           string
		graphical, sni bool
		want           persona
		why            string
	}{
		{"darwin", false, false, personaTray, "macOS always has a menu bar; the probes are never consulted"},
		{"windows", false, false, personaTray, "Windows always has a tray"},
		{"linux", true, true, personaTray, "a desktop with an SNI host draws the tray"},
		{"linux", true, false, personaHeadlessNoTray, "a desktop without one runs headless — and gets told why"},
		{"linux", false, true, personaHeadless, "no display server: a server or SSH session"},
		{"linux", false, false, personaHeadless, "…with no bus either, likewise"},
	}
	for _, c := range cases {
		if got := pickPersona(c.goos, c.graphical, c.sni); got != c.want {
			t.Fatalf("pickPersona(%q, graphical=%v, sni=%v) = %v, want %v (%s)",
				c.goos, c.graphical, c.sni, got, c.want, c.why)
		}
	}
}

// Verbs come from argv[1]; anything flag-shaped is not a verb (older macOS passed
// -psn_… to Finder-launched apps, which must still land in the tray).
func TestCliVerb(t *testing.T) {
	cases := []struct {
		args []string
		verb string
		ok   bool
	}{
		{[]string{"smartbrain"}, "", false},
		{[]string{"smartbrain", "run"}, "run", true},
		{[]string{"smartbrain", "status"}, "status", true},
		{[]string{"smartbrain", "-psn_0_12345"}, "", false},
		{[]string{"smartbrain", "--flag"}, "", false},
	}
	for _, c := range cases {
		verb, ok := cliVerb(c.args)
		if verb != c.verb || ok != c.ok {
			t.Fatalf("cliVerb(%v) = %q, %v; want %q, %v", c.args, verb, ok, c.verb, c.ok)
		}
	}
}

func TestRunVerbRejectsUnknown(t *testing.T) {
	if got := runVerb("frobnicate"); got != 2 {
		t.Fatalf("an unknown verb must print usage and exit 2, got %d", got)
	}
}

func TestCgroupIsOurServiceIgnoresDesktopScopes(t *testing.T) {
	// GNOME launches desktop apps inside transient scopes and leaks INVOCATION_ID into
	// their environment; only the real unit may claim systemd will restart us.
	if cgroupIsOurService("0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-smartbrain-2231.scope\n") {
		t.Fatal("a desktop app scope must not count as our service")
	}
	if !cgroupIsOurService("0::/user.slice/user-1000.slice/user@1000.service/app.slice/smartbrain.service\n") {
		t.Fatal("our unit must be recognized")
	}
	if cgroupIsOurService("") {
		t.Fatal("empty cgroup must not count")
	}
}

func TestPendingAssembledOffersOnlyWhatIsNewerThanRunning(t *testing.T) {
	cases := []struct{ current, running, want string }{
		{"0.9.27", "0.9.26", "0.9.27"}, // staged, restart adopted the old app: offer it
		{"0.9.27", "0.9.27", ""},       // running what is assembled
		{"0.9.26", "0.9.27", ""},       // pointer behind the app (pinned/dev): nothing to offer
		{"", "0.9.26", ""},             // nothing assembled
		{"0.9.27", "", ""},             // app hasn't said what it runs yet
	}
	for _, c := range cases {
		if got := pendingAssembled(c.current, c.running); got != c.want {
			t.Fatalf("pendingAssembled(%q, %q) = %q, want %q", c.current, c.running, got, c.want)
		}
	}
}

// `smartbrain status` asks the app what it runs. Presenting the launcher's token with
// nothing staged is how the app learns the launcher WITHDREW an update offer, so the
// status probe must stay anonymous or the in-app banner vanishes until the next beat.
func TestStatusProbeDoesNotSpeakAsTheLauncher(t *testing.T) {
	var auth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth = r.Header.Get("Authorization")
		_, _ = w.Write([]byte(`{"version":"1.2.3"}`))
	}))
	defer srv.Close()
	prevSB, prevTok := sb, stack.LocalAPIToken
	t.Cleanup(func() { sb, stack.LocalAPIToken = prevSB, prevTok })
	sb = stack.Stack{Port: srv.Listener.Addr().(*net.TCPAddr).Port}
	stack.LocalAPIToken = strings.Repeat("T", 40)
	if code := cmdStatus(); code != 0 {
		t.Fatalf("status exit = %d, want 0", code)
	}
	if auth != "" {
		t.Errorf("status presented the launcher token %q", auth)
	}
}

// Real /api/status/overview answers, produced by the app's own status code (locked, in its
// Docker image). From an app before the `renderer` field (B2 rows only): the Docker stack
// with no sidecar, the page browser switched off (SMARTBRAIN_NO_BROWSER=1), a sidecar
// answering with this release's engine, and one answering with another build.
const (
	statusNoSidecar    = `{"browsers": [{"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura", "pct": 0, "phase": "unavailable", "reason": "docker_no_sidecar", "sandbox": {"kind": "none", "verified_at": null}, "version": "0.2.4"}, {"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura-stealth", "pct": 0, "phase": "unavailable", "reason": "docker_no_sidecar", "sandbox": {"kind": "none", "verified_at": null}, "version": "0.2.4"}], "memory": {"python": "3.14.7", "rss_bytes": 123547648}, "storage": {"browser_bytes": 0, "data_dir": "/tmp/sb", "db_bytes": 0, "models_bytes": 0, "total_bytes": 0}, "unlocked": false, "version": "0.0.0-dev", "voice_local": {"error": "", "pct": 0, "phase": "absent"}}`
	statusDisabled     = `{"browsers": [{"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura", "pct": 0, "phase": "disabled", "reason": "disabled", "sandbox": {"kind": "none", "verified_at": null}, "version": "0.2.4"}, {"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura-stealth", "pct": 0, "phase": "disabled", "reason": "disabled", "sandbox": {"kind": "none", "verified_at": null}, "version": "0.2.4"}], "unlocked": false, "version": "0.0.0-dev"}`
	statusSidecarReady = `{"browsers": [{"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura", "pct": 100, "phase": "ready", "reason": "", "sandbox": {"kind": "sidecar", "verified_at": null}, "version": "0.2.4"}, {"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura-stealth", "pct": 0, "phase": "absent", "reason": "tier_disabled", "sandbox": {"kind": "sidecar", "verified_at": null}, "version": "0.2.4"}], "unlocked": false, "version": "0.0.0-dev"}`
	statusSidecarStale = `{"browsers": [{"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura", "pct": 0, "phase": "absent", "reason": "", "sandbox": {"kind": "sidecar", "verified_at": null}, "version": "0.2.4"}, {"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura-stealth", "pct": 0, "phase": "absent", "reason": "tier_disabled", "sandbox": {"kind": "sidecar", "verified_at": null}, "version": "0.2.4"}], "unlocked": false, "version": "0.0.0-dev"}`
)

// The same, from an app WITH the `renderer` field (B3a): a fresh start no card has needed a
// render in yet; a card needed one and no sidecar runs; a sidecar left on another build
// (READY names 0.25.0); the sidecar ready for this build; a B2-era sidecar whose READY names
// no build (stale too); and a card's need while the page browser is switched off (`wanted`
// is set regardless — the launcher must still leave it alone).
const (
	statusFresh        = `{"browsers": [{"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura", "pct": 0, "phase": "unavailable", "reason": "docker_no_sidecar", "sandbox": {"kind": "none", "verified_at": null}, "version": "0.2.4"}, {"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura-stealth", "pct": 0, "phase": "unavailable", "reason": "docker_no_sidecar", "sandbox": {"kind": "none", "verified_at": null}, "version": "0.2.4"}], "renderer": {"sidecar": "missing", "wanted": false}, "unlocked": false, "version": "0.0.0-dev"}`
	statusWanted       = `{"browsers": [{"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura", "pct": 0, "phase": "unavailable", "reason": "docker_no_sidecar", "sandbox": {"kind": "none", "verified_at": null}, "version": "0.2.4"}, {"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura-stealth", "pct": 0, "phase": "unavailable", "reason": "docker_no_sidecar", "sandbox": {"kind": "none", "verified_at": null}, "version": "0.2.4"}], "renderer": {"sidecar": "missing", "wanted": true}, "unlocked": false, "version": "0.0.0-dev"}`
	statusWantedStale  = `{"browsers": [{"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura", "pct": 100, "phase": "ready", "reason": "", "sandbox": {"kind": "sidecar", "verified_at": null}, "version": "0.2.4"}, {"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura-stealth", "pct": 0, "phase": "absent", "reason": "tier_disabled", "sandbox": {"kind": "sidecar", "verified_at": null}, "version": "0.2.4"}], "renderer": {"sidecar": "stale", "wanted": true}, "unlocked": false, "version": "0.0.0-dev"}`
	statusWantedReady  = `{"browsers": [{"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura", "pct": 100, "phase": "ready", "reason": "", "sandbox": {"kind": "sidecar", "verified_at": null}, "version": "0.2.4"}, {"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura-stealth", "pct": 0, "phase": "absent", "reason": "tier_disabled", "sandbox": {"kind": "sidecar", "verified_at": null}, "version": "0.2.4"}], "renderer": {"sidecar": "ready", "wanted": true}, "unlocked": false, "version": "0.0.0-dev"}`
	statusWantedLegacy = `{"browsers": [{"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura", "pct": 100, "phase": "ready", "reason": "", "sandbox": {"kind": "sidecar", "verified_at": null}, "version": "0.2.4"}, {"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura-stealth", "pct": 0, "phase": "absent", "reason": "tier_disabled", "sandbox": {"kind": "sidecar", "verified_at": null}, "version": "0.2.4"}], "renderer": {"sidecar": "stale", "wanted": true}, "unlocked": false, "version": "0.0.0-dev"}`
	statusWantedOff    = `{"browsers": [{"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura", "pct": 0, "phase": "disabled", "reason": "disabled", "sandbox": {"kind": "none", "verified_at": null}, "version": "0.2.4"}, {"age_days": 8, "eligible": false, "error": "", "health": "ok", "last_self_test": "never", "name": "obscura-stealth", "pct": 0, "phase": "disabled", "reason": "disabled", "sandbox": {"kind": "none", "verified_at": null}, "version": "0.2.4"}], "renderer": {"sidecar": "missing", "wanted": true}, "unlocked": false, "version": "0.0.0-dev"}`
)

// The rule: with the `renderer` field, start when a card has needed a render and the
// sidecar is missing or stale; without it (an older app), when a browsers[] row says
// docker_no_sidecar. Never while the page browser is switched off. Everything unreadable is
// "" — a garbled answer must never start a container.
func TestRenderWanted(t *testing.T) {
	cases := []struct {
		name, status, want string
	}{
		{"renderer: no card has needed a render yet", statusFresh, ""},
		{"renderer: a card needs it, no sidecar", statusWanted, "missing"},
		{"renderer: a card needs it, sidecar on another build", statusWantedStale, "stale"},
		{"renderer: a card needs it, sidecar ready", statusWantedReady, ""},
		{"renderer: a B2-era sidecar (no build named) is stale", statusWantedLegacy, "stale"},
		{"renderer: switched off outranks wanted", statusWantedOff, ""},
		{"renderer: not Docker", `{"renderer":{"wanted":true,"sidecar":"n/a"}}`, ""},
		{"renderer: stale but not wanted", `{"renderer":{"wanted":false,"sidecar":"stale"}}`, ""},
		{"renderer: an unknown state", `{"renderer":{"wanted":true,"sidecar":"rebuilding"}}`, ""},
		{"renderer: outranks the rows", `{"renderer":{"wanted":false,"sidecar":"missing"},"browsers":[{"phase":"unavailable","reason":"docker_no_sidecar"}]}`, ""},
		{"renderer: null reads as absent", `{"renderer":null,"browsers":[{"phase":"unavailable","reason":"docker_no_sidecar"}]}`, "missing"},
		{"renderer malformed: wanted not a bool", `{"renderer":{"wanted":"yes","sidecar":"missing"}}`, ""},
		{"renderer malformed: sidecar not a string", `{"renderer":{"wanted":true,"sidecar":7}}`, ""},
		{"renderer malformed: not an object", `{"renderer":["wanted","missing"]}`, ""},
		{"older app: Docker without a sidecar", statusNoSidecar, "missing"},
		{"older app: the page browser switched off", statusDisabled, ""},
		{"older app: a sidecar answering", statusSidecarReady, ""},
		{"older app: a sidecar on another build is not visible in the rows", statusSidecarStale, ""},
		{"older app: one row is enough", `{"browsers":[{"phase":"ready","reason":""},{"phase":"unavailable","reason":"docker_no_sidecar"}]}`, "missing"},
		{"older app: disabled outranks the reason", `{"browsers":[{"phase":"disabled","reason":"docker_no_sidecar"}]}`, ""},
		{"older app: a native wall's reason starts nothing", `{"browsers":[{"phase":"unavailable","reason":"userns_blocked"}]}`, ""},
		{"absent rows: none", `{"browsers":[]}`, ""},
		{"absent rows: an app from before the page browser", `{"version":"0.25.0","unlocked":true}`, ""},
		{"absent rows: null", `{"browsers":null}`, ""},
		{"malformed: not JSON", `<html>502 Bad Gateway</html>`, ""},
		{"malformed: truncated", statusWanted[:150], ""},
		{"malformed: rows not a list", `{"browsers":{"phase":"unavailable","reason":"docker_no_sidecar"}}`, ""},
		{"malformed: one garbled row spoils the answer", `{"renderer":{"wanted":true,"sidecar":"missing"},"browsers":[{"phase":"unavailable","reason":7}]}`, ""},
		{"malformed: empty body", ``, ""},
	}
	for _, c := range cases {
		if got := renderWanted([]byte(c.status)); got != c.want {
			t.Errorf("%s: renderWanted = %q, want %q", c.name, got, c.want)
		}
	}
}

// One start per need, then a pause: a sidecar still booting (no READY yet, so the app still
// says missing) is not asked for again on the next beat. The app going away — Stop,
// Restart, an update — re-arms the gate, so its next run is answered at once.
func TestRenderGateStartsWhenACardNeedsIt(t *testing.T) {
	var g renderGate
	starts := 0
	start := func() error { starts++; return nil }
	now := time.Date(2026, 10, 12, 9, 0, 0, 0, time.UTC)
	beat := func(status string, ok bool) string {
		now = now.Add(handshakeInterval)
		return g.step(now, []byte(status), ok, start)
	}
	if line := beat("", false); line != "" || starts != 0 {
		t.Fatalf("the app not answering yet: nothing to do, got %q and %d starts", line, starts)
	}
	for i := 0; i < 3; i++ {
		if line := beat(statusFresh, true); line != "" || starts != 0 {
			t.Fatalf("no card has needed a render: no start, got %q and %d starts", line, starts)
		}
	}
	if line := beat(statusWanted, true); starts != 1 || line != "page browser: the renderer sidecar was missing; ran docker compose --profile render up -d render" {
		t.Fatalf("a card needs it and no sidecar runs: one start, one line, got %q and %d starts", line, starts)
	}
	for _, status := range []string{statusWanted, statusWanted, statusWantedReady, statusWanted} {
		if line := beat(status, true); line != "" || starts != 1 {
			t.Fatalf("within the pause: never asked again, got %q and %d starts", line, starts)
		}
	}
	if line := beat("", false); line != "" {
		t.Fatalf("the app going away logs nothing, got %q", line)
	}
	if line := beat(statusWanted, true); starts != 2 || line == "" {
		t.Fatalf("the next app run is answered at once, got %q and %d starts", line, starts)
	}
	g = renderGate{}
	for i := 0; i < 3; i++ {
		if line := beat(statusWantedOff, true); line != "" {
			t.Fatalf("switched off: nothing, ever, got %q", line)
		}
	}
	if starts != 2 {
		t.Fatalf("switched off: no start, got %d", starts)
	}
}

// A stale sidecar gets the SAME start (the named service with pull_policy: always pulls the
// newer image and recreates it — measured against a registry). If the release's renderer
// image is not out yet, the sidecar stays stale; the gate asks again after the pause, not on
// every beat, until it is current.
func TestRenderGateRecreatesAStaleSidecar(t *testing.T) {
	var g renderGate
	starts := 0
	start := func() error { starts++; return nil }
	t0 := time.Date(2026, 10, 12, 9, 0, 0, 0, time.UTC)
	if line := g.step(t0, []byte(statusWantedStale), true, start); starts != 1 ||
		line != "page browser: the renderer sidecar was stale; ran docker compose --profile render up -d render" {
		t.Fatalf("stale: one start, one line, got %q and %d starts", line, starts)
	}
	for at := t0.Add(handshakeInterval); at.Before(t0.Add(renderRetryDelay)); at = at.Add(handshakeInterval) {
		if l := g.step(at, []byte(statusWantedStale), true, start); l != "" {
			t.Fatalf("still stale within the pause: no second start, got %q", l)
		}
	}
	if l := g.step(t0.Add(renderRetryDelay), []byte(statusWantedLegacy), true, start); starts != 2 || !strings.Contains(l, "stale") {
		t.Fatalf("still stale after the pause: asked again, got %q and %d starts", l, starts)
	}
	if l := g.step(t0.Add(3*renderRetryDelay), []byte(statusWantedReady), true, start); l != "" || starts != 2 {
		t.Fatalf("current: nothing to do, got %q and %d starts", l, starts)
	}
}

// A failed start (offline, the image out of reach) owes exactly one log line, is not
// repeated on every beat, and is tried again after renderRetryDelay. The start is the only
// thing the gate runs, so its failure cannot reach the app.
func TestRenderGateFailedStartLogsOneLineAndRetriesLater(t *testing.T) {
	var g renderGate
	calls := 0
	fail := func() error {
		calls++
		return errors.New("docker [--profile render up -d render]: exit status 18: render Pulling\n" +
			"render Error pull access denied for ghcr.io/securecloudgroup/smartbrain_3000-render\nError response from daemon: denied")
	}
	t0 := time.Date(2026, 10, 12, 9, 0, 0, 0, time.UTC)
	line := g.step(t0, []byte(statusWanted), true, fail)
	if calls != 1 || strings.Contains(line, "\n") || !strings.Contains(line, "missing and did not start") ||
		!strings.Contains(line, "pull access denied") {
		t.Fatalf("a failed start owes one line naming the cause, got %q (%d calls)", line, calls)
	}
	for at := t0.Add(handshakeInterval); at.Before(t0.Add(renderRetryDelay)); at = at.Add(handshakeInterval) {
		if l := g.step(at, []byte(statusWanted), true, fail); l != "" {
			t.Fatalf("no retry and no line before the delay, got %q", l)
		}
	}
	if calls != 1 {
		t.Fatalf("retried before the delay: %d calls", calls)
	}
	ok := func() error { calls++; return nil }
	if l := g.step(t0.Add(renderRetryDelay), []byte(statusWanted), true, ok); calls != 2 || !strings.Contains(l, "ran docker compose") {
		t.Fatalf("after the delay it tries again, got %q (%d calls)", l, calls)
	}
	long := oneLine("docker [--profile render up -d render]: exit status 1: " +
		strings.Repeat("render Downloading [=====>    ] é\n", 60) + "Error response from daemon: no space left on device")
	if n := len([]rune(long)); n > 303 || strings.Contains(long, "\n") ||
		!strings.Contains(long, "exit status 1") || !strings.HasSuffix(long, "no space left on device") {
		t.Fatalf("a long error must stay one bounded line keeping both ends, got %d runes: %q", n, long)
	}
}

// Golden: the exact command the sidecar start runs, for a missing sidecar and a stale one
// alike. The service is NAMED on purpose — a bare `--profile render up -d` converges every
// service, and the app's pull_policy: always then installs an update the background pull
// staged, with no click (measured). Named, the render service's own pull_policy: always pulls
// a moved tag and recreates a stale sidecar on it, the app untouched (measured).
func TestRenderStartArgv(t *testing.T) {
	s := stack.Stack{Dir: filepath.Join("some", "data"), Port: stack.DefaultPort}
	want := []string{"compose", "-f", filepath.Join("some", "data", "docker-compose.release.yml"),
		"--profile", "render", "up", "-d", "render"}
	if got := s.RenderUpArgs(); !slices.Equal(got, want) {
		t.Fatalf("RenderUpArgs = %q, want %q", got, want)
	}
}

// The command names a profile and a service; the compose file the launcher writes must
// declare exactly those, on the published image under the app's own tag variable — or every
// start would fail with "no such service".
func TestEmbeddedComposeDeclaresTheRenderService(t *testing.T) {
	args := stack.Stack{Dir: "d", Port: stack.DefaultPort}.RenderUpArgs()
	compose := strings.ReplaceAll(string(composeFile), "\r\n", "\n") // a CRLF checkout reads the same
	for _, want := range []string{
		"\n  " + args[len(args)-1] + ":\n",
		`profiles: ["` + stack.RenderProfile + `"]`,
		"image: ghcr.io/securecloudgroup/smartbrain_3000-render:${SMARTBRAIN_VERSION:-latest}",
		"image: ghcr.io/securecloudgroup/smartbrain_3000:${SMARTBRAIN_VERSION:-latest}",
	} {
		if !strings.Contains(compose, want) {
			t.Errorf("the embedded compose file lacks %q", want)
		}
	}
}

// --no-render rides a verb or a bare launch alike, and is never mistaken for a verb.
func TestNoRenderFlag(t *testing.T) {
	cases := []struct {
		args []string
		want bool
	}{
		{nil, false},
		{[]string{"--no-render"}, true},
		{[]string{"run", "--no-render"}, true},
		{[]string{"run"}, false},
		{[]string{"--no-renderer"}, false},
		{[]string{"-psn_0_12345"}, false},
	}
	for _, c := range cases {
		if got := noRenderFlag(c.args); got != c.want {
			t.Errorf("noRenderFlag(%q) = %v, want %v", c.args, got, c.want)
		}
	}
	if _, ok := cliVerb([]string{"smartbrain", "--no-render"}); ok {
		t.Fatal("a bare `smartbrain --no-render` must still land in the tray, not in a verb")
	}
}
