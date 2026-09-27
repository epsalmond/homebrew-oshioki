require "json"

class Oshioki < Formula
  desc "Touch ID or WebAuthn approval for sudo requests"
  homepage "https://github.com/epsalmond/oshioki"
  # URL, sha256, and version are filled in by the bottle workflow the first
  # time a release is bottled. Do not hand-edit them.
  url "https://github.com/epsalmond/oshioki/releases/download/v0.3.0/oshioki-macos-arm64-0.3.0.tar.gz"
  sha256 "8362b50dd32b6e3db501221fec7997252cc09f907bd63322b9b3abedc77cb8d5"

  bottle do
    root_url "https://github.com/epsalmond/oshioki/releases/download/v0.3.0"
    rebuild 16
    sha256 cellar: :any_skip_relocation, arm64_sonoma: "59113f38f674a9d8838a08995c4c33e59486127b954dc84c2204bcd9b72ed0c7"
  end
  version "0.3.0"
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
      import errno
      import json
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

      class Native:
          """Read selected loaded-job metadata; never traverse its environment."""
          def __init__(self):
              self.lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
              signatures = {
                  "launch_data_alloc": (ctypes.c_void_p, [ctypes.c_int]),
                  "launch_data_new_string": (ctypes.c_void_p, [ctypes.c_char_p]),
                  "launch_data_dict_insert": (ctypes.c_bool, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]),
                  "launch_msg": (ctypes.c_void_p, [ctypes.c_void_p]),
                  "launch_data_get_type": (ctypes.c_int, [ctypes.c_void_p]),
                  "launch_data_get_errno": (ctypes.c_int, [ctypes.c_void_p]),
                  "launch_data_dict_lookup": (ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_char_p]),
                  "launch_data_get_string": (ctypes.c_char_p, [ctypes.c_void_p]),
                  "launch_data_get_integer": (ctypes.c_longlong, [ctypes.c_void_p]),
                  "launch_data_get_bool": (ctypes.c_bool, [ctypes.c_void_p]),
                  "launch_data_array_get_index": (ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_size_t]),
                  "launch_data_array_get_count": (ctypes.c_size_t, [ctypes.c_void_p]),
                  "launch_data_free": (None, [ctypes.c_void_p]),
                  "proc_pidpath": (ctypes.c_int, [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]),
              }
              for name, (result, args) in signatures.items():
                  function = getattr(self.lib, name)
                  function.restype, function.argtypes = result, args

          def command(self, args, timeout=5):
              # Only commands that return selected non-secret metadata use PIPE.
              try:
                  result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          timeout=timeout, check=False)
              except OSError as error:
                  raise RefreshError("launchd metadata is unavailable", args[1], error.errno) from None
              except subprocess.TimeoutExpired:
                  raise RefreshError("launchd metadata is unavailable", args[1], "timeout") from None
              if result.returncode:
                  raise RefreshError("launchd metadata is unavailable", args[1], result.returncode)
              return result.stdout.decode("utf-8", "strict").strip()

          def context(self, uid):
              return (self.command(["/bin/launchctl", "managername"]) == "Aqua"
                      and self.command(["/bin/launchctl", "manageruid"]) == str(uid))

          def disabled(self, uid):
              output = self.command(["/bin/launchctl", "print-disabled", f"gui/{uid}"])
              # This command returns only the override table, never job dictionaries.
              return bool(re.search(r'"com\.oshioki\.agent"\s*=>\s*true', output))

          def job(self, timeout=5):
              # Bound the deprecated synchronous launchd API in a child. Only
              # the selected metadata crosses this pipe; no job dictionary is
              # serialized. A wedged launchd cannot extend verification.
              try:
                  result = subprocess.run([sys.executable, __file__, "--job"],
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          timeout=timeout, check=False)
                  decoded = json.loads(result.stdout) if result.stdout else None
                  if result.returncode:
                      stages = {"allocate_request", "allocate_label", "insert_request", "launch_msg",
                                "response_errno", "response_type", "Program", "ProgramArguments",
                                "PID", "Disabled", "argument_type", "load_library"}
                      if isinstance(decoded, dict) and decoded.get("stage") in stages:
                          code = decoded.get("code")
                          if code is None or isinstance(code, int):
                              raise RefreshError("launchd metadata is unavailable", decoded["stage"], code)
                      raise RefreshError("launchd metadata is unavailable", "job_child", result.returncode)
                  return decoded
              except OSError as error:
                  raise RefreshError("launchd metadata is unavailable", "job_child", error.errno) from None
              except (ValueError, subprocess.TimeoutExpired):
                  raise RefreshError("launchd metadata is unavailable", "job_child", "timeout_or_invalid_json") from None

          def read_job(self):
              lib = self.lib
              request = lib.launch_data_alloc(1)
              if not request:
                  raise RefreshError("launchd metadata is unavailable", "allocate_request", ctypes.get_errno())
              response = None
              try:
                  value = lib.launch_data_new_string(LABEL.encode())
                  if not value:
                      raise RefreshError("launchd metadata is unavailable", "allocate_label", ctypes.get_errno())
                  if not lib.launch_data_dict_insert(request, value, b"GetJob"):
                      lib.launch_data_free(value)
                      raise RefreshError("launchd metadata is unavailable", "insert_request", ctypes.get_errno())
                  ctypes.set_errno(0)
                  response = lib.launch_msg(request)
                  if not response:
                      raise RefreshError("launchd metadata is unavailable", "launch_msg", ctypes.get_errno())
                  kind = lib.launch_data_get_type(response)
                  if kind == 9:
                      response_errno = lib.launch_data_get_errno(response)
                      if response_errno == errno.ESRCH:
                          return None
                      raise RefreshError("launchd metadata is unavailable", "response_errno", response_errno)
                  if kind != 1:
                      raise RefreshError("launchd metadata is unavailable", "response_type", kind)
                  def field(name, kind):
                      item = lib.launch_data_dict_lookup(response, name)
                      if item and lib.launch_data_get_type(item) != kind:
                          raise RefreshError("launchd metadata is malformed", name.decode("ascii"), lib.launch_data_get_type(item))
                      return item
                  program = field(b"Program", 7)
                  if not program:
                      arguments = field(b"ProgramArguments", 2)
                      if arguments and lib.launch_data_array_get_count(arguments):
                          program = lib.launch_data_array_get_index(arguments, 0)
                          if lib.launch_data_get_type(program) != 7:
                              raise RefreshError("launchd metadata is malformed", "argument_type", lib.launch_data_get_type(program))
                  pid = field(b"PID", 4)
                  disabled = field(b"Disabled", 6)
                  return {"program": os.fsdecode(lib.launch_data_get_string(program)) if program else None,
                          "pid": lib.launch_data_get_integer(pid) if pid else None,
                          "disabled": bool(disabled and lib.launch_data_get_bool(disabled))}
              finally:
                  if response:
                      lib.launch_data_free(response)
                  lib.launch_data_free(request)

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
          if len(sys.argv) == 2 and sys.argv[1] == "--job" and sys.platform == "darwin":
              try:
                  print(json.dumps(Native().read_job()))
                  return 0
              except RefreshError as error:
                  print(json.dumps({"stage": error.stage, "code": error.code}))
                  return 1
              except (OSError, AttributeError):
                  print(json.dumps({"stage": "load_library", "code": None}))
                  return 1
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
