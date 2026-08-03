// Hermetic self-update tests: injected fetchers, temp install roots, recorded
// relaunches — no network, no real swap of anything living.
package update

import (
	"archive/zip"
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
		!strings.Contains(err.Error(), "zip missing") {
		t.Fatalf("a zip without the app must be refused, got: %v", err)
	}
	if _, err := os.Stat(root); err != nil {
		t.Fatal("the running install must still be in place")
	}
}
