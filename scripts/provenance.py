"""Verify public Oshioki release provenance with an independently pinned APT key."""
import json
from pathlib import Path
import re
import subprocess
import tempfile


def verify_provenance(downloads, trust, identity, digest):
    fingerprint = (trust / "archive-fingerprint").read_text().strip().upper()
    if not re.fullmatch(r"[0-9A-F]{40}|[0-9A-F]{64}", fingerprint):
        raise ValueError("full archive primary fingerprint required")
    signed = downloads / "RELEASE-PROVENANCE.json.asc"
    if not signed.read_bytes().startswith(b"-----BEGIN PGP SIGNED MESSAGE-----"):
        raise ValueError("expected clearsigned release provenance")
    with tempfile.TemporaryDirectory(prefix="oshioki-provenance-verify-") as directory:
        home = Path(directory)
        home.chmod(0o700)

        def gpg(*args):
            return subprocess.run(["gpg", "--no-options", "--homedir", str(home), "--batch",
                                   "--no-autostart", "--no-auto-key-retrieve", *args],
                                  check=True, capture_output=True, text=True, timeout=30)

        gpg("--import", str(trust / "archive-key.asc"))
        keys = gpg("--with-colons", "--list-keys").stdout.splitlines()
        primaries = [keys[index + 1].split(":")[9] for index, line in enumerate(keys) if line.startswith("pub:")]
        if primaries != [fingerprint]:
            raise ValueError("archive keyring must contain only the pinned primary key")
        unsigned = home / "provenance.json"
        result = gpg("--status-fd", "1", "--output", str(unsigned), "--decrypt", str(signed))
        valid = [line.split() for line in result.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
        if len(valid) != 1 or fingerprint not in (valid[0][2], valid[0][-1]):
            raise ValueError("release provenance signer mismatch")
        manifest = json.loads(unsigned.read_text())
    expected = {"schema_version": 1, "repository": "epsalmond/oshioki",
                "tag": identity["tag"], "version": identity["version"],
                "source_commit": identity["commit"], "tag_object": identity["tag_object"]}
    if set(manifest) != set(expected) | {"sha256"} or any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("release provenance source/tag identity mismatch")
    version = identity["version"]
    names = {f"oshioki_{version}_amd64.deb", f"oshioki-macos-arm64-{version}.tar.gz", "SHA256SUMS"}
    if set(manifest["sha256"]) != names:
        raise ValueError("release provenance asset set mismatch")
    for name in names:
        if manifest["sha256"][name] != digest(downloads / name):
            raise ValueError(f"release provenance hash mismatch: {name}")
    return {"primary_fingerprint": fingerprint, "sha256": digest(signed)}
