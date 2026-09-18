# M2 local implementation decisions

- Baseline: merged M1 `33a7472f06516753c5c66d76eac8e60538c2660f`, schema 6/session 1. Historical schema-5 evidence remains a migration fixture, not the current installation baseline.
- T03 imports only the selected contract and three fixture artifacts from preparatory `fff6c3f3183d352f9c02244541fd339adb4b0a18`; no old integration history is replayed. Freeze requires executable strict consumers and independent architecture/security review.
- Engine schema/migration/retrieval has one owner. App registries have one owner. Client work may split by package only after API fixture freeze.
- Full verification and native/package checks run through the supervising parent's serialized check requests. Candidate HEAD must match each receipt.
- M2 completion is local only. Publication, live data, later lifecycle operations and optional model research remain outside this milestone.

T03 is verified as a contract submilestone. The exact frozen artifact bundle is `868d3cd2ad6ce1fa7f375151df3a5cf7553ba3165c3fefa409fe95cb6258c37c`; [T03-FREEZE.json](T03-FREEZE.json) records version allocations, review and parent check receipts. T04–T08 were pending at that freeze; later checkpoints below record implementation evidence. An external read-only T07 audit confirms clients must wait for T06 APIs; reuse its source map without treating it as implementation evidence.

T04 compatibility: a legacy public-job changed-delivery request has no upstream revision key, ordering evidence or expected-head CAS. After the new storage contract activates, it must enter existing durable conflict custody instead of overwriting the current source. An unchanged retry keeps its original receipt. New explicit revision intake supplies the frozen ordering and CAS evidence; this does not claim legacy collectors automatically support ordered updates.

Migration inputs were constructed using actual historical writers at schema-5 `3376a7674ec27c88862f4e2f6ebed4b17620a675` and schema-6 `33a7472f06516753c5c66d76eac8e60538c2660f`. Each extended synthetic case retains six captures, four current SQL heads, two connection namespaces with an equal external key, two revisions of an ordered mixed-source publication and a managed workspace. Each historical export validates with 22 files. Private receipts retain exact byte inventories. These are migration inputs and exploratory backend evidence; activation, current export APIs, comprehensive interruption and old-writer refusal remain separate T04 gates.

The reviewed T06 bridge client fixture is frozen for disjoint T07 work in [T06-BRIDGE-FREEZE.json](T06-BRIDGE-FREEZE.json). MCP repairs and actual engine-wired acceptance remain open; this is not completion of T06.

T04 source/migration is locally verified at `c320ff02b6f3bc375045f32e2437deea6343946e`; [T04-CHECKPOINT.json](T04-CHECKPOINT.json) records independent review and the complete parent verification receipt. Schema 7/session 2 and Portable 4 export now execute. Historical schema-5/schema-6 source bytes, ordered publication evidence and supported old imports remain intact. Final native/package acceptance and T05–T08 completion remain open.

M2 implementation and automated/native verification are recorded in [M2-CHECKPOINT.json](M2-CHECKPOINT.json). Current/history grants remain independent; cross-runtime bridge cursors bind a trusted runtime identity. Canonical reads verify retained publication bytes against independent approval evidence. Canonical predecessors follow durable review bindings, with iterative traversal and explicit diagnostics for independent historical roots.

Relationship decisions retain exact revision endpoints and append-only decisions in an optional, independently versioned Portable 4 sidecar. A distinct supported catalog digest preserves validation of prior source-only v4 exports. Existing schema 7 DDL and frozen task/bridge artifacts are unchanged. New Portable 4 restore remains excluded.

Verification retains the complete suite at the application checkpoint and reruns affected native gates after the smoke-only compatibility repair. The parent corrected a proof-checker field-name mismatch without changing the product or rebuilding unchanged artifacts. Obsidian GUI acceptance remains an observed manual gate; the ready artifact/profile does not certify that gate. Desktop GUI acceptance is deferred by the subsequent user decision below.

## Core delivery priority, 2026-09-17

User direction supersedes desktop release/GUI gating: prioritize CLI and MCP, preserve completed desktop work, defer further desktop features and release preparation, and keep shared-engine verification, compatibility checks and Obsidian scope. The plan records desktop-only deferrals across T07, T11–T14, T18–T21 and T23. No M3 implementation or publication is authorized by this change.
