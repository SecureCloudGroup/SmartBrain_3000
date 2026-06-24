# Remote access (away from home)

By default SmartBrain_3000 runs only on your own computer. **Remote access** lets you
reach it from your phone — on Wi-Fi or cellular — without any router or port-forward
setup. It's **off by default**; you opt in by pairing a phone.

## How it works

Your **Desktop** is where you set everything up. To use SmartBrain on your phone, you
**pair** the phone once. After that, the phone reaches your Desktop over **WebRTC** — a
direct, **end-to-end-encrypted** connection (DTLS). When a direct link isn't possible,
traffic falls back to an encrypted **relay** that still can't read your data.

This needs a small **signaling node** running on a cheap public server (not your home
machine). Your Desktop dials **out** to it, so nothing on your home network is ever
exposed. The node only helps your phone find your Desktop — it is content-blind and
never sees your data. Your operator runs this node; if SmartBrain was set up for you,
remote access may already be configured.

## Pair your phone

![Settings → Remote access: name a phone and pair it](assets/06-remote-access.png)

There are two ways to pair, depending on how you open the app on the phone.

### Option A — Scan a QR code (phone browser / Safari)

1. On the **Desktop**, open **Settings → Remote access**.
2. Give the phone a name (e.g. "My phone") and tap **Pair a new phone**.
3. On the phone, **scan the QR code with the camera**. It opens the app from the node,
   so this works from anywhere with internet.
4. Tap **Pair this phone** to confirm. You're connected.
5. (Recommended) **Add to Home Screen** so you can reopen it with one tap.

The QR is shown **once** and carries a one-time credential — close it after pairing.

### Option B — Enter a 6-character code (installed Home-Screen app)

On iPhone/iPad, an app you've **added to the Home Screen** keeps its own storage,
separate from Safari. So the installed app **can't** inherit a QR you scanned in
Safari — it has to be paired on its own, with a code.

1. Open the **installed (Home Screen) app**. On the pairing screen, it asks for a code.
2. On the **Desktop**, open **Settings → Remote access**, name the device, and tap
   **Pair via code**. A **6-character code** appears.
3. In the installed app, type that code and tap **Pair**. Do this **within 5 minutes**
   and **on the same Wi-Fi as your Desktop**.

> In short: Safari and the installed Home-Screen app each pair **once, separately** —
> that's why the code path exists.

## Using it on your phone

The phone shows a **trimmed set** of areas meant for use on the go: **Chat**,
**Knowledge**, **Planner**, **Email**, and **Activity**. Settings and first-time setup
live on the **Desktop**.

A small **"Remote"** chip shows the connection state: **direct** (phone-to-Desktop),
**relayed** (through the encrypted relay), or **BLOCKED** in red if your Desktop's
identity can't be verified — re-pair if you reinstalled the app.

## Manage devices

Under **Settings → Remote access** you can pair more devices and **Revoke** any device
at any time. A revoked device can no longer connect.

## Security

- **Off by default.** Nothing is reachable until you pair a device.
- **End-to-end encrypted.** The connection is encrypted (DTLS); the signaling node and
  relay only ever see scrambled bytes, never your data.
- **Identity-checked.** Before sending anything, your phone verifies your Desktop's
  identity (a key pinned at pairing), so a compromised node can't impersonate it.
- **One-time credentials.** A QR or code carries a single-use pairing secret — close
  the QR after pairing, and don't share a code.

This changes *where you can reach the app from*, not what protects your data. See
[Privacy &amp; security](06-privacy-security.md).

## On your own Wi-Fi (LAN, HTTPS)

If you only want your phone to reach the Desktop **on the same Wi-Fi**, you don't need
the signaling node at all — you can serve the app over HTTPS on your local network. This
uses a local certificate so your phone trusts the connection.

1. **Make a local certificate** (uses [mkcert](https://github.com/FiloSottile/mkcert)),
   passing a name and your Desktop's LAN IP:

   ```sh
   python3 installer/install.py certs smartbrain.local 192.168.1.50
   ```

   It writes the cert to `data/certs/`, trusts the local CA on your computer, and prints
   the path to **`rootCA.pem`**.
2. **Trust the CA on your phone** — install that `rootCA.pem` (AirDrop/email it to
   yourself, then open it) so the phone trusts the local certificate.
3. **Allow your LAN address and bring it up over HTTPS.** Set
   `SMARTBRAIN_ALLOWED_HOSTS` to include your LAN IP/name in `compose/.env`, e.g.
   `SMARTBRAIN_ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.50,smartbrain.local`, then
   re-run `python3 installer/install.py install`. Once a cert exists the installer
   automatically serves HTTPS on your LAN.
4. **On the phone (same Wi-Fi)** open `https://192.168.1.50:33000`.

This path is **same-network only**. To reach the Desktop from cellular or another
network, use the WebRTC pairing above.

## Setup for operators (one-time)

Remote access needs the signaling node and the WebRTC overlay running. If you're setting
this up yourself:

1. **Run the signaling node** on a small public server (not your home machine):

   ```sh
   SIGNALING_TOKEN=<a-secret>  TURN_USER=<user>  TURN_PASSWORD=<pass> \
     docker compose -f compose/docker-compose.signaling.yml up -d
   ```

   Put a TLS proxy in front so phones can reach it at `wss://<your-node-domain>`.
2. **Point your Desktop at it** — set these in your environment / `.env`:

   ```sh
   SMARTBRAIN_SIGNALING_URL=wss://<your-node-domain>
   SMARTBRAIN_SIGNALING_TOKEN=<the same SIGNALING_TOKEN>
   SMARTBRAIN_ICE_URLS=stun:<your-node-domain>:3478,turn:<your-node-domain>:3478
   SMARTBRAIN_TURN_USERNAME=<user>   SMARTBRAIN_TURN_CREDENTIAL=<pass>
   ```
3. **Turn it on:** `python3 installer/install.py webrtc up` (outbound only — no
   port-forward). Turn it off with `python3 installer/install.py webrtc down`.

Then pair devices as above. (A WireGuard VPN overlay also exists as a CLI-only
alternative — `python3 installer/install.py wireguard up` — but WebRTC is the
recommended path and needs no router changes.)

## Next

- [Privacy &amp; security](06-privacy-security.md) — what's protected and the real world limits.
