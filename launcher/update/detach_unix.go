//go:build !windows

package update

import "syscall"

// detachAttr: the replacement must outlive this (exiting) process — own session.
func detachAttr() *syscall.SysProcAttr { return &syscall.SysProcAttr{Setsid: true} }
