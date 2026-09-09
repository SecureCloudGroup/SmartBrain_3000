package main

import (
	"testing"

	"github.com/SecureCloudGroup/SmartBrain_3000/launcher/stack"
)

// The first successful poll must only record the baseline: a launcher (re)start
// replaying the last ten notices as ten toasts is exactly the storm §17 forbids.
func TestSelectNotificationsBaselinesWithoutToasting(t *testing.T) {
	notices := []stack.Notice{
		{ID: 30, Kind: "alert", Body: "c"},
		{ID: 20, Kind: "broken", Body: "b"},
		{ID: 10, Kind: "repaired", Body: "a"},
	}
	toasts, last := selectNotifications(notices, -1)
	if len(toasts) != 0 {
		t.Fatalf("first poll must not toast, got %d toasts", len(toasts))
	}
	if last != 30 {
		t.Fatalf("baseline must be the highest id, got %d", last)
	}
	// An empty first answer still counts as a baseline (0), so the next real
	// notice — whose id is always positive — toasts.
	if _, last := selectNotifications(nil, -1); last != 0 {
		t.Fatalf("empty first poll must baseline at 0, got %d", last)
	}
}

func TestSelectNotificationsDedupesByHighestSeenID(t *testing.T) {
	notices := []stack.Notice{
		{ID: 30, Kind: "alert", Body: "newest"},
		{ID: 20, Kind: "alert", Body: "seen"},
		{ID: 10, Kind: "alert", Body: "older"},
	}
	toasts, last := selectNotifications(notices, 20)
	if len(toasts) != 1 || toasts[0].body != "newest" {
		t.Fatalf("only ids above the mark are new, got %+v", toasts)
	}
	if last != 30 {
		t.Fatalf("mark must advance to 30, got %d", last)
	}
	// Nothing new: no toasts, and the mark never moves backwards.
	toasts, last = selectNotifications(notices, 30)
	if len(toasts) != 0 || last != 30 {
		t.Fatalf("no news must mean no toasts and an unmoved mark, got %+v, %d", toasts, last)
	}
}

// Each kind names its toast so the notification says what KIND of news arrived
// before the user reads a word of the body.
func TestSelectNotificationsTitlesByKind(t *testing.T) {
	notices := []stack.Notice{
		{ID: 3, Kind: "repaired", Body: "r"},
		{ID: 2, Kind: "broken", Body: "b"},
		{ID: 1, Kind: "alert", Body: "a"},
	}
	toasts, _ := selectNotifications(notices, 0)
	want := []string{
		"SmartBrain: card repaired itself",
		"SmartBrain: card needs attention",
		"SmartBrain alert",
	}
	if len(toasts) != 3 {
		t.Fatalf("three new notices must toast individually, got %d", len(toasts))
	}
	for i, w := range want {
		if toasts[i].title != w {
			t.Fatalf("toast %d title = %q, want %q", i, toasts[i].title, w)
		}
	}
	// An unrecognized kind falls back to the mildest title rather than vanishing.
	toasts, _ = selectNotifications([]stack.Notice{{ID: 9, Kind: "surprise", Body: "x"}}, 0)
	if len(toasts) != 1 || toasts[0].title != "SmartBrain alert" {
		t.Fatalf("unknown kind must fall back to the alert title, got %+v", toasts)
	}
}

// A backlog burst must never storm the desktop: at most three notifications,
// the newest two individually and the rest collapsed into one summary.
func TestSelectNotificationsCollapsesABurst(t *testing.T) {
	notices := []stack.Notice{
		{ID: 50, Kind: "alert", Body: "e"},
		{ID: 40, Kind: "alert", Body: "d"},
		{ID: 30, Kind: "alert", Body: "c"},
		{ID: 20, Kind: "alert", Body: "b"},
		{ID: 10, Kind: "alert", Body: "a"},
	}
	toasts, last := selectNotifications(notices, 0)
	if len(toasts) != 3 {
		t.Fatalf("a burst must cap at %d toasts, got %d", maxToastsPerPoll, len(toasts))
	}
	if toasts[0].body != "e" || toasts[1].body != "d" {
		t.Fatalf("the newest notices show individually, got %+v", toasts)
	}
	if toasts[2].body != "…and 3 more on your Neural Interface." {
		t.Fatalf("extras must collapse into the summary line, got %q", toasts[2].body)
	}
	if last != 50 {
		t.Fatalf("the mark covers collapsed notices too (no re-toast next poll), got %d", last)
	}
	// Exactly the cap: all individual, no summary.
	toasts, _ = selectNotifications(notices[:3], 0)
	if len(toasts) != 3 || toasts[2].body != "c" {
		t.Fatalf("exactly %d new notices need no summary, got %+v", maxToastsPerPoll, toasts)
	}
}
