# Workstream State

- ID: `20260918-open-brain-public-m3-source-continuity-owner-control-c63538`
- Repository: `open-brain-public`; branch: `ob-new-user/m3`.
- T20 baseline: `2ea2e66473915e7b7864bd8023b0ca42f4eb691f`.
- Verified implementation HEAD: `907bfe737b9366cafdb47ffb763f72df8dcf5f32`.
- Core v0.1 T09, T19 and narrowed T20 are locally verified; prior checkpoint evidence is preserved.
- Current evidence: [T20-CHECKPOINT.json](T20-CHECKPOINT.json); [artifact contract](../../../design/core-v01-t20-artifacts.md).
- PASS: 2422 Python/5 existing skips; 337-file MyPy; four package builds; 291 Obsidian, 259 desktop TS, 1 desktop Python, 26 Rust; macOS native/Homebrew and independent delta review READY.
- Exact artifact hashes bind the verified implementation. Later checkpoint-only documentation is identified by the private final exact-HEAD handoff.
- T09/T19 product behavior, schema 7/runtime session 2, Obsidian scope and completed desktop compatibility remain unchanged.
- All children released; shared child-session ledger remains private.
- Linux native/Homebrew, Obsidian GUI and public promotion are open gates; global A15 acceptance is not claimed.
- T10–T18/T22 and desktop release remain deferred. T21 and T23 remain unimplemented.
- Next: Start T21 in a fresh thread using this exact checkpoint. Do not start T23 here. Push/PR/merge/release, live sources, OAuth, real services and model downloads remain separately gated.
- Raw logs, prompts, reviews, runner receipts and measured usage remain private.
- Fresh-thread safety requires the final private exact-HEAD HANDOFF.json validation and clean checkout.
