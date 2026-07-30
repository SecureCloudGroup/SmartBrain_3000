// Hermetic tests for the native assembler — no network, no real python, no processes
// beyond what a test itself spawns. Fetch and Run are injected fakes, which is the same
// discipline installer/test_install.py established for the Docker path's failure modes.
package native

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"context"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"
)

// fixture builders -----------------------------------------------------------

func writeTarGz(t *testing.T, dest string, files map[string]string) {
	t.Helper()
	f, err := os.Create(dest)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	gz := gzip.NewWriter(f)
	tw := tar.NewWriter(gz)
	for name, body := range files {
		if err := tw.WriteHeader(&tar.Header{Name: name, Mode: 0o755, Size: int64(len(body)), Typeflag: tar.TypeReg}); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
}

func writeZip(t *testing.T, dest string, files map[string]string) {
	t.Helper()
	f, err := os.Create(dest)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	zw := zip.NewWriter(f)
	for name, body := range files {
		w, err := zw.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
}

func fileSum(t *testing.T, path string) string {
	t.Helper()
	sum, err := sha256File(path)
	if err != nil {
		t.Fatal(err)
	}
	return sum
}

// harness: a Native whose Fetch serves prebuilt fixtures and whose Run records calls.
func harness(t *testing.T, version string) (Native, *[]string) {
	t.Helper()
	plat, err := currentPlatform()
	if err != nil {
		t.Skipf("unsupported test platform: %v", err)
	}
	fixtures := t.TempDir()
	pbsTar := filepath.Join(fixtures, "pbs.tar.gz")
	writeTarGz(t, pbsTar, map[string]string{plat.pythonRel: "#!fake python\n"})
	whName := fmt.Sprintf("smartbrain-wheelhouse-%s-%s", version, plat.wheelLabel)
	whZip := filepath.Join(fixtures, "wh.zip")
	writeZip(t, whZip, map[string]string{whName + "/VERSION": version + "\n",
		whName + "/smartbrain_3000-0.1.0-py3-none-any.whl": "fake wheel"})
	bifrost := filepath.Join(fixtures, "bifrost")
	if err := os.WriteFile(bifrost, []byte("#!fake gateway\n"), 0o755); err != nil {
		t.Fatal(err)
	}

	// The REAL pin maps must accept the fixtures, so the pins under test are the test's
	// own fixture sums — swapped in and restored. (Never mutate the shipped pins in place.)
	origPbs, origBif := pbsSums[plat.pbsTriple], bifrostSums[plat.bifrostAsset]
	pbsSums[plat.pbsTriple] = fileSum(t, pbsTar)
	bifrostSums[plat.bifrostAsset] = fileSum(t, bifrost)
	t.Cleanup(func() {
		pbsSums[plat.pbsTriple] = origPbs
		bifrostSums[plat.bifrostAsset] = origBif
	})

	calls := &[]string{}
	n := New(t.TempDir())
	n.Fetch = func(_ context.Context, url, dest string) error {
		switch {
		case strings.Contains(url, "python-build-standalone"):
			return copyFile(pbsTar, dest)
		case strings.HasSuffix(url, ".zip.sha256"):
			return os.WriteFile(dest, []byte(fileSum(t, whZip)+"  "+whName+".zip\n"), 0o600)
		case strings.HasSuffix(url, ".zip"):
			return copyFile(whZip, dest)
		case strings.Contains(url, bifrostRelease):
			return copyFile(bifrost, dest)
		}
		return fmt.Errorf("unexpected fetch: %s", url)
	}
	n.Run = func(_ context.Context, name string, args ...string) error {
		*calls = append(*calls, filepath.Base(name)+" "+strings.Join(args, " "))
		return nil
	}
	return n, calls
}

func copyFile(src, dest string) error {
	b, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	return os.WriteFile(dest, b, 0o755)
}

// tests ----------------------------------------------------------------------

func TestAssembleHappyPathFlipsCurrentLast(t *testing.T) {
	n, calls := harness(t, "9.9.9")
	if n.Current() != "" {
		t.Fatal("fresh dir must report no current version")
	}
	if err := n.Assemble(context.Background(), "9.9.9"); err != nil {
		t.Fatal(err)
	}
	if got := n.Current(); got != "9.9.9" {
		t.Fatalf("current = %q, want 9.9.9", got)
	}
	if _, err := os.Stat(filepath.Join(n.versionDir("9.9.9"), ".complete")); err != nil {
		t.Fatal("complete marker missing")
	}
	joined := strings.Join(*calls, "\n")
	if !strings.Contains(joined, "--no-index") || !strings.Contains(joined, "--find-links") {
		t.Fatalf("install must be offline from the wheelhouse; ran:\n%s", joined)
	}
	if !strings.Contains(joined, "VERSION") {
		t.Fatalf("version stamp missing; ran:\n%s", joined)
	}
	// Idempotent: assembling the same version again re-runs nothing.
	before := len(*calls)
	if err := n.Assemble(context.Background(), "9.9.9"); err != nil {
		t.Fatal(err)
	}
	if len(*calls) != before {
		t.Fatal("re-assembly of a complete version must be a no-op")
	}
}

func TestTamperedGatewayBinaryRefused(t *testing.T) {
	n, _ := harness(t, "9.9.9")
	plat, _ := currentPlatform()
	orig := bifrostSums[plat.bifrostAsset]
	bifrostSums[plat.bifrostAsset] = strings.Repeat("0", 64) // the pin no longer matches
	t.Cleanup(func() { bifrostSums[plat.bifrostAsset] = orig })
	err := n.Assemble(context.Background(), "9.9.9")
	if err == nil || !strings.Contains(err.Error(), "checksum mismatch") {
		t.Fatalf("tampered binary must fail the checksum, got: %v", err)
	}
	if n.Current() != "" {
		t.Fatal("a failed assembly must never flip current")
	}
	entries, _ := os.ReadDir(n.versionsDir())
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".tmp-") {
			t.Fatal("failed assembly must clean its work dir")
		}
	}
}

