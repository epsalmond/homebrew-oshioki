#!/usr/bin/env python3
"""Upload an absent bottle, accept identical retries, reject asset replacement."""
import argparse
import json
from pathlib import Path
import re
import tempfile

from release_candidate import REPOSITORY, sha256
from signing import run, stable_version


def upload(candidate):
    provenance = json.loads((candidate / "provenance.json").read_text())
    tag = provenance["upstream"]["tag"]
    version = stable_version(tag)
    name = provenance["bottle"]["filename"]
    if not re.fullmatch(rf"oshioki-{re.escape(version)}\.arm64_sonoma\.bottle(?:\.[1-9][0-9]*)?\.tar\.gz", name):
        raise ValueError("unexpected bottle asset name")
    archive = candidate / name
    if sha256(archive) != provenance["bottle"]["sha256"]:
        raise ValueError("handoff bottle checksum mismatch")
    release = json.loads(run("gh", "release", "view", tag, "--repo", REPOSITORY,
                             "--json", "id,tagName,isDraft,isPrerelease,assets"))
    if (release["id"] != provenance["release"]["release_id"] or release["tagName"] != tag
            or release["isDraft"] or release["isPrerelease"]):
        raise ValueError("upstream release identity changed")
    matches = [asset for asset in release["assets"] if asset["name"] == name]
    if len(matches) > 1:
        raise ValueError("duplicate release asset name")
    if matches:
        with tempfile.TemporaryDirectory() as temporary:
            existing = Path(temporary) / name
            run("curl", "--fail", "--location", "--silent", "--show-error",
                "--output", str(existing), matches[0]["url"])
            if sha256(existing) != sha256(archive):
                raise ValueError("existing bottle has different bytes; use a new version or explicit recovery decision")
        print("Existing bottle is identical; no upload needed.")
    else:
        # No --clobber: a competing upload also fails closed.
        run("gh", "release", "upload", tag, str(archive), "--repo", REPOSITORY)
        print("Uploaded previously absent bottle.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    upload(parser.parse_args().candidate)
