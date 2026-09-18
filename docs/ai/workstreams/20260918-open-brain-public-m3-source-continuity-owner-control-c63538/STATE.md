# Workstream State

- ID: `20260918-open-brain-public-m3-source-continuity-owner-control-c63538`; repository: `open-brain-public`; branch: `ob-new-user/m3`.
- T21 baseline: `6ab6863b0f3b33d918cbbe4a4ea0005c2fe9b0b7`.
- Verified implementation HEAD: `987dea814a4b8730f8b3b73e19bb35262f6a6044`.
- T09, T19, T20 and T21/scoped A16 are locally verified; prior checkpoint evidence is preserved.
- Current evidence: [T21-CHECKPOINT.json](T21-CHECKPOINT.json); [documentation contract](../../../design/core-v01-t21-documentation.md).
- PASS: 2431 Python/5 existing skips; 339-file MyPy; four package builds; 291 Obsidian, 259 desktop TS, 1 desktop Python, 26 Rust; 11 exact artifact examples; independent delta READY.
- Hash-pinned T20 macOS artifacts were reused; their runtime build inputs remain unchanged.
- No runtime/schema changes, source services, desktop release or T23 implementation occurred.
- Public handoff head names the fully verified implementation; the private final handoff binds the later checkpoint-only commit to exact HEAD.
- All children released. Raw logs, reviews, prompts, runner receipts and measured usage remain private.
- Linux exact-candidate native/Homebrew, Obsidian GUI and public promotion remain open. T10–T18/T22 and desktop release remain deferred.
- Next: Start T23 in a fresh thread using this exact T21 checkpoint. Do not start T23 here. Push/PR/merge/release, live sources, OAuth, real services and model downloads remain separately gated.
- Fresh-thread safety requires the final private exact-HEAD HANDOFF.json validation and clean checkout.
