import copy
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from refresh_source import load

refresh = load()


class Fake:
    def __init__(self, opt):
        self.opt = opt
        self.loaded = {"program": str(opt / "Oshioki.app/Contents/MacOS/oshioki-agent"),
                       "pid": 100, "disabled": False}
        self.initial = copy.deepcopy(self.loaded)
        self.calls = []
        self.background = self.override = self.bad_signature = self.failed = self.timeout = False
        self.path = opt.resolve() / "Oshioki.app/Contents/MacOS/oshioki-agent"
        self.seconds = 0
        # Represents private state outside the helper's API: any access fails.
        self.preserved = {"configuration": b"unchanged", "identity": b"SECRET_IDENTITY",
                          "enrollment": b"SECRET_ENROLLMENT", "credentials": b"SECRET_CREDENTIAL"}

    def context(self, uid):
        self.calls.append("context")
        return not self.background

    def job(self, timeout=5):
        self.calls.append("job")
        return copy.deepcopy(self.loaded)

    def disabled(self, uid):
        self.calls.append("disabled")
        return self.override

    def icon(self, path):
        self.calls.append("icon")

    def signature(self, bundle):
        self.calls.append("signature")
        if self.bad_signature:
            raise refresh.RefreshError("new agent bundle signature is invalid")

    def restart(self, target, timeout):
        self.calls.append(("restart", target, timeout))
        if self.failed:
            raise refresh.RefreshError("agent restart failed")
        if not self.timeout:
            self.loaded["pid"] = 101

    def executable(self, pid):
        self.calls.append("executable")
        return self.path

    def clock(self):
        return self.seconds

    def sleep(self, duration):
        self.seconds += duration


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        keg = root / "Cellar/oshioki/0.3.1"
        self.opt = root / "opt/oshioki"
        self.opt.parent.mkdir()
        self.opt.symlink_to(keg, target_is_directory=True)
        app = keg / "Oshioki.app/Contents"
        (app / "MacOS").mkdir(parents=True)
        (app / "Resources").mkdir()
        exe = app / "MacOS/oshioki-agent"
        exe.write_bytes(b"test fixture")
        exe.chmod(0o755)
        self.info = {"CFBundleIdentifier": "dev.oshioki.agent", "CFBundleExecutable": "oshioki-agent",
                     "CFBundleIconFile": "Oshioki", "CFBundlePackageType": "APPL", "CFBundleVersion": "0.3.1",
                     "CFBundleShortVersionString": "0.3.1"}
        self.plist = app / "Info.plist"
        self.write_info()
        self.icon = app / "Resources/Oshioki.icns"
        self.icon.write_bytes(b"icns\0\0\0\x0ctest")
        self.fake = Fake(self.opt)
        self.logs = []
        self.preserved = copy.deepcopy(self.fake.preserved)

    def write_info(self):
        self.plist.write_bytes(plistlib.dumps(self.info))

    def run_refresh(self):
        result = refresh.refresh(self.opt, "0.3.1", self.fake, uid=501,
                                 clock=self.fake.clock, sleep=self.fake.sleep, emit=self.logs.append)
        self.assertEqual(self.fake.preserved, self.preserved)
        self.assertNotIn("SECRET", "\n".join(self.logs))
        return result

    def restarts(self):
        return [call for call in self.fake.calls if isinstance(call, tuple)]

    def test_stale_and_current_processes_replace_pid(self):
        for stale in (True, False):
            with self.subTest(stale=stale):
                self.fake = Fake(self.opt)
                if stale:
                    self.fake.path = Path("/removed/old/Oshioki.app/Contents/MacOS/oshioki-agent")
                    original = self.fake.restart
                    def restart(*args):
                        original(*args)
                        self.fake.path = self.opt.resolve() / "Oshioki.app/Contents/MacOS/oshioki-agent"
                    self.fake.restart = restart
                self.assertEqual(self.run_refresh(), 0)
                self.assertEqual(self.fake.loaded["pid"], 101)
                self.assertEqual(self.restarts()[0][1], "gui/501/com.oshioki.agent")
                self.assertEqual(self.fake.loaded["program"], self.fake.initial["program"])
                self.assertLess(self.fake.calls.index("signature"), self.fake.calls.index(self.restarts()[0]))

    def test_absent_stopped_disabled_custom_and_background_untouched(self):
        for case in ("absent", "stopped", "negative_pid", "disabled", "override", "custom", "background"):
            with self.subTest(case=case):
                self.fake = Fake(self.opt)
                if case == "absent": self.fake.loaded = None
                if case == "stopped": self.fake.loaded["pid"] = None
                if case == "negative_pid": self.fake.loaded["pid"] = -1
                if case == "disabled": self.fake.loaded["disabled"] = True
                if case == "override": self.fake.override = True
                if case == "custom": self.fake.loaded["program"] = "/custom/agent"
                if case == "background": self.fake.background = True
                initial = copy.deepcopy(self.fake.loaded)
                self.assertEqual(self.run_refresh(), 0)
                self.assertEqual(self.fake.loaded, initial)
                self.assertFalse(self.restarts())
                self.assertNotIn("signature", self.fake.calls)
                if case == "custom": self.assertIn("oshioki-laptop-setup", self.logs[-1])
                if case == "background": self.assertNotIn("job", self.fake.calls)

    def test_invalid_bundle_never_restarts(self):
        for key in self.info:
            original = self.info[key]
            self.info[key] = "invalid"
            self.write_info()
            self.assertEqual(self.run_refresh(), 1)
            self.assertFalse(self.restarts())
            self.info[key] = original
        self.write_info()
        self.icon.unlink()
        self.assertEqual(self.run_refresh(), 1)
        self.assertFalse(self.restarts())

    def test_malformed_plist_root_never_restarts(self):
        self.plist.write_bytes(plistlib.dumps(["malformed"]))
        self.assertEqual(self.run_refresh(), 1)
        self.assertFalse(self.restarts())

    def test_native_requires_aqua_and_current_uid(self):
        native = refresh.Native.__new__(refresh.Native)
        for name, uid, expected in (("Aqua", "501", True), ("Background", "501", False),
                                    ("System", "0", False), ("Aqua", "502", False)):
            def command(args, timeout=5):
                return name if args[-1] == "managername" else uid
            native.command = command
            self.assertEqual(native.context(501), expected)

    def service_dump(self, state="running", pid=100):
        target = "gui/501/com.oshioki.agent"
        output = (target + " = {\n\tactive count = 1\n\tpath = /fixture/agent.plist\n"
                  "\ttype = LaunchAgent\n\tstate = " + state + "\n\n"
                  "\tprogram = /stable/agent\n\targuments = {\n\t\t/stable/agent\n\t}\n"
                  "\tenvironment = {\n\t\tNATS_PASS = SYNTHETIC_PRIVATE_SENTINEL\n\t}\n")
        if pid is not None:
            output += f"\tpid = {pid}\n"
        return target, output + "}\n"

    def test_service_parser_retains_only_metadata(self):
        target, output = self.service_dump()
        metadata = refresh.parse_service(output, target)
        self.assertEqual(metadata, {"program": "/stable/agent", "state": "running", "pid": 100})
        self.assertNotIn("PRIVATE", str(metadata))
        target, output = self.service_dump(state="waiting", pid=None)
        self.assertIsNone(refresh.parse_service(output, target)["pid"])

    def test_service_parser_rejects_forged_environment_or_argument_metadata(self):
        target, output = self.service_dump()
        for field in ("program = /evil/agent", "state = running", "pid = 999"):
            for insertion in ("\t\tNATS_PASS =", "\t\t/stable/agent"):
                forged = output.replace(insertion, insertion + "\n}\n\t" + field)
                with self.assertRaises(refresh.RefreshError) as error:
                    refresh.parse_service(forged, target)
                self.assertNotIn("PRIVATE", str(error.exception))

    def test_service_parser_rejects_malformed_truncated_and_wrong_domain(self):
        target, output = self.service_dump()
        for malformed in (output[:-2], output.replace(target, "gui/502/com.oshioki.agent", 1),
                          output.replace("\tprogram = /stable/agent\n", ""),
                          output.replace("\tstate = running\n", ""),
                          output.replace("\tpid = 100", "\tpid = -1"),
                          output.replace("\tpid = 100", "\tpid = 2147483648"),
                          output.replace("\tpid = 100", "\t\tpid = 100"),
                          output.replace("\tpid = 100\n", "")):
            with self.assertRaises(refresh.RefreshError):
                refresh.parse_service(malformed, target)
        # A program line appearing only inside an environment cannot qualify.
        hidden = output.replace("\tprogram = /stable/agent\n", "").replace(
            "\t\tNATS_PASS =", "\tprogram = /stable/agent\n\t\tNATS_PASS =")
        with self.assertRaises(refresh.RefreshError):
            refresh.parse_service(hidden, target)

    def test_native_explicit_gui_print_and_missing_service(self):
        native = refresh.Native.__new__(refresh.Native)
        target, output = self.service_dump()
        calls = []
        def result(args, timeout=5):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout=output.encode())
        native.result = result
        with patch.object(refresh.os, "getuid", return_value=501):
            self.assertEqual(native.job(), {"program": "/stable/agent", "pid": 100, "disabled": False})
            self.assertEqual(calls, [["/bin/launchctl", "print", target]])
            native.result = lambda args, timeout=5: SimpleNamespace(returncode=113, stdout=b"PRIVATE")
            self.assertIsNone(native.job())
            native.result = lambda args, timeout=5: SimpleNamespace(returncode=1, stdout=b"PRIVATE")
            with self.assertRaises(refresh.RefreshError) as error:
                native.job()
            self.assertNotIn("PRIVATE", str(error.exception))

    def test_native_rejects_undecodable_icon_even_when_sips_exits_zero(self):
        native = refresh.Native.__new__(refresh.Native)
        class Result:
            returncode = 0
            stdout = b"pixelWidth: <nil>\npixelHeight: <nil>\n"
        with patch.object(refresh.subprocess, "run", return_value=Result()):
            with self.assertRaises(refresh.RefreshError):
                native.icon(self.icon)

    def test_missing_executable_and_bad_signature(self):
        exe = self.opt / "Oshioki.app/Contents/MacOS/oshioki-agent"
        exe.chmod(0o644)
        self.assertEqual(self.run_refresh(), 1)
        exe.chmod(0o755)
        self.fake.bad_signature = True
        self.assertEqual(self.run_refresh(), 1)
        self.assertFalse(self.restarts())

    def test_restart_failure_and_timeout_visible(self):
        for case in ("failed", "timeout", "wrong_path"):
            self.fake = Fake(self.opt)
            if case == "wrong_path": self.fake.path = Path("/old/agent")
            else: setattr(self.fake, case, True)
            self.assertEqual(self.run_refresh(), 1)
            self.assertIn("brew postinstall epsalmond/oshioki/oshioki", self.logs[-1])
            self.assertLessEqual(self.fake.seconds, 10)

    def test_late_metadata_never_reports_success(self):
        original = self.fake.job
        def job(timeout=5):
            if self.fake.loaded["pid"] == 101:
                self.fake.seconds = 11
            return original(timeout)
        self.fake.job = job
        self.assertEqual(self.run_refresh(), 1)
        self.assertNotIn("refreshed to", self.logs[-1])

    def test_external_exception_never_logged(self):
        def signature(bundle):
            raise OSError("SECRET_CREDENTIAL")
        self.fake.signature = signature
        self.assertEqual(self.run_refresh(), 1)
        self.assertFalse(self.restarts())

    def test_reconfiguration_during_validation_untouched(self):
        def signature(bundle):
            self.fake.loaded["program"] = "/custom/agent"
        self.fake.signature = signature
        self.assertEqual(self.run_refresh(), 0)
        self.assertFalse(self.restarts())
        self.assertEqual(self.fake.loaded["program"], "/custom/agent")


if __name__ == "__main__":
    unittest.main()
