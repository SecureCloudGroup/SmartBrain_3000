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
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"time"

	"fyne.io/systray"

	"github.com/SecureCloudGroup/SmartBrain_3000/launcher/native"
	"github.com/SecureCloudGroup/SmartBrain_3000/launcher/stack"
	"github.com/SecureCloudGroup/SmartBrain_3000/launcher/update"
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
	// The single native supervisor (see startWatch): its cancel func, nil when none runs.
	watchMu     sync.Mutex
	watchCancel context.CancelFunc
)

// startWatch (re)arms the native supervisor. Exactly ONE Watch goroutine may live at
// a time: startNative used to spawn one per call, and a deliberate Stop left the old
// one running — which then dutifully restarted the stack the user had just stopped.
// Residual race, accepted: a watcher cancelled mid-restart finishes that iteration;
// Down/Up are idempotent and converge.
func startWatch(nv native.Native) {
	watchMu.Lock()
	defer watchMu.Unlock()
	if watchCancel != nil {
		watchCancel()
	}
	ctx, cancel := context.WithCancel(context.Background())
	watchCancel = cancel
	go nv.Watch(ctx, setStatus)
}

// stopWatch retires the supervisor before a deliberate stop or supervised restart.
func stopWatch() {
	watchMu.Lock()
	defer watchMu.Unlock()
	if watchCancel != nil {
		watchCancel()
		watchCancel = nil
	}
}

