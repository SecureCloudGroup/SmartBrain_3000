// Package stack is the launcher's Docker orchestration — everything the tray menu does under the
// hood, with NO dependency on the systray/GUI layer. Keeping it separate is what lets it be unit
// tested on any platform: the GUI glue in main.go needs CGO and a real desktop, this does not.
//
// It is a thin, honest wrapper over `docker compose`. The launcher deliberately owns no state the
// user can't see: it writes one compose file into a per-user directory and shells out to Docker
// exactly as a person typing the commands would. Nothing here is hidden or magic.
package stack

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const (
	// The app is loopback-only on this fixed port (see docker-compose.release.yml). The launcher
	// talks to the same port the browser will.
	DefaultPort = 33000
	composeName = "docker-compose.release.yml"
)

// Stack points at one installed SmartBrain: the directory holding its compose file and ./data.
type Stack struct {
	Dir  string // per-user app-data dir; the compose file and ./data live here
	Port int    // host port the app serves on (loopback)
}

// New returns a Stack rooted at the per-user application-data directory. That is a stable location
// the user owns, so their knowledge (./data beside the compose file) survives launcher upgrades and
// is easy to find and back up.
func New() (Stack, error) {
	base, err := os.UserConfigDir()
	if err != nil {
		return Stack{}, fmt.Errorf("locate app-data dir: %w", err)
	}
	dir := filepath.Join(base, "SmartBrain")
	return Stack{Dir: dir, Port: DefaultPort}, nil
}

// ComposePath is where the stack's compose file lives.
func (s Stack) ComposePath() string { return filepath.Join(s.Dir, composeName) }

// URL is where the app is reachable in a browser.
func (s Stack) URL() string { return fmt.Sprintf("http://localhost:%d", s.Port) }

// Install writes the compose file (and creates ./data) so `docker compose` has something to run.
// It is idempotent and always rewrites the file, so a launcher upgrade that ships a newer compose
// definition takes effect on next start. The user's data in ./data is never touched.
func (s Stack) Install(compose []byte) error {
	if err := os.MkdirAll(filepath.Join(s.Dir, "data"), 0o700); err != nil {
		return fmt.Errorf("create data dir: %w", err)
	}
	if err := os.WriteFile(s.ComposePath(), compose, 0o600); err != nil {
		return fmt.Errorf("write compose file: %w", err)
	}
	return nil
}

// composeArgs builds the argument vector for `docker compose -f <file> <args...>`. Factored out so
// the exact command the launcher runs is unit-testable — no surprises about what gets executed.
func (s Stack) composeArgs(args ...string) []string {
	return append([]string{"compose", "-f", s.ComposePath()}, args...)
}

func (s Stack) compose(ctx context.Context, args ...string) error {
	cmd := exec.CommandContext(ctx, "docker", s.composeArgs(args...)...)
	cmd.Dir = s.Dir // relative ./data in the compose file resolves under the app-data dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("docker %v: %w: %s", args, err, out)
	}
	return nil
}

// Up pulls the latest images and starts the stack in the background.
func (s Stack) Up(ctx context.Context) error { return s.compose(ctx, "up", "-d") }

// Down stops and removes the containers. The user's ./data is left untouched.
func (s Stack) Down(ctx context.Context) error { return s.compose(ctx, "down") }

// Healthy reports whether the app is answering. It is the launcher's single source of truth for
// "is it up?" — the same check the browser would succeed or fail at.
func (s Stack) Healthy(ctx context.Context) bool {
	url := fmt.Sprintf("http://127.0.0.1:%d/api/health", s.Port)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return false
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// WaitHealthy polls until the app is up or the deadline passes. A first `up` pulls images, so the
// caller should allow generous time.
func (s Stack) WaitHealthy(ctx context.Context, deadline time.Duration) bool {
	ctx, cancel := context.WithTimeout(ctx, deadline)
	defer cancel()
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		if s.Healthy(ctx) {
			return true
		}
		select {
		case <-ctx.Done():
			return false
		case <-ticker.C:
		}
	}
}

