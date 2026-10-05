#!/usr/bin/env python3
"""Produce and check a formula handoff without committing or pushing it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile

from signing import formula_policy, run, stable_version, verify_tag
from provenance import verify_provenance

REPOSITORY = "epsalmond/oshioki"
FORMULA = "epsalmond/oshioki/oshioki"


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def without_bottle(text):
    return re.sub(r"^  bottle do\n.*?^  end\n", "", text, flags=re.M | re.S)


def identity(text):
    result = {}
    for field in ("url", "sha256", "version"):
        values = re.findall(rf'^  {field} "([^"\n]+)"$', text, flags=re.M)
        if len(values) != 1:
            raise ValueError(f"expected exactly one formula {field}")
        result[field] = values[0]
    version = stable_version("v" + result["version"])
    expected = f"https://github.com/{REPOSITORY}/releases/download/v{version}/oshioki-macos-arm64-{version}.tar.gz"
    if result["url"] != expected or not re.fullmatch(r"[0-9a-f]{64}", result["sha256"]):
        raise ValueError("formula release URL or digest is not canonical")
    return result


def archive_revision(archive):
    with tarfile.open(archive, "r:gz") as tar:
        manifests = [member for member in tar.getmembers() if member.name.removeprefix("./") == "manifest.json"]
        if len(manifests) != 1 or not manifests[0].isfile() or manifests[0].size > 1024 * 1024:
            raise ValueError("archive must contain one regular manifest.json")
        return json.load(tar.extractfile(manifests[0]))["revision"]


def verify_archive(archive, sums, commit):
    entries = [line.split() for line in Path(sums).read_text().splitlines()]
    matches = [fields[0] for fields in entries if len(fields) == 2 and fields[1].lstrip("*") == Path(archive).name]
    digest = sha256(archive)
    if matches != [digest]:
        raise ValueError("published archive checksum missing, duplicate, or mismatched")
    if archive_revision(archive) != commit:
        raise ValueError("published archive revision differs from signed upstream commit")
    return digest


def fetch(tag, output):
    version = stable_version(tag)
    output.mkdir(parents=True, exist_ok=False)
    # Fresh local repository: no configurable/forked upstream remote or tag cache.
    source = output / "source"
    run("git", "init", str(source))
    run("git", "-C", str(source), "fetch", "--no-tags", f"https://github.com/{REPOSITORY}.git",
        "refs/heads/main:refs/remotes/origin/main", f"refs/tags/{tag}:refs/tags/{tag}")
    run("git", "-C", str(source), "checkout", "--detach", f"refs/tags/{tag}^{{commit}}")
    release = json.loads(run("gh", "release", "view", tag, "--repo", REPOSITORY,
                             "--json", "id,tagName,isDraft,isPrerelease,assets"))
    if release["tagName"] != tag or release["isDraft"] or release["isPrerelease"]:
        raise ValueError("expected published stable upstream release")
    names = [f"oshioki-macos-arm64-{version}.tar.gz", f"oshioki_{version}_amd64.deb",
             "SHA256SUMS", "RELEASE-PROVENANCE.json.asc"]
    asset_ids = {}
    for name in names:
        matches = [asset for asset in release["assets"] if asset["name"] == name]
        if len(matches) != 1:
            raise ValueError(f"release must have exactly one {name}")
        asset_ids[name] = matches[0]["id"]
        # curl fails on HTTP errors; this also avoids gh's cached downloads.
        run("curl", "--fail", "--location", "--silent", "--show-error", "--output",
            str(output / name), matches[0]["url"])
    (output / "release.json").write_text(json.dumps({"repository": REPOSITORY,
                                                       "release_id": release["id"], "asset_ids": asset_ids}, indent=2) + "\n")


def prepare(formula, downloads, trust, tag, output):
    version = stable_version(tag)
    verified = verify_tag(downloads / "source", trust, tag,
                          expected_commit=run("git", "-C", str(downloads / "source"), "rev-parse", "HEAD"))
    provenance_signature = verify_provenance(downloads, trust, verified, sha256)
    archive = downloads / f"oshioki-macos-arm64-{version}.tar.gz"
    digest = verify_archive(archive, downloads / "SHA256SUMS", verified["commit"])
    text = without_bottle(formula.read_text())
    fields = {"url": f"https://github.com/{REPOSITORY}/releases/download/{tag}/{archive.name}",
              "sha256": digest, "version": version}
    for field, value in fields.items():
        text, count = re.subn(rf'^  {field} "[^"\n]+"$', f'  {field} "{value}"', text, flags=re.M)
        if count != 1:
            raise ValueError(f"formula {field} is missing or ambiguous")
    identity(text)
    formula_policy(formula.read_text(), text)
    output.mkdir(parents=True, exist_ok=False)
    (output / "Formula").mkdir()
    candidate = output / "Formula/oshioki.rb"
    candidate.write_text(text)
    shutil.copyfile(downloads / "SHA256SUMS", output / "upstream-SHA256SUMS")
    shutil.copyfile(downloads / "RELEASE-PROVENANCE.json.asc", output / "upstream-RELEASE-PROVENANCE.json.asc")
    provenance = {"schema_version": 1, "upstream": verified,
                  "release": json.loads((downloads / "release.json").read_text()),
                  "source_archive_sha256": digest, "candidate_sha256": sha256(candidate),
                  "signed_provenance": provenance_signature,
                  "tap_base_commit": run("git", "rev-parse", "HEAD"),
                  "run_url": f'https://github.com/{os.environ.get("GITHUB_REPOSITORY", "epsalmond/homebrew-oshioki")}/actions/runs/{os.environ.get("GITHUB_RUN_ID", "local")}',
                  "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local")}
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")


def assert_candidate(formula, output):
    provenance = json.loads((output / "provenance.json").read_text())
    if sha256(formula) != provenance["candidate_sha256"]:
        raise ValueError("installed tap formula differs from generated candidate")
    return provenance


def verify_formula(formula, downloads, trust):
    fields = identity(formula.read_text())
    verified = verify_tag(downloads / "source", trust, "v" + fields["version"])
    verify_provenance(downloads, trust, verified, sha256)
    digest = verify_archive(downloads / f'oshioki-macos-arm64-{fields["version"]}.tar.gz',
                            downloads / "SHA256SUMS", verified["commit"])
    if fields["sha256"] != digest:
        raise ValueError("submitted formula hash does not match published archive")
    return verified


def finish(formula, output, bottle_json, archive):
    provenance = assert_candidate(formula, output)
    version = provenance["upstream"]["version"]
    root_url = f'https://github.com/{REPOSITORY}/releases/download/v{version}'
    data = json.loads(bottle_json.read_text())
    if set(data) != {FORMULA}:
        raise ValueError("bottle JSON is not for the canonical candidate tap")
    item = data[FORMULA]
    if (item["formula"]["pkg_version"] != version or item["formula"]["name"] != "oshioki"
            or item["formula"]["tap_git_path"] != "Formula/oshioki.rb"):
        raise ValueError("bottle JSON formula identity mismatch")
    bottle = item["bottle"]
    if bottle["root_url"] != root_url or bottle["cellar"] != "any_skip_relocation":
        raise ValueError("bottle root URL or relocation policy mismatch")
    tags = bottle["tags"]
    if set(tags) != {"arm64_sonoma"}:
        raise ValueError("expected one arm64_sonoma bottle")
    entry = tags["arm64_sonoma"]
    rebuild = bottle["rebuild"]
    if not isinstance(rebuild, int) or rebuild < 0:
        raise ValueError("invalid bottle rebuild")
    suffix = f'.bottle{("." + str(rebuild)) if rebuild else ""}.tar.gz'
    expected_local = f"oshioki--{version}.arm64_sonoma{suffix}"
    expected_remote = f"oshioki-{version}.arm64_sonoma{suffix}"
    if archive.name != expected_local or entry["local_filename"] != expected_local or entry["filename"] != expected_remote:
        raise ValueError("bottle filename differs from candidate version")
    if sha256(archive) != entry["sha256"]:
        raise ValueError("bottle archive digest differs from bottle JSON")
    # Homebrew embeds the installed formula. This catches an old keg/cache
    # even if the current tap file and bottle JSON metadata look correct.
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name.endswith("/.brew/oshioki.rb")]
        if len(members) != 1 or not members[0].isfile() or members[0].size > 1024 * 1024:
            raise ValueError("bottle does not contain exactly one installed formula")
        embedded = tar.extractfile(members[0]).read().decode()
    if embedded != formula.read_text():
        raise ValueError("bottle contains a different formula candidate")
    block = f'  bottle do\n    root_url "{root_url}"\n'
    if rebuild:
        block += f"    rebuild {rebuild}\n"
    block += f'    sha256 cellar: :any_skip_relocation, arm64_sonoma: "{entry["sha256"]}"\n  end\n'
    text = re.sub(r'(^  sha256 "[0-9a-f]{64}"\n)', lambda match: match[0] + "\n" + block,
                  formula.read_text(), count=1, flags=re.M)
    (output / "Formula/oshioki.rb").write_text(text)
    shutil.copyfile(archive, output / expected_remote)
    shutil.copyfile(bottle_json, output / bottle_json.name)
    provenance.update({"formula_sha256": sha256(output / "Formula/oshioki.rb"),
                       "bottle": {"filename": expected_remote, "sha256": entry["sha256"]},
                       "checks": ["signed-upstream-tag", "signed-release-provenance", "published-archive-checksum", "archive-revision",
                                  "candidate-build-test", "bottle-pour-test", "embedded-candidate-match"]})
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    # One complete replacement formula plus a conventional reviewable patch.
    baseline = run("git", "show", "HEAD:Formula/oshioki.rb") + "\n"
    import difflib
    (output / "formula.patch").write_text("".join(difflib.unified_diff(
        baseline.splitlines(keepends=True), text.splitlines(keepends=True),
        fromfile="a/Formula/oshioki.rb", tofile="b/Formula/oshioki.rb")))
    files = sorted(path for path in output.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
    (output / "SHA256SUMS").write_text("".join(f"{sha256(path)}  {path.relative_to(output)}\n" for path in files))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_parser = commands.add_parser("fetch")
    fetch_parser.add_argument("tag")
    fetch_parser.add_argument("output", type=Path)
    prepare_parser = commands.add_parser("prepare")
    for name in ("formula", "downloads", "trust"):
        prepare_parser.add_argument(name, type=Path)
    prepare_parser.add_argument("tag")
    prepare_parser.add_argument("output", type=Path)
    check = commands.add_parser("assert-candidate")
    check.add_argument("formula", type=Path)
    check.add_argument("output", type=Path)
    verify = commands.add_parser("verify-formula")
    for name in ("formula", "downloads", "trust"):
        verify.add_argument(name, type=Path)
    final = commands.add_parser("finish")
    for name in ("formula", "output", "bottle_json", "archive"):
        final.add_argument(name, type=Path)
    args = parser.parse_args()
    values = vars(args).copy()
    values.pop("command")
    functions = {"fetch": fetch, "prepare": prepare, "assert-candidate": assert_candidate,
                 "verify-formula": verify_formula, "finish": finish}
    functions[args.command](**values)


if __name__ == "__main__":
    main()
