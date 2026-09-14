# Open Brain Desktop D0

This is a native integration proof, not the source-connection product. It creates a disposable
synthetic Brain, checks its packaged runtime pair, captures and searches one note, exercises
Graphify, and stops the owned children. It does not open an existing Brain, connect an account,
configure an AI client, or install a collector.

## Build and verify

Run commands from the repository root. Install the core contributor prerequisites, stable Rust,
and [Tauri's platform prerequisites](https://v2.tauri.app/start/prerequisites/).

```sh
uv sync --frozen --group dev --group native-build
make verify
make native-audit homebrew-smoke desktop-native-proof
```

The final command shares the native-build prerequisite within one Make invocation. Do not run
another Homebrew smoke check concurrently. The desktop build verifies executable digests against
the core component manifest and keeps the helper in the `runtime/libexec` location expected by
the core in `runtime/bin`.

On macOS arm64, the app is at:

```text
packages/desktop/src-tauri/target/release/bundle/macos/Open Brain Desktop.app
```

Open it and check that the native window reports successful capture, one search match, Graphify
success, and confirmed cleanup. Use **Run proof again** to repeat against another disposable Brain.
The headless JSON proof is a separate check and does not establish that the webview rendered or
that quitting during an active operation stops its children.

The first packaged Python startup can take several seconds. Its handshake has a 15-second deadline;
later local capture/search requests have five-second deadlines. The Rust failure tests retain short
deadlines to prove that a stalled child is terminated. No provider request is made by the proof.

## Component boundaries

The renderer can invoke the native proof command and has no general shell or database interface.
The native host launches only the packaged component paths, checks their digests and protocol, and
owns their process groups. Once shutdown starts, it admits no new child. A malformed response or
deadline closes the bridge. Retrying an uncertain capture requires the original request ID.

The synthetic directory lives outside the app bundle and is removed when the proof finishes.
Packaged resources are not writable application state. An installed Homebrew CLI is neither
discovered nor modified by desktop startup.

The Python fixture in `tests/test_control_boundary.py` proves private Unix-domain control transport
and one lock-held instance. It belongs to the optional desktop scope; the core test suite continues
to prohibit socket access. This fixture does not implement the collector protocol or scheduling.

## Linux and public distribution

`make desktop-native` selects an AppImage build on Linux x86_64 with a matching Linux runtime pair.
The contributor needs WebKitGTK and Tauri's Linux dependencies. Validate the AppImage on a clean
target host before claiming Linux desktop support. The macOS-only `desktop-native-proof` target
deliberately does not stand in for that gate.

D0 creates local development artifacts. Public macOS desktop distribution still needs Developer ID
signing, notarization, and installation evidence. Mixed use of an existing Brain with older installed
clients also needs an enforced compatibility floor before D1 onboarding. See
[ADR 0017](../../docs/architecture/decisions/0017-desktop-companion-boundary.md) and the
[ordered desktop plan](../../docs/plans/2026-09-14-desktop-companion.md).
