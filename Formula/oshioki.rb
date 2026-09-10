class Oshioki < Formula
  desc "Touch ID or WebAuthn approval for sudo requests"
  homepage "https://github.com/epsalmond/oshioki"
  # URL, sha256, and version are filled in by the bottle workflow the first
  # time a release is bottled. Do not hand-edit them.
  url "https://github.com/epsalmond/oshioki/releases/download/v0.1.4/oshioki-macos-arm64-0.1.4.tar.gz"
  sha256 "51c04c85dbfea0957a8337a1d81eabb5f01657db679ae7571a3cb579727a268e"

  bottle do
    root_url "https://github.com/epsalmond/oshioki/releases/download/v0.1.3"
    rebuild 4
    sha256 cellar: :any, arm64_sonoma: "e1db20d4f4d34853e94d3835b769f206283842f100f436bb46caa7e42df1c228"
  end
  version "0.1.4"
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

  # Pouring a relocatable bottle rewrites the plugin's install name to the
  # Cellar path and re-signs it, so the manifest built in CI no longer
  # matches what is on disk and install-oshioki-hook refuses the plugin.
  # Brew has verified the bottle itself by now; the manifest is regenerated
  # from the installed files so the installer checks those.
  def post_install_steps
    sums = %w[oshioki oshioki-agent install-oshioki-hook oshioki-laptop-setup].map { |f| bin/f } +
           %w[oshioki.dylib manifest.json].map { |f| libexec/f }
    sums += %w[oshioki-server oshioki-phone-setup].map { |f| bin/f }.select(&:exist?)
    (libexec/"SHA256SUMS").atomic_write(sums.map { |f| "#{Digest::SHA256.file(f).hexdigest}  #{f.basename}\n" }.join)
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
