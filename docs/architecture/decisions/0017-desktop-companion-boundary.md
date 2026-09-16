# ADR 0017: Separate desktop companion and collector boundaries

Status: accepted product boundary; implementation acceptance is tracked separately

Date: 2026-09-14

## Context

The owner selected a dedicated desktop companion for local use and source setup. Requiring
Obsidian does not meet that outcome. A desktop window and an optional collector also have different
lifetimes: closing a window must not be the hidden switch that controls recurring imports.

[ADR 0015](0015-homebrew-only-distribution.md) and
[ADR 0016](0016-foreground-runtime-package-boundary.md) remain authoritative for the core CLI
distribution. This decision scopes their guarantees to that distribution and defines separate
companion boundaries. It does not restore archived appliance code.

## Decision

The core remains an unprivileged foreground application with inherited stdio CLI/MCP/plugin
interfaces, SQLite, and ordinary files. Installing it does not install a desktop app, collector,
scheduler, network listener, or service manager dependency.

`packages/desktop` is a separately versioned Tauri 2 application with React and TypeScript. Its
native Rust host owns a bounded stdio child and exposes named operations to packaged interface
assets. The renderer receives no database handle, general shell interface, arbitrary executable
selector, or raw credential retrieval capability. Normal app exit must stop its owned process
group, including descendants that outlive the immediate child.

D0 is a synthetic native proof. It does not open the owner's existing Brain or configure accounts.
D1 adds the usable local interface and shared desktop/headless agent setup. GitHub collection belongs to D2; independent
collection belongs to D3. The full scope is in the
[desktop plan](../../plans/2026-09-14-desktop-companion.md).

### Runtime and recovery

The desktop owns an exact paired core/Graphify runtime. A versioned component manifest binds the
desktop version, core version, target, protocol, component roles, and executable digests. Both
components must work from their packaged locations. Merely hashing an unreachable helper is not
distribution evidence. Startup never repairs or replaces an installed Homebrew CLI.

All participating foreground clients register their lifetimes through the app bootstrap. Admission
and abandoned-session recovery are serialized. A healthy second client must preserve a live
client's consent and pending inference. Actual crash recovery remains conservative and revokes
consent; clean final shutdown also ends session authority. Durable recovery evidence must survive
an interrupted admission or shutdown until recovery completes. A registry lock has a deadline.

These guarantees require participating clients. An older installed runtime that does not register
its lifetime can still run the older recovery path. Matching a version string alone does not prove
compatibility. Before D1 opens an existing Brain concurrently with installed clients, its setup must
enforce a tested runtime/schema compatibility floor. A runtime must reject a newer unsupported
SQLite schema without mutating it. D1 uses private state schema 4 and runtime session version 1,
with migration under exclusive participating-client admission. Desktop setup points clients at its
exact bundled executable. Stop old sessions before upgrading; existing installed runtimes must be
updated to read the new schema. Product version 0.1.0 alone is not the compatibility check.

The stdio bridge has bounded writes, responses, deadlines, and a 2,000-request session limit.
Cold runtime startup gets a separate bounded handshake allowance. Session exhaustion is explicit.
A retry of an uncertain capture must keep its original operation ID; reconnection never restores
provider consent or replays owner mutations automatically.

### Optional collector

The future collector is a separate package and process, outside the core dependency closure. It
owns source authentication, provider requests, cursors, scheduling, and optional per-user service
registration. Credentials belong in the OS credential store. Without that store, only session
operation is available and unattended collection is disabled.

The selected collector control transport is a private Unix-domain socket in an owner-only directory,
with owner-only socket permissions and one lock-held instance. D0 proves those local transport and
ownership primitives using disposable fixtures; D3 must prove the actual control protocol, durable
pause acknowledgement, reconnection, and service lifecycle before enabling collection. Filesystem
permissions protect against other OS users and provide no hostile-same-user isolation claim.

Collection and launch at login are separate opt-ins. The desktop shows whether collection continues
when its window closes. Quit stops desktop-owned children; stopping an independently enabled
collector is a separate action. Laptop-local collection does not run while the laptop is asleep or
the user is logged out. Disabling a collector preserves Brain content.

Source records enter through validated, untrusted intake with stable delivery identities. They do
not gain owner authority by using the existing desktop capture operation. Source capture permission
and AI-client read permission remain separate; current MCP search reads the whole Brain.

## Distribution and release gates

The CLI keeps its Homebrew lifecycle and existing preservation smoke test. Bundling exact components
is the selected desktop architecture because it avoids a mutable PATH dependency and permits
independent desktop upgrades. Public desktop distribution is not part of D0.

On macOS arm64, contributors build a local `.app` and prove native capture, search, helper discovery,
and cleanup. Local ad hoc signatures are development evidence. A browser-delivered desktop release
requires a separate Developer ID signing and notarization gate, following
[Tauri's macOS signing guidance](https://v2.tauri.app/distribute/sign/macos/).

The defined Linux path is a native `x86_64-unknown-linux-gnu` build containing the matching Linux
core and Graphify components, packaged as an AppImage. Build on the oldest supported base image and
test the result on a clean target with the required WebKitGTK and system libraries. See
[Tauri's Linux prerequisites](https://v2.tauri.app/start/prerequisites/#linux) and
[AppImage guidance](https://v2.tauri.app/distribute/appimage/). macOS compilation and a documented
Linux recipe do not establish Linux desktop support. The clean-host native UI and helper checks
remain a distinct Linux release gate.

## Alternatives

| Approach | Tradeoff |
|---|---|
| Exact bundled runtime, native host, separate optional collector | Selected. Each product owns its dependencies, child lifetime, and release evidence. |
| Discover and invoke the installed Homebrew CLI | Smaller app, but upgrades and older concurrent clients can change protocol and recovery behavior independently. Not selected. |
| Electron with a sandboxed renderer and narrow preload | Viable fallback if Tauri cannot meet the native gates; adds a Chromium/Node runtime. |
| SwiftUI on macOS and a separate Linux interface | Strong platform integration, but requires maintaining two interfaces. |

## Consequences

The core's foreground and archive-exclusion guarantees remain testable against its own installed
artifact. The desktop adds native bridge, packaging, and UI verification without adding a service
dependency to that artifact. A future collector must earn its own permission and lifecycle evidence.
Historical ADRs and acceptance reports retain their original results; they do not certify the new
desktop or future sources.
