# NW0 async transport proof

This synthetic probe tests the experimental direct-API transport on macOS arm64 and native Linux
x86_64. It does not enable a provider, install a plugin, change application dependencies, or approve
the full NW0 goal.

The fixtures serve provider-shaped responses through owned loopback DNS and HTTPS servers. TLS
certificates and keys are generated for each test process and are not committed. The selected
transport has no real endpoint configuration in this probe. Twenty-three checks run normally and
with Python optimization, covering output/schema rejection, cancellation, deadlines, attribution,
cleanup and task-inventory negative controls.

Run from the repository root with CPython 3.14 and the project's pinned uv available:

```sh
uv run --frozen python -m tools.nw0_api_probe.run
```

Use a new `--output build/nw0-api-next` for another run. Existing output directories are refused.
`--wheelhouse` selects a previously downloaded compatible wheel directory for offline installation.
Otherwise installation uses only the official PyPI index. The exact hash lock admits the reviewed
macOS arm64 and Linux x86_64 wheels; no source distributions or unpinned dependencies are allowed.

Dependencies install into the new proof directory, not the repository environment. In particular,
the synthetic TLS fixture uses cryptography 46.0.5 and cffi 2.0.0, while the application development
environment uses newer versions. This is a test-only lock, not a shipping dependency choice.
`dependency-wheels.json` records artifact hashes, sizes, compatibility and bundled license paths.

Experimental source inputs live under `payload/*.py.txt` and become executable Python only in the
new proof directory. The source manifest binds their exact bytes. The typed coordinator has normal
repository lint, type and unit checks; the materialized inputs run their actual transport suite.
They remain outside the installed application and its type surface.

The coordinator reuses the existing bounded proof-command runner for installation and test-process
supervision. It retains local bounded logs and emits `release/verification.json` even after failure.
Only that path-free summary is suitable for CI upload. It omits local command paths, raw errors,
TLS keys and test logs. A successful summary requires both complete test modes and known child
cleanup, not just a zero exit status.

The summary identifies the coordinator, source manifest and shared command runner by SHA-256.
Verified input bytes are materialized once; installation and version checks use that snapshot.
CI runs this proof in separate macOS arm64 and Linux x86_64 jobs and uploads only the summary.

Wheel compatibility and a Mac run do not prove Linux execution. This probe also does not establish
Engine authority integration, real credentials or providers, kernel connect/write stall coverage,
Claude subscription isolation, desktop behavior, or the complete user journey. Those remain
separate NW0 gates. Retain failed receipts; do not select only favorable runs.
