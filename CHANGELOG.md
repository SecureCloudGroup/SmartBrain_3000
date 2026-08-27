# Changelog

All notable changes to SmartBrain_3000 are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Keep an `## [Unreleased]` section at the top; on each release, rename it to the version +
date and start a fresh `## [Unreleased]`. Call out **breaking changes** and any migration
step explicitly — SmartBrain runs forward-only, data-safe migrations, but users still need
to know when a release changes behavior.

## [Unreleased]

### Security
- **Knowledge and prompt injection: tighter containment.** A review of everything the
  assistant reads found no hole in the approval gates, and closed the gaps around them:
  feed items, fetched web pages, search results, and emails are now marked *data, not
  instructions* as they enter the model's context (imported vault content already was),
  and the assistant is told that outside text never carries instructions; approval cards
  show every argument in full (an email body or schedule prompt can no longer hide its
  tail); unattended scheduled runs can no longer remember facts on a standing grant;
  document titles can no longer break out of the summarizer's instructions; out-of-budget
  rescue no longer replays tool text as a system instruction; and replies cannot load
  remote images (an exfiltration channel the content policy already blocked). A
  containment test suite feeds a model that *obeys* injected instructions and asserts
  that nothing reaches beyond a parked approval.

## [0.9.33] - 2026-08-26

### Fixed
- **Spoken replies no longer fade.** With a wake word set, the microphone opened while the
  reply was still being read, and an active microphone makes the operating system turn
  its own audio down (macOS voice processing, iOS even more) — replies started loud and
  got softer. The mic now opens only after the voice finishes; Stop is the way to cut a
  reply short.

### Changed
- **Help shows each page's sections.** The Help sidebar now lists the open page's sections
  (Voice, Knowledge, Settings…) as links, so what you're looking for is one click away
  instead of a scroll through a long page.

## [0.9.32] - 2026-08-26

### Fixed
- **No more talking to itself.** With spoken replies on, the microphone could hear the
  reply being read aloud (the browser's echo cancellation covers only its own audio, not
  the system voice — on a phone the speaker sits next to the mic) and hands-free or
  conversation mode sent the fragments back: a loop. Two fixes: a recording that is
  mostly the reply's own words is dropped and the hint says so, on every path; and the
  mic no longer opens while a reply is being read unless a wake word is set — the
  phrase is what tells your voice from its own. To interrupt without a wake word, press
  Stop. The follow-up mic also waits a moment after the voice ends.

## [0.9.31] - 2026-08-26

### Added
- **Short · Medium · Long for spoken replies.** A segmented pill in the voice row sets how
  long replies should be when they are read aloud (Speak replies or Conversation on);
  typed chat is unaffected. Default Short — a long spoken answer is the thing that made
  voice tiring, and Long is one tap away when you need it.
- **Stop while it's speaking.** Send turns into Stop for as long as a reply is being read
  aloud, not just while the text streams. In conversation mode you can also just start
  talking over it — your voice interrupts the reply, with or without a wake word.

## [0.9.30] - 2026-08-26

### Changed
- **Calmer voice controls.** The three voice modes — Speak replies, Hands-free,
  Conversation — are now labeled pills in a row above the message box instead of
  unlabeled circles crowding the input; the mic stays beside the field. Same on the
  phone. The Conversation pill shows your wake word when one is set.

## [0.9.29] - 2026-08-26

### Added
- **Conversation mode — 100% voice.** The 🗣 button beside the mic: you talk, it sends
  itself, the reply is spoken, and the mic reopens for your follow-up. Say “stop
  listening” or “goodbye” to end. The first mic open still needs one tap (a browser
  rule); after that, no buttons.
- **Your own wake word.** Settings → Status → Voice: set a phrase (“Hey Merl”,
  “Hey Catherine”, “Hey SmartBrain”) and **test it** — three tries show exactly what the
  engine heard, and if it spells your name its own way you can accept those spellings so
  the phrase works as you say it. With a wake word set, conversation mode waits for the
  phrase instead of listening all the time; “Hey Merl, what's on my calendar” carries the
  question through in one breath.

## [0.9.28] - 2026-08-26

### Fixed
- **The launcher no longer strands a downloaded app update.** After a launcher
  self-update, a restart adopted the still-running old app while the newer app version
  sat assembled on disk, and the update check then saw "already newest" — no Install
  item, no way forward (Linux, v0.9.27). The launcher now compares what the app reports
  it is *running* and offers any newer assembled version. A failed app download is now
  announced once instead of silently retrying, and pruning old versions never removes
  the directory the live app runs from.

## [0.9.27] - 2026-08-25

### Added
- **Live transcription.** Your words appear under the message box while you are still
  talking. The recording so far is re-read every 1.5 s (a fast greedy pass); the full
  pass at the pause writes the finished transcript into the box, exactly as before.
  Works the same on the Desktop and on a paired phone.

### Fixed
- **Phone dictation actually works now.** The Desktop side of the encrypted link accepts
  messages up to 64 KB, and browsers refuse to send anything larger — the phone's
  recording never left the phone (a clock forever), and on Safari the failed send took
  the whole link down with it ("no models available" until a restart). Recordings now
  travel in parts that fit.
- **Replies stream and speak on the Desktop.** The app read the wrong field from the
  server's streaming events, so answers only appeared whole at the end and "read
  replies aloud" never received a word (Listen worked because it bypasses streaming).
- **A locked Desktop says so to the phone.** The Desktop used to ignore a phone's
  connection while locked, so the phone spun and then reported "unreachable". It now
  answers, says "your Desktop is locked — unlock it there", and the phone keeps
  retrying so it walks in on its own after the unlock. The pairing timeout names the
  locked case too.

### Changed
- **Playback speed** offers finer steps (0.8× to 2× in tenths through 1.5×).

## [0.9.26] - 2026-08-25

### Added
- **Playback speed for spoken replies.** Settings → Status → Voice gains a speed control
  (0.8× to 2×) that applies to the next sentence spoken and confirms itself out loud.

### Fixed
- **Dictation from the phone works.** Requests crossed the phone's encrypted link as one
  message, and a few seconds of recording was too big for it — the phone showed a
  transcribing clock forever. Large requests now travel in ordered parts, both ways.
- **A locked Desktop tells the phone.** Pairing and connecting to a locked Desktop
  failed with a bare "failed"; the phone now says "your Desktop is locked — unlock it
  there," and the pairing screen says what to check.
- **Updates no longer hoard disk.** The launcher kept every version it ever assembled
  (~600 MB each — a field machine held 41 of them, 24 GB). It now keeps the current
  version plus the previous one as a rollback backup and removes the rest.
- **The Linux launcher restarts after updating.** On GNOME desktops the launcher
  mistook itself for a systemd service (the session leaks a systemd marker into every
  app), swapped its binary, and waited for a restart that never came. It now checks the
  real service identity and relaunches itself like the Mac and Windows launchers.

## [0.9.25] - 2026-08-25

### Added
- **Dictation that ends when you stop talking — and listens to you about everything
  else.** Pause for a moment and the recording finishes itself (no second tap). Say
  "send" to submit in the same breath, "cancel" to discard, "start over" to re-listen.
  Hold **Space** to talk on a keyboard; **Esc** cancels. A **hands-free** toggle sends
  every dictation automatically. And Settings → Status now shows **how much disk and
  memory SmartBrain uses** (database, voice model, everything — plus this browser's
  cache), and the mic test's playback works in every browser.

## [0.9.24] - 2026-08-25

### Changed
- **Dictation now runs on Whisper — the engine that survives real voices.** The Mic &
  speaker check caught the previous engine returning nothing for perfectly clear
  speech (byte-verified: the same recording transcribes flawlessly under Whisper).
  The built-in engine is now Whisper base via faster-whisper, the industry-standard
  local runtime: sub-half-second on ordinary CPUs, a smaller one-time download
  (~141 MB, integrity-checked, replacing the old files automatically), and it
  understands dozens of languages instead of English only. Everything else —
  background download with live progress, the always-honest mic states, your own
  audio server taking over when configured — is unchanged.

## [0.9.23] - 2026-08-25

### Added
- **Mic & speaker check, built in.** Settings → Status → Voice gains a one-tap test:
  it records three seconds with a live level bar, plays the recording back so you hear
  exactly what the recorder heard (that's the speaker check — and the fastest possible
  diagnosis of a bad microphone), and shows what dictation understood. Each leg failing
  points at exactly one culprit, on screen, in plain words. The test recording is kept
  locally so a problem can be diagnosed from the audio itself.

## [0.9.22] - 2026-08-25

### Fixed
- **Dictation records for real now — and says so.** Two stacked capture bugs made the
  mic record silence while looking alive: Safari starts audio processing suspended
  (never resumed), and the capture node wasn't on a path the audio graph pulls — so on
  every engine the red button could capture nothing. Both fixed, and verified by
  driving the real UI end-to-end in a browser. The mic now also pulses with your
  actual voice level while recording, and a silent or word-free recording gets a
  plain-words message naming the fix — never a silent nothing again.

## [0.9.21] - 2026-08-24

### Fixed
- **The microphone was forbidden by our own security header.** The app's
  Permissions-Policy — written before voice existed — denied microphone access
  document-wide, so browsers that enforce it (Chrome-family, and the PWA) refused to
  record no matter what permissions you granted. Dictation now allows the microphone
  for the app itself; geolocation and camera stay denied. Found by driving the real
  UI in an automated browser after a field report of dead mic clicks.

## [0.9.20] - 2026-08-24

### Added
- **Settings → Status: a live view of everything the app is doing.** App version, lock
  state, the voice model's download (with a real progress bar and a retry button),
  which dictation engine is active, model-server configuration, knowledge counts,
  schedules, feeds, and paired devices — one calm page, updating while you watch.

