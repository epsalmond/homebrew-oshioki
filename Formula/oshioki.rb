class Oshioki < Formula
  desc "Touch ID or WebAuthn approval for sudo requests"
  homepage "https://github.com/epsalmond/oshioki"
  # URL, sha256, and version are filled in by the bottle workflow the first
  # time a release is bottled. Do not hand-edit them.
  url "https://github.com/epsalmond/oshioki/releases/download/v0.1.12/oshioki-macos-arm64-0.1.12.tar.gz"
  sha256 "46140e02b798a15b95f40bb31e1c74e7409c6c94ed19fd311c7e772347c027ec"

  bottle do
    root_url "https://github.com/epsalmond/oshioki/releases/download/v0.1.11"
    rebuild 13
    sha256 cellar: :any_skip_relocation, arm64_sonoma: "14949fcab0522c0ff0bd07b11ca661422dc4ef104653dc000c694727c2614d41"
  end
  version "0.1.12"
  license any_of: ["MIT", "Apache-2.0"]

  depends_on "python@3.14"

  # The release tarball is already built: this formula only lays its files out
  # in the keg, so --build-from-source and a bottle pour install identical
  # bytes and there is no cargo build here to point at a prefix.
  #
  # From v0.1.8 onward the two Mach-O files in libexec carry
  # /opt/homebrew/opt/oshioki/libexec/<name> as their install name, set at
  # link time upstream (oshioki PR #89), so Homebrew's keg fixup finds nothing
  # to rewrite and the SHA256SUMS shipped beside them still verifies (oshioki
  # issue #87). Earlier tarballs, 0.1.7 included, do not: a bottle of one of
  # those still fails checksum verification whatever this formula says, so the
  # bottle has to be rebuilt from a release that includes that PR. A
  # non-default Homebrew prefix would need a tarball rebuilt with
  # OSHIOKI_PAM_INSTALL_NAME/OSHIOKI_PLUGIN_INSTALL_NAME set; only the default
  # arm64 prefix is supported here.
  def install
    bin.install "oshioki", "oshioki-agent", "install-oshioki-hook", "oshioki-laptop-setup"
    # Older release archives predate phone setup. The next release includes
    # both files, so keep this formula usable while the release is prepared.
    %w[oshioki-server oshioki-phone-setup].each do |name|
      bin.install name if File.exist?(name)
    end
    # The Touch ID sheet takes its name and icon from the calling process's
    # app bundle, so the agent oshioki-laptop-setup starts has to run from
    # inside Oshioki.app. Its inner binary is a copy of the flat
    # oshioki-agent from the same build, ad hoc signed, and hashed in
    # SHA256SUMS under Oshioki.app/Contents/MacOS/oshioki-agent; setup
    # verifies it against that entry before preferring it. Release archives
    # from before the bundle was packaged have no Oshioki.app and no such
    # entry, and setup keeps the flat binary for those.
    prefix.install "Oshioki.app" if File.exist?("Oshioki.app")
    libexec.install "oshioki.dylib", "SHA256SUMS", "manifest.json"
    # Older release archives predate the contextual PAM module.
    libexec.install "liboshioki_pam.dylib" if File.exist?("liboshioki_pam.dylib")
  end

  def caveats
    <<~EOS
      The sudo plugin and hook state live outside the Cellar and need root.
      Run setup as yourself, not under sudo; it elevates once by itself and
      finds this keg's binaries, module and SHA256SUMS on its own:
        oshioki-laptop-setup
      From releases that include Oshioki.app the keg carries it and setup runs
      the agent from inside it, so the Touch ID sheet shows the Oshioki name
      and icon.
      Add --contextual-pam to authenticate sudo through PAM instead of the
      approval plugin. Keep a second root shell open while that runs.
      To drive the installer by hand instead, create /etc/oshioki/install.env
      (0600, root-owned;
      see https://github.com/epsalmond/oshioki/blob/main/RUNBOOK.md),
      then run, with no environment at all:
        sudo install-oshioki-hook --contextual-pam
        sudo install-oshioki-hook --prelaunch \\
          --config-file /etc/oshioki/install.env
      From 0.1.10 the installer resolves this keg through the bin symlink and
      finds the hook, the sudo plugin, the PAM module and SHA256SUMS on its
      own, so HOOK_BIN, PLUGIN_BIN, PAM_MODULE_BIN and OSHIOKI_CHECKSUMS are
      no longer needed.
      The Mac agent's own NATS credentials are optional. Set
      OSHIOKI_AGENT_NATS_URL, OSHIOKI_AGENT_NATS_USER and
      OSHIOKI_AGENT_NATS_PASS (in install.env, or in the environment for one
      run) so approvals raised on other hosts reach this Mac. Left unset, the
      agent is socket-only: it only answers sudo started on this machine.
      To check what is live against this keg's SHA256SUMS:
        sudo install-oshioki-hook --contextual-pam-status
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
