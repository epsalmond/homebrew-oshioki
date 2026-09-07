class Oshioki < Formula
  desc "Touch ID or WebAuthn approval for sudo requests"
  homepage "https://github.com/epsalmond/oshioki"
  # URL, sha256, and version are filled in by the bottle workflow the first
  # time a release is bottled. Do not hand-edit them.
  url "https://github.com/epsalmond/oshioki/releases/download/v0.1.3/oshioki-macos-arm64-0.1.3.tar.gz"
  sha256 "fbd4420ae54b51e26a2eb0e44cc0c70842de5f5165e6134a75f1ca9f9b106c8a"

  bottle do
    root_url "https://github.com/epsalmond/oshioki/releases/download/v0.1.3"
    rebuild 4
    sha256 cellar: :any, arm64_sonoma: "e1db20d4f4d34853e94d3835b769f206283842f100f436bb46caa7e42df1c228"
  end
  version "0.1.3"
  license any_of: ["MIT", "Apache-2.0"]

  def install
    bin.install "oshioki", "oshioki-agent", "install-oshioki-hook", "oshioki-laptop-setup"
    libexec.install "oshioki.dylib", "SHA256SUMS", "manifest.json"
  end

  # Pouring a relocatable bottle rewrites the plugin's install name to the
  # Cellar path and re-signs it, so the manifest built in CI no longer
  # matches what is on disk and install-oshioki-hook refuses the plugin.
  # Brew has verified the bottle itself by now; the manifest is regenerated
  # from the installed files so the installer checks those.
  def post_install
    sums = %w[oshioki oshioki-agent install-oshioki-hook oshioki-laptop-setup].map { |f| bin/f } +
           %w[oshioki.dylib manifest.json].map { |f| libexec/f }
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
      The server is not started automatically; run oshioki-server with
      the environment in the runbook. NATS with JetStream is required.
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/oshioki --version")
  end
end
