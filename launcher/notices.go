// The tray's Neural Interface notices (ni-format §17): a 60-second poll of the
// local app's /api/ni/notices, surfacing NEW entries as desktop notifications.
// The app being down or the vault locked (423) reads as "no news" — a locked
// vault must produce no notifications at all, so sealed content never crosses
// the unlock boundary. Windows shows nothing: stack.Notify is a no-op there.
package main

import (
	"context"
	"fmt"
	"time"

	"github.com/SecureCloudGroup/SmartBrain_3000/launcher/stack"
)

const noticesInterval = 60 * time.Second

// lastSeenNotice is the highest notice id already surfaced — the
// lastNotifiedVersion idiom, in memory only: a launcher restart re-baselines
// rather than replaying history. -1 means no successful poll yet, so the first
// answer only records the baseline and old notices never toast on launch.
// Touched only by the noticesLoop goroutine.
var lastSeenNotice int64 = -1

// noticesLoop polls for new NI notices and toasts them. Same shape as its
// siblings updateChecker and handshakeLoop: sleep, one bounded call, act.
func noticesLoop() {
	for {
		time.Sleep(noticesInterval)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		notices, ok := stack.FetchNotices(ctx, sb.Port)
		cancel()
		if !ok {
			continue // down, locked (423), or garbled — silently skip this poll
		}
		toasts, last := selectNotifications(notices, lastSeenNotice)
		lastSeenNotice = last
		for _, t := range toasts {
			stack.Notify(t.title, t.body)
		}
	}
}

// toast is one desktop notification to show.
type toast struct{ title, body string }

// noticeTitle names the toast by what KIND of news it carries (§17).
func noticeTitle(kind string) string {
	switch kind {
	case "broken":
		return "SmartBrain: card needs attention"
	case "repaired":
		return "SmartBrain: card repaired itself"
	default:
		return "SmartBrain alert"
	}
}

// maxToastsPerPoll caps a burst at three notifications, never a storm: a bigger
// batch shows the newest two plus one "…and N more" summary.
const maxToastsPerPoll = 3

// selectNotifications is the pure half of the poll (loops are not testable;
// selection is): given the newest-first notices and the highest id already
// surfaced, pick what to toast and the new high-water mark.
//
//   - lastSeen < 0 means "no baseline yet": the first successful poll only
//     records the mark (an empty answer baselines at 0), so a launcher
//     (re)start never replays old notices.
//   - Otherwise every notice with id > lastSeen is new. Up to three toast
//     individually; a bigger burst keeps the newest two and collapses the rest
//     into one summary line, so a backlog can never storm the desktop.
func selectNotifications(notices []stack.Notice, lastSeen int64) ([]toast, int64) {
	high := lastSeen
	if high < 0 {
		high = 0
	}
	var fresh []stack.Notice
	for _, n := range notices {
		if n.ID > high {
			high = n.ID
		}
		if lastSeen >= 0 && n.ID > lastSeen {
			fresh = append(fresh, n) // stays newest-first
		}
	}
	shown := len(fresh)
	if shown > maxToastsPerPoll {
		shown = maxToastsPerPoll - 1 // leave room for the summary line
	}
	var toasts []toast
	for _, n := range fresh[:shown] {
		toasts = append(toasts, toast{noticeTitle(n.Kind), n.Body})
	}
	if len(fresh) > maxToastsPerPoll {
		toasts = append(toasts, toast{"SmartBrain",
			fmt.Sprintf("…and %d more on your Neural Interface.", len(fresh)-shown)})
	}
	return toasts, high
}
