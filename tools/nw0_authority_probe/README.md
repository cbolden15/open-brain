# NW0 authority proof

This synthetic proof exercises Engine authority, credential preflight, cancellation, terminal
ownership and host shutdown on CPython 3.14. The fixed targets are macOS arm64 and Linux x86_64.
Forty tests run normally and with Python optimization against owned loopback HTTPS fixtures.
Each process creates its own temporary TLS material. Provider calls remain zero.

Run from the repository root:

```sh
make nw0-authority-proof
```

The output defaults to `build/nw0-authority`. Existing output directories are refused. Use
`uv run --frozen --python 3.14 python -m tools.nw0_authority_probe.run --output build/authority-next`
to select a fresh directory. `--wheelhouse` selects an offline directory containing exactly the
fourteen wheel archives for the current target. Otherwise the coordinator downloads each pinned
archive from its fixed HTTPS URL on files.pythonhosted.org, with size and time limits.

The coordinator copies verified Engine and app source into a clean snapshot, checks exact wheel
bytes, and builds the authority bundle without installing dependencies into the application.
`source-manifest.json` binds the candidate, runner, dependency metadata and expected bundle hashes.
`expected-bundle-manifests.json` supplies an external manifest anchor for each target. The bundle
checks those anchors and all extracted dependency bytes before executing its isolated children
with `-I -S -B`. Repository bytecode caches are excluded from the clean source snapshot; loadable
bytecode in the resulting bundle is rejected.

The preserved candidate `requirements.txt` describes its original thirteen dependencies. Bundle
construction adds the pinned rfc8785 wheel required by Engine imports, producing a fourteen-wheel
runtime. `dependency-wheels.json` records both target inventories, sizes, hashes and licenses.
These proof dependencies and text payloads remain outside the installed application.

The parent and bundle use bounded child supervision. Success requires the exact source and
dependency bindings, a successful metadata preflight, forty tests in each mode, untruncated logs,
and known process cleanup. Local receipts and logs stay in the output directory. CI uploads only
`release/verification.json`, a projection containing fixed identifiers, hashes, counts and cleanup
results. Local paths, commands, errors, test logs and TLS material are omitted.

The native CI matrix runs both targets independently. A local Mac result does not establish Linux
execution. This proof does not establish real-provider behavior, kernel connect/write stalls,
subscription isolation, desktop interaction or the full NW0 user journey. Those gates retain
their separate evidence and review requirements. Preserve failed runs alongside successful ones.