### Fixed
- **Voice setup is now fully visible and never hangs.** The first field test hit every
  version of invisible: a mic press could stall for minutes downloading the model
  inline, "100%" showed while the engine was still loading, and a stuck request left
  the mic dead to further clicks. Now the download starts at app launch, the mic
  button itself shows the live percent (then "loading engine", then ready — it enables
  itself), a press can never block on a download, failed downloads offer one-tap
  retry, transcription requests time out instead of wedging the mic, and a configured
  audio server that fails is skipped for a while instead of slowing every press.
  Recording also survives a missing or blocked audio-worklet by falling back to a
  universal capture path — the phone failure's root cause.

## [0.9.19] - 2026-08-24

### Added
- **Voice now just works — dictation is built in, zero setup, every OS.** SmartBrain
  transcribes on your machine with Moonshine, a fast on-device speech-to-text engine:
  the model (~236 MB) is fetched once, integrity-checked, quietly in the background
  after unlock, and dictation is instant and offline from then on (English, for now).
  A configured audio server still takes over automatically for other languages or
  maximum accuracy — and if it can't serve, the built-in engine carries on instead of
  failing. No model names, no servers, no settings.

## [0.9.18] - 2026-08-24

### Fixed
- **Dictation finds the server's whisper model by itself — and tells the truth when
  there isn't one.** The first real dictation failed three layers deep: the voice
  server had no whisper model loaded, the app insisted on one exact model name, and
  the on-screen error blamed the connection. Now SmartBrain uses whatever whisper
  model the server offers (and remembers it), the error names the actual fix when the
  server has none, and the Settings Voice card warns about a reachable-but-modelless
  server before you're mid-sentence. The guide no longer claims Macs need zero setup —
  loading a whisper model into oMLX once is the honest requirement.

## [0.9.17] - 2026-08-23

### Fixed
- **Dictation actually starts now.** Pressing the mic failed immediately: the audio
  recorder loaded its processing module from an inline blob URL, which the app's own
  Content-Security-Policy rightly refuses — and the error message blamed mic
  permissions, which was wrong. The module now ships as a normal app file (the strict
  CSP stays exactly as strict), and mic errors name what actually happened: permission
  denied, no microphone, or the real failure.

## [0.9.16] - 2026-08-23

### Fixed
- **The voice buttons are actually visible now.** The mic and speaker buttons in the
  message box rendered as 2-pixel slivers — the app's base button padding inside their
  fixed width squeezed the icons to nothing. They now size like the Send button.

## [0.9.15] - 2026-08-23

### Added
- **Voice: dictate and listen, on every platform.** Chat gains a microphone (push to
  talk — your words land in the message box for review) and spoken replies (read aloud
  sentence-by-sentence as they stream, with a per-answer Listen action and natural
  barge-in). Speech-to-text runs on a local audio server you configure under Settings →
  Local models → Voice — on a Mac with oMLX there is nothing to set up — and the phone
  sends its audio over the same encrypted connection as everything else. Your voice
  never leaves your machines. Replies use your device's own voices, with an optional
  server voice as upgrade or Linux fallback.

## [0.9.14] - 2026-08-23

### Added
- **Retry a message with one click.** Every message you've sent shows a small colored
  Retry pill beside "You" — click it and the same request is sent again, exactly as if
  you'd retyped it. For the times a nudge ("Go") deserves a second run without the typing.

## [0.9.13] - 2026-08-23

### Added
- **Check for updates from the menu.** The launcher's tray menu gains "Check for
  updates" — the same quiet check the 6-hour timer runs, on demand, and it answers
  either way: an update surfaces the usual install choices, and "no newer version"
  says so instead of leaving you waiting on the timer.

## [0.9.12] - 2026-08-23

### Changed
- **Approve without leaving chat.** When the assistant needs your go-ahead (a web search,
  a fetch, an email), the blocked actions now appear right in the conversation as cards
  with Approve / Always allow / Deny — and Approve all when several reviewed actions are
  waiting. Resolving the last one resumes the turn by itself; no more trip to Activity
  and back. Activity still lists everything.
- **Chat opens and jumps to the real bottom.** Opening a conversation lands on the newest
  message, and the new Top / Latest pills jump either way. "Jump to latest" used to stop
  with the newest message hidden behind the message box — fixed.

### Fixed
- **Times are your local times, everywhere.** The assistant is now told to state dates
  and times in your local zone (it sometimes echoed UTC when summarizing search results),
  and three displays that read UTC timestamps as if they were local — vault "last
  checked" ages, the hosted-check tooltip, and the feed row's checked time — now convert
  properly.

## [0.9.11] - 2026-08-22

### Added
- **Tag a feed once, every article carries it.** Subscribing to a feed now takes optional
  tags, stamped on every document the feed ever ingests — one click on the tag chip
  filters your library to the subject you follow.
- **Bulk tag and bulk delete.** The selection bar on the Knowledge page can now tag or
  delete everything you've ticked in one action. Publisher-owned copies refuse
  individually, and the result reports the honest split.

## [0.9.10] - 2026-08-22

### Added
- **Follow websites — RSS/Atom feed subscriptions.** Paste a feed URL on the Knowledge
  page and SmartBrain keeps up with it for you: each subscription gets its own vault,
  and every new post lands there as a searchable, citable, encrypted document — ask
  about them in Chat, or have a schedule summarize the week's posts. Checks run about
  every six hours directly from your machine (no server in the middle), already-saved
  posts are never duplicated, and unsubscribing keeps your saved articles unless you
  say otherwise.

## [0.9.9] - 2026-08-22

### Changed
- **One vault, one lock — now said everywhere, and fully true.** The Desktop and a
  paired phone always shared the same lock (the phone is a window onto the Desktop),
  but the words never quite said so and a Desktop sitting on its unlock screen
  wouldn't notice a phone-side unlock until refreshed. The unlock screen now watches
  and continues on its own when the vault is unlocked from any other device, and the
  unlock screen, getting-started guide, and remote-access guide all state the shared
  lock plainly: unlock (or lock) on either device, and both follow.

## [0.9.8] - 2026-08-22

### Fixed
- **The phone picks up updates on its own now.** After an update, a resumed phone
  app could keep running the old version until you force-closed it — its
  once-a-minute version check freezes in the background and iOS throttles it on
  return. The check now runs the moment the app comes back to the foreground
  (riding out the reconnect), and when the vault is locked — the guaranteed
  post-update state, with nothing in flight to lose — the app reloads itself to
  the new version on the spot. Unlocked sessions still get the reload banner
  rather than a surprise: no one loses a half-typed message to an update.

## [0.9.7] - 2026-08-22

### Fixed
- **Deep-audit hardening (pre-launch).** Three independent adversarial reviews of
  the codebase produced four fixes, all shipped here: the launcher's new stale-
  gateway cleanup now verifies the port-holder's identity before acting, so it can
  never touch Docker's port-forwarder or any other legitimate process (unknown
  identity refuses with an actionable message); the Windows process lookup no
  longer depends on English netstat output (it silently failed on German, French,
  and every other localized Windows); the phone's resume check can no longer
  misfire against a connection that is still mid-handshake or against a later
  connection cycle; and the two streaming chat endpoints now route vault-locked
  responses through the same central handler as everything else, closing the last
  gap of the redirect-stampede class.

### Fixed
- **An update can no longer leave the app looking broken.** Two real failure
  modes from the field, both closed: a browser tab open across an update-restart
  got stuck in a redirect loop so fast the unlock screen never loaded (the tab
  now flips itself to "locked" on the first refusal and lands on the unlock page
  once); and a gateway process orphaned by a years-old bookkeeping bug could
  survive updates forever, silently serving an old version under a new app — the
  launcher now clears anything squatting on the gateway port before starting the
  right one, so the gateway always matches the release.

## [0.9.6] - 2026-08-22

### Fixed
- **The desktop now shows "thinking" while a streamed answer is on its way.** The
  streamed chat path hid the thinking indicator the moment the request started,
  leaving the page silent until the first token — many seconds on a large model.
  The indicator now stays up until real content arrives (and the previous answer
  no longer briefly wears a "streaming" caret it didn't earn).
- **Coming back to the phone app reconnects in seconds, not "eventually".** iOS
  freezes the connection when the app is backgrounded but keeps claiming it's
  connected, so returning could leave the phone staring at a dead link until the
  keepalive noticed (up to ~45 s). On return the phone now demands a fresh
  heartbeat within a couple of seconds and reconnects immediately if it doesn't
  arrive; a genuinely healthy connection is left untouched.

## [0.9.5] - 2026-08-19

### Changed
- **Using a model server on another machine is now a first-class, explained path.**
  Field testing (SmartBrain on a Linux laptop, models on a Mac) found the remote
  option buried: the Ollama/MLX panels now say plainly when to use the port (same
  machine) and when to use the renamed **"Server on another machine"** field, the
  can't-reach hint explains network access and API keys, and the models guide gains
  a step-by-step "Use a model server on another machine" section (server-side
  toggles, the one-line verification curl, then connect). The **MLX embeddings**
  card — whose install instruction pointed at a repo path desktop installs don't
  have — now leads with "most setups don't need this", says honestly that the
  fallback installer requires a source checkout, and gains the same remote-server
  and API-key fields as the other backends. The Linux install script and guide now
  also give the real fix for `smartbrain: command not found` right after install
  (log out and back in).

## [0.9.4] - 2026-08-17

### Changed
- **Housekeeping and hardening.** Web UI dependency refresh (including an icon-set
  update), defense-in-depth hardening of the Gmail OAuth helper's redirect (header
  values are rebuilt from verified parts, never forwarded from the request), and
  the README + issue templates now state support expectations plainly: the project
  is maintained part-time — everything is read and addressed as quickly as
  possible, with no promised turnaround.

