# M2 local implementation decisions

- Baseline: merged M1 `33a7472f06516753c5c66d76eac8e60538c2660f`, schema 6/session 1. Historical schema-5 evidence remains a migration fixture, not the current installation baseline.
- T03 imports only the selected contract and three fixture artifacts from preparatory `fff6c3f3183d352f9c02244541fd339adb4b0a18`; no old integration history is replayed. Freeze requires executable strict consumers and independent architecture/security review.
- Engine schema/migration/retrieval has one owner. App registries have one owner. Client work may split by package only after API fixture freeze.
- Full verification and native/package checks run through the supervising parent's serialized check requests. Candidate HEAD must match each receipt.
- M2 completion is local only. Publication, live data, later lifecycle operations and optional model research remain outside this milestone.

No M2 task is verified yet. Exact frozen hashes and submilestone checks will be recorded as each gate completes.
