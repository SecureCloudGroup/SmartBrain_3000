// Package native is the launcher's Docker-free stack (Docker-exit Phase 1) — behind an
// explicit opt-in flag, nothing here runs by default.
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

// Native points at one Docker-free SmartBrain install rooted under the launcher's dir.
type Native struct {
	Dir  string // <launcher dir>/native — versions/, current, run/, bifrost-data/ live here
	Port int    // app port (loopback)

	// Injectable side effects (tests replace these; production uses the defaults).
	Fetch func(ctx context.Context, url, dest string) error            // download url -> dest file
	Run   func(ctx context.Context, name string, args ...string) error // run a command to completion
}

// New returns a Native rooted beside the launcher's existing state dir.
func New(launcherDir string) Native {
	n := Native{Dir: filepath.Join(launcherDir, "native"), Port: AppPort}
	n.Fetch = fetchURL
	n.Run = runCmd
	return n
}

func (n Native) versionsDir() string        { return filepath.Join(n.Dir, "versions") }
func (n Native) versionDir(v string) string { return filepath.Join(n.versionsDir(), v) }
func (n Native) currentPath() string        { return filepath.Join(n.Dir, "current") }
func (n Native) runDir() string             { return filepath.Join(n.Dir, "run") }
func (n Native) bifrostData() string        { return filepath.Join(n.Dir, "bifrost-data") }

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
	if err := n.fetchVerified(ctx, pbsBase+"/"+pbsRelease+"/"+pbsName, pbsTar, pbsSums[plat.pbsTriple]); err != nil {
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
	if err := n.Fetch(ctx, base+whName+".zip.sha256", sumFile); err != nil {
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
	if err := n.fetchVerified(ctx, base+whName+".zip", whZip, want[0]); err != nil {
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
	if err := n.fetchVerified(ctx, bifrostURL, bifrostDest, bifrostSums[plat.bifrostAsset]); err != nil {
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

// fetchVerified downloads to dest and enforces the expected sha256, deleting on mismatch.
func (n Native) fetchVerified(ctx context.Context, url, dest, wantSum string) error {
	if wantSum == "" {
		return fmt.Errorf("no pinned checksum for %s — refusing to download", filepath.Base(dest))
	}
	if err := n.Fetch(ctx, url, dest); err != nil {
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

// Up starts bifrost-http then the app from the current assembly, recording pids so a
// relaunched launcher can still stop them. Bounded health waits; any failure stops
// whatever was started (no half-up stack).
func (n Native) Up(ctx context.Context) error {
	version := n.Current()
	if version == "" {
		return fmt.Errorf("native up: nothing assembled yet")
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
	bifrost := filepath.Join(vdir, "bifrost-http")
	if runtime.GOOS == "windows" {
		bifrost += ".exe"
	}
	if err := n.spawn(ctx, "bifrost", bifrost,
		"-app-dir", n.bifrostData(), "-host", "127.0.0.1", "-port", strconv.Itoa(BifrostPort)); err != nil {
		return fmt.Errorf("native up: gateway: %w", err)
	}
	if !waitHealthy(ctx, fmt.Sprintf("http://127.0.0.1:%d/api/health", BifrostPort), 60*time.Second) {
		n.Down()
		return fmt.Errorf("native up: gateway never became healthy")
	}
	// The app needs no forced env: Phase 0's native defaults point it at loopback Bifrost
	// and the per-OS data dir on their own — running the defaults IS the test of them.
	py := filepath.Join(vdir, plat.pythonRel)
	if err := n.spawn(ctx, "app", py, "-m", "smartbrain_3000.serve"); err != nil {
		n.Down()
		return fmt.Errorf("native up: app: %w", err)
	}
	if !waitHealthy(ctx, fmt.Sprintf("http://127.0.0.1:%d/api/health", n.Port), 120*time.Second) {
		n.Down()
		return fmt.Errorf("native up: app never became healthy")
	}
	return nil
}

// Down stops both children (best-effort, idempotent): TERM, brief grace, then KILL.
func (n Native) Down() {
	for _, name := range []string{"app", "bifrost"} { // app first: it talks to the gateway
		pidFile := filepath.Join(n.runDir(), name+".pid")
		raw, err := os.ReadFile(pidFile)
		if err != nil {
			continue
		}
		if pid, perr := strconv.Atoi(strings.TrimSpace(string(raw))); perr == nil && pid > 1 {
			terminate(pid)
		}
		_ = os.Remove(pidFile)
	}
}

// Healthy mirrors stack.Healthy: one cheap loopback GET.
func (n Native) Healthy(ctx context.Context) bool {
	return probe(ctx, fmt.Sprintf("http://127.0.0.1:%d/api/health", n.Port))
}

// spawn starts a child with logs under run/ and its pid recorded.
func (n Native) spawn(ctx context.Context, name, bin string, args ...string) error {
	logFile, err := os.OpenFile(filepath.Join(n.runDir(), name+".log"),
		os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	cmd := exec.Command(bin, args...) // deliberately NOT CommandContext: ctx cancel must not kill the stack
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	if err := cmd.Start(); err != nil {
		logFile.Close()
		return err
	}
	go func() { _ = cmd.Wait(); logFile.Close() }() // reap; restart-on-crash is the next phase
	return os.WriteFile(filepath.Join(n.runDir(), name+".pid"),
		[]byte(strconv.Itoa(cmd.Process.Pid)+"\n"), 0o600)
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

func fetchURL(ctx context.Context, url, dest string) error {
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
	_, err = io.Copy(f, resp.Body)
	return err
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
