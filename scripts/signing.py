#!/usr/bin/env python3
"""Verify Git objects against public-only, repository-owned SSH trust."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True, stderr=subprocess.STDOUT, timeout=300).strip()


def stable_version(tag):
    if not re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", tag):
        raise ValueError("expected a stable vMAJOR.MINOR.PATCH release tag")
    version = tag[1:]
    if tuple(map(int, version.split("."))) < (0, 4, 1):
        raise ValueError("signed release policy starts at v0.4.1")
    return version


def git(repo, trust, *args):
    trust = Path(trust).resolve()
    allowed = trust / "allowed_signers"
    revoked = trust / "revoked_keys"
    fingerprint = (trust / "fingerprint").read_text().strip()
    # One namespace-scoped public key; the fingerprint is an independent pin.
    fields = allowed.read_text().split()
    if len(fields) != 4 or fields[:3] != ["epsalmond@gmail.com", 'namespaces="git"', "ssh-ed25519"]:
        raise ValueError("unexpected allowed signer policy")
    if not revoked.is_file():
        raise ValueError("missing revocation policy")
    with tempfile.TemporaryDirectory() as temporary:
        public = Path(temporary) / "key.pub"
        public.write_text(" ".join(fields[2:]) + "\n")
        actual = run("ssh-keygen", "-lf", str(public), "-E", "sha256").split()[1]
    if actual != fingerprint:
        raise ValueError("public signer fingerprint mismatch")
    return run("git", "-C", str(repo), "-c", "gpg.format=ssh",
               "-c", "gpg.ssh.program=/usr/bin/ssh-keygen",
               "-c", f"gpg.ssh.allowedSignersFile={allowed}",
               "-c", f"gpg.ssh.revocationFile={revoked}", *args)


def require_bootstrap(repo, commit, source):
    paths = [".github/signing/allowed_signers", ".github/signing/revoked_keys",
             "scripts/verify-release-tag" if source else "scripts/signing.py",
             ".github/workflows/release.yml" if source else ".github/workflows/signing.yml"]
    for path in paths:
        if not run("git", "-C", str(repo), "show", f"{commit}:{path}"):
            raise ValueError(f"missing committed bootstrap artifact: {path}")


def formula_policy(base_formula, candidate_formula):
    def fields(text):
        result = {}
        for name in ("version", "url", "sha256"):
            matches = re.findall(rf'^  {name} "([^"\n]+)"$', text, re.M)
            if len(matches) != 1:
                raise ValueError(f"formula {name} is ambiguous")
            result[name] = matches[0]
        if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", result["version"]):
            raise ValueError("invalid formula version")
        result["bottle"] = re.findall(r"^  bottle do\n.*?^  end\n", text, re.M | re.S)
        return result
    base, candidate = fields(base_formula), fields(candidate_formula)
    base_version = tuple(map(int, base["version"].split(".")))
    version = tuple(map(int, candidate["version"].split(".")))
    if version < base_version:
        raise ValueError("formula downgrade or stale candidate is not allowed")
    if version < (0, 4, 1):
        if base != candidate:
            raise ValueError("legacy release data is grandfathered only while unchanged from trusted base")
        return {"signed_release": False, "version": candidate["version"]}
    tag = "v" + candidate["version"]
    stable_version(tag)
    url = f"https://github.com/epsalmond/oshioki/releases/download/{tag}/oshioki-macos-arm64-{candidate['version']}.tar.gz"
    if candidate["url"] != url or not re.fullmatch(r"[0-9a-f]{64}", candidate["sha256"]):
        raise ValueError("noncanonical signed release formula")
    return {"signed_release": True, "version": candidate["version"]}


def verify_tag(repo, trust, tag, source=True, expected_commit=None, expected_object=None):
    version = stable_version(tag)
    ref = f"refs/tags/{tag}"
    if run("git", "-C", str(repo), "cat-file", "-t", ref) != "tag":
        raise ValueError("release tag must be annotated and signed")
    # Ensure the tag object's embedded name agrees with the requested ref.
    raw = run("git", "-C", str(repo), "cat-file", "tag", ref)
    if "\ntype commit\n" not in raw.split("\n\n", 1)[0] + "\n":
        raise ValueError("release tag must point directly to a commit")
    if f"\ntag {tag}\n" not in raw.split("\n\n", 1)[0] + "\n":
        raise ValueError("tag object name mismatch")
    tag_object = run("git", "-C", str(repo), "rev-parse", ref)
    commit = run("git", "-C", str(repo), "rev-parse", f"{ref}^{{commit}}")
    if expected_commit is not None and commit != expected_commit:
        raise ValueError("source commit mismatch")
    if expected_object is not None and tag_object != expected_object:
        raise ValueError("tag object mismatch")
    if run("git", "-C", str(repo), "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("full canonical main history is required")
    main = run("git", "-C", str(repo), "rev-parse", "refs/remotes/origin/main^{commit}")
    run("git", "-C", str(repo), "merge-base", "--is-ancestor", commit, main)
    require_bootstrap(repo, commit, source)
    # Read current canonical-main policy, never tag-carried policy. The tap's
    # independently approved SSH fingerprint still pins the upstream signer;
    # upstream rotation requires a reviewed tap policy update.
    current_signers = run("git", "-C", str(repo), "show", f"{main}:.github/signing/allowed_signers")
    if current_signers != (Path(trust) / "allowed_signers").read_text().strip():
        raise ValueError("canonical main signer differs from pinned tap signer")
    with tempfile.TemporaryDirectory() as temporary:
        policy = Path(temporary)
        for name in ("allowed_signers", "fingerprint"):
            (policy / name).write_bytes((Path(trust) / name).read_bytes())
        revoked = run("git", "-C", str(repo), "show", f"{main}:.github/signing/revoked_keys")
        (policy / "revoked_keys").write_text(revoked + "\n" + (Path(trust) / "revoked_keys").read_text())
        verification = git(repo, policy, "verify-tag", ref)
    fingerprint = (Path(trust) / "fingerprint").read_text().strip()
    if f'Good "git" signature for epsalmond@gmail.com with ED25519 key {fingerprint}' not in verification:
        raise ValueError("tag did not verify with the pinned SSH identity")
    if source:
        manifest = run("git", "-C", str(repo), "show", f"{commit}:Cargo.toml")
        if tomllib.loads(manifest)["workspace"]["package"]["version"] != version:
            raise ValueError("upstream Cargo version does not match signed tag")
    else:
        formula = run("git", "-C", str(repo), "show", f"{commit}:Formula/oshioki.rb")
        if re.findall(r'^  version "([^"]+)"$', formula, re.M) != [version]:
            raise ValueError("tap formula version differs from checkpoint tag")
        url = f"https://github.com/epsalmond/oshioki/releases/download/{tag}/oshioki-macos-arm64-{version}.tar.gz"
        if re.findall(r'^  url "([^"]+)"$', formula, re.M) != [url]:
            raise ValueError("tap formula URL differs from checkpoint release")
    return {"tag": tag, "version": version, "commit": commit,
            "tag_object": tag_object,
            "signer_fingerprint": fingerprint}


def verify_commits(repo, trust, base, head):
    for revision in (base, head):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("expected full commit IDs")
    # Ordinary PRs may diverge when main advances. Shared history must already
    # contain the reviewed signing bootstrap; unrelated/replaced history fails.
    shared = run("git", "-C", str(repo), "merge-base", base, head)
    require_bootstrap(repo, shared, source=False)
    require_bootstrap(repo, base, source=False)
    formula_policy(run("git", "-C", str(repo), "show", f"{base}:Formula/oshioki.rb") + "\n",
                   run("git", "-C", str(repo), "show", f"{head}:Formula/oshioki.rb") + "\n")
    commits = run("git", "-C", str(repo), "rev-list", f"{base}..{head}").splitlines()
    if not commits:
        raise ValueError("no introduced PR commits")
    fingerprint = (Path(trust) / "fingerprint").read_text().strip()
    for commit in commits:
        git(repo, trust, "verify-commit", commit)
        status = git(repo, trust, "show", "-s", "--format=%G?%n%GS%n%GF", commit).splitlines()
        if status != ["G", "epsalmond@gmail.com", fingerprint]:
            raise ValueError(f"commit {commit} is not signed by the approved maintainer")
    return commits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--trust", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    tag = commands.add_parser("tag")
    tag.add_argument("tag")
    tag.add_argument("--tap", action="store_true")
    tag.add_argument("--expected-commit")
    tag.add_argument("--expected-object")
    commits = commands.add_parser("commits")
    commits.add_argument("base")
    commits.add_argument("head")
    args = parser.parse_args()
    if args.command == "tag":
        print(json.dumps(verify_tag(args.repo, args.trust, args.tag, not args.tap,
                                   args.expected_commit, args.expected_object), indent=2))
    else:
        print(json.dumps(verify_commits(args.repo, args.trust, args.base, args.head), indent=2))


if __name__ == "__main__":
    main()
