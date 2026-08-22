// Hermetic tests for the native assembler — no network, no real python, no processes
// beyond what a test itself spawns. Fetch and Run are injected fakes, which is the same
// discipline installer/test_install.py established for the Docker path's failure modes.
package native

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
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
	n.Fetch = func(_ context.Context, url, dest string, _ func(done, total int64)) error {
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
	for i, link := range []string{"/etc/passwd", "../../outside"} {
		archive := filepath.Join(dir, fmt.Sprintf("link%d.tar.gz", i))
		writeTarGzSymlink(t, archive, "sub/planted", link)
		if err := untarGz(archive, filepath.Join(dir, fmt.Sprintf("out-link%d", i))); err == nil {
			t.Fatalf("symlink escape %q must be refused", link)
		}
	}
}

func writeTarGzSymlink(t *testing.T, dest, name, linkname string) {
	t.Helper()
	f, err := os.Create(dest)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	gz := gzip.NewWriter(f)
	tw := tar.NewWriter(gz)
	if err := tw.WriteHeader(&tar.Header{Name: name, Linkname: linkname, Mode: 0o777, Typeflag: tar.TypeSymlink}); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestProgressReaderReportsEachPercentOnce(t *testing.T) {
	var got []int
	r := newProgressReader(bytes.NewReader(make([]byte, 200)), 200,
		func(done, total int64) { got = append(got, percent(done, total)) })
	buf := make([]byte, 1) // two 1-byte reads per percent step — only the first may report
	for {
		if _, err := r.Read(buf); err == io.EOF {
			break
		}
	}
	if len(got) != 101 {
		t.Fatalf("200 reads made %d reports, want 101 (0%%–100%%, once each)", len(got))
	}
	for i, pct := range got {
		if pct != i {
			t.Fatalf("report %d = %d%%, want %d%% (monotonic, no repeats)", i, pct, i)
		}
	}
}

func TestProgressReaderSilentWithoutContentLength(t *testing.T) {
	for _, total := range []int64{0, -1} { // "no Content-Length" arrives as either
		calls := 0
		r := newProgressReader(strings.NewReader("some download body"), total,
			func(int64, int64) { calls++ })
		if _, err := io.Copy(io.Discard, r); err != nil {
			t.Fatal(err)
		}
		if calls != 0 {
			t.Fatalf("total=%d must report nothing, got %d calls", total, calls)
		}
	}
}

func TestProgressReaderClampsALyingContentLength(t *testing.T) {
	last := -1
	r := newProgressReader(bytes.NewReader(make([]byte, 150)), 100, // server said 100, sent 150
		func(done, total int64) {
			last = percent(done, total)
			if last > 100 {
				t.Fatalf("reported %d%% — must clamp at 100", last)
			}
		})
	if _, err := io.Copy(io.Discard, r); err != nil {
		t.Fatal(err)
	}
	if last != 100 {
		t.Fatalf("final report = %d%%, want 100%%", last)
	}
}

func TestProgressForNamesTheArtifactAndSurvivesNil(t *testing.T) {
	n := New(t.TempDir())
	if n.progressFor("python") != nil {
		t.Fatal("no Progress sink must mean a nil per-fetch callback, not a live one")
	}
	gotArtifact, gotPct := "", -1
	n.Progress = func(artifact string, pct int) { gotArtifact, gotPct = artifact, pct }
	n.progressFor("app")(50, 200)
	if gotArtifact != "app" || gotPct != 25 {
		t.Fatalf(`got (%q, %d%%), want ("app", 25%%)`, gotArtifact, gotPct)
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

func TestUpRefusesWhenGatewayHolderCannotBeStopped(t *testing.T) {
	// A survivor answers the gateway port but cannot be killed (here: it IS the test
	// process, tripping the self-kill guard — the stand-in for any unkillable holder).
	// Up must fail LOUD before spawning anything, never adopt the imposter: silent
	// adoption is how an Aug-7 gateway served under an Aug-22 app for four releases.
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
	if err == nil || !strings.Contains(err.Error(), "could not be stopped") {
		t.Fatalf("an unkillable gateway-port holder must fail Up loud, got: %v", err)
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

// A tester on a clean machine installed via Homebrew, let it start once under Docker
// (which creates the volumes before anyone finishes first-run), then switched to native —
// and got "database missing after copy" instead of an app. An empty volume is an ordinary
// situation, not a failure: there is simply nothing to carry over.
func TestEmptyDockerVolumeMeansFreshStartNotFailure(t *testing.T) {
	if runtime.GOOS == "darwin" {
		t.Skip("writes to the real per-OS app data dir; exercised on linux CI")
	}
	n := New(t.TempDir())
	// Every docker command "succeeds" and copies nothing — exactly what an empty volume does.
	n.Run = func(ctx context.Context, name string, args ...string) error { return nil }

	err := n.MigrateFromDocker(context.Background())
	if !errors.Is(err, ErrNoDataToMigrate) {
		t.Fatalf("an empty volume must report 'nothing to migrate', got: %v", err)
	}
}

func TestACopyThatActuallyFailsIsStillAnError(t *testing.T) {
	// The forgiving branch above must not swallow a real failure: if docker itself errors,
	// the user's data may exist and be unreachable, and a fresh start would look like loss.
	if runtime.GOOS == "darwin" {
		t.Skip("writes to the real per-OS app data dir; exercised on linux CI")
	}
	n := New(t.TempDir())
	n.Run = func(ctx context.Context, name string, args ...string) error {
		return fmt.Errorf("docker daemon is not running")
	}
	err := n.MigrateFromDocker(context.Background())
	if err == nil || errors.Is(err, ErrNoDataToMigrate) {
		t.Fatalf("a failed copy must stay an error, got: %v", err)
	}
}

// --- port-holder resolution (the bifrost zombie fix) ------------------------

func TestPortHolderPidFindsOurOwnListener(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	port := ln.Addr().(*net.TCPAddr).Port
	pid, err := portHolderPid(port)
	if err != nil {
		t.Skipf("no lookup tool on this runner: %v", err) // lsof/ss/netstat availability varies
	}
	if pid != os.Getpid() {
		t.Fatalf("expected our own pid %d, got %d", os.Getpid(), pid)
	}
}

func TestKillPortHolderRefusesSelf(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	port := ln.Addr().(*net.TCPAddr).Port
	if _, err := portHolderPid(port); err != nil {
		t.Skipf("no lookup tool on this runner: %v", err)
	}
	// The holder is THIS test process — the guard must refuse rather than suicide,
	// and the self check must fire BEFORE identity could ever justify a kill.
	n := New(t.TempDir())
	if err := n.killPortHolderIfOurs(port, "native.test"); err == nil || !strings.Contains(err.Error(), "refusing to kill self") {
		t.Fatalf("expected refusing-to-kill-self, got %v", err)
	}
}

func TestKillPortHolderRefusesForeignProcess(t *testing.T) {
	// The audit finding: the port answering does not make its holder OURS. A listener
	// whose command line lacks the marker (stand-in for Docker's port-forwarder) must
	// be left ALIVE, and the refusal must say why.
	py, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("needs python3 for the foreign listener")
	}
	foreign := exec.Command(py, "-c",
		"import socket,time,sys\ns=socket.socket()\ns.bind(('127.0.0.1',0))\ns.listen()\nprint(s.getsockname()[1])\nsys.stdout.flush()\ntime.sleep(60)")
	out, err := foreign.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := foreign.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = foreign.Process.Kill(); _, _ = foreign.Process.Wait() }()
	var port int
	if _, err := fmt.Fscan(out, &port); err != nil {
		t.Fatal(err)
	}
	if _, err := portHolderPid(port); err != nil {
		t.Skipf("no lookup tool on this runner: %v", err)
	}
	n := New(t.TempDir())
	err = n.killPortHolderIfOurs(port, bifrostProcessMarker)
	if err == nil || !strings.Contains(err.Error(), "is not a bifrost-http process") {
		t.Fatalf("a foreign port holder must be refused with the reason, got %v", err)
	}
	if err := foreign.Process.Signal(syscall.Signal(0)); err != nil {
		t.Fatal("the foreign process must still be alive — refusal means NOT killed")
	}
}

func TestParseSsPid(t *testing.T) {
	out := `State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process
LISTEN 0      1024   127.0.0.1:38080    0.0.0.0:*         users:(("bifrost-http",pid=9005,fd=7))`
	pid, err := parseSsPid(out)
	if err != nil || pid != 9005 {
		t.Fatalf("want 9005, got %d / %v", pid, err)
	}
	if _, err := parseSsPid("LISTEN 0 1024 127.0.0.1:38080 0.0.0.0:*"); err == nil {
		t.Fatal("pid-less output (another user's listener) must error, not guess")
	}
}

func TestParseNetstatPid(t *testing.T) {
	out := "  Proto  Local Address          Foreign Address        State           PID\r\n" +
		"  TCP    127.0.0.1:38080        0.0.0.0:0              LISTENING       4321\r\n" +
		"  TCP    127.0.0.1:33000        0.0.0.0:0              ESTABLISHED     9999\r\n"
	pid, err := parseNetstatPid(out, 38080)
	if err != nil || pid != 4321 {
		t.Fatalf("want 4321, got %d / %v", pid, err)
	}
	if _, err := parseNetstatPid(out, 12345); err == nil {
		t.Fatal("absent port must error")
	}
	// The state column is LOCALIZED (German below) — matching the English word
	// silently broke every non-English Windows. Shape-matching must not care.
	de := "  Proto  Lokale Adresse         Remoteadresse          Status          PID\r\n" +
		"  TCP    127.0.0.1:38080        0.0.0.0:0              ABH\u00d6REN         7777\r\n"
	pid, err = parseNetstatPid(de, 38080)
	if err != nil || pid != 7777 {
		t.Fatalf("German netstat must parse: want 7777, got %d / %v", pid, err)
	}
}

func TestParseFirstPid(t *testing.T) {
	if pid, err := parseFirstPid("9005\n"); err != nil || pid != 9005 {
		t.Fatalf("want 9005, got %d / %v", pid, err)
	}
	if _, err := parseFirstPid("\n\n"); err == nil {
		t.Fatal("empty output must error")
	}
}

// gatewaySurvivor starts a separate-process HTTP-200 server on the gateway port. When
// asOurs is true, the process is launched via an executable named `bifrost-http` (a
// python script under the hood) so its command line carries the identity marker — the
// faithful stand-in for the observed stale-gateway zombie; when false, it runs as a
// plain python process (the stand-in for Docker's port-forwarder or any foreign tool).
func gatewaySurvivor(t *testing.T, asOurs bool) *exec.Cmd {
	t.Helper()
	py, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("needs python3 for the separate-process survivor")
	}
	script := "#!/usr/bin/env python3\n" +
		"from http.server import HTTPServer, BaseHTTPRequestHandler\n" +
		"class S(BaseHTTPRequestHandler):\n" +
		"    def do_GET(self):\n        self.send_response(200)\n        self.end_headers()\n" +
		"    def log_message(self, *a):\n        pass\n" +
		fmt.Sprintf("HTTPServer(('127.0.0.1', %d), S).serve_forever()\n", BifrostPort)
	var cmd *exec.Cmd
	if asOurs {
		bin := filepath.Join(t.TempDir(), "bifrost-http")
		if err := os.WriteFile(bin, []byte(script), 0o755); err != nil {
			t.Fatal(err)
		}
		cmd = exec.Command(bin)
	} else {
		cmd = exec.Command(py, "-c", strings.TrimPrefix(script, "#!/usr/bin/env python3\n"))
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill(); _, _ = cmd.Process.Wait() })
	healthURL := fmt.Sprintf("http://127.0.0.1:%d/api/health", BifrostPort)
	deadline := time.Now().Add(10 * time.Second)
	for !probe(context.Background(), healthURL) { // bounded by the deadline
		if time.Now().After(deadline) {
			t.Fatal("survivor server never came up")
		}
		time.Sleep(100 * time.Millisecond)
	}
	return cmd
}

func TestUpKillsStaleGatewayAndProceeds(t *testing.T) {
	// The auto-heal itself: a SEPARATE stale bifrost-http answers the gateway port
	// (the observed zombie — an Aug-7 gateway under an Aug-22 app). Up's preflight
	// must verify its identity, kill it, and proceed to spawn its own gateway —
	// proven by Up failing LATER on the stub bifrost dying, which is only reachable
	// after the port was cleared.
	if runtime.GOOS == "windows" {
		t.Skip("uses a unix shell-script spawn")
	}
	if _, err := currentPlatform(); err != nil {
		t.Skipf("unshipped platform: %v", err)
	}
	healthURL := fmt.Sprintf("http://127.0.0.1:%d/api/health", BifrostPort)
	if probe(context.Background(), healthURL) {
		t.Skip("gateway port busy on this host")
	}
	gatewaySurvivor(t, true) // command line carries the bifrost-http marker

	n := New(t.TempDir())
	n.Port = freeClosedPort(t) // nothing answers the APP port -> that preflight passes
	completedVersion(t, n, "1.0.0", map[string]string{"bifrost-http": "#!/bin/sh\nexit 0\n"})
	err := n.Up(context.Background())
	// Past the preflight the stub gateway exits at once — awaitChild's net catches
	// THAT. Reaching this error is the proof the stale holder was removed first.
	if err == nil || !strings.Contains(err.Error(), "another instance is running") {
		t.Fatalf("expected the spawn-death error after the stale holder was cleared, got: %v", err)
	}
	if probe(context.Background(), healthURL) {
		t.Fatal("the stale gateway is still serving — the preflight did not kill it")
	}
}

func TestUpRefusesForeignGatewayPortHolder(t *testing.T) {
	// The identity gate at the Up level: a FOREIGN process on the gateway port (the
	// Docker port-forwarder case from the audit) must fail Up loud — and survive.
	if runtime.GOOS == "windows" {
		t.Skip("uses a unix shell-script spawn")
	}
	if _, err := currentPlatform(); err != nil {
		t.Skipf("unshipped platform: %v", err)
	}
	healthURL := fmt.Sprintf("http://127.0.0.1:%d/api/health", BifrostPort)
	if probe(context.Background(), healthURL) {
		t.Skip("gateway port busy on this host")
	}
	gatewaySurvivor(t, false) // plain python: no marker in the command line

	n := New(t.TempDir())
	n.Port = freeClosedPort(t)
	completedVersion(t, n, "1.0.0", map[string]string{"bifrost-http": "#!/bin/sh\nexit 0\n"})
	err := n.Up(context.Background())
	if err == nil || !strings.Contains(err.Error(), "could not be stopped") {
		t.Fatalf("a foreign gateway-port holder must fail Up loud, got: %v", err)
	}
	if !probe(context.Background(), healthURL) {
		t.Fatal("the foreign process must still be serving — refusal means NOT killed")
	}
}