## [0.9.3] - 2026-08-17

### Fixed
- **Approvals from scheduled runs no longer expire before you can act.** A parked
  action expired one hour after it was created — fine for a live chat, but a
  schedule that fires while you're away parks its approvals precisely so you can
  resolve them later, and every click after the hour was refused with "approval
  expired". Scheduled approvals now wait **30 days**; chat approvals keep the
  one-hour clock (a stale conversation shouldn't act late), and irreversible
  actions still re-confirm every time. Expired items also now drop off the
  Activity list instead of showing Approve buttons that can never work.

## [0.9.2] - 2026-08-07

### Fixed
- **Schedule times shown hours off on desktop installs.** The database session
  followed the computer's timezone, so native installs stored local times while
  the app rendered every timestamp as if it were UTC — a schedule due at
  11:48 AM displayed as 7:48 AM. All timestamps are now stored in UTC on every
  install (a one-time migration shifts existing schedule times on this machine),
  and every time in the app is displayed in your local timezone. Docker installs
  always stored UTC and are unaffected.

## [0.9.1] - 2026-08-06

### Fixed
- **Connecting Gmail works again.** The Phase-0 origin guard (0.8.18) refused
  Google's redirect back from the consent screen — by nature a cross-site
  navigation — so finishing OAuth died on "Cross-origin request refused" and
  the connect (or reconnect) never completed. The guard now steps aside for
  exactly that one navigation (the callback GET, whose one-shot `state` check
  is the real defense); scripted cross-site requests to the same path are
  refused as before.

## [0.9.0] - 2026-08-06

### Added
- **Linux joins the native stack — one launcher, tray or headless.** This release
  train is the Linux one. The same launcher that lives in the macOS menu bar and the
  Windows tray now runs on Linux desktops and servers: where the desktop can host a
  tray icon it gets the familiar menu, and where it can't — a server, an SSH
  session, or a stock GNOME without the AppIndicator extension — it says so once and
  keeps running headless instead of pretending a tray exists. It also grew verbs for
  people without a mouse on the machine: `smartbrain run` (foreground, what a
  systemd unit runs), `start` (prints the app's URL), `stop`, `status`, and
  `version`. Self-updates understand systemd: under a unit the launcher swaps
  itself and lets `Restart=` bring up the new binary, instead of detaching a child
  the unit would kill. And it refuses to self-update out of a shared `bin`
  directory — the swap replaces the launcher's whole folder, which must never be a
  folder other tools live in.
- **The Linux launcher ships with every release.** Releases now attach
  `SmartBrain-linux-x86_64.tar.gz` — a fully static binary plus desktop entry and
  icon — with the same sha256 sidecar and minisign signature as the macOS and
  Windows builds. CI now also proves the native assembly end to end on a clean
  Linux runner: the launcher downloads and verifies a real release (Python
  runtime, wheelhouse, gateway), serves on loopback only, and stops clean.
- **A Linux install worth auditing.** `installer/install-linux.sh` is the documented
  two-command story: download the script, read it, run it — never `curl | sh`. It
  verifies the release's minisign signature and checksum, installs per-user with no
  root, and adds a menu entry — or, with `--headless`, a systemd `--user` service
  for servers. The same script uninstalls: `--uninstall` keeps your data and names
  exactly what remains, `--purge` removes that too. The getting-started guide,
  README, and landing page now tell the native Linux story, containers staying the
  documented first-class alternative.

## [0.8.24] - 2026-08-05

### Added
- **Open logs in the menu — the folder every "see the log" message points at is now
  one click away.** The troubleshooting docs send people to `native/run/app.log`, but
  macOS hides `~/Library`, and a real user reported they simply could not get to that
  folder. The menu-bar / tray menu now has **Open logs**, which opens it in Finder /
  Explorer (on a Docker install, which writes no native log files, it opens the
  SmartBrain data folder instead). The getting-started guide now also names the full
  per-OS paths and the Finder **Go to Folder** route.

## [0.8.23] - 2026-08-05

### Changed
- **A paired phone now tells you the difference between a locked Desktop and an
  unreachable one.** The **Remote** chip used to say only *offline* — with a hint
  that muddled the two ("it may be off or locked"). It now shows **Desktop locked**
  (tap-through to unlock from the phone, since the encrypted bridge is fine and the
  Desktop is still answering) when the vault is locked, and **unreachable** ("may be
  off or asleep") only when the phone genuinely can't reach the Desktop at all. The
  distinction is honest: a locked Desktop still answers over the bridge, so it is
  never the reason for a connection failure.

### Added
- **The vault publisher lifecycle: retire, dated publishes, dead-host handling
  — with a truthful public version chip.** Sharing a vault used to have one
  path (publish) and one way to stop (delete the file and hope everyone
  noticed). It now has the whole arc a real publisher needs:
  - **Retire a vault, keep everyone's copies.** A new **Retire** action on the
    publisher side produces one final, dated open export marked `retired`;
    subscribers apply the last content update and then move into a **Retired
    by publisher** state that drops out of auto-checks. Nothing gets deleted
    from anyone's Knowledge — retirement stops the update channel, it doesn't
    reach back and take your documents. A later normal publish from the same
    key un-retires the subscription (the publisher came back).
  - **The public version chip now says the truth.** Sealed private-share
    exports used to inflate the same version counter subscribers saw as the
    "public" version, so private activity moved the public number. There are
    now two: `internal_seq` (bumps on every export) and `published_seq` (only
    an open publish moves it). The Knowledge card can honestly show *Public
    v3, internal v5* and *Changed since last publish*.
  - **Publish dates.** Every export stamps a UTC calendar date the manifest
    carries; the subscriber sees when what they're looking at was actually
    made.
  - **A publisher taking the vault down is a distinct signal.** A hosted vault
    that returns **HTTP 410 Gone** flips the subscription to *Unreachable —
    the publisher took this vault down* immediately; a host that's just been
    down for a real week (8 failed attempts across 7 days) escalates to the
    same state via the slower path. Auto-update stops in both cases; a manual
    check still tries.
  - **Delete a subscribed vault — your choice.** `DELETE /api/vaults/{id}`
    still keeps documents by default (removing a grouping isn't shredding your
    files), but `?remove_docs=1` also deletes the vault's import-origin docs
    outright. Owner-origin copies always survive either way.
  - **Re-subscribing after a delete un-freezes updates.** The old "delete but
    keep docs" then "re-subscribe" combination silently froze every future
    update to the re-adopted docs. A new one-way import trace lets a
    re-subscribe re-adopt ex-import orphans as vault-owned so updates apply
    again — while never converting a user-authored duplicate into a
    stranger's to update.
  - **A publisher's description is theirs to write.** Subscribing used to
    overwrite the publisher's own description with "Public vault · publisher
    &lt;fingerprint&gt;". Now the publisher's name and description are shown as
    written and propagate on every update — a publisher rename is called out
    in the update result.
  - **Sealed re-share warns about the key.** Every sealed export mints a
    fresh Vault Key that orphans everyone who had the previous file; the
    export response now flags the re-key so the UI can warn before you
    distribute the new file.
  - **Unchanged republish flag.** Publishing twice with no content changes
    is flagged on the export response so the UI can ask "did you mean to?"
  - **Remember where you uploaded the vault, and verify it.** A vault can
    now carry an optional `hosted_url` (settable via `PATCH /api/vaults/{id}`,
    validated with the same public-internet rules the subscribe path already
    applies — no localhost, no LAN, http(s) only); and a new
    `POST /api/vaults/{id}/verify-hosted` action fetches that URL and reports
    whether the hosted file matches this install's last publish
    (`{reachable, seq, matches, behind, retired, detail}`) — including
    the "you forgot to upload the new file" case, the "hosted file is newer
    than this install's record — was it published from another machine?"
    anomaly, and the "the signature at that URL isn't yours" case. Read-only:
    no pin, no subscription state is touched. The Share panel on a published
    vault now shows a **Hosted at** row (URL + Save) and, once set, a
    **Verify hosted copy** button whose verdict styles the server's plain-words
    detail — green when it matches, warn on any anomaly, muted when the host is
    unreachable.

  All of it ships with its interface in this release: the Retire… flow, the
  *Retired by publisher* and *Unreachable* card treatments, the two-choice
  delete confirmation, the sealed re-key warning, and the "you republished
  with no changes" nudge — on desktop and phone alike.

### Changed
- **The first download shows its progress.** Assembling SmartBrain fetches
  ~400 MB, and the tray used to sit on a bare "Downloading SmartBrain…" for
  minutes with no sign of life. The status line now names each piece and how
  far along it is — *python 43%*, *app 12%*, *gateway 87%* — during the first
  install and app updates alike.
- Ruff upgraded to 0.16 deliberately, and the pin is now read from a single
  source. The old `<0.16` pin was correct — 0.16 promoted new default rules —
  but CI hardcoded its own copy of the version, so a Dependabot bump to
  `app/pyproject.toml` would show green while changing nothing CI ran (#191).
  The 0.16 rule set is now triaged with an explicit `[tool.ruff.lint]` config
  that records every deviation from defaults with a one-line rationale, and
  both CI ruff invocations install ruff from the app's `[dev]` extra so the
  pin in `app/pyproject.toml` is the only place it lives.

## [0.8.22] - 2026-08-05

### Changed
- **A paired phone can now add to Knowledge — and see Usage.** The phone was
  read-only for Knowledge; it now offers the full **Add to Knowledge** card (write a
  note, upload a file, add a web page or PDF by URL) and the **Add someone else's
  vault** panel (import a `.sbvault`, or subscribe to a public URL) — the desktop is
  still where it lands, and the bridge carries up to 25 MB per upload. **Usage &
  cost** also joins the phone's More sheet so you can check spend on the go. The
  security fences that stay Desktop-only are unchanged and for the same reasons:
  exporting or sharing a vault, trusting a rotated publisher key, connecting Gmail,
  downloading a backup or export, and managing paired devices — all things that hand
  out your data or change trust.
- **Self-review cadence is now yours to pick.** How often SmartBrain reviews itself
  used to be hard-wired to every 8 hours; **Settings → Self-improvement** now offers
  **every 2, 4, 8 (default), or 24 hours** — plus the existing **Off**, which stays the
  fail-closed kill switch. Heavy chat days benefit from a 2-hour loop (faster feedback);
  24 keeps a light machine quiet. Note that the "on trial" and auto-revert windows are
  denominated in *intervals*, so they scale with the cadence too: a 2-hour cadence
  reviews AND reverts about four times faster than the default, and a 24-hour cadence
  three times slower. The suggestion-detector's own "one burst of retries is not a
  routine" window (48 real hours) stays wall-clock — it isn't affected by the setting.

## [0.8.21] - 2026-08-04

### Fixed
- **Approving — or denying — a scheduled run's parked action now finishes that
  run.** A scheduled "News check" that parked a `web_fetch` for approval used to
  end at "Awaiting your approval" and stay there: approving the action ran the
  fetch but nothing resumed the parked scheduled turn, so the run visibly did
  nothing until the user triggered the schedule again. Resolving the last
  pending of a scheduled run now server-side resumes the turn and its answer
  lands in the Scheduled updates feed alongside every other scheduled run;
  denying does the same so the "couldn't do X" answer is recorded instead of
  dangling forever. Chat parks are unaffected — the chat page still owns its
  Resume flow. The resume honors the same safety guards as the tick: even a
  remembered schedule-writing tool still parks for human approval (an injected
  prompt must not self-modify schedules mid-resume).

## [0.8.20] - 2026-08-04

### Changed
- **"Always allow" is now available for the URL tools — per site.** Approving a
  `web_fetch` or `kb_ingest_url` action offered no way to stop being asked again, because
  remembering the whole tool would have let a prompt-injected URL fetch anywhere unattended.
  The button now remembers ONE host: the scheduled news check runs unattended after one
  approval, and a fetch pointed at an unknown host — the shape an exfiltration takes —
  still parks for approval. Existing whole-tool remembered consents (e.g. `web_search`,
  `add_task`) are unchanged. Each allowed site is its own row under **Always allowed** on
  the Activity page, with its own **Stop allowing**.
- **A denial sticks for the rest of the turn.** Denying a tool call would let the model
  immediately re-request the exact same action, spawning a fresh pending row — a loop of
  deny, request, deny. The tool result now says plainly that the user denied it and not
  to try again this turn; if the model re-emits the identical call anyway, the server
  refuses without creating another pending, so the turn converges. A DIFFERENT call still
  parks normally.

## [0.8.19] - 2026-08-04

### Changed
- **Semantic search on the default local embedder now sends the task prefixes it was
  trained on.** `nomic-embed-text` was trained to expect `search_document: ` on passages
  and `search_query: ` on queries; sending raw text is a known retrieval-quality loss
  (measured against the real knowledge base on 2026-07-24). The gateway now prepends the
  right prefix on every embed call for nomic models — and only for nomic; other embed
  models (`bge-m3`, `mxbai-embed-large`, OpenAI's, ...) see byte-identical wire behavior,
  because sending nomic-style prefixes to a model that wasn't trained on them corrupts
  results. Existing vectors were embedded WITHOUT the prefix, so fusing them with a
  prefixed query would silently degrade ranking: they are treated as stale and the
  background indexer re-embeds them under the new scheme with no user action needed. A
  large knowledge base takes a few passes to drain — during that window the default
  hybrid search keeps returning results (its keyword half is unaffected), and semantic-
  only mode returns no hits for the not-yet-re-embedded documents until the backfill
  reaches them.
- The web app now builds the same bytes from the same sources: its build id is a content
  hash instead of a build-time timestamp, and CI fails when the committed bundle no longer
  matches the source next to it — the guard for the stale-interface class of bug fixed in
  0.8.18. The first update after this change reloads open tabs once as the cache id rolls
  over.

### Removed
- **Dead surface off the pages that carry it.** Settings → Model routing no longer lists
  **Fast chat** and **Reasoning** slots — nothing in the app read them, so setting either
  did nothing but suggest otherwise. An old install that had saved a value into one of them
  loads fine; the retired keys are silently dropped rather than resurfacing. The pairing
  page has also lost its leftover QR-payload decode path: the Desktop stopped emitting
  payload QRs when pairing moved to the 6-character code, so that branch could never fire.

## [0.8.18] - 2026-08-03

### Fixed
- **"Always allow" works now.** Approving an action with "Always allow" recorded nothing,
  so the same action kept coming back — and the interface built for managing it had never
  actually shipped inside the app bundle. Remembered approvals now stick, and they are
  deliberately narrow: only reviewable tools with fixed destinations qualify, so a tool
  whose target the model composes on the fly (fetching an arbitrary URL, ingesting from
  an arbitrary address) asks every time, always. The Activity page lists everything you
  have allowed, each with a **Stop allowing** button.

## [0.8.17] - 2026-08-03

### Security
- **Launch-readiness pass (Phase 0)** across the security core, the hosted broker,
  release signing, and onboarding. The user-visible piece: release artifacts are now
  signed (Ed25519 / minisign) and the desktop app verifies the signature before
  installing any self-update, with the public key compiled into the app. Verification
  instructions for stock `minisign` are in the security policy.

## [0.8.16] - 2026-08-03

- Identical to 0.8.15 — the tag points at the same commit; no changes.

## [0.8.15] - 2026-08-02

### Fixed
- The doctor scopes its process cross-check to the install being examined, instead of
  flagging processes that belong to a different SmartBrain install on the same machine.

## [0.8.14] - 2026-08-02

### Fixed
- **The doctor was diagnosing a product that no longer exists.** `installer/install.py
  doctor` opened by testing for Docker — "Docker not found", "Docker daemon not reachable",
  "Docker Compose v2 not found" — and on a normal, perfectly healthy install all three
  failed, so someone whose only real problem was a locked vault was told to go and install
  Docker. Its advice was `compose up -d`, which does nothing to the app they are running.
  It has been rebuilt around the install people actually have.

  The new `python3 installer/doctor.py` needs no Docker, no repository, and — importantly —
  no running app, since that is when people reach for it. It reports on the assembled
  install and the version selected to run; both processes and whether their records still
  name them; both ports and *who* is answering; whether the vault is locked (which looks a
  great deal like being broken); the database file; the model gateway's providers and the
  local model servers behind them, including one that is reloading its model on every
  request; disk space; a downloaded update waiting for a restart; leftovers from an
  interrupted download; and the handful of failures this project has hit before, read out
  of the log as one sentence each.

  It is read-only until you ask. `--fix` then offers each repair one at a time, printing in
  full what it is about to do, and every one of them stays away from your data. Docker is
  mentioned in exactly two places: on the Intel Macs and ARM Linux machines that genuinely
  run it, and when a leftover container really is holding the port SmartBrain needs — in
  which case removing it is offered, with the reassurance that Docker volumes are untouched.
- The Restore card on Settings → Account & Data told users to run a restart command that
  does nothing on the install they actually have; its instructions now match how the app
  runs.

### Changed
- Documentation completeness pass: the guides now document every feature, the landing page
  shows what SmartBrain actually does with each feature linked to its documentation, and
  the Linux story is told truthfully (Linux runs via Docker and has no desktop app).

## [0.8.13] - 2026-08-01

### Added
- **Docker-free by default, with automatic data migration.** On Apple Silicon Macs and
  64-bit Windows the desktop app now assembles and runs SmartBrain natively — no Docker
  required. An existing Docker install's data is copied across on the first native start
  (copied, never moved: the Docker volumes stay untouched as a fallback), and
  `SMARTBRAIN_NATIVE=0` forces the Docker path for one run.

### Changed
- The documentation now describes the Docker-free product instead of the Docker-only one.

### Fixed
- **`brew uninstall --zap` could delete your knowledge base.** The cask removed the whole
  `~/Library/Application Support/SmartBrain` folder, on the stated grounds that your data
  lived in Docker volumes — true when that was written, and false the moment the
  Docker-free stack landed, because the database now lives inside that very folder. A
  `--zap` uninstall now removes only the app's own files (assembled runtimes, logs,
  bookkeeping, and the gateway config that holds provisioned provider keys) and never
  touches your data.

## [0.8.12] - 2026-08-01

### Fixed
- **A clean install could refuse to start in Docker-free mode.** If SmartBrain had been
  started once under Docker — which happens the moment you install it — the empty data
  volumes it creates were mistaken for data waiting to be moved, and the switch to
  Docker-free mode failed with "database missing after copy" instead of simply starting.
  An empty volume now means what it should: there is nothing to carry over, so it starts
  fresh. A copy that genuinely fails is still reported, because that one might mean your
  data is there and unreachable.

## [0.8.11] - 2026-07-31

### Fixed
- The menu-bar version line no longer lags behind an update it just installed: for up to
  half a minute it could read "Running (updated)" above the *old* version number, which is
  precisely the confusion the line exists to prevent. It now refreshes the moment a start
  or an install finishes. The launcher's test suite now runs in CI.

## [0.8.10] - 2026-07-31

### Added
- **Updates now appear in SmartBrain itself, with one button.** A waiting update used to
  exist only as a menu item behind the menu-bar icon — easy to miss, and nothing in the
  app you were actually looking at ever mentioned it. When the desktop app has downloaded
  a new version, the page now says so and offers **Install now**; it restarts, and the page
  reconnects and reloads on its own. "Not now" hides that version and stays quiet until a
  newer one arrives. The menu-bar items still work exactly as before.
  - Installing is **Desktop-only**: a paired phone can see that an update is waiting but
    cannot restart the machine across the network.
  - The menu-bar app now shows **which version is running**, so the question has an answer
    without opening SmartBrain. During an update, when the desktop app has been replaced
    but the app it supervises has not, it names both numbers instead of one misleading one.
  - The app still makes **no outbound calls of its own** — it learns about the update from
    the desktop app on the heartbeat they already exchange, rather than asking the internet.

### Fixed
- The one-time "your desktop app needs an update" banner stopped appearing in the previous
  release. A lifecycle call added alongside it threw where the error could not be seen —
  into an empty catch — which skipped the lines after it, including the banner. The banner
  is back, the timer it introduced no longer outlives the page, and that catch now reports
  what it caught instead of hiding it.

## [0.8.9] - 2026-07-31

### Added
- A **stress suite** that shakes the pieces together instead of holding them still:
  sustained mixed traffic (chats, agent turns, searches and history reads at once),
  a streamed answer abandoned mid-flight against a real server socket, repeated
  tool turns, and locking the vault while requests are in flight. Each one asserts
  the single local-model slot comes back free — the failure that would quietly wedge
  every later request. Every test was checked by breaking the protection it guards and
  confirming it fails; two earlier versions passed against broken code and were rewritten.

### Changed
- **Action turns no longer ask the model the same question twice.** When you asked for
  something that needs a tool ("add a task…"), the app streamed a first answer, saw the
  model reach for a tool, threw that response away, and re-ran the whole turn from the
  start — sending an identical ~4,000-token prompt a second time (measured: 4.18s then
  3.88s). The first response is now reassembled from the stream and handed to the tool
  phase, so it is paid for once. If anything about that response is doubtful — truncated
  arguments, a missing tool name — it is discarded and the model is asked again exactly
  as before, because wrong arguments would be far worse than a slower answer.

### Fixed
- **An update no longer strands itself for six hours.** The launcher looks for updates
  every six hours, but a look that *couldn't happen* — because a start, stop, or install
  was in flight, or because the very first assembly was still running — used to count as
  a look, pushing the next one a full six hours out. Restarting the app didn't help,
  because a fresh launch is exactly when something is most likely in flight. A skipped
  look now retries in 90 seconds, and the first look after launch comes in 20 seconds
  instead of 45.
- **An open tab now notices when SmartBrain updates underneath it.** The app updates
  itself while you're using it, but a page loaded before the update kept showing the old
  version under the logo and the old interface — which reads as "the update didn't work"
  (it caused exactly that confusion twice in one day). The page now notices the version
  change and offers a one-click **Reload**, or you can dismiss it and keep working.
- **SmartBrain now says so when your model server is reloading its model on every
  request.** A misconfigured local server can spend seconds loading the model before
  each answer — on a real install that was 4.5 seconds of every turn, three times the
  cost of the actual work, for five days — and nothing in the app mentioned it, because
  usage tracking only ever looked at token counts. Diagnosing it meant reading the model
  server's own logs. It now appears in SmartBrain's log with what to check, at most once
  every fifteen minutes.

## [0.8.8] - 2026-07-30

### Fixed
- **A busy model server can no longer cost the assistant its tools.** When the local
  model server was briefly unavailable — it answers "busy" for the seconds it spends
  reloading a model — the assistant quietly re-asked *without any tools* and marked the
  answer degraded. For a request like "add a task", that hands the job to a model that
  cannot act, and a model that cannot act tends to describe the action instead of
  performing it: the exact failure the approval system exists to prevent. Observed six
  times on a real install. A transient failure is now retried **with** the tools, so the
  action parks for your approval as it should; the forgiving plain-answer path stays only
  for models that genuinely cannot use tools.
- **Chat no longer waits behind background indexing.** When Knowledge had documents to
  embed, the background indexer drained them in a tight loop that re-took the single
  local-model slot for every document — and because that slot has no queue fairness, a
  chat sent mid-backlog kept losing the race. Measured on a real install: consecutive
  turns at 16.1s and 28.6s while a backlog drained, against ~8s on an idle machine. The
  indexer now steps aside the moment a chat arrives and resumes on the next pass (the
  work was always idempotent). It already yielded to a chat *in flight*; what it missed
  was a chat that arrived after it started.

## [0.8.7] - 2026-07-29

### Fixed
- **Native supervision now verifies instead of trusts.** Two colliding starts (a
  relaunch that didn't stop the running stack, then the first auto-update install)
  exposed the class: a health check passes whenever *something* answers the port, so
  a second start's own dying processes went unnoticed, the recorded pids ended up
  naming dead processes, and every later Stop silently stopped nothing. Starting now
  refuses when an instance already answers (retried, so a momentary stall can't read
  as "nothing is running") **and** when a previously recorded process is still alive
  (verified to be ours, so a pid file that outlived a reboot can never block a
  start). A health answer is credited to the process we started only once it has
  outlived the startup in which a doomed one dies — and its own exit is watched
  throughout, so a death is caught whenever it happens. Stopping now confirms a
  process actually exited before dropping its record; a survivor keeps its record so
  the launcher never loses its only handle on a running process.
- **Chat could fail in Safari while the backend answered perfectly.** A local model
  takes ~8 seconds to its first token on a plain "hi", and the chat stream sent
  *nothing at all* during that wait — no opening bytes, no keepalive, and (unlike the
  sibling tool-turn stream, which has always done both) no `Cache-Control: no-cache`.
  Safari drops an idle streamed response, so the browser showed "Couldn't reach
  SmartBrain — check your connection" for a turn the server had completed and
  recorded. The stream now puts bytes on the wire the moment it opens and sends a
  keepalive every 5 seconds until the model produces text. Belt and braces: if a
  streamed answer's connection breaks before a single word arrives, the app quietly
  re-requests it whole on the non-streaming endpoint instead of showing an error —
  chat now survives any browser or proxy that dislikes long-lived streams.

## [0.8.6] - 2026-07-29

### Added
- **Native installs now update the app themselves** — the answer to "will SmartBrain
  auto-update?" is now yes on every layer. The launcher's 6-hour check (which already
  updates the launcher binary) now also watches for new app releases in native mode:
  a newer release is assembled in the background into its own versioned directory
  (verified downloads; the running version keeps serving untouched) and offered
  through the same **Install update now / Install on next start** menu the Docker
  path has — installing is a supervised restart, and the previous version stays on
  disk as the rollback. Supervision is tightened along the way: exactly one watcher
  ever runs, and a deliberate **Stop** no longer fights it (the supervisor used to
  restart a stack the user had just stopped). A stale `SMARTBRAIN_NATIVE_VERSION`
  pin can bootstrap a first install or force an upgrade, but can never downgrade an
  auto-updated one.
- **Native mode now sticks.** It was env-only, so a reboot or plain relaunch of the
  desktop app silently fell back to Docker — observed live as a compose attempt
  colliding with the surviving native stack's ports and blaming the internet. A
  successful native start now writes a persistent marker: every later launch boots
  native with no env needed, and a relaunched launcher **adopts** a healthy running
  stack instead of spawning a second one into the same ports. `SMARTBRAIN_NATIVE=0`
  forces Docker for one run; deleting the marker rolls back for good.

## [0.8.5] - 2026-07-29

### Fixed
- **Native mode: "Save & connect" on a local model could take chat down** until the
  next unlock — and each retry made it worse. Three stacked defects: the settings
  page submitted the Docker-era `host.docker.internal` host, the save endpoints
  registered that URL untranslated (the probe path translated; the register path
  didn't), and the gateway refuses a provider whose hostname doesn't resolve —
  *after* the old registration was already deleted, leaving no provider at all.
  Saves now translate the URL for the runtime they're in (both directions, so
  rolling back to Docker keeps working), a refused registration restores the
  previous one instead of leaving nothing, and the degraded-catalog fallback
  translates too (it used to fail exactly when it was needed).
- **The assistant now tells the time in your timezone.** The chat's time note used to
  inject bare UTC and leave the conversion to the model — which couldn't know your zone
  and sometimes got the math wrong ("Good morning! It's 5:32 am" at 11:32 PM). The
  browser now reports its timezone, and the note states your local date and time
  outright (UTC kept alongside for cross-zone questions). Scheduled runs use it too.
- Setup wording no longer says your passphrase "encrypts everything on this device" —
  it encrypts your SmartBrain data (chats, documents, settings), and now says exactly
  that.

## [0.8.4] - 2026-07-29

### Added
- Users still on an old desktop app get a **one-time, self-retiring banner** in the
  app ("your desktop app needs a one-time update", with the download link and the
  brew/scoop one-liners). It ships in the app image — which every install already
  updates automatically — and disappears forever the moment a self-updating desktop
  app talks to the backend. After that one click, nobody performs a manual update
  again.

### Added
- **The desktop app now updates itself — the last thing that didn't.** The app
  image always self-updated; the launcher binary required a package-manager
  command, which is why new capabilities (like native mode) only reached users
  who happened to run `brew upgrade`. The launcher's 6-hour check now also
  updates the launcher itself: verified download from the release (sha256
  sidecar), atomic swap with the previous version kept as backup, seamless
  relaunch — the running stack is untouched and outlives the handover. Dev
  builds never self-update; every failure path leaves the current install
  running. From this version on, no one runs `brew upgrade` again.

### Fixed
- The first live docker→native migration failed while unpacking the Python runtime
  (its archive lists `bin/` symlinks before anything creates `bin/`; the unpacker
  now creates a symlink's parent directory like it always did for files) — and a
  failed takeover now discards its half-made data copy, so a later retry re-copies
  fresh instead of ever booting a stale snapshot. Docker data was and remains
  untouched throughout; the failed run cost ~10 minutes of downtime and rolled
  back cleanly.

## [0.8.3] - 2026-07-29

### Changed
- Native mode can now take over an existing Docker install: on its first run it
  COPIES your data out of the volumes (which are mounted read-only during the copy
  and left byte-for-byte untouched as the rollback), and stored local-model
  addresses translate automatically between the Docker and native worlds in both
  directions — migrating there and rolling back both work without re-entering
  anything.

### Changed
- Native mode (still behind its opt-in flag) grew up: the launcher now writes the
  gateway's no-request-logging config before it ever starts (the native equivalent
  of the container fix — a fresh native gateway was observed booting with logging
  on by default), watches both processes and restarts a crashed one (bounded — a
  crash loop reports instead of spinning), and detaches them so quitting the
  launcher no longer takes SmartBrain down with it.

### Changed
- The launcher can now run SmartBrain **without Docker** behind a hidden opt-in
  (`SMARTBRAIN_NATIVE=1` — deliberately not in the menu yet while it matures): it
  assembles an install from the pinned standalone-Python runtime, the release's
  wheelhouse, and the mirrored gateway binary — every download sha256-verified,
  installed offline into a versioned directory whose `current` pointer only flips
  on success, so a failed upgrade leaves the previous version running (a rollback
  the Docker path never had). Docker remains the default and is untouched.

## [0.8.2] - 2026-07-28

### Changed
- Groundwork for running SmartBrain WITHOUT Docker (no user-visible change today —
  inside the container everything behaves byte-for-byte as before): the app now
  detects where it's running, and natively defaults to loopback URLs for the
  gateway and local model servers, a per-OS user-data directory for the database,
  and a file-based version stamp. A new CI job boots the app natively on macOS,
  Windows, and Linux — using the exact standalone-Python runtime a future
  Docker-free install would ship — and walks the full first-run flow on each.

### Changed
- Self-improvement's learning is now repeatable: the critique call pins a low
  sampling temperature (identical evidence used to yield nothing on one run and a
  high-confidence finding on the next), and the critique now sees how the
  unsatisfying requests classify — plus clearer rules on when a pattern is a
  global preference versus a per-request-type strategy. Verified live: two
  identical probe runs now produce the identical finding.

## [0.8.1] - 2026-07-28

### Added
- **Settings → Self-improvement**: the framework's home in the app — switches for the
  self-review and the prompt optimizer, the full record of every improvement it has
  proposed, applied, kept, or reverted, and each learned strategy with its status.
  No more API-only toggles.

### Changed
- **Docs truth pass** across the user guide: the MLX-only embeddings path now documents
  the simple settled setup (an encoder model served directly on your MLX server) with the
  bundled Qwen3 shim as the fallback; MLX chat setup leads with a server app instead of
  `pip install`; the privacy page's "what leaves your machine" now includes web
  search/fetch; document **tags**, the chat **Trash**, **big-document** behavior,
  where scheduled output lands, the Web-search and Memory settings, and the whole
  self-improvement framework; plus an **Uninstall** section, README links to the
  changelog/contributing/security policy, and assorted small fixes.

## [0.8.0] - 2026-07-28

### Added
- **The Prompt Optimizer can now go live — with evidence, on trial, and visibly.**
  A shadow strategy earns activation only when it kept firing (8+ matched asks) AND
  the problem it targets persisted (a quarter or more of its matched turns going
  badly); activation is announced in the digest and starts a measured trial against
  that baseline — kept only if things measurably improve, auto-disabled (and
  announced) if they don't or get worse. One trial at a time across the whole
  framework, so every measurement stays attributable. When guidance shapes an
  answer it rides a tiny trailing note (the prompt cache is never touched) and the
  answer shows a quiet **"guided · …" chip** — hover it to read the exact steering
  sentence. Everything remains behind the optimizer switch, off by default.

### Added
- **Prompt Optimizer groundwork — shadow mode** (off by default:
  `PUT /api/selfimprove/optimizer {"enabled": true}`). When enabled, each incoming
  ask is bucketed by a zero-latency rule-based classifier (factual / multi-step /
  code / retrieval / ambiguous) and the self-review may learn a per-type steering
  strategy from flagged windows — but every strategy is **shadow**: it never touches
  a live prompt. Shadow strategies only count the turns they *would* have applied
  to, building the evidence the go-live gate (next phase) will judge them on before
  anything ever changes a real answer. All observation rows are content-free;
  strategy text is encrypted at rest; `GET /api/selfimprove/optimizer` shows
  everything learned.

### Added
- **The self-review now makes suggestions, not just fixes.** Two new detectors — both
  pure pattern-matching over your own messages, no model involved, and neither ever
  acts on its own:
  - **Routine spotting**: an ask you've typed 3+ times on a daily or weekly rhythm
    ("summarize my open tasks…") becomes a ready-made schedule **waiting for your
    approval in Activity** — approve it and it's created; ignore or decline it and it
    is never offered again.
  - **Knowledge gaps**: when several searches come back empty, the digest names the
    topics your knowledge base couldn't answer so you know what's worth adding.
  Both appear under a "Suggested:" section of the self-review digest and in the
  improvement ledger.

## [0.7.0] - 2026-07-27

### Security
- **The gateway no longer keeps a plaintext copy of your prompts.** Bifrost (the
  built-in model gateway) ships with request logging on by default, which had been
  writing every prompt and reply — chat, memory, knowledge content sent for
  embedding — into an unencrypted `logs.db` inside its data volume, alongside
  SmartBrain's encrypted database. Fixed at three layers: the stack now starts
  Bifrost with its logging store disabled at the source and **destroys any existing
  `logs.db` on startup**; the app itself re-enforces no-logging over the gateway's
  admin API at every startup and unlock (so installs still on an older stack
  definition are protected as soon as their app image updates — content capture
  stops immediately and historical rows are purged by the gateway's own 1-day
  retention cleaner); and the weekly fresh-install test now fails if a `logs.db`
  ever reappears. No user action needed beyond restarting SmartBrain.

### Added
- **SmartBrain now reviews and improves itself** (off by default — turn it on with
  `PUT /api/selfimprove {"enabled": true}`; a Settings toggle is coming). Three pieces:
  - **It finally measures itself**: every chat/agent turn records content-free speed &
    quality telemetry (latency, steps, degraded/step-budget outcomes, per-turn tokens),
    and stopping or regenerating an answer is recorded as implicit feedback.
  - **An 8-hour self-review** (while unlocked) scores Chat, Knowledge, and Tools from
    that telemetry — pure SQL over plaintext metadata — and surfaces a digest through
    the scheduled-updates feed ONLY when something needs attention; silence is normal.
  - **A careful improvement loop**: when a window is flagged, a LOCAL model (never a
    cloud one — your chats don't leave the machine, and only messages YOU wrote are
    used as evidence) may propose one durable preference. High-confidence findings are
    applied as a visible "(learned) …" memory fact, put on trial, measured against the
    dissatisfaction rate they were applied under, auto-reverted if things get worse
    (or if no evidence accumulates), and every applied/reverted change is announced in
    the digest. One change at a time, at most 10 learned facts, hand-deleting a fact
    in Settings → Memory permanently rejects it, and a reverted preference is never
    re-applied. Kill-switch and privacy gates fail closed.

## [0.6.9] - 2026-07-24

_This section rolls up everything shipped across tags 0.6.1-0.6.9; earlier practice left these under Unreleased after tagging._

### Added
- "Delete all…" now lives on the Chat page itself (next to the saved-chats picker, on
  desktop and phone) — behind a confirmation, and everything still lands in the Trash
  with 30 days to restore. The Trash itself stays in Settings → Account & Data.

### Fixed
- Chats and agent turns answer noticeably sooner on local models: the live clock in
  the system prompt was invalidating the model server's prompt cache every minute
  (measured 11% cache efficiency — the model re-read the entire conversation on
  almost every step, ~12 seconds of pure re-processing per agent step on a long
  context). The prompt head is now byte-stable and the current time rides a tiny
  trailing note instead, so the cached prefix survives across steps and turns.

### Fixed
- The assistant can no longer present an unrelated document as a finding: when it
  asks for a focused summary of a document that never mentions the focus topic, the
  result now says so plainly ("the focus does not appear anywhere in this document"),
  giving the model a clean basis to drop it — seen live when a "summarize the Tribeca
  doc" reply confidently included a document with zero Tribeca references. And a
  document-scoped search called with a title instead of an id now returns a corrective
  error instead of a silent empty result the model reads as "the document doesn't
  mention it."

### Added
- **MLX-only semantic search**: a new "MLX embeddings" local provider plus a bundled
  one-command server (`tools/mlx_embed_server/install.sh`) serve Qwen3-Embedding on
  Apple Silicon with correct pooling — chat servers like oMLX refuse this model class.
  Connect it under Settings → Local models, route the Embedding slot to
  `mlxe/qwen3-embedding-0.6b`, Reindex, and the whole stack runs without Ollama.
  The provider is embeddings-only by construction: it can never be offered a chat turn.

### Fixed
- Hundreds-of-pages documents are now FULLY searchable by meaning. Semantic coverage
  used to stop silently at ~256k characters (64 chunks) and ingest truncated files at
  1M characters — most of a big S-1 was invisible to meaning search. Both limits now
  cover ~4M characters (about a thousand dense pages), and they match, so every stored
  character is reachable. Big documents embed INCREMENTALLY in the background: adding
  one returns instantly, the indexer works between your chats and resumes exactly where
  it left off after a restart or re-lock, the "Indexing X of Y" count now stays honest
  until every chunk of every document is really done (it used to claim done after the
  first chunk), and renaming a huge document no longer stalls while it re-embeds.
- Upgrade safety: the database is checkpointed immediately after schema migrations —
  a migration left in the write-ahead log could make the database fail to reopen after
  an unclean stop (found while testing; never reported in the field).

### Added
- Chat gained a Refresh button — and refreshes itself whenever you return to the app —
  so a conversation continued on your phone appears on the desktop (and vice versa)
  without a page reload. Injected scheduled-update notices survive the refresh.

### Changed
- Activity's "Always allowed" list is collapsed by default (with a count), keeping the
  page focused on what needs your attention.

### Fixed
- "response exceeds channel limit" is gone from the phone: big responses (a grown
  Activity feed, a large document opened remotely) now stream over the encrypted
  connection in ordered parts and reassemble on the phone, instead of being refused
  once they outgrew a single message. Bounded at 8 MB per response; an older phone
  app keeps the old clean error until it updates.
- Phone screens no longer clip content off the right edge: a long unbreakable token
  (a URL in a schedule's output, a full source link in a search citation) or a wide
  control (the seven-tab Settings strip, a file picker) could silently inflate the
  page past the viewport — Info, Knowledge, every Settings page, and Help were all
  affected. Pages now stay at screen width everywhere: run output and snippets wrap
  long tokens, citation chips clip inside their pill, document tag rows flow to a
  second line, and Help's section nav scrolls in place. Audited: every route is
  clean at phone width, desktop layouts unchanged.
- A phone could keep running an old version of the app for hours after a release —
  even after deleting and re-adding it: the hosted origin served the app's HTML
  without a no-cache header, so iOS was allowed to treat stale HTML as fresh
  (and "Add to Home Screen" copies Safari's cached site data into the new app).
  The origin now forces a revalidation of the HTML on every launch, and the
  build's content-hashed assets are marked immutable so they cache forever safely.
- A paired phone stays connected instead of quietly dying after a short idle: the
  connection now sends a small keepalive every 20 seconds (idle NAT/firewall mappings
  were expiring in as little as 30 seconds, killing the path while the status still
  said "connected"), a dead path is noticed within 45 seconds and reconnects on its
  own, the retry budget doubled to about three minutes of patience (a phone radio
  waking from sleep needs more than three quick attempts), switching apps for up to
  three minutes no longer drops the session (was 15 seconds), and even after the
  retries give up, the next tap in the app restarts the connection by itself instead
  of failing until you find Retry.
- Citations under an answer now reflect what the assistant actually READ, not
  everything its searches merely surfaced: a broad question no longer sprays chips
  for every unrelated document in the knowledge base, the same document found by two
  searches shows one chip per page, and a document that was read keeps its precise
  page links instead of a redundant whole-document chip. Search-only answers still
  cite their snippet hits — there, the snippets were the evidence.
- Tool-using turns stopped taking ten minutes: the agent now stops asking for more
  tools the moment its gathered results reach the model's context budget and writes
  the answer (every extra round-trip past that point re-fed the model a prompt it
  couldn't hold — pure prefill waste), a "Writing the answer…" line shows during that
  final long call, and the background summary-tree builder now waits for five quiet
  minutes before touching the local model — its 30-second chunk calls were making
  chats queue behind them on oMLX's single request slot.

### Added
- **Tags on documents and vaults**: label anything in Knowledge with your own tags
  (a Tags button on every document row and vault card — comma-separated, up to 20).
  Tags show as chips; clicking one filters both lists to that tag, and keyword +
  Best search match tags instantly with no reindex. Tags are encrypted like
  everything else, survive renames, never travel in a shared vault export, and
  can't be put on an imported vault's copies (a vault update would overwrite
  them — Detach first to make the copy yours).
- **Chat trash**: deleting a chat — or every chat at once with the new "Delete all
  chats" action in Settings → Account & Data — now moves it to a Trash instead of
  destroying it. Trashed chats disappear from every list but stay restorable for
  30 days from the new Trash card, which shows when each was deleted and how long it
  has left; after that the scheduler purges them for good, or "Empty trash" does it
  immediately.
- **Info page**: schedule output moved out of Schedules into a new Info page — in the
  sidebar and, on a phone, its own bottom tab. The All tab shows every run newest-first;
  a tab per schedule shows just that schedule's output. Schedules keeps Items + Create,
  and "Run now" points at Info for results. (New scheduled-run notices still appear in
  Chat exactly as before.)
- Book-scale documents, summarized instantly: every document now gets a background
  **summary tree** (chunk summaries reduced into one whole-document summary), built a
  piece at a time by the scheduler — encrypted like everything else, resumable, yielding
  to your live chats, and shown as "Preparing instant summaries — X of Y" on the
  Knowledge page. Asking Chat to summarize becomes an instant cached lookup at any size;
  a still-building document answers from what's covered and says so; focus questions
  ("summarize the fees") run over the stored tree in seconds. A new **Document
  summaries** slot in Model routing lets a big-context model build trees fast while the
  local model stays the private default; `kb_search` can now search INSIDE one document
  (the right way to find one fact in a thousand pages); and tool-using turns get two
  more steps now that an exhausted budget degrades to an answer.
- Web tooling that meets what users expect of a modern assistant:
  **web pages read as articles** (fetches now return clean extracted prose + title via
  the same reader ingestion uses, instead of raw HTML soup); **pluggable search
  providers** — SearXNG (self-hosted), Brave, and Tavily (bring-your-own keys, stored
  encrypted) with DuckDuckGo always anchoring the fallback chain, configured on a new
  Settings → Web search page; a **one-step `web_research` tool** that searches, then
  fetches and extracts the top pages (one per site, bounded) so a research question
  no longer burns the step budget page by page; and **live tool activity in Chat** —
  "Searching the web… ✓ / Reading a page…" narrated in place of the silent thinking
  dots while the assistant works.

### Fixed
- A bot-blocked website can no longer convince the assistant it has "no web access":
  page fetches now send the full browser-consistent header set (many WAFs 403 a
  browser User-Agent that arrives with a bare client fingerprint), and when a site
  still refuses, the error fed back to the model says exactly that — this one site
  refused, web access works, try a different result — instead of a bare HTTP status
  that small local models read as a dead internet and give up on.
- Huge documents no longer defeat the budget rescue: the recovery answer is now
  built from a prompt REBUILT to fit the model's context (the question, the first
  tool result, and the newest work — a 170k-character document had overflowed a
  32k-token model so badly that the rescue call itself failed), and reading a
  document several times larger than the context now says so in the result and
  points the model at summarize_document, which chunks and covers the whole file.
- "step budget exhausted" can no longer be an entire chat reply: when the assistant
  runs out of tool steps mid-task it now writes a real answer from everything it
  already gathered (saying what it couldn't finish); document reading no longer
  starves the budget — a timidly small page request is raised to the largest window
  the model's context fits, so long documents take one or two reads instead of five —
  and the read tool now points models at summarize_document for whole-document
  overviews.

## [0.6.0] - 2026-07-21

### Added
- All docs media regenerated in the new design, dark theme throughout: the 11
  quickstart GIFs (with their reduced-motion posters) and the five guide screenshots
  are re-shot on the redesigned app — sidebar shell, message rows, chips, modals. The
  recorder now pins the dark theme explicitly and its storyboards follow the new
  surfaces (settings tabs, the icon Send button, chip citations and badges, and the
  document viewer that is now a true modal).
- An accessibility and performance sweep, verified by axe across every route in both
  themes and viewports (now zero violations): the app version, composer hint, and
  mobile tab labels meet AA contrast; light-theme green/red are retuned so status
  chips pass on their tinted backgrounds (enforced forever by new contrast tests);
  dialogs return focus to what opened them; task checkboxes are labeled for screen
  readers and — with all native toggles — bigger and theme-colored; Help's scrollable
  code blocks are keyboard-reachable; and the sidebar no longer pops in after load
  (layout shift 0.157 → 0.002 on a cold open).
- A quiet motion pass: dialogs, the mobile More sheet, toasts, and the "Jump to latest"
  pill now ease in (120–200ms, transform/opacity only) while dismissal stays instant;
  tab hovers stopped snapping. Every animation — including the two smooth-scroll
  jumps — honors `prefers-reduced-motion`.
- One brand mark everywhere: a new generator (`tools/brand/make_icons.py`) derives every
  raster asset from the one mascot source — PWA icons with a **properly maskable** variant
  (no more white bleed or clipped wordmark on Android), an apple-touch icon, a real
  favicon, a face-tight header mark that's legible at 30px, and the macOS Dock icon.
  The app manifest moved to the design palette (#121212) and gained the 192px maskable
  size; iOS gets proper standalone metas (app title, translucent status bar). The
  landing page now runs the app's design system — Inter, the token palettes in both
  themes, the real mark instead of an abstract gradient chip, icon pillars instead of
  emoji — plus a favicon and social-preview (Open Graph) card, and a fix for a
  pre-existing bug where the long install commands forced the whole page to scroll
  sideways (phones included). The tray monogram is untouched.
- Settings and onboarding joined the system: the settings section tabs are the same
  pill strip as everywhere else (one scrollable row on phones), and the last bare
  "Loading…" texts — root dispatcher, Settings, Usage, and the Setup busy state —
  became the shared spinner.
- Planner, Schedules, and Email joined the system: one tab style everywhere, real
  empty states, spinners instead of bare "Loading…", and the email reader now opens
  in the same focused modal as every other dialog.
- Knowledge on the same system: document rows with quiet actions, search-result
  citations as chips, vault identity as proper Subscribed/Public chips with monospace
  fingerprints, real empty states — and the document viewer is now a true focused
  modal that still opens at the cited passage.
- Chat reads like a modern assistant: full-width labeled message rows on a reading
  measure (bubbles retired), visible thinking/streaming states, a redesigned composer
  with an always-visible Stop during generation, citation chips, an inline approval
  card, and a "Jump to latest" pill with scroll-aware auto-follow.
- Approvals got their proper surface: pending actions in Activity render as deliberate
  "Action Cards" — tool, plain scope lines, a reversibility badge, and a clear
  Approve / Deny / Always-allow hierarchy (red is reserved for irreversible actions).
- The app shell is rebuilt: a desktop sidebar rail (icons, labels, badges) with a slim
  top strip carrying an "Encrypted · On-device" trust chip, and on phones a bottom tab
  bar in the thumb zone (Chat · Knowledge · Activity · More) with proper notch/home-bar
  safe areas — replacing the wall-of-links top bar on every screen size.
- A shared component library (Modal, Tabs, Field, Toast, EmptyState, Spinner, Chip,
  ActionCard): one modal shell now backs every dialog — starting with the app-wide
  Confirm — ending the era of three competing overlay implementations.
- A real icon system: a vendored Lucide subset (ISC) rendered by one Icon component
  — the emoji-as-icons era (\U0001F313\u2600\U0001F319\u22EF\u2715) is over; icons inherit theme color and weight.
- The app's typeface is now Inter Variable, self-hosted (latin subset, 97 KB, OFL
  license shipped alongside) — no font CDN, matching the privacy posture.
- A new visual foundation — "calm precision-minimal": tonal dark (#121212 family) and
  warm-white light themes with a single muted-teal accent, a real type/spacing/radius
  token system, a visible keyboard-focus ring everywhere, and WCAG-AA contrast enforced
  by a permanent test in both themes. Every page reskins; nothing moves yet.
- Public vaults: publish a vault openly, subscribe to one by URL with trust-on-first-use
  publisher pinning, check for and apply updates (the pinned publisher enforces every
  update), opt in to scheduled auto-updates, and the finishing UI surfaces plus an MCP
  provenance door (#76, #77, #78, #79, #80).
- The official **example vault**: the user guide itself, published as a public vault at
  `https://smartbrain.securecloudgroup.com/vaults/smartbrain-docs.sbvault` — subscribe by
  URL to try Vaults in one paste — plus the builder that mints and updates it
  (`tools/example-vaults/build.py`).

### Changed
- Docs & landing truth pass: the privacy page discloses vault-subscription fetches, the
  landing page gains a Vaults pillar and drops an overstated "nothing is sent to us, ever"
  (the optional phone-access relay exists), the MCP page notes imported-content provenance
  labels, and getting-started notes the paired phone updates itself.
- Vaults are now a first-class guide: **docs/04-vaults.md** ("Share knowledge with Vaults" —
  it also headlines the in-app Help) instead of a subsection buried in Using SmartBrain_3000;
  the later guides renumbered 05–09 with every cross-link updated.

### Fixed
- One dead local-provider URL can no longer blank the whole model list: the gateway
  catalog call is now time-bounded (it previously inherited the pooled client's 60s
  timeout), and when the catalog fails, `/api/models` degrades to directly-probed local
  models — Chat keeps working on local models and shows an honest "couldn't load the
  model list / degraded" notice instead of a misleading "add a key" empty state. Save
  failures on the Model-routing page now land inline next to their button instead of a
  vague connection error at the page bottom.

**Migration:** subscribing to a public vault records its upstream source in new encrypted
columns — additive and forward-only, applied automatically on first launch (#77).

## [0.4.6] - 2026-07-16

### Added
- Deterministic citations — source chips on chat answers that drew on your knowledge (#68).
- Chat controls: stop, copy, regenerate, and rename a message (#70).
- Public-vaults groundwork: an "open" mode in the vault format (the transport half of
  public vaults), imported-document protections that must predate any update path, and
  SSRF-guarded vault-fetch transport helpers (#72, #74, #75).

### Changed
- Docs: a Windows dev-VM runbook, and refreshed onboarding GIFs including the Vaults
  clip (#69, #71).

### Fixed
- Vault updates preserve unknown body fields via read-modify-write (#73).

## [0.4.5] - 2026-07-15

Re-release of v0.4.4 on the same commit (a release-pipeline retag); no source or behavior
changes.

## [0.4.4] - 2026-07-15

### Changed
- CI: a Linux install smoke test runs the documented cold start on every release and as a
  weekly drift canary (#67).

### Fixed
- Vaults: visible membership, guided add, inline errors, and a labeled export — from
  live-test findings (#65).
- A corrupted vault container now returns a clean 400 instead of an unhandled 500 (#66).

## [0.4.3] - 2026-07-15

### Changed
- Packaging manifests bumped to v0.4.2; the release job no longer fails when Actions
  can't open the packaging-bump PR (#62, #63).

### Fixed
- Three UX papercuts: tool-leak guidance, inline save feedback, and Homebrew update
  docs (#64).

## [0.4.2] - 2026-07-15

### Added
- Installer polish: auto-launch after install, a download notification, a real app icon,
  and fewer privacy prompts (#61).

### Changed
- Bifrost mirrored to GHCR and pinned to v1.6.4 for a single-registry install (#59).

### Security
- Vault name no longer leaks in the plaintext manifest and is preserved on import;
  first-run polish from end-to-end testing (#58).

### Fixed
- Local models reach routing again on bifrost v1.6.4 — they had been blocked by its
  SSRF guard (#60).

## [0.4.1] - 2026-07-15

**Migration:** the Docker layout moved from bind-mounts to named volumes to fix a
Linux-only install crash-loop. Your data now lives in named volumes; back up with the
in-app encrypted backup, and note that uninstall never touches the data volumes (#57).

### Added
- Download/landing page for the app, with an opt-in Caddy overlay for the RTC signaling
  node (#51).

### Changed
- One-command install packaging: a Homebrew cask plus winget and Scoop manifests, with a
  release workflow that auto-bumps Homebrew/Scoop/packaging on a version tag and
  auto-submits winget updates (#50, #52, #53).
- Docs refreshed for the v0.4.0 install and Vaults, with a new-user test plan (#54, #55).

### Fixed
- The launcher finds Docker when started from a Finder-launched app (#56).
- Named volumes replace bind-mounts so a fresh Linux install no longer crash-loops on
  volume ownership; plus launcher hardening and doc corrections (#57).

## [0.4.0] - 2026-07-13

The knowledge release: a real knowledge base with citations, shareable Vaults, chat
document tools, schedules, and prebuilt-image distribution.

**Migration:** first launch creates the new vault tables and a per-document ownership
column automatically — forward-only and data-safe (schema migrations 20–22).

### Added
- Knowledge search that is fast and honest — hybrid BM25 + vector ranking over an
  in-memory index, with citations that open a document at the passage that matched
  (#38, #40, #48).
- Ingest Word, PowerPoint, and Excel files, with deduped, non-blocking uploads and a
  background indexer that actually drains (#42, #48).
- Vaults: group documents into collections and export or import a sealed vault to share
  knowledge with another person (#44, #48).
- Chat can read, list, and summarize a document of any length, and save a note or
  summary back into your knowledge (#34, #35, #36).
- Chat renders assistant markdown instead of raw `###` and `**` (#37).
- Schedules: Output / Create / Items tabs with PWA parity, approval-gated chat tools to
  read and manage schedules, and fired-schedule output delivered into the chat window
  (#24, #26, #27).
- A dedicated Agent-tasks model route, with a cold-load timeout fix (#25).

### Changed
- Distribution: prebuilt multi-arch images published to GHCR with a pull-based compose,
  so install no longer builds from source (#46, #49).
- `.dockerignore` keeps the vault and secrets out of image builds (#22).
- Dependency and toolchain maintenance: the web build on Node 22, Vite 8 (Rolldown),
  marked 18, pinned Dependabot toolchain majors, and the signaling image on Python 3.14
  (#11, #12, #15, #17, #19, #20, #21).

### Fixed
- Chat can keyword-search any saved document, not just the semantic index (#29).
- Reindex waits out a cold local embedding model instead of failing the first try (#32).
- Local-model calls are serialized, fixing oMLX "model is busy" chat failures (#33).
- PDF uploads are no longer titled from a stale `.docx` filename in metadata (#30).
- Scheduled output appears in the chat window; the separate tab was removed (#28).
- In-app Help deep links resolve to the right section and heading (#14).
- Gmail OAuth completes over HTTPS via a loopback redirect helper (#23).
- End-to-end PDF ingest, indexing, and search test coverage (#31).

## [0.1.0] - 2026-07-09

First tagged public release. SmartBrain_3000 is a local-first, single-user, self-hosted
encrypted AI assistant that runs in Docker on your own machine.

### Added
- CI (GitHub Actions): backend ruff + pytest, hermetic installer tests, web
  svelte-check + vitest + build, and a build + test of the shipped Docker image on
  every PR — the required status checks that guard `main`.
- Dependabot: grouped weekly updates for pip, npm, GitHub Actions, and Docker.

### Changed
- Base image bumped to `python:3.14-slim`; web dev toolchain upgraded to Vite 7.

### Security
- Desktop-local fence extended to device-enrollment and MCP-token endpoints so a
  paired remote device cannot self-enrol/revoke devices or read/rotate the MCP token.

### Fixed
- Restore keeps the displaced database's WAL and quarantines future-schema backups;
  deterministic chat-message ordering; installer gating, custom-port, and failed-update
  recovery; assorted UX/accessibility and docs accuracy fixes.
