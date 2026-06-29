# Remote access (away from home)

By default SmartBrain_3000 runs only on your own computer. **Remote access** lets you
reach it from your phone — on Wi-Fi or cellular — without any router or port-forward
setup. It's **off by default**; you opt in by pairing a phone.

## How it works

Your **Desktop** is where you set everything up. To use SmartBrain on your phone, you
**pair** the phone once. After that, the phone reaches your Desktop over **WebRTC** — a
direct, **end-to-end-encrypted** connection (DTLS). When a direct link isn't possible,
traffic falls back to an encrypted **relay** that still can't read your data.

This uses a small **signaling node** on a public server (not your home machine) that helps your
phone find your Desktop. SmartBrain is **preconfigured to use one**, so there's nothing to set
up — your Desktop dials **out** to it, so nothing on your home network is ever exposed. The node
is **content-blind**: it only relays the encrypted connection setup, never your data. (Prefer your
own node? See *Self-hosting the signaling node* at the end.)

## Pair your phone

![Settings → Remote access: name a phone and pair it](assets/06-remote-access.png)

On the **Desktop**, open **Settings → Remote access**, give the phone a name, and tap
**Pair a new phone**. You'll see a QR code, three short steps, and a **6-character code**.

On the **phone**:

1. **Scan the QR** (or open the address shown) to load SmartBrain in your browser.
2. **Add it to your Home Screen**, then open the installed app:
   - **iPhone/iPad:** the **Share** button → *Add to Home Screen*.
   - **Android:** the **⋮** menu → *Install app*.
3. In the installed app, **enter the 6-character code** and tap **Pair**.

That's it — the phone connects, from Wi-Fi or cellular. The code lasts a few minutes; if it
expires, tap **Pair a new phone** for a fresh one.

> Why install first? On iPhone, an app on the Home Screen has its own private storage, separate
> from Safari — so pairing happens *in the installed app*. The QR's only job is to open the site
> so you can install it; it carries no secret.

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
- **One-time code.** The 6-character pairing code is single-use and short-lived — don't share
  it. (The QR only opens the site; it carries no secret.)

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

## Self-hosting the signaling node (advanced)

SmartBrain ships pointed at a hosted, content-blind node, so **most people need none of this.**
To run your own node instead:

1. **Run the node** on a small public server with a domain (open ports 80/443 TCP, 3478
   TCP+UDP, 49160-49260 UDP):

   ```sh
   SIGNALING_DOMAIN=<your-domain>  ACME_EMAIL=<you@example.com>  SIGNALING_OPEN=1 \
   TURN_SECRET=$(openssl rand -hex 32)  TURN_PUBLIC_IP=<vps-ipv4> \
     docker compose -f compose/docker-compose.signaling.yml up -d
   ```

   The node mints **ephemeral TURN credentials** per connection (coturn `use-auth-secret`),
   so no secret is ever baked into the app or a QR.
2. **Point your Desktop at it** — set in your environment / `.env`:

   ```sh
   SMARTBRAIN_SIGNALING_URL=wss://<your-domain>
   ```

   The Desktop fetches STUN/TURN from the node automatically; there's nothing else to set.
   Then pair devices as above.

(A WireGuard VPN overlay also exists as a CLI-only alternative —
`python3 installer/install.py wireguard up` — but WebRTC is the recommended path.)

## Next

- [Privacy &amp; security](06-privacy-security.md) — what's protected and the real world limits.
