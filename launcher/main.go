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

const dockerGetURL = "https://docs.docker.com/get-docker/"

var (
	sb           stack.Stack
	mu           sync.Mutex // serialize compose ops so two quick menu clicks can't race
	mStatus      *systray.MenuItem
	mGetDocker   *systray.MenuItem
	mUpdateNow   *systray.MenuItem // hidden until a newer image is staged
	mUpdateLater *systray.MenuItem
	// Auto-open the Docker download page ONCE when Docker is missing — a helping hand, not a popup
	// storm on every Restart while the user is mid-install.
	openedDockerPage bool
	// One "downloading…" desktop notification per launch, not one per Restart click.
	notifiedStart bool
	// One gentle "update available" notification per NEW version, never re-nagging the same one. The
	// sentinel is a value no real version equals, so the first surfacing (even of a blank version)
	// still notifies once.
	lastNotifiedVersion = "\x00"
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
	mGetDocker = systray.AddMenuItem("Get Docker…", "Open the Docker download page")
	mGetDocker.Hide() // only shown when Docker is actually missing
	systray.AddSeparator()
	mStop := systray.AddMenuItem("Stop", "Stop SmartBrain (your data is kept)")
	mRestart := systray.AddMenuItem("Restart", "Restart SmartBrain")
	mUpdateNow = systray.AddMenuItem("Install update now", "Download and apply the latest update now")
	mUpdateNow.Hide()
	mUpdateLater = systray.AddMenuItem("Install on next start", "Apply the update the next time you start SmartBrain")
	mUpdateLater.Hide()
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

	go start()         // bring it up on launch
	go updateChecker() // then quietly watch for a newer image

	go func() {
		for {
			select {
			case <-mOpen.ClickedCh:
				go openOrStart()
			case <-mUpdateNow.ClickedCh:
				go installUpdate()
			case <-mUpdateLater.ClickedCh:
				// The new image is already pulled; Up() applies it on the next start (see #87). So
				// "install on next start" is just: dismiss the prompt and let startup do it.
				mUpdateNow.Hide()
				mUpdateLater.Hide()
				setStatus("Update ready — installs next time you start")
			case <-mGetDocker.ClickedCh:
				go func() {
					if err := stack.OpenBrowser(dockerGetURL); err != nil {
						log.Println("open docker page:", err)
					}
				}()
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

// updateChecker quietly watches for a newer app image: it pulls in the background (download only, so
// a live session is never disturbed) and, if the pulled :latest differs from what's running, surfaces
// "Install update now" / "Install on next start" in the menu. The user chooses when to apply — a
// click, never a command. A dead/offline daemon just means "no update known"; the check stays silent.
func updateChecker() {
	time.Sleep(45 * time.Second) // let first-run startup settle before the first check
	for {
		checkForUpdate()
		time.Sleep(6 * time.Hour)
	}
}

func checkForUpdate() {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	_ = sb.Pull(ctx) // best-effort background pre-fetch; offline is fine
	ready, ver, err := sb.UpdateReady(ctx)
	if err != nil || !ready {
		return // offline / daemon down / already latest — surface nothing
	}
	label := "Update available"
	if ver != "" {
		label += " (v" + ver + ")"
	}
	setStatus(label)
	systray.SetTooltip("SmartBrain — " + label)
	mUpdateNow.Show()
	mUpdateLater.Show()
	if ver != lastNotifiedVersion { // one gentle heads-up per new version; never re-nag the same one
		lastNotifiedVersion = ver
		stack.Notify("SmartBrain update available", "Open the menu to install now — or it installs next time you start.")
	}
}

// installUpdate applies a waiting update immediately: Up() pulls (a no-op — already staged) then
// recreates the container with the new image. It shares the operation lock with start/stop so it can
// never race them; a dropped click is fine (the status line says what's happening).
func installUpdate() {
	if !mu.TryLock() {
		return
	}
	defer mu.Unlock()
	setStatus("Installing update…")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	if err := sb.Up(ctx); err != nil {
		setStatus("Update failed — it'll install next time you start")
		log.Println("update:", err)
		return
	}
	mUpdateNow.Hide()
	mUpdateLater.Hide()
	if sb.WaitHealthy(ctx, 6*time.Minute) {
		setStatus("Running ● (updated)")
	} else {
		setStatus("Updated — click Open in a moment")
	}
}

// start ensures Docker is up, starts the stack, waits for health, and opens the browser. TryLock:
// while one operation is in flight, further clicks are DROPPED, not queued — five impatient Restart
// clicks during a first pull must not replay five ups and open five browser tabs. The status line
// already says what's happening.
func start() {
	if !mu.TryLock() {
		return
	}
	defer mu.Unlock()
	ctx := context.Background()

	if !stack.DockerRunning(ctx) {
		if !stack.DockerInstalled() {
			// Don't dead-end a newcomer on a grey status line: take them to the fix. Open the
			// download page once, and leave a "Get Docker…" menu item for later.
			mGetDocker.Show()
			if !openedDockerPage {
				openedDockerPage = true
				if err := stack.OpenBrowser(dockerGetURL); err != nil {
					log.Println("open docker page:", err)
				}
			}
			setStatus("Docker is required — install it, start it, then click Restart")
			return
		}
		mGetDocker.Hide()
		setStatus("Starting Docker…")
		stack.TryStartDocker(ctx)
		if !waitDocker(ctx, 90*time.Second) {
			setStatus("Docker isn't running — start Docker, then Restart")
			return
		}
	}
	mGetDocker.Hide()

	// `docker` on PATH does not imply the compose PLUGIN exists (e.g. `brew install docker` without
	// it). Catch that here with an honest message instead of blaming the network later.
	if !stack.ComposeAvailable(ctx) {
		setStatus("Docker Compose is missing — update Docker Desktop, or install the compose plugin")
		return
	}

	setStatus("Starting… (first run downloads the app)")
	if !notifiedStart {
		notifiedStart = true
		// One heads-up per launch: the download can take minutes and this app has no window.
		stack.Notify("SmartBrain is starting", "Downloading the app — your browser will open when it's ready.")
	}
	// Bounded: a wedged pull must not hold the operation lock forever. 15 min covers a slow first
	// download; after that the user gets an honest failure instead of a frozen "Starting…".
	upCtx, cancel := context.WithTimeout(ctx, 15*time.Minute)
	defer cancel()
	if err := sb.Up(upCtx); err != nil {
		setStatus("Couldn't start — check your internet connection and Docker's disk space")
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
	// Deadline: a wedged localhost read (docker-proxy after a sleep/wake) must not hang the click.
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if sb.Healthy(ctx) {
		if err := stack.OpenBrowser(sb.URL()); err != nil {
			log.Println("open browser:", err)
		}
		return
	}
	start()
}

func stop() {
	if !mu.TryLock() {
		return // an operation is in flight — see start()
	}
	defer mu.Unlock()
	setStatus("Stopping…")
	// Bounded like Up: never hold the lock forever on a wedged daemon.
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	if err := sb.Down(ctx); err != nil {
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
