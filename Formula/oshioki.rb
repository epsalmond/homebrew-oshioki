class Oshioki < Formula
  desc "Touch ID or WebAuthn approval for sudo requests"
  homepage "https://github.com/epsalmond/oshioki"
  # URL, sha256, and version are filled in by the bottle workflow the first
  # time a release is bottled. Do not hand-edit them.
  url "https://github.com/epsalmond/oshioki/releases/download/v0.1.6/oshioki-macos-arm64-0.1.6.tar.gz"
  sha256 "950cb36c19cb195c505d72fb29e6f6759b406efb8887bd384e7f04b5c0e74288"

  bottle do
    root_url "https://github.com/epsalmond/oshioki/releases/download/v0.1.6"
    rebuild 7
    sha256 cellar: :any, arm64_sonoma: "12cb93945b8eff1973199176b1be019b27c3dc03d6062b8830de3bb59d162bf9"
  end
  version "0.1.6"
  license any_of: ["MIT", "Apache-2.0"]

  depends_on "python@3.14"

  def install
    bin.install "oshioki", "oshioki-agent", "install-oshioki-hook", "oshioki-laptop-setup"
    # Older release archives predate phone setup. The next release includes
    # both files, so keep this formula usable while the release is prepared.
    %w[oshioki-server oshioki-phone-setup].each do |name|
      bin.install name if File.exist?(name)
    end
    libexec.install "oshioki.dylib", "SHA256SUMS", "manifest.json"
  end

  def caveats
    <<~EOS
      The sudo plugin and hook state live outside the Cellar and need root.
      To wire them up, create /etc/oshioki/install.env (0600, root-owned;
      see https://github.com/epsalmond/oshioki/blob/main/RUNBOOK.md),
      then run:
        sudo HOOK_BIN=#{bin}/oshioki \\
          PLUGIN_BIN=#{libexec}/oshioki.dylib \\
          OSHIOKI_CHECKSUMS=#{libexec}/SHA256SUMS \\
          #{bin}/install-oshioki-hook --prelaunch \\
          --config-file /etc/oshioki/install.env
      Releases with phone setup include oshioki-server and oshioki-phone-setup.
      To enroll a phone, install nats-server and run as yourself:
        oshioki-phone-setup
        sudo oshioki enroll
      Setup prefers Tailscale Serve. An existing HTTPS server is also supported:
        oshioki-phone-setup --server-url https://sudo.example.com --nats-config /path/to/hook-nats.env
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/oshioki --version")
  end
end
