// Hermetic self-update tests: injected fetchers, temp install roots, recorded
// relaunches — no network, no real swap of anything living.
package update

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNewerComparison(t *testing.T) {
	cases := []struct {
		candidate, current string
		want               bool
	}{
		{"0.8.4", "0.8.3", true},
		{"0.9.0", "0.8.9", true},
		{"1.0.0", "0.9.9", true},
		{"0.8.3", "0.8.3", false},
		{"0.8.2", "0.8.3", false},
		{"garbage", "0.8.3", false}, // unparseable NEVER updates (fail-closed)
		{"0.8.4", "garbage", false},
		{"0.8", "0.8.3", false},
		{"1.2.3.4", "0.8.3", false},
	}
	for _, c := range cases {
		if got := Newer(c.candidate, c.current); got != c.want {
			t.Fatalf("Newer(%q, %q) = %v, want %v", c.candidate, c.current, got, c.want)
		}
	}
}

func TestLatestIsIndependentOfLauncherVersion(t *testing.T) {
	serve := func(version, body string, err error) Updater {
		u := New(version)
		u.FetchBody = func(context.Context, string) ([]byte, error) { return []byte(body), err }
		return u
	}
	// A dev launcher may not self-update, but it must still SEE releases — the
	// native app assembly updates against Latest regardless of the binary's version.
	if v, ok := serve("dev", `{"tag_name":"v0.8.5"}`, nil).Latest(context.Background()); !ok || v != "0.8.5" {
		t.Fatalf("Latest must work from a dev build, got %q %v", v, ok)
	}
	if _, ok := serve("0.8.4", `{"tag_name":"nonsense"}`, nil).Latest(context.Background()); ok {
		t.Fatal("an unparseable tag must read as no release")
	}
	if _, ok := serve("0.8.4", ``, fmt.Errorf("offline")).Latest(context.Background()); ok {
		t.Fatal("API failure must read as no release")
	}
}

func TestAvailableFailsClosed(t *testing.T) {
	serve := func(body string, err error) Updater {
		u := New("0.8.3")
		u.FetchBody = func(context.Context, string) ([]byte, error) { return []byte(body), err }
		return u
	}
	if _, ok := New("dev").Available(context.Background()); ok {
		t.Fatal("dev builds must never self-update")
	}
	if _, ok := serve(`{"tag_name":"v0.8.4"}`, nil).Available(context.Background()); !ok {
		t.Fatal("a newer release must be offered")
	}
	if _, ok := serve(`{"tag_name":"v0.8.3"}`, nil).Available(context.Background()); ok {
		t.Fatal("the same version must not be offered")
	}
	if _, ok := serve(`not json`, nil).Available(context.Background()); ok {
		t.Fatal("garbage API answers must read as no update")
	}
	if _, ok := serve(``, fmt.Errorf("offline")).Available(context.Background()); ok {
		t.Fatal("API failure must read as no update")
	}
}

