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
  version "0.4.0"
  sha256 "59a37dc877d24088a1132f22c54819fb4914845ff6754f4756225c8cf3e1b395"

  url "https://github.com/SecureCloudGroup/SmartBrain_3000/releases/download/v#{version}/SmartBrain-macos.zip"
  name "SmartBrain"
  desc "Local-first personal AI assistant that keeps your data on your machine"
  homepage "https://github.com/SecureCloudGroup/SmartBrain_3000"

  app "SmartBrain.app"

  postflight do
    system_command "/usr/bin/xattr",
                   args: ["-dr", "com.apple.quarantine", "#{appdir}/SmartBrain.app"]
  end

  # Docker is required (the app runs the stack in containers). We do NOT force-install Docker Desktop
  # as a dependency, because plenty of this audience run Colima / OrbStack / Engine instead — the
  # launcher detects Docker and guides the user if it's missing.
  caveats <<~EOS
    SmartBrain needs Docker (Desktop, Colima, OrbStack, or Engine). Open SmartBrain from your
    Applications folder; on first run it downloads the app image and opens it in your browser.
  EOS

  # "zap" is what `brew uninstall --zap` removes. Your knowledge lives here — a plain uninstall keeps
  # it; only an explicit zap clears it.
  zap trash: "~/Library/Application Support/SmartBrain"
end
