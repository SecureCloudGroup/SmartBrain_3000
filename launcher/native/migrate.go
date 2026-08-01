// Docker -> native data migration (Docker-exit Phase 2b).
//
// Existing users' data lives in named Docker volumes; the native app reads a per-OS
// user directory. The move is a COPY, never a move: the volumes are left byte-for-byte
// untouched as the rollback — turning the flag off returns to Docker with nothing lost,
// and the app's URL localization keeps stored settings working in both worlds. The real
// verification of a migration is the native stack booting healthy on the copied data;
// this file only gets the bytes across and sanity-checks what landed.
//
// Scope honesty: this migrates the LAUNCHER-managed stack (compose project
// "smartbrain", the population the launcher can see). From-source installs already
// keep data on the host, and hand-rolled compose projects have arbitrary volume names
// — their documented path is the in-app encrypted backup/restore, which is tested
// end-to-end app-side.
package native

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

// ErrNoDataToMigrate means the Docker volume was there but empty — nothing to carry over.
//
// That is a completely ordinary situation, not a failure: `docker compose up` creates the
// volumes the first time the stack starts, long before anyone finishes first-run setup. So
// anyone who installs, lets it start once under Docker, and then switches to native has an
// empty volume waiting. Reported by a tester who did exactly that on a clean machine and
// got "database missing after copy" instead of an app.
var ErrNoDataToMigrate = errors.New("no SmartBrain data in the Docker volumes")

const (
	appVolume     = "smartbrain_smartbrain_data"
	bifrostVolume = "smartbrain_bifrost_data"
	// The copy runs inside the app image the user already has locally — no new pull,
	// and it certainly contains a shell + cp.
	copyImage = "ghcr.io/securecloudgroup/smartbrain_3000:latest"
)

// appDataDir mirrors the app's own native default (runtime.default_data_dir in Python):
// the two MUST agree, or the app would boot against an empty directory after migration.
func appDataDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	switch runtime.GOOS {
	case "darwin":
		return filepath.Join(home, "Library", "Application Support", "SmartBrain", "data"), nil
	case "windows":
		if appdata := os.Getenv("APPDATA"); appdata != "" {
			return filepath.Join(appdata, "SmartBrain", "data"), nil
		}
		return filepath.Join(home, "AppData", "Roaming", "SmartBrain", "data"), nil
	default:
		if xdg := os.Getenv("XDG_DATA_HOME"); xdg != "" {
			return filepath.Join(xdg, "smartbrain", "data"), nil
		}
		return filepath.Join(home, ".local", "share", "smartbrain", "data"), nil
	}
}

// NeedsMigration is true when there is Docker data to move and no native data yet.
// Both conditions matter: native data present means migration (or a fresh native
// start) already happened, and no volume means there is simply nothing to move.
func (n Native) NeedsMigration(ctx context.Context) bool {
	dataDir, err := appDataDir()
	if err != nil {
		return false
	}
	if _, err := os.Stat(filepath.Join(dataDir, "smartbrain.duckdb")); err == nil {
		return false // native data exists — never overwrite it with an old copy
	}
	// `docker volume inspect` exits non-zero for a missing volume; Run surfaces that.
	return n.Run(ctx, "docker", "volume", "inspect", appVolume) == nil
}

// MigrateFromDocker copies both volumes out. The caller must have STOPPED the Docker
// stack first (copying under a live writer risks a torn database). Fails loudly and
// leaves partial native data for inspection — the volumes are never modified, so no
// failure here can lose anything.
func (n Native) MigrateFromDocker(ctx context.Context) error {
	dataDir, err := appDataDir()
	if err != nil {
		return fmt.Errorf("migrate: locate native data dir: %w", err)
	}
	if err := os.MkdirAll(dataDir, 0o700); err != nil {
		return fmt.Errorf("migrate: create native data dir: %w", err)
	}
	if err := os.MkdirAll(n.bifrostData(), 0o700); err != nil {
		return fmt.Errorf("migrate: create gateway data dir: %w", err)
	}
	// cp -a inside the image: preserves modes; /from is the volume (read-only mount —
	// the rollback guarantee enforced by the mount itself, not by discipline).
	if err := n.Run(ctx, "docker", "run", "--rm",
		"-v", appVolume+":/from:ro", "-v", dataDir+":/to",
		"--entrypoint", "sh", copyImage, "-c", "cp -a /from/. /to/"); err != nil {
		return fmt.Errorf("migrate: copy app data: %w", err)
	}
	if err := n.Run(ctx, "docker", "run", "--rm",
		"-v", bifrostVolume+":/from:ro", "-v", n.bifrostData()+":/to",
		"--entrypoint", "sh", copyImage, "-c", "cp -a /from/. /to/"); err != nil {
		return fmt.Errorf("migrate: copy gateway data: %w", err)
	}
	// Sanity: the database landed and is not a stub. The REAL verification is the
	// native stack booting healthy on it (the caller's next step).
	//
	// A missing database HERE means the copies above succeeded and found nothing — an
	// empty volume, i.e. a Docker install that never completed first-run. That is not an
	// error; it is a fresh start. A copy that genuinely failed returned above, so this
	// branch cannot swallow one.
	db := filepath.Join(dataDir, "smartbrain.duckdb")
	info, err := os.Stat(db)
	if err != nil {
		return ErrNoDataToMigrate
	}
	if info.Size() < 4096 {
		return fmt.Errorf("migrate: database suspiciously small (%d bytes)", info.Size())
	}
	return nil
}

// DiscardMigratedData removes the copied native data after a FAILED takeover, so a
// retry re-copies fresh instead of silently booting a stale snapshot. (The first live
// migration failed after its copy; weeks later, NeedsMigration would have seen the
// old copy, skipped the fresh one, and resurrected that night's data.) The Docker
// volumes are, as ever, untouched — this only discards the native-side copy.
func (n Native) DiscardMigratedData() {
	if dataDir, err := appDataDir(); err == nil {
		_ = os.RemoveAll(dataDir)
	}
	_ = os.RemoveAll(n.bifrostData())
}
