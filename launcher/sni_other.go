//go:build !linux

package main

// macOS and Windows always have a host for the tray icon; these stubs exist so
// main.go reads the same on every OS (and so the persona logic tests everywhere).
func sniAvailable() bool        { return true }
func sessionBusReachable() bool { return true }
