package stack

import (
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
)

func TestMissingPathDirs(t *testing.T) {
	sep := string(os.PathListSeparator)
	// /usr/local/bin already present → not returned; the others returned, in order.
	pathEnv := "/usr/bin" + sep + "/bin" + sep + "/usr/local/bin"
	got := missingPathDirs(pathEnv, []string{"/usr/local/bin", "/opt/homebrew/bin", "/h/.docker/bin"})
	want := []string{"/opt/homebrew/bin", "/h/.docker/bin"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("missingPathDirs = %v, want %v", got, want)
	}
	if got := missingPathDirs("/a"+sep+"/b", []string{"/a", "/b"}); len(got) != 0 {
		t.Errorf("all present should return none, got %v", got)
	}
}

func TestDockerPathDirs(t *testing.T) {
	dirs := dockerPathDirs("/Users/x")
	joined := strings.Join(dirs, ":")
	// /usr/local/bin (Docker Desktop's symlink) is the #1 dir a GUI PATH omits — it must be here.
	if !strings.Contains(joined, "/usr/local/bin") {
		t.Errorf("must include /usr/local/bin, got %v", dirs)
	}
	// HOME-relative entries must be expanded against the given home.
	if !strings.Contains(joined, "/Users/x/.docker/bin") {
		t.Errorf("~/.docker/bin should expand under home, got %v", dirs)
	}
}

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

func TestComposeArgsPull(t *testing.T) {
	// Up() pulls before up -d so an upgraded launcher fetches a moved :latest instead of reusing a
	// cached image; pin the pull invocation's args.
	s := Stack{Dir: "/tmp/sb", Port: DefaultPort}
	got := s.composeArgs("pull")
	want := []string{"compose", "-f", filepath.Join("/tmp/sb", composeName), "pull"}
	if len(got) != len(want) {
		t.Fatalf("arg count: got %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("arg %d: got %q, want %q", i, got[i], want[i])
		}
	}
}

func TestVersionFromEnv(t *testing.T) {
	cases := []struct {
		name string
		env  []string
		want string
	}{
		{"present among others", []string{"PATH=/bin", "SMARTBRAIN_VERSION=0.5.2", "TZ=UTC"}, "0.5.2"},
		{"absent (pre-stamp image)", []string{"PATH=/bin", "TZ=UTC"}, ""},
		{"empty value", []string{"SMARTBRAIN_VERSION="}, ""},
		{"nil env", nil, ""},
	}
	for _, c := range cases {
		if got := versionFromEnv(c.env); got != c.want {
			t.Errorf("%s: versionFromEnv = %q, want %q", c.name, got, c.want)
		}
	}
}

func TestUpdateReadyFromIDs(t *testing.T) {
	cases := []struct {
		running, latest string
		want            bool
	}{
		{"sha256:aaa", "sha256:bbb", true},  // differ -> an update is staged
		{"sha256:aaa", "sha256:aaa", false}, // identical -> already latest
		{"", "sha256:bbb", false},           // unknown running -> can't tell, never claim an update
		{"sha256:aaa", "", false},           // unknown latest -> ditto
		{"", "", false},
	}
	for _, c := range cases {
		if got := updateReadyFromIDs(c.running, c.latest); got != c.want {
			t.Errorf("updateReadyFromIDs(%q,%q) = %v, want %v", c.running, c.latest, got, c.want)
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
