package stack

import (
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestComposeArgs(t *testing.T) {
	s := Stack{Dir: "/tmp/sb", Port: DefaultPort}
	got := s.composeArgs("up", "-d")
	want := []string{"compose", "-f", filepath.Join("/tmp/sb", composeName), "up", "-d"}
	if len(got) != len(want) {
		t.Fatalf("arg count: got %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("arg %d: got %q, want %q", i, got[i], want[i])
		}
	}
}

func TestComposePathAndURL(t *testing.T) {
	s := Stack{Dir: "/opt/data", Port: 33000}
	if got := s.ComposePath(); got != "/opt/data/docker-compose.release.yml" {
		t.Errorf("ComposePath = %q", got)
	}
	if got := s.URL(); got != "http://localhost:33000" {
		t.Errorf("URL = %q", got)
	}
}

// The launcher must point ./data at a stable per-user location, not the CWD it happened to launch
// from — otherwise a user's knowledge would scatter across wherever they double-clicked.
func TestNewRootsUnderAppData(t *testing.T) {
	s, err := New()
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if !strings.HasSuffix(s.Dir, "SmartBrain") {
		t.Errorf("Dir should end in SmartBrain, got %q", s.Dir)
	}
	if !filepath.IsAbs(s.Dir) {
		t.Errorf("Dir should be absolute, got %q", s.Dir)
	}
	if s.Port != DefaultPort {
		t.Errorf("Port = %d, want %d", s.Port, DefaultPort)
	}
}

func TestOpenArgs(t *testing.T) {
	got := openArgs("http://localhost:33000")
	if len(got) == 0 {
		t.Fatal("openArgs returned nothing")
	}
	// The URL must be the final argument on every platform, or we'd open the wrong thing.
	if got[len(got)-1] != "http://localhost:33000" {
		t.Errorf("URL not last arg: %v", got)
	}
	switch runtime.GOOS {
	case "darwin":
		if got[0] != "open" {
			t.Errorf("darwin should use open, got %v", got)
		}
	case "windows":
		if got[0] != "rundll32" {
			t.Errorf("windows should use rundll32, got %v", got)
		}
	default:
		if got[0] != "xdg-open" {
			t.Errorf("linux should use xdg-open, got %v", got)
		}
	}
}

func TestStartDockerArgs(t *testing.T) {
	// Only darwin/windows have a desktop app to launch; elsewhere it must be nil so the caller
	// doesn't try to "start Docker" on a daemon-managed system.
	got := startDockerArgs()
	switch runtime.GOOS {
	case "darwin", "windows":
		if got == nil {
			t.Errorf("%s should have a start command", runtime.GOOS)
		}
	default:
		if got != nil {
			t.Errorf("%s should have no start command, got %v", runtime.GOOS, got)
		}
	}
}
