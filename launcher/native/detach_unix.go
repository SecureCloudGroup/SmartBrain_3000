//go:build !windows

package native

import "syscall"

// detachAttr puts a child in its own session so it survives the launcher quitting
// or its terminal's Ctrl-C — the native parity for `restart: unless-stopped`'s
// "containers outlive the launcher" behavior.
func detachAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setsid: true}
}
