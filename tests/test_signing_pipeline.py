import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from signing import formula_policy, run, stable_version, verify_commits, verify_tag
from release_candidate import assert_candidate, finish, identity, prepare, sha256, verify_archive, verify_formula
from provenance import verify_provenance
from upload_bottle import upload


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.signer_home = tempfile.TemporaryDirectory(prefix="oshioki-test-gpg-")
        Path(cls.signer_home.name).chmod(0o700)
        for name in ("Ephemeral Expected", "Ephemeral Wrong"):
            cls.gpg("--passphrase", "", "--pinentry-mode", "loopback", "--quick-generate-key", name,
                    "ed25519", "sign", "0")
        keys = cls.gpg("--with-colons", "--list-keys").splitlines()
        cls.fingerprints = [keys[index + 1].split(":")[9] for index, line in enumerate(keys) if line.startswith("pub:")]

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["gpgconf", "--homedir", cls.signer_home.name, "--kill", "gpg-agent"], check=True)
        cls.signer_home.cleanup()

    @classmethod
    def gpg(cls, *args):
        return run("gpg", "--no-options", "--homedir", cls.signer_home.name, "--batch", *args)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trust = self.root / "trust"
        self.trust.mkdir()
        self.key = self.root / "ephemeral"
        self.wrong = self.root / "wrong"
        for key in (self.key, self.wrong):
            run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key))
        (self.trust / "allowed_signers").write_text(
            'epsalmond@gmail.com namespaces="git" ' + " ".join(self.key.with_suffix(".pub").read_text().split()[:2]) + "\n")
        (self.trust / "fingerprint").write_text(run("ssh-keygen", "-lf", str(self.key.with_suffix(".pub")),
                                                    "-E", "sha256").split()[1] + "\n")
        (self.trust / "revoked_keys").write_text("# none\n")
        (self.trust / "archive-fingerprint").write_text(self.fingerprints[0] + "\n")
        (self.trust / "archive-key.asc").write_text(self.gpg("--armor", "--export", self.fingerprints[0]) + "\n")
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        self.repo = self.downloads / "source"
        run("git", "init", "-q", str(self.repo))
        for key, value in {"user.name": "Ephemeral Test", "user.email": "epsalmond@gmail.com",
                           "gpg.format": "ssh", "user.signingkey": str(self.key),
                           "commit.gpgsign": "false", "tag.gpgsign": "false"}.items():
            self.git("config", key, value)
        (self.repo / "Cargo.toml").write_text('[workspace.package]\nversion = "0.4.1"\n')
        for path in (".github/signing/allowed_signers", ".github/signing/revoked_keys", ".github/signing/fingerprint"):
            file = self.repo / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes((self.trust / file.name).read_bytes())
        for path in ("scripts/verify-release-tag", "scripts/signing.py", ".github/workflows/release.yml", ".github/workflows/signing.yml"):
            file = self.repo / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("fixture committed bootstrap policy\n")
        (self.repo / "Formula").mkdir()
        (self.repo / "Formula/oshioki.rb").write_text('class Oshioki < Formula\n  version "0.4.1"\n'
            '  url "https://github.com/epsalmond/oshioki/releases/download/v0.4.1/oshioki-macos-arm64-0.4.1.tar.gz"\n'
            '  sha256 "' + "a" * 64 + '"\nend\n')
        self.git("add", ".")
        self.git("commit", "-q", "-m", "fixture")
        self.commit = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.commit)
        self.git("tag", "-s", "v0.4.1", "-m", "ephemeral signed tag")
        self.archive = self.downloads / "oshioki-macos-arm64-0.4.1.tar.gz"
        self.write_tar(self.archive, {"manifest.json": json.dumps({"revision": self.commit})})
        self.sums = self.downloads / "SHA256SUMS"
        self.sums.write_text(f"{sha256(self.archive)}  {self.archive.name}\n")
        self.package = self.downloads / "oshioki_0.4.1_amd64.deb"
        self.package.write_bytes(b"ephemeral package bytes")
        self.sign_provenance()
        (self.downloads / "release.json").write_text(json.dumps({"repository": "epsalmond/oshioki", "release_id": 1,
                                                                 "asset_ids": {self.archive.name: 2, "SHA256SUMS": 3}}))
        self.formula = self.root / "oshioki.rb"
        self.formula.write_text('class Oshioki < Formula\n  url "legacy"\n  sha256 "' + "a" * 64
                                + '"\n  bottle do\n    rebuild 17\n  end\n  version "0.3.1"\nend\n')
        self.output = self.root / "candidate"

    def sign_provenance(self, changes=None, wrong=False):
        manifest = {"schema_version": 1, "repository": "epsalmond/oshioki", "tag": "v0.4.1", "version": "0.4.1",
                    "source_commit": self.commit, "tag_object": self.git("rev-parse", "refs/tags/v0.4.1"),
                    "sha256": {p.name: sha256(p) for p in (self.archive, self.package, self.sums)}}
        manifest.update(changes or {})
        unsigned = self.root / "release.json"
        unsigned.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        self.gpg("--yes", "--passphrase", "", "--pinentry-mode", "loopback", "--local-user",
                 self.fingerprints[1 if wrong else 0], "--digest-algo", "SHA256", "--armor", "--output",
                 str(self.downloads / "RELEASE-PROVENANCE.json.asc"), "--clearsign", str(unsigned))

    def git(self, *args):
        return run("git", "-C", str(self.repo), *args)

    def write_tar(self, path, members):
        with tarfile.open(path, "w:gz") as tar:
            for name, text in members.items():
                content = text.encode()
                info = tarfile.TarInfo(name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))

    def candidate(self):
        prepare(self.formula, self.downloads, self.trust, "v0.4.1", self.output)
        self.installed = self.root / "installed.rb"
        self.installed.write_bytes((self.output / "Formula/oshioki.rb").read_bytes())

    def bottle(self, text=None):
        self.bottle_archive = self.root / "oshioki--0.4.1.arm64_sonoma.bottle.tar.gz"
        self.write_tar(self.bottle_archive, {"oshioki/0.4.1/.brew/oshioki.rb":
                                            self.installed.read_text() if text is None else text})
        self.bottle_data = {"epsalmond/oshioki/oshioki": {
            "formula": {"name": "oshioki", "pkg_version": "0.4.1", "tap_git_path": "Formula/oshioki.rb"},
            "bottle": {"root_url": "https://github.com/epsalmond/oshioki/releases/download/v0.4.1",
                       "cellar": "any_skip_relocation", "rebuild": 0,
                       "tags": {"arm64_sonoma": {"local_filename": self.bottle_archive.name,
                                                 "filename": self.bottle_archive.name.replace("oshioki--", "oshioki-"),
                                                 "sha256": sha256(self.bottle_archive)}}}}}
        self.bottle_json = self.root / "oshioki--0.4.1.arm64_sonoma.bottle.json"
        self.bottle_json.write_text(json.dumps(self.bottle_data))

    def test_expected_signed_tag_passes(self):
        verified = verify_tag(self.repo, self.trust, "v0.4.1")
        self.assertEqual(self.commit, verified["commit"])

    def test_unsigned_and_lightweight_tags_fail(self):
        self.git("tag", "-d", "v0.4.1")
        self.git("tag", "-a", "v0.4.1", "-m", "unsigned")
        with self.assertRaises(subprocess.CalledProcessError):
            verify_tag(self.repo, self.trust, "v0.4.1")
        self.git("tag", "-d", "v0.4.1")
        self.git("tag", "v0.4.1")
        with self.assertRaisesRegex(ValueError, "annotated"):
            verify_tag(self.repo, self.trust, "v0.4.1")

    def test_wrong_signer_tag_fails(self):
        self.git("tag", "-d", "v0.4.1")
        self.git("-c", f"user.signingkey={self.wrong}", "tag", "-s", "v0.4.1", "-m", "wrong key")
        with self.assertRaises(subprocess.CalledProcessError):
            verify_tag(self.repo, self.trust, "v0.4.1")

    def test_revoked_or_missing_trust_fails(self):
        (self.trust / "revoked_keys").write_text(self.key.with_suffix(".pub").read_text())
        with self.assertRaises(subprocess.CalledProcessError):
            verify_tag(self.repo, self.trust, "v0.4.1")
        (self.trust / "allowed_signers").unlink()
        with self.assertRaises(FileNotFoundError):
            verify_tag(self.repo, self.trust, "v0.4.1")

    def test_fingerprint_mismatch_fails(self):
        (self.trust / "fingerprint").write_text("SHA256:wrong\n")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            verify_tag(self.repo, self.trust, "v0.4.1")

    def test_tag_alias_checkout_and_version_mismatch_fail(self):
        self.git("update-ref", "refs/tags/v0.4.2", self.git("rev-parse", "refs/tags/v0.4.1"))
        with self.assertRaisesRegex(ValueError, "name mismatch"):
            verify_tag(self.repo, self.trust, "v0.4.2")
        self.git("commit", "--allow-empty", "-q", "-m", "different checkout")
        with self.assertRaisesRegex(ValueError, "commit mismatch"):
            verify_tag(self.repo, self.trust, "v0.4.1", expected_commit=self.git("rev-parse", "HEAD"))
        self.git("tag", "-s", "v0.4.3", "-m", "wrong Cargo version")
        self.git("update-ref", "refs/remotes/origin/main", self.git("rev-parse", "HEAD"))
        with self.assertRaisesRegex(ValueError, "Cargo version"):
            verify_tag(self.repo, self.trust, "v0.4.3")

    def test_input_validation_fails_before_command_execution(self):
        for tag in ("v0.4.0", "v0.4.1-rc1", "v0.04.1", "v0.4.1;touch /tmp/unwanted", "--help"):
            with self.assertRaises(ValueError):
                stable_version(tag)

    def test_all_introduced_commits_require_expected_signer(self):
        self.git("commit", "-S", "--allow-empty", "-q", "-m", "signed candidate")
        head = self.git("rev-parse", "HEAD")
        self.assertEqual([head], verify_commits(self.repo, self.trust, self.commit, head))
        self.git("commit", "--allow-empty", "-q", "-m", "unsigned introduced ancestor")
        self.git("commit", "-S", "--allow-empty", "-q", "-m", "signed head")
        with self.assertRaises(subprocess.CalledProcessError):
            verify_commits(self.repo, self.trust, self.commit, self.git("rev-parse", "HEAD"))

    def test_candidate_keeps_complete_formula_and_removes_stale_bottle(self):
        self.candidate()
        fields = identity(self.installed.read_text())
        self.assertEqual("0.4.1", fields["version"])
        self.assertNotIn("bottle do", self.installed.read_text())
        self.assertEqual(sha256(self.archive), fields["sha256"])
        assert_candidate(self.installed, self.output)
        verify_formula(self.installed, self.downloads, self.trust)

    def test_candidate_mismatch_fails(self):
        self.candidate()
        self.installed.write_text(self.installed.read_text().replace("0.4.1", "0.3.1"))
        with self.assertRaisesRegex(ValueError, "differs"):
            assert_candidate(self.installed, self.output)

    def test_joint_archive_and_checksum_substitution_is_rejected(self):
        self.write_tar(self.archive, {"manifest.json": json.dumps({"revision": self.commit}),
                                      "oshioki": "attacker controlled executable"})
        self.sums.write_text(f"{sha256(self.archive)}  {self.archive.name}\n")
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            prepare(self.formula, self.downloads, self.trust, "v0.4.1", self.output)

    def test_signed_off_main_tag_is_rejected(self):
        self.git("update-ref", "refs/remotes/origin/main", self.commit)
        self.git("commit", "-S", "--allow-empty", "-q", "-m", "off main")
        self.git("tag", "-d", "v0.4.1")
        self.git("tag", "-s", "v0.4.1", "-m", "off main signed tag")
        for source in (True, False):
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                verify_tag(self.repo, self.trust, "v0.4.1", source=source)

    def test_signed_pr_remains_valid_when_main_advances(self):
        self.git("commit", "-S", "--allow-empty", "-q", "-m", "signed candidate")
        head = self.git("rev-parse", "HEAD")
        self.git("checkout", "--detach", self.commit)
        self.git("commit", "-S", "--allow-empty", "-q", "-m", "different main commit")
        base = self.git("rev-parse", "HEAD")
        self.assertEqual([head], verify_commits(self.repo, self.trust, base, head))

    def test_signature_job_is_owned_by_trusted_base_and_runs_no_candidate_code(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/signing.yml").read_text()
        self.assertIn("pull_request_target:", workflow)
        self.assertNotIn("  pull_request:", workflow)
        self.assertNotIn("verifier=candidate", workflow)
        self.assertNotIn("path: candidate", workflow)
        self.assertNotIn("unittest", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertIn("refs/review/head", workflow)
        self.assertIn("ref: ${{ github.event.pull_request.base.sha }}", workflow)
        self.assertNotIn("ref: ${{ github.event.pull_request.head.sha", workflow)
        self.assertIn("python3 scripts/signing.py --repo . --trust .github/signing commits", workflow)

    def test_current_main_revocation_overrides_tag_policy(self):
        revoked = self.repo / ".github/signing/revoked_keys"
        revoked.write_text(self.key.with_suffix(".pub").read_text())
        self.git("add", str(revoked))
        self.git("commit", "-S", "-q", "-m", "revoke signer on canonical main")
        self.git("update-ref", "refs/remotes/origin/main", self.git("rev-parse", "HEAD"))
        with self.assertRaises(subprocess.CalledProcessError):
            verify_tag(self.repo, self.trust, "v0.4.1")

    def test_missing_bootstrap_and_exact_object_mismatch_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "object mismatch"):
            verify_tag(self.repo, self.trust, "v0.4.1", expected_object="0" * 40)
        (self.repo / "scripts/verify-release-tag").unlink()
        self.git("add", "-u")
        self.git("commit", "-S", "-q", "-m", "missing bootstrap")
        self.git("update-ref", "refs/remotes/origin/main", self.git("rev-parse", "HEAD"))
        self.git("tag", "-d", "v0.4.1")
        self.git("tag", "-s", "v0.4.1", "-m", "signed but missing policy")
        with self.assertRaises(subprocess.CalledProcessError):
            verify_tag(self.repo, self.trust, "v0.4.1")

    def test_tap_checkpoint_formula_mismatch_is_rejected(self):
        verify_tag(self.repo, self.trust, "v0.4.1", source=False)
        formula = self.repo / "Formula/oshioki.rb"
        formula.write_text(formula.read_text().replace('version "0.4.1"', 'version "0.3.1"'))
        self.git("add", "Formula/oshioki.rb")
        self.git("commit", "-S", "-q", "-m", "wrong formula checkpoint")
        self.git("update-ref", "refs/remotes/origin/main", self.git("rev-parse", "HEAD"))
        self.git("tag", "-d", "v0.4.1")
        self.git("tag", "-s", "v0.4.1", "-m", "formula mismatch")
        with self.assertRaisesRegex(ValueError, "formula version"):
            verify_tag(self.repo, self.trust, "v0.4.1", source=False)

    def test_pr_merge_excludes_existing_main_unsigned_history(self):
        self.git("commit", "-S", "--allow-empty", "-q", "-m", "signed feature")
        feature = self.git("rev-parse", "HEAD")
        self.git("checkout", "--detach", self.commit)
        (self.repo / "main-file").write_text("grandfathered main data")
        self.git("add", "main-file")
        self.git("commit", "-q", "-m", "existing main commit")
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "--detach", feature)
        self.git("merge", "--no-ff", "-S", "-m", "signed merge of advanced main", base)
        head = self.git("rev-parse", "HEAD")
        self.assertEqual({head, feature}, set(verify_commits(self.repo, self.trust, base, head)))

    def test_history_replacement_is_rejected(self):
        self.git("checkout", "--orphan", "replacement")
        self.git("add", ".")
        self.git("commit", "-S", "-q", "-m", "unrelated signed history")
        with self.assertRaises(subprocess.CalledProcessError):
            verify_commits(self.repo, self.trust, self.commit, self.git("rev-parse", "HEAD"))

    def test_provenance_signature_identity_and_joint_package_replacement_rejected(self):
        verified = verify_tag(self.repo, self.trust, "v0.4.1")
        verify_provenance(self.downloads, self.trust, verified, sha256)
        self.sign_provenance(wrong=True)
        with self.assertRaises(subprocess.CalledProcessError):
            verify_provenance(self.downloads, self.trust, verified, sha256)
        for changes in ({"source_commit": "0" * 40}, {"tag_object": "0" * 40}, {"repository": "attacker/oshioki"},
                        {"version": "0.4.2"}, {"schema_version": 2}):
            self.sign_provenance(changes)
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                verify_provenance(self.downloads, self.trust, verified, sha256)
        self.sign_provenance()
        signed = self.downloads / "RELEASE-PROVENANCE.json.asc"
        signed.write_text(signed.read_text().replace("epsalmond/oshioki", "attacker/oshioki"))
        with self.assertRaises(subprocess.CalledProcessError):
            verify_provenance(self.downloads, self.trust, verified, sha256)
        self.sign_provenance()
        self.package.write_bytes(b"attacker controlled package")
        self.sums.write_text(f"{sha256(self.archive)}  {self.archive.name}\n{sha256(self.package)}  {self.package.name}\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            verify_provenance(self.downloads, self.trust, verified, sha256)

    def test_legacy_opt_out_and_routine_downgrade_are_rejected(self):
        legacy = self.formula.read_text()
        self.assertFalse(formula_policy(legacy, legacy)["signed_release"])
        with self.assertRaisesRegex(ValueError, "grandfathered"):
            formula_policy(legacy, legacy.replace('url "legacy"', 'url "other"'))
        current = (self.repo / "Formula/oshioki.rb").read_text()
        with self.assertRaisesRegex(ValueError, "downgrade"):
            formula_policy(current, legacy)
        newer = current.replace("0.4.1", "0.4.2")
        with self.assertRaisesRegex(ValueError, "downgrade"):
            formula_policy(newer, current)
        self.formula.write_text(newer)
        with self.assertRaisesRegex(ValueError, "downgrade"):
            prepare(self.formula, self.downloads, self.trust, "v0.4.1", self.output)

    def test_signed_provenance_replay_for_recreated_tag_is_rejected(self):
        self.git("tag", "-d", "v0.4.1")
        self.git("tag", "-s", "v0.4.1", "-m", "different signed tag object on identical source")
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            prepare(self.formula, self.downloads, self.trust, "v0.4.1", self.output)

    def test_published_archive_checksum_and_revision_mismatch_fail(self):
        self.sums.write_text(f'{"b" * 64}  {self.archive.name}\n')
        with self.assertRaisesRegex(ValueError, "checksum"):
            verify_archive(self.archive, self.sums, self.commit)
        self.sums.write_text(f"{sha256(self.archive)}  {self.archive.name}\n")
        with self.assertRaisesRegex(ValueError, "revision"):
            verify_archive(self.archive, self.sums, "0" * 40)

    def test_submitted_formula_url_and_hash_mismatch_fail(self):
        self.candidate()
        self.installed.write_text(self.installed.read_text().replace(sha256(self.archive), "b" * 64))
        with self.assertRaisesRegex(ValueError, "hash"):
            verify_formula(self.installed, self.downloads, self.trust)
        with self.assertRaisesRegex(ValueError, "URL"):
            identity(self.installed.read_text().replace("github.com/epsalmond", "example.org/attacker"))

    def test_finish_emits_complete_formula_patch_and_digests(self):
        self.candidate()
        self.bottle()
        finish(self.installed, self.output, self.bottle_json, self.bottle_archive)
        final = (self.output / "Formula/oshioki.rb").read_text()
        self.assertEqual(1, final.count("bottle do"))
        self.assertIn(sha256(self.bottle_archive), final)
        self.assertTrue((self.output / "formula.patch").is_file())
        for line in (self.output / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split()
            self.assertEqual(digest, sha256(self.output / name))

    def test_stale_embedded_candidate_fails(self):
        self.candidate()
        self.bottle(text=self.formula.read_text())
        with self.assertRaisesRegex(ValueError, "different formula"):
            finish(self.installed, self.output, self.bottle_json, self.bottle_archive)

    def test_bottle_metadata_mismatch_fails(self):
        self.candidate()
        self.bottle()
        self.bottle_data["epsalmond/oshioki/oshioki"]["formula"]["pkg_version"] = "0.3.1"
        self.bottle_json.write_text(json.dumps(self.bottle_data))
        with self.assertRaisesRegex(ValueError, "identity"):
            finish(self.installed, self.output, self.bottle_json, self.bottle_archive)

    def upload_fixture(self, existing=None):
        self.candidate()
        self.bottle()
        finish(self.installed, self.output, self.bottle_json, self.bottle_archive)
        name = self.bottle_archive.name.replace("oshioki--", "oshioki-")
        release = {"id": 1, "tagName": "v0.4.1", "isDraft": False, "isPrerelease": False,
                   "assets": [] if existing is None else [{"name": name, "url": "https://example.org/asset"}]}
        calls = []

        def fake_run(*args):
            calls.append(args)
            if args[:3] == ("gh", "release", "view"):
                return json.dumps(release)
            if args[0] == "curl":
                Path(args[args.index("--output") + 1]).write_bytes(existing)
            return ""
        return calls, fake_run

    def test_upload_absent_asset_has_no_clobber(self):
        calls, fake = self.upload_fixture()
        with patch("upload_bottle.run", side_effect=fake):
            upload(self.output)
        self.assertTrue(any(call[:3] == ("gh", "release", "upload") for call in calls))
        self.assertFalse(any("--clobber" in call or "delete-asset" in call for call in calls))

    def test_upload_identical_is_noop(self):
        # Create the fixture's bottle first; deterministic tar bytes are read
        # from the handoff inside the mock instead of relying on timestamps.
        calls, fake = self.upload_fixture(existing=b"placeholder")

        def identical(*args):
            if args[0] == "curl":
                calls.append(args)
                Path(args[args.index("--output") + 1]).write_bytes(self.bottle_archive.read_bytes())
                return ""
            return fake(*args)
        with patch("upload_bottle.run", side_effect=identical):
            upload(self.output)
        self.assertFalse(any(call[:3] == ("gh", "release", "upload") for call in calls))

    def test_upload_conflicting_bytes_fails(self):
        calls, fake = self.upload_fixture(existing=b"different published bottle")
        with patch("upload_bottle.run", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "different bytes"):
                upload(self.output)
        self.assertFalse(any(call[:3] == ("gh", "release", "upload") for call in calls))


if __name__ == "__main__":
    unittest.main()
