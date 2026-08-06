// Package native is the launcher's Docker-free stack, and the DEFAULT wherever it can
// be assembled — see resolveNativeMode in main.go. It stopped being opt-in some time
// ago; SMARTBRAIN_NATIVE=1/0 now only forces the answer either way.
//
// Where the Docker path pulls one image, the native path ASSEMBLES an install from three
// verified parts: a pinned python-build-standalone runtime, the release's wheelhouse
// (the same requirements.lock the image installs), and the mirrored bifrost-http binary.
// Every external download is sha256-verified — the runtime and gateway binary against
// sums PINNED IN THIS SOURCE (upstream CDNs have changed files behind pinned URLs), the
// wheelhouse against its release-side checksum (our repo, our trust root, like the image
// registry today). Assembly lands in a versioned directory and a `current` pointer flips
// only after everything succeeded — a failed assembly leaves the previous version
// untouched, which is the rollback the Docker path never had.
//
// Like package stack, this has NO dependency on the systray/GUI layer and every side
// effect (fetching, process exec) is injectable, so it unit-tests hermetically on any
// platform — the discipline installer/test_install.py set for the Docker path.
package native

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	// The app is loopback-only on this fixed port — same contract as the Docker path.
	AppPort = 33000
	// Native Bifrost listens where the compose file has always mapped its admin port, so
	// the app's native default gateway URL (Phase 0) points here without configuration.
	BifrostPort = 38080

	releaseBase    = "https://github.com/SecureCloudGroup/SmartBrain_3000/releases/download"
	bifrostRelease = "bifrost-native-v1.6.4"
	pbsBase        = "https://github.com/astral-sh/python-build-standalone/releases/download"
	pbsRelease     = "20260718"
	pbsPython      = "3.12.13"
)

// Pinned sha256s. Changing a pin is a deliberate, reviewed act — never automatic.
var bifrostSums = map[string]string{ // asset name -> sum (from our immutable mirror's SHA256SUMS)
	"bifrost-http-darwin-amd64":      "323d243a7a8d6e04a9c56912a3d1d35491903a67bfe04fbc9a0ef26af1bb3681",
	"bifrost-http-darwin-arm64":      "039a491b995d5835eaf4d30ef2da13bb9059ba77f2e462a3bb509bdd13005051",
	"bifrost-http-linux-amd64":       "2561420820239d54e346a7dcccc77436a5f8305fe3202a2ef20c9ccd58e2dd76",
	"bifrost-http-windows-amd64.exe": "a082beeb471014f300788a54a234322b4c8b24b374e6d21eeb343df378aaa4a6",
}

var pbsSums = map[string]string{ // pbs triple -> sum (hashed at pin time from the tagged assets)
	"x86_64-unknown-linux-gnu": "5854aa6ec71cad00334d5065633c210b2e7feb40956767a59a91791cadcf0b79",
	"aarch64-apple-darwin":     "9a1e9e06175c10efd8378b904b07fa21bd791ab3345d7cdffeb4a76c9ff55903",
	"x86_64-pc-windows-msvc":   "0d422a1439ec308e03f47df551bc30f5994727c456e414b026d202bcda9b7c1c",
}

// platform describes everything OS/arch-specific about an assembly.
type platform struct {
	pbsTriple    string // python-build-standalone asset triple
	wheelLabel   string // wheelhouse artifact label (release.yml matrix)
	bifrostAsset string // mirrored gateway binary asset name
	pythonRel    string // python executable, relative to the version dir
}

// currentPlatform maps GOOS/GOARCH to the supported assembly, or an honest error for
// combinations the release pipeline doesn't build yet (Intel macs, arm Linux).
func currentPlatform() (platform, error) {
	switch runtime.GOOS + "/" + runtime.GOARCH {
	case "darwin/arm64":
		return platform{"aarch64-apple-darwin", "macos-arm64", "bifrost-http-darwin-arm64",
			filepath.Join("python", "bin", "python3")}, nil
	case "linux/amd64":
		return platform{"x86_64-unknown-linux-gnu", "linux-x64", "bifrost-http-linux-amd64",
			filepath.Join("python", "bin", "python3")}, nil
	case "windows/amd64":
		return platform{"x86_64-pc-windows-msvc", "windows-x64", "bifrost-http-windows-amd64.exe",
			filepath.Join("python", "python.exe")}, nil
	}
	return platform{}, fmt.Errorf("native mode is not built for %s/%s yet (Docker mode works everywhere)",
		runtime.GOOS, runtime.GOARCH)
}

