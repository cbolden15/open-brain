# NW0 unprivileged transport proof

This proof tests whether an ordinary Open Brain process can own DNS resolution and a bounded TLS request on macOS arm64 and Linux x86_64. It uses synthetic loopback fixtures, makes zero provider calls, and does not activate application transport code.

The candidate owns one unshared aiodns resolver, its pycares channel, the TCP socket, TLS stream, request task, cancellation watcher, body release, and cleanup task. Receipts cover resolver entry and completion, TCP connect entry and completion, TLS completion, send entry, writer-drain completion, cancellation, and terminal close. The send receipt proves the maintained Python writer seam completed; it does not claim kernel acknowledgement. The stronger product design remains the E42 platform-backend interface, including Network.framework on macOS where stack processing completion is available.

aiodns 4.0.4 calls pycares 5.0.1 `Channel.close()`, but that version leaves its module-global channel-destruction worker waiting forever. E43 rejects the unpatched dependency. The proof applies `patches/pycares-5.0.1-terminal-shutdown.patch` only after verifying the exact upstream module hash. The patch serializes channel registration, destruction, and final stop; waits for each owned channel's destruction; sends an explicit stop sentinel only after no live channel or queued destruction remains; and joins the worker within the operation deadline. A source mismatch, premature worker stop, or residual thread fails the proof.

`dependency-wheels.json` freezes the macOS arm64 and Linux x86_64 wheels, embedded c-ares 1.34.6, licenses, native members, and loader dependencies. aiohttp 3.14.3 is recorded as excluded from the E43 runtime because E42 rejected its shared resolver and private send path. Dependencies install below the new proof output directory with hashes required. They do not enter the project environment or application dependency lock.

Run from the repository root with CPython 3.14 and uv 0.12.8 or later:

```sh
uv run --frozen --python 3.14 python -m tools.nw0_unprivileged_transport_probe.run
```

Pass a new `--output` path for a second run. `--wheelhouse` selects a reviewed local wheel directory and disables index access. The runner executes 13 controls in normal mode and 13 under `python -O`, then executes one structured runtime pass. It publishes only `release/verification.json`; raw install, patch, and test logs stay outside the release directory.

The runtime covers resolver success, timeout, caller cancellation, concurrent close, captured configuration, malformed replies, and a deterministic two-resolver close interleaving. The interleaving closes the first resolver while the second remains live, proves the worker remains available to the second resolver, then proves the second close performs the only final worker stop. Transport controls cover a successful TLS request and cancellation during a stalled TLS handshake. Before and after process inventories must match for asyncio tasks, file descriptors, and threads. The process must have matching real/effective IDs and a nonzero effective UID. Root, sudo, namespaces, Linux capabilities, and fixture-owner processes are forbidden.

A passing E43 receipt is feasibility evidence for an unprivileged candidate. Product integration, real credentials, provider traffic, the 32-attempt matrix, and full NW0 completion remain closed until both platform receipts and an independent review approve the next gate.
