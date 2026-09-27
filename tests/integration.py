"""Disposable hosted-Mac proof using production formula, label and sandbox.

The signed sleeping executable prevents identity or Keychain access. It is
packaged in two real Homebrew kegs and a bottle, using the production formula's
install/post-install code. Never run this harness on a personal computer.
"""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from refresh_source import load


def run(args, env=None, success=True):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env, timeout=300)
    # The only secret-looking value is synthetic and must never enter logs.
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in result.stdout, "private field leaked"
    if success and result.returncode:
        print(result.stdout)
        raise AssertionError(f"command failed: {args[0:2]}")
    return result


def wait_job(native, running=True):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = native.read_job()
        if job and bool(job["pid"] and job["pid"] > 0) == running:
            return job
        time.sleep(0.1)
    raise AssertionError("fixture service did not reach expected state")


def main():
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
    assert sys.platform == "darwin" and os.getuid() != 0
    module = load()
    native = module.Native()
    uid = os.getuid()
    assert native.context(uid), "runner must provide the current user's Aqua session"
    assert native.read_job() is None, "refuse to replace an existing production label"
    assert not native.disabled(uid), "refuse to replace an existing disabled override"
    target = f"gui/{uid}/{module.LABEL}"
    user_target = f"user/{uid}/{module.LABEL}"
    tap = Path(run(["brew", "--repository"]).stdout.strip()) / "Library/Taps/fixture/homebrew-refresh"
    assert not tap.exists()
    with tempfile.TemporaryDirectory(prefix="oshioki-refresh-ci-") as temp:
        root = Path(temp)
        home = root / "home"
        home.mkdir()
        sentinel = home / "identity-state"
        sentinel.write_text("SYNTHETIC_PRIVATE_SENTINEL")
        sentinel.chmod(0o600)
        env = os.environ.copy()
        env.update(HOME=str(home), HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_INSTALL_FROM_API="1",
                   HOMEBREW_NO_INSTALL_CLEANUP="1", HOMEBREW_DEVELOPER="1")
        prefix = Path(run(["brew", "--prefix"]).stdout.strip())
        opt = prefix / "opt/oshioki"
        assert not opt.exists(), "runner already has Oshioki installed"
        formula = tap / "Formula/oshioki.rb"
        formula.parent.mkdir(parents=True)
        source = (Path(__file__).resolve().parents[1] / "Formula/oshioki.rb").read_text()
        # Strip only versioned release data. Keep actual install and all
        # declarative post-install steps verbatim from this PR.
        source = re.sub(r"  bottle do.*?^  end\n", "", source, flags=re.M | re.S)
        source = re.sub(r'^  url .*\n|^  sha256 .*\n|^  version .*\n', "", source, flags=re.M)
        helper_start = source.index("  post_install_steps do\n")
        helper_end = source.index("  def caveats\n", helper_start)
        legacy_source = source[:helper_start] + source[helper_end:]
        fixture_c = root / "agent.c"
        fixture_c.write_text("#include <unistd.h>\nint main(void) { for (;;) pause(); }\n")
        agent = root / "sleeping-agent"
        run(["clang", "-o", str(agent), str(fixture_c)])
        plist = root / "com.oshioki.agent.plist"
        contents = {"Label": module.LABEL,
                    "ProgramArguments": [str(opt / "Oshioki.app/Contents/MacOS/oshioki-agent")],
                    "RunAtLoad": True,
                    "EnvironmentVariables": {"HOME": str(home),
                                             "OSHIOKI_AGENT_NATS_PASS": "SYNTHETIC_PRIVATE_SENTINEL"}}
        plist.write_bytes(plistlib.dumps(contents))
        plist.chmod(0o600)
        plist_before = plist.read_bytes()
        state_before = sentinel.read_bytes()

        def write_formula(version, legacy=False):
            stage = root / version
            stage.mkdir()
            app = stage / "Oshioki.app/Contents"
            (app / "MacOS").mkdir(parents=True)
            (app / "Resources").mkdir()
            shutil.copy2(agent, app / "MacOS/oshioki-agent")
            info = {"CFBundleIdentifier": "dev.oshioki.agent", "CFBundleExecutable": "oshioki-agent",
                    "CFBundleIconFile": "Oshioki", "CFBundlePackageType": "APPL",
                    "CFBundleVersion": version, "CFBundleShortVersionString": version}
            (app / "Info.plist").write_bytes(plistlib.dumps(info))
            (app / "Resources/Oshioki.icns").write_bytes(b"icns\0\0\0\x0ctest")
            run(["/usr/bin/codesign", "--force", "--sign", "-", str(stage / "Oshioki.app")])
            for name in ("oshioki", "oshioki-agent", "install-oshioki-hook", "oshioki-laptop-setup",
                         "oshioki-browser-relay"):
                shutil.copy2(agent, stage / name)
            for name in ("oshioki.dylib", "SHA256SUMS", "manifest.json"):
                (stage / name).write_text("{}" if name == "manifest.json" else "fixture")
            archive = root / f"oshioki-{version}.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for path in stage.iterdir():
                    tar.add(path, arcname=path.name)
            data = legacy_source if legacy else source
            data = data.replace('  desc "',
                                f'  url "{archive.as_uri()}"\n'
                                f'  sha256 "{hashlib.sha256(archive.read_bytes()).hexdigest()}"\n'
                                f'  version "{version}"\n  desc "', 1)
            formula.write_text(data)

        def brew(*args):
            return run(["brew", *args], env=env)

        def preserved():
            assert plist.read_bytes() == plist_before
            assert sentinel.read_bytes() == state_before
            assert sentinel.stat().st_mode & 0o777 == 0o600

        def refreshed(previous, result):
            job = wait_job(native)
            assert job["pid"] != previous["pid"]
            assert job["program"] == previous["program"]
            assert native.executable(job["pid"]) == (opt / "Oshioki.app/Contents/MacOS/oshioki-agent").resolve()
            assert "Oshioki agent refreshed" in result.stdout, result.stdout
            assert "Retry any approval" in result.stdout
            preserved()
            return job

        try:
            write_formula("0.1.13", legacy=True)
            brew("install", "--build-from-source", "fixture/refresh/oshioki")
            run(["/bin/launchctl", "bootstrap", f"gui/{uid}", str(plist)])
            old = wait_job(native)
            old_path = native.executable(old["pid"])
            assert old_path.exists()
            # Remove the installed old package while its process is alive.
            # This is the historical failure: loaded Program remains stable opt.
            brew("uninstall", "--force", "fixture/refresh/oshioki")
            assert not old_path.exists()
            assert native.read_job()["pid"] == old["pid"]
            write_formula("0.3.1")
            result = brew("install", "--build-bottle", "fixture/refresh/oshioki")
            current = refreshed(old, result)
            # Homebrew's postinstall temporary HOME must not influence lookup.
            assert not (home / "Library/LaunchAgents").exists()
            # Build a real bottle and pour it using the actual stored formula.
            bottle_result = run(["brew", "bottle", "--skip-relocation", "--json",
                                 "--root-url=" + root.as_uri(), "fixture/refresh/oshioki"], env=env)
            print("Signed old package removal and sandboxed build postinstall passed")
            bottle_paths = list(Path.cwd().glob("oshioki--*.bottle*.tar.gz"))
            assert len(bottle_paths) == 1
            bottle = bottle_paths[0]
            brew("uninstall", "--force", "fixture/refresh/oshioki")
            brew("trust", "--formula", "fixture/refresh/oshioki")
            result = brew("install", str(bottle.resolve()))
            current = refreshed(current, result)
            print("Bottle installation refreshed the production label")
            # Add generated bottle metadata to the tap formula for a normal
            # force-bottle reinstall; canonical URL has one dash.
            json_paths = list(Path.cwd().glob("oshioki--*.bottle*.json"))
            assert len(json_paths) == 1
            entry = next(iter(json.loads(json_paths[0].read_text()).values()))["bottle"]
            tags = entry.get("tags") or entry["files"]
            canonical = root / bottle.name.replace("oshioki--", "oshioki-", 1)
            shutil.copy2(bottle, canonical)
            lines = ["  bottle do", f'    root_url "{root.as_uri()}"']
            if entry.get("rebuild"):
                lines.append(f'    rebuild {entry["rebuild"]}')
            for tag, details in tags.items():
                lines.append(f'    sha256 cellar: :any_skip_relocation, {tag}: "{details["sha256"]}"')
            lines.append("  end")
            formula.write_text(formula.read_text().replace("  def install\n", "\n".join(lines) + "\n\n  def install\n"))
            result = brew("reinstall", "--force-bottle", "fixture/refresh/oshioki")
            current = refreshed(current, result)
            print("Bottle reinstall refreshed the production label")
            # No service: another bottle reinstall may write only keg helper.
            run(["/bin/launchctl", "bootout", target])
            result = brew("reinstall", "--force-bottle", "fixture/refresh/oshioki")
            assert native.read_job() is None
            assert "Oshioki agent refreshed" not in result.stdout
            preserved()
            print("No-service bottle reinstall was side-effect free")
            # A loaded stopped service must remain stopped.
            stopped = dict(contents, RunAtLoad=False)
            plist.write_bytes(plistlib.dumps(stopped))
            plist_before = plist.read_bytes()
            run(["/bin/launchctl", "bootstrap", f"gui/{uid}", str(plist)])
            wait_job(native, running=False)
            brew("postinstall", "fixture/refresh/oshioki")
            assert not native.read_job()["pid"]
            run(["/bin/launchctl", "bootout", target])
            # A custom program in GUI cannot be refreshed; no private config
            # output is logged, and actionable setup guidance stays visible.
            custom = root / "custom-agent"
            shutil.copy2(agent, custom)
            custom_job = dict(contents, ProgramArguments=[str(custom)])
            plist.write_bytes(plistlib.dumps(custom_job))
            plist_before = plist.read_bytes()
            run(["/bin/launchctl", "bootstrap", f"gui/{uid}", str(plist)])
            custom_before = wait_job(native)
            result = brew("postinstall", "fixture/refresh/oshioki")
            assert native.read_job() == custom_before
            assert "oshioki-laptop-setup" in result.stdout
            preserved()
            # Same production label in user domain must not authorize touching
            # the GUI custom job. asuser adopts the background manager.
            background_plist = root / "background.plist"
            background_plist.write_bytes(plistlib.dumps(contents))
            run(["/bin/launchctl", "bootstrap", f"user/{uid}", str(background_plist)])
            helper = opt / "libexec/refresh-agent.py"
            result = run(["/bin/launchctl", "asuser", str(uid), sys.executable,
                          str(helper), str(opt), "0.3.1"])
            assert "outside this user's GUI login session" in result.stdout
            assert native.read_job() == custom_before
            preserved()
            print("Stopped, custom and same-label background services remained unchanged")
        finally:
            run(["/bin/launchctl", "bootout", target], success=False)
            run(["/bin/launchctl", "bootout", user_target], success=False)
            run(["brew", "uninstall", "--force", "fixture/refresh/oshioki"], env=env, success=False)
            shutil.rmtree(tap)


if __name__ == "__main__":
    main()