// Supported reports whether this OS/arch can run the Docker-free stack — i.e. whether a
// Python runtime and gateway binary are pinned for it. Intel Macs and arm Linux have
// neither, and the macOS cask ships a UNIVERSAL binary, so an Intel user installs exactly
// this app: they must keep Docker rather than be handed a launcher that cannot start
// anything.
func Supported() bool {
	_, err := currentPlatform()
	return err == nil
}

// Native points at one Docker-free SmartBrain install rooted under the launcher's dir.
type Native struct {
	Dir  string // <launcher dir>/native — versions/, current, run/, bifrost-data/ live here
	Port int    // app port (loopback)

	// Injectable side effects (tests replace these; production uses the defaults).
	// Fetch's progress callback is optional — nil means download silently.
	Fetch func(ctx context.Context, url, dest string, progress func(done, total int64)) error // download url -> dest file
	Run   func(ctx context.Context, name string, args ...string) error                        // run a command to completion
	Tick  time.Duration                                                                       // watchdog interval (tests shrink it)
	PS    func(pid int) string                                                                // a pid's command line ("" if unknown)

	// Progress, when set, hears how far each artifact's download is: the artifact's
	// user-facing name ("python", "app", "gateway") and the whole percent fetched.
	// Called from the downloading goroutine, at most once per percent step, and only
	// when the server said how big the file is. Nil = the old silent behavior.
	Progress func(artifact string, percent int)
}

// New returns a Native rooted beside the launcher's existing state dir.
func New(launcherDir string) Native {
	n := Native{Dir: filepath.Join(launcherDir, "native"), Port: AppPort, Tick: 30 * time.Second}
	n.Fetch = fetchURL
	n.Run = runCmd
	n.PS = psCommand
	return n
}

func (n Native) versionsDir() string        { return filepath.Join(n.Dir, "versions") }
func (n Native) versionDir(v string) string { return filepath.Join(n.versionsDir(), v) }
func (n Native) currentPath() string        { return filepath.Join(n.Dir, "current") }
func (n Native) runDir() string             { return filepath.Join(n.Dir, "run") }
func (n Native) bifrostData() string        { return filepath.Join(n.Dir, "bifrost-data") }

// RunDir is the folder holding the children's logs (app.log, bifrost.log) and pid
// files — exported so the menu's "Open logs" can show it to the user.
func (n Native) RunDir() string { return n.runDir() }

// Current returns the assembled version the pointer names, or "" when none is complete.
func (n Native) Current() string {
	raw, err := os.ReadFile(n.currentPath())
	if err != nil {
		return ""
	}
	v := strings.TrimSpace(string(raw))
	if v == "" {
		return ""
	}
	if _, err := os.Stat(filepath.Join(n.versionDir(v), ".complete")); err != nil {
		return "" // pointer to a missing/incomplete assembly reads as no assembly
	}
	return v
}

