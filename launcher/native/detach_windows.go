//go:build windows

package native

import "syscall"

// detachAttr detaches the child from the launcher's console/process group —
// see detach_unix.go for the why.
func detachAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP}
}