// dockerPathDirs are the usual places the docker CLI lives on macOS. `home` is $HOME.
func dockerPathDirs(home string) []string {
	return []string{
		"/usr/local/bin",                                  // Docker Desktop's `docker` symlink; Intel Homebrew
		"/opt/homebrew/bin",                               // Apple Silicon Homebrew (docker, colima)
		filepath.Join(home, ".docker/bin"),                // newer Docker Desktop
		filepath.Join(home, ".orbstack/bin"),              // OrbStack
		"/Applications/Docker.app/Contents/Resources/bin", // Docker Desktop's bundled CLI
	}
}

// missingPathDirs returns which of dirs are not already present in the colon/semicolon-separated
// pathEnv, preserving order. Pure, so it's unit-testable without touching the process environment.
func missingPathDirs(pathEnv string, dirs []string) []string {
	seen := map[string]bool{}
	for _, d := range strings.Split(pathEnv, string(os.PathListSeparator)) {
		if d != "" {
			seen[d] = true
		}
	}
	var add []string
	for _, d := range dirs {
		if d != "" && !seen[d] {
			add = append(add, d)
		}
	}
	return add
}

// EnsureDockerPath makes `docker` findable from a GUI-launched app. On macOS, a .app opened from the
// Finder inherits launchd's minimal PATH (roughly /usr/bin:/bin:/usr/sbin:/sbin), which EXCLUDES
// /usr/local/bin and Homebrew — so `exec.LookPath("docker")` fails and Docker looks "not installed"
// even when Docker Desktop is right there. Prepending the usual locations fixes that for every
// subsequent docker/compose call in this process. No-op off macOS, where GUI apps inherit a full PATH.
func EnsureDockerPath() {
	if runtime.GOOS != "darwin" {
		return
	}
	add := missingPathDirs(os.Getenv("PATH"), dockerPathDirs(os.Getenv("HOME")))
	if len(add) == 0 {
		return
	}
	sep := string(os.PathListSeparator)
	os.Setenv("PATH", strings.Join(add, sep)+sep+os.Getenv("PATH"))
}

// DockerInstalled reports whether the docker CLI is on PATH. Call EnsureDockerPath first.
func DockerInstalled() bool {
	_, err := exec.LookPath("docker")
	return err == nil
}

// DockerRunning reports whether the daemon is actually up (installed-but-not-started is the common
// case on Desktop). `docker info` succeeds only when the daemon answers.
func DockerRunning(ctx context.Context) bool {
	if !DockerInstalled() {
		return false
	}
	return exec.CommandContext(ctx, "docker", "info").Run() == nil
}

// startDockerArgs is the platform command to launch Docker Desktop, or nil where there's nothing to
// launch (Linux uses a daemon/systemd, not a desktop app). Separated for testability.
func startDockerArgs() []string {
	switch runtime.GOOS {
	case "darwin":
		return []string{"open", "-a", "Docker"}
	case "windows":
		return []string{"cmd", "/c", "start", "", "Docker Desktop"}
	default:
		return nil
	}
}

// TryStartDocker makes a best effort to launch Docker Desktop. It returns whether it started
// anything; the caller still waits for DockerRunning to go true.
func TryStartDocker(ctx context.Context) bool {
	args := startDockerArgs()
	if args == nil {
		return false
	}
	return exec.CommandContext(ctx, args[0], args[1:]...).Start() == nil
}

// openArgs is the platform command to open a URL in the default browser. Separated for testability.
func openArgs(url string) []string {
	switch runtime.GOOS {
	case "darwin":
		return []string{"open", url}
	case "windows":
		return []string{"rundll32", "url.dll,FileProtocolHandler", url}
	default:
		return []string{"xdg-open", url}
	}
}

// OpenBrowser opens url in the user's default browser.
func OpenBrowser(url string) error {
	args := openArgs(url)
	return exec.Command(args[0], args[1:]...).Start()
}