// Assemble downloads, verifies, and installs one app version. Idempotent: an already
// complete version returns immediately. On ANY failure the partial directory is removed
// and the `current` pointer is untouched — the previous version keeps working.
func (n Native) Assemble(ctx context.Context, version string) error {
	if version == "" {
		return fmt.Errorf("assemble: version required")
	}
	plat, err := currentPlatform()
	if err != nil {
		return err
	}
	final := n.versionDir(version)
	if _, err := os.Stat(filepath.Join(final, ".complete")); err == nil {
		return n.writeCurrent(version) // assembled earlier — just make sure the pointer agrees
	}
	tmp := filepath.Join(n.versionsDir(), ".tmp-"+version)
	_ = os.RemoveAll(tmp)
	if err := os.MkdirAll(tmp, 0o700); err != nil {
		return fmt.Errorf("assemble: create work dir: %w", err)
	}
	cleanup := true
	defer func() {
		if cleanup {
			_ = os.RemoveAll(tmp)
		}
	}()

	// 1) The Python runtime — pinned upstream asset, pinned sum.
	pbsName := fmt.Sprintf("cpython-%s+%s-%s-install_only_stripped.tar.gz", pbsPython, pbsRelease, plat.pbsTriple)
	pbsTar := filepath.Join(tmp, "pbs.tar.gz")
	if err := n.fetchVerified(ctx, pbsBase+"/"+pbsRelease+"/"+pbsName, pbsTar, pbsSums[plat.pbsTriple], "python"); err != nil {
		return fmt.Errorf("runtime: %w", err)
	}
	if err := untarGz(pbsTar, tmp); err != nil { // yields tmp/python/
		return fmt.Errorf("runtime: unpack: %w", err)
	}
	_ = os.Remove(pbsTar)

	// 2) The wheelhouse — this release's own artifact + its release-side checksum.
	whName := fmt.Sprintf("smartbrain-wheelhouse-%s-%s", version, plat.wheelLabel)
	whZip := filepath.Join(tmp, whName+".zip")
	sumFile := filepath.Join(tmp, whName+".zip.sha256")
	base := fmt.Sprintf("%s/v%s/", releaseBase, version)
	if err := n.Fetch(ctx, base+whName+".zip.sha256", sumFile, nil); err != nil { // 64 bytes — nothing to report
		return fmt.Errorf("wheelhouse checksum: %w", err)
	}
	wantRaw, err := os.ReadFile(sumFile)
	if err != nil {
		return fmt.Errorf("wheelhouse checksum: %w", err)
	}
	want := strings.Fields(strings.TrimSpace(string(wantRaw)))
	if len(want) == 0 || len(want[0]) != 64 {
		return fmt.Errorf("wheelhouse checksum: malformed sidecar")
	}
	if err := n.fetchVerified(ctx, base+whName+".zip", whZip, want[0], "app"); err != nil {
		return fmt.Errorf("wheelhouse: %w", err)
	}
	if err := unzip(whZip, tmp); err != nil { // yields tmp/<whName>/
		return fmt.Errorf("wheelhouse: unpack: %w", err)
	}
	_ = os.Remove(whZip)

	// 3) The gateway — OUR mirrored binary, pinned sum (never the upstream CDN).
	bifrostDest := filepath.Join(tmp, "bifrost-http")
	if strings.HasSuffix(plat.bifrostAsset, ".exe") {
		bifrostDest += ".exe"
	}
	bifrostURL := fmt.Sprintf("%s/%s/%s", releaseBase, bifrostRelease, plat.bifrostAsset)
	if err := n.fetchVerified(ctx, bifrostURL, bifrostDest, bifrostSums[plat.bifrostAsset], "gateway"); err != nil {
		return fmt.Errorf("gateway: %w", err)
	}
	if err := os.Chmod(bifrostDest, 0o755); err != nil {
		return fmt.Errorf("gateway: chmod: %w", err)
	}

	// 4) Offline install: the runtime installs the app from the wheelhouse — no index,
	// no network, exactly the wheels the release built. Then stamp the version where
	// runtime.version_from_file reads it (the app has no baked env natively).
	py := filepath.Join(tmp, plat.pythonRel)
	if err := n.Run(ctx, py, "-m", "pip", "install", "--quiet", "--no-index",
		"--find-links", filepath.Join(tmp, whName), "smartbrain_3000"); err != nil {
		return fmt.Errorf("install: %w", err)
	}
	stamp := "import smartbrain_3000, os; open(os.path.join(os.path.dirname(smartbrain_3000.__file__), 'VERSION'), 'w').write('" + version + "')"
	if err := n.Run(ctx, py, "-c", stamp); err != nil {
		return fmt.Errorf("install: version stamp: %w", err)
	}

	// 5) Commit: marker, atomic-ish dir rename, THEN the pointer flip.
	if err := os.WriteFile(filepath.Join(tmp, ".complete"), []byte(version+"\n"), 0o600); err != nil {
		return fmt.Errorf("assemble: marker: %w", err)
	}
	_ = os.RemoveAll(final)
	if err := os.Rename(tmp, final); err != nil {
		return fmt.Errorf("assemble: commit: %w", err)
	}
	cleanup = false
	return n.writeCurrent(version)
}