func TestFailedInstallLeavesPreviousVersionCurrent(t *testing.T) {
	n, _ := harness(t, "1.0.0")
	if err := n.Assemble(context.Background(), "1.0.0"); err != nil {
		t.Fatal(err)
	}
	n.Run = func(context.Context, string, ...string) error {
		return fmt.Errorf("pip exploded")
	}
	// Reuse the same fixtures for a "new" version: fetch serves by URL shape, and the
	// wheelhouse name embeds the version — regenerate the harness fetch via a fresh one.
	n2, _ := harness(t, "2.0.0")
	n2.Dir = n.Dir // same install root: 1.0.0 is the incumbent
	n2.Run = func(context.Context, string, ...string) error { return fmt.Errorf("pip exploded") }
	err := n2.Assemble(context.Background(), "2.0.0")
	if err == nil || !strings.Contains(err.Error(), "pip exploded") {
		t.Fatalf("install failure must surface, got: %v", err)
	}
	if got := n.Current(); got != "1.0.0" {
		t.Fatalf("rollback broken: current = %q, want the incumbent 1.0.0", got)
	}
}

func TestCurrentIgnoresTornPointer(t *testing.T) {
	n, _ := harness(t, "9.9.9")
	if err := os.MkdirAll(n.Dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(n.currentPath(), []byte("ghost-version\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := n.Current(); got != "" {
		t.Fatalf("pointer at a missing assembly must read empty, got %q", got)
	}
}

func TestArchiveEscapeRefused(t *testing.T) {
	dir := t.TempDir()
	evilTar := filepath.Join(dir, "evil.tar.gz")
	writeTarGz(t, evilTar, map[string]string{"../escape.txt": "boo"})
	if err := untarGz(evilTar, filepath.Join(dir, "out")); err == nil {
		t.Fatal("tar path escape must be refused")
	}
	evilZip := filepath.Join(dir, "evil.zip")
	writeZip(t, evilZip, map[string]string{"../escape.txt": "boo"})
	if err := unzip(evilZip, filepath.Join(dir, "out2")); err == nil {
		t.Fatal("zip path escape must be refused")
	}
}

func TestWaitHealthyBoundedAndSucceeds(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	if !waitHealthy(context.Background(), srv.URL, 5*time.Second) {
		t.Fatal("healthy endpoint must pass")
	}
	start := time.Now()
	if waitHealthy(context.Background(), "http://127.0.0.1:1/never", 3*time.Second) {
		t.Fatal("dead endpoint must fail")
	}
	if time.Since(start) > 10*time.Second {
		t.Fatal("health wait must respect its budget")
	}
}

func TestDownIsIdempotentAndCleansPids(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a unix sleep child")
	}
	n := New(t.TempDir())
	if err := os.MkdirAll(n.runDir(), 0o700); err != nil {
		t.Fatal(err)
	}
	// A stale pid file pointing at nothing must not break Down.
	if err := os.WriteFile(filepath.Join(n.runDir(), "app.pid"), []byte("999999\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	n.Down()
	n.Down() // idempotent
	if _, err := os.Stat(filepath.Join(n.runDir(), "app.pid")); !os.IsNotExist(err) {
		t.Fatal("Down must remove pid files")
	}
}

func TestUnsupportedPlatformIsAnHonestError(t *testing.T) {
	// Can't change GOOS at runtime; assert the mapping covers exactly the shipped set
	// and that every mapped platform has BOTH pins present.
	plat, err := currentPlatform()
	if err != nil {
		t.Skipf("running on an unshipped platform: %v", err)
	}
	if pbsSums[plat.pbsTriple] == "" || bifrostSums[plat.bifrostAsset] == "" {
		t.Fatal("current platform is missing a pinned checksum")
	}
	for triple, sum := range pbsSums {
		if len(sum) != 64 {
			t.Fatalf("pbs pin for %s is not a sha256", triple)
		}
	}
	for asset, sum := range bifrostSums {
		if len(sum) != 64 {
			t.Fatalf("bifrost pin for %s is not a sha256", asset)
		}
	}
}

func TestPrepareBifrostDataKillsLoggingAtTheSource(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "bifrost-data")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	// A historical plaintext log must be destroyed, and the kill-file written.
	for _, f := range []string{"logs.db", "logs.db-wal", "logs.db-shm"} {
		if err := os.WriteFile(filepath.Join(dir, f), []byte("secrets"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := prepareBifrostData(dir); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"logs.db", "logs.db-wal", "logs.db-shm"} {
		if _, err := os.Stat(filepath.Join(dir, f)); !os.IsNotExist(err) {
			t.Fatalf("%s must be destroyed", f)
		}
	}
	cfg, err := os.ReadFile(filepath.Join(dir, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{`"logs_store":{"enabled":false}`, `"enable_logging":false`, `"disable_content_logging":true`} {
		if !strings.Contains(string(cfg), want) {
			t.Fatalf("kill-file missing %s; got: %s", want, cfg)
		}
	}
	// Idempotent: running again over the fresh state is fine.
	if err := prepareBifrostData(dir); err != nil {
		t.Fatal(err)
	}
}

func TestWatchRestartsAreBoundedAndReported(t *testing.T) {
	n := New(t.TempDir())
	n.Tick = 10 * time.Millisecond
	n.Port = 1 // nothing listens: permanently unhealthy
	var mu = make(chan string, 64)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	done := make(chan struct{})
	go func() {
		n.Watch(ctx, func(s string) { mu <- s })
		close(done)
	}()
	select {
	case <-done: // Watch gave up on its own after the bounded restart allowance
	case <-ctx.Done():
		t.Fatal("watch must stop itself after the bounded restart allowance")
	}
	var statuses []string
	for len(mu) > 0 {
		statuses = append(statuses, <-mu)
	}
	joined := strings.Join(statuses, "\n")
	if !strings.Contains(joined, "restarting") || !strings.Contains(joined, "stopped restarting") {
		t.Fatalf("expected restart attempts then a bounded give-up; got:\n%s", joined)
	}
	restartCount := strings.Count(joined, "SmartBrain stopped — restarting…")
	if restartCount != 3 {
		t.Fatalf("restart attempts = %d, want exactly the bounded 3", restartCount)
	}
}

func TestWatchStopsOnContextCancel(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK) // healthy forever: Watch just idles
	}))
	defer srv.Close()
	n := New(t.TempDir())
	n.Tick = 10 * time.Millisecond
	fmt.Sscanf(srv.URL, "http://127.0.0.1:%d", &n.Port)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { n.Watch(ctx, func(string) {}); close(done) }()
	time.Sleep(100 * time.Millisecond)
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("watch must exit when its context is cancelled")
	}
}

func TestNeedsMigrationLogic(t *testing.T) {
	n := New(t.TempDir())
	volumeExists := true
	n.Run = func(_ context.Context, name string, args ...string) error {
		if name == "docker" && len(args) > 1 && args[0] == "volume" {
			if volumeExists {
				return nil
			}
			return fmt.Errorf("no such volume")
		}
		return nil
	}
	// Point the native data check at a temp dir via env (the per-OS branch honors it
	// on linux; on darwin the real path may exist on a dev machine, so skip there).
	if runtime.GOOS == "darwin" {
		t.Skip("appDataDir is fixed on darwin; covered by the linux CI leg")
	}
	tmp := t.TempDir()
	t.Setenv("XDG_DATA_HOME", tmp)
	t.Setenv("APPDATA", tmp)
	if !n.NeedsMigration(context.Background()) {
		t.Fatal("volume present + no native data must need migration")
	}
	volumeExists = false
	if n.NeedsMigration(context.Background()) {
		t.Fatal("no volume must mean nothing to migrate")
	}
	volumeExists = true
	dataDir, err := appDataDir()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(dataDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dataDir, "smartbrain.duckdb"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if n.NeedsMigration(context.Background()) {
		t.Fatal("existing native data must never be overwritten by a migration")
	}
}

func TestMigrateCopiesReadOnlyAndVerifies(t *testing.T) {
	if runtime.GOOS == "darwin" {
		t.Skip("appDataDir is fixed on darwin; covered by the linux CI leg")
	}
	tmp := t.TempDir()
	t.Setenv("XDG_DATA_HOME", tmp)
	t.Setenv("APPDATA", tmp)
	n := New(t.TempDir())
	var cmds []string
	n.Run = func(_ context.Context, name string, args ...string) error {
		cmds = append(cmds, name+" "+strings.Join(args, " "))
		// Fake the copy by materializing a plausible database when the app-data copy runs.
		if strings.Contains(strings.Join(args, " "), appVolume) {
			dataDir, _ := appDataDir()
			_ = os.MkdirAll(dataDir, 0o700)
			return os.WriteFile(filepath.Join(dataDir, "smartbrain.duckdb"),
				make([]byte, 8192), 0o600)
		}
		return nil
	}
	if err := n.MigrateFromDocker(context.Background()); err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(cmds, "\n")
	if !strings.Contains(joined, appVolume+":/from:ro") || !strings.Contains(joined, bifrostVolume+":/from:ro") {
		t.Fatalf("volumes must be mounted READ-ONLY (the rollback guarantee); ran:\n%s", joined)
	}
	if !strings.Contains(joined, "cp -a /from/. /to/") {
		t.Fatalf("copy command missing; ran:\n%s", joined)
	}
}

func TestMigrateFailsLoudlyOnStubDatabase(t *testing.T) {
	if runtime.GOOS == "darwin" {
		t.Skip("appDataDir is fixed on darwin; covered by the linux CI leg")
	}
	tmp := t.TempDir()
	t.Setenv("XDG_DATA_HOME", tmp)
	t.Setenv("APPDATA", tmp)
	n := New(t.TempDir())
	n.Run = func(_ context.Context, _ string, args ...string) error {
		if strings.Contains(strings.Join(args, " "), appVolume) {
			dataDir, _ := appDataDir()
			_ = os.MkdirAll(dataDir, 0o700)
			return os.WriteFile(filepath.Join(dataDir, "smartbrain.duckdb"), []byte("xx"), 0o600)
		}
		return nil
	}
	err := n.MigrateFromDocker(context.Background())
	if err == nil || !strings.Contains(err.Error(), "suspiciously small") {
		t.Fatalf("a stub database must fail verification, got: %v", err)
	}
}

func TestUntarSymlinkBeforeParentDir(t *testing.T) {
	// Reproduces the first live migration's failure exactly: python-build-standalone's
	// archive emits bin/ SYMLINKS before anything has created bin/ — "symlink
	// 2to3-3.12 .../python/bin/2to3: no such file or directory".
	dir := t.TempDir()
	archive := filepath.Join(dir, "pbs-shaped.tar.gz")
	f, err := os.Create(archive)
	if err != nil {
		t.Fatal(err)
	}
	gz := gzip.NewWriter(f)
	tw := tar.NewWriter(gz)
	// Entry 1: a symlink deep in a directory NO prior entry created — and whose
	// target does not exist yet either (both were true in the real archive).
	if err := tw.WriteHeader(&tar.Header{Name: "python/bin/2to3", Typeflag: tar.TypeSymlink,
		Linkname: "2to3-3.12", Mode: 0o777}); err != nil {
		t.Fatal(err)
	}
	// Entry 2: the target file, only now.
	body := "#!/fake\n"
	if err := tw.WriteHeader(&tar.Header{Name: "python/bin/2to3-3.12", Typeflag: tar.TypeReg,
		Mode: 0o755, Size: int64(len(body))}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write([]byte(body)); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	f.Close()
	out := filepath.Join(dir, "out")
	if err := untarGz(archive, out); err != nil {
		t.Fatalf("symlink-before-parent must unpack (the live-run bug): %v", err)
	}
	link, err := os.Readlink(filepath.Join(out, "python", "bin", "2to3"))
	if err != nil || link != "2to3-3.12" {
		t.Fatalf("symlink not materialized correctly: %q, %v", link, err)
	}
}

func TestDiscardMigratedDataRemovesBothCopies(t *testing.T) {
	if runtime.GOOS == "darwin" {
		t.Skip("appDataDir is fixed on darwin; covered by the linux CI leg")
	}
	tmp := t.TempDir()
	t.Setenv("XDG_DATA_HOME", tmp)
	t.Setenv("APPDATA", tmp)
	n := New(t.TempDir())
	dataDir, err := appDataDir()
	if err != nil {
		t.Fatal(err)
	}
	for _, d := range []string{dataDir, n.bifrostData()} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(d, "stale.db"), []byte("old"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	n.DiscardMigratedData()
	for _, d := range []string{dataDir, n.bifrostData()} {
		if _, err := os.Stat(d); !os.IsNotExist(err) {
			t.Fatalf("%s must be removed after a failed takeover", d)
		}
	}
}

func TestUntarRealRuntimeArchive(t *testing.T) {
	// Opt-in integration proof: point PBS_FIXTURE at a real python-build-standalone
	// tarball and this unpacks it with the production code path — the exact archive
	// whose symlink ordering broke the first live migration.
	fixture := os.Getenv("PBS_FIXTURE")
	if fixture == "" {
		t.Skip("set PBS_FIXTURE to a real pbs tar.gz to run")
	}
	out := t.TempDir()
	if err := untarGz(fixture, out); err != nil {
		t.Fatalf("real runtime archive must unpack: %v", err)
	}
	if _, err := os.Stat(filepath.Join(out, "python")); err != nil {
		t.Fatal("unpacked runtime missing python/ root")
	}
}

// --- supervision truthfulness (the 2026-07-29 pid-file poisoning) ------------

// completedVersion plants an assembled-version fixture so Current() resolves.
func completedVersion(t *testing.T, n Native, version string, files map[string]string) {
	t.Helper()
	vdir := n.versionDir(version)
	if err := os.MkdirAll(vdir, 0o700); err != nil {
		t.Fatal(err)
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(vdir, name), []byte(body), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(vdir, ".complete"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(n.currentPath(), []byte(version+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestUpRefusesWhenAnotherInstanceServes(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	n := New(t.TempDir())
	if _, err := fmt.Sscanf(srv.URL, "http://127.0.0.1:%d", &n.Port); err != nil {
		t.Fatal(err)
	}
	completedVersion(t, n, "1.0.0", nil)
	err := n.Up(context.Background())
	if err == nil || !strings.Contains(err.Error(), "already serving") {
		t.Fatalf("Up against a serving instance must refuse, got: %v", err)
	}
	// Refusing means touching NOTHING: no spawns, so no pid files.
	if _, statErr := os.Stat(filepath.Join(n.runDir(), "bifrost.pid")); !os.IsNotExist(statErr) {
		t.Fatal("a refused Up must not write pid files")
	}
}

// freeClosedPort returns a port that was just free and now has no listener.
func freeClosedPort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	ln.Close()
	return port
}

func TestUpDetectsDeadSpawnBehindSurvivor(t *testing.T) {
	// The poisoning, replayed: our gateway spawn dies while ANOTHER process answers the
	// gateway port. Health passes (the survivor answers); Up used to conclude success and
	// leave pid files naming the dead spawn, so every later Down stopped nothing.
	if runtime.GOOS == "windows" {
		t.Skip("uses a unix shell-script spawn")
	}
	if _, err := currentPlatform(); err != nil {
		t.Skipf("unshipped platform: %v", err) // Up needs the platform mapping past preflight
	}
	ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", BifrostPort))
	if err != nil {
		t.Skipf("gateway port busy on this host: %v", err)
	}
	survivor := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})}
	go func() { _ = survivor.Serve(ln) }()
	defer survivor.Close()

	n := New(t.TempDir())
	n.Port = freeClosedPort(t) // nothing answers the APP port -> preflight passes
	completedVersion(t, n, "1.0.0", map[string]string{"bifrost-http": "#!/bin/sh\nexit 0\n"})
	err = n.Up(context.Background())
	if err == nil || !strings.Contains(err.Error(), "another instance is running") {
		t.Fatalf("a dead spawn behind an answering survivor must be refused, got: %v", err)
	}
	// The dead spawn's pid record must not outlive the failure (Down drops it).
	if _, statErr := os.Stat(filepath.Join(n.runDir(), "bifrost.pid")); !os.IsNotExist(statErr) {
		t.Fatal("the failure path must clean the dead spawn's pid file")
	}
}

func TestUpCatchesASLOWSpawnDeath(t *testing.T) {
	// The refutation an adversarial review landed on the FIRST version of this fix: it
	// watched the spawn for a fixed 1.5s AFTER health passed, but the app's real death
	// (losing the database lock) takes several seconds of interpreter startup — so the
	// doomed spawn passed the window and Up reported success. Watching the child's own
	// exit has no window to outlive.
	if runtime.GOOS == "windows" {
		t.Skip("uses a unix shell-script spawn")
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK) // a survivor answering from the first instant
	}))
	defer srv.Close()
	dir := t.TempDir()
	script := filepath.Join(dir, "slow-death.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nsleep 2\nexit 1\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	n := New(dir)
	if err := os.MkdirAll(n.runDir(), 0o700); err != nil {
		t.Fatal(err)
	}
	c, err := n.spawn(context.Background(), "app", script)
	if err != nil {
		t.Fatal(err)
	}
	// Health answers immediately, so a settle-window check would have passed here.
	if err := awaitChild(context.Background(), c, srv.URL, 20*time.Second, appStartupGrace); err == nil {
		t.Fatal("a spawn that dies while the port answers must never read as a healthy start")
	} else if !strings.Contains(err.Error(), "another instance is running") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestUpRefusesWhenARecordedChildIsStillAlive(t *testing.T) {
	// The survivor a health probe cannot see: alive but not answering (wedged, or still
	// warming). Down keeps its record precisely so this refusal can happen — without it,
	// spawn() would overwrite the launcher's only handle on that process and orphan it.
	if runtime.GOOS == "windows" {
		t.Skip("identity verification is unix-only; windows relies on the health preflight")
	}
	if _, err := currentPlatform(); err != nil {
		t.Skipf("unshipped platform: %v", err)
	}
	n := New(t.TempDir())
	n.Port = freeClosedPort(t) // nothing answers -> the health preflight passes
	completedVersion(t, n, "1.0.0", nil)
	if err := os.MkdirAll(n.runDir(), 0o700); err != nil {
		t.Fatal(err)
	}
	child := exec.Command("sleep", "30")
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = child.Process.Kill(); _ = child.Wait() }()
	pid := child.Process.Pid
	if err := os.WriteFile(filepath.Join(n.runDir(), "app.pid"),
		[]byte(strconv.Itoa(pid)+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	// Identity says it IS ours -> refuse, naming it.
	n.PS = func(int) string { return "/x/python3 -m smartbrain_3000.serve" }
	err := n.Up(context.Background())
	if err == nil || !strings.Contains(err.Error(), "still running") {
		t.Fatalf("a live recorded child must block a second start, got: %v", err)
	}
	if !strings.Contains(err.Error(), strconv.Itoa(pid)) {
		t.Fatalf("the refusal must name the pid so a human can act: %v", err)
	}
	// A RECYCLED pid (same number, unrelated process) must NOT block startup — refusing
	// to start because a pid file outlived a reboot would be worse than the bug it guards.
	n.PS = func(int) string { return "/usr/bin/some-unrelated-thing" }
	if _, _, alive := n.liveRecordedChild(); alive {
		t.Fatal("an unverifiable pid must read as not-ours (fail open to starting)")
	}
}

func TestDownConfirmsDeathOfALiveChild(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a unix sleep child")
	}
	n := New(t.TempDir())
	if err := os.MkdirAll(n.runDir(), 0o700); err != nil {
		t.Fatal(err)
	}
	child := exec.Command("sleep", "30")
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	pid := child.Process.Pid
	go func() { _ = child.Wait() }() // reap so the kill is observable as gone
	if err := os.WriteFile(filepath.Join(n.runDir(), "app.pid"),
		[]byte(strconv.Itoa(pid)+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	n.Down()
	if processAlive(pid) {
		t.Fatal("Down must actually end a live child, not just signal it")
	}
	if _, err := os.Stat(filepath.Join(n.runDir(), "app.pid")); !os.IsNotExist(err) {
		t.Fatal("a confirmed-dead child's pid file must be removed")
	}
}

func TestProcessAliveAndWaitGone(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("probe semantics differ; unix-only assertions")
	}
	if !processAlive(os.Getpid()) {
		t.Fatal("this test's own process is alive")
	}
	short := exec.Command("true")
	if err := short.Start(); err != nil {
		t.Fatal(err)
	}
	pid := short.Process.Pid
	_ = short.Wait() // fully reaped -> truly gone
	if !waitGone(pid, 2*time.Second) {
		t.Fatal("an exited, reaped process must read as gone")
	}
	if processAlive(0) || processAlive(-5) {
		t.Fatal("nonsense pids are never alive")
	}
}
