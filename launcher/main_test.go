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
		env    string
		marker bool
		want   bool
		why    string
	}{
		{"1", false, true, "explicit opt-in works before any marker exists"},
		{"", true, true, "a marked machine stays native with no env at all"},
		{"0", true, false, "env 0 forces Docker for this run (the rollback)"},
		{"", false, false, "fresh machines keep the Docker default"},
		{"garbage", true, true, "unrecognized env defers to the marker"},
		{"garbage", false, false, "unrecognized env on a fresh machine stays Docker"},
	}
	for _, c := range cases {
		if got := resolveNativeMode(c.env, c.marker); got != c.want {
			t.Fatalf("resolveNativeMode(%q, %v) = %v, want %v (%s)",
				c.env, c.marker, got, c.want, c.why)
		}
	}
}

// The boot-version rule carries the whole native update model: `current` (what
// auto-update assembles) must win over a stale env pin, because relaunches inherit
// the environment — the self-update handover preserves it verbatim, so a pin from
// the night of the migration would otherwise downgrade every future update.
func TestNativeBootVersion(t *testing.T) {
	cases := []struct {
		current, pinned, want string
		why                   string
	}{
		{"", "", "", "nothing anywhere -> caller reports first-run guidance"},
		{"", "0.8.4", "0.8.4", "first run bootstraps from the pin"},
		{"0.8.5", "", "0.8.5", "normal operation boots what is assembled"},
		{"0.8.5", "0.8.5", "0.8.5", "pin agreeing with current changes nothing"},
		{"0.8.5", "0.8.4", "0.8.5", "STALE pin must never downgrade an updated install"},
		{"0.8.5", "0.9.0", "0.9.0", "a deliberately newer pin is a manual upgrade"},
		{"0.8.5", "garbage", "0.8.5", "an unparseable pin is ignored (fail-closed)"},
	}
	for _, c := range cases {
		if got := nativeBootVersion(c.current, c.pinned); got != c.want {
			t.Fatalf("nativeBootVersion(%q, %q) = %q, want %q (%s)",
				c.current, c.pinned, got, c.want, c.why)
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