// writeCurrent flips the pointer via write-then-rename so a crash can't leave it torn.
func (n Native) writeCurrent(version string) error {
	if err := os.MkdirAll(n.Dir, 0o700); err != nil {
		return err
	}
	tmp := n.currentPath() + ".tmp"
	if err := os.WriteFile(tmp, []byte(version+"\n"), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, n.currentPath())
}

// fetchVerified downloads to dest and enforces the expected sha256, deleting on
// mismatch. artifact is the user-facing name progress is reported under.
func (n Native) fetchVerified(ctx context.Context, url, dest, wantSum, artifact string) error {
	if wantSum == "" {
		return fmt.Errorf("no pinned checksum for %s — refusing to download", filepath.Base(dest))
	}
	if err := n.Fetch(ctx, url, dest, n.progressFor(artifact)); err != nil {
		return err
	}
	got, err := sha256File(dest)
	if err != nil {
		return err
	}
	if got != wantSum {
		_ = os.Remove(dest)
		return fmt.Errorf("checksum mismatch for %s: got %s want %s", filepath.Base(dest), got, wantSum)
	}
	return nil
}

// progressFor adapts the per-fetch byte counts to the Progress sink, naming the
// artifact and rounding to a whole percent. Nil when no sink is set, so the fetch
// stays exactly as silent as before.
func (n Native) progressFor(artifact string) func(done, total int64) {
	if n.Progress == nil {
		return nil
	}
	return func(done, total int64) { n.Progress(artifact, percent(done, total)) }
}

// Up starts bifrost-http then the app from the current assembly, recording pids so a
// relaunched launcher can still stop them. Bounded health waits; any failure stops
// whatever was started (no half-up stack).
func (n Native) Up(ctx context.Context) error {
	version := n.Current()
	if version == "" {
		return fmt.Errorf("native up: nothing assembled yet")
	}
	// PREFLIGHT — never create a second instance. Spawning alongside a survivor
	// "succeeds" against the survivor's health answers while the new spawns die on the
	// held ports and the database lock, and spawn() then overwrites the pid records
	// with pids that are about to die: every later Down stops nothing (the 2026-07-29
	// poisoning, twice). Callers that WANT a running stack adopt it before calling Up.
	//
	// Two independent questions, because either alone has a blind spot: the port can
	// answer while the records are stale (the live incident), and a survivor can be
	// alive-but-not-answering (wedged, or still warming) while the port is silent.
	if n.serving(ctx) {
		return fmt.Errorf("native up: an instance is already serving on port %d — refusing to start a second", n.Port)
	}
	if name, pid, alive := n.liveRecordedChild(); alive {
		return fmt.Errorf("native up: the previous %s (pid %d) is still running — stop it first", name, pid)
	}
	plat, err := currentPlatform()
	if err != nil {
		return err
	}
	vdir := n.versionDir(version)
	if err := os.MkdirAll(n.runDir(), 0o700); err != nil {
		return err
	}
	if err := os.MkdirAll(n.bifrostData(), 0o700); err != nil {
		return err
	}
	if err := prepareBifrostData(n.bifrostData()); err != nil {
		return fmt.Errorf("native up: gateway data: %w", err)
	}
	bifrost := filepath.Join(vdir, "bifrost-http")
	if runtime.GOOS == "windows" {
		bifrost += ".exe"
	}
	gateway, err := n.spawn(ctx, "bifrost", bifrost,
		"-app-dir", n.bifrostData(), "-host", "127.0.0.1", "-port", strconv.Itoa(BifrostPort))
	if err != nil {
		return fmt.Errorf("native up: gateway: %w", err)
	}
	if err := awaitChild(ctx, gateway,
		fmt.Sprintf("http://127.0.0.1:%d/api/health", BifrostPort), 60*time.Second, 0); err != nil {
		n.Down()
		return fmt.Errorf("native up: gateway: %w", err)
	}
	// The app needs no forced env: Phase 0's native defaults point it at loopback Bifrost
	// and the per-OS data dir on their own — running the defaults IS the test of them.
	py := filepath.Join(vdir, plat.pythonRel)
	app, err := n.spawn(ctx, "app", py, "-m", "smartbrain_3000.serve")
	if err != nil {
		n.Down()
		return fmt.Errorf("native up: app: %w", err)
	}
	if err := awaitChild(ctx, app,
		fmt.Sprintf("http://127.0.0.1:%d/api/health", n.Port), 120*time.Second, appStartupGrace); err != nil {
		n.Down()
		return fmt.Errorf("native up: app: %w", err)
	}
	return nil
}

