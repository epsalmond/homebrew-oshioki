import json
import contextlib
import io
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prepare_ci_homebrew import prepare


class HostedHomebrewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
                            "RUNNER_TEMP": str(self.root)}

    def fixture(self, layout, owner=None, installed=True):
        prefix = self.root / layout
        (prefix / "bin").mkdir(parents=True)
        marker = prefix / "var/homebrew/linked/openssl@1.1"
        marker.parent.mkdir(parents=True)
        executable = prefix / "bin/openssl"
        if owner:
            keg = prefix / "Cellar" / owner / "fixture-version"
            (keg / "bin").mkdir(parents=True)
            (keg / "bin/openssl").write_text("fixture binary")
            (prefix / "opt").mkdir()
            (prefix / "opt" / owner).symlink_to(keg, target_is_directory=True)
            executable.symlink_to(Path("../opt") / owner / "bin/openssl")
            if owner == "openssl@1.1":
                marker.symlink_to(keg, target_is_directory=True)
        if installed:
            (prefix / "Cellar/openssl@1.1/fixture-version").mkdir(parents=True, exist_ok=True)
        calls = []

        def brew(*args):
            calls.append(args)
            if args == ("--prefix",):
                return str(prefix)
            if args == ("list", "--versions", "openssl@1.1"):
                return "openssl@1.1 fixture-version\n" if installed else ""
            if args == ("unlink", "openssl@1.1"):
                executable.unlink()
                marker.unlink()
                return "Unlinked known fixture keg"
            raise AssertionError(f"unexpected Homebrew command: {args}")
        return prefix, executable, calls, brew

    def test_known_preinstalled_conflict_is_unlinked_and_recorded_on_both_prefixes(self):
        for layout in ("opt/homebrew", "usr/local"):
            with self.subTest(prefix=layout):
                prefix, executable, calls, brew = self.fixture(layout, "openssl@1.1")
                state = self.root / (layout.replace("/", "-") + ".json")
                prepare(state, brew=brew, environment=self.environment, platform="darwin")
                self.assertEqual([("--prefix",), ("list", "--versions", "openssl@1.1"),
                                  ("unlink", "openssl@1.1")], calls)
                self.assertFalse(os.path.lexists(executable))
                record = json.loads(state.read_text())
                self.assertEqual(str(prefix), record["prefix"])
                self.assertTrue(record["linked_before"])
                self.assertEqual(["fixture-version"], record["installed_versions"])
                self.assertEqual("../opt/openssl@1.1/bin/openssl", record["openssl_link_before"])
                self.assertEqual("unlinked-openssl@1.1", record["action"])

    def test_absent_and_installed_unlinked_formula_are_safe_noops(self):
        for installed in (False, True):
            with self.subTest(installed=installed):
                _, _, calls, brew = self.fixture(str(installed), installed=installed)
                state = self.root / f"{installed}.json"
                prepare(state, brew=brew, environment=self.environment, platform="darwin")
                self.assertNotIn(("unlink", "openssl@1.1"), calls)
                self.assertEqual("unchanged", json.loads(state.read_text())["action"])

    def test_already_linked_openssl3_is_preserved(self):
        _, executable, calls, brew = self.fixture("modern", "openssl@3")
        prepare(self.root / "modern.json", brew=brew, environment=self.environment, platform="darwin")
        self.assertTrue(executable.is_symlink())
        self.assertNotIn(("unlink", "openssl@1.1"), calls)

    def test_unexpected_owner_or_regular_file_fails_without_mutation(self):
        for owner in ("libressl", None):
            with self.subTest(owner=owner):
                _, executable, calls, brew = self.fixture(str(owner), owner)
                if owner is None:
                    executable.write_text("unknown regular file")
                with self.assertRaisesRegex(ValueError, "unexpected"):
                    prepare(self.root / f"{owner}.json", brew=brew, environment=self.environment, platform="darwin")
                self.assertTrue(os.path.lexists(executable))
                self.assertFalse(any(args[0] == "unlink" for args in calls))

    def test_stale_link_without_installed_known_formula_fails(self):
        _, executable, calls, brew = self.fixture("stale", "openssl@1.1", installed=False)
        with self.assertRaisesRegex(ValueError, "installed"):
            prepare(self.root / "stale.json", brew=brew, environment=self.environment, platform="darwin")
        self.assertTrue(executable.is_symlink())
        self.assertNotIn(("unlink", "openssl@1.1"), calls)

    def test_unexpected_linked_keg_metadata_fails_without_mutation(self):
        prefix, executable, calls, brew = self.fixture("metadata", "openssl@3")
        (prefix / "var/homebrew/linked/openssl@1.1").symlink_to(prefix / "Cellar/openssl@3/fixture-version")
        with self.assertRaisesRegex(ValueError, "unexpected linked"):
            prepare(self.root / "metadata.json", brew=brew, environment=self.environment, platform="darwin")
        self.assertTrue(executable.is_symlink())
        self.assertNotIn(("unlink", "openssl@1.1"), calls)

    def old_marker(self, prefix, version="fixture-version"):
        keg = prefix / "Cellar/openssl@1.1" / version
        (keg / "bin").mkdir(parents=True, exist_ok=True)
        marker = prefix / "var/homebrew/linked/openssl@1.1"
        if os.path.lexists(marker):
            marker.unlink()
        marker.symlink_to(keg, target_is_directory=True)

    def test_modern_canonical_with_stale_old_marker_is_unchanged(self):
        prefix, executable, calls, brew = self.fixture("modern-stale", "openssl@3")
        self.old_marker(prefix)
        original = os.readlink(executable)
        prepare(self.root / "modern-stale.json", brew=brew, environment=self.environment, platform="darwin")
        self.assertEqual(original, os.readlink(executable))
        self.assertNotIn(("unlink", "openssl@1.1"), calls)

    def test_absent_canonical_with_old_marker_is_unchanged(self):
        prefix, executable, calls, brew = self.fixture("absent-stale")
        self.old_marker(prefix)
        prepare(self.root / "absent-stale.json", brew=brew, environment=self.environment, platform="darwin")
        self.assertFalse(os.path.lexists(executable))
        self.assertNotIn(("unlink", "openssl@1.1"), calls)

    def test_unlisted_canonical_old_version_is_rejected(self):
        prefix, executable, calls, brew = self.fixture("unlisted", "openssl@1.1")
        extra = prefix / "Cellar/openssl@1.1/unlisted-version/bin/openssl"
        extra.parent.mkdir(parents=True)
        extra.write_text("unlisted fixture binary")
        executable.unlink()
        executable.symlink_to(extra)
        with self.assertRaisesRegex(ValueError, "installed|version"):
            prepare(self.root / "unlisted.json", brew=brew, environment=self.environment, platform="darwin")
        self.assertNotIn(("unlink", "openssl@1.1"), calls)

    def test_mismatched_installed_canonical_and_marker_versions_are_rejected(self):
        prefix, _, calls, original = self.fixture("mismatch", "openssl@1.1")
        self.old_marker(prefix, "another-installed-version")

        def brew(*args):
            result = original(*args)
            if args[0] == "list":
                return "openssl@1.1 fixture-version another-installed-version\n"
            return result
        with self.assertRaisesRegex(ValueError, "version|consistent"):
            prepare(self.root / "mismatch.json", brew=brew, environment=self.environment, platform="darwin")
        self.assertNotIn(("unlink", "openssl@1.1"), calls)

    def test_complete_prior_receipt_is_logged_and_written_before_failed_unlink(self):
        _, _, _, original = self.fixture("unlink-fails", "openssl@1.1")
        state = self.root / "unlink-fails.json"
        output = io.StringIO()

        def brew(*args):
            if args == ("unlink", "openssl@1.1"):
                record = json.loads(state.read_text())
                self.assertEqual("planned-unlink-openssl@1.1", record["action"])
                self.assertIn('"installed_versions": ["fixture-version"]', output.getvalue())
                self.assertIn('"openssl_link_before": "../opt/openssl@1.1/bin/openssl"', output.getvalue())
                raise RuntimeError("fixture unlink failure")
            return original(*args)
        with contextlib.redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "unlink failure"):
            prepare(state, brew=brew, environment=self.environment, platform="darwin")
        self.assertEqual("unlink-failed", json.loads(state.read_text())["action"])
        self.assertIn('"action": "unlink-failed"', output.getvalue())

    def test_non_symlink_marker_is_rejected_before_unlink(self):
        prefix, _, calls, brew = self.fixture("regular-marker", "openssl@1.1")
        marker = prefix / "var/homebrew/linked/openssl@1.1"
        marker.unlink()
        marker.write_text("unrecognized marker")
        with self.assertRaisesRegex(ValueError, "unexpected linked"):
            prepare(self.root / "regular-marker.json", brew=brew, environment=self.environment, platform="darwin")
        self.assertNotIn(("unlink", "openssl@1.1"), calls)

    def noop_unlink_fixture(self, layout, additional=None):
        prefix, executable, calls, original = self.fixture(layout, "openssl@1.1")
        (prefix / "var/homebrew/linked/openssl@1.1").unlink()
        keg = prefix / "Cellar/openssl@1.1/fixture-version"
        for name in additional or ():
            source = keg / "bin" / name
            source.write_text("known old CLI fixture")
            (prefix / "bin" / name).symlink_to(source)
        inventory = [str(path) for path in sorted((keg / "bin").iterdir())]

        def brew(*args):
            if args == ("unlink", "openssl@1.1"):
                calls.append(args)
                return "Unlinking 0 symlinks"
            if args == ("list", "--verbose", "openssl@1.1"):
                calls.append(args)
                return "\n".join(inventory)
            return original(*args)
        return prefix, executable, calls, brew, inventory

    def test_fallback_removes_exact_canonical_link_left_by_noop_brew_unlink(self):
        _, executable, calls, brew, _ = self.noop_unlink_fixture("fallback-one")
        state = self.root / "fallback-one.json"
        prepare(state, brew=brew, environment=self.environment, platform="darwin")
        self.assertFalse(os.path.lexists(executable))
        self.assertIn(("unlink", "openssl@1.1"), calls)
        record = json.loads(state.read_text())
        self.assertEqual(["bin/openssl"], record["fallback"]["removed"])

    def test_fallback_removes_multiple_exact_old_exported_cli_symlinks(self):
        prefix, executable, _, brew, _ = self.noop_unlink_fixture("fallback-many", ["c_rehash"])
        state = self.root / "fallback-many.json"
        prepare(state, brew=brew, environment=self.environment, platform="darwin")
        self.assertFalse(os.path.lexists(executable))
        self.assertFalse(os.path.lexists(prefix / "bin/c_rehash"))
        self.assertEqual({"bin/openssl", "bin/c_rehash"}, set(json.loads(state.read_text())["fallback"]["removed"]))

    def test_fallback_rejects_mixed_modern_outside_regular_or_mismapped_nodes_before_removal(self):
        for mode in ("modern", "outside", "regular", "mismapped"):
            with self.subTest(mode=mode):
                prefix, executable, _, brew, _ = self.noop_unlink_fixture(mode, ["c_rehash"])
                other = prefix / "bin/c_rehash"
                other.unlink()
                if mode == "regular":
                    other.write_text("unrecognized regular file")
                else:
                    if mode == "modern":
                        target = prefix / "Cellar/openssl@3/another-version/bin/c_rehash"
                    elif mode == "outside":
                        target = self.root / "foreign/bin/c_rehash"
                    else:
                        target = prefix / "Cellar/openssl@1.1/fixture-version/bin/openssl"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if mode != "mismapped":
                        target.write_text("preserved foreign target")
                    other.symlink_to(target)
                with self.assertRaisesRegex(ValueError, "fallback|export|owner"):
                    prepare(self.root / f"{mode}.json", brew=brew, environment=self.environment, platform="darwin")
                self.assertTrue(executable.is_symlink())
                self.assertTrue(os.path.lexists(other))

    def test_fallback_rejects_malicious_inventory_and_prefix_parent_escape(self):
        for mode in ("inventory", "parent"):
            with self.subTest(mode=mode):
                prefix, executable, _, brew, inventory = self.noop_unlink_fixture(mode)
                if mode == "inventory":
                    outside = self.root / "outside-source/openssl"
                    outside.parent.mkdir()
                    outside.write_text("not an installed keg file")
                    inventory.append(str(outside))
                else:
                    executable.unlink()
                    executable.symlink_to(prefix / "Cellar/openssl@1.1/fixture-version/bin/openssl")
                    moved = self.root / "moved-bin"
                    (prefix / "bin").rename(moved)
                    (prefix / "bin").symlink_to(moved, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "fallback|prefix|keg"):
                    prepare(self.root / f"{mode}.json", brew=brew, environment=self.environment, platform="darwin")
                self.assertTrue(executable.is_symlink())

    def test_fallback_logs_plan_before_removal_and_rejects_raced_symlink(self):
        _, executable, _, brew, _ = self.noop_unlink_fixture("race")
        state = self.root / "race.json"
        output = io.StringIO()
        replace = os.replace
        changed = False

        def race_after_receipt(source, destination):
            nonlocal changed
            replace(source, destination)
            record = json.loads(state.read_text())
            if not changed and record.get("fallback", {}).get("planned_removal") == "bin/openssl":
                changed = True
                executable.unlink()
                executable.write_text("preserve raced regular file")
        with contextlib.redirect_stdout(output), patch("prepare_ci_homebrew.os.replace", side_effect=race_after_receipt):
            with self.assertRaisesRegex(ValueError, "changed|race|fallback"):
                prepare(state, brew=brew, environment=self.environment, platform="darwin")
        self.assertTrue(changed)
        self.assertEqual("preserve raced regular file", executable.read_text())
        self.assertIn('"planned_removal": "bin/openssl"', output.getvalue())

    def test_absent_canonical_never_invokes_fallback_inventory(self):
        _, _, calls, brew = self.fixture("absent-no-fallback", installed=False)
        prepare(self.root / "absent-no-fallback.json", brew=brew, environment=self.environment, platform="darwin")
        self.assertFalse(any("--verbose" in args for args in calls))

    def test_fallback_rejects_version_change_after_plan_without_removing_link(self):
        _, executable, _, original, _ = self.noop_unlink_fixture("version-race")
        state = self.root / "version-race.json"
        changed = False
        replace = os.replace

        def race_after_receipt(source, destination):
            nonlocal changed
            replace(source, destination)
            if json.loads(state.read_text()).get("fallback", {}).get("planned_removal"):
                changed = True

        def brew(*args):
            if changed and args == ("list", "--versions", "openssl@1.1"):
                return "openssl@1.1 unexpected-version\n"
            return original(*args)
        with patch("prepare_ci_homebrew.os.replace", side_effect=race_after_receipt):
            with self.assertRaisesRegex(ValueError, "changed"):
                prepare(state, brew=brew, environment=self.environment, platform="darwin")
        self.assertTrue(executable.is_symlink())

    def test_fallback_rejects_missing_canonical_export_before_other_removals(self):
        prefix, executable, _, brew, inventory = self.noop_unlink_fixture("omitted", ["c_rehash"])
        inventory[:] = [name for name in inventory if not name.endswith("/openssl")]
        with self.assertRaisesRegex(ValueError, "canonical"):
            prepare(self.root / "omitted.json", brew=brew, environment=self.environment, platform="darwin")
        self.assertTrue(executable.is_symlink())
        self.assertTrue((prefix / "bin/c_rehash").is_symlink())

    def test_personal_or_nonmac_environment_fails_before_homebrew(self):
        def brew(*args):
            self.fail("Homebrew must not run outside a hosted Mac")
        for environment, platform in (({}, "darwin"), (self.environment, "linux"),
                                      ({**self.environment, "RUNNER_ENVIRONMENT": "self-hosted"}, "darwin")):
            with self.assertRaisesRegex(ValueError, "hosted macOS"):
                prepare(self.root / "guard.json", brew=brew, environment=environment, platform=platform)

    def test_all_mac_dependency_jobs_prepare_before_install(self):
        root = Path(__file__).resolve().parents[1]
        for workflow, job, invocation in (
            ("validate.yml", "bottle-round-trip", "python3 tap/scripts/prepare_ci_homebrew.py"),
            ("validate.yml", "release-round-trip", "python3 scripts/prepare_ci_homebrew.py"),
            ("bottle.yml", "candidate", "python3 scripts/prepare_ci_homebrew.py"),
            ("agent-refresh.yml", "homebrew-postinstall", "python3 scripts/prepare_ci_homebrew.py"),
        ):
            text = (root / ".github/workflows" / workflow).read_text()
            pieces = re.split(r"(?m)^  ([a-z][\w-]+):\n", text.split("jobs:\n", 1)[1])
            jobs = dict(zip(pieces[1::2], pieces[2::2]))
            relevant = jobs[job]
            self.assertEqual(1, relevant.count(invocation), workflow)
            self.assertLess(relevant.index(invocation), relevant.index("brew install"), job)
            self.assertNotIn("--overwrite", text, workflow)


if __name__ == "__main__":
    unittest.main()
