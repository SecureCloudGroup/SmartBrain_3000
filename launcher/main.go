// SmartBrain launcher — a menu-bar/tray app whose only job is to make the app one click to reach:
// start Docker if needed, `docker compose up`, wait until it's healthy, and open the browser. It
// draws no app UI of its own (the real UI is the SvelteKit app in your browser), so it stays tiny.
//
// It is deliberately transparent: it writes ONE compose file into a per-user folder and shells out
// to `docker compose` exactly as you would by hand. Quitting the launcher leaves SmartBrain running
// (like Docker Desktop's own tray); use Stop to actually shut it down.
package main

import (
	"context"
	_ "embed"
	"log"
	"runtime"
	"sync"
	"time"

	"fyne.io/systray"

	"github.com/SecureCloudGroup/SmartBrain_3000/launcher/stack"
)

// The release compose file is baked into the binary and written to the app-data dir on start. It is
// drift-checked against compose/docker-compose.release.yml in CI so this copy can't fall behind.
//
//go:embed docker-compose.release.yml
var composeFile []byte

//go:embed icon/icon_mac.png
var iconMac []byte

//go:embed icon/icon_win.ico
var iconWin []byte

var (
	sb      stack.Stack
	mu      sync.Mutex // serialize compose ops so two quick menu clicks can't race
	mStatus *systray.MenuItem
)

func main() {
	// A Finder-launched .app on macOS gets launchd's minimal PATH, which hides /usr/local/bin and
	// Homebrew — so `docker` would look "not installed". Fix PATH before any Docker check runs.
	stack.EnsureDockerPath()
	systray.Run(onReady, func() {})
}

func onReady() {
	if runtime.GOOS == "darwin" {
		systray.SetTemplateIcon(iconMac, iconMac) // template = auto light/dark in the macOS menu bar
	} else {
		systray.SetIcon(iconWin)
	}
	systray.SetTooltip("SmartBrain")

	mOpen := systray.AddMenuItem("Open SmartBrain", "Open the app in your browser")
	mStatus = systray.AddMenuItem("Starting…", "")
	mStatus.Disable() // a label, not a button
	systray.AddSeparator()
	mStop := systray.AddMenuItem("Stop", "Stop SmartBrain (your data is kept)")
	mRestart := systray.AddMenuItem("Restart", "Restart SmartBrain")
	systray.AddSeparator()
	mQuit := systray.AddMenuItem("Quit launcher", "Quit this menu — SmartBrain keeps running")

	var err error
	if sb, err = stack.New(); err != nil {
		setStatus("Error: " + err.Error())
		return
	}
	if err = sb.Install(composeFile); err != nil {
		setStatus("Error: " + err.Error())
		return
	}

	go start() // bring it up on launch

	go func() {
		for {
			select {
			case <-mOpen.ClickedCh:
				go openOrStart()
			case <-mStop.ClickedCh:
				go stop()
			case <-mRestart.ClickedCh:
				go start()
			case <-mQuit.ClickedCh:
				systray.Quit()
				return
			}
		}
	}()
}

func setStatus(s string) { mStatus.SetTitle(s) }

// start ensures Docker is up, starts the stack, waits for health, and opens the browser. Held under
// mu so a Restart mid-start queues behind the start instead of racing it.
func start() {
	mu.Lock()
	defer mu.Unlock()
	ctx := context.Background()

	if !stack.DockerRunning(ctx) {
		if !stack.DockerInstalled() {
			setStatus("Docker isn't installed — install Docker Desktop, then Restart")
			return
		}
		setStatus("Starting Docker…")
		stack.TryStartDocker(ctx)
		if !waitDocker(ctx, 90*time.Second) {
			setStatus("Docker isn't running — start Docker, then Restart")
			return
		}
	}

	setStatus("Starting… (first run downloads the app)")
	if err := sb.Up(ctx); err != nil {
		setStatus("Couldn't start — check Docker has disk space")
		log.Println("up:", err)
		return
	}
	// A first `up` pulls images, so allow generous time before calling it stuck.
	if sb.WaitHealthy(ctx, 6*time.Minute) {
		setStatus("Running ●")
		if err := stack.OpenBrowser(sb.URL()); err != nil {
			log.Println("open browser:", err)
		}
	} else {
		setStatus("Still warming up — click Open in a moment")
	}
}

// openOrStart opens the browser if the app is already up, otherwise starts it first.
func openOrStart() {
	if sb.Healthy(context.Background()) {
		if err := stack.OpenBrowser(sb.URL()); err != nil {
			log.Println("open browser:", err)
		}
		return
	}
	start()
}

func stop() {
	mu.Lock()
	defer mu.Unlock()
	setStatus("Stopping…")
	if err := sb.Down(context.Background()); err != nil {
		setStatus("Couldn't stop")
		log.Println("down:", err)
		return
	}
	setStatus("Stopped")
}

// waitDocker polls until the daemon answers or the deadline passes.
func waitDocker(ctx context.Context, deadline time.Duration) bool {
	ctx, cancel := context.WithTimeout(ctx, deadline)
	defer cancel()
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		if stack.DockerRunning(ctx) {
			return true
		}
		select {
		case <-ctx.Done():
			return false
		case <-ticker.C:
		}
	}
}