// awaitChild waits for a spawned child to serve its port — and fails the instant the
// child dies instead. "The port answers" alone is not proof the CHILD answers: a
// survivor can answer while our spawn dies on the held port or the database lock. The
// child's own exit is watched (its reaper closes exited), so a death is caught whenever
// it happens — no settle window to tune, no latency added to a healthy start. The first
// attempt at this used a fixed 1.5s watch AFTER health passed, which an adversarial
// review correctly refuted: the app's database-lock death takes several seconds of
// interpreter startup, so it outlived the window.
//
// Residual, honestly: a survivor that holds no live pid record AND fails the preflight
// probes, then starts answering mid-Up, still looks like a healthy start. The preflight
// is what narrows that to near-nothing; this check is what catches the rest.
func awaitChild(ctx context.Context, c *child, healthURL string, budget, grace time.Duration) error {
	start := time.Now()
	deadline := start.Add(budget)
	for time.Now().Before(deadline) { // bounded by the budget
		select {
		case <-c.exited:
			return fmt.Errorf("the process we started exited — another instance is running, or see the log")
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(250 * time.Millisecond):
		}
		if !probe(ctx, healthURL) {
			continue
		}
		// The port answers — but by whom? A survivor answers from the first instant while
		// our spawn is still starting up and about to die on the held resource. The
		// disambiguator is time since SPAWN, not time since the answer: a doomed app dies
		// DURING startup (it cannot answer health before opening the database, so losing
		// the database lock always precedes its first answer), so an answer we see before
		// our child has outlived startup may well be someone else's.
		if time.Since(start) < grace {
			continue
		}
		select {
		case <-c.exited:
			return fmt.Errorf("the port answers but the process we started is gone — another instance is running")
		default:
			return nil
		}
	}
	return fmt.Errorf("never became healthy within %s", budget)
}

// How long the app must outlive its own spawn before a health answer is credited to it
// (see awaitChild). Costs nothing in practice — a real app start takes longer than this
// to answer anyway. The gateway needs no grace: its failure mode is an immediate bind
// error, caught by the exit channel on the first poll.
const appStartupGrace = 6 * time.Second

// serving reports whether an instance already answers the app port. Retried: one
// unretried probe can lose to a momentary stall (a database checkpoint outlasts the
// 3s probe), and a wrong "nothing is running" is precisely how the poisoning began.
func (n Native) serving(ctx context.Context) bool {
	for i := 0; i < 3; i++ { // fixed bound
		if n.Healthy(ctx) {
			return true
		}
		time.Sleep(400 * time.Millisecond)
	}
	return false
}

// liveRecordedChild reports a recorded child that is still running — the survivor a
// health probe cannot see because it is wedged or still warming. Identity is VERIFIED
// against the process's command line: a pid file outliving a reboot can name a
// recycled, unrelated pid, and refusing to start because of that would be a far worse
// bug than the one this prevents. An unverifiable pid therefore reads as "not ours"
// (fail-open to starting) — which on Windows, where PS returns nothing, means this
// check never fires and the health preflight carries the weight alone.
func (n Native) liveRecordedChild() (string, int, bool) {
	for name, marker := range map[string]string{"app": appProcessMarker, "bifrost": bifrostProcessMarker} {
		raw, err := os.ReadFile(filepath.Join(n.runDir(), name+".pid"))
		if err != nil {
			continue
		}
		pid, perr := strconv.Atoi(strings.TrimSpace(string(raw)))
		if perr != nil || !processAlive(pid) {
			continue
		}
		if ps := n.PS; ps != nil && strings.Contains(ps(pid), marker) {
			return name, pid, true
		}
	}
	return "", 0, false
}

