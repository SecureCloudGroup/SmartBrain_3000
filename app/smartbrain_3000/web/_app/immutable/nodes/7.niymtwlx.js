import{$ as e,C as t,D as n,E as r,G as i,J as a,P as o,Q as s,R as c,S as l,U as u,Y as d,c as f,it as p,k as m,p as h,q as g,y as _,z as v}from"../chunks/9DS1GQYk.js";import"../chunks/xihTtKlq.js";import{t as y}from"../chunks/CvvHLdVa.js";var b=[{slug:`getting-started`,title:`Getting started`,html:`<h1 id="getting-started">Getting started</h1>
<p>SmartBrain_3000 is a <strong>local-first, single-user AI assistant</strong> that runs entirely
on your own machine within a container (Docker). Your data and credentials stay on-box, encrypted
at rest. The only outbound calls it makes are to services you explicitly opt into:
the AI providers you configure, and Google&#39;s APIs if you connect Gmail. See
<a href="#privacy-security">Privacy &amp; security</a> for the full picture.</p>
<h2 id="what-you-need">What you need</h2>
<p><strong>Docker</strong> — that&#39;s the only requirement. Install
<a href="https://docs.docker.com/get-docker/">Docker Desktop</a> (macOS/Windows) or Docker Engine /
Colima / OrbStack (Linux/macOS), and make sure it&#39;s running.</p>
<p>That&#39;s it — no accounts, and no config files to edit. SmartBrain runs as a small,
prebuilt image it downloads for you; your data stays on your machine.</p>
<h2 id="install">Install</h2>
<p>The easiest way is a package manager. It installs the SmartBrain <strong>desktop app</strong> — a small
menu-bar / system-tray launcher that starts Docker if needed, brings up the stack, and opens
the app in your browser. The download page is <strong><a href="https://smartbrain.securecloudgroup.com">https://smartbrain.securecloudgroup.com</a></strong>, or
run the command for your system:</p>
<p><strong>macOS</strong> — in the Terminal app:</p>
<pre><code class="language-sh">brew install --cask securecloudgroup/tap/smartbrain
</code></pre>
<p><strong>Windows</strong> — in Terminal or PowerShell, using <a href="https://scoop.sh">Scoop</a>:</p>
<pre><code class="language-powershell">scoop bucket add securecloudgroup https://github.com/SecureCloudGroup/scoop-bucket
scoop install securecloudgroup/smartbrain
</code></pre>
<p>(<code>winget install SecureCloudGroup.SmartBrain</code> is coming soon.)</p>
<p><strong>Any OS, straight from Docker</strong> — download the release compose file and run it (no app, no
clone):</p>
<pre><code class="language-sh">curl -fsSLO https://raw.githubusercontent.com/SecureCloudGroup/SmartBrain_3000/main/compose/docker-compose.release.yml
docker compose -f docker-compose.release.yml up -d
</code></pre>
<p>Open the app (the launcher does this for you) at <strong><a href="http://localhost:33000">http://localhost:33000</a></strong>. The first run
<strong>downloads the app image</strong> — a minute or two — and after that it starts instantly. Then
complete first-run setup below.</p>
<h3 id="install-from-source-for-contributors">Install from source (for contributors)</h3>
<p>Building from the repo is slower — it compiles the image locally — and additionally needs
<strong>git</strong> and <strong>Python 3</strong>. Use it when you&#39;re developing on the code:</p>
<pre><code class="language-sh">git clone https://github.com/SecureCloudGroup/SmartBrain_3000.git
cd SmartBrain_3000
python3 installer/install.py install
</code></pre>
<p><code>python3 installer/install.py doctor</code> checks and offers to fix common problems (start Docker,
restart the stack, pull the embedding model). See <a href="#">installer/</a>.</p>
<h2 id="first-run">First run</h2>
<p>The first time you open the app it walks you through setup:</p>
<ol>
<li><strong>Choose a passphrase</strong> (at least 8 characters). This encrypts everything.</li>
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
<li><p><strong>Connect a model.</strong> Open <strong>Chat</strong>. If you&#39;re already running Ollama, you&#39;ll see
<em>&quot;Found Ollama running on this machine&quot;</em> — tap <strong>Connect</strong> and you&#39;re set. No
Ollama? Add a cloud key under <strong>Settings → Cloud providers</strong>, or
<a href="https://ollama.com/download">install Ollama</a> and pull a model
(<code>ollama pull llama3.1:8b</code>). See <a href="#models">Connect a model</a>.</p>
<p><img src="assets/01-chat-connect.png" alt="Chat offering a one-tap connect for a detected Ollama server"></p>
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
<p>How you update depends on how you installed:</p>
<ul>
<li><strong>Homebrew (macOS):</strong> <code>brew update &amp;&amp; brew upgrade --cask smartbrain</code>, then reopen the app.</li>
<li><strong>Scoop (Windows):</strong> <code>scoop update smartbrain</code>, then reopen the app.</li>
<li><strong>Release compose / desktop app:</strong> it pulls the latest image on start, so updating is just a
<strong>restart</strong> — use the launcher&#39;s <strong>Restart</strong>, or run <code>docker compose -f docker-compose.release.yml up -d</code> again.</li>
<li><strong>From source:</strong> <code>python3 installer/install.py update</code> — it <strong>backs up your encrypted data
first</strong>, pulls the latest code, rebuilds the image, restarts the stack, and verifies it&#39;s
healthy. It prompts before making changes and runs on the host, never inside the container.</li>
</ul>
<p>Your data is kept in Docker volumes on your machine and is left untouched by an update. (More on
backups: <a href="#backup-recovery">Backup &amp; recovery</a>.)</p>
<h2 id="troubleshooting">Troubleshooting</h2>
<p>Most first-run problems are one of these:</p>
<ul>
<li><strong>macOS asks if SmartBrain may &quot;access data from other apps.&quot;</strong> Click <strong>Allow</strong> — that&#39;s the
launcher locating your Docker installation; it reads nothing else.</li>
<li><strong>&quot;Docker daemon not reachable&quot; / it fails immediately.</strong> Docker isn&#39;t running. Start
Docker Desktop (or <code>colima start</code>), then click <strong>Restart</strong> in the menu (or reopen the app).
Note: Docker Desktop&#39;s very first launch asks you to accept its terms — do that first.</li>
<li><strong>The page won&#39;t load at <a href="http://localhost:33000">http://localhost:33000</a>.</strong> Give a first run another minute (it&#39;s
downloading the image) — the menu&#39;s status line says what it&#39;s doing. If it reads <em>&quot;Still
warming up&quot;</em>, click <strong>Open SmartBrain</strong> again in a moment. Still stuck? Check the logs:
<code>docker compose -f docker-compose.release.yml logs smartbrain</code> (from source, use
<code>compose/docker-compose.yml</code>).</li>
<li><strong>Chat says &quot;No models available yet.&quot;</strong> You haven&#39;t connected a model. If Ollama
is running, the Chat screen offers a one-tap <strong>Connect</strong>; otherwise add a cloud key
under <strong>Settings → Cloud providers</strong>. See <a href="#models">Connect a model</a>.</li>
<li><strong>Semantic search returns keyword results (&quot;degraded&quot;).</strong> The embedding model isn&#39;t
pulled. On the Desktop run <code>ollama pull nomic-embed-text:v1.5</code> (the installer and
<code>doctor</code> try to do this for you), then <strong>Reindex</strong> in Knowledge.</li>
<li><strong>The browser warns about the certificate</strong> (only if you set up LAN/HTTPS). Trust
the local mkcert CA — see <a href="#remote-access">Remote access</a>.</li>
<li><strong>&quot;Database is newer than this app&quot; / a restore is refused.</strong> Pointing an older build
at a newer data directory, or restoring a backup from a newer version, is refused on
purpose to prevent data loss. Upgrade SmartBrain_3000 first (<code>install.py update</code>), then
reopen or retry the restore.</li>
</ul>
<h2 id="next">Next</h2>
<ul>
<li><a href="#models">Connect a model</a> — add a cloud provider key or a local model.</li>
<li><a href="#features">Using SmartBrain_3000</a> — chat, knowledge, planner, schedules, email.</li>
</ul>
`},{slug:`models`,title:`Connect a model`,html:`<h1 id="connect-a-model">Connect a model</h1>
<p>SmartBrain_3000 talks to language models through a local <strong>gateway</strong> (Bifrost),
which runs as part of the stack. You can use <strong>cloud providers</strong> (with your own
API keys) and/or <strong>local models</strong> running on your machine. Nothing is sent to a
provider unless you configure it and use it.</p>
<h2 id="cloud-providers-your-api-keys">Cloud providers (your API keys)</h2>
<p>Open <strong>Settings → Cloud providers</strong> and add a key for any of:</p>
<ul>
<li><strong>OpenAI</strong></li>
<li><strong>Anthropic</strong></li>
<li><strong>Google (Gemini)</strong></li>
</ul>
<p><img src="assets/02-providers.png" alt="Settings → Cloud providers, with key fields for OpenAI, Anthropic, and Google"></p>
<p><img src="assets/gifs/02-connect-a-model.gif" alt="Connect a model — one-tap local Ollama, or an encrypted cloud key"></p>
<p>Keys are stored <strong>encrypted on your machine</strong> and pushed to the local gateway
while you&#39;re unlocked; locking removes them from the gateway again. The app never
returns a stored key over its API — only the fact that one is set.</p>
<blockquote>
<p>Using a cloud model means your prompts (and any content you send) go to that
provider. If you&#39;d rather keep everything on your machine, use a local model.</p>
</blockquote>
<h2 id="local-models-on-your-machine">Local models (on your machine)</h2>
<p>Local models run on the <strong>host</strong> (not inside the container), and the app reaches
them at <code>host.docker.internal</code>. Open <strong>Settings → Local models</strong> to connect them.</p>
<ul>
<li><strong>Ollama</strong> — works on any OS. Install it, pull a model
(e.g. <code>ollama pull llama3.1:8b</code>), and point SmartBrain_3000 at it.</li>
<li><strong>MLX</strong> — Apple-Silicon Macs only, run on the host.</li>
</ul>
<p>The panel shows whether each is reachable and which models it has.</p>
<blockquote>
<p><strong>Already running Ollama?</strong> You usually don&#39;t need to touch this panel.
SmartBrain detects a local server on its default port and offers a one-tap
<strong>Connect</strong> — on the <strong>Chat</strong> screen when you have no model yet, and here under
the port field. The manual port/URL fields are for non-standard setups.</p>
</blockquote>
<p><img src="assets/03-local-models.png" alt="Settings → Local models showing a detected Ollama server with a Connect link"></p>
<h2 id="embeddings-for-knowledge-search">Embeddings (for Knowledge search)</h2>
<p>Semantic search in the <a href="#features">Knowledge base</a> needs an <strong>embedding
model</strong>. The default is a <strong>local</strong> Ollama model — <code>nomic-embed-text:v1.5</code> — so
your knowledge content stays on-box.</p>
<p><strong>The installer pulls this for you</strong> when Ollama is present (and
<code>python3 installer/install.py doctor</code> offers to). If you ever need to do it by hand,
pull that exact tag:</p>
<pre><code class="language-sh">ollama pull nomic-embed-text:v1.5
</code></pre>
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
are formatted (headings, lists, tables, and code blocks render properly).</p>
<p>Tools are <strong>risk-tiered</strong>, and this is the core safety idea:</p>
<ul>
<li><strong>Observe</strong> (e.g. knowledge search) runs automatically — it only reads.</li>
<li><strong>Reviewed / Irreversible</strong> (e.g. add a task, send an email, delete a task) are
<strong>never run automatically</strong>. The assistant <em>proposes</em> them and they wait for
your approval in <strong>Activity</strong>. Irreversible actions need an extra confirmation.</li>
</ul>
<p>So the assistant can draft and suggest, but anything that changes data or reaches
out requires your explicit OK. Every tool attempt is written to the audit log.</p>
<p><strong>For example:</strong> ask <em>&quot;search my knowledge for the lease terms&quot;</em> and the assistant
reads and answers immediately (Observe). Ask <em>&quot;email the landlord about it&quot;</em> and it
<strong>drafts</strong> the message but <strong>parks it in Activity</strong> — nothing sends until you open
Activity and approve (Irreversible, with an extra confirm).</p>
<h2 id="knowledge">Knowledge</h2>
<p>A private, encrypted knowledge base. Drag in <strong>PDFs, Word (.docx), PowerPoint (.pptx),
Excel (.xlsx), HTML and text files</strong> — many files in one drop if you like — paste a URL,
or write a note. Uploads don&#39;t block: they land right away and a background indexer makes
them searchable within seconds. Adding the same content twice is a no-op — SmartBrain
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
cites its sources the same way, so you can check any claim against the original.</p>
<p><strong>Try it:</strong> open <strong>Knowledge</strong>, drag in a document, and search it. Then ask <strong>Chat</strong>
<em>&quot;what does my knowledge say about …&quot;</em> — the assistant searches it for you and tells you
which file and page it got the answer from.</p>
<p><img src="assets/05-knowledge.png" alt="The Knowledge page: add a document, then search it"></p>
<p><img src="assets/gifs/04-add-knowledge.gif" alt="Add a document, then search your knowledge and ask Chat about it"></p>
<blockquote>
<p>Semantic search needs the embedding model pulled (the installer does this for you).
If results say <em>&quot;degraded&quot;</em>, run <code>ollama pull nomic-embed-text:v1.5</code> on the Desktop
and Reindex — see <a href="#models__embeddings-for-knowledge-search">Embeddings</a>.</p>
</blockquote>
<p>Your knowledge is also what external tools can read over <a href="#mcp">MCP</a>.</p>
<h2 id="vaults">Vaults</h2>
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
<li><strong>Import someone else&#39;s.</strong> Pick the <code>.sbvault</code> file and paste the key. Its documents are
<strong>re-encrypted under your own passphrase</strong> as they land (nothing you import can read or
weaken your data), and anything you already have is kept as-is rather than overwritten. The
result shows the publisher&#39;s fingerprint — the one thing that says <em>who</em> the knowledge came
from.</li>
</ul>
<p>Creating, adding, and searching a vault work everywhere, including a paired phone. <strong>Exporting and
importing a vault are done on the Desktop</strong> — sharing a vault&#39;s contents, or bringing new ones in, is
sensitive, so those actions live in the Desktop app.</p>
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
<li><strong>Pending approvals</strong> — review and approve/deny what the assistant proposed.</li>
<li><strong>Audit log</strong> — an encrypted record of every tool attempt (what, when, outcome).</li>
</ul>
<h2 id="next">Next</h2>
<ul>
<li><a href="#mcp">Connect external tools</a> via MCP.</li>
<li><a href="#backup-recovery">Backup &amp; recovery</a>.</li>
</ul>
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
<li><strong>Can:</strong> search and read your Knowledge base.</li>
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
<li>After staging, restart the stack (<code>python3 installer/install.py update</code>, or
restart the container) and unlock with that backup&#39;s passphrase.</li>
<li>A backup from a <strong>newer version</strong> of SmartBrain_3000 is <strong>refused on purpose</strong>
(it would risk data loss under older code): upgrade this app first, then restore.</li>
</ul>
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
<li><strong>Local-first.</strong> Everything runs in Docker on your machine: no account server and
no telemetry. The only SmartBrain-operated service is the optional, content-blind
signaling node for remote phone access — off by default (see below).</li>
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
<li><strong>Nothing else.</strong> Beyond the above, the app makes no outbound calls.</li>
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
<strong>Knowledge</strong>, <strong>Planner</strong>, <strong>Email</strong>, and <strong>Activity</strong>. Settings and first-time setup
live on the <strong>Desktop</strong>.</p>
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
surface for little benefit at this scale. One owner, one key, one vault.</p>
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
`}],x=m(`<a> </a>`),S=m(`<div class="help svelte-1vby5nc"><nav class="help-nav svelte-1vby5nc" aria-label="Help sections"><h2 class="svelte-1vby5nc">Help</h2> <!></nav> <article class="help-body card svelte-1vby5nc"></article></div>`);function C(m,C){e(C,!0);let w=d(()=>y.url.hash.replace(/^#/,``).split(`__`)),T=d(()=>b.find(e=>e.slug===o(w)[0])??b[0]),E=d(()=>o(w)[1]),D=a(void 0);v(()=>{if(o(T),typeof window>`u`||!o(D))return;let e=window.matchMedia(`(prefers-reduced-motion: reduce)`),t=()=>{for(let t of o(D).querySelectorAll(`img`)){let n=t.getAttribute(`src`)??``;e.matches&&n.endsWith(`.gif`)?(t.dataset.gif=n,t.setAttribute(`src`,n.replace(/\.gif$/,`.poster.png`))):!e.matches&&t.dataset.gif&&(t.setAttribute(`src`,t.dataset.gif),delete t.dataset.gif)}};return t(),e.addEventListener(`change`,t),()=>e.removeEventListener(`change`,t)}),v(()=>{o(T);let e=o(E);typeof window>`u`||!o(D)||!e||!/^[\w-]+$/.test(e)||o(D).querySelector(`[id="${e}"]`)?.scrollIntoView()});var O=S(),k=u(O);t(i(u(k),2),17,()=>b,e=>e.slug,(e,t)=>{var i=x();let a;var s=u(i,!0);p(i),c(()=>{a=_(i,1,`help-link svelte-1vby5nc`,null,a,{active:o(t).slug===o(T).slug}),h(i,`aria-current`,o(t).slug===o(T).slug?`page`:void 0),h(i,`href`,`#${o(t).slug}`),r(s,o(t).title)}),n(e,i)}),p(k);var A=i(k,2);l(A,()=>o(T).html,!0),p(A),f(A,e=>g(D,e),()=>o(D)),p(O),n(m,O),s()}export{C as component};