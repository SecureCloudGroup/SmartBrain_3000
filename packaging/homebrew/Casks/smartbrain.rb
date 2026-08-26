# SmartBrain Homebrew cask — for the tap repo SecureCloudGroup/homebrew-tap.
# Installs with:  brew install --cask securecloudgroup/tap/smartbrain
#
# The app has no paid Apple Developer ID (a deliberate $0 choice) — it is ad-hoc signed, which is all
# Apple Silicon needs to RUN it. The only thing that would stop it is the quarantine flag, and modern
# Homebrew applies that to every cask with no built-in override. So the postflight below strips it,
# which is safe here because we built and ad-hoc-signed this exact bundle in our own CI. Without this
# step the user would hit the Gatekeeper "unidentified developer" wall — the whole reason we ship via
# Homebrew instead of a browser download is to avoid exactly that.
cask "smartbrain" do
  version "0.9.31"
  sha256 "b74736989fcf758dd821cbfdac9164c10bdf9998958e3097242b932127119b02"

  url "https://github.com/SecureCloudGroup/SmartBrain_3000/releases/download/v#{version}/SmartBrain-macos.zip"
  name "SmartBrain"
  desc "Local-first personal AI assistant that keeps your data on your machine"
  homepage "https://github.com/SecureCloudGroup/SmartBrain_3000"

  app "SmartBrain.app"

  postflight do
    system_command "/usr/bin/xattr",
                   args: ["-dr", "com.apple.quarantine", "#{appdir}/SmartBrain.app"]
    # Launch it right away: `brew install` should be the LAST step a user types. The launcher takes
    # it from here — pulls the app image and opens the browser — instead of leaving the user to dig
    # the next step out of caveat text buried in Homebrew's output.
    system_command "/usr/bin/open", args: ["-a", "#{appdir}/SmartBrain.app"]
  end

  # SmartBrain brings its own runtime on Apple Silicon — no Docker, nothing to install first.
  # Intel Macs have no native build (no pinned Python runtime exists for x86_64 darwin), so the
  # launcher falls back to Docker there and says so. We still do NOT force-install Docker Desktop:
  # that audience often runs Colima / OrbStack / Engine instead.
  caveats <<~EOS
    SmartBrain has been LAUNCHED for you — it's a menu-bar app (icon at the top-right of your
    screen). The first run downloads and verifies everything it needs (a few hundred MB, a few
    minutes), then opens it in your browser at http://localhost:33000. To start it again later,
    open SmartBrain from Applications.

    On an Intel Mac, SmartBrain runs in Docker instead. Install Docker first (Docker Desktop is
    the easiest; Colima and OrbStack also work) and start it: https://docs.docker.com/get-docker/

    If macOS asks whether SmartBrain may "access data from other apps", Allow or Deny is fine —
    it is only looking for Docker, which an Apple Silicon Mac does not need.

    Your data survives uninstalls — including `--zap`, which removes only the app's own files.
    Back it up any time from Settings → Account & Data.
  EOS

  # "zap" removes the app's OWN files and nothing else. It used to remove the whole
  # ~/Library/Application Support/SmartBrain tree, on the stated grounds that "the user's DATA is in
  # Docker volumes" — true when that line was written, and false the moment the Docker-free stack
  # landed: the database now lives at data/smartbrain.duckdb inside that very tree. On a live install
  # that path held a 34 MB knowledge base, so `brew uninstall --zap` would have shredded it.
  #
  # Listed leaf by leaf, deliberately, so that adding a new state directory later cannot silently
  # widen this into user data again:
  #   data/               the user's encrypted database — NEVER removed by an uninstall
  #   native/versions/    assembled runtimes (GBs) — safe to drop, re-downloaded on demand
  #   native/run/         pid files and logs
  #   native/bifrost-data/ gateway config, which holds PROVISIONED PROVIDER KEYS — removing it on
  #                       uninstall is the point: leaving credentials behind would be worse
  #   native/current, native-mode, docker-compose.release.yml — launcher bookkeeping
  zap trash: [
    "~/Library/Application Support/SmartBrain/docker-compose.release.yml",
    "~/Library/Application Support/SmartBrain/native-mode",
    "~/Library/Application Support/SmartBrain/native/current",
    "~/Library/Application Support/SmartBrain/native/run",
    "~/Library/Application Support/SmartBrain/native/versions",
    "~/Library/Application Support/SmartBrain/native/bifrost-data",
  ]
end
