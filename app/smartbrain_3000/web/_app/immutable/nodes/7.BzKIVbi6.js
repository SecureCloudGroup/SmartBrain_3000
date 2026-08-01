import{A as e,B as t,C as n,D as r,I as i,J as a,K as o,O as s,V as c,W as l,Y as u,Z as d,b as f,c as p,m,nt as h,st as g,tt as _,w as v}from"../chunks/DBeuis-e.js";import"../chunks/xihTtKlq.js";import{t as y}from"../chunks/_rcgagQJ.js";var b=[{slug:`getting-started`,title:`Getting started`,html:`<h1 id="getting-started">Getting started</h1>
<p>SmartBrain_3000 is a <strong>local-first, single-user AI assistant</strong> that runs entirely
on your own machine. Your data and credentials stay on-box, encrypted
at rest. The only outbound calls it makes are to services you explicitly opt into:
the AI providers you configure, and Google&#39;s APIs if you connect Gmail. See
<a href="#privacy-security">Privacy &amp; security</a> for the full picture.</p>
<h2 id="what-you-need">What you need</h2>
<p>Nothing. There is no Docker to install, no Python, no accounts, and no config files to
edit. SmartBrain brings its own runtime: on first start the desktop app downloads a
Python runtime, the app itself, and the model gateway, checks each one against a
checksum, and runs them as two ordinary programs on your machine.</p>
<p>One exception: there is no native build yet for <strong>Intel Macs</strong> or <strong>ARM Linux</strong>. On those
machines the launcher runs SmartBrain in Docker instead, so you need
<a href="https://docs.docker.com/get-docker/">Docker</a> installed and running. Everything else in
this guide works the same either way.</p>
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
<p>On macOS the launcher starts by itself once Homebrew finishes; on Windows, open
<strong>SmartBrain</strong> from the Start menu. The menu-bar icon shows what it is doing. The first
start downloads a few hundred megabytes, so give it a few minutes — the status line reads
<em>&quot;Downloading SmartBrain…&quot;</em>, then <em>&quot;Starting (native)…&quot;</em>, then <em>&quot;Running ● (native)&quot;</em>.
After that it starts in seconds and your browser opens at <strong><a href="http://localhost:33000">http://localhost:33000</a></strong>. Then
complete first-run setup below.</p>
<p>Everything the app installs lives in one folder you own, alongside your data:</p>
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
<td>Linux</td>
<td><code>~/.config/SmartBrain</code> (your database is under <code>~/.local/share/smartbrain/data</code>)</td>
</tr>
</tbody></table>
<h3 id="if-an-install-is-misbehaving-a-clean-upgrade">If an install is misbehaving: a clean upgrade</h3>
<p>Rarely — usually after an interrupted first start, or on a machine that ran an early
Docker build — the launcher can end up with a half-finished install or a leftover
container holding port 33000. This resets it without touching your data:</p>
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
<code>scoop update smartbrain</code> on Windows.</p>
</li>
<li><p><strong>Start SmartBrain again</strong> and watch the menu. It re-downloads whatever is missing and
settles on <strong>Running ● (native)</strong>; the line under it names the version now running.</p>
</li>
</ol>
<p>Your database is in the <code>data</code> folder above and none of these steps touch it. To force a
full re-download of the runtime, delete the <code>native</code> folder next to it — that folder holds
only downloaded parts and is rebuilt on the next start.</p>
<h3 id="install-from-source-for-contributors">Install from source (for contributors)</h3>
<p>Building from the repo uses <strong>Docker</strong> and additionally needs <strong>git</strong> and <strong>Python 3</strong>,
and is slower — it compiles the image locally. Use it when you&#39;re developing on the code:</p>
<pre><code class="language-sh">git clone https://github.com/SecureCloudGroup/SmartBrain_3000.git
cd SmartBrain_3000
python3 installer/install.py install
</code></pre>
<p><code>python3 installer/install.py doctor</code> checks and offers to fix common problems (start Docker,
restart the stack, pull the embedding model). See <a href="#">installer/</a>.</p>
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
   tomorrow.&quot;</em> Because creating a task changes data, it <strong>parks for your approval</strong> in
   <strong>Activity</strong> instead of acting on its own. Open <strong>Activity</strong> and approve it.
5. <strong>That&#39;s the core loop:</strong> the assistant can read freely, but anything that changes
   data or reaches out waits for your <strong>OK</strong> — and every attempt is audited.</p>
<h2 id="locking-and-unlocking">Locking and unlocking</h2>
<ul>
<li>Use <strong>Lock</strong> (top right) to drop the key from memory — your data is sealed until
you unlock again. Locking also clears your provider keys from the gateway.</li>
<li><strong>Unlock</strong> with your passphrase. Forgot it? Choose <strong>Use recovery key</strong>
and enter the key from your Emergency Kit (dashes and letter case don&#39;t matter).</li>
</ul>
<h2 id="updating">Updating</h2>
<p><strong>SmartBrain updates itself — no commands.</strong> The launcher checks for a newer version in the
background and downloads it quietly, without disturbing a session in progress. The download
is separate from the install, so nothing changes under you until you say so.</p>
<p>When an update is ready you&#39;re told in two places:</p>
<ul>
<li><strong>In SmartBrain itself</strong>, a strip at the top of the page: <em>&quot;SmartBrain v0.8.12 is ready to
install. Installing restarts it — under a minute, and you&#39;ll unlock again afterwards.&quot;</em>
Click <strong>Install now</strong> to apply it. The page reconnects and reloads by itself when the new
version comes up — there&#39;s nothing to click twice. Dismissing the notice hides that
version and stays quiet until a newer one arrives.</li>
<li><strong>In the menu-bar / tray menu</strong>, as <strong>Install update now</strong> and <strong>Install on next start</strong>.</li>
</ul>
<p>Ignore it entirely and the update installs the next time you start SmartBrain. Either way
you jump straight to the newest version, even if you&#39;re several behind. Because the key is
never kept on disk, an install leaves the app <strong>locked</strong> — you unlock again afterwards.</p>
<p>Installing is <strong>Desktop-only</strong>. A <strong>paired phone</strong> can see that an update is waiting but
can&#39;t restart your machine over the network; the phone app itself refreshes the next time
you open it.</p>
<p><strong>Which version is running?</strong> The app shows it under the logo, top-left, and the menu-bar
menu names it too. During an update, when the launcher has been replaced but the app it
supervises hasn&#39;t yet, the menu names both numbers rather than one misleading one.</p>
<p>If SmartBrain updates while you have a page open, that page notices and offers a <strong>Reload</strong>:
<em>&quot;SmartBrain updated to v0.8.12 while this page was open — reload to use the new version.&quot;</em>
You can dismiss it and keep working on the old page.</p>
<p>The launcher updates itself on the same schedule, so <code>brew upgrade --cask smartbrain</code> and
<code>scoop update smartbrain</code> are not part of normal use — they&#39;re there if you ever need to
force it.</p>
<p><strong>From source:</strong> <code>python3 installer/install.py update</code> — it <strong>backs up your encrypted data first</strong>,
pulls the latest code, rebuilds the image, restarts the stack, and verifies it&#39;s healthy. It prompts
before making changes and runs on the host, never inside the container.</p>
<p>Your data lives in the <code>data</code> folder named under <strong>Install</strong> above, and an update never
touches it. (More on backups: <a href="#backup-recovery">Backup &amp; recovery</a>.)</p>
<h2 id="troubleshooting">Troubleshooting</h2>
<p>Most first-run problems are one of these:</p>
<ul>
<li><strong>The page won&#39;t load at <a href="http://localhost:33000">http://localhost:33000</a>.</strong> Give a first start a few more minutes —
it&#39;s downloading a few hundred megabytes, and the menu&#39;s status line says what it&#39;s doing.
Once that line reads <strong>Running ●</strong>, click <strong>Open SmartBrain</strong> in the menu.</li>
<li><strong>&quot;Download failed — nothing was changed; check the log and Restart.&quot;</strong> The download of the
runtime didn&#39;t finish (no connection, a proxy, or not enough disk space). Nothing on your
machine was altered. Fix the cause and click <strong>Restart</strong> in the menu.</li>
<li><strong>&quot;SmartBrain keeps crashing — stopped restarting; see the native logs.&quot;</strong> The launcher
restarts a stopped SmartBrain, but gives up after three tries in ten minutes rather than
spinning. The logs are <code>native/run/app.log</code> and <code>native/run/bifrost.log</code> inside the folder
named under <strong>Install</strong> above.</li>
<li><strong>&quot;Native start failed — see the log.&quot;</strong> Open <code>native/run/app.log</code>. If it says an instance
is <em>already serving on port 33000</em>, something else holds that port — usually a SmartBrain
a previous launcher started and never stopped. Choose <strong>Stop</strong> in the menu, then
<strong>Restart</strong>; if it persists, follow <strong>If an install is misbehaving: a clean upgrade</strong>
under <strong>Install</strong> above.</li>
<li><strong>macOS asks if SmartBrain may &quot;access data from other apps.&quot;</strong> Click <strong>Allow</strong>, or don&#39;t —
the launcher is checking whether Docker is installed, which it only needs as a fallback.
It reads nothing else, and declining doesn&#39;t stop SmartBrain from running.</li>
<li><strong>Chat says &quot;No models available yet.&quot;</strong> You haven&#39;t connected a model. If a local
model server (MLX or Ollama) is running, the Chat screen offers a one-tap <strong>Connect</strong>;
otherwise add a cloud key under <strong>Settings → Cloud providers</strong>. See
<a href="#models">Connect a model</a>.</li>
<li><strong>Every answer is slow, by several seconds, always.</strong> A local model server can be
configured to reload the model on every single request. SmartBrain notices and writes a
line to <code>native/run/app.log</code> naming the model and the seconds lost, with what to check
(a draft/speculative-decoding option pointed at an incompatible model, or an
idle-unload setting). It isn&#39;t shown in the app — read the log if answers feel
uniformly slow.</li>
<li><strong>Semantic search returns keyword results (&quot;degraded&quot;).</strong> No embedding model is set
up for your backend. See <a href="#models__embeddings-for-knowledge-search">Embeddings</a> for
your setup, then <strong>Reindex</strong> in Knowledge.</li>
<li><strong>The browser warns about the certificate</strong> (only if you set up LAN/HTTPS). Trust
the local mkcert CA — see <a href="#remote-access">Remote access</a>.</li>
<li><strong>&quot;Database is newer than this app&quot; / a restore is refused.</strong> Pointing an older build
at a newer data directory, or restoring a backup from a newer version, is refused on
purpose to prevent data loss. Let SmartBrain update itself first, then reopen or retry
the restore.</li>
</ul>
<p>On an <strong>Intel Mac</strong> or <strong>ARM Linux</strong>, where SmartBrain runs in Docker, two more apply:</p>
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
Windows. From source, <code>docker compose down</code> in <code>compose/</code>.</p>
<p>On macOS you can add <code>--zap</code> to clear what the app downloaded as well:
<code>brew uninstall --zap --cask smartbrain</code>. That removes the assembled runtime, the logs,
the launcher&#39;s bookkeeping, and the gateway&#39;s configuration — which holds provider keys
the app pushed into it, so clearing it is the point. <strong>It does not touch your <code>data</code>
folder</strong>, and neither does a plain uninstall.</p>
</li>
<li><p><strong>Your data</strong>, if and when you want it gone. It is the single folder named under
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
<td>Linux</td>
<td><code>~/.config/SmartBrain</code> and <code>~/.local/share/smartbrain</code></td>
</tr>
</tbody></table>
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
<p>Open <strong>Settings → Cloud providers</strong> and add a key for any of:</p>
<ul>
<li><strong>OpenAI</strong></li>
<li><strong>Anthropic</strong></li>
<li><strong>Google (Gemini)</strong></li>
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
<h2 id="local-models-on-your-machine">Local models (on your machine)</h2>
<p>Local models keep every prompt on your hardware — nothing goes to a provider. You run the
model server yourself and SmartBrain reaches it over loopback on your own machine.
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
<strong>MLX embeddings server</strong> (<code>tools/mlx_embed_server/install.sh</code> — a tiny login service on
port 8899 serving <code>Qwen3-Embedding-0.6B</code> with correct pooling), connected under
Settings → Local models → <strong>MLX embeddings</strong> and routed to <code>mlxe/qwen3-embedding-0.6b</code>.</p>
<p><strong>Pull it yourself</strong> once, with that exact tag:</p>
<pre><code class="language-sh">ollama pull nomic-embed-text:v1.5
</code></pre>
<p>(A from-source install does this for you when Ollama is present, and
<code>python3 installer/install.py doctor</code> offers to.)</p>
<p>The tag matters: the bare <code>nomic-embed-text</code> won&#39;t resolve. If semantic search shows
keyword results and says <em>&quot;degraded&quot;</em>, this model isn&#39;t pulled — run the command above
and <strong>Reindex</strong>. You can change the model, but pointing embeddings at a cloud provider
sends your documents there on every reindex — only do that if you accept that tradeoff.</p>
<h2 id="next">Next</h2>
<ul>
<li><a href="#features">Using SmartBrain_3000</a> — start chatting and add knowledge.</li>
<li><a href="#mcp">Connect external tools</a> — let a desktop AI client (e.g. Claude Desktop) read your Knowledge.</li>
</ul>
`},{slug:`features`,title:`Using SmartBrain_3000`,html:`<h1 id="using-smartbrain_3000">Using SmartBrain_3000</h1>
<p>Everything here runs locally and is encrypted at rest. Here&#39;s what each area does.</p>
<p>The <strong>Desktop</strong> is the main surface and shows everything below. On a <strong>paired phone</strong>
(<a href="#remote-access">Remote access</a>) you get a trimmed set for use on the go — Chat,
Knowledge, Planner, Schedules, Email, and Activity — while Settings and setup stay on the Desktop.</p>
<h2 id="chat">Chat</h2>
<p>Talk to your assistant. Chat can optionally <strong>use tools</strong> to act on your behalf —
search your knowledge, <strong>read or summarize a whole document</strong>, <strong>save a note back to
your knowledge</strong>, add a task, fetch a public web page, send an email, and more. Replies
are formatted (headings, lists, tables, and code blocks render properly). You can
<strong>Stop</strong> an answer mid-stream, <strong>Copy</strong> any reply, <strong>Regenerate</strong> the latest one, and
<strong>Rename</strong> a saved chat. Deleting a chat — or using <strong>Delete all…</strong> next to the saved-chats
picker — moves it to the <strong>Trash</strong>, where it can be restored for 30 days from
Settings → Account &amp; Data.</p>
<p>The assistant also knows what time it is <strong>where you are</strong>. Your browser reports its
timezone and every turn is told your local date and time, with UTC alongside for
cross-zone questions; scheduled runs get the same. There is nothing to configure — the
zone is read from your browser and stored locally, like any other setting.</p>
<p>Tools are <strong>risk-tiered</strong>, and this is the core safety idea:</p>
<ul>
<li><strong>Observe</strong> (e.g. knowledge search) runs automatically — it only reads.</li>
<li><strong>Reviewed</strong> (e.g. add a task, search the web) is <strong>never run automatically</strong> until you
say so. The assistant <em>proposes</em> it and it waits for your approval in <strong>Activity</strong>. If
you get tired of approving the same tool, <strong>Always allow</strong> lets that one run without
asking from then on — and <strong>Stop allowing</strong> takes it back.</li>
<li><strong>Irreversible</strong> (e.g. send an email, delete a task) always waits for your approval, with
an extra confirmation, and can never be pre-authorized.</li>
</ul>
<p>So the assistant can draft and suggest, but anything that changes data or reaches
out requires your explicit OK. Every tool attempt is written to the audit log.</p>
<p><strong>For example:</strong> ask <em>&quot;search my knowledge for the lease terms&quot;</em> and the assistant
reads and answers immediately (Observe). Ask <em>&quot;email the landlord about it&quot;</em> and it
<strong>drafts</strong> the message but <strong>parks it in Activity</strong> — nothing sends until you open
Activity and approve (Irreversible, with an extra confirm).</p>
<h2 id="knowledge">Knowledge</h2>
<p>A private, encrypted knowledge base. Drag in <strong>PDFs, Word (.docx), PowerPoint (.pptx),
Excel (.xlsx), HTML, Markdown, CSV/JSON and other text files</strong> — many files in one drop if
you like — paste a URL, or write a note. <strong>Big documents are welcome</strong>: a
several-hundred-page PDF is fine. Uploads don&#39;t block: they land right away, keyword search
works within seconds, and meaning-search for a very large document fills in over the next
few minutes in the background (it resumes by itself after a restart). Adding the same content twice is a no-op — SmartBrain
recognises it and keeps the one copy rather than cluttering your results with duplicates.</p>
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
Editing tags is instant and never re-indexes the document.</p>
<p><strong>Try it:</strong> open <strong>Knowledge</strong>, drag in a document, and search it. Then ask <strong>Chat</strong>
<em>&quot;what does my knowledge say about …&quot;</em> — the assistant searches it for you and tells you
which file and page it got the answer from.</p>
<p><img src="assets/05-knowledge.png" alt="The Knowledge page: add a document, then search it"></p>
<p><img src="assets/gifs/04-add-knowledge.gif" alt="Drop in a file, search it, open the cited passage, then ask Chat — answers cite their sources"></p>
<blockquote>
<p>Semantic search needs an embedding model set up for your backend. If results say
<em>&quot;degraded&quot;</em>, set one up — see
<a href="#models__embeddings-for-knowledge-search">Embeddings</a> — then <strong>Reindex</strong>.</p>
</blockquote>
<p>Your knowledge is also what external tools can read over <a href="#mcp">MCP</a>.
Group documents into <strong>vaults</strong> to scope a search — and to share them, privately
or publicly: see <a href="#vaults">Share knowledge with Vaults</a>.</p>
<h2 id="planner">Planner</h2>
<p><img src="assets/gifs/06-planner.gif" alt="Planner — tasks grouped Today / This week / by due date"></p>
<p>Simple task tracking — add tasks with optional due dates; they group into Today /
This week / Later. The assistant can propose new tasks (which you approve).</p>
<h2 id="schedules">Schedules</h2>
<p><img src="assets/gifs/07-schedule-a-prompt.gif" alt="Schedules — run a prompt on a timer, then Run now"></p>
<p>Run a prompt on a timer — e.g. &quot;every morning, summarize my open tasks.&quot; A
schedule fires an assistant turn on its cadence. Two things to know:</p>
<ul>
<li>Schedules only run <strong>while the app is unlocked</strong> (a locked vault can&#39;t decrypt
or act — there&#39;s no background access to your data).</li>
<li>If a scheduled run wants to do something <strong>dangerous</strong> (send, delete, etc.), it
<strong>parks for your approval</strong> in Activity just like in chat — it won&#39;t act alone.</li>
</ul>
<p>Use <strong>Run now</strong> to fire one immediately.</p>
<p>A run&#39;s output lands in four places: it appears <strong>in your open Chat</strong> (as a
&quot;Scheduled Item&quot; notice), in the schedule&#39;s <strong>Output</strong> tab, as a durable copy on the
<strong>Info</strong> page, and the Chat tab shows a badge while results are unseen.</p>
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
<p>Once connected you can read recent mail and compose/send:</p>
<ul>
<li><strong>You</strong> sending from the app is a direct action.</li>
<li>The <strong>assistant</strong> sending email is an <strong>Irreversible</strong> tool — it always parks
for your approval first. It can draft; you approve the send.</li>
</ul>
<h2 id="memory">Memory</h2>
<p><strong>Settings → Memory</strong> holds who the assistant is for: your name, its name, standing
custom instructions, and a list of remembered facts. Everything there is encrypted and
injected into every conversation, so it&#39;s the place to look when you wonder &quot;why does it
keep doing that?&quot; — including any <em>&quot;(learned) …&quot;</em> facts self-improvement added (delete
one to permanently reject it).</p>
<h2 id="web-search">Web search</h2>
<p>The assistant&#39;s web tools search with <strong>DuckDuckGo by default — no key needed</strong>. Under
<strong>Settings → Web search</strong> you can switch engines: bring your own <strong>Brave Search</strong> or
<strong>Tavily</strong> key, or point at a self-hosted <strong>SearXNG</strong>. Searches only happen when the
assistant actually uses the web tools in a turn; see
<a href="#privacy-security">Privacy &amp; security</a> for exactly what leaves your machine.</p>
<h2 id="self-improvement">Self-improvement</h2>
<p>SmartBrain can review its own recent performance and carefully improve — <strong>off by
default</strong>, and switched on under <strong>Settings → Self-improvement</strong>. Every 8 hours (while
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
<p>A running estimate of what your <strong>cloud</strong> models cost. <strong>Usage</strong> shows estimated
spend per model over a date range (today, last 5/10/30 days, or a custom range),
computed from each provider&#39;s live pricing, with a total. <strong>Local models (Ollama,
MLX) are free</strong> and show as such. Usage appears here after you chat with a model;
none of your usage or token data leaves your machine — it&#39;s computed locally from your
token counts (the only network call is a local fetch of the model price list from the
on-device gateway).</p>
<h2 id="activity">Activity</h2>
<p><img src="assets/gifs/05-approve-an-action.gif" alt="The safety loop — the assistant proposes, you approve in Activity"></p>
<p>Your audit + approvals view:</p>
<ul>
<li><strong>Awaiting your approval</strong> — review what the assistant proposed and <strong>Approve</strong> or
<strong>Deny</strong> it. <strong>Always allow</strong> approves it and stops asking for that tool from then on;
anything pre-authorized this way is listed under <strong>Always allowed</strong>, where <strong>Stop
allowing</strong> takes the permission back. Irreversible tools can&#39;t be pre-authorized —
they ask every time.</li>
<li><strong>Audit log</strong> — an encrypted record of every tool attempt (what, when, outcome).</li>
</ul>
<h2 id="next">Next</h2>
<ul>
<li><a href="#vaults">Share knowledge with Vaults</a> — sealed shares, public publishing, subscriptions.</li>
<li><a href="#mcp">Connect external tools</a> via MCP.</li>
<li><a href="#backup-recovery">Backup &amp; recovery</a>.</li>
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
<li><strong>Share it.</strong> <strong>Export</strong> a vault and SmartBrain seals it into a single <code>.sbvault</code> file and
shows you a one-time key (starting <code>SBVK1-</code>). Send the file however you like, then give the
person the key over a <strong>different</strong> channel — together they are the contents in the clear,
so keep them apart.</li>
<li><strong>Share it publicly.</strong> Choose <strong>Public</strong> in the share panel instead: the export is the same
<code>.sbvault</code> file with <strong>no key at all</strong> — anyone with the link can read everything in this
vault, and there is <strong>no taking it back</strong>. Upload the file anywhere (Drive, S3, any web host)
and share the link — or unzip it and upload the folder to a static host so future updates only
re-upload what changed. Once published, the vault card shows a <strong>Public</strong> badge beside your
publisher fingerprint (<code>SB-…</code>) and the published version — the identity and version readers will
see. The file is still signed, so nobody else can publish an &quot;update&quot; to your vault in your name.
To publish a <strong>new version</strong>, export it again (replacing the file where you host it): the version
bumps automatically, and the button reads <strong>Export update (v<em>N</em>)</strong> so you know where it lands.</li>
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
and new versions are offered as updates whenever the docs change.</p>
<p>Creating, adding, and searching a vault work everywhere, including a paired phone. <strong>Exporting and
importing a vault are done on the Desktop</strong> — sharing a vault&#39;s contents, or bringing new ones in, is
sensitive, so those actions live in the Desktop app.</p>
`},{slug:`mcp`,title:`Connect external tools (MCP)`,html:`<h1 id="connect-external-tools-mcp">Connect external tools (MCP)</h1>
<p>SmartBrain_3000 is also an <strong>MCP server</strong> — it can expose your <strong>Knowledge base
(read-only)</strong> to a desktop AI client (e.g. Claude Desktop, Cursor). The
tool reads your knowledge to ground its answers; it can&#39;t change anything.</p>
<h2 id="turn-it-on">Turn it on</h2>
<p>Open <strong>Settings → Connections (MCP)</strong> and <strong>generate an access token</strong>. MCP is <strong>off until a
token exists</strong> — generating one enables it; revoking it turns access off again.</p>
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
<p>The client can then call the read-only Knowledge tools (search and read your documents).</p>
<h2 id="what-it-can-and-cant-do">What it can and can&#39;t do</h2>
<ul>
<li><strong>Can:</strong> search and read your Knowledge base. Content that came from an imported or
subscribed vault is labeled with its provenance (which vault, whose key), so a client
can treat third-party knowledge as data rather than instructions.</li>
<li><strong>Can&#39;t:</strong> see your credentials, write or delete anything, or reach other
features — and by default it&#39;s reachable only from your own machine (loopback); it
follows the app&#39;s host binding, so a LAN/HTTPS setup that exposes the app exposes it
too. The token is stored encrypted at rest; revoke any time in Settings → Connections (MCP).</li>
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
<p><strong>Export data (JSON)</strong> downloads your content — knowledge, chats, tasks,
memories, profile — as readable JSON. It&#39;s decrypted (it&#39;s yours), so keep the
file somewhere safe. Good for reading your data elsewhere or migrating out.
Because it hands out decrypted data, it runs on the <strong>Desktop only</strong> (never from a
paired phone) and <strong>re-prompts for your passphrase</strong> to authorize.</p>
<h2 id="encrypted-backup">Encrypted backup</h2>
<p><strong>Download encrypted backup</strong> gives you a complete, portable copy of the database
(a <code>.duckdb</code> file). It&#39;s still encrypted — it includes your wrapped keys — so it
restores with the <strong>same passphrase</strong>. This is the one to keep for disaster
recovery and to move your install to a new machine. Like Export, it&#39;s
<strong>Desktop-only</strong> and <strong>re-prompts for your passphrase</strong> before it hands over the vault.</p>
<h2 id="restore">Restore</h2>
<p><strong>Stage restore</strong> takes a backup file, validates it, and applies it the <strong>next
time SmartBrain_3000 restarts</strong> (swapping the live database while it&#39;s running
isn&#39;t safe). Your current database is kept alongside as <code>*.pre-restore-&lt;timestamp&gt;</code>,
so a restore is reversible.</p>
<ul>
<li>Allowed when you&#39;re <strong>unlocked</strong>, or onto a <strong>fresh install</strong> (moving to a new
machine) — never over a locked, initialized vault.</li>
<li>After staging, restart SmartBrain — <strong>Restart</strong> in the menu-bar / tray menu (from
source, <code>python3 installer/install.py update</code>) — and unlock with that backup&#39;s
passphrase.</li>
<li>A backup from a <strong>newer version</strong> of SmartBrain_3000 is <strong>refused on purpose</strong>
(it would risk data loss under older code): upgrade this app first, then restore.</li>
</ul>
<h2 id="starting-completely-fresh">Starting completely fresh</h2>
<p>If an install is broken in a way that restarting and updating cannot fix, there is a full
reset: back up, remove everything SmartBrain put on the machine, install the latest
version, and restore your data.</p>
<p><strong>You almost certainly do not need this.</strong> Try quitting and reopening SmartBrain from the
menu bar first, then <code>brew update &amp;&amp; brew upgrade --cask smartbrain</code>, then a hard reload of
the browser tab. A full reset re-downloads the whole app and takes 10–30 minutes.</p>
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
<p>macOS only for now. On Windows the same five steps apply, against <code>%APPDATA%\\SmartBrain</code>
and the Scoop package.</p>
<h2 id="chat-trash">Chat Trash</h2>
<p>Deleted chats (one at a time, or Chat&#39;s <strong>Delete all…</strong>) land here for <strong>30 days</strong> —
restore any of them, or <strong>Empty trash now</strong> to purge them immediately. After 30 days
they&#39;re removed for good automatically.</p>
<h2 id="change-your-passphrase">Change your passphrase</h2>
<p><strong>Change passphrase</strong> re-wraps your master key under a new passphrase after
verifying the current one. Your data and your Recovery Key stay valid — only the
passphrase changes.</p>
<h2 id="forgot-your-passphrase">Forgot your passphrase?</h2>
<p>There is <strong>no server and no reset</strong>. Use your <strong>Recovery Key</strong> from the Emergency
Kit you saved during setup:</p>
<ol>
<li>Lock / reopen the app and choose <strong>Unlock with Recovery Key</strong>.</li>
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
extra confirm for irreversible actions. Everything it attempts is audited.</li>
<li><strong>Credential firewall.</strong> Tools and connected MCP clients act on your behalf but
never receive your raw keys or tokens.</li>
<li><strong>Web-fetch guard.</strong> The web-fetch tool refuses private/internal addresses and
doesn&#39;t follow redirects into them (anti-SSRF).</li>
</ul>
<h2 id="what-leaves-your-machine-and-when">What leaves your machine (and when)</h2>
<ul>
<li><strong>Cloud model calls.</strong> If you use an OpenAI/Anthropic/Google model, your prompts
and the content you send go to that provider. Use a <strong>local model</strong> (Ollama/MLX)
to keep everything on-box.</li>
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
<li><strong>Update checks — by the desktop app, not by SmartBrain.</strong> Every six hours the menu-bar
launcher asks GitHub whether a newer release exists, and downloads it from GitHub if so.
That request carries no identity and nothing about you or your data; it is the same
public release page anyone can open. SmartBrain itself makes no such call — it hears
about a waiting update from the launcher over the local heartbeat they already exchange.</li>
<li><strong>Nothing else.</strong> Beyond the above, the app makes no outbound calls. Self-improvement
(if you enable it) is fully local by design: its reviews and learning run on your
machine against a local model only — it never sends your activity anywhere.</li>
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
<p>On the <strong>Desktop</strong>, open <strong>Settings → Remote access</strong>, give the phone a name, and tap
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
<p>That&#39;s it — the phone connects, from Wi-Fi or cellular. The code lasts a few minutes; if it
expires, tap <strong>Pair a new phone</strong> for a fresh one.</p>
<blockquote>
<p>Why install first? On iPhone, an app on the Home Screen has its own private storage, separate
from Safari — so pairing happens <em>in the installed app</em>. The QR&#39;s only job is to open the site
so you can install it; it carries no secret.</p>
</blockquote>
<h2 id="using-it-on-your-phone">Using it on your phone</h2>
<p>The phone shows a <strong>trimmed set</strong> of areas meant for use on the go: <strong>Chat</strong>,
<strong>Knowledge</strong>, <strong>Planner</strong>, <strong>Schedules</strong>, <strong>Email</strong>, and <strong>Activity</strong>. Settings and
first-time setup live on the <strong>Desktop</strong>.</p>
<p>A small <strong>&quot;Remote&quot;</strong> chip shows the connection state: <strong>direct</strong> (phone-to-Desktop),
<strong>relayed</strong> (through the encrypted relay), or <strong>BLOCKED</strong> in red if your Desktop&#39;s
identity can&#39;t be verified — re-pair if you reinstalled the app.</p>
<h2 id="manage-devices">Manage devices</h2>
<p>Under <strong>Settings → Remote access</strong> you can pair more devices and <strong>Revoke</strong> any device
at any time. A revoked device can no longer connect.</p>
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
<p><strong>Why:</strong> this trades some unattended resilience for security. The upside is
that data at rest is never decryptable without your passphrase or Recovery Key,
even if someone copies the disk. The cost is that an unattended restart leaves
the app locked until you return.</p>
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
dimensions). Very large libraries are bounded by an explicit ceiling, and if a corpus
exceeds it that is <strong>reported, not silently ignored</strong>.</li>
<li><strong>Nothing is written to disk.</strong> The index is never persisted, so encryption at rest is
unchanged: it exists only while the vault is unlocked and dies with the master key.</li>
</ul>
<p><strong>Why:</strong> indexing encrypted content on disk without leaking it is hard. Rebuilding in memory
keeps the encryption promise intact while still giving fast, whole-corpus search.</p>
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
`}],x=e(`<a> </a>`),S=e(`<div class="help svelte-1vby5nc"><nav class="help-nav svelte-1vby5nc" aria-label="Help sections"><h2 class="svelte-1vby5nc">Help</h2> <!></nav> <article class="help-body card svelte-1vby5nc"></article></div>`);function C(e,C){h(C,!0);let w=d(()=>y.url.hash.replace(/^#/,``).split(`__`)),T=d(()=>b.find(e=>e.slug===i(w)[0])??b[0]),E=d(()=>i(w)[1]),D=u(void 0);c(()=>{if(i(T),typeof window>`u`||!i(D))return;let e=window.matchMedia(`(prefers-reduced-motion: reduce)`);for(let e of i(D).querySelectorAll(`pre`))e.tabIndex=0;let t=()=>{for(let t of i(D).querySelectorAll(`img`)){let n=t.getAttribute(`src`)??``;e.matches&&n.endsWith(`.gif`)?(t.dataset.gif=n,t.setAttribute(`src`,n.replace(/\.gif$/,`.poster.png`))):!e.matches&&t.dataset.gif&&(t.setAttribute(`src`,t.dataset.gif),delete t.dataset.gif)}};return t(),e.addEventListener(`change`,t),()=>e.removeEventListener(`change`,t)}),c(()=>{i(T);let e=i(E);typeof window>`u`||!i(D)||!e||!/^[\w-]+$/.test(e)||i(D).querySelector(`[id="${e}"]`)?.scrollIntoView()});var O=S(),k=l(O);v(o(l(k),2),17,()=>b,e=>e.slug,(e,n)=>{var a=x();let o;var c=l(a,!0);g(a),t(()=>{o=f(a,1,`help-link svelte-1vby5nc`,null,o,{active:i(n).slug===i(T).slug}),m(a,`aria-current`,i(n).slug===i(T).slug?`page`:void 0),m(a,`href`,`#${i(n).slug}`),r(c,i(n).title)}),s(e,a)}),g(k);var A=o(k,2);n(A,()=>i(T).html,!0),g(A),p(A,e=>a(D,e),()=>i(D)),g(O),s(e,O),_()}export{C as component};