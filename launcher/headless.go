// The launcher's tray-less half: CLI verbs and the headless persona. One binary
// serves every seat — a desktop gets the tray, a server (or a desktop with no tray
// host) gets the same supervisor without menus, and scripts/systemd get verbs. The
// heavy lifting all lives in main.go and the stack/native packages; this file only
// routes into it.
package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/SecureCloudGroup/SmartBrain_3000/launcher/native"
	"github.com/SecureCloudGroup/SmartBrain_3000/launcher/stack"
)

const usageText = `usage: smartbrain [command]

  (none)   desktop tray — falls back to headless where no tray can be drawn
  run      run in the foreground without a tray (what a systemd unit runs)
  start    start SmartBrain and print its URL
  stop     stop SmartBrain (your data is kept)
  status   report whether SmartBrain is running (exit 0 yes, 1 no)
  version  print the launcher and app versions
`

// runVerb executes one CLI verb and returns its exit code.
func runVerb(verb string) int {
	switch verb {
	case "run", "start", "stop", "status", "version":
	default:
		fmt.Fprint(os.Stderr, usageText)
		return 2
	}
	if verb == "run" {
		runHeadless() // does its own stack init; returns only after a clean stop
		return 0
	}
	var err error
	if sb, err = stack.New(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	switch verb {
	case "start":
		if err := sb.Install(composeFile); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		return cmdStart()
	case "stop":
		return cmdStop()
	case "status":
		return cmdStatus()
	default:
		return cmdVersion()
	}
}

// runHeadless is the tray-less persona: the same stack init and the exact three
// goroutines onReady launches, minus the menus (whose seams no-op — see setStatus
// and friends in main.go). SIGTERM/SIGINT stops the stack and exits 0, so a
// systemd unit's stop actually stops SmartBrain — unlike quitting the tray, which
// deliberately leaves it running for the browser.
func runHeadless() {
	var err error
	if sb, err = stack.New(); err != nil {
		log.Println("headless:", err)
		os.Exit(1)
	}
	if err = sb.Install(composeFile); err != nil {
		log.Println("headless:", err)
		os.Exit(1)
	}

	go start()         // bring it up on launch
	go updateChecker() // then quietly watch for a newer image
	go handshakeLoop() // and keep the app told about what is staged

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)
	s := <-sigCh
	log.Println(s, "— stopping SmartBrain")
	// Mirror the tray's Stop path. mu.Lock, not TryLock: an in-flight start must
	// finish before the teardown, or Down would race Up.
	mu.Lock()
	if nativeMode() {
		stopWatch() // the supervisor must not resurrect what we are stopping
		native.New(sb.Dir).Down()
	} else {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
		if err := sb.Down(ctx); err != nil {
			log.Println("down:", err)
		}
		cancel()
	}
	mu.Unlock()
	os.Exit(0)
}

// cmdStart brings SmartBrain up and prints its URL. It reuses the tray's start()
// wholesale — assembly, migration, adoption, the Docker fallback — with only the
// browser-open swapped out: a CLI verb prints where the app is instead of seizing
// the desktop. The stack's processes detach, so exiting afterwards is safe.
func cmdStart() int {
	openBrowser = func(string) error { return nil }
	start()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if !sb.Healthy(ctx) {
		fmt.Fprintln(os.Stderr, "SmartBrain did not come up — see the messages above")
		return 1
	}
	fmt.Println(sb.URL())
	return 0
}

// cmdStop is the tray's Stop without the menu.
func cmdStop() int {
	if nativeMode() {
		native.New(sb.Dir).Down()
		fmt.Println("stopped")
		return 0
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	if err := sb.Down(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "stop:", err)
		return 1
	}
	fmt.Println("stopped")
	return 0
}

// cmdStatus asks the app itself, on the port the browser would use. Exit codes
// make it scriptable: 0 running, 1 not.
func cmdStatus() int {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	running, _, ok := stack.Handshake(ctx, sb.Port, "")
	if !ok {
		fmt.Println("not running")
		return 1
	}
	if running != "" {
		fmt.Printf("running — version %s — %s\n", running, sb.URL())
	} else {
		fmt.Println("running —", sb.URL())
	}
	return 0
}

// cmdVersion names both halves: this binary, and the app assembly it supervises
// (absent on a Docker install, whose version lives in the image).
func cmdVersion() int {
	fmt.Println("launcher", launcherVersion)
	if cur := native.New(sb.Dir).Current(); cur != "" {
		fmt.Println("app", cur)
	}
	return 0
}

// underSystemd reports whether systemd is supervising this process (it sets
// INVOCATION_ID for every unit it runs). Then the unit's Restart= policy owns
// relaunching after a self-update — see checkForUpdate.
func underSystemd() bool { return os.Getenv("INVOCATION_ID") != "" }

// graphicalSession reports whether a desktop could draw ANY UI: a display server
// plus a session bus (systray's SNI backend needs both). Off linux it is always
// true and never consulted.
func graphicalSession() bool {
	return (os.Getenv("DISPLAY") != "" || os.Getenv("WAYLAND_DISPLAY") != "") && sessionBusReachable()
}
