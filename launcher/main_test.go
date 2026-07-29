package main

import "testing"

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