// Command-line fragments that identify our own children (see liveRecordedChild).
const (
	appProcessMarker     = "smartbrain_3000.serve"
	bifrostProcessMarker = "bifrost-http"
)

// psCommand returns a pid's command line, or "" when it cannot be determined (any
// error, and always on Windows — `ps` is unix-only, so identity there is unknown).
func psCommand(pid int) string {
	if runtime.GOOS == "windows" || pid <= 1 {
		return ""
	}
	out, err := exec.Command("ps", "-p", strconv.Itoa(pid), "-o", "command=").Output()
	if err != nil {
		return ""
	}
	return string(out)
}

// Down stops both children (best-effort, idempotent): TERM, bounded wait for the
// process to actually VANISH, then KILL and wait again. A pid file is removed only
// for a process confirmed gone (or never alive — stale records from reboots and
// prior crashes are dropped on sight). A survivor keeps its pid file: the record
// must never claim less than the truth, Up's preflight refuses to double-start,
// and the next Down retries. Trusting the file without verifying was how two
// colliding starts poisoned the records and made every later stop a no-op.
func (n Native) Down() {
	for _, name := range []string{"app", "bifrost"} { // app first: it talks to the gateway
		pidFile := filepath.Join(n.runDir(), name+".pid")
		raw, err := os.ReadFile(pidFile)
		if err != nil {
			continue
		}
		pid, perr := strconv.Atoi(strings.TrimSpace(string(raw)))
		if perr != nil || pid <= 1 || !processAlive(pid) {
			_ = os.Remove(pidFile) // garbage or already gone — drop the stale record
			continue
		}
		terminate(pid) // TERM + bounded grace + KILL (unix); hard kill (windows)
		if !waitGone(pid, 3*time.Second) {
			continue // still alive: KEEP the pid file and let the caller's Up refuse
		}
		_ = os.Remove(pidFile)
	}
}

// processAlive reports whether pid names a live process. Unix: signal 0 probes without
// touching the target (EPERM still means alive — someone else's process, but alive).
// Windows: FindProcess opens a handle and fails for a pid with no process object.
//
// Release() is not optional: on Windows an open handle keeps a TERMINATED process's
// object alive, so leaking one per probe would pin the zombie and make it read as
// alive forever. Two honest limits remain — pid reuse can say "alive" about an
// unrelated process, and Windows liveness is best-effort — so callers must treat this
// as ADVISORY: liveRecordedChild verifies identity before acting on it, Down bounds
// its waits, and Up's own-spawn check uses the child's exit channel rather than this.
func processAlive(pid int) bool {
	if pid <= 1 {
		return false
	}
	proc, err := os.FindProcess(pid)
	if err != nil {
		return false // windows: no such process
	}
	defer func() { _ = proc.Release() }()
	if runtime.GOOS == "windows" {
		return true
	}
	sigErr := proc.Signal(syscall.Signal(0))
	return sigErr == nil || errors.Is(sigErr, syscall.EPERM)
}

// waitGone polls (bounded) until pid has actually exited.
func waitGone(pid int, budget time.Duration) bool {
	deadline := time.Now().Add(budget)
	for time.Now().Before(deadline) { // bounded by the budget
		if !processAlive(pid) {
			return true
		}
		time.Sleep(100 * time.Millisecond)
	}
	return !processAlive(pid)
}

// Healthy mirrors stack.Healthy: one cheap loopback GET.
func (n Native) Healthy(ctx context.Context) bool {
	return probe(ctx, fmt.Sprintf("http://127.0.0.1:%d/api/health", n.Port))
}

// child is a process we started: its pid, plus a channel the reaper closes the moment
// it exits. The channel is what makes "did OUR spawn survive?" answerable without
// polling a liveness primitive that pid reuse (and Windows handle semantics) can lie
// about — this launcher started it, so this launcher knows.
type child struct {
	pid    int
	exited chan struct{}
}

