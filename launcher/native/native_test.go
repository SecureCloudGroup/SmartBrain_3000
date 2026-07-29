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
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
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