// fixtureZip builds a launcher zip in the given layout and returns its path.
func fixtureZip(t *testing.T, dir, layout, marker string) string {
	t.Helper()
	path := filepath.Join(dir, "launcher.zip")
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	zw := zip.NewWriter(f)
	name := "SmartBrain.exe"
	if layout == "bundle" {
		name = "SmartBrain.app/Contents/MacOS/SmartBrain"
	}
	w, err := zw.Create(name)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := w.Write([]byte(marker)); err != nil {
		t.Fatal(err)
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	f.Close()
	return path
}

// harness returns a ready Updater, the recorded relaunches, the install root, and
// the private key its release fixtures are signed with.
func harness(t *testing.T, layout string) (Updater, *[]string, string, string) {
	t.Helper()
	work := t.TempDir()
	// The "installed" launcher the swap will displace.
	root := filepath.Join(work, "SmartBrain.app")
	exeDir := root
	if layout == "bundle" {
		exeDir = filepath.Join(root, "Contents", "MacOS")
	}
	if err := os.MkdirAll(exeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	exeName := "SmartBrain"
	if layout == "flat" {
		exeName = "SmartBrain.exe"
	}
	if err := os.WriteFile(filepath.Join(exeDir, exeName), []byte("OLD"), 0o755); err != nil {
		t.Fatal(err)
	}
	zipPath := fixtureZip(t, t.TempDir(), layout, "NEW")
	sum, err := sha256File(zipPath)
	if err != nil {
		t.Fatal(err)
	}
	started := &[]string{}
	u := New("0.8.3")
	u.AppRoot = root
	u.Asset = "launcher.zip"
	u.Layout = layout
	u.Exe = "SmartBrain.exe" // pinned like its siblings, so the fixtures hold on any GOOS
	// A throwaway release key per test: the harness signs exactly what a release
	// workflow signs, so the whole authenticity path runs rather than being stubbed.
	pub, priv, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	u.PubKey = pub
	u.Fetch = func(_ context.Context, url, dest string) error {
		b, err := os.ReadFile(zipPath)
		if err != nil {
			return err
		}
		return os.WriteFile(dest, b, 0o755)
	}
	u.FetchBody = func(_ context.Context, url string) ([]byte, error) {
		return releaseBody(t, url, sum+"  launcher.zip\n", priv)
	}
	u.Start = func(path string) error { *started = append(*started, path); return nil }
	return u, started, root, priv
}

// releaseBody answers the three URLs Apply fetches, signing the sidecar with priv.
func releaseBody(t *testing.T, url, sidecar, priv string) ([]byte, error) {
	t.Helper()
	switch {
	case strings.HasSuffix(url, ".sha256.minisig"):
		sig, err := SignDetached(priv, []byte(sidecar), "launcher.zip")
		if err != nil {
			t.Fatal(err)
		}
		return []byte(sig), nil
	case strings.HasSuffix(url, ".sha256"):
		return []byte(sidecar), nil
	}
	return []byte(`{"tag_name":"v9.9.9"}`), nil
}

func TestApplySwapsKeepsBackupAndRelaunches(t *testing.T) {
	for _, layout := range []string{"bundle", "flat"} { // both product shapes, on every OS
		t.Run(layout, func(t *testing.T) { applySwapCase(t, layout) })
	}
}

func applySwapCase(t *testing.T, layout string) {
	u, started, root, _ := harness(t, layout)
	newExe, err := u.Apply(context.Background(), "9.9.9")
	if err != nil {
		t.Fatal(err)
	}
	// The new install answers at the old path; the displaced one is the backup.
	var installedExe string
	if layout == "bundle" {
		installedExe = filepath.Join(root, "Contents", "MacOS", "SmartBrain")
	} else {
		installedExe = filepath.Join(root, "SmartBrain.exe")
	}
	body, err := os.ReadFile(installedExe)
	if err != nil || string(body) != "NEW" {
		t.Fatalf("swap did not install the new binary: %q %v", body, err)
	}
	backupMarker := root + ".previous"
	if _, err := os.Stat(backupMarker); err != nil {
		t.Fatal("the displaced install must remain as a backup")
	}
	if len(*started) != 1 || (*started)[0] != newExe || newExe != installedExe {
		t.Fatalf("replacement must be started at its installed path; got %v / %q", *started, newExe)
	}
}

func TestApplyRefusesTamperedZipAndTouchesNothing(t *testing.T) {
	u, started, root, _ := harness(t, "flat")
	// Tamper the PAYLOAD, leaving the signed sidecar untouched — the case where an
	// attacker can swap the zip but cannot forge the signature over its checksum.
	u.Fetch = func(_ context.Context, _, dest string) error {
		return os.WriteFile(dest, []byte("not the release you signed"), 0o600)
	}
	if _, err := u.Apply(context.Background(), "9.9.9"); err == nil ||
		!strings.Contains(err.Error(), "checksum mismatch") {
		t.Fatalf("tampered zip must be refused, got: %v", err)
	}
	installedExe := filepath.Join(root, "SmartBrain.exe")
	body, _ := os.ReadFile(installedExe)
	if string(body) != "OLD" {
		t.Fatal("a refused update must leave the running install untouched")
	}
	if len(*started) != 0 {
		t.Fatal("a refused update must not relaunch anything")
	}
}

func TestApplyRefusesZipMissingTheApp(t *testing.T) {
	u, _, root, priv := harness(t, "flat")
	empty := fixtureZip(t, t.TempDir(), "flat", "x")
	// Rebuild the fixture as an EMPTY zip (no expected member).
	f, err := os.Create(empty)
	if err != nil {
		t.Fatal(err)
	}
	zw := zip.NewWriter(f)
	if _, err := zw.Create("README.txt"); err != nil {
		t.Fatal(err)
	}
	zw.Close()
	f.Close()
	sum, _ := sha256File(empty)
	u.Fetch = func(_ context.Context, _, dest string) error {
		b, _ := os.ReadFile(empty)
		return os.WriteFile(dest, b, 0o755)
	}
	u.FetchBody = func(_ context.Context, url string) ([]byte, error) {
		return releaseBody(t, url, sum+"  launcher.zip\n", priv)
	}
	if _, err := u.Apply(context.Background(), "9.9.9"); err == nil ||
		!strings.Contains(err.Error(), "archive missing") {
		t.Fatalf("a zip without the app must be refused, got: %v", err)
	}
	if _, err := os.Stat(root); err != nil {
		t.Fatal("the running install must still be in place")
	}
}

// Every OS's release artifact name, checked from any OS. Linux ships a tarball
// (mode bits and symlinks survive; zips lose them), and only amd64 has one.
func TestAssetForEachOS(t *testing.T) {
	cases := []struct {
		goos, goarch, want string
		ok                 bool
	}{
		{"darwin", "arm64", "SmartBrain-macos.zip", true},
		{"darwin", "amd64", "SmartBrain-macos.zip", true},
		{"windows", "amd64", "SmartBrain-windows.zip", true},
		{"linux", "amd64", "SmartBrain-linux-x86_64.tar.gz", true},
		{"linux", "arm64", "", false}, // no artifact — an honest error, not a bad URL
		{"plan9", "amd64", "", false},
	}
	for _, c := range cases {
		got, err := assetFor(c.goos, c.goarch)
		if c.ok && (err != nil || got != c.want) {
			t.Fatalf("assetFor(%s, %s) = %q, %v; want %q", c.goos, c.goarch, got, err, c.want)
		}
		if !c.ok && err == nil {
			t.Fatalf("assetFor(%s, %s) must error", c.goos, c.goarch)
		}
	}
}

func TestExeNameForEachOS(t *testing.T) {
	cases := map[string]string{
		"windows": "SmartBrain.exe",
		"linux":   "smartbrain",
		"darwin":  "SmartBrain.exe", // bundle OSes never use flat in production; tests keep the historical name
	}
	for goos, want := range cases {
		if got := exeNameFor(goos); got != want {
			t.Fatalf("exeNameFor(%s) = %q, want %q", goos, got, want)
		}
	}
}

// The flat swap RENAMES the app root. Pointed at a shared bin directory, that
// would eat every other tool living there — refuse, loudly.
func TestRefuseSharedBinSwap(t *testing.T) {
	home := filepath.Join(string(filepath.Separator)+"home", "u")
	refuse := []struct{ layout, root string }{
		{"flat", filepath.Join(home, ".local", "bin")},
		{"flat", filepath.Join(string(filepath.Separator)+"opt", "tools", "bin")},
	}
	for _, c := range refuse {
		err := refuseSharedBinSwap(c.layout, c.root, home)
		if err == nil || !strings.Contains(err.Error(), "install-linux.sh") {
			t.Fatalf("refuseSharedBinSwap(%s, %s) must refuse and name install-linux.sh, got %v", c.layout, c.root, err)
		}
	}
	allow := []struct{ layout, root string }{
		{"flat", filepath.Join(home, ".local", "share", "smartbrain")}, // its own directory: the supported shape
		{"bundle", filepath.Join(home, "bin")},                         // bundle swaps a .app, never a bin dir
	}
	for _, c := range allow {
		if err := refuseSharedBinSwap(c.layout, c.root, home); err != nil {
			t.Fatalf("refuseSharedBinSwap(%s, %s) must allow, got %v", c.layout, c.root, err)
		}
	}
}

// fixtureTarGz builds a linux-style launcher tarball: the executable (0755), a
// plain file (0644), and a symlink placed BEFORE anything creates its parent
// directory — the entry ordering that failed live in the native package.
func fixtureTarGz(t *testing.T, dir, exe, marker string) string {
	t.Helper()
	path := filepath.Join(dir, "launcher.tar.gz")
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	gz := gzip.NewWriter(f)
	tw := tar.NewWriter(gz)
	write := func(hdr *tar.Header, body string) {
		t.Helper()
		hdr.Size = int64(len(body))
		if err := tw.WriteHeader(hdr); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	write(&tar.Header{Name: "libexec/" + exe + "-link", Typeflag: tar.TypeSymlink, Linkname: "../" + exe, Mode: 0o777}, "")
	write(&tar.Header{Name: exe, Typeflag: tar.TypeReg, Mode: 0o755}, marker)
	write(&tar.Header{Name: "README", Typeflag: tar.TypeReg, Mode: 0o644}, "hi")
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	f.Close()
	return path
}

// The linux artifact end to end: a .tar.gz asset routes to untarGz, the swap
// installs it, and what tar promised survives — the executable bit and the
// symlink whose parent directory no earlier entry created.
func TestApplyUnpacksTarball(t *testing.T) {
	work := t.TempDir()
	root := filepath.Join(work, "SmartBrain")
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "smartbrain"), []byte("OLD"), 0o755); err != nil {
		t.Fatal(err)
	}
	tarPath := fixtureTarGz(t, t.TempDir(), "smartbrain", "NEW")
	sum, err := sha256File(tarPath)
	if err != nil {
		t.Fatal(err)
	}
	started := []string{}
	u := New("0.8.3")
	u.AppRoot = root
	u.Asset = "SmartBrain-linux-x86_64.tar.gz"
	u.Layout = "flat"
	u.Exe = "smartbrain"
	pub, priv, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	u.PubKey = pub
	u.Fetch = func(_ context.Context, url, dest string) error {
		b, err := os.ReadFile(tarPath)
		if err != nil {
			return err
		}
		return os.WriteFile(dest, b, 0o600)
	}
	u.FetchBody = func(_ context.Context, url string) ([]byte, error) {
		return releaseBody(t, url, sum+"  SmartBrain-linux-x86_64.tar.gz\n", priv)
	}
	u.Start = func(path string) error { started = append(started, path); return nil }

	newExe, err := u.Apply(context.Background(), "9.9.9")
	if err != nil {
		t.Fatal(err)
	}
	installedExe := filepath.Join(root, "smartbrain")
	body, err := os.ReadFile(installedExe)
	if err != nil || string(body) != "NEW" {
		t.Fatalf("swap did not install the new binary: %q %v", body, err)
	}
	info, err := os.Stat(installedExe)
	if err != nil || info.Mode().Perm()&0o100 == 0 {
		t.Fatalf("the executable bit must survive the tarball: %v %v", info, err)
	}
	if info, err := os.Stat(filepath.Join(root, "README")); err != nil || info.Mode().Perm()&0o111 != 0 {
		t.Fatalf("a plain file must stay plain: %v %v", info, err)
	}
	link, err := os.Readlink(filepath.Join(root, "libexec", "smartbrain-link"))
	if err != nil || link != "../smartbrain" {
		t.Fatalf("the early-ordered symlink must survive: %q %v", link, err)
	}
	if _, err := os.Stat(root + ".previous"); err != nil {
		t.Fatal("the displaced install must remain as a backup")
	}
	if len(started) != 1 || started[0] != newExe || newExe != installedExe {
		t.Fatalf("replacement must be started at its installed path; got %v / %q", started, newExe)
	}
}
