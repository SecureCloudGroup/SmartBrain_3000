// Command sbsign generates the release signing keypair and signs release
// checksum files with it.
//
// It exists so the release workflow needs exactly one secret — a base64 private
// key — rather than a scrypt-wrapped minisign key file plus a password piped into
// an interactive prompt. What it writes is minisign's format, so a release stays
// verifiable with the stock tool by anyone who does not want to trust ours:
//
//	minisign -Vm SmartBrain-macos.zip.sha256 -p minisign.pub
//
// Usage:
//
//	sbsign gen                       # print a new keypair; the private half is a secret
//	sbsign sign FILE [FILE...]       # write FILE.minisig beside each FILE
//	sbsign pubkey                    # print the public key for the configured private key
//
// The private key is read from SMARTBRAIN_SIGNING_KEY. It is never echoed, never
// written to disk, and must not be passed as an argument, where it would land in
// the process list and shell history.
package main

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/SecureCloudGroup/SmartBrain_3000/launcher/update"
)

const keyEnv = "SMARTBRAIN_SIGNING_KEY"

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "gen":
		gen()
	case "pubkey":
		pubkey()
	case "sign":
		if len(os.Args) < 3 {
			usage()
		}
		sign(os.Args[2:])
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: sbsign gen | pubkey | sign FILE [FILE...]")
	os.Exit(2)
}

func gen() {
	pub, priv, err := update.GenerateKey()
	if err != nil {
		fail(err)
	}
	// Printed once, to a human, deliberately: there is nowhere safer for this
	// program to put it, and writing a private key to disk invites it into a commit.
	fmt.Println("public key (publish this; paste into releasePublicKey):")
	fmt.Println(pub)
	fmt.Println()
	fmt.Printf("private key (SECRET — store as the %s repository secret, nowhere else):\n", keyEnv)
	fmt.Println(priv)
}

func pubkey() {
	pub, err := update.PublicKeyFor(mustKey())
	if err != nil {
		fail(err)
	}
	fmt.Println(pub)
}

func sign(paths []string) {
	key := mustKey()
	for _, path := range paths {
		body, err := os.ReadFile(path)
		if err != nil {
			fail(err)
		}
		sig, err := update.SignDetached(key, body, filepath.Base(path))
		if err != nil {
			fail(err)
		}
		out := path + ".minisig"
		if err := os.WriteFile(out, []byte(sig), 0o644); err != nil {
			fail(err)
		}
		fmt.Println("signed", out)
	}
}

func mustKey() string {
	key := os.Getenv(keyEnv)
	if key == "" {
		fail(fmt.Errorf("%s is not set — refusing to sign without a key", keyEnv))
	}
	return key
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "sbsign:", err)
	os.Exit(1)
}
