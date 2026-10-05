#!/usr/bin/env python3
"""Isolate the hosted Mac image's known OpenSSL 1.1 link conflict.

The runner is disposable: record its prior state, then keep dependency OpenSSL
3 available through the tests rather than relinking the image's old keg.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def command(*args):
    return subprocess.check_output(["brew", *args], text=True, timeout=60).strip()


def prepare(state, brew=command, environment=None, platform=None):
    environment = os.environ if environment is None else environment
    platform = sys.platform if platform is None else platform
    if (platform != "darwin" or environment.get("GITHUB_ACTIONS") != "true"
            or environment.get("RUNNER_ENVIRONMENT") != "github-hosted"):
        raise ValueError("Homebrew preparation requires a disposable GitHub-hosted macOS runner")
    state = Path(state).resolve()
    state.relative_to(Path(environment["RUNNER_TEMP"]).resolve())
    if state.exists():
        raise ValueError("refusing to replace the prior Homebrew state receipt")
    prefix = Path(brew("--prefix")).resolve()
    executable = prefix / "bin/openssl"
    linked = prefix / "var/homebrew/linked/openssl@1.1"
    # An absent keg needs no query (brew list exits nonzero when absent).
    # If the keg directory exists, query exactly this formula and propagate
    # any Homebrew error rather than guessing installed versions.
    listing = brew("list", "--versions", "openssl@1.1") if (prefix / "Cellar/openssl@1.1").is_dir() else ""
    installed = [line.split()[1:] for line in listing.splitlines()
                 if line.split() and line.split()[0] == "openssl@1.1"]
    versions = [version for entry in installed for version in entry]

    def owner():
        if not os.path.lexists(executable):
            return None
        if executable.is_symlink():
            try:
                parts = executable.resolve(strict=True).relative_to(prefix / "Cellar").parts
                if len(parts) == 4 and parts[2:] == ("bin", "openssl") and parts[0] in ("openssl@1.1", "openssl@3"):
                    return parts[0], parts[1]
            except (ValueError, FileNotFoundError):
                pass
        raise ValueError("unexpected owner of Homebrew bin/openssl; no overwrite or unlink performed")

    current_owner = owner()
    conflict = current_owner is not None and current_owner[0] == "openssl@1.1"
    linked_before = linked.is_symlink() or conflict
    if conflict and not versions:
        raise ValueError("OpenSSL 1.1 link has no installed known keg; refusing to unlink")
    if conflict and current_owner[1] not in versions:
        raise ValueError("canonical OpenSSL 1.1 version is not installed; refusing to unlink")
    marker_version = None
    if os.path.lexists(linked):
        try:
            if not linked.is_symlink():
                raise ValueError("linked marker is not a symlink")
            parts = linked.resolve(strict=True).relative_to(prefix / "Cellar").parts
            if len(parts) != 2 or parts[0] != "openssl@1.1":
                raise ValueError("unexpected linked OpenSSL 1.1 keg")
            marker_version = parts[1]
        except (ValueError, FileNotFoundError) as error:
            raise ValueError("unexpected linked OpenSSL 1.1 keg; refusing to unlink") from error
    if conflict and marker_version is not None and marker_version != current_owner[1]:
        raise ValueError("canonical and linked-keg OpenSSL 1.1 versions are inconsistent; refusing to unlink")
    record = {"prefix": str(prefix), "installed_versions": versions, "linked_before": linked_before,
              "canonical_owner_before": current_owner, "linked_keg_version_before": marker_version,
              "openssl_link_before": os.readlink(executable) if executable.is_symlink() else None,
              "linked_keg_before": os.readlink(linked) if linked.is_symlink() else None,
              "action": "planned-unlink-openssl@1.1" if conflict else "unchanged",
              "restore": "Not restored: this disposable hosted runner is discarded after the job; OpenSSL 3 remains available to dependencies."}
    def snapshot():
        with tempfile.NamedTemporaryFile(mode="w", dir=state.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(json.dumps(record, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(temporary, state)
            finally:
                temporary.unlink(missing_ok=True)
        print(json.dumps(record, sort_keys=True), flush=True)
    snapshot()  # Complete prior state reaches disk and retained logs BEFORE unlink.
    if conflict:
        try:
            brew("unlink", "openssl@1.1")
            remaining = owner()
            if (remaining is not None and remaining[0] == "openssl@1.1") or os.path.lexists(linked):
                raise ValueError("known OpenSSL 1.1 conflict remains after unlink")
        except Exception:
            record["action"] = "unlink-failed"
            snapshot()
            raise
        record["action"] = "unlinked-openssl@1.1"
        snapshot()
    print(f"Hosted Homebrew preparation: {record['action']}; prior state: {state}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    prepare(parser.parse_args().state)