// spawn starts a child with logs under run/ and its pid recorded.
func (n Native) spawn(ctx context.Context, name, bin string, args ...string) (*child, error) {
	logFile, err := os.OpenFile(filepath.Join(n.runDir(), name+".log"),
		os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return nil, err
	}
	cmd := exec.Command(bin, args...) // deliberately NOT CommandContext: ctx cancel must not kill the stack
	cmd.SysProcAttr = detachAttr()    // survive launcher quit / terminal Ctrl-C (per-OS)
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	if err := cmd.Start(); err != nil {
		logFile.Close()
		return nil, err
	}
	c := &child{pid: cmd.Process.Pid, exited: make(chan struct{})}
	go func() { // reap, and publish the exit
		_ = cmd.Wait()
		logFile.Close()
		close(c.exited)
	}()
	return c, os.WriteFile(filepath.Join(n.runDir(), name+".pid"),
		[]byte(strconv.Itoa(c.pid)+"\n"), 0o600)
}

// prepareBifrostData is the native equivalent of the compose entrypoint from the
// plaintext-log privacy fix: destroy any historical request log and write the
// config.json that disables Bifrost's logging STORE at the source (for that section
// the file wins on every boot, so neither the admin UI nor the API can resurrect
// it). Rewritten on every Up, exactly like the compose wrapper runs on every start.
// Without this, a fresh native gateway boots with logging ON by default — observed
// live on the first native run.
func prepareBifrostData(dir string) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	for _, f := range []string{"logs.db", "logs.db-wal", "logs.db-shm"} {
		_ = os.Remove(filepath.Join(dir, f))
	}
	kill := `{"logs_store":{"enabled":false},"client":{"enable_logging":false,"disable_content_logging":true}}` + "\n"
	return os.WriteFile(filepath.Join(dir, "config.json"), []byte(kill), 0o600)
}

// Watch is the supervisor loop: while the context lives, health-check both children
// and restart whichever died — gateway first (the app depends on it). Restarts are
// BOUNDED (a crash loop reports instead of spinning forever), and onStatus keeps the
// tray honest about what happened. Runs in its own goroutine; returns when ctx ends.
func (n Native) Watch(ctx context.Context, onStatus func(string)) {
	const maxRestarts = 3 // per window — a persistent crash needs a human
	const window = 10 * time.Minute
	restarts := 0
	windowStart := time.Now()
	for {
		select {
		case <-ctx.Done():
			return
		case <-time.After(n.Tick):
		}
		if n.Healthy(ctx) {
			continue
		}
		if time.Since(windowStart) > window {
			restarts, windowStart = 0, time.Now() // fresh window, fresh allowance
		}
		if restarts >= maxRestarts {
			onStatus("SmartBrain keeps crashing — stopped restarting; see the native logs")
			return
		}
		restarts++
		onStatus("SmartBrain stopped — restarting…")
		n.Down()
		if err := n.Up(ctx); err != nil {
			onStatus("Restart failed — see the native logs")
			continue // the next tick re-attempts within the bounded allowance
		}
		onStatus("Running ● (native)")
	}
}

// terminate asks a process to exit (TERM + short grace on unix; hard kill on
// windows, which has no TERM) and is silent about already-gone processes.
func terminate(pid int) {
	proc, err := os.FindProcess(pid)
	if err != nil {
		return
	}
	if runtime.GOOS != "windows" {
		_ = proc.Signal(syscall.SIGTERM)
		for i := 0; i < 10; i++ { // ~2s of grace, bounded
			time.Sleep(200 * time.Millisecond)
			if proc.Signal(syscall.Signal(0)) != nil {
				return // gone
			}
		}
	}
	_ = proc.Kill()
}

// --- small pure helpers (each independently testable) ------------------------

func sha256File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func fetchURL(ctx context.Context, url, dest string, progress func(done, total int64)) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("GET %s: %s", url, resp.Status)
	}
	f, err := os.Create(dest)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = io.Copy(f, newProgressReader(resp.Body, resp.ContentLength, progress))
	return err
}

// newProgressReader wraps a download stream so progress hears (done, total) as bytes
// flow — but only when the whole-number percent advances, so a ~400 MB body makes at
// most 101 reports instead of one per 32 KiB chunk (the tray must not be hammered).
// A nil callback or an unknown length (ContentLength <= 0) returns r untouched: the
// status line simply keeps its static text.
func newProgressReader(r io.Reader, total int64, progress func(done, total int64)) io.Reader {
	if progress == nil || total <= 0 {
		return r
	}
	return &progressReader{r: r, total: total, lastPct: -1, report: progress}
}

