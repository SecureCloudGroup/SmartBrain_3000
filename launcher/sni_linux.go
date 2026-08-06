//go:build linux

package main

import "github.com/godbus/dbus/v5"

// sniAvailable reports whether a StatusNotifierItem host (the freedesktop tray) is
// on the session bus. systray NEVER surfaces this itself: its SNI registration
// failure is log-only and onReady fires regardless, which on a stock GNOME (no
// AppIndicator extension) means a launcher that "runs" with no visible tray at
// all. Probing the watcher name first lets main pick the headless persona and SAY
// so instead.
func sniAvailable() bool {
	conn, err := dbus.SessionBus() // a shared connection — never Close it
	if err != nil {
		return false
	}
	var has bool
	if err := conn.BusObject().Call("org.freedesktop.DBus.NameHasOwner", 0,
		"org.kde.StatusNotifierWatcher").Store(&has); err != nil {
		return false
	}
	return has
}

// sessionBusReachable reports whether a session bus exists at all — without one
// there is no SNI and no tray, whatever DISPLAY says.
func sessionBusReachable() bool {
	_, err := dbus.SessionBus()
	return err == nil
}
