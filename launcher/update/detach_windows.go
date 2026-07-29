//go:build windows

package update

import "syscall"

// detachAttr: see detach_unix.go.
func detachAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP}
}
