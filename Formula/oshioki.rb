class Oshioki < Formula
  desc "Touch ID or WebAuthn approval for sudo requests"
  homepage "https://github.com/epsalmond/oshioki"
  # URL, sha256, and version are filled in by the bottle workflow the first
  # time a release is bottled. Do not hand-edit them.
  url "https://github.com/epsalmond/oshioki/releases/download/v0.1.0/oshioki-macos-arm64-0.1.0.tar.gz"
  sha256 "8a1e7e060e9762ce4a1c228c705913972e0c6af9cc8b0956153c31ab751c143f"

  bottle do
    root_url "https://github.com/epsalmond/oshioki/releases/download/v0.1.0"
    rebuild 2
    sha256 cellar: :any, arm64_sonoma: "9c18ca84f39d4964948a7b3474c9a892d3badb664f02a52786bff10f29a13ebd"
  end
  version "0.1.0"
  license any_of: ["MIT", "Apache-2.0"]

  def install
    bin.install "oshioki", "oshioki-agent", "install-oshioki-hook"
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
      The server is not started automatically; run oshioki-server with
      the environment in the runbook. NATS with JetStream is required.
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/oshioki --version")
  end
end
