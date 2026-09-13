# Open Brain as a first-class Prism Harness plugin

## Purpose

Open Brain is a standalone open-source product that Caleb built. It is not a component of Prism Harness and should not be absorbed into the Prism repository. It should also be an official, first-class Prism memory plugin.

These roles are compatible:

- Open Brain remains useful on its own, with its own repository, releases, CLI, services, architecture, and roadmap.
- Open Brain owns and ships its Prism integration.
- Prism defines the generic plugin contract and conformance tests.
- Prism can feature Open Brain as its reference memory implementation without depending on it.

## Current Open Brain boundaries

Open Brain is already more than a memory database. It is a local-first capture, provenance, redaction, retrieval, review, ledger, and operations system built as a ports-and-adapters monolith.

The repository already has useful integration boundaries:

- `open-brain-secure-node-mcp` is the retained bounded, work-only stdio MCP service. It requires an
  explicit Secure Node installation and must not become an implicit dependency of default Open Brain.
- The MCP adapter exposes `brain_query` and `brain_retrieval_feedback`.
- Retrieval uses the typed `WorkRetriever` and `RetrievalFeedback` ports.
- Results are bounded and redacted before crossing the work-context boundary.
- Open Brain has an authenticated, loopback-default, read-only HTTP/UI boundary.
- Private content, credentials, runtime state, and deployment configuration stay outside the public repository.
- The project is licensed under Apache-2.0.

The first Prism integration should reuse these boundaries instead of creating a parallel Open Brain API.

## Product relationship

The intended ownership is:

```text
open-brain/
├── Open Brain application
├── open-brain CLI
├── optional Secure Node MCP service
└── official Prism memory-plugin entrypoint

prism-harness/
├── trusted kernel
├── memory-plugin contract
├── renderer-plugin contract
├── plugin conformance test kit
└── documentation featuring Open Brain
```

Prism should not copy Open Brain source code or private data into its repository. Open Brain should not make its core domain depend on Prism. The Prism-specific adapter belongs at Open Brain's existing integration or service boundary.

## What “first-class plugin” means

Open Brain is first-class when all of the following are true:

1. Open Brain owns and releases the plugin from its standalone repository.
2. Prism recognizes it as a real `memory` plugin rather than an improvised generic tool.
3. Open Brain's CI runs Prism's official memory-plugin conformance suite.
4. Prism provides a supported installation and configuration path for it.
5. Prism documents Open Brain as a reference integration while remaining fully usable without it.

A target developer experience could look like:

```bash
prism plugin install open-brain
prism run --memory open-brain
```

The exact command names remain a Prism CLI design decision.

## First plugin slice

The first release should expose only Open Brain's existing work-context retrieval boundary:

- Query bounded, redacted work context.
- Return typed retrieval results with trust information.
- Record allow-listed retrieval feedback.

This maps naturally to the existing `brain_query` and `brain_retrieval_feedback` behavior.

The first slice should not grant general capture, review, administrative, or filesystem authority. Those are separate powers and should become separate capabilities only when their contracts and approval requirements are explicit.

## Implementation options

### Option 1: MCP adapter bridge

Create a small Prism memory plugin in the Open Brain repository. For users who explicitly select
Secure Node, it may translate Prism memory requests into calls to the existing
`open-brain-secure-node-mcp` process. A default-product bridge needs its own daemonless local read
entry point and cannot silently install the Secure Node extra.

Advantages:

- Reuses a working and tested privacy boundary.
- Requires the least new Open Brain surface area.
- Provides the fastest path to a real end-to-end integration.

Tradeoffs:

- Adds a protocol translation layer.
- May require a small Node entrypoint if Prism's subprocess runner remains Node-specific.
- Proves the developer experience before proving a language-neutral plugin architecture.

### Option 2: Native language-neutral subprocess plugin

Define Prism's subprocess plugin protocol as bounded JSON messages over stdin and stdout, independent of implementation language. Open Brain then ships a Python entrypoint such as:

```text
open-brain-prism
```

That entrypoint implements Prism's memory contract directly through Open Brain's existing `WorkRetriever` and `RetrievalFeedback` ports.

Advantages:

- Open Brain needs no Node wrapper.
- Prism can support plugins written in Python, Rust, Go, or other languages.
- The integration demonstrates a credible open-source plugin ecosystem rather than a JavaScript-only extension mechanism.
- Open Brain's core remains independent from Prism.

Tradeoffs:

- Requires a broader Prism protocol and runner design.
- Needs language-neutral conformance fixtures and process-lifecycle rules.
- Takes longer than the MCP bridge.

This is the recommended architecture. The MCP bridge remains a useful first delivery slice if the native protocol is not ready.

## Dependency direction

The dependency direction should remain:

```text
Open Brain core
      ↑
Open Brain Prism adapter
      ↓
Prism memory protocol
```

Prism must not depend on Open Brain. Open Brain's core must not depend on Prism. Only the Open Brain adapter depends on Prism's public protocol or SDK.

## Permissions and trust

The initial plugin should receive only the authority needed for work-context retrieval and feedback. A future capability model may distinguish powers such as:

```text
memory.work.read
memory.feedback.write
capture.submit
review.decide
memory.admin
```

The last three should not be included in the initial plugin grant. In particular, a provider or ordinary harness run must not gain silent authority to capture content, approve reviews, or rewrite durable knowledge.

Prism's trusted kernel, plugin admission, capability enforcement, and arbitration remain core code. They are not plugins.

## Prism UI plugin

A Prism user interface can be a separate `renderer` plugin. It should display structured harness events and submit user actions through a stable Prism protocol.

The Prism UI should not:

- Call Codex or another provider directly.
- Read Open Brain's storage directly.
- Receive credentials or unrestricted filesystem access.
- Combine renderer authority with memory administration.

Open Brain's existing read-only UI and a future Prism renderer solve different problems. Open Brain's UI presents Open Brain pages. A Prism renderer presents harness runs, approvals, tool activity, provider output, and plugin state.

## Open-source boundary

The public Open Brain repository can include:

- The Open Brain application and adapters.
- The official Prism plugin entrypoint.
- Prism conformance tests and fixtures.
- Synthetic example data.
- Installation and configuration documentation.

It must continue to exclude:

- Private brain content and indexes derived from it.
- Credentials and authentication material.
- Machine-specific private configuration.
- Live runtime state, migration evidence, and cutover receipts.

## Completion criteria

The Open Brain integration can be called a first-class Prism plugin when:

1. A clean installation can launch Open Brain through Prism on a supported Mac.
2. Prism admits the exact pinned Open Brain plugin artifact before launch.
3. The plugin passes Prism's memory-plugin conformance suite in mocked CI.
4. A live acceptance test returns bounded, redacted work-context results from Open Brain.
5. Documentation explains installation, permissions, privacy boundaries, failure behavior, and removal.

## Recommended sequence

1. Freeze the minimal Prism memory request, result, error, and feedback contract.
2. Decide whether the first executable uses the MCP bridge or the native language-neutral protocol.
3. Implement the plugin in the Open Brain repository against existing retrieval ports.
4. Add Prism conformance tests to Open Brain CI and mocked Open Brain tests to Prism CI.
5. Run one live acceptance test on a trusted Mac without copying authentication or private data into either repository.
