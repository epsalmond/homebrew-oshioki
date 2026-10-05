# homebrew-oshioki

Homebrew tap for [Oshioki](https://github.com/epsalmond/oshioki).

```bash
brew install epsalmond/oshioki/oshioki
```

## Bottling a release

From the v0.4.1 formula update onward, every introduced tap PR commit must be
signed. Upstream release tags must be annotated and signed by the public SSH
key in [.github/signing](.github/signing). The current v0.3.1 formula remains
available while the first signed upstream release is prepared.

After a signed upstream release is published, dispatch `bottle` from `main`
with its stable tag (v0.4.1 or later). The workflow checks the signed tag,
Cargo version, and main ancestry with committed bootstrap policy. It uses the
latest canonical-main SSH revocations and checks clearsigned
`RELEASE-PROVENANCE.json.asc` against the independently pinned APT primary
key in `.github/signing/archive-key.asc`, fingerprint
`AA93668ABF0040387C7EB51C1632F50E2226409D`. Provenance must bind the exact
repository/version/tag object/source commit and Mac archive, Debian package,
and SHA256SUMS hashes. The archive's source revision remains a consistency check.
It builds the generated formula in a local `epsalmond/oshioki` tap, tests its
bottle, and emits the `signed-formula-candidate` artifact. It never pushes
tap commits. Set `upload` only when ready to publish the bottle; that separate
job can upload absent assets or accept byte-identical retries. Conflicting
published bytes fail without replacement.

Download the artifact, verify `SHA256SUMS`, and review `provenance.json` and
`formula.patch`. Copy its complete `Formula/oshioki.rb` into a branch based
on the recorded tap base commit, then commit and submit a human-signed PR.
Include the workflow run URL, upstream tag object/commit, and archive/bottle
digests from provenance in the PR description. If the tap changed meanwhile,
review the introduced commits and validate the resulting formula again. Main
advancing alone does not require a rebase; only commits not reachable from the
current trusted base are signature-checked. The PR checks
verify the actual published URL/hash and test both the submitted bottle and
an archive-to-bottle round trip; they do not substitute source `main` for a
signed release candidate.

Setup (once): add a `RELEASE_TOKEN` secret to this repo — a fine-grained
personal access token with contents read/write on `epsalmond/oshioki` so the
workflow can upload the bottle to that repo's release.

GitHub's contents write scope can also change upstream source. Before exposing
this token, independently review its actual scope and active upstream signed
commit/main/tag immutability controls with no token bypass, then record
`BOTTLE_UPLOAD_AUTHORITY_REVIEWED=true` as a repository variable. The upload
job fails closed while that prerequisite is absent. Keep this receipt current
when token scope or rules change. It needs no tap write permission or signing
private key. The separate APT signature authenticates a source/build/hash
provenance assertion; it is not an independently reproducible build proof.

## Signing and recovery

The initial key fingerprint is
`SHA256:TzhZQs7IkRk41Y1UxiNVZfLRJBO8MMAte5YEhQHmfdw`, principal
`epsalmond@gmail.com`, namespace `git`. Verify that public key independently
before the bootstrap PR merges, and independently confirm the APT key pin.
The required `Trusted PR signatures` check uses `pull_request_target` and
only its base's workflow/verifier/key. It fetches PR Git objects as inert data,
never checks out or executes PR scripts/tests/formula code, and has read-only
permissions with no secrets. Candidate tests/builds use ordinary
`pull_request` jobs with distinct names (`Candidate signing tests`,
`release-identity`, `release-round-trip`, `bottle-round-trip`). Confirm observed
check names before configuring required statuses. The bootstrap PR itself
needs exact-tree/signature review because this base-owned gate first becomes
available after merge. Configure Git SSH signing on the human signer host; no private key
belongs in Actions. GitHub commit `Verified` and the local pinned-key gate
are separate checks. Existing reachable unsigned history is grandfathered.

PR shared ancestry must already include the trusted bootstrap. Routine
downgrades and stale generated candidates fail. The legacy path is allowed
only when a precheckpoint trusted base has identical release URL/hash/version
and bottle data; a candidate cannot select it after main advances to v0.4.1+.
After a security fix, recovery must retain that fix and use the current or a
newer corrected version. These checks depend on reviewed main/ref governance;
an authorized maintainer can still deliberately change policy in a signed PR.
GitHub-generated merge/squash signatures and final-main tree equality are a
separate acceptance check; this PR gate does not verify a future merge result.

After the signed v0.4.1 formula PR is merged, create an annotated signed tap
`v0.4.1` tag on that exact checkpoint. `signing` checks tap tag signatures;
the tap needs no separate GitHub release. Rulesets are configured separately
after bootstrap review; this code neither enables them nor adds a bot bypass.

For key rotation, review the replacement public key independently and sign
the policy change with the currently trusted key. This minimal policy pins
one active key; preserve the old checkpoint hashes and public key for
historical verification. Compromised public keys belong in `revoked_keys`.
Upstream SSH rotation also requires a reviewed tap pin update. APT signing
subkey renewal keeps the independently approved primary fingerprint and needs
a reviewed public-key refresh; a primary-key change needs explicit provenance
and client-keyring migration. Never take a replacement key from release assets.
Lost-key recovery needs an explicit out-of-band trust decision. Keep published
tags and bottle bytes immutable; use a new version after a faulty publication.

Offline checks: `python3 -m unittest discover -s tests -v`,
`ruby -c Formula/oshioki.rb`, and `actionlint .github/workflows/*.yml`.