func main() {
	// A Finder-launched .app on macOS gets launchd's minimal PATH, which hides /usr/local/bin and
	// Homebrew — so `docker` would look "not installed". Fix PATH before any Docker check runs.
	stack.EnsureDockerPath()
	stack.LauncherVersion = launcherVersion // rides health probes: the modern-launcher handshake
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
// Baked at release time via -ldflags (see launcher.yml); "dev" never self-updates.
var launcherVersion = "dev"

// How long to wait before looking for updates again. A check that could not run (an
// install/start was holding the operation lock) must not push the next look SIX HOURS
// away — that is how "I restarted twice and nothing happened" happens: the first launch
// is busy assembling, the check skips, and the user is left waiting out the full period.
const (
	updateFirstDelay = 20 * time.Second
	updateInterval   = 6 * time.Hour
	updateRetryDelay = 90 * time.Second
)

func updateChecker() {
	delay := updateFirstDelay // let startup settle, but only briefly
	for {
		time.Sleep(delay)
		if checkForUpdate() {
			delay = updateInterval
		} else {
			delay = updateRetryDelay // we never actually got to look — try again soon
		}
	}
}

// checkForUpdate reports whether a check actually happened; false means something was
// in flight and the caller should retry soon rather than wait out the full interval.
func checkForUpdate() bool {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	// The launcher updates ITSELF first: a newer release means new compose/native
	// capabilities that only a new binary can deliver. Verified download, atomic
	// swap (previous kept as backup), then hand over to the replacement — the
	// running stack (Docker or native) is untouched and outlives the handover.
	upd := update.New(launcherVersion)
	if ver, ok := upd.Available(ctx); ok {
		setStatus("Updating SmartBrain…")
		if _, err := upd.Apply(ctx, ver); err != nil {
			log.Println("self-update:", err) // fail-closed: keep running, retry in 6h
			setStatus("Running ●")
		} else {
			stack.Notify("SmartBrain updated", "Restarting with version "+ver+"…")
			systray.Quit() // the replacement is already running, detached
			return true
		}
	}
	if nativeMode() {
		return checkNativeUpdate(ctx, upd) // everything below is Docker's update path
	}
	_ = sb.Pull(ctx) // best-effort background pre-fetch; offline is fine
	ready, ver, err := sb.UpdateReady(ctx)
	if err != nil || !ready {
		return true // offline / daemon down / already latest — a real look, nothing to do
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
	return true
}

// checkNativeUpdate is the native stack's equivalent of the image pre-fetch: when a
// newer APP release exists, assemble it into its own versioned directory (verified
// downloads; the running version is untouched) and flip the `current` pointer — then
// offer the same two menu choices the Docker path shows. Because Up() always boots
// `current`, "Install on next start" needs no further mechanism, and "Install update
// now" is just a supervised restart. Failures leave the running version current and
// retry on the next 6-hour tick.
func checkNativeUpdate(ctx context.Context, upd update.Updater) bool {
	nv := native.New(sb.Dir)
	current := nv.Current()
	if current == "" {
		return false // still assembling the first version — look again shortly, not in 6h
	}
	latest, ok := upd.Latest(ctx)
	if !ok || !update.Newer(latest, current) {
		return true // offline / API trouble / already newest — a real look, nothing to do
	}
	if !mu.TryLock() {
		return false // a start/stop/install is in flight — retry soon, do not wait out the interval
	}
	defer mu.Unlock()
	setStatus("Downloading update v" + latest + "…")
	// Assembly needs its own budget (checkForUpdate's ctx is minutes; downloads are
	// ~400 MB on a slow line) — bounded like the first assembly in start().
	asmCtx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	if err := nv.Assemble(asmCtx, latest); err != nil {
		log.Println("native update:", err)
		setStatus("Running ● (native)") // current version untouched
		return false                    // a half-finished download deserves a prompt retry
	}
	label := "Update available (v" + latest + ")"
	setStatus(label)
	systray.SetTooltip("SmartBrain — " + label)
	mUpdateNow.Show()
	mUpdateLater.Show()
	if latest != lastNotifiedVersion {
		lastNotifiedVersion = latest
		stack.Notify("SmartBrain update ready", "Install from the menu now — or it installs next time you start.")
	}
	return true
}

// installUpdate applies a waiting update immediately: Up() pulls (a no-op — already staged) then
// recreates the container with the new image. It shares the operation lock with start/stop so it can
// never race them; a dropped click is fine (the status line says what's happening).
// Natively the staged version is already `current`, so this is a supervised Down+Up.
func installUpdate() {
	if !mu.TryLock() {
		return
	}
	defer mu.Unlock()
	setStatus("Installing update…")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	if nativeMode() {
		nv := native.New(sb.Dir)
		stopWatch() // deliberate restart — the supervisor must not fight it
		nv.Down()
		if err := nv.Up(ctx); err != nil { // boots `current` — the staged version
			setStatus("Update restart failed — see the native logs")
			log.Println("native update install:", err)
			return
		}
		startWatch(nv)
		mUpdateNow.Hide()
		mUpdateLater.Hide()
		setStatus("Running ● (native, updated)")
		return
	}
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

	// Docker-exit Phase 1, behind an explicit opt-in: SMARTBRAIN_NATIVE=1 (plus
	// SMARTBRAIN_NATIVE_VERSION to pick the release) runs the assembled native stack
	// instead of Docker. Deliberately env-only and menu-invisible while it matures.
	if nativeMode() {
		startNative(ctx)
		return
	}

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

// nativeMode reports whether this machine runs the native (Docker-free) stack.
// The choice PERSISTS: a successful native start writes a marker in the app-data
// dir, so reboots, Finder relaunches, and self-update handovers — none of which
// carry the opt-in env — keep booting native. (Observed live: a plain relaunch
// fell back to Docker, whose compose up then failed against the surviving native
// stack's ports and blamed the internet.) Env still expresses the explicit acts:
// "1" opts in, "0" forces Docker for this run; deleting the marker rolls back
// for good.
func nativeMode() bool {
	return resolveNativeMode(os.Getenv("SMARTBRAIN_NATIVE"), nativeMarkerExists())
}

func resolveNativeMode(env string, marker bool) bool {
	switch env {
	case "1":
		return true
	case "0":
		return false
	}
	return marker
}

func nativeMarkerPath() string { return filepath.Join(sb.Dir, "native-mode") }

func nativeMarkerExists() bool {
	_, err := os.Stat(nativeMarkerPath())
	return err == nil
}

// persistNativeMode records the mode choice; failure only means a plain relaunch
// would fall back to Docker once more, so log-and-continue is enough.
func persistNativeMode() {
	if err := os.WriteFile(nativeMarkerPath(), []byte("1\n"), 0o600); err != nil {
		log.Println("native marker:", err)
	}
}

// nativeBootVersion picks what to boot: the assembled `current` normally; the env pin
// only when it bootstraps a first assembly or names a STRICTLY NEWER release (a
// deliberate manual upgrade). The pin must not win otherwise: relaunches inherit the
// environment (the self-update handover preserves it), so a stale pin would silently
// downgrade past whatever auto-update has assembled since. Forcing an older version
// is a dev act: delete <dir>/current and pin.
func nativeBootVersion(current, pinned string) string {
	if current == "" || (pinned != "" && update.Newer(pinned, current)) {
		return pinned
	}
	return current
}

func startNative(ctx context.Context) {
	nv := native.New(sb.Dir)
	if nv.Healthy(ctx) {
		// The stack outlives the launcher by design (detached processes). A fresh
		// launcher ADOPTS a healthy running stack instead of spawning a second one
		// into the same ports — a collision that would "pass" health checks against
		// the survivor while poisoning the pid files with its own dead spawns.
		persistNativeMode()
		setStatus("Running ● (native)")
		startWatch(nv)
		return
	}
	version := nativeBootVersion(nv.Current(), os.Getenv("SMARTBRAIN_NATIVE_VERSION"))
	if version == "" {
		setStatus("Native mode needs SMARTBRAIN_NATIVE_VERSION for its first run")
		return
	}
	migrated := false
	if nv.NeedsMigration(ctx) {
		// A COPY, not a move: the Docker volumes stay untouched as the rollback, and
		// they are mounted read-only during the copy so nothing can modify them.
		if !stack.DockerRunning(ctx) {
			setStatus("Start Docker once more so your data can be copied out, then Restart")
			return
		}
		setStatus("Copying your data out of Docker…")
		downCtx, cancelDown := context.WithTimeout(ctx, 3*time.Minute)
		if err := sb.Down(downCtx); err != nil { // never copy under a live writer
			cancelDown()
			setStatus("Couldn't stop the Docker stack — see the log")
			log.Println("native migrate: down:", err)
			return
		}
		cancelDown()
		if err := nv.MigrateFromDocker(ctx); err != nil {
			setStatus("Data copy failed — Docker data is untouched; see the log")
			log.Println("native migrate:", err)
			nv.DiscardMigratedData() // a partial copy must not shadow a fresh one later
			return
		}
		migrated = true
	}
	setStatus("Assembling native install…")
	// Bounded like the Docker pull path: downloads are ~400 MB on a first assembly.
	asmCtx, cancel := context.WithTimeout(ctx, 15*time.Minute)
	defer cancel()
	if err := nv.Assemble(asmCtx, version); err != nil {
		setStatus("Native assembly failed — see the log")
		log.Println("native assemble:", err)
		if migrated { // tonight's copy must not resurrect as a stale snapshot on retry
			nv.DiscardMigratedData()
		}
		return
	}
	setStatus("Starting (native)…")
	if err := nv.Up(ctx); err != nil {
		setStatus("Native start failed — see the log")
		log.Println("native up:", err)
		if migrated {
			nv.DiscardMigratedData()
		}
		return
	}
	persistNativeMode() // the stack runs natively — plain relaunches must too
	setStatus("Running ● (native)")
	if err := stack.OpenBrowser(sb.URL()); err != nil {
		log.Println("open browser:", err)
	}
	// Supervision parity: the Docker path has restart: unless-stopped; natively the
	// launcher watches and restarts (bounded — a crash loop reports, never spins).
	startWatch(nv)
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
	if nativeMode() {
		stopWatch() // a deliberate stop — the supervisor must not resurrect the stack
		native.New(sb.Dir).Down()
		setStatus("Stopped")
		return
	}
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
