# homebrew-oshioki

Homebrew tap for [Oshioki](https://github.com/epsalmond/oshioki).

```bash
brew install epsalmond/oshioki/oshioki
```

## Bottling a release

After the oshioki release workflow publishes a tag, dispatch the `bottle`
workflow here with that tag (e.g. `v0.1.0`). It points the formula at the
release tarball, builds the bottle on `macos-14`, uploads it to the oshioki
release, and commits the bottle block back to the formula.

To observe a bottle run that has already been dispatched, take its run ID from
the Actions run page and wait on that exact run. On a maintainer machine with
the shared waiter installed, run:

```sh
wait-for-status --repo epsalmond/homebrew-oshioki gh-run <run-id>
```

This command reports the selected run's result and failure diagnostics. It
does not dispatch a workflow or change a release.

Setup (once): add a `RELEASE_TOKEN` secret to this repo — a fine-grained
personal access token with contents read/write on `epsalmond/oshioki` so the
workflow can upload the bottle to that repo's release.
