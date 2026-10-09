require "json"

class Oshioki < Formula
  desc "Touch ID or WebAuthn approval for sudo requests"
  homepage "https://github.com/epsalmond/oshioki"
  # URL, sha256, and version are filled in by the bottle workflow the first
  # time a release is bottled. Do not hand-edit them.
  url "https://github.com/epsalmond/oshioki/releases/download/v0.4.2/oshioki-macos-arm64-0.4.2.tar.gz"
  sha256 "c6f2ce904c93f1aad544a873fb99e56d7135523cf0c85a428d27b3d26ba0139f"

  bottle do
    root_url "https://github.com/epsalmond/oshioki/releases/download/v0.4.2"
    sha256 cellar: :any_skip_relocation, arm64_sonoma: "92767eb67281b7e6a36b23b30a6478fc45118b1d5d9fb1ce57a111c95053637e"
  end


  version "0.4.2"
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
    # Keep older releases installable; releases after 0.1.15 must ship the helper.
    if version > Version.new("0.1.15") || File.exist?("oshioki-browser-relay")
      bin.install "oshioki-browser-relay"
    end
    # Older release archives predate phone setup. The next release includes
    # both files, so keep this formula usable while the release is prepared.
    %w[oshioki-server oshioki-phone-setup].each do |name|
      bin.install name if File.exist?(name)
    end
    %w[oshioki-google-login-setup oshioki-browser-service].each do |name|
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

  # The helper is serialized with the formula, so a normal remote bottle
  # upgrade receives this refresh logic even when the old bottle has no helper.
  # Homebrew supplies a temporary HOME; loaded launchd metadata is authoritative.
  post_install_steps do
    write_file "refresh-agent.py", <<~'PYTHON', base: :libexec
      import ctypes
      import os
      from pathlib import Path
      import plistlib
      import re
      import subprocess
      import sys
      import time

      LABEL = "com.oshioki.agent"

      class RefreshError(Exception):
          def __init__(self, message, stage=None, code=None):
              super().__init__(message)
              self.stage, self.code = stage, code

          def __str__(self):
              message = super().__str__()
              return message if self.stage is None else f"{message} (stage={self.stage}, code={self.code})"

      def parse_service(output, target):
          """Keep only unambiguous scalar metadata; never return raw job output."""
          lines = output.splitlines()
          if not lines or lines[0] != target + " = {" or lines[-1] != "}":
              raise RefreshError("launchd service metadata is malformed")
          critical = {name: [] for name in ("program", "state", "pid")}
          for line in lines[1:-1]:
              match = re.fullmatch(r"\t(program|state|pid) = (.*)", line)
              if match:
                  critical[match[1]].append(match[2])
          if any(len(values) > 1 for values in critical.values()):
              # Multiline argument/environment values must never impersonate a
              # program, state or PID. Ambiguous dumps are left untouched.
              raise RefreshError("launchd service metadata is ambiguous")
          header = {}
          for line in lines[1:]:
              if re.fullmatch(r"\t[^\t]+ = \{", line):
                  break
              match = re.fullmatch(r"\t(program|state) = (.*)", line)
              if match:
                  header[match[1]] = match[2]
          if set(header) != {"program", "state"} or any(critical[name] != [header[name]] for name in header):
              raise RefreshError("launchd service metadata is malformed")
          pid = None
          if critical["pid"]:
              if not re.fullmatch(r"[1-9][0-9]*", critical["pid"][0]):
                  raise RefreshError("launchd service PID is malformed")
              top_level = re.findall(r"^\tpid = ([1-9][0-9]*)$", output, flags=re.M)
              if top_level != critical["pid"]:
                  raise RefreshError("launchd service PID is ambiguous")
              pid = int(critical["pid"][0])
              if pid > 2147483647:
                  raise RefreshError("launchd service PID is malformed")
          if header["state"] == "running" and pid is None:
              raise RefreshError("running launchd service has no PID")
          return {"program": header["program"], "state": header["state"], "pid": pid}


      def parse_disabled(output):
          lines = output.strip().splitlines()
          if not lines or lines[0] != "disabled services = {" or lines[-1].strip() != "}":
              raise RefreshError("launchd disabled-service metadata is malformed")
          found = []
          values = {"disabled": True, "enabled": False, "true": True, "false": False}
          for line in lines[1:-1]:
              if not line.strip():
                  continue
              match = re.fullmatch(r'\s*"([^"\r\n]+)"\s*=>\s*([a-z]+)\s*,?', line)
              if not match:
                  raise RefreshError("launchd disabled-service metadata is malformed")
              if match[1] == LABEL:
                  if match[2] not in values:
                      raise RefreshError("launchd disabled-service state is unknown")
                  found.append(values[match[2]])
          if len(found) > 1:
              raise RefreshError("launchd disabled-service metadata is ambiguous")
          return found[0] if found else False


      class Native:
          def __init__(self):
              self.lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
              self.lib.proc_pidpath.restype = ctypes.c_int
              self.lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]

          def result(self, args, timeout=5):
              try:
                  return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        timeout=timeout, check=False)
              except OSError as error:
                  raise RefreshError("launchd metadata is unavailable", args[1], error.errno) from None
              except subprocess.TimeoutExpired:
                  raise RefreshError("launchd metadata is unavailable", args[1], "timeout") from None

          def command(self, args, timeout=5):
              result = self.result(args, timeout)
              if result.returncode:
                  raise RefreshError("launchd metadata is unavailable", args[1], result.returncode)
              return result.stdout.decode("utf-8", "strict").strip()

          def context(self, uid):
              # The service query explicitly selects this user's GUI domain.
              # Keep refreshes inside the GUI login session; background jobs
              # never authorize a GUI restart.
              return (self.command(["/bin/launchctl", "managername"]) == "Aqua"
                      and self.command(["/bin/launchctl", "manageruid"]) == str(uid))

          def disabled(self, uid):
              output = self.command(["/bin/launchctl", "print-disabled", f"gui/{uid}"])
              return parse_disabled(output)

          def job(self, timeout=5):
              target = f"gui/{os.getuid()}/{LABEL}"
              result = self.result(["/bin/launchctl", "print", target], timeout)
              if result.returncode == 113:
                  return None
              if result.returncode:
                  raise RefreshError("launchd metadata is unavailable", "print", result.returncode)
              # This local capture can contain credentials. Keep it private and
              # discard all fields except scalar program/state/PID metadata. Never
              # print the dump, command errors, or exception text from external tools.
              metadata = parse_service(result.stdout.decode("utf-8", "strict"), target)
              pid = metadata["pid"] if metadata["state"] == "running" else None
              return {"program": metadata["program"], "pid": pid, "disabled": False}

          def executable(self, pid):
              buffer = ctypes.create_string_buffer(4096)
              if self.lib.proc_pidpath(pid, buffer, len(buffer)) <= 0:
                  return None
              return Path(os.fsdecode(buffer.value)).resolve()

          def restart(self, target, timeout):
              try:
                  result = subprocess.run(["/bin/launchctl", "kickstart", "-k", target],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          timeout=timeout, check=False)
              except (OSError, subprocess.TimeoutExpired):
                  raise RefreshError("agent restart failed") from None
              if result.returncode:
                  raise RefreshError("agent restart failed")

          def icon(self, path):
              try:
                  result = subprocess.run(["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          timeout=5, check=False)
              except (OSError, subprocess.TimeoutExpired):
                  raise RefreshError("new agent bundle icon could not be verified") from None
              dimensions = re.findall(rb"pixel(?:Width|Height):\s*([0-9]+)", result.stdout)
              if result.returncode or len(dimensions) != 2 or any(int(value) <= 0 for value in dimensions):
                  raise RefreshError("new agent bundle icon is invalid")

          def signature(self, bundle):
              try:
                  result = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          timeout=5, check=False)
              except (OSError, subprocess.TimeoutExpired):
                  raise RefreshError("new agent bundle signature could not be verified") from None
              if result.returncode:
                  raise RefreshError("new agent bundle signature is invalid")


      def validate_bundle(opt, version, native):
          bundle = opt / "Oshioki.app"
          executable = bundle / "Contents/MacOS/oshioki-agent"
          if not executable.is_file() or not os.access(executable, os.X_OK):
              raise RefreshError("new bundled agent is missing or not executable")
          with (bundle / "Contents/Info.plist").open("rb") as stream:
              info = plistlib.load(stream)
          if not isinstance(info, dict):
              raise RefreshError("new agent bundle metadata is invalid")
          expected = {"CFBundleIdentifier": "dev.oshioki.agent", "CFBundleExecutable": "oshioki-agent",
                      "CFBundleIconFile": "Oshioki", "CFBundlePackageType": "APPL", "CFBundleVersion": version,
                      "CFBundleShortVersionString": version}
          if any(info.get(key) != value for key, value in expected.items()):
              raise RefreshError("new agent bundle identity or version is invalid")
          icon = (bundle / "Contents/Resources/Oshioki.icns").read_bytes()
          if len(icon) <= 8 or icon[:4] != b"icns" or int.from_bytes(icon[4:8], "big") != len(icon):
              raise RefreshError("new agent bundle icon is invalid")
          native.icon(bundle / "Contents/Resources/Oshioki.icns")
          native.signature(bundle)
          resolved = executable.resolve(strict=True)
          if resolved.parent != opt.resolve(strict=True) / "Oshioki.app/Contents/MacOS":
              raise RefreshError("new bundled executable leaves the current keg")
          return resolved


      def refresh(opt, version, native, uid=None, clock=time.monotonic, sleep=time.sleep, emit=None):
          uid = os.getuid() if uid is None else uid
          emit = emit or (lambda text: print(text, file=sys.stderr))
          recovery = "brew postinstall epsalmond/oshioki/oshioki"
          target = f"gui/{uid}/{LABEL}"
          try:
              # GetJob is scoped to the caller's bootstrap namespace. A background
              # or another user's manager must never authorize an Aqua job restart.
              if not native.context(uid):
                  emit("Oshioki agent refresh skipped outside this user's GUI login session. "
                       f"In a logged-in Terminal, run: {recovery}")
                  return 0
              job = native.job()
              if not job or job["disabled"] or (not job["pid"] or job["pid"] <= 0) or native.disabled(uid):
                  return 0
              expected = str(opt / "Oshioki.app/Contents/MacOS/oshioki-agent")
              if job["program"] != expected:
                  emit("Oshioki agent uses a custom executable. To update its configuration, "
                       "run your original setup command: oshioki-laptop-setup (add --local "
                       "if you originally used local mode).")
                  return 0
              current = validate_bundle(opt, version, native)
              # Recheck ownership and running state after validation; never start a
              # service which stopped or was reconfigured while the bundle was checked.
              before = native.job()
              if before != job or native.disabled(uid):
                  emit("Oshioki agent changed during validation; refresh skipped. "
                       f"Retry: {recovery}")
                  return 0
              deadline = clock() + 10
              native.restart(target, max(0.001, deadline - clock()))
              while clock() < deadline:
                  after = native.job(timeout=max(0.001, deadline - clock()))
                  if after and after["program"] == expected and not after["disabled"]:
                      pid = after["pid"]
                      if pid and pid > 0 and pid != before["pid"] and native.executable(pid) == current and clock() < deadline:
                          emit("Oshioki agent refreshed to the current bundled executable. "
                               "Retry any approval interrupted by the refresh.")
                          return 0
                  sleep(min(0.1, max(0, deadline - clock())))
              raise RefreshError("replacement agent did not reach the current bundled executable within ten seconds")
          except RefreshError as error:
              # RefreshError contains only our fixed messages, never command output.
              emit(f"Oshioki agent refresh failed: {error}. "
                   f"In a logged-in Terminal, retry: {recovery}. "
                   "For an invalid bundle, reinstall: brew reinstall epsalmond/oshioki/oshioki. "
                   "Retry any approval interrupted by the refresh.")
              return 1
          except (OSError, ValueError, plistlib.InvalidFileException):
              emit("Oshioki agent bundle could not be read or validated. "
                   "Reinstall: brew reinstall epsalmond/oshioki/oshioki. "
                   f"Then retry in a logged-in Terminal: {recovery}")
              return 1


      def main():
          if len(sys.argv) != 3 or sys.platform != "darwin":
              return 1
          try:
              native = Native()
          except (OSError, AttributeError):
              print("Oshioki agent metadata API is unavailable. Retry in a logged-in Terminal: "
                    "brew postinstall epsalmond/oshioki/oshioki", file=sys.stderr)
              return 1
          return refresh(Path(sys.argv[1]), sys.argv[2], native)


      if __name__ == "__main__":
          sys.exit(main())
    PYTHON
    run "{{HOMEBREW_PREFIX}}/opt/python@3.14/bin/python3.14",
        args: ["{{libexec}}/refresh-agent.py", "{{opt_prefix}}", "{{version}}"],
        print_stdout: true, print_stderr: true
  end

  def caveats
    <<~EOS
      The sudo plugin and hook state live outside the Cellar and need root.
      Run setup as yourself, not under sudo; it elevates once by itself and
      finds this keg's binaries, module and SHA256SUMS on its own:
        oshioki-laptop-setup
      Upgrades automatically refresh only your already running GUI Mac agent
      configured with this formula's stable opt app-bundle executable. Absent,
      stopped, disabled, and custom agents remain unchanged. Custom paths need
      your original oshioki-laptop-setup command to update their configuration.
      If refresh fails, retry from a logged-in Terminal:
        brew postinstall epsalmond/oshioki/oshioki
      Retry approvals interrupted by the refresh. The privileged hook, plugin,
      and PAM update still requires the setup command above (oshioki issue #48).
      From releases that include Oshioki.app the keg carries it and setup runs
      the agent from inside it, so the Touch ID sheet shows the Oshioki name
      and icon.
      Add --contextual-pam to authenticate sudo through PAM instead of the
      approval plugin. Keep a second root shell open while that runs.
      Releases after 0.1.15 also include oshioki-browser-relay. With Google
      Cloud CLI installed separately, approve a local login with:
        oshioki-browser-relay local-login --config ~/.config/oshioki/browser-relay/local.json
      Identity, account, and opt-in plain gcloud login setup:
        https://github.com/epsalmond/oshioki/blob/main/docs/browser-ceremony-relay.md
      Newer releases include opt-in helpers for routing exactly `gcloud auth
      login` through the relay and managing the Mac receiver. Other gcloud
      invocations pass through unchanged.
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
    if version > Version.new("0.1.15") || (bin/"oshioki-browser-relay").exist?
      relay = bin/"oshioki-browser-relay"
      assert_path_exists relay
      sums = (libexec/"SHA256SUMS").read.lines.map do |line|
        hash, name = line.split
        hash if name == "oshioki-browser-relay"
      end.compact
      assert_equal [Digest::SHA256.file(relay).hexdigest], sums
      %w[local-login login serve keygen].each do |command|
        assert_match "Usage:", shell_output("#{relay} #{command} --help")
      end
      public_key = shell_output("#{relay} keygen #{testpath}/signing.key").strip
      assert_match(/\A[A-Za-z0-9_-]{87}\z/, public_key)
      assert_equal 0600, (testpath/"signing.key").stat.mode & 0777
    end
    shipped = JSON.parse((libexec/"manifest.json").read)["files"].map { |item| item["name"] }
    %w[oshioki-google-login-setup oshioki-browser-service].each do |name|
      next unless shipped.include?(name)
      helper = bin/name
      assert_path_exists helper
      sums = (libexec/"SHA256SUMS").read.lines.filter_map do |line|
        hash, shipped_name = line.split
        hash if shipped_name == name
      end
      assert_equal [Digest::SHA256.file(helper).hexdigest], sums
      assert_match(/usage:/i, shell_output("#{helper} --help"))
    end
  end
end
