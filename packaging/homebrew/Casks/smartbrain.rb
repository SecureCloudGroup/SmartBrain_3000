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
  version "0.4.2"
  sha256 "3fe38a60fba4b720e79fc8522a1b47de1f67345f74292bcf3db1de83dbdce249"

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

  # Docker is required (the app runs the stack in containers). We do NOT force-install Docker Desktop
  # as a dependency, because plenty of this audience run Colima / OrbStack / Engine instead — the
  # launcher detects Docker and guides the user if it's missing.
  caveats <<~EOS
    SmartBrain runs on Docker. If you don't have it yet, install Docker first (Docker Desktop is the
    easiest; Colima/OrbStack also work) and start it: https://docs.docker.com/get-docker/
    Note: Docker Desktop's own first launch asks you to accept its terms — do that before continuing.

    SmartBrain has been LAUNCHED for you — it's a menu-bar app (icon at the top-right of your
    screen). The first run downloads the app image (a minute or two), then opens it in your browser
    at http://localhost:33000. To start it again later, open SmartBrain from Applications.

    If macOS asks whether SmartBrain may "access data from other apps", click Allow — that's it
    locating your Docker installation.

    Your data lives in Docker volumes and survives uninstalls; back it up in-app (Settings).
  EOS

  # "zap" removes the launcher's config dir. The user's DATA is in Docker volumes (smartbrain_data /
  # bifrost_data), which Homebrew cannot remove — deliberate: uninstalling an app should not silently
  # shred a knowledge base. Removing the data is an explicit `docker volume rm` by the user.
  zap trash: "~/Library/Application Support/SmartBrain"
end
