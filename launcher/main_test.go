package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

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