type progressReader struct {
	r       io.Reader
	total   int64
	done    int64
	lastPct int // last percent reported; -1 before the first
	report  func(done, total int64)
}

func (p *progressReader) Read(b []byte) (int, error) {
	n, err := p.r.Read(b)
	if n > 0 {
		p.done += int64(n)
		if pct := percent(p.done, p.total); pct != p.lastPct {
			p.lastPct = pct
			p.report(p.done, p.total)
		}
	}
	return n, err
}

// percent is the whole-number percent of done over total, clamped to 100 — a server
// that understates Content-Length must never yield "104%".
func percent(done, total int64) int {
	if done >= total {
		return 100
	}
	return int(done * 100 / total)
}

func runCmd(ctx context.Context, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%s: %w: %s", filepath.Base(name), err, strings.TrimSpace(string(out)))
	}
	return nil
}

func waitHealthy(ctx context.Context, url string, budget time.Duration) bool {
	deadline := time.Now().Add(budget)
	for time.Now().Before(deadline) { // bounded by the budget
		if probe(ctx, url) {
			return true
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(2 * time.Second):
		}
	}
	return false
}

func probe(ctx context.Context, url string) bool {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return false
	}
	client := http.Client{Timeout: 3 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// untarGz unpacks a .tar.gz beneath dest, refusing entries that escape it.
func untarGz(archive, dest string) error {
	f, err := os.Open(archive)
	if err != nil {
		return err
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return err
	}
	defer gz.Close()
	tr := tar.NewReader(gz)
	for { // bounded by archive contents
		hdr, err := tr.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		target, ok := securePath(dest, hdr.Name)
		if !ok {
			return fmt.Errorf("archive entry escapes destination: %q", hdr.Name)
		}
		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
		case tar.TypeReg:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			out, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, os.FileMode(hdr.Mode)&0o777)
			if err != nil {
				return err
			}
			if _, err := io.Copy(out, tr); err != nil { //nolint:gosec // trusted, checksum-verified archive
				out.Close()
				return err
			}
			out.Close()
		case tar.TypeSymlink:
			if _, ok := securePath(filepath.Dir(target), hdr.Linkname); !ok && filepath.IsAbs(hdr.Linkname) {
				return fmt.Errorf("archive symlink escapes destination: %q", hdr.Linkname)
			}
			// The symlink's PARENT may not exist yet — tar entries are not ordered the
			// way the regular-file branch assumes (python-build-standalone's archive
			// places bin/ symlinks before any file creates bin/). Failed live on the
			// first real migration: "symlink 2to3-3.12 .../python/bin/2to3: no such
			// file or directory". Creating a symlink never requires its TARGET to
			// exist, only its own parent directory.
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			_ = os.Remove(target)
			if err := os.Symlink(hdr.Linkname, target); err != nil {
				return err
			}
		}
	}
}

// unzip unpacks a .zip beneath dest, refusing entries that escape it.
func unzip(archive, dest string) error {
	r, err := zip.OpenReader(archive)
	if err != nil {
		return err
	}
	defer r.Close()
	for _, zf := range r.File { // bounded by archive contents
		target, ok := securePath(dest, zf.Name)
		if !ok {
			return fmt.Errorf("archive entry escapes destination: %q", zf.Name)
		}
		if zf.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		in, err := zf.Open()
		if err != nil {
			return err
		}
		out, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
		if err != nil {
			in.Close()
			return err
		}
		if _, err := io.Copy(out, in); err != nil { //nolint:gosec // trusted, checksum-verified archive
			in.Close()
			out.Close()
			return err
		}
		in.Close()
		out.Close()
	}
	return nil
}

// securePath joins base+name and confirms the result stays under base.
func securePath(base, name string) (string, bool) {
	target := filepath.Join(base, filepath.FromSlash(name))
	rel, err := filepath.Rel(base, target)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", false
	}
	return target, true
}
