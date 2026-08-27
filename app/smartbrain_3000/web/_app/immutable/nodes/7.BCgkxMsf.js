import{A as e,B as t,C as n,D as r,I as i,J as a,K as o,O as s,Q as c,V as l,W as u,Y as d,b as f,c as p,m,nt as h,rt as g,st as _,w as v}from"../chunks/Ch8MiCKH.js";import"../chunks/xihTtKlq.js";import{t as y}from"../chunks/N94lREeT.js";var b=[{slug:`getting-started`,title:`Getting started`,html:`<h1 id="getting-started">Getting started</h1>
<p>SmartBrain_3000 is a <strong>local-first, single-user AI assistant</strong> that runs entirely
on your own machine. Your data and credentials stay on-box, encrypted
at rest. The only outbound calls it makes are to services you explicitly opt into:
the AI providers you configure, and Google&#39;s APIs if you connect Gmail. See
<a href="#privacy-security">Privacy &amp; security</a> for the full picture.</p>
<h2 id="what-you-need">What you need</h2>
<p>On <strong>macOS, Windows, and Linux (x86_64)</strong>: nothing. There is no Docker to install, no
Python, no accounts, and no config files to edit. SmartBrain brings its own runtime — on
first start the launcher downloads a Python runtime, the app itself, and the model
gateway, checks each one against a checksum, and runs them as two ordinary programs on
your machine.</p>
<p>Two cases need <a href="https://docs.docker.com/get-docker/">Docker</a> installed and running:</p>
<ul>
<li><strong>Intel Macs</strong> — they install the same desktop app as Apple Silicon, but there is no
native build for them, so it falls back to running SmartBrain in Docker.</li>
<li><strong>Other Linux machines</strong> — arm boards and musl distros (Alpine) have no native build
yet. Containers also remain first-class on any Linux where you simply prefer them.</li>
</ul>
<p>Everything else in this guide works the same on all of them.</p>
<h2 id="install">Install</h2>
<p>Install the SmartBrain <strong>desktop app</strong> — a small menu-bar / system-tray launcher that
starts SmartBrain and opens it in your browser. The download page is
<strong><a href="https://smartbrain.securecloudgroup.com">https://smartbrain.securecloudgroup.com</a></strong>, or run the command for your system:</p>
<p><strong>macOS</strong> — in the Terminal app:</p>
<pre><code class="language-sh">brew install --cask securecloudgroup/tap/smartbrain
</code></pre>
<p><strong>Windows</strong> — in Terminal or PowerShell, using <a href="https://scoop.sh">Scoop</a>:</p>
<pre><code class="language-powershell">scoop bucket add securecloudgroup https://github.com/SecureCloudGroup/scoop-bucket
scoop install securecloudgroup/smartbrain
</code></pre>
<p><strong>Linux (x86_64)</strong> — download the install script, read it if you like (it&#39;s written to
be read), then run it:</p>
<pre><code class="language-sh">curl -fsSLO https://raw.githubusercontent.com/SecureCloudGroup/SmartBrain_3000/main/installer/install-linux.sh
sh install-linux.sh
</code></pre>
<p>It verifies the release&#39;s minisign signature and checksum, installs the launcher for
your user only (no root), and puts <strong>SmartBrain</strong> in your app menu. Start it with
<code>smartbrain start</code>. (If that says <em>command not found</em>, log out and back in — your shell
adds <code>~/.local/bin</code> to PATH automatically once it exists; until then use
<code>~/.local/bin/smartbrain</code>.) Where no tray can be drawn — a server, or stock GNOME without the
AppIndicator extension — the launcher says so once and keeps running without one;
SmartBrain works the same either way.</p>
<p><strong>Linux server (headless)</strong> — the same script with <code>--headless</code> installs a systemd
<code>--user</code> unit instead of a menu entry, started immediately; allow lingering and it also
starts at boot:</p>
<pre><code class="language-sh">sh install-linux.sh --headless
sudo loginctl enable-linger $USER
</code></pre>
<p>Manage it with <code>systemctl --user status|restart|stop smartbrain</code> or the launcher&#39;s own
verbs (<code>smartbrain status</code>, <code>smartbrain stop</code>). It serves this machine only
(<a href="http://127.0.0.1:33000">http://127.0.0.1:33000</a>); to reach it from your other devices see
<a href="#remote-access">Remote access</a>.</p>
<p><strong>Prefer containers?</strong> The Docker stack stays first-class on Linux. Download the release
compose file and start it — data then lives in named Docker volumes; back it up with the
in-app encrypted backup:</p>
<pre><code class="language-sh">curl -fsSLO https://raw.githubusercontent.com/SecureCloudGroup/SmartBrain_3000/main/compose/docker-compose.release.yml
docker compose -f docker-compose.release.yml up -d
</code></pre>
<p>On macOS the launcher starts by itself once Homebrew finishes; on Windows, open
<strong>SmartBrain</strong> from the Start menu; on Linux, launch <strong>SmartBrain</strong> from the app menu or
run <code>smartbrain start</code>. The menu-bar icon shows what it is doing. The first
start downloads a few hundred megabytes, so give it a few minutes — the status line reads
<em>&quot;Downloading SmartBrain…&quot;</em>, then <em>&quot;Starting (native)…&quot;</em>, then <em>&quot;Running ● (native)&quot;</em>.
After that it starts in seconds and your browser opens at <strong><a href="http://localhost:33000">http://localhost:33000</a></strong>. Then
complete first-run setup below.</p>
<p>On macOS and Windows, everything the desktop app installs lives in one folder you own,
alongside your data. Linux follows the XDG convention, so there it is two:</p>
<table>
<thead>
<tr>
<th></th>
<th>Folder</th>
</tr>
</thead>
<tbody><tr>
<td>macOS</td>
<td><code>~/Library/Application Support/SmartBrain</code></td>
</tr>
<tr>
<td>Windows</td>
<td><code>%APPDATA%\\SmartBrain</code></td>
</tr>
<tr>
<td>Linux — your data</td>
<td><code>~/.local/share/smartbrain</code></td>
</tr>
<tr>
<td>Linux — runtime + logs</td>
<td><code>~/.config/SmartBrain</code></td>
</tr>
</tbody></table>
<p>(The Linux Docker stack keeps its data in named Docker volumes instead.)</p>
<h3 id="if-an-install-is-misbehaving-a-clean-upgrade">If an install is misbehaving: a clean upgrade</h3>
<p>On macOS and Windows, rarely — usually after an interrupted first start, or on a machine
that ran an early Docker build — the launcher can end up with a half-finished install or a
leftover container holding port 33000. This resets it without touching your data.</p>
<p>Try <strong>Restart</strong> in the menu first — it is faster and fixes most of what goes wrong. If that
doesn&#39;t do it, work through the four steps below. They take a couple of minutes and are the
right answer for a half-finished install or a stuck port.</p>
<ol>
<li><p><strong>Stop it.</strong> In the menu-bar / tray menu choose <strong>Stop</strong>, then <strong>Quit launcher</strong>.
(<strong>Quit launcher</strong> on its own leaves SmartBrain running — <strong>Stop</strong> is what shuts it
down.)</p>
</li>
<li><p><strong>Clear any leftover containers</strong>, if that machine ever ran the Docker path:</p>
<pre><code class="language-sh">docker rm -f smartbrain_3000 smartbrain_bifrost
</code></pre>
</li>
<li><p><strong>Upgrade the launcher</strong> — <code>brew upgrade --cask smartbrain</code> on macOS,
<code>scoop update smartbrain</code> on Windows, re-run the install script on Linux.</p>
</li>
<li><p><strong>Start SmartBrain again</strong> and watch the menu. It re-downloads whatever is missing and
settles on <strong>Running ● (native)</strong>; the line under it names the version now running.</p>
</li>
</ol>
<p>Your database is in the <code>data</code> folder above and none of these steps touch it. To force a
full re-download of the runtime, delete the <code>native</code> folder next to it — that folder holds
only downloaded parts and is rebuilt on the next start.</p>
<p>If even that leaves the install broken, there is one more step — a full reset, which removes
everything SmartBrain put on the machine and reinstalls it. It is slow and deliberate, and
almost nobody needs it: see
<a href="#backup-recovery__starting-completely-fresh">Backup &amp; recovery → Starting completely fresh</a>.</p>
<h3 id="install-from-source-for-contributors">Install from source (for contributors)</h3>
<p>Building from the repo uses <strong>Docker</strong> and additionally needs <strong>git</strong> and <strong>Python 3</strong>,
and is slower — it compiles the image locally. Use it when you&#39;re developing on the code:</p>
<pre><code class="language-sh">git clone https://github.com/SecureCloudGroup/SmartBrain_3000.git
cd SmartBrain_3000
python3 installer/install.py install
</code></pre>
<p>A from-source install keeps its data in the repo&#39;s own <code>data/</code> directory, not in the
folder above.</p>
<h2 id="first-run">First run</h2>
<p>The first time you open the app it walks you through setup:</p>
<ol>
<li><strong>Choose a passphrase</strong> (at least 8 characters). It encrypts your SmartBrain
data — chats, documents, and settings — so only you can read them.</li>
<li><strong>Save your Emergency Kit.</strong> You&#39;ll be shown a <strong>Recovery Key</strong> <em>once</em>. Store it
somewhere safe and offline (print it, or put it in a password manager).<ul>
<li>There is <strong>no server and no password reset</strong>. If you forget your passphrase,
the Recovery Key is the <em>only</em> way back into your data.</li>
</ul>
</li>
<li>You&#39;re now <strong>unlocked</strong> and ready to use the app.</li>
</ol>
<h2 id="your-first-5-minutes">Your first 5 minutes</h2>
<p>A quick path from zero to seeing what SmartBrain does:</p>
<ol>
<li><p><strong>Connect a model.</strong> Open <strong>Chat</strong>. If a local model server is already running you&#39;ll
see <em>&quot;Found … running on this machine&quot;</em> — tap <strong>Connect</strong> and you&#39;re set. Nothing
running yet? Add a cloud key under <strong>Settings → Cloud providers</strong>, or start a local
model — <strong>MLX</strong> on an Apple-Silicon Mac, or <a href="https://ollama.com/download">Ollama</a> on any
OS (<code>ollama pull qwen2.5:7b-instruct</code>). See <a href="#models">Connect a model</a>.</p>
<p><img src="assets/01-chat-connect.png" alt="Chat offering a one-tap connect for a detected local model server"></p>
</li>
</ol>
<p><img src="assets/gifs/03-first-chat.gif" alt="Your first chat — tap a suggestion, get a reply">
2. <strong>Send your first message.</strong> Ask it anything — e.g. <em>&quot;What can you help me with?&quot;</em>
3. <strong>Add something to Knowledge.</strong> Open <strong>Knowledge</strong>, add a note or drop in a PDF — it&#39;s
   indexed automatically within seconds. Now ask Chat about it.
4. <strong>Watch the approval flow.</strong> Ask the assistant to <em>&quot;add a task to call the dentist
   tomorrow.&quot;</em> Because creating a task changes data, it <strong>parks for your approval</strong> instead
   of acting on its own: a card appears right in the conversation with <strong>Approve</strong>,
   <strong>Always allow</strong>, and <strong>Deny</strong>. Tap <strong>Approve</strong> and the turn picks up where it left off.
   <strong>Activity</strong> keeps a record of every such request.
5. <strong>Talk to it.</strong> Tap the <strong>mic</strong> button, say something, and stop — it notices the pause
   and stops recording on its own. Say <em>&quot;send&quot;</em> to send it. See
   <a href="#features__voice">Voice</a>.
6. <strong>That&#39;s the core loop:</strong> the assistant can read freely, but anything that changes
   data or reaches out waits for your <strong>OK</strong> — and every attempt is audited.</p>
<h2 id="locking-and-unlocking">Locking and unlocking</h2>
<p>There is <strong>one vault and one lock</strong>: the Desktop and a paired phone share it. Unlocking
on the phone unlocks the Desktop; unlocking on the Desktop unlocks the phone — a screen
sitting on the unlock page notices on its own and continues. Locking anywhere locks
everywhere, for the same reason: the key lives (or doesn&#39;t) in exactly one place.</p>
<ul>
<li>Use <strong>Lock</strong>, at the bottom of the sidebar (under <strong>More</strong> on a phone), to drop the key
from memory — your data is sealed until you unlock again. Locking also clears your
provider keys from the gateway.</li>
<li><strong>Unlock</strong> with your passphrase. Forgot it? Choose <strong>Use recovery key</strong>
and enter the key from your Emergency Kit (dashes and letter case don&#39;t matter).</li>
</ul>
<h2 id="updating">Updating</h2>
<p><strong>SmartBrain updates itself — no commands.</strong> The launcher checks for a newer version in the
background and downloads it quietly, without disturbing a session in progress. The download
is separate from the install, so nothing changes under you until you say so. The launcher
keeps only the version you&#39;re running plus the previous one, as a rollback backup — older
downloaded versions are removed automatically, so updates don&#39;t pile up on disk.</p>
<p>When an update is ready you&#39;re told in two places:</p>
<ul>
<li><p><strong>In SmartBrain itself</strong>, a strip at the top of the page: <em>&quot;SmartBrain vX.Y.Z is ready to
install. Installing restarts it — under a minute, and you&#39;ll unlock again afterwards.&quot;</em>
Click <strong>Install now</strong> to apply it. The page reconnects and reloads by itself when the new
version comes up — there&#39;s nothing to click twice. Dismissing the notice hides that
version and stays quiet until a newer one arrives.</p>
<p><img src="assets/07-update-banner.png" alt="The in-app update strip saying a new version is ready to install, with an Install now button"></p>
</li>
<li><p><strong>In the menu-bar / tray menu</strong>, as <strong>Install update now</strong> and <strong>Install on next start</strong>.
<strong>Check for updates</strong> in the same menu runs the background check on demand, and says so
when there is no newer version.</p>
</li>
</ul>
<p>Ignore it entirely and the update installs the next time you start SmartBrain. Either way
you jump straight to the newest version, even if you&#39;re several behind. Because the key is
never kept on disk, an install leaves the app <strong>locked</strong> — you unlock again afterwards.</p>
<p>Installing is <strong>Desktop-only</strong>. A <strong>paired phone</strong> can see that an update is waiting but
can&#39;t restart your machine over the network; the phone app itself refreshes the next time
you open it.</p>
<p><strong>Which version is running?</strong> The app shows it under the logo, top-left, and the menu-bar
menu names it too. During an update, when the launcher has been replaced but the app it
supervises hasn&#39;t yet, the menu names both numbers rather than one misleading one. After
the launcher updates itself it compares the version the app is actually running with what
it has already downloaded, and offers any newer one from the menu.</p>
<p>If SmartBrain updates while you have a page open, that page notices and offers a <strong>Reload</strong>:
<em>&quot;SmartBrain updated to vX.Y.Z while this page was open — reload to use the new version.&quot;</em>
You can dismiss it and keep working on the old page.</p>
<p>The launcher updates itself on the same schedule, so <code>brew upgrade --cask smartbrain</code> and
<code>scoop update smartbrain</code> are not part of normal use — they&#39;re there if you ever need to
force it.</p>
<p><strong>Linux (native)</strong> updates itself the same way. On a desktop (GNOME and the like) the
launcher relaunches itself after installing the update — an earlier version mistook the
desktop session for systemd and waited forever for a restart that never came. On a headless
install the swap happens under systemd: the launcher installs the new version, exits, and
the unit&#39;s <code>Restart=</code> brings the new one up.</p>
<p><strong>Linux (Docker):</strong> <code>docker compose -f docker-compose.release.yml pull</code>, then
<code>docker compose -f docker-compose.release.yml up -d</code>. The stack tracks the newest release;
to hold a specific version instead, export <code>SMARTBRAIN_VERSION=0.8.18</code> (or put it in a
<code>.env</code> next to the compose file) before <code>up</code> — unset it to go back to the newest.</p>
<p><strong>From source:</strong> <code>python3 installer/install.py update</code> — it <strong>backs up your encrypted data first</strong>,
pulls the latest code, rebuilds the image, restarts the stack, and verifies it&#39;s healthy. It prompts
before making changes and runs on the host, never inside the container.</p>
<p>Your data lives in the <code>data</code> folder named under <strong>Install</strong> above, and an update never
touches it. (More on backups: <a href="#backup-recovery">Backup &amp; recovery</a>.)</p>
<h2 id="troubleshooting">Troubleshooting</h2>
<h3 id="ask-the-doctor-first">Ask the doctor first</h3>
<p>If you have Python 3 and a copy of the repository, one command inspects this computer&#39;s
install and says what is wrong in plain words — it needs no running app, which is exactly
when you want it:</p>
<pre><code class="language-sh">python3 installer/doctor.py
</code></pre>
<p>It changes nothing. Add <code>--fix</code> and it offers each safe repair one at a time, describing
what it will do before it does it; it never touches your data. It knows about half-finished
downloads, records pointing at processes that are gone, ports held by something else, a
gateway that has quietly died under a running app, a locked vault, a model server that
isn&#39;t answering, staged updates, and low disk space.</p>
<p>The rest of this section is what those problems look like from the menu.</p>
<p>Most first-run problems are one of these:</p>
<ul>
<li><strong>Start with Settings → Status.</strong> Before restarting anything, open <strong>Settings → Status</strong>
in the app. It shows the app version, whether the vault is locked, the voice model&#39;s
download progress with a one-tap <strong>Retry download</strong>, which dictation engine is active,
how the model server is configured, your knowledge counts, schedules, feeds (with a chip
counting any that are failing), paired devices, and <strong>Storage &amp; memory</strong> — disk used by
the database and the models (the voice model is about 141 MB), and memory in use. Most
&quot;something seems off&quot; questions are answered there.</li>
<li><strong>The page won&#39;t load at <a href="http://localhost:33000">http://localhost:33000</a>.</strong> Give a first start a few more minutes —
it&#39;s downloading a few hundred megabytes, and the menu&#39;s status line says what it&#39;s doing.
Once that line reads <strong>Running ●</strong>, click <strong>Open SmartBrain</strong> in the menu.</li>
<li><strong>&quot;Download failed — nothing was changed; check the log and Restart.&quot;</strong> The download of the
runtime didn&#39;t finish (no connection, a proxy, or not enough disk space). Nothing on your
machine was altered. Fix the cause and click <strong>Restart</strong> in the menu. A failed download of
an <em>update</em> is announced the same way, in a desktop notification that names the error,
rather than being retried silently.</li>
<li><strong>&quot;SmartBrain keeps crashing — stopped restarting; see the native logs.&quot;</strong> The launcher
restarts a stopped SmartBrain, but gives up after three tries in ten minutes rather than
spinning. The logs are <code>app.log</code> and <code>bifrost.log</code> — choose <strong>Open logs</strong> in the
menu-bar / tray menu and the folder opens in Finder / Explorer. Their full home is
<code>~/Library/Application Support/SmartBrain/native/run/</code> on macOS (Finder hides
<code>~/Library</code>, so outside the menu use <strong>Go → Go to Folder…</strong>, ⇧⌘G),
<code>%APPDATA%\\SmartBrain\\native\\run\\</code> on Windows, and
<code>~/.config/SmartBrain/native/run/</code> on Linux.</li>
<li><strong>&quot;Native start failed — see the log.&quot;</strong> Open <code>app.log</code> (<strong>Open logs</strong> in the menu).
If it says an instance is <em>already serving on port 33000</em>, something else holds that
port — usually a SmartBrain a previous launcher started and never stopped. Choose
<strong>Stop</strong> in the menu, then <strong>Restart</strong>; if it persists, follow <strong>If an install is
misbehaving: a clean upgrade</strong> under <strong>Install</strong> above.</li>
<li><strong>No tray icon on Linux (usually stock GNOME).</strong> GNOME removed tray icons; the
<a href="https://extensions.gnome.org/extension/615/appindicator-support/">AppIndicator extension</a>
brings them back. SmartBrain notices the missing tray, tells you once in a desktop
notification, and keeps running without it — the browser at <a href="http://localhost:33000">http://localhost:33000</a> and
the <code>smartbrain</code> commands work exactly the same.</li>
<li><strong>macOS asks if SmartBrain may &quot;access data from other apps.&quot;</strong> Click <strong>Allow</strong>, or don&#39;t —
the launcher is checking whether Docker is installed, which it only needs as a fallback.
It reads nothing else, and declining doesn&#39;t stop SmartBrain from running.</li>
<li><strong>The mic is greyed out or shows a percent.</strong> The voice model (about 141 MB) is still
downloading — it starts at app launch on every OS, and the percent is its progress. Wait
for it to finish. A red error state on the mic means the download failed — tap the mic to
retry, or use <strong>Retry download</strong> under <strong>Settings → Status</strong>. A failed voice download is
also announced, so it never fails silently. See <a href="#features__voice">Voice</a>.</li>
<li><strong>Chat says &quot;No models available yet.&quot;</strong> You haven&#39;t connected a model. If a local
model server (MLX or Ollama) is running, the Chat screen offers a one-tap <strong>Connect</strong>;
otherwise add a cloud key under <strong>Settings → Cloud providers</strong>. See
<a href="#models">Connect a model</a>.</li>
<li><strong>Every answer is slow, by several seconds, always.</strong> A local model server can be
configured to reload the model on every single request. SmartBrain notices and writes a
line to <code>native/run/app.log</code> naming the model and the seconds lost, with what to check
(a draft/speculative-decoding option pointed at an incompatible model, or an
idle-unload setting). It isn&#39;t shown in the app — read it via <strong>Open logs</strong> in the
menu if answers feel uniformly slow.</li>
<li><strong>Search says &quot;Showing keyword results&quot;.</strong> No embedding model is set up yet. See
<a href="#models__embeddings-for-knowledge-search">Embeddings</a> for your setup, then
<strong>Reindex</strong> in Knowledge.</li>
<li><strong>The browser warns about the certificate</strong> (only if you set up LAN/HTTPS). Trust
the local mkcert CA — see <a href="#remote-access">Remote access</a>.</li>
<li><strong>&quot;Database is newer than this app&quot; / a restore is refused.</strong> Pointing an older build
at a newer data directory, or restoring a backup from a newer version, is refused on
purpose to prevent data loss. Let SmartBrain update itself first, then reopen or retry
the restore.</li>
</ul>
<p>On <strong>Intel Macs</strong> and Linux machines running the <strong>Docker stack</strong>, two more apply:</p>
<ul>
<li><strong>&quot;Docker is required — install it, start it, then click Restart.&quot;</strong> Install
<a href="https://docs.docker.com/get-docker/">Docker</a> — the launcher opens the download page for
you the first time — then click <strong>Restart</strong> in the menu. Docker Desktop&#39;s very first
launch asks you to accept its terms; do that before continuing.</li>
<li><strong>&quot;Docker isn&#39;t running — start Docker, then Restart.&quot;</strong> The daemon is installed but
stopped. Start Docker Desktop (or <code>colima start</code>), then <strong>Restart</strong>. To read the logs:
<code>docker compose -f docker-compose.release.yml logs smartbrain</code>, run from the folder
named under <strong>Install</strong> above.</li>
</ul>
<h2 id="uninstall">Uninstall</h2>
<p><strong>An uninstall never removes your data.</strong> Removing SmartBrain is two steps, and the second
one is yours to take deliberately.</p>
<ol>
<li><p><strong>The app.</strong> Stop it first (<strong>Stop</strong> in the menu), then remove it however you installed
it: <code>brew uninstall --cask smartbrain</code> on macOS, <code>scoop uninstall smartbrain</code> on
Windows, <code>sh install-linux.sh --uninstall</code> on Linux (it removes the launcher, menu
entry, and systemd unit, then names exactly what data remains), or
<code>docker compose -f docker-compose.release.yml down</code> for the Linux Docker stack. From
source, <code>docker compose down</code> in <code>compose/</code>.</p>
<p>On macOS you can add <code>--zap</code> to clear what the app downloaded as well:
<code>brew uninstall --zap --cask smartbrain</code>. That removes the assembled runtime, the logs,
the launcher&#39;s bookkeeping, and the gateway&#39;s configuration — which holds provider keys
the app pushed into it, so clearing it is the point. <strong>It does not touch your <code>data</code>
folder</strong>, and neither does a plain uninstall.</p>
</li>
<li><p><strong>Your data</strong>, if and when you want it gone. It is the folder named under
<strong>Install</strong> above, with <code>data</code> inside it:</p>
<table>
<thead>
<tr>
<th></th>
<th>Delete</th>
</tr>
</thead>
<tbody><tr>
<td>macOS</td>
<td><code>~/Library/Application Support/SmartBrain</code></td>
</tr>
<tr>
<td>Windows</td>
<td><code>%APPDATA%\\SmartBrain</code></td>
</tr>
<tr>
<td>Linux (native)</td>
<td><code>~/.local/share/smartbrain</code> and <code>~/.config/SmartBrain</code></td>
</tr>
</tbody></table>
<p>On native Linux, <code>sh install-linux.sh --purge</code> is the one-command version: launcher,
runtime, logs, gateway configuration, and data, all gone.</p>
<p>On the Linux Docker stack the data is in Docker volumes, so it goes with the stack:
<code>docker compose -f docker-compose.release.yml down -v</code>. The <code>-v</code> is what deletes the
volumes — without it your data stays.</p>
<p>Take a <strong>Download encrypted backup</strong> first if there&#39;s any chance you&#39;ll want it back —
see <a href="#backup-recovery">Backup &amp; recovery</a>. There is no way to recover it afterwards.</p>
<p>(From source, data lives in the repo&#39;s <code>data/</code> directory instead — delete that.)</p>
</li>
</ol>
<h2 id="next">Next</h2>
<ul>
<li><a href="#models">Connect a model</a> — add a cloud provider key or a local model.</li>
<li><a href="#features">Using SmartBrain_3000</a> — chat, knowledge, planner, schedules, email.</li>
</ul>
`},{slug:`models`,title:`Connect a model`,html:`<h1 id="connect-a-model">Connect a model</h1>
<p>SmartBrain_3000 talks to language models through a local <strong>gateway</strong> (Bifrost),
which runs on your machine alongside the app. You can use <strong>cloud providers</strong> (with your
own API keys) and/or <strong>local models</strong> running on your machine. Nothing is sent to a
provider unless you configure it and use it.</p>
<h2 id="cloud-providers-your-api-keys">Cloud providers (your API keys)</h2>
<p>An API key is a long secret string you create in a provider&#39;s developer console. It is
<strong>billed per use and is not the same thing as a consumer subscription</strong> — a ChatGPT Plus
or Claude Pro plan does not include one, and paying for a plan does not give you a key.
Most providers ask for a card and bill cents per request at typical personal use. If you
would rather pay nothing and keep everything on your machine, skip this section entirely
and use <a href="#local-models-yours-on-this-machine-or-another-one-you-own">a local model</a> instead.</p>
<p>Open <strong>Settings → Cloud providers</strong> and add a key for any of:</p>
<ul>
<li><strong>OpenAI</strong> — <a href="https://platform.openai.com/api-keys">platform.openai.com/api-keys</a></li>
<li><strong>Anthropic</strong> — <a href="https://console.anthropic.com/settings/keys">console.anthropic.com/settings/keys</a></li>
<li><strong>Google (Gemini)</strong> — <a href="https://aistudio.google.com/apikey">aistudio.google.com/apikey</a></li>
</ul>
<p><img src="assets/02-providers.png" alt="Settings → Cloud providers, with key fields for OpenAI, Anthropic, and Google"></p>
<p><img src="assets/gifs/02-connect-a-model.gif" alt="Connect a model — one-tap connect a detected local model, or add an encrypted cloud key"></p>
<p>Keys are stored <strong>encrypted on your machine</strong> and pushed to the local gateway
while you&#39;re unlocked; locking removes them from the gateway again. The app never
returns a stored key over its API — only the fact that one is set.</p>
<blockquote>
<p>Using a cloud model means your prompts (and any content you send) go to that
provider. If you&#39;d rather keep everything on your machine, use a local model.</p>
</blockquote>
<h2 id="local-models-yours-on-this-machine-or-another-one-you-own">Local models (yours — on this machine or another one you own)</h2>
<p>Local models keep every prompt on hardware you control — nothing goes to a provider. You run
the model server yourself and SmartBrain connects to it: usually on the same machine, but a
server elsewhere on your network works too (a common setup: SmartBrain on a Linux box, the
models on a Mac&#39;s GPU — see <a href="#use-a-model-server-on-another-machine">Use a model server on another machine</a>).
SmartBrain supports two backends and connects to either the same way:</p>
<ul>
<li><p><strong>MLX</strong> — Apple&#39;s on-device runtime for <strong>Apple-Silicon Macs</strong> (M-series). It&#39;s the fastest
path on a Mac, so it&#39;s the one to reach for first there. The easiest way to run it is an
MLX <strong>server app</strong> (for example oMLX): download it, pick a model, and it serves on port
<code>8888</code> — SmartBrain&#39;s one-tap Connect finds it from there. No Python, no terminal.</p>
<p>Prefer the command line? <code>mlx-lm</code> works too (<code>pip install mlx-lm</code>, then):</p>
<pre><code class="language-sh">mlx_lm.server --port 8888 --model mlx-community/Qwen2.5-7B-Instruct-4bit
</code></pre>
</li>
<li><p><strong>Ollama</strong> — works on <strong>any OS</strong>, and is <strong>the</strong> local-model path on Windows and Linux
(MLX is Apple-Silicon-only). <a href="https://ollama.com/download">Install it</a>, then pull a model:</p>
<pre><code class="language-sh">ollama pull qwen2.5:7b-instruct
</code></pre>
</li>
</ul>
<p><strong>Which model?</strong> For local chat we suggest <strong>Qwen2.5-7B-Instruct</strong> — it follows instructions
and drives the assistant&#39;s tools reliably at a size that runs comfortably on a laptop. That&#39;s
<code>mlx-community/Qwen2.5-7B-Instruct-4bit</code> on MLX, or <code>qwen2.5:7b-instruct</code> on Ollama. Any
tool-capable model works; the Chat model picker lists whatever your server has.</p>
<p>Open <strong>Settings → Local models</strong> to connect a backend by port. The panel shows whether each
is reachable and which models it has.</p>
<blockquote>
<p><strong>Already running MLX or Ollama?</strong> You usually don&#39;t need to touch this panel. SmartBrain
<strong>detects</strong> a local MLX (<code>:8888</code>) or Ollama (<code>:11434</code>) server on its default port and offers
a one-tap <strong>Connect</strong> — on the <strong>Chat</strong> screen when you have no model yet, and here under the
port field. The manual port/URL fields are for non-standard setups.</p>
</blockquote>
<p><img src="assets/03-local-models.png" alt="Settings → Local models showing a detected local server with a Connect link"></p>
<h3 id="use-a-model-server-on-another-machine">Use a model server on another machine</h3>
<p>SmartBrain on one computer can use a model server on another — e.g. SmartBrain on a Linux
laptop, the models on an Apple-Silicon Mac. Three steps:</p>
<ol>
<li><p><strong>Make the server listen beyond localhost</strong>, on the server machine:</p>
<ul>
<li><strong>oMLX</strong>: enable its <em>network access</em> setting — it starts listening on the LAN and
shows an <strong>API key</strong> (copy it; requests without it are refused).</li>
<li><strong><code>mlx_lm.server</code></strong>: start with <code>--host 0.0.0.0</code>.</li>
<li><strong>Ollama</strong>: start with the environment variable <code>OLLAMA_HOST=0.0.0.0</code>.
Allow the app through that machine&#39;s firewall if prompted.</li>
</ul>
</li>
<li><p><strong>Verify from the SmartBrain machine</strong> (expect a JSON model list):</p>
<pre><code class="language-sh">curl -s -H &quot;Authorization: Bearer YOUR_KEY&quot; http://SERVER_IP:8888/v1/models
</code></pre>
<p>(Drop the header for a server with no key; Ollama&#39;s port is <code>11434</code>.)</p>
</li>
<li><p><strong>Connect in SmartBrain</strong>: Settings → Local models → the backend&#39;s
<strong>&quot;Server on another machine&quot;</strong> field → enter <code>http://SERVER_IP:PORT</code>, paste the API key
if the server has one, <strong>Save &amp; connect</strong>.</p>
</li>
</ol>
<p>Traffic between the two machines is plain HTTP on your own network — fine at home; don&#39;t
route it across networks you don&#39;t trust. Note that local model servers answer one request
at a time, so two SmartBrains sharing one server take turns.</p>
<h2 id="choosing-a-model-in-chat">Choosing a model in Chat</h2>
<p>The <strong>Chat</strong> screen has a <strong>Provider</strong> and a <strong>Model</strong> picker above the conversation. It
opens on your routed Chat model (below); picking a different one there applies to that
session only and is never saved. Only chat-capable models are listed — an embedding model
can&#39;t hold a conversation, so it isn&#39;t offered.</p>
<p>If you pick a model that can&#39;t call tools, SmartBrain says so under the reply rather than
pretending: <em>&quot;This model can&#39;t use tools, so it answered from its own knowledge only — web
search, tasks, knowledge, and email actions won&#39;t run.&quot;</em></p>
<h2 id="which-model-does-what-model-routing">Which model does what (Model routing)</h2>
<p><strong>Settings → Model routing</strong> decides which model serves which job. Every model you have
configured — cloud or local — can be pointed at any slot, and the list is discovered live
from your providers.</p>
<table>
<thead>
<tr>
<th>Slot</th>
<th>What it serves</th>
<th>If you don&#39;t set it</th>
</tr>
</thead>
<tbody><tr>
<td><strong>Chat</strong></td>
<td>Ordinary conversation and the assistant&#39;s tool-using turns.</td>
<td><code>openai/gpt-4o-mini</code>, which needs an OpenAI key — so this is the one slot worth setting deliberately.</td>
</tr>
<tr>
<td><strong>Agent tasks (schedules)</strong></td>
<td>Scheduled runs and background turns. These call tools, so pick a model that reliably tool-calls.</td>
<td><strong>Same as Chat</strong></td>
</tr>
<tr>
<td><strong>Embedding (semantic search)</strong></td>
<td>Turning your documents and queries into vectors for meaning search. Only embedding models are offered.</td>
<td><code>ollama/nomic-embed-text:v1.5</code></td>
</tr>
<tr>
<td><strong>Document summaries</strong></td>
<td>The background summary tree that makes &quot;summarize this&quot; instant on large documents.</td>
<td>Same as Chat</td>
</tr>
</tbody></table>
<p>Chat is the root: the two slots that say <em>Same as Chat</em> really do follow it, so setting Chat
alone is a complete configuration.</p>
<p>Two more things worth knowing:</p>
<ul>
<li>Changing <strong>Embedding</strong> only affects new items. Run <strong>Reindex (semantic)</strong> on the
Knowledge page afterwards so existing documents stay searchable.</li>
<li><strong>Document summaries</strong> is the slot to change if you have a big-context cloud model and a
book-sized library: it turns a summary tree that would trickle for hours on a small local
model into minutes. Point it at a cloud model and your documents are sent to that provider
as the tree is built — keep it on a local model if that matters to you.</li>
</ul>
<h3 id="model-context-length">Model context length</h3>
<p>Under the routing table, <strong>Model context length</strong> tells SmartBrain how many tokens each
model can hold. That number sizes how much of a document, or how large a tool result, the
model is handed in one step — a bigger context means Chat reads and summarizes far longer
documents per step.</p>
<p>MLX servers report their own context length and are filled in automatically. Anything else
uses <strong>8,192 tokens</strong> until you set a value. Leave a field blank to go back to the default,
and use the model&#39;s real figure — this setting tells SmartBrain what the model can take, it
doesn&#39;t change what the model can take.</p>
<h2 id="embeddings-for-knowledge-search">Embeddings (for Knowledge search)</h2>
<p>Semantic search in the <a href="#features">Knowledge base</a> needs an <strong>embedding
model</strong>. The default is a <strong>local</strong> <code>nomic-embed-text:v1.5</code>, served through Ollama, so
your knowledge content stays on-box.</p>
<p><strong>MLX-only stack (no Ollama):</strong> the simplest path is to serve an <strong>encoder embedding
model directly on your MLX chat server</strong> — no second server needed. MLX server apps like
oMLX serve encoder-class embedders (ModernBERT/BERT family; a good pick is
<code>nomic-ai/modernbert-embed-base</code>): load it alongside your chat model, then route
Settings → Model routing → <strong>Embedding</strong> → <code>mlx/&lt;that model&gt;</code> and <strong>Reindex</strong>. Done —
one server runs everything.</p>
<p>They refuse <em>decoder</em> embedding models such as Qwen3-Embedding (&quot;not an embedding
model&quot;). Only if you specifically want one of those, use the bundled fallback: the
<strong>MLX embeddings server</strong> — a tiny login service on port 8899 serving
<code>Qwen3-Embedding-0.6B</code> with correct pooling. Its installer ships <strong>in the source
repository</strong>, not in the desktop install: on the server machine,
<code>git clone https://github.com/SecureCloudGroup/SmartBrain_3000.git</code> and run
<code>tools/mlx_embed_server/install.sh</code>. Then connect it under Settings → Local models →
<strong>MLX embeddings</strong> (same-machine port, or its &quot;Server on another machine&quot; field) and route
Embedding to <code>mlxe/qwen3-embedding-0.6b</code>.</p>
<p><strong>Pull it yourself</strong> once, with that exact tag:</p>
<pre><code class="language-sh">ollama pull nomic-embed-text:v1.5
</code></pre>
<p>(A from-source install does this for you when Ollama is present, and
<code>python3 installer/doctor.py --fix</code> offers to pull it if it is missing.)</p>
<p>The tag matters: the bare <code>nomic-embed-text</code> won&#39;t resolve. If search says <em>&quot;Showing
keyword results&quot;</em>, no embedding model is in place — run the command above and
<strong>Reindex</strong>. You can change the model, but pointing embeddings at a cloud provider
sends your documents there on every reindex — only do that if you accept that tradeoff.</p>
<h2 id="voice-dictation-and-spoken-replies">Voice (dictation and spoken replies)</h2>
<p>Chat can listen and talk — see <a href="#features__voice">Voice</a> for how it is used (modes,
wake word, choosing a voice per OS). This page is only about what runs underneath.
<strong>Your voice never leaves your machines</strong>, and there is <strong>nothing to set up</strong>: dictation
is built in, on every OS.</p>
<ul>
<li><strong>Built in (the default)</strong> — SmartBrain transcribes on your machine with
<strong>Whisper</strong> (via <a href="https://github.com/SYSTRAN/faster-whisper">faster-whisper</a>, the
industry-standard local runtime — chosen after field testing for its robustness on
real-world voices). Its model files (~141 MB, integrity-checked) download once, in
the background, starting the moment the app launches — <strong>watch the progress right
on the mic button</strong>, or on <strong>Settings → Status</strong>. Until it&#39;s ready the mic shows a
live percent instead of pretending; after that, dictation runs in well under a
second, fully offline, in your language — Whisper understands dozens. One dictation
is transcribed at a time; a recording is capped at two minutes.</li>
<li><strong>Your own audio server (optional)</strong> — for other languages or maximum accuracy,
run any local server that speaks the standard <code>/v1/audio/transcriptions</code> API (oMLX
with a whisper model on a Mac, <a href="https://github.com/speaches-ai/speaches">speaches</a>
or <code>whisper.cpp</code>&#39;s server elsewhere) and put its address in <strong>Settings → Local
models → Voice</strong>. When it&#39;s configured and healthy it takes over automatically —
and if it ever can&#39;t serve, the built-in engine carries on instead of failing.</li>
<li><strong>Phone (PWA)</strong> — nothing to configure: your phone&#39;s microphone audio travels the
same encrypted connection as everything else to the Desktop, which transcribes it
locally. Replies use the phone&#39;s own voices, offline.</li>
</ul>
<p><strong>Spoken replies</strong> use your device&#39;s built-in voices by default — instant and offline
on macOS, Windows, iPhone, and Android; Linux gets excellent ones through Pied/Piper
(see <a href="#features__choosing-a-voice">Choosing a voice</a>). If you would rather use a
server voice, set the optional <strong>Server voice model</strong> in the Voice card (e.g. <code>kokoro</code>
on a server that offers speech) and SmartBrain speaks through it instead.</p>
<p>Voice settings live in two places: the optional <strong>servers</strong> above under <strong>Settings →
Local models → Voice</strong>; everything about <em>using</em> voice — download progress and retry,
the engine in use, playback speed, wake word and its test, the mic &amp; speaker check —
under <strong>Settings → Status → Voice</strong>.</p>
<h2 id="next">Next</h2>
<ul>
<li><a href="#features">Using SmartBrain_3000</a> — start chatting and add knowledge.</li>
<li><a href="#mcp">Connect external tools</a> — let a desktop AI client (e.g. Claude Desktop) read your Knowledge.</li>
</ul>
`},{slug:`features`,title:`Using SmartBrain_3000`,html:`<h1 id="using-smartbrain_3000">Using SmartBrain_3000</h1>
<p>Everything here runs locally and is encrypted at rest. Here&#39;s what each area does.</p>
<h2 id="where-things-live">Where things live</h2>
<p>The sidebar holds nine areas — this is the whole app. On a phone the four you reach for
most sit in the bottom bar (Chat, Knowledge, Info, Activity) and the rest are under <strong>More</strong>:</p>
<table>
<thead>
<tr>
<th>Area</th>
<th>What it&#39;s for</th>
</tr>
</thead>
<tbody><tr>
<td><strong>Chat</strong></td>
<td>Talk to the assistant. It can use tools; anything consequential waits for you.</td>
</tr>
<tr>
<td><strong>Knowledge</strong></td>
<td>Your documents and notes, plus the vaults that group them.</td>
</tr>
<tr>
<td><strong>Planner</strong></td>
<td>Tasks, with due dates, priorities and recurrence.</td>
</tr>
<tr>
<td><strong>Schedules</strong></td>
<td>Prompts that run on a timer.</td>
</tr>
<tr>
<td><strong>Email</strong></td>
<td>An optional Gmail connection: read and send.</td>
</tr>
<tr>
<td><strong>Info</strong></td>
<td>The output of your scheduled runs, newest first.</td>
</tr>
<tr>
<td><strong>Activity</strong></td>
<td>Approvals waiting for you, and the record of everything the assistant tried.</td>
</tr>
<tr>
<td><strong>Usage</strong></td>
<td>What your cloud models have cost.</td>
</tr>
<tr>
<td><strong>Settings</strong></td>
<td>Everything you configure — and <strong>Status</strong>, a live view of everything the app is doing. Desktop only.</td>
</tr>
</tbody></table>
<p>Below them sit four controls: <strong>Help</strong> (this guide, offline, no unlock needed), <strong>Theme</strong>
(follow the system, or force light or dark), <strong>Lock</strong>, and — on a paired phone — <strong>Unpair</strong>.
The top strip shows an <strong>Encrypted · On-device</strong> chip and, on a phone, the remote connection
state. The version you&#39;re running is under the logo, top-left.</p>
<p>The <strong>Desktop</strong> shows all nine areas. On a <strong>paired phone</strong>
(<a href="#remote-access">Remote access</a>) you get the eight meant for use on the go — Chat,
Knowledge, Planner, Schedules, Email, Info, Activity, and Usage — while Settings and
first-time setup stay on the Desktop.</p>
<h2 id="chat">Chat</h2>
<p>Talk to your assistant. Chat can optionally <strong>use tools</strong> to act on your behalf —
search your knowledge, <strong>read or summarize a whole document</strong>, <strong>save a note back to
your knowledge</strong>, add a task, fetch a public web page, send an email, and more — the full
list is under <strong>What the assistant can do</strong>, below. Replies are formatted: headings, lists,
tables, and code blocks render properly.</p>
<h3 id="while-an-answer-is-being-written">While an answer is being written</h3>
<p>Answers <strong>stream in</strong> word by word. While one is arriving the Send button becomes
<strong>Stop</strong> — press it and the partial answer is kept, marked <em>(stopped)</em>, rather than thrown
away. When the assistant is using tools it narrates what it is doing in place of the
thinking dots: <em>&quot;Searching the web…&quot;</em>, <em>&quot;Reading a document…&quot;</em>, <em>&quot;Writing the answer…&quot;</em>,
each ticked off as it finishes.</p>
<p>If the conversation has scrolled, <strong>Top</strong> and <strong>Latest</strong> pills jump you to either end.</p>
<h3 id="after-an-answer">After an answer</h3>
<p>Every reply carries <strong>Copy</strong> (the raw Markdown, not the rendered page) and <strong>Listen</strong>
(read just this answer aloud — see <strong>Voice</strong>, below). The most recent one also offers
<strong>Regenerate</strong> — ask again for a fresh answer to your last message. The new answer is
added below the old one rather than replacing it, so what you see is exactly what a
reload will show. Only the newest answer can be regenerated; redoing an older one would
fork the thread. Every message <em>you</em> sent carries a colored <strong>Retry</strong> beside <strong>You</strong> — it
sends that exact message again, handy after a model hiccup or a model switch.</p>
<p>Answers that used your knowledge show <strong>source chips</strong> underneath — more on those under
<strong>Knowledge</strong>, below.</p>
<h3 id="saved-chats">Saved chats</h3>
<p><strong>+ New chat</strong> starts a fresh thread. The <strong>Saved chats</strong> picker at the top switches
between them, with <strong>Load older</strong> for threads beyond the first page; <strong>Load older messages</strong>
does the same inside a long thread. <strong>Rename</strong> retitles the open chat (a new chat is titled
from your first message).</p>
<p><strong>Refresh</strong> reloads the thread and the chat list — useful when you continued a conversation
on your phone and want it on the Desktop, or the reverse. The page also refreshes itself
whenever you come back to it.</p>
<p><strong>Delete</strong> moves the open chat to the <strong>Trash</strong>; <strong>Delete all…</strong> moves every chat there,
behind a confirmation. Trashed chats are restorable for 30 days from
Settings → Account &amp; Data, and are removed for good after that.</p>
<p>Above the conversation, <strong>Provider</strong> and <strong>Model</strong> pick the model for this session — see
<a href="#models__choosing-a-model-in-chat">Choosing a model in Chat</a>.</p>
<h3 id="it-knows-what-time-it-is">It knows what time it is</h3>
<p>The assistant knows what time it is <strong>where you are</strong>. Your browser reports its
timezone and every turn is told your local date and time, with UTC alongside for
cross-zone questions; scheduled runs get the same. There is nothing to configure — the
zone is read from your browser and stored locally, like any other setting.</p>
<h3 id="tools-and-approval">Tools and approval</h3>
<p>Tools are <strong>risk-tiered</strong>, and this is the core safety idea:</p>
<ul>
<li><strong>Observe</strong> (e.g. knowledge search) runs automatically — it only reads.</li>
<li><strong>Reviewed</strong> (e.g. add a task, search the web) is <strong>never run automatically</strong> until you
say so. The assistant <em>proposes</em> it and a card appears <strong>right in the conversation</strong>
with <strong>Approve</strong>, <strong>Always allow</strong>, and <strong>Deny</strong>; resolving the last pending card resumes
the turn by itself, and <strong>Approve all</strong> appears when several reviewed-tier actions are
waiting. <strong>Activity</strong> keeps the record. If you get tired of approving the same tool, <strong>Always allow</strong> lets that one run without
asking from then on — and <strong>Stop allowing</strong> takes it back. The two tools that fetch a
URL the assistant composed (<strong>Fetch a page</strong>, <strong>Add a URL to knowledge</strong>) are allowed
<strong>per site</strong>: the button reads <em>Always allow <that site></em>, future calls to that exact
site run unattended, and a different site still asks once. That&#39;s deliberate — a page
the assistant reads could try to talk it into fetching an address an attacker owns,
and an unknown site always parks for your review.</li>
<li><strong>Irreversible</strong> (e.g. send an email, delete a task) always waits for your approval, with
an extra confirmation, and can never be pre-authorized.</li>
</ul>
<p>So the assistant can draft and suggest, but anything that changes data or reaches
out requires your explicit OK. Every attempt is recorded in <strong>Activity</strong>.</p>
<p><strong>For example:</strong> ask <em>&quot;search my knowledge for the lease terms&quot;</em> and the assistant
reads and answers immediately (Observe). Ask <em>&quot;email the landlord about it&quot;</em> and it
<strong>drafts</strong> the message and <strong>parks it as a card in the chat</strong> — nothing sends until you
approve it there (Irreversible, with an extra confirm). Activity lists it too.</p>
<p>A parked action doesn&#39;t wait indefinitely — see <strong>Activity</strong>, below.</p>
<h2 id="voice">Voice</h2>
<p>Chat can listen and talk, with <strong>nothing to set up</strong>: dictation is built in on every OS
and runs on your own machine (how, and the optional own-server upgrade, is under
<a href="#models__voice-dictation-and-spoken-replies">Voice</a> on the models page). Nothing you
say is sent to any speech service — on the phone, audio travels the encrypted link to
your Desktop, which transcribes it locally.</p>
<h3 id="dictate">Dictate</h3>
<p>Tap the <strong>mic</strong> beside the message box (or <strong>hold Space</strong> on a keyboard) and talk. Your
words appear <strong>under the box as you speak</strong>; when you pause, the recording ends by itself
and the finished transcript lands in the message box — <strong>you review before it sends</strong>.
Say <strong>“send”</strong> at the end to submit in the same breath, <strong>“cancel”</strong> to discard, or
<strong>“start over”</strong> to clear and re-listen; <strong>Esc</strong> cancels a recording too. One recording
is capped at two minutes.</p>
<p>The first time, the mic may show a <strong>percent</strong>: the speech model is downloading (once,
about 141 MB) and the mic enables itself when it is ready. A red mic means the download
failed — tap it to retry, or use <strong>Retry download</strong> on <strong>Settings → Status</strong>.</p>
<h3 id="the-voice-modes">The voice modes</h3>
<p>Above the message box sits a row of labeled pills. Each is a mode you leave on:</p>
<ul>
<li><strong>Speak replies</strong> — answers are read aloud <strong>sentence by sentence as they stream in</strong>,
in your device&#39;s own voice. Every answer also has a quiet <strong>Listen</strong> action underneath
to hear just that one. While a reply is being read, <strong>Send becomes Stop</strong> — press it to
stop the voice (a stopped answer keeps its text).</li>
<li><strong>Hands-free</strong> — every dictation sends itself when you pause; say “cancel” to stop one.</li>
<li><strong>Conversation</strong> — 100% voice. You talk, it sends itself, the reply is spoken, and the
mic reopens for your follow-up. Say <strong>“stop listening”</strong> or <strong>“goodbye”</strong> to end. The
first mic open still needs one tap (a browser rule); after that, no buttons. To cut a
reply short, press <strong>Stop</strong> — or, with a wake word set, say the phrase over it. (The
mic stays closed while a reply is read unless a wake word is set: the microphone hears
the reply too, and only your phrase can tell the two apart.)</li>
<li><strong>Short · Medium · Long</strong> — how long <em>spoken</em> replies should be. It applies only while
replies are read aloud (Speak replies or Conversation on); typed chat is unaffected. The
default is <strong>Short</strong>, because a long spoken answer is tiring — Long is one tap away when
you need the detail.</li>
</ul>
<h3 id="your-own-wake-word">Your own wake word</h3>
<p>With Conversation on, SmartBrain can wait for a phrase instead of listening all the time
— <strong>“Hey SmartBrain”</strong>, <strong>“Hey Merl”</strong>, whatever you like. Set it under <strong>Settings →
Status → Voice</strong> and press <strong>Test recognition</strong>: say the phrase three times, and it shows
exactly what the engine heard each time. Unusual names are often spelled the engine&#39;s own
way (“Merl” may come back as “Merle”); one tap <strong>accepts those spellings</strong>, and from then
on the phrase works as you say it. Then, in Chat, the Conversation pill shows your phrase
and the mic waits for it: <strong>“Hey Merl, what&#39;s on my calendar?”</strong> carries the question
through in one breath, and anything that doesn&#39;t start with the phrase is ignored (the
hint under the box tells you what it heard). If a recording turns out to be the reply&#39;s
own voice coming back through the microphone, it is dropped and the hint says so.</p>
<h3 id="choosing-a-voice">Choosing a voice</h3>
<p>Spoken replies use the voices your browser gets from the operating system — instant and
offline. The default voice is rarely the best one installed, and a better one is a
settings change away:</p>
<ul>
<li><strong>macOS</strong> — <em>System Settings → Accessibility → Spoken Content → System voice → ⓘ</em>, pick a
language and download an <strong>Enhanced</strong> or <strong>Premium</strong> voice (Zoe, Ava, Samantha
Enhanced…). Safari and Chrome pick them up immediately; the built-in voices are
excellent and need nothing else.</li>
<li><strong>Linux</strong> — desktops often ship no browser voices at all. Install
<a href="https://github.com/Elleo/pied">Pied</a> (a Flatpak): it sets up the <strong>Piper</strong> neural
voices for speech-dispatcher, which Chrome, Chromium and Firefox use. Chrome lists them
as “… piper”; Firefox lists them by file name (<code>en_US-…-medium.onnx</code>) — same voices.
They sound as good as the commercial ones.</li>
<li><strong>Windows</strong> — <em>Settings → Accessibility → Narrator → Add natural voices</em> installs
Microsoft&#39;s <strong>Natural</strong> voices (Ava, Andrew…). <strong>Edge</strong> exposes them to the web; Chrome
and Firefox see only the classic SAPI voices (David, Zira), so on Windows use Edge for
the best voice.</li>
<li><strong>iPhone / iPad / Android</strong> — the phone&#39;s own voices work as they are; iOS gets better
ones under <em>Settings → Accessibility → Spoken Content → Voices</em>.</li>
</ul>
<p>Whichever voice you pick, <strong>Playback speed</strong> (Settings → Status → Voice) speaks at 0.8× to
2×, and the <strong>Mic &amp; speaker check</strong> on the same page records three seconds, plays them
back, and shows the transcript — the fastest way to tell a microphone problem from a
voice problem.</p>
<h3 id="voice-on-the-phone">Voice on the phone</h3>
<p>Dictation, live words, spoken replies, the modes, and the wake word all work on the phone
exactly as on the Desktop. The settings behind them — wake word, playback speed, the
Short/Medium/Long default — are set on the Desktop under <strong>Settings → Status → Voice</strong>,
because Settings is Desktop-only. Replies speak with the phone&#39;s own voices, even offline.</p>
<h2 id="what-the-assistant-can-do">What the assistant can do</h2>
<p>These are the tools it can reach for. It picks them itself; you decide whether they run.</p>
<p><strong>Observe — runs on its own, reads only:</strong></p>
<table>
<thead>
<tr>
<th>Tool</th>
<th>What it does</th>
</tr>
</thead>
<tbody><tr>
<td>Search knowledge</td>
<td>Finds passages across your documents, or inside one named document.</td>
</tr>
<tr>
<td>Read a document</td>
<td>Reads a document&#39;s text, a window at a time.</td>
</tr>
<tr>
<td>Summarize a document</td>
<td>Summarizes a document of any length, whole or on a topic you name.</td>
</tr>
<tr>
<td>List documents</td>
<td>Lists what&#39;s in your knowledge base.</td>
</tr>
<tr>
<td>List tasks</td>
<td>Reads your planner.</td>
</tr>
<tr>
<td>List schedules</td>
<td>Reads your schedules.</td>
</tr>
<tr>
<td>Read schedule output</td>
<td>Reads what recent scheduled runs produced.</td>
</tr>
</tbody></table>
<p><strong>Reviewed — proposed, then waits for your approval. Can be pre-authorized:</strong></p>
<table>
<thead>
<tr>
<th>Tool</th>
<th>What it does</th>
</tr>
</thead>
<tbody><tr>
<td>Save a note</td>
<td>Writes a new document into your knowledge.</td>
</tr>
<tr>
<td>Remember a fact</td>
<td>Adds a fact to Settings → Memory.</td>
</tr>
<tr>
<td>Add a task</td>
<td>Adds a planner task. Asking twice for the same thing won&#39;t duplicate it.</td>
</tr>
<tr>
<td>Complete a task</td>
<td>Ticks a task off; a recurring one rolls forward.</td>
</tr>
<tr>
<td>Update a task</td>
<td>Edits a task&#39;s title, date, time, priority, repeat or notes. (Tags are yours to set in Planner; the assistant can&#39;t change them.)</td>
</tr>
<tr>
<td>Search the web</td>
<td>Searches with your configured engine.</td>
</tr>
<tr>
<td>Fetch a page</td>
<td>Reads one public web page as article text.</td>
</tr>
<tr>
<td>Research the web</td>
<td>Searches, then reads the top results, in one step.</td>
</tr>
<tr>
<td>Add a URL to knowledge</td>
<td>Fetches a page or PDF and saves its text.</td>
</tr>
<tr>
<td>List email</td>
<td>Lists recent inbox messages, without bodies.</td>
</tr>
<tr>
<td>Read an email</td>
<td>Reads one message.</td>
</tr>
<tr>
<td>Create a schedule</td>
<td>Adds a recurring prompt.</td>
</tr>
<tr>
<td>Update a schedule</td>
<td>Edits one.</td>
</tr>
<tr>
<td>Enable or pause a schedule</td>
<td>Turns one on or off.</td>
</tr>
</tbody></table>
<p><strong>Irreversible — always asks, every time, with an extra confirmation:</strong></p>
<table>
<thead>
<tr>
<th>Tool</th>
<th>What it does</th>
</tr>
</thead>
<tbody><tr>
<td>Send an email</td>
<td>Sends from the connected Gmail account.</td>
</tr>
<tr>
<td>Delete a task</td>
<td>Permanently deletes a planner task.</td>
</tr>
<tr>
<td>Delete a schedule</td>
<td>Permanently deletes a schedule and its run history.</td>
</tr>
</tbody></table>
<p>Two details worth knowing. First, a turn is bounded: the assistant gets <strong>eight tool steps</strong>
and then must write an answer from what it has, saying what it couldn&#39;t finish — it can&#39;t
loop forever. Second, the three schedule-writing tools <strong>always ask inside a scheduled run</strong>,
even if you pre-authorized them in chat, so a schedule can never quietly grow more schedules.</p>
<h2 id="knowledge">Knowledge</h2>
<p>A private, encrypted knowledge base. There are three ways in, all on the <strong>Knowledge</strong> page:</p>
<ul>
<li><strong>Drop in files.</strong> Drag them onto the box, or click it to choose. <strong>PDF, Word (.docx),
PowerPoint (.pptx), Excel (.xlsx), HTML, Markdown, CSV, JSON, and plain text</strong> are
understood — up to 200 files in one drop, 25 MB each.</li>
<li><strong>Paste a URL.</strong> SmartBrain fetches the page, extracts the article text (not the
navigation and ads around it), and saves that. A URL pointing at a PDF works too. You can
ask Chat to do the same: <em>&quot;add this PDF to my knowledge: …&quot;</em>.</li>
<li><strong>Write a note.</strong> A title and some text, typed straight in.</li>
</ul>
<p><strong>Big documents are welcome</strong>: a several-hundred-page PDF is fine, and roughly a thousand
dense pages of text are stored and reachable per document. Uploads don&#39;t block — they land
right away, keyword search works within seconds, and meaning-search for a very large
document fills in over the next few minutes in the background (it resumes by itself after a
restart). While that is happening the page says so: <em>&quot;Indexing for meaning search — 4 of 9
done. Keyword search already finds them.&quot;</em></p>
<p>Adding the same content twice is a no-op — SmartBrain recognises it and keeps the one copy
rather than cluttering your results with duplicates.</p>
<p><strong>What it can&#39;t read.</strong> There is no OCR and no image or audio support. A scanned PDF — one
that is pictures of pages rather than text — has no text to extract, so it is refused with
<em>&quot;no readable text found in that file&quot;</em> rather than silently added empty. Word files get no
page numbers either: <code>.docx</code> has no fixed pagination, so citations into one name the
document but not a page.</p>
<p>Search your knowledge three ways:</p>
<ul>
<li><strong>Best</strong> (default) — combines both of the below. Keyword search nails an exact name
or invoice number; meaning search finds a paraphrase. Each misses what the other
catches, so fusing them beats either alone.</li>
<li><strong>Keyword</strong> — ranks by relevance: rare words count for more, and a long document
can&#39;t win just by being long. Needs no model at all.</li>
<li><strong>Meaning</strong> — matches by sense rather than wording, using an
<a href="#models">embedding model</a>.</li>
</ul>
<p><strong>Results are citations.</strong> Every hit shows where it came from — <em>&quot;Lease.pdf · p.12&quot;</em>
(a slide deck cites <em>slide 3</em>, a spreadsheet <em>sheet 2</em>) — and clicking it opens the
document <strong>at the passage that matched</strong>, highlighted, rather than at the top. Chat
answers that used your knowledge show the same source chips underneath the reply —
click one to open the document at the cited passage. The chips come from what the
assistant actually searched and read, not from what it <em>says</em> it did, so you can
check any claim against the original.</p>
<p><strong>Organize with tags.</strong> Every document (and vault) has an inline tag editor — click the
tags line on a row to add or change them, and click any tag chip to filter the list to it.
Editing tags is instant and never re-indexes the document. Up to 20 tags per document.</p>
<p><strong>Each row also has Rename and Delete</strong>, and a checkbox for selecting documents — put the
selection in a vault, <strong>tag them all at once</strong>, or <strong>delete them all at once</strong> from the bar
that appears. Renaming re-indexes the document in the background, because the title is part
of what search matches on; tagging doesn&#39;t. Documents that came from someone else&#39;s vault
refuse bulk edits individually (a publisher update could overwrite them) and the result
says so honestly — detach a copy to make it yours first.</p>
<p><strong>Instant summaries.</strong> In the background SmartBrain builds a summary of every document —
summaries of its parts, reduced into a summary of the whole. That&#39;s what makes <em>&quot;summarize
this&quot;</em> answer immediately even on a book-length file, and what lets a focused question
(&quot;summarize the fees&quot;) be answered in seconds instead of a full re-read. The page shows the
progress: <em>&quot;Preparing instant summaries — 6 of 9 documents ready.&quot;</em> It is built a piece at a
time, resumes after a restart, and steps aside whenever you are chatting.</p>
<p><strong>Reindex (semantic)</strong> at the top of the document list re-embeds anything that needs it.
Use it after you change the embedding model, or if a document you know is there never turns
up in Meaning search. It works in batches and tells you what&#39;s left: <em>&quot;Indexed 12
document(s) — 30 still to go, continuing in the background.&quot;</em></p>
<p><strong>Try it:</strong> open <strong>Knowledge</strong>, drag in a document, and search it. Then ask <strong>Chat</strong>
<em>&quot;what does my knowledge say about …&quot;</em> — the assistant searches it for you and tells you
which file and page it got the answer from.</p>
<p><img src="assets/05-knowledge.png" alt="The Knowledge page: add a document, then search it"></p>
<p><img src="assets/gifs/04-add-knowledge.gif" alt="Drop in a file, search it, open the cited passage, then ask Chat — answers cite their sources"></p>
<blockquote>
<p>Semantic search needs an embedding model. If results say <em>&quot;Showing keyword
results&quot;</em>, set one up — see
<a href="#models__embeddings-for-knowledge-search">Embeddings</a> — then <strong>Reindex</strong>.</p>
</blockquote>
<p>Your knowledge is also what external tools can read over <a href="#mcp">MCP</a>.
Group documents into <strong>vaults</strong> to scope a search — and to share them, privately
or publicly: see <a href="#vaults">Share knowledge with Vaults</a>.</p>
<h3 id="follow-websites-feeds">Follow websites (feeds)</h3>
<p>Any site that publishes an <strong>RSS or Atom feed</strong> — most blogs, news sites, and release
pages do — can fill your knowledge by itself. On the <strong>Knowledge</strong> page, open <em>Follow a
website</em>, paste the feed URL, and Subscribe. Add tags there (optional, comma-separated)
and <strong>every article the feed ever saves carries them</strong> — so one click on the tag chip
filters your whole library to that subject. The subscription gets its own vault, and
every new post lands there as a searchable, citable document — so <em>&quot;what did that blog
say about X?&quot;</em> works in Chat, and a schedule (below) can summarize the week&#39;s posts for
you. What lands is what the feed carries: usually the title, link, and summary — some
feeds include the full article, many don&#39;t.</p>
<p>SmartBrain checks each feed <strong>about every six hours, directly from this machine</strong> — no
server in the middle — and articles are encrypted at rest like every other document.
Posts it has already saved are recognised and skipped, so a feed never duplicates itself.
<strong>Refresh</strong> on a feed&#39;s row checks right now, and the row always shows when it last
checked and what happened. Only the public URLs you pasted yourself are ever fetched.</p>
<p><strong>Unsubscribe</strong> stops the checking and removes the feed&#39;s vault. Its saved articles are
your documents and stay in your knowledge — unless you choose <em>Delete articles too</em>.</p>
<h2 id="planner">Planner</h2>
<p><img src="assets/gifs/06-planner.gif" alt="Planner — tasks grouped Today / This week / by due date"></p>
<p>Task tracking, deliberately plain. A task is a title plus, if you want them:</p>
<ul>
<li>a <strong>due date</strong> and a <strong>time</strong> on that date;</li>
<li>a <strong>priority</strong> — Low, Medium (the default), or High;</li>
<li>a <strong>repeat</strong> — none, Daily, or Weekly. Completing a repeating task rolls it forward to
the next occurrence instead of closing it;</li>
<li><strong>tags</strong>, comma-separated, and free-text <strong>notes</strong>.</li>
</ul>
<p>Tasks group themselves by when they are due: <strong>Today &amp; overdue</strong>, <strong>This week</strong>, <strong>Later</strong>,
<strong>No date</strong>, and <strong>Done</strong>. Anything overdue is called out in red. Each row has a checkbox to
tick it off, <strong>Edit</strong> to change any field, and <strong>Delete</strong>.</p>
<p>The assistant can read your tasks freely, and can add, complete, or edit one with your
approval. Deleting a task is irreversible, so it asks every time.</p>
<h2 id="schedules">Schedules</h2>
<p><img src="assets/gifs/07-schedule-a-prompt.gif" alt="Schedules — run a prompt on a timer, then Run now"></p>
<p>Run a prompt on a timer — e.g. &quot;every morning, summarize my open tasks.&quot; A
schedule fires an assistant turn on its cadence.</p>
<p>The page has two tabs and opens on <strong>Items</strong>. <strong>Create</strong> takes a name, the prompt itself, how often it should
<strong>Repeat</strong> — <strong>Once</strong>, <strong>Hourly</strong>, <strong>Daily</strong>, or <strong>Weekly</strong> — and when it should <strong>First
run</strong>: <strong>Now</strong>, <strong>In 1 hour</strong>, or <strong>Tomorrow</strong>. Three presets (Check the news, Morning
briefing, Weekly knowledge review) fill the form in if you&#39;d rather start from one.</p>
<p><strong>Items</strong> lists what you have. Each row has a checkbox that enables or pauses it, <strong>Edit</strong>
to change the prompt or cadence, <strong>Run now</strong> to fire it immediately, and a delete button.</p>
<p>Two things to know:</p>
<ul>
<li>Schedules only run <strong>while the app is unlocked</strong> (a locked vault can&#39;t decrypt
or act — there&#39;s no background access to your data).</li>
<li>If a scheduled run wants to do something <strong>dangerous</strong> (send, delete, etc.), it
<strong>parks for your approval</strong> in Activity just like in chat — it won&#39;t act alone.</li>
</ul>
<p>A run&#39;s output lands in three places: <strong>in your open Chat</strong> as a &quot;Scheduled Item&quot;
notice, as a durable copy on the <strong>Info</strong> page, and as a badge on the Chat tab while
results are unseen.</p>
<h2 id="info">Info</h2>
<p>Where scheduled output is kept. The <strong>All</strong> tab lists every run across every schedule,
newest first; there is a tab per schedule for just that one&#39;s output, and a <strong>Refresh</strong>
button. Each entry shows when it ran and what it produced — or, if the run wanted approval
for something, <em>&quot;Awaiting your approval — open Activity to review.&quot;</em></p>
<p>Nothing here is editable. It&#39;s the record: Chat&#39;s notice is easy to scroll past, so this is
where you go when you want to find last Tuesday&#39;s briefing again. Manage the schedules
themselves on the <strong>Schedules</strong> page.</p>
<h2 id="email-gmail">Email (Gmail)</h2>
<p>Connect a Gmail account with <strong>your own</strong> Google OAuth client. The whole flow is
loopback-only — the authorization happens on your machine and nothing leaves it except
the calls to Google. SmartBrain asks for just two scopes: <strong>read</strong> and <strong>send</strong> (no
archive, delete, or label changes). It&#39;s optional; most people run SmartBrain without it.</p>
<p><strong>One-time setup</strong> (the in-app <strong>Email</strong> page walks you through these):</p>
<ol>
<li>Open <a href="https://console.cloud.google.com/apis/credentials">Google Cloud Console → Credentials</a>,
then <strong>Create credentials → OAuth client ID</strong>, and choose type <strong>Desktop app</strong>. A Desktop-app
client needs <strong>no redirect URL</strong> — Google handles loopback automatically.</li>
<li>On the <strong>OAuth consent screen</strong>, add the <code>gmail.readonly</code> and <code>gmail.send</code> scopes and set
<strong>Publishing status</strong> to <strong>In production</strong> — otherwise Google signs you out every 7 days.</li>
<li>In the app&#39;s <strong>Email</strong> page, paste the client <strong>ID</strong> and <strong>secret</strong> and click <strong>Connect Gmail</strong>.
A Google sign-in opens; if it warns the app is &quot;unverified&quot; (it&#39;s your own client), choose
<strong>Advanced → Continue</strong>, then approve the two scopes.</li>
</ol>
<p>Once connected, the <strong>Email</strong> page shows which address you&#39;re connected as, a <strong>Compose</strong>
form (to, subject, message, <strong>Send</strong>), and your recent <strong>Inbox</strong> — click a message to read
it in full. <strong>Disconnect</strong> removes the connection.</p>
<ul>
<li><strong>You</strong> sending from the app is a direct action.</li>
<li>The <strong>assistant</strong> sending email is an <strong>Irreversible</strong> tool — it always parks
for your approval first. It can draft; you approve the send. It can also list and read
your recent mail, both of which wait for approval the first time.</li>
</ul>
<p>Google sometimes signs SmartBrain out — every 7 days if you left the OAuth consent screen in
testing rather than setting <strong>Publishing status</strong> to <em>In production</em>, and occasionally even
if you didn&#39;t. The page then says <strong>&quot;Gmail needs reconnecting&quot;</strong> with a one-click
<strong>Reconnect Gmail</strong>; you don&#39;t re-enter the client ID or secret. Reconnecting is done on the
Desktop, and a paired phone starts working again by itself afterwards.</p>
<h2 id="memory">Memory</h2>
<p><strong>Settings → Memory</strong> holds who the assistant is for. Four things live there:</p>
<ul>
<li><strong>Assistant name</strong> — what it calls itself.</li>
<li><strong>Your name</strong> — what it calls you.</li>
<li><strong>Custom instructions</strong> — standing guidance for every conversation, e.g. <em>&quot;Be concise.
Prefer metric units.&quot;</em></li>
<li><strong>Remembered facts</strong> — a list you add to with <strong>Remember</strong> and prune with <strong>Forget</strong>. The
assistant can propose one too, with your approval.</li>
</ul>
<p>All of it is encrypted and composed into every conversation, so it&#39;s the place to look when
you wonder &quot;why does it keep doing that?&quot; — including any <em>&quot;(learned) …&quot;</em> facts
self-improvement added (delete one to permanently reject it).</p>
<h2 id="web-search">Web search</h2>
<p>The assistant&#39;s web tools search with <strong>DuckDuckGo by default — no key needed</strong>. Under
<strong>Settings → Web search</strong> you can pick which engine to use:</p>
<ul>
<li><strong>Automatic</strong> (the default) — the first engine you have configured, with DuckDuckGo
last. If one is down, the next takes over.</li>
<li><strong>SearXNG</strong> — an instance you host or trust. Paste its URL; its JSON API must be on.</li>
<li><strong>Brave Search</strong> or <strong>Tavily</strong> — bring your own key. Both are stored encrypted, like
cloud-provider keys.</li>
<li><strong>DuckDuckGo</strong> — no key, always available as the fallback.</li>
</ul>
<p>Searches only happen when the assistant actually uses the web tools in a turn; see
<a href="#privacy-security">Privacy &amp; security</a> for exactly what leaves your machine.</p>
<h2 id="self-improvement">Self-improvement</h2>
<p>SmartBrain can review its own recent performance and carefully improve — <strong>off by
default</strong>, and switched on under <strong>Settings → Self-improvement</strong>. On the cadence you
choose — every 2, 4, 8 (default), or 24 hours — under Settings → Self-improvement (while
unlocked) it scores Chat, Knowledge, and Tools from private, on-device telemetry. Quiet
periods stay silent; when something needs attention you get a short digest in the chat
feed. From a flagged period it may act — always within hard bounds:</p>
<ul>
<li><strong>Learned preferences</strong> — a local model (never a cloud one, and only from messages
<em>you</em> wrote) may learn one durable preference, applied as a visible <em>&quot;(learned) …&quot;</em>
fact in Settings → Memory, measured against your satisfaction, and <strong>auto-reverted if
it doesn&#39;t help</strong>. Deleting the fact yourself permanently rejects it.</li>
<li><strong>Suggested routines</strong> — an ask you repeat on a daily/weekly rhythm becomes a
ready-made schedule <strong>waiting for your approval in Activity</strong>; decline it once and it
is never offered again.</li>
<li><strong>Knowledge gaps</strong> — searches your knowledge couldn&#39;t answer get named in the digest.</li>
<li><strong>Prompt optimizer</strong> (its own switch) — learns how kinds of requests go and may steer
them with a short guidance note; a strategy watches in <em>shadow</em> first, goes live only
after a measured trial, is turned off automatically if it doesn&#39;t help, and guided
answers always show a small <strong>&quot;guided · …&quot;</strong> chip.</li>
</ul>
<p>One change is ever on trial at a time, everything is reversible, every applied or
reverted change is announced, and <strong>Settings → Self-improvement</strong> shows the record of
what it has done under <strong>What it has done</strong>.</p>
<h2 id="usage-cost">Usage &amp; cost</h2>
<p>A running estimate of what your <strong>cloud</strong> models cost. Pick a <strong>Range</strong> — Today (the
default), the last 5, 10 or 30 days, or a custom pair of dates — and you get a row per model
with its calls, prompt and completion tokens, and estimated cost, plus a total. Pricing
comes from each provider&#39;s live figures. <strong>Local models (Ollama, MLX) are free</strong> and say
<code>free</code> in the cost column.</p>
<p>Usage appears here after you chat with a model. None of it leaves your machine — it&#39;s
computed locally from your own token counts, and the only network call is a local fetch of
the price list from the on-device gateway.</p>
<h2 id="activity">Activity</h2>
<p><img src="assets/gifs/05-approve-an-action.gif" alt="The safety loop — the assistant proposes, you approve in Activity"></p>
<p>Your audit and approvals view. Two parts:</p>
<ul>
<li><p><strong>Awaiting your approval</strong> — a card per proposed action, naming the tool, what it would
do, and whether it is reversible (the same card appears in the chat itself, and
resolving it there is identical). <strong>Approve</strong> or <strong>Deny</strong> it. <strong>Always allow</strong> approves it
and stops asking for that tool from then on (for the URL tools, for that tool <strong>on that
site</strong> — the list shows each allowed site as its own row). Denying an action holds for
the rest of that run: the assistant is told, and an identical retry is refused instead
of asking you again; anything pre-authorized this way is listed
under <strong>Always allowed</strong>, where <strong>Stop allowing</strong> takes the permission back. Irreversible
tools can&#39;t be pre-authorized — they ask every time, with an extra confirmation. When the
action you resolve belongs to a <strong>scheduled</strong> run, the run finishes on the spot and its
answer lands in the Scheduled updates feed — no need to trigger the schedule again.</p>
<p><img src="assets/08-always-allowed.png" alt="The Always allowed list on the Activity page — a pre-authorized tool with its Stop allowing button"></p>
</li>
<li><p><strong>History</strong> — the record of every tool the assistant ran or tried to run: which tool, its
risk tier, what you decided, whether it succeeded, when, and a summary of what it was
given. Any error it hit is shown too. Arguments and results are encrypted at rest, and
secrets are stripped before anything is recorded.</p>
</li>
</ul>
<p>Nothing here can be edited or deleted from inside the app — see
<a href="#design-limits">Design limits</a> for what that does and doesn&#39;t guarantee.</p>
<p>An action left unanswered <strong>expires after an hour</strong>, and <strong>locking cancels everything
pending</strong> — in both cases the action never runs at all. When you deny one instead, the
assistant is told it wasn&#39;t approved and carries on from there; it is never told an action
succeeded when it didn&#39;t.</p>
<h2 id="next">Next</h2>
<ul>
<li><a href="#vaults">Share knowledge with Vaults</a> — sealed shares, public publishing, subscriptions.</li>
<li><a href="#mcp">Connect external tools</a> via MCP.</li>
<li><a href="#backup-recovery">Backup &amp; recovery</a>.</li>
<li><a href="#design-limits">Design limits</a> — why some of the boundaries above are where they are.</li>
</ul>
`},{slug:`vaults`,title:`Share knowledge with Vaults`,html:`<h1 id="share-knowledge-with-vaults">Share knowledge with Vaults</h1>
<p><img src="assets/gifs/10-vaults.gif" alt="Vaults — tick documents into a vault, then publish it public: the no-key warning, a Public badge with your SB-… publisher fingerprint, and a version that bumps each time you export an update"></p>
<p><img src="assets/gifs/11-vault-subscribe.gif" alt="Subscribe to a public vault by URL, then pull the publisher&#39;s verified updates — the docs land re-encrypted under your key, a keyword search hits, you make one copy yours with Detach, and Update now applies v2 all-or-nothing while keeping your copy"></p>
<p>A <strong>vault</strong> is a named set of your knowledge documents — the unit you scope a search to,
and the unit you share. Vaults live on the Knowledge page.</p>
<ul>
<li><strong>Create one and add documents.</strong> Tick documents in your list, then add them to a new or
existing vault — or click <strong>Add documents</strong> on the vault itself and it walks you to the list. A document can belong to several vaults; adding it to a vault never moves
or copies the file, and deleting a vault never deletes its documents — it only removes the
grouping.</li>
<li><strong>See what&#39;s inside.</strong> Click the document count on a vault to list its contents — open any of
them, or remove one from the vault (the document itself is kept).</li>
<li><strong>Search inside one.</strong> Pick a vault next to the search box to search <em>only</em> its documents
— e.g. keep a &quot;Work&quot; vault and a &quot;Home&quot; vault and ask each separately.</li>
<li><strong>Share it.</strong> Choose <strong>Share…</strong> on the vault and SmartBrain seals it into a single <code>.sbvault</code> file and
shows you a one-time key (starting <code>SBVK1-</code>). Send the file however you like, then give the
person the key over a <strong>different</strong> channel — together they are the contents in the clear,
so keep them apart. <strong>Every sealed re-share mints a fresh key</strong>: the moment you seal again,
anyone holding the previous key can no longer open the new file. The share panel warns
before AND after, and the previous file they already opened is unaffected — the rotation
only bites the <em>next</em> one.</li>
<li><strong>Share it publicly.</strong> Choose <strong>Public</strong> in the share panel instead: the export is the same
<code>.sbvault</code> file with <strong>no key at all</strong> — anyone with the link can read everything in this
vault, and there is <strong>no taking it back</strong>. Upload the file anywhere (Drive, S3, any web host)
and share the link — or unzip it and upload the folder to a static host so future updates only
re-upload what changed. Once published, the vault card shows a <strong>Public v<em>N</em></strong> badge beside your
publisher fingerprint (<code>SB-…</code>) — the identity and version readers will see. The file is still
signed, so nobody else can publish an &quot;update&quot; to your vault in your name.</li>
<li><strong>Publish updates.</strong> Export the vault again (choose <strong>Public</strong>) and replace the hosted file
with the new one — subscribers pick the update up on their next check. The version bumps
automatically, and the button reads <strong>Export update (v<em>N</em>)</strong> so you know where it lands. If
you export twice with <strong>no content change</strong>, the share panel says so — <em>&quot;Nothing changed
since v</em>N* — you published an identical version.&quot;* — before you distribute a file that would
look like an update but ship no changes. Between publishes, any local edits (renames, added
documents, sealed re-shares) show on the card as <strong>Unpublished changes</strong> — the muted chip
that says your working copy has moved past the last public version and a re-export would
ship the difference.</li>
<li><strong>Remember where you put it, and verify the hosted copy.</strong> Below the hosting hint in the
Share panel, a <strong>Hosted at</strong> row lets you paste the URL you uploaded the <code>.sbvault</code> to
(localhost and LAN addresses are refused — public internet only, <code>http(s)</code>, same rule
subscribers see). It&#39;s a note <em>this install</em> keeps — it doesn&#39;t travel with the vault.
Once saved, <strong>Verify hosted copy</strong> fetches the file at that URL and checks it against your
own key and your last publish — the verdicts are plain:<ul>
<li><em>&quot;the hosted file matches what this install last published (v</em>N*)&quot;* — you&#39;re good.</li>
<li><em>&quot;the hosted file is v</em>N*, but this install has published up to v<em>M</em> — did you forget to
upload the new file?&quot;* — the classic gap this row catches.</li>
<li><em>&quot;the hosted file is NEWER (v</em>N*) than this install&#39;s record (v<em>M</em>) — was it published
from another machine?&quot;* — an anomaly worth pausing on.</li>
<li><em>&quot;the hosted file&#39;s signature isn&#39;t yours — it is signed by SB-…&quot;</em> — someone else is
publishing at that URL; the check never touches your subscription state, so this is safe
to run.</li>
<li><em>&quot;upstream returned HTTP 410&quot;</em> / a timeout / a 404 — reachable=false, the honest network
fact. A manual verify is a Desktop-only action (like exporting), for the same reason: it
names your publisher identity in its verdict.</li>
</ul>
</li>
<li><strong>Retire the vault.</strong> Choose <strong>Share → Retire…</strong> on a public vault to close the channel:
SmartBrain produces one final, dated <code>.sbvault</code> marked <em>retired</em> — upload it in place of
the current file. Subscribers apply that last version, then move into a <strong>Retired by
publisher</strong> state that drops out of their auto-checks. <strong>Their documents stay in their
Knowledge and remain readable</strong> — retirement stops the update channel, it doesn&#39;t reach
back and take your documents. The card on your side flips its Public chip to a muted
<strong>Retired v<em>N</em></strong> — that&#39;s the version subscribers pinned against. If you change your mind
later, publish again from the same install (same publisher key): the next normal export
un-retires the vault for every subscriber whose install picks it up.</li>
<li><strong>Remove content from every subscriber (the destructive path).</strong> Regular retirement leaves
everyone&#39;s documents intact. If you actually need a document <em>gone</em> from subscribers&#39;
Knowledge — a mistake, a takedown request — remove it from the vault and publish an update
(or publish an <em>empty</em> vault to remove everything). On a subscriber&#39;s next update the
documents you removed are deleted from their Knowledge — <strong>only the imported copies</strong>.
Anything they authored themselves is never touched, and anything they explicitly claimed
as theirs with <strong>Detach</strong> is theirs — updates skip it. Use this deliberately: a subscriber
who&#39;s already read a document can&#39;t be made to un-read it, and a subscriber who&#39;s offline
won&#39;t see the removal until they come back and update.</li>
<li><strong>Delete a subscription (the reader&#39;s side).</strong> Deleting a subscribed vault card asks what
to do with its documents: <strong>Keep documents</strong> (the historical default — the grouping goes,
the documents stay in Knowledge) or <strong>Also remove the vault&#39;s imported documents</strong> (the
imported copies are shredded too). Anything you authored yourself stays either way, and
anything you&#39;d detached is yours — those always survive.</li>
<li><strong>What subscribers see if things go wrong.</strong> The card is honest about state:<ul>
<li><strong>Unreachable — the publisher took this vault down.</strong> The host returned HTTP 410 Gone
(an intentional takedown). Auto-update stops; a manual <strong>Check for updates</strong> will still
try, and a success clears the flag.</li>
<li><strong>Unreachable — the host hasn&#39;t answered for a week.</strong> Eight consecutive failures over
≥ 7 days. Same posture: auto-update stops, a manual check still runs, a success clears
the flag.</li>
<li><strong>Blocked.</strong> The publisher&#39;s signing key changed. Updates refuse (never silently
accept), and the card shows <strong>Pinned (trusted)</strong> and <strong>Offered (new)</strong> fingerprints side
by side; only a Desktop <strong>Trust new key</strong> with your passphrase moves the pin — the app
never re-pins on a timer.</li>
</ul>
</li>
<li><strong>Every publish is dated.</strong> Each open export stamps a UTC calendar date the manifest
carries; on a subscriber&#39;s card the date appears as <strong>published <em>YYYY-MM-DD</em></strong>. It&#39;s a
small thing that answers a large question: &quot;when was this actually written?&quot;</li>
<li><strong>Import someone else&#39;s.</strong> Pick the <code>.sbvault</code> file and paste the key. Its documents are
<strong>re-encrypted under your own passphrase</strong> as they land (nothing you import can read or
weaken your data), and anything you already have is kept as-is rather than overwritten. The
result shows the publisher&#39;s fingerprint — the one thing that says <em>who</em> the knowledge came
from. Imported documents are protected from accidental edits (rename/delete are refused);
<strong>Detach</strong> one in the vault&#39;s member list to make that copy yours.</li>
<li><strong>Subscribe to a public vault.</strong> For a vault someone published <strong>Public</strong>, paste its URL
instead of picking a file — no key needed. Link the <code>.sbvault</code> file itself, or — if the
publisher hosts the unzipped folder on a static host — its <code>manifest.json</code>. SmartBrain fetches
it (public internet hosts only, not localhost or LAN addresses), verifies the publisher&#39;s
signature, and re-encrypts the documents under <strong>your</strong> passphrase as they land. The
publisher&#39;s identity is <strong>pinned on first contact</strong> — the vault card shows a <strong>Subscribed</strong>
badge with the pinned fingerprint and the host it came from — and future updates will only
ever be accepted from that same publisher.</li>
<li><strong>Keep a subscription up to date.</strong> Click <strong>Check for updates</strong> on a subscribed vault; when the
publisher has published a newer version, <strong>Update now</strong> fetches it, verifies everything against
the pinned publisher identity, and applies it all-or-nothing — you are never left half-updated.
Changed documents are updated <strong>in place</strong>, so citations and links to them keep working; new
ones are added, and ones the publisher removed are deleted. <strong>Anything you edited stays yours</strong>:
the update reports it as &quot;kept&quot; instead of overwriting it (same for documents you already had —
your copy wins). On a <code>manifest.json</code> (folder) host only the changed files are downloaded; a
single-file host re-downloads the whole file, and the card notes so. The card also shows how
long ago it was last checked and flags a failed check (&quot;host may be unreachable&quot;), so a dead or
stale host is easy to spot. If the
publisher&#39;s <strong>key ever changes</strong>, updates stop with a warning showing both fingerprints — pinned
(trusted) and offered (new), side by side — until you confirm the new key with the publisher
out-of-band and choose <strong>Trust new key</strong> (Desktop + passphrase). A newer <code>.sbvault</code> <em>file</em> of a subscribed vault also applies as an
update — importing it never creates a duplicate.</li>
<li><strong>Scheduled auto-update (opt-in).</strong> Turn on <strong>Auto-update</strong> on a subscribed vault card and pick a
cadence (daily or weekly) to have SmartBrain check and apply clean updates for you. It is <strong>off by
default</strong>, runs <strong>only on the Desktop while unlocked</strong>, and <strong>never applies a publisher key change
on its own</strong> — a changed key still blocks and waits for you to confirm it. Each run reports what it
did <strong>in the chat feed</strong> (&quot;updated to v3 — 2 documents changed&quot;, or a &quot;new publisher key&quot; notice).</li>
</ul>
<p><strong>Try it now — the official example vault.</strong> This user guide is itself published as a public
vault. Use <strong>Subscribe to a public vault</strong> and paste
<code>https://smartbrain.securecloudgroup.com/vaults/smartbrain-docs.sbvault</code> — on first subscribe
you&#39;ll see the publisher fingerprint being pinned; ours is <code>SB-3WZM-7CEI-GPJ7-3MLC</code>. If it
matches, you&#39;re talking to us. The whole guide lands in your Knowledge, searchable and askable,
and new versions are offered as updates whenever the docs change — the card shows the date the
version you&#39;re reading was published, so you can see at a glance how current it is.</p>
<p><strong>What travels.</strong> A vault export carries the documents themselves — their titles and text —
and nothing else. Your <strong>tags do not travel</strong>: they&#39;re your labels, not the publisher&#39;s, and
they stay behind. For the same reason you can&#39;t tag the copies an imported vault gave you; a
future update would overwrite them. <strong>Detach</strong> a copy first if you want to make it yours and
label it.</p>
<p><strong>Reading the card.</strong> A vault card carries chips for whatever applies:</p>
<ul>
<li><strong>Private</strong> — a local vault you haven&#39;t shared. The default and the positive indicator, not
the absence of one.</li>
<li><strong>Shared · sealed</strong> — you&#39;ve sealed-shared this at least once; the receiver needs the key.</li>
<li><strong>Public v<em>N</em></strong> — the version you last published open. Beside it: your publisher
fingerprint (<code>SB-…</code>) — the identity subscribers pin.</li>
<li><strong>Unpublished changes</strong> — your working copy has moved past <strong>Public v<em>N</em></strong>. Re-export to
ship the difference.</li>
<li><strong>Retired v<em>N</em></strong> — you retired this vault at that version. Muted, because it&#39;s a done
state; publish again to un-retire.</li>
<li><strong>Imported</strong> — the vault arrived as a <code>.sbvault</code> file (not a URL subscription); the pinned
fingerprint is beside it either way.</li>
<li><strong>Subscribed</strong> — a live URL subscription. Beside it: the pinned fingerprint, the version
you have, the host it came from, and — from the publisher&#39;s own manifest — <strong>published
<em>YYYY-MM-DD</em></strong>.</li>
<li><strong>Blocked</strong> — the publisher&#39;s key changed; updates refuse until you trust it out-of-band.</li>
<li><strong>Retired by publisher</strong> — the publisher retired this vault. Documents stay in your
Knowledge, checking stops.</li>
<li><strong>Unreachable</strong> — the host stopped responding (taken down or dead for a week); auto-update
stops, a manual check still runs.</li>
</ul>
<p>Checking a subscription tells you where you stand — <em>&quot;Up to date (v3).&quot;</em> or <em>&quot;Update
available (v3 → v4).&quot;</em></p>
<p>Creating a vault, adding documents to it, and searching inside it work everywhere, including
a paired phone. <strong>Exporting, importing, subscribing, and trusting a publisher&#39;s changed key
are done on the Desktop</strong> — sharing a vault&#39;s contents, bringing new ones in, and deciding
whom to trust are all sensitive, so those actions live in the Desktop app.</p>
<h2 id="next">Next</h2>
<ul>
<li><a href="#features">Using SmartBrain_3000</a> — the Knowledge page these vaults live on.</li>
<li><a href="#mcp">Connect external tools</a> — imported vault content is labeled with its
provenance there too.</li>
<li><a href="#privacy-security">Privacy &amp; security</a> — what a subscription fetches, and when.</li>
</ul>
`},{slug:`mcp`,title:`Connect external tools (MCP)`,html:`<h1 id="connect-external-tools-mcp">Connect external tools (MCP)</h1>
<p>SmartBrain_3000 is also an <strong>MCP server</strong> — it can expose your <strong>Knowledge base
(read-only)</strong> to a desktop AI client (e.g. Claude Desktop, Cursor). The
tool reads your knowledge to ground its answers; it can&#39;t change anything.</p>
<h2 id="turn-it-on">Turn it on</h2>
<p>Open <strong>Settings → Connections (MCP)</strong> and click <strong>Generate token</strong>. MCP is <strong>off until a
token exists</strong> — generating one enables it. The page then shows the endpoint and the token,
with <strong>Copy token</strong>, <strong>Regenerate</strong> (mints a new one and invalidates the old), and
<strong>Revoke</strong> (turns access off again). Managing the token is Desktop-only; a paired phone
can&#39;t read or change it.</p>
<p><strong>SmartBrain has to be unlocked.</strong> The token authorizes the connection, but the knowledge
base is encrypted — while the app is locked, a client&#39;s calls are refused with <em>&quot;SmartBrain
is locked; unlock it to use the knowledge base&quot;</em>.</p>
<p>By default the endpoint is loopback-only:</p>
<pre><code>http://localhost:33000/mcp/
</code></pre>
<p>Every request must include the token as a bearer header:</p>
<pre><code>Authorization: Bearer &lt;your-token&gt;
</code></pre>
<h2 id="point-a-tool-at-it">Point a tool at it</h2>
<p>In your MCP client (Claude Desktop, Cursor, or another desktop AI app), add a server with the
endpoint and the <code>Authorization</code> header above. For a client that takes a streamable-HTTP
server as JSON, it looks like this (paste your token):</p>
<pre><code class="language-json">{
  &quot;mcpServers&quot;: {
    &quot;smartbrain&quot;: {
      &quot;url&quot;: &quot;http://localhost:33000/mcp/&quot;,
      &quot;headers&quot;: { &quot;Authorization&quot;: &quot;Bearer &lt;your-token&gt;&quot; }
    }
  }
}
</code></pre>
<p>The client then sees exactly two tools:</p>
<table>
<thead>
<tr>
<th>Tool</th>
<th>What it does</th>
</tr>
</thead>
<tbody><tr>
<td><code>kb_search</code></td>
<td>Searches your knowledge by meaning, falling back to keyword search if no embedding model is available. Returns matching documents as id, title, snippet, and score. Takes a <code>limit</code>, 1 to 20, defaulting to 5.</td>
</tr>
<tr>
<td><code>kb_read</code></td>
<td>Returns one document in full, by the id <code>kb_search</code> gave back.</td>
</tr>
</tbody></table>
<p>A typical use is to ask the client a question and let it search your knowledge for the
grounding, the same way SmartBrain&#39;s own assistant does.</p>
<h2 id="what-it-can-and-cant-do">What it can and can&#39;t do</h2>
<ul>
<li><strong>Can:</strong> search and read your Knowledge base. Content that came from an imported or
subscribed vault is labeled with its provenance (which vault, whose key), so a client
can treat third-party knowledge as data rather than instructions.</li>
<li><strong>Can&#39;t:</strong> see your credentials, write or delete anything, or reach other
features. There is no tool to add, edit, rename, or delete a document, and none to
reach Chat, Planner, Schedules, Email, or Settings. Vaults are not exposed either — a
client sees documents, not the vault structure.</li>
<li><strong>Where from:</strong> by default it&#39;s reachable only from your own machine (loopback). It
follows the app&#39;s host binding, so a LAN/HTTPS setup that exposes the app exposes it too.
The token is stored encrypted at rest; revoke any time in Settings → Connections (MCP).</li>
</ul>
<h2 id="next">Next</h2>
<ul>
<li><a href="#backup-recovery">Backup &amp; recovery</a>.</li>
<li><a href="#privacy-security">Privacy &amp; security</a>.</li>
</ul>
`},{slug:`backup-recovery`,title:`Backup & recovery`,html:`<h1 id="backup-recovery">Backup &amp; recovery</h1>
<p><img src="assets/gifs/09-backup-recovery.gif" alt="Download an encrypted backup, then unlock with your Recovery Key"></p>
<p>Everything lives in one encrypted database on your machine. These tools, under
<strong>Settings → Account &amp; Data</strong>, let you take it with you, restore it, and change
your passphrase — plus how to get back in if you forget it.</p>
<h2 id="export-your-data">Export your data</h2>
<p><strong>Export data (JSON)</strong> downloads your content as readable JSON: your profile, remembered
facts, tasks, knowledge documents (title and text), and every conversation with its
messages. It&#39;s decrypted (it&#39;s yours), so keep the file somewhere safe. Good for reading
your data elsewhere or migrating out.</p>
<p>It is <strong>not</strong> a backup — it holds no keys and cannot be restored from. Use the encrypted
backup below for that.</p>
<p>Because it hands out decrypted data, it runs on the <strong>Desktop only</strong> (never from a
paired phone) and <strong>re-prompts for your passphrase</strong> to authorize. It saves as
<code>smartbrain-export.json</code>.</p>
<h2 id="encrypted-backup">Encrypted backup</h2>
<p><strong>Download encrypted backup</strong> gives you a complete, portable copy of the database
(<code>smartbrain-backup.duckdb</code>). It&#39;s still encrypted — it includes your wrapped keys — so it
restores with the <strong>same passphrase</strong>. This is the one to keep for disaster
recovery and to move your install to a new machine. Like Export, it&#39;s
<strong>Desktop-only</strong> and <strong>re-prompts for your passphrase</strong> before it hands over the vault.</p>
<p>Both buttons ask for the <strong>passphrase</strong>, so if you got in with your Recovery Key, set a new
passphrase first (<strong>Change passphrase → &quot;Forgot your current passphrase… Set a new one&quot;</strong>)
and then take the backup.</p>
<h2 id="restore">Restore</h2>
<p><strong>Stage restore</strong> takes a backup file, validates it, and applies it the <strong>next
time SmartBrain_3000 restarts</strong> (swapping the live database while it&#39;s running
isn&#39;t safe). Your current database is kept alongside as <code>*.pre-restore-&lt;timestamp&gt;</code>,
so a restore is reversible.</p>
<ul>
<li>Allowed when you&#39;re <strong>unlocked</strong>, or onto a <strong>fresh install</strong> (moving to a new
machine) — never over a locked, initialized vault.</li>
<li>After staging, restart SmartBrain — <strong>Restart</strong> in the menu-bar / tray menu
(headless Linux: <code>systemctl --user restart smartbrain</code>), or
<code>docker compose -f docker-compose.release.yml restart</code> on the Docker stack (from
source, <code>python3 installer/install.py update</code>) — and unlock with that backup&#39;s
passphrase.</li>
<li>A backup from a <strong>newer version</strong> of SmartBrain_3000 is <strong>refused on purpose</strong>
(it would risk data loss under older code): upgrade this app first, then restore.</li>
<li>A file that isn&#39;t a SmartBrain backup, is empty, or is larger than 1 GiB is refused
before anything is touched.</li>
</ul>
<h3 id="moving-to-a-new-machine">Moving to a new machine</h3>
<p>The whole move is four steps:</p>
<ol>
<li>On the old machine, <strong>Download encrypted backup</strong>.</li>
<li>Install SmartBrain on the new machine and let it finish its first start. Don&#39;t complete
setup — a restore onto a <strong>fresh install</strong> is exactly the supported case.</li>
<li><strong>Stage restore</strong> with the backup file.</li>
<li>Restart SmartBrain and unlock with the <strong>old machine&#39;s</strong> passphrase.</li>
</ol>
<p>Your Recovery Key comes across with the backup and still works.</p>
<h2 id="when-something-is-broken-what-to-try-in-order">When something is broken: what to try, in order</h2>
<p>Three escalating repairs. Start at the top; each is slower and more disruptive than the one
above it, and most problems never get past the first.</p>
<ol start="0">
<li><strong>Look first.</strong> Open <strong>Settings → Status</strong> in the app before you touch anything. It shows
the app version, the lock state, the voice model&#39;s download progress (with a one-tap
<strong>Retry download</strong>), the model-server configuration, knowledge counts, schedules, feeds
with a count of any that are failing, paired devices, and <strong>Storage &amp; memory</strong> — disk
used by the database and the models, and memory in use. If SmartBrain seems to be using
a lot of disk, that last panel says where it went.</li>
<li><strong>Restart.</strong> Choose <strong>Restart</strong> in the menu-bar / tray menu. Then reload the browser tab.
This fixes most transient trouble and takes seconds.</li>
<li><strong>A clean upgrade.</strong> Non-destructive, a couple of minutes: stop SmartBrain, clear any
leftovers, upgrade the launcher, start it again. This is the right answer for a
half-finished install or a stuck port. See
<a href="#getting-started__if-an-install-is-misbehaving-a-clean-upgrade">Getting started → If an install is misbehaving</a>.</li>
<li><strong>A full reset</strong>, below — the last resort.</li>
</ol>
<h2 id="starting-completely-fresh">Starting completely fresh</h2>
<p>If an install is broken in a way that neither a restart nor a clean upgrade can fix, there
is a full reset: back up, remove everything SmartBrain put on the machine, install the
latest version, and restore your data.</p>
<p><strong>This is the last resort, and you almost certainly do not need it.</strong> Work through the two
steps above first — a <strong>Restart</strong> from the menu, then the
<a href="#getting-started__if-an-install-is-misbehaving-a-clean-upgrade">clean upgrade</a>, then a
hard reload of the browser tab. A full reset re-downloads the whole app and takes 10–30
minutes.</p>
<pre><code class="language-sh">bash installer/full-reset.sh --inventory   # what is on this machine; changes nothing
bash installer/full-reset.sh --dry-run     # the whole plan, carried out on nothing
bash installer/full-reset.sh               # do it
</code></pre>
<p>It will not continue without a backup file it has checked, it shows every deletion before
it happens and asks you to type a confirmation word, and it never deletes your backup or
your data — your data folder is <strong>moved</strong> to <code>~/SmartBrain-reset-&lt;timestamp&gt;/</code>, not removed.</p>
<p>One step is manual, and skipping it is the usual reason a correct reinstall still looks
broken: <strong>clear the browser</strong>. SmartBrain installs a service worker that caches the app,
and it will keep serving the old version after a reinstall. The script prints a snippet to
paste into your browser console that unregisters it and clears the cached app, the
paired-device credential, and stored settings. A paired phone has to be paired again
afterwards.</p>
<p>macOS only for now. On Windows the same process applies, against <code>%APPDATA%\\SmartBrain</code>
and the Scoop package. On native Linux, <code>sh install-linux.sh --purge</code> removes everything
(<code>--uninstall</code> keeps your data) — see
<a href="#getting-started__uninstall">Getting started → Uninstall</a>.</p>
<h2 id="chat-trash">Chat Trash</h2>
<p>Deleted chats (one at a time, or Chat&#39;s <strong>Delete all…</strong>) land here for <strong>30 days</strong>. Each
one shows when it was deleted and how long it has left, with <strong>Restore</strong> to bring it back.
<strong>Delete all chats</strong> here does the same as Chat&#39;s own button, and <strong>Empty trash</strong> purges
everything in the trash immediately. After 30 days they&#39;re removed for good automatically.</p>
<h2 id="change-your-passphrase">Change your passphrase</h2>
<p><strong>Change passphrase</strong> re-wraps your master key under a new passphrase after
verifying the current one. Your data and your Recovery Key stay valid — only the
passphrase changes. A passphrase must be at least 8 characters.</p>
<h2 id="forgot-your-passphrase">Forgot your passphrase?</h2>
<p>There is <strong>no server and no reset</strong>. Use your <strong>Recovery Key</strong> from the Emergency
Kit you saved during setup:</p>
<ol>
<li>Lock / reopen the app and choose <strong>Use recovery key</strong>.</li>
<li>Enter the key exactly as shown (dashes and letter case don&#39;t matter).</li>
<li>Once in, go to <strong>Settings → Account &amp; Data → Change passphrase</strong> and use
<strong>&quot;Forgot your current passphrase… Set a new one&quot;</strong> — that path sets a new
passphrase from your unlocked session, so you don&#39;t need the old one. (The
normal Change passphrase form still requires the current one.)</li>
</ol>
<p>If you lose <strong>both</strong> the passphrase and the Recovery Key, the data cannot be
recovered — that&#39;s the cost of having no backdoor. Keep the Emergency Kit safe.</p>
<h2 id="next">Next</h2>
<ul>
<li><a href="#privacy-security">Privacy &amp; security</a> — what&#39;s protected and what leaves your machine.</li>
</ul>
`},{slug:`privacy-security`,title:`Privacy & security`,html:`<h1 id="privacy-security">Privacy &amp; security</h1>
<p>SmartBrain_3000 is built to keep your data on your machine and under your
control. Here&#39;s the model in plain terms, including the real world limits.</p>
<h2 id="what-protects-your-data">What protects your data</h2>
<ul>
<li><strong>Local-first.</strong> Everything runs on your machine — the app, the model gateway, and your
database — with no account server and no telemetry. The only SmartBrain-operated service
is the optional, content-blind signaling node for remote phone access — off by default
(see below).</li>
<li><strong>Verified at install.</strong> The desktop app assembles SmartBrain from a pinned Python
runtime, the release&#39;s own packages, and the model gateway. Every download is checked
against a known checksum before it is used, and a version only becomes the live one once
all of it succeeded — so a failed or tampered download leaves the previous version
running rather than replacing it.</li>
<li><strong>Encrypted at rest.</strong> Your knowledge, chats, tasks, memories, email
credentials, and provider keys are encrypted (AES-256-GCM) in the local
database. The encryption key is derived from your passphrase (a slow, modern
key-derivation function) and also wrapped under your Recovery Key.</li>
<li><strong>Locked by default.</strong> On startup the app holds no key. Unlocking loads it into
memory for the session; <strong>Lock</strong> drops it again.</li>
<li><strong>Loopback-only.</strong> The app binds to <code>localhost</code> and validates the request host,
which blocks DNS-rebinding attacks from web pages you visit. It isn&#39;t exposed to
your network.</li>
<li><strong>Approval gates.</strong> The assistant can read freely but can&#39;t change data or reach
out (send email, delete, fetch the web) without your explicit approval, with an
extra confirm for irreversible actions. Everything it attempts is audited. A parked
action expires after an hour, and locking cancels every pending one.</li>
<li><strong>Credential firewall.</strong> Tools and connected MCP clients act on your behalf but
never receive your raw keys or tokens. On top of that, any tool setting <em>named</em>
like a credential — <code>api_key</code>, <code>token</code>, <code>password</code>, <code>passphrase</code>, <code>secret</code> — is
stripped before an action is shown to you or written to the audit log. That match
is on the name, not the content: a secret you type into ordinary text, like the
body of an email, isn&#39;t recognised as one, so treat free text as visible.</li>
<li><strong>Web-fetch guard.</strong> The web-fetch tool refuses private/internal addresses and
doesn&#39;t follow redirects into them (anti-SSRF).</li>
<li><strong>The model gateway keeps no transcript.</strong> Bifrost ships with request logging on, which
would write every prompt and reply to an unencrypted file beside your database.
SmartBrain starts it with that store disabled, destroys any log database it finds on
startup, and re-asserts the setting at every start and unlock.</li>
</ul>
<h3 id="what-is-encrypted-and-what-isnt">What is encrypted, and what isn&#39;t</h3>
<p>Content is encrypted; the small amount of bookkeeping needed to find and schedule things is
not. Being precise about the line matters more than claiming everything:</p>
<ul>
<li><strong>Encrypted</strong> (AES-256-GCM, under your key): documents and their text, chat messages,
task titles, notes and tags, memories and your profile, schedule titles and prompts,
scheduled-run output, provider and search API keys, the Gmail token, the MCP token,
and the arguments and results recorded in the audit log.</li>
<li><strong>Not encrypted, outside the database:</strong> the speech model files (about 141 MB under
the data folder&#39;s <code>models/</code>) — public model weights, nothing of yours — and, if you
run the Mic &amp; speaker check, its last three-second test recording, kept beside them
so a bad capture can be diagnosed from the audio itself.</li>
<li><strong>Not encrypted</strong> (plaintext metadata in the same local database): timestamps, a
schedule&#39;s cadence and next-run time, a task&#39;s due date, priority and status, which
model is routed to what, and, in the audit log, the tool&#39;s name, its risk tier, what you
decided and whether it worked. Someone with your disk learns <em>that</em> a tool ran and
when — not what it was given or what came back.</li>
</ul>
<h2 id="what-leaves-your-machine-and-when">What leaves your machine (and when)</h2>
<ul>
<li><strong>Cloud model calls.</strong> If you use an OpenAI/Anthropic/Google model, your prompts
and the content you send go to that provider. Use a <strong>local model</strong> (Ollama/MLX)
to keep everything on-box. Four jobs use a model, and each is routed separately
under Settings → Model routing: chat, scheduled runs, embeddings for search, and
background document summaries. Point any of them at a cloud provider and that job&#39;s
content goes there — the embedding and summary slots are the easy ones to overlook,
because they run over your documents in the background rather than in front of you.</li>
<li><strong>Email.</strong> If you connect Gmail, the app talks to Google&#39;s APIs to read/send your
mail — over a loopback OAuth flow, with your own OAuth client.</li>
<li><strong>Remote access (only if you enable it).</strong> Phone access is <strong>off by default</strong>. When
you turn it on, your Desktop dials out to a content-blind signaling node to broker the
connection — the SecureCloudGroup-hosted node (<code>rtc.securecloudgroup.com</code>) by default,
or your own via <code>SMARTBRAIN_SIGNALING_URL</code>. It carries only connection metadata, never
your data (the link is end-to-end encrypted). See <a href="#remote-access">Remote access</a>.</li>
<li><strong>Public vaults (only if you subscribe).</strong> Subscribing to a vault by URL — and any
<strong>Check for updates</strong> or scheduled auto-update on it — fetches the vault from the host
in that URL (public internet hosts only, never localhost or LAN addresses). Recurring
checks happen only if you turned auto-update on.</li>
<li><strong>Web search &amp; fetch (only when the assistant uses those tools).</strong> A web search goes
to the engine you chose — <strong>DuckDuckGo by default</strong>, or your own Brave/Tavily key or
self-hosted SearXNG (Settings → Web search) — and a web fetch goes to that page&#39;s
host. Dangerous fetches are approval-gated and SSRF-guarded; nothing is searched or
fetched outside a turn that calls for it.</li>
<li><strong>The speech model, once.</strong> At launch, SmartBrain fetches the Whisper dictation model
(about 141 MB, four files) from the public Hugging Face repository
<code>Systran/faster-whisper-base</code>, and checks every file against a pinned hash. The request
carries no identity and nothing about you; it happens once, and never again while the
files are in place. Air-gapped or network-forbidden deployments set
<code>SMARTBRAIN_NO_VOICE_PREFETCH=1</code> to skip it (dictation is then unavailable until a
voice server is configured). <strong>Your voice itself never leaves your machines</strong>: dictation
is transcribed on the Desktop, spoken replies use your device&#39;s own voices, and a
phone&#39;s audio travels only the end-to-end encrypted link to your Desktop.</li>
<li><strong>Update checks — by the desktop app, not by SmartBrain.</strong> Every six hours — or when you
choose <strong>Check for updates</strong> in its menu — the menu-bar
launcher asks GitHub whether a newer release exists, and downloads it from GitHub if so.
That request carries no identity and nothing about you or your data; it is the same
public release page anyone can open. SmartBrain itself makes no such call — it hears
about a waiting update from the launcher over the local heartbeat they already exchange.</li>
<li><strong>Nothing else.</strong> Beyond the above, the app makes no outbound calls. Self-improvement
(if you enable it) is fully local by design: its reviews and learning run on your
machine against a local model only — it never sends your activity anywhere.</li>
</ul>
<p>Two things that sound like they&#39;d leave and don&#39;t:</p>
<ul>
<li><strong>MCP.</strong> A connected desktop AI client reads your knowledge over a loopback
connection on your own machine. SmartBrain sends nothing outward for it. What that
<em>client</em> then does with what it read is its business, not SmartBrain&#39;s — see
<a href="#mcp">MCP</a>.</li>
<li><strong>Publishing a vault.</strong> Export writes a file to your disk. Nothing is uploaded;
where it goes afterwards is entirely your doing. See <a href="#vaults">Vaults</a>.</li>
</ul>
<h2 id="honest-limits">Honest limits</h2>
<ul>
<li><strong>Your host machine.</strong> If your computer or OS is compromised, local encryption
can&#39;t fully protect a running, unlocked session. Keep your machine secure.</li>
<li><strong>No recovery backdoor.</strong> Lose both your passphrase and Recovery Key and the data
is unrecoverable — by design. Keep the Emergency Kit safe and offline.</li>
<li><strong>Prompt injection.</strong> Content the assistant reads (web pages, emails, documents)
could try to manipulate it. The approval gates are the backstop: nothing
consequential happens without your sign-off.</li>
<li><strong>Single-user, personal scale.</strong> SmartBrain_3000 is built for one owner on one
machine. Several boundaries — one global unlock, a single-writer database, no
key at rest — are deliberate. See <a href="#design-limits">Design limits</a> for the
full list and the reasoning.</li>
</ul>
<h2 id="reporting-an-issue">Reporting an issue</h2>
<p>Found a security problem? Please report it privately — see
<a href="https://github.com/SecureCloudGroup/SmartBrain_3000/blob/main/SECURITY.md"><code>SECURITY.md</code></a>
(email <code>info@securecloudgroup.com</code>). Don&#39;t open a public issue for vulnerabilities.</p>
`},{slug:`remote-access`,title:`Remote access (away from home)`,html:`<h1 id="remote-access-away-from-home">Remote access (away from home)</h1>
<p>By default SmartBrain_3000 runs only on your own computer. <strong>Remote access</strong> lets you
reach it from your phone — on Wi-Fi or cellular — without any router or port-forward
setup. It&#39;s <strong>off by default</strong>; you opt in by pairing a phone.</p>
<h2 id="how-it-works">How it works</h2>
<p>Your <strong>Desktop</strong> is where you set everything up. To use SmartBrain on your phone, you
<strong>pair</strong> the phone once. After that, the phone reaches your Desktop over <strong>WebRTC</strong> — a
direct, <strong>end-to-end-encrypted</strong> connection (DTLS). When a direct link isn&#39;t possible,
traffic falls back to an encrypted <strong>relay</strong> that still can&#39;t read your data.</p>
<p>This uses a small <strong>signaling node</strong> on a public server (not your home machine) that helps your
phone find your Desktop. SmartBrain is <strong>preconfigured to use one</strong>, so there&#39;s nothing to set
up — your Desktop dials <strong>out</strong> to it, so nothing on your home network is ever exposed. The node
is <strong>content-blind</strong>: it only relays the encrypted connection setup, never your data. (Prefer your
own node? See <em>Self-hosting the signaling node</em> at the end.)</p>
<h2 id="pair-your-phone">Pair your phone</h2>
<p><img src="assets/06-remote-access.png" alt="Settings → Remote access: name a phone and pair it"></p>
<p><img src="assets/gifs/08-pair-a-phone.gif" alt="Pair a phone — QR + 6-character code over end-to-end-encrypted WebRTC"></p>
<p>On the <strong>Desktop</strong>, open <strong>Settings → Remote access</strong>, give the phone a name (it defaults to
<em>My phone</em>, and is only a label so you can tell your devices apart later), and tap
<strong>Pair a new phone</strong>. You&#39;ll see a QR code, three short steps, and a <strong>6-character code</strong>.</p>
<p>On the <strong>phone</strong>:</p>
<ol>
<li><strong>Scan the QR</strong> (or open the address shown) to load SmartBrain in your browser.</li>
<li><strong>Add it to your Home Screen</strong>, then open the installed app:<ul>
<li><strong>iPhone/iPad:</strong> the <strong>Share</strong> button → <em>Add to Home Screen</em>.</li>
<li><strong>Android:</strong> the <strong>⋮</strong> menu → <em>Install app</em>.</li>
</ul>
</li>
<li>In the installed app, <strong>enter the 6-character code</strong> and tap <strong>Pair</strong>.</li>
</ol>
<p>The Desktop watches while you do this and says so: <em>&quot;Waiting for your phone…&quot;</em>, then
<em>&quot;Your phone connected.&quot;</em></p>
<p>That&#39;s it — the phone connects, from Wi-Fi or cellular. The code lasts <strong>5 minutes</strong> and the
page counts it down; if it expires, tap <strong>Pair a new phone</strong> for a fresh one. One pairing
can be in progress at a time.</p>
<p>If your Desktop is <strong>locked</strong> while the phone pairs or connects, the Desktop says so to the
phone: <em>&quot;your Desktop is locked — unlock it there, then this phone reconnects.&quot;</em> The phone
keeps retrying and walks in on its own once you unlock; the pairing screen&#39;s timeout names
the locked case too, so you know what to do.</p>
<blockquote>
<p>Why install first? On iPhone, an app on the Home Screen has its own private storage, separate
from Safari — so pairing happens <em>in the installed app</em>. The QR&#39;s only job is to open the site
so you can install it; it carries no secret.</p>
</blockquote>
<h2 id="using-it-on-your-phone">Using it on your phone</h2>
<p>The phone shows a <strong>trimmed set</strong> of areas meant for use on the go: <strong>Chat</strong>,
<strong>Knowledge</strong>, <strong>Planner</strong>, <strong>Schedules</strong>, <strong>Email</strong>, <strong>Info</strong>, <strong>Activity</strong>, and <strong>Usage</strong>.
Settings and first-time setup live on the <strong>Desktop</strong>. Adding to your Knowledge — notes,
uploads, and add-by-URL, plus importing or subscribing to someone else&#39;s vault — works from
the phone too; the desktop is still where it lands.</p>
<p>A handful of individual actions are Desktop-only, even inside those areas — anything that
hands out your data or changes trust. Exporting a vault (sharing it sealed or public),
trusting a publisher&#39;s new key after it rotates, connecting Gmail, and downloading a backup
or export all stay on the Desktop. On the phone those controls are either not shown or
replaced with a line pointing you at the Desktop, so nothing fails halfway.</p>
<p>Voice works on the phone too — the mic, spoken replies, and the pills above the message box.
The settings behind those pills (the wake word, playback speed, and the <strong>Short / Medium /
Long</strong> reply default) are set on the Desktop under <strong>Settings → Status → Voice</strong>, since
Settings is Desktop-only. See <a href="#features__voice">Voice</a>.</p>
<p>A small <strong>&quot;Remote&quot;</strong> chip shows the connection state: <strong>direct</strong> (phone-to-Desktop),
<strong>relayed</strong> (through the encrypted relay), <strong>Desktop locked</strong> if your Desktop is up and
the encrypted bridge is fine but its vault is locked — tap the chip to unlock from here
(it&#39;s one shared lock: unlocking from the phone unlocks the Desktop too, and a Desktop
sitting on its unlock screen walks in on its own),
<strong>unreachable</strong> if your Desktop is off, asleep, or otherwise can&#39;t be reached at all, or
<strong>BLOCKED</strong> in red if your Desktop&#39;s identity can&#39;t be verified — re-pair if you reinstalled
the app.</p>
<p>The connection is built to survive a phone: it sends a small keepalive so an idle mobile
network can&#39;t quietly drop it, notices a dead path within about a minute and reconnects on
its own, and tolerates you switching apps for a few minutes. If the retries do give up, the
next tap in the app starts it again.</p>
<p>Your Desktop must be <strong>running</strong> for any of this to work, and <strong>unlocked</strong> to do anything
with — a locked Desktop tells your phone so, and the phone reconnects by itself after the
unlock. Since the phone is a window onto the Desktop, they share one vault and one lock:
unlock (or lock) on either, and both follow. The phone is a window onto it, not a copy of
it.</p>
<h2 id="manage-devices">Manage devices</h2>
<p>Under <strong>Settings → Remote access</strong> you can pair more devices, see when each was paired, and
<strong>Revoke</strong> any of them at any time. A revoked device can no longer connect. On the phone
itself, <strong>Unpair</strong> in the sidebar forgets the pairing from that end.</p>
<h2 id="security">Security</h2>
<ul>
<li><strong>Off by default.</strong> Nothing is reachable until you pair a device.</li>
<li><strong>End-to-end encrypted.</strong> The connection is encrypted (DTLS); the signaling node and
relay only ever see scrambled bytes, never your data.</li>
<li><strong>Identity-checked.</strong> Before sending anything, your phone verifies your Desktop&#39;s
identity (a key pinned at pairing), so a compromised node can&#39;t impersonate it.</li>
<li><strong>One-time code.</strong> The 6-character pairing code is single-use and short-lived — don&#39;t share
it. (The QR only opens the site; it carries no secret.)</li>
</ul>
<p>This changes <em>where you can reach the app from</em>, not what protects your data. See
<a href="#privacy-security">Privacy &amp; security</a>.</p>
<h2 id="on-your-own-wi-fi-lan-https">On your own Wi-Fi (LAN, HTTPS)</h2>
<p>If you only want your phone to reach the Desktop <strong>on the same Wi-Fi</strong>, you don&#39;t need
the signaling node at all — you can serve the app over HTTPS on your local network. This
uses a local certificate so your phone trusts the connection.</p>
<blockquote>
<p>This path is set up by the repo&#39;s installer, so it needs an
<a href="#getting-started__install-from-source-for-contributors">install from source</a>. Pairing
above works on every install and needs none of this.</p>
</blockquote>
<ol>
<li><p><strong>Make a local certificate</strong> (uses <a href="https://github.com/FiloSottile/mkcert">mkcert</a>),
passing a name and your Desktop&#39;s LAN IP:</p>
<pre><code class="language-sh">python3 installer/install.py certs smartbrain.local 192.168.1.50
</code></pre>
<p>It writes the cert to <code>data/certs/</code>, trusts the local CA on your computer, and prints
the path to <strong><code>rootCA.pem</code></strong>.</p>
</li>
<li><p><strong>Trust the CA on your phone</strong> — install that <code>rootCA.pem</code> (AirDrop/email it to
yourself, then open it) so the phone trusts the local certificate.</p>
</li>
<li><p><strong>Allow your LAN address and bring it up over HTTPS.</strong> Set
<code>SMARTBRAIN_ALLOWED_HOSTS</code> to include your LAN IP/name in <code>compose/.env</code>, e.g.
<code>SMARTBRAIN_ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.50,smartbrain.local</code>, then
re-run <code>python3 installer/install.py install</code>. Once a cert exists the installer
automatically serves HTTPS on your LAN.</p>
</li>
<li><p><strong>On the phone (same Wi-Fi)</strong> open <code>https://192.168.1.50:33000</code>.</p>
</li>
</ol>
<blockquote>
<p><strong>Connecting Gmail over HTTPS.</strong> Google&#39;s loopback OAuth redirect is <code>http://</code>, which the
HTTPS app can&#39;t serve directly. In HTTPS mode the app therefore also runs a tiny
<strong>loopback-only</strong> helper (on <code>127.0.0.1:33001</code>; set <code>SMARTBRAIN_OAUTH_HELPER_PORT</code> to change
it — it must differ from the app port) that forwards the OAuth callback to HTTPS. Connecting
Gmail then works exactly as on plain HTTP, and the helper is <strong>never</strong> exposed to the LAN.</p>
</blockquote>
<p>This path is <strong>same-network only</strong>. To reach the Desktop from cellular or another
network, use the WebRTC pairing above.</p>
<h2 id="self-hosting-the-signaling-node-advanced">Self-hosting the signaling node (advanced)</h2>
<p>SmartBrain ships pointed at a hosted, content-blind node, so <strong>most people need none of this.</strong>
To run your own node instead:</p>
<ol>
<li><p><strong>Run the node</strong> on a small public server with a domain (open ports 80/443 TCP, 3478
TCP+UDP, 49160-49260 UDP):</p>
<pre><code class="language-sh">SIGNALING_DOMAIN=&lt;your-domain&gt;  ACME_EMAIL=&lt;you@example.com&gt;  SIGNALING_OPEN=1 \\
TURN_SECRET=$(openssl rand -hex 32)  TURN_PUBLIC_IP=&lt;vps-ipv4&gt; \\
  docker compose -f compose/docker-compose.signaling.yml up -d
</code></pre>
<p>The node mints <strong>ephemeral TURN credentials</strong> per connection (coturn <code>use-auth-secret</code>),
so no secret is ever baked into the app or a QR.</p>
</li>
<li><p><strong>Point your Desktop at it</strong> — set in your environment / <code>.env</code>:</p>
<pre><code class="language-sh">SMARTBRAIN_SIGNALING_URL=wss://&lt;your-domain&gt;
</code></pre>
<p>The Desktop fetches STUN/TURN from the node automatically; there&#39;s nothing else to set.
Then pair devices as above.</p>
</li>
</ol>
<p>(A WireGuard VPN overlay also exists as a CLI-only alternative —
<code>python3 installer/install.py wireguard up</code> — but WebRTC is the recommended path.)</p>
<h2 id="next">Next</h2>
<ul>
<li><a href="#privacy-security">Privacy &amp; security</a> — what&#39;s protected and the real world limits.</li>
</ul>
`},{slug:`design-limits`,title:`Design limits`,html:`<h1 id="design-limits">Design limits</h1>
<p>SmartBrain_3000 is built as a <strong>single-user, local-first, personal-scale</strong> app.
Some of its boundaries are deliberate scope decisions — the kind of tradeoffs
that keep a personal tool simple, predictable, and safe — rather than missing
features. This page documents those choices and the reasoning behind each, so
there are no surprises.</p>
<p>These are intentional for the single-user model. They are <strong>not</strong> the right
tradeoffs for a multi-tenant or team deployment; SmartBrain_3000 isn&#39;t built for
that.</p>
<h2 id="single-user-global-unlock">Single-user global unlock</h2>
<p>There is <strong>one master key per running process</strong>. When you unlock, the whole app
is unlocked; there is no per-user isolation, no separate accounts, and no
sandboxing of one &quot;user&quot; from another within the same instance.</p>
<p><strong>Why:</strong> the product is a personal assistant for one owner on one machine.
Adding multi-user identity, per-user keys, and access control would add a large
surface for little benefit at this scale. One owner, one key, one encrypted store.</p>
<h2 id="single-writer-embedded-database-duckdb">Single-writer embedded database (DuckDB)</h2>
<p>Data lives in an <strong>embedded DuckDB</strong> file. There is effectively <strong>one concurrent
writer</strong> — the app — and the database is sized for personal use, not for many
clients writing at once.</p>
<p><strong>Why:</strong> an embedded, file-based store keeps the install trivial (no separate
database server) and matches a single-user workload. Concurrency that a
multi-client server would need isn&#39;t a goal here.</p>
<h2 id="no-key-at-rest-restart-returns-to-locked">No key at rest (restart returns to locked)</h2>
<p>The encryption key is <strong>never written to disk</strong>. It lives only in memory while
you&#39;re unlocked. So a <strong>restart</strong> (or a crash, or <code>Lock</code>) returns the app to the
<strong>locked</strong> state, and any <strong>in-flight approvals are invalidated</strong> — a parked
action won&#39;t silently run after a restart; you&#39;ll unlock and re-approve.</p>
<p>A parked action also <strong>expires after an hour</strong> on its own. Approval is consent to something
happening <em>now</em>, and an hour-old &quot;yes&quot; to a half-remembered request is not the same thing.</p>
<p><strong>Why:</strong> this trades some unattended resilience for security. The upside is
that data at rest is never decryptable without your passphrase or Recovery Key,
even if someone copies the disk. The cost is that an unattended restart leaves
the app locked until you return, and that nothing — no schedule, no vault
auto-update, no self-review — happens while it is locked.</p>
<h2 id="append-only-audit-log-no-hash-chain">Append-only audit log (no hash chain)</h2>
<p>Every tool attempt is recorded, and the audit log is <strong>append-only at the API
surface</strong> — the app exposes no way to edit or delete entries. It is <strong>not</strong> a
cryptographically chained, tamper-evident log (no per-entry hash chain).</p>
<p><strong>Why:</strong> append-only-at-the-API gives you a faithful record for a single-owner
tool, where the threat isn&#39;t the owner forging their own history. A verifiable
hash chain is a reasonable post-MVP hardening, but it isn&#39;t needed to meet the
single-user transparency goal today.</p>
<h2 id="the-search-index-lives-in-memory-rebuilt-on-each-unlock">The search index lives in memory (rebuilt on each unlock)</h2>
<p>Because content is <strong>encrypted at rest</strong>, we can&#39;t push search predicates down into a
plaintext database index. Instead, the corpus is decrypted <strong>once per unlock</strong> into an
in-memory index — a BM25 keyword index plus a matrix of chunk vectors — and every query is
answered from RAM. Only the handful of documents actually returned are decrypted again, to
cut their snippets.</p>
<p>The trade-offs that follow from that:</p>
<ul>
<li><strong>The first search after unlocking pays a one-time build.</strong> Roughly 0.2s for 1,000
documents and ~1.8s for 10,000. Searches after that are single-digit milliseconds.</li>
<li><strong>The index costs RAM</strong> — dominated by the vectors (~30 MB per 1,000 documents at 768
dimensions). Very large libraries are bounded by an explicit ceiling — <strong>100,000
documents</strong> — and if a corpus exceeds it that is <strong>reported, not silently ignored</strong>.</li>
<li><strong>Nothing is written to disk.</strong> The index is never persisted, so encryption at rest is
unchanged: it exists only while the vault is unlocked and dies with the master key.</li>
</ul>
<p><strong>Why:</strong> indexing encrypted content on disk without leaking it is hard. Rebuilding in memory
keeps the encryption promise intact while still giving fast, whole-corpus search.</p>
<h2 id="one-local-model-request-at-a-time">One local-model request at a time</h2>
<p>A local model server — Ollama, MLX, oMLX — serves <strong>one request at a time</strong>. SmartBrain has
several things that might want it at once: your chat, the background indexer embedding new
documents, the summary builder, a scheduled run. They are <strong>queued</strong>, never overlapped, so a
second caller can&#39;t provoke the &quot;model is busy&quot; failure that would break the first.</p>
<p>Your chat has priority: background work steps aside the moment a chat arrives and picks up
where it left off afterwards. The visible cost is that a large indexing backlog can still
make an answer feel slower than usual while it drains.</p>
<p><strong>Why:</strong> the alternative is either failed requests or a queue the user can&#39;t see. Cloud
providers have no such limit and are unaffected — this applies only to local models.</p>
<h2 id="voice-one-dictation-at-a-time-120-seconds-one-download">Voice: one dictation at a time, 120 seconds, one download</h2>
<p>Dictation runs on your own machine, and three limits follow from that:</p>
<ul>
<li><strong>One dictation is transcribed at a time.</strong> Like the local model, the speech engine is
queued, never overlapped — a second recording waits for the first to be turned into text.</li>
<li><strong>A single recording is capped at 120 seconds.</strong> Dictation normally stops itself when you
pause; the cap is the backstop for a mic left open. Say it in two pieces if you need more.</li>
<li><strong>The voice model is a one-time ~141 MB download.</strong> It starts at app launch on every OS,
unconditionally, so voice is zero-setup — and the price of zero-setup is that disk.</li>
</ul>
<p><strong>Why:</strong> running speech recognition locally is what keeps your voice on-box. Bounding each
recording and serializing transcription keeps memory and CPU predictable on an ordinary
laptop; downloading the model without asking is what makes the mic simply work.</p>
<h2 id="a-turn-is-bounded">A turn is bounded</h2>
<p>One request to the assistant gets at most <strong>eight tool steps</strong>. When those run out — or when
what it has gathered would no longer fit in the model&#39;s context — it stops asking for tools
and writes an answer from what it has, saying plainly what it couldn&#39;t finish. It never
loops, and it never quietly gives up.</p>
<p><strong>Why:</strong> an unbounded agent is a way to spend an afternoon and a lot of money on a question
that needed one search. A hard step count makes the worst case predictable. Where it isn&#39;t
enough, the answer says so and you can ask a narrower question — which is nearly always
faster than letting it wander.</p>
<h2 id="webrtc-signaling-broker-is-single-operator">WebRTC signaling broker is single-operator</h2>
<p><a href="#remote-access">Remote access</a> uses a signaling broker that is
<strong>single-operator</strong> by design. The hosted broker is <strong>tokenless</strong> (open
registration, bounded by a desktop-count cap and per-registration rate limits),
and the cryptographic guarantee that your phone is really talking to <strong>your</strong>
Desktop is the <strong>DTLS-fingerprint pin</strong> captured at pairing — not the broker.
TURN relay uses <strong>ephemeral credentials</strong> (coturn <code>use-auth-secret</code>, minted per
connection and short-lived); those credentials grant <strong>relay bandwidth only</strong>,
never access to the app or your data. A <strong>self-hosted</strong> node may instead run with
a shared registration token and static, quota-bounded TURN creds.</p>
<p><strong>Why:</strong> the broker is content-blind — it only helps devices find each other.
The end-to-end security comes from the pinned fingerprint, so the broker doesn&#39;t
need per-user accounts to be safe. Ephemeral, per-connection TURN creds keep the
relay simple while ensuring a leaked credential can, at worst, consume some relay
bandwidth before it expires.</p>
<h2 id="next">Next</h2>
<ul>
<li><a href="#features">Using SmartBrain_3000</a> — what each area does, day to day.</li>
<li><a href="#privacy-security">Privacy &amp; security</a> — what protects your data and the
real world limits.</li>
<li>Back to the <a href="#">documentation index</a>.</li>
</ul>
`}],x=e(`<a> </a>`),S=e(`<div class="help svelte-1vby5nc"><nav class="help-nav svelte-1vby5nc" aria-label="Help sections"><h2 class="svelte-1vby5nc">Help</h2> <!></nav> <article class="help-body card svelte-1vby5nc"></article></div>`);function C(e,C){g(C,!0);let w=c(()=>y.url.hash.replace(/^#/,``).split(`__`)),T=c(()=>b.find(e=>e.slug===i(w)[0])??b[0]),E=c(()=>i(w)[1]),D=d(void 0);l(()=>{if(i(T),typeof window>`u`||!i(D))return;let e=window.matchMedia(`(prefers-reduced-motion: reduce)`);for(let e of i(D).querySelectorAll(`pre`))e.tabIndex=0;let t=()=>{for(let t of i(D).querySelectorAll(`img`)){let n=t.getAttribute(`src`)??``;e.matches&&n.endsWith(`.gif`)?(t.dataset.gif=n,t.setAttribute(`src`,n.replace(/\.gif$/,`.poster.png`))):!e.matches&&t.dataset.gif&&(t.setAttribute(`src`,t.dataset.gif),delete t.dataset.gif)}};return t(),e.addEventListener(`change`,t),()=>e.removeEventListener(`change`,t)}),l(()=>{i(T);let e=i(E);typeof window>`u`||!i(D)||!e||!/^[\w-]+$/.test(e)||i(D).querySelector(`[id="${e}"]`)?.scrollIntoView()});var O=S(),k=u(O),A=o(u(k),2);v(A,17,()=>b,e=>e.slug,(e,n)=>{var a=x();let o;var c=u(a,!0);_(a),t(()=>{o=f(a,1,`help-link svelte-1vby5nc`,null,o,{active:i(n).slug===i(T).slug}),m(a,`aria-current`,i(n).slug===i(T).slug?`page`:void 0),m(a,`href`,`#${i(n).slug}`),r(c,i(n).title)}),s(e,a)}),_(k);var j=o(k,2);n(j,()=>i(T).html,!0),_(j),p(j,e=>a(D,e),()=>i(D)),_(O),s(e,O),h()}export{C as component};