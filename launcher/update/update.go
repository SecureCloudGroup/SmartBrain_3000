// Package update lets the launcher update ITS OWN binary — the one artifact that
// never updated itself. The app image always self-updated (pull + up); the launcher
// required a package-manager command, which meant compose changes and native mode
// only reached users who happened to run `brew upgrade`. From now on the launcher
// rides the same channel as everything else we ship: check our GitHub release,
// download the platform zip, VERIFY its sha256 sidecar, swap atomically with the
// previous version kept as backup, and relaunch. A Go HTTP download sets no
// quarantine/Mark-of-the-Web, so the swap hits no Gatekeeper/SmartScreen wall —
// the same property the whole no-signing-cert distribution rests on.
//
// Trust model: the zips and their checksums come from OUR release under TLS — the
// same trust root as the app image and the wheelhouse. A "dev" build never
// self-updates (fail-closed for local builds and tests).
//
// Package-manager bookkeeping stays cosmetically behind (brew/scoop still list the
// version they installed); their upgrade commands keep working and simply fast-
// forward the record. Documented, accepted.
package update

import (
	"archive/zip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

const (
	releaseAPI  = "https://api.github.com/repos/SecureCloudGroup/SmartBrain_3000/releases/latest"
	releaseBase = "https://github.com/SecureCloudGroup/SmartBrain_3000/releases/download"

	// Ceilings on what a download may consume before the checksum ever runs. The
	// endpoint is trusted-by-TLS, not trusted-by-signature, so a hostile or
	// misbehaving response must not be able to exhaust memory or the disk: metadata
	// is the release JSON + a 64-char sidecar, and the payload is one launcher zip
	// (tens of MB). Both are orders of magnitude under these bounds.
	maxMetaBytes    = 8 << 20   // release JSON / sha256 sidecar
	maxPayloadBytes = 256 << 20 // the platform zip
)

// Updater self-updates one installed launcher. Side effects are injectable so the
// swap logic tests hermetically (the native-package discipline).
type Updater struct {
	Version string // this binary's baked version; "dev" (or empty) never updates

	Fetch     func(ctx context.Context, url, dest string) error // download url -> dest
	FetchBody func(ctx context.Context, url string) ([]byte, error)
	Start     func(path string) error // launch the replacement binary, detached
	AppRoot   string                  // install root; resolved from the executable when empty
	Asset     string                  // release zip name; resolved from GOOS when empty
	Layout    string                  // "bundle" (.app) or "flat" (exe); resolved from GOOS when empty
	PubKey    string                  // release signing key; the compiled-in one when empty
}

// New returns a production Updater for this binary.
func New(version string) Updater {
	return Updater{Version: version, Fetch: fetchURL, FetchBody: fetchBody, Start: startDetached}
}

// assetName is the per-OS launcher zip our releases have always attached. The
// Asset/Layout fields exist so the swap logic itself tests on any OS (linux CI has
// no launcher artifact; production there stays an honest error).
func (u Updater) assetName() (string, error) {
	if u.Asset != "" {
		return u.Asset, nil
	}
	switch runtime.GOOS {
	case "darwin":
		return "SmartBrain-macos.zip", nil
	case "windows":
		return "SmartBrain-windows.zip", nil
	}
	return "", fmt.Errorf("no launcher artifact for %s", runtime.GOOS)
}

func (u Updater) layout() string {
	if u.Layout != "" {
		return u.Layout
	}
	if runtime.GOOS == "darwin" {
		return "bundle"
	}
	return "flat"
}

// appRoot locates what gets swapped: the .app bundle on macOS, the exe's directory
// on Windows.
func (u Updater) appRoot() (string, error) {
	if u.AppRoot != "" {
		return u.AppRoot, nil
	}
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	exe, err = filepath.EvalSymlinks(exe)
	if err != nil {
		return "", err
	}
	if runtime.GOOS == "darwin" {
		// …/SmartBrain.app/Contents/MacOS/SmartBrain -> the .app bundle
		return filepath.Dir(filepath.Dir(filepath.Dir(exe))), nil
	}
	return filepath.Dir(exe), nil
}

// Latest returns the newest released version ("1.2.3"). Fail-closed: API trouble
// or an unparseable tag reads as "no release". Deliberately NOT gated on Version —
// callers use it for release artifacts beyond the launcher binary (the native app
// assembly updates against it even from a dev-built launcher).
func (u Updater) Latest(ctx context.Context) (string, bool) {
	body, err := u.FetchBody(ctx, releaseAPI)
	if err != nil {
		return "", false
	}
	var payload struct {
		Tag string `json:"tag_name"`
	}
	if json.Unmarshal(body, &payload) != nil {
		return "", false
	}
	latest := strings.TrimPrefix(strings.TrimSpace(payload.Tag), "v")
	if _, ok := parts(latest); !ok {
		return "", false
	}
	return latest, true
}

// Available reports the newest released version when it is strictly newer than this
// binary. Fail-closed everywhere: dev builds, unparseable versions, and API trouble
// all mean "no update" — the launcher must never brick itself on a bad answer.
func (u Updater) Available(ctx context.Context) (string, bool) {
	if u.Version == "" || u.Version == "dev" {
		return "", false
	}
	latest, ok := u.Latest(ctx)
	if !ok || !Newer(latest, u.Version) {
		return "", false
	}
	return latest, true
}

// Newer is a strict semver-ish compare; anything unparseable reads as NOT newer.
func Newer(candidate, current string) bool {
	ca, ok1 := parts(candidate)
	cu, ok2 := parts(current)
	if !ok1 || !ok2 {
		return false
	}
	for i := 0; i < 3; i++ { // fixed bound: major.minor.patch
		if ca[i] != cu[i] {
			return ca[i] > cu[i]
		}
	}
	return false
}

func parts(v string) ([3]int, bool) {
	var out [3]int
	fields := strings.SplitN(strings.TrimSpace(v), ".", 3)
	if len(fields) != 3 {
		return out, false
	}
	for i, f := range fields {
		n, err := strconv.Atoi(strings.TrimSpace(f))
		if err != nil || n < 0 {
			return out, false
		}
		out[i] = n
	}
	return out, true
}

// Apply downloads, verifies, swaps, and starts the replacement. On ANY failure the
// running install is untouched (staging is separate; the swap is the last step, and
// the displaced version is kept as a one-generation backup beside the install).
// Returns the path of the started replacement so the caller can exit gracefully.
func (u Updater) Apply(ctx context.Context, version string) (string, error) {
	asset, err := u.assetName()
	if err != nil {
		return "", err
	}
	root, err := u.appRoot()
	if err != nil {
		return "", fmt.Errorf("locate install: %w", err)
	}
	staging, err := os.MkdirTemp(filepath.Dir(root), ".smartbrain-update-")
	if err != nil {
		return "", fmt.Errorf("staging: %w", err)
	}
	defer os.RemoveAll(staging)

	zipPath := filepath.Join(staging, asset)
	base := fmt.Sprintf("%s/v%s/", releaseBase, version)
	// Establish that the checksum is OURS before spending bandwidth on the payload
	// it describes: the sidecar comes from the same release as the zip, so on its own
	// it proves only that the two agree — which an attacker who can publish both can
	// arrange. The signature is what makes it evidence.
	sumRaw, err := u.FetchBody(ctx, base+asset+".sha256")
	if err != nil {
		return "", fmt.Errorf("checksum: %w", err)
	}
	sigRaw, err := u.FetchBody(ctx, base+asset+".sha256.minisig")
	if err != nil {
		return "", fmt.Errorf("signature: %w", err)
	}
	if err := verifySignature(sumRaw, string(sigRaw), u.publicKey()); err != nil {
		return "", fmt.Errorf("signature: %w", err)
	}
	want := strings.Fields(strings.TrimSpace(string(sumRaw)))
	if len(want) == 0 || len(want[0]) != 64 {
		return "", fmt.Errorf("checksum: malformed sidecar")
	}
	if err := u.Fetch(ctx, base+asset, zipPath); err != nil {
		return "", fmt.Errorf("download: %w", err)
	}
	got, err := sha256File(zipPath)
	if err != nil {
		return "", err
	}
	if got != want[0] {
		return "", fmt.Errorf("checksum mismatch: got %s want %s", got, want[0])
	}
	unpacked := filepath.Join(staging, "unpacked")
	if err := unzip(zipPath, unpacked); err != nil {
		return "", fmt.Errorf("unpack: %w", err)
	}
	fresh, newExe, err := freshInstall(u.layout(), unpacked, root)
	if err != nil {
		return "", err
	}
	// The swap: displace the running install (renaming a live bundle/exe is allowed on
	// both OSes — open files follow their inodes), move the new one in, keep ONE backup.
	backup := root + ".previous"
	_ = os.RemoveAll(backup)
	if err := os.Rename(root, backup); err != nil {
		return "", fmt.Errorf("swap: displace current: %w", err)
	}
	if err := os.Rename(fresh, root); err != nil {
		_ = os.Rename(backup, root) // restore — the update simply didn't happen
		return "", fmt.Errorf("swap: install new: %w", err)
	}
	if err := u.Start(newExe); err != nil {
		return "", fmt.Errorf("relaunch: %w (installed; will run on next start)", err)
	}
	return newExe, nil
}

// freshInstall finds what the zip provided and where its executable will live once
// the swap puts it at root's path.
func freshInstall(layout, unpacked, root string) (dir string, exeAfterSwap string, err error) {
	if layout == "bundle" {
		fresh := filepath.Join(unpacked, "SmartBrain.app")
		if _, err := os.Stat(fresh); err != nil {
			return "", "", fmt.Errorf("zip missing SmartBrain.app")
		}
		return fresh, filepath.Join(root, "Contents", "MacOS", "SmartBrain"), nil
	}
	exe := filepath.Join(unpacked, "SmartBrain.exe")
	if _, err := os.Stat(exe); err != nil {
		return "", "", fmt.Errorf("zip missing SmartBrain.exe")
	}
	// Windows swaps the whole directory shape the same way: the unpacked dir becomes root.
	return unpacked, filepath.Join(root, "SmartBrain.exe"), nil
}

// --- side-effect defaults ----------------------------------------------------

// fetchURL streams the response straight to dest, never holding the whole payload
// in memory, and stops at maxPayloadBytes. dest is a zip we unpack — never executed
// in place — so it needs no execute bit.
func fetchURL(ctx context.Context, url, dest string) error {
	resp, err := get(ctx, url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	f, err := os.OpenFile(dest, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	// No early return between here and Close, so the close error is reported rather
	// than discarded by a defer — a short write must not look like a good download.
	written, copyErr := io.Copy(f, io.LimitReader(resp.Body, maxPayloadBytes+1))
	closeErr := f.Close()
	if copyErr != nil {
		return copyErr
	}
	if closeErr != nil {
		return closeErr
	}
	if written > maxPayloadBytes {
		return fmt.Errorf("GET %s: response exceeds %d bytes", url, maxPayloadBytes)
	}
	return nil
}

func fetchBody(ctx context.Context, url string) ([]byte, error) {
	resp, err := get(ctx, url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxMetaBytes+1))
	if err != nil {
		return nil, err
	}
	if len(body) > maxMetaBytes {
		return nil, fmt.Errorf("GET %s: response exceeds %d bytes", url, maxMetaBytes)
	}
	return body, nil
}

// get issues the request and rejects any non-200 before the caller reads a byte.
func get(ctx context.Context, url string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("GET %s: %s", url, resp.Status)
	}
	return resp, nil
}

func startDetached(path string) error {
	cmd := exec.Command(path)
	cmd.SysProcAttr = detachAttr()
	return cmd.Start()
}

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

// unzip mirrors the native package's helper (kept local: these two packages must
// not depend on each other just to share 40 lines). Refuses path escapes.
func unzip(archive, dest string) error {
	r, err := zip.OpenReader(archive)
	if err != nil {
		return err
	}
	defer r.Close()
	for _, zf := range r.File { // bounded by archive contents
		target := filepath.Join(dest, filepath.FromSlash(zf.Name))
		rel, err := filepath.Rel(dest, target)
		if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
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
		mode := zf.Mode() & 0o777
		if mode == 0 {
			mode = 0o644
		}
		out, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
		if err != nil {
			in.Close()
			return err
		}
		if _, err := io.Copy(out, in); err != nil { //nolint:gosec // checksum-verified archive
			in.Close()
			out.Close()
			return err
		}
		in.Close()
		out.Close()
	}
	return nil
}
