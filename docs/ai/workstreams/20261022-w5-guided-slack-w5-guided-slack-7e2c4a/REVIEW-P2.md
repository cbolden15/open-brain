# P2 review (independent)

Verdict: PASS_WITH_FIXES. No crash interleaving found that produces a duplicate import, double preview fetch, or altered-request replay. Journal v1 modes/no-follow/size/atomicity/closed schema and the exact macOS alias allowlist conform.

Fixes sent back to the P2 implementer (resolved before P3 starts):
1. Important: refused import / import replay treated as proof of non-commit (`source_guide_flow.py` ~674, ~1384, `_unresolved` ~525). Must halt unless metadata proves the same `preview_id` is still pending; add replaced-`last_run` test.
2. Minor: lost-ack results must use `unknown_outcome`.
3. Minor: map stray `OSError`/`FileExistsError` in advance to `unsafe_path`.
4. Minor: >1000 operations -> `capacity_reached`; `cancel` uses reserve 0.
5. Minor: record deviation 5 in `REANCHOR-P2.md`; remove hardcoded "Portable 16" message.

Assigned to P3/P4:
- P4: define a collector state identity so `collector_binding` detects a state swapped at the same path (today only path strings are hashed; Brain swap is caught by brain_id/issuer_epoch).
- P4: resolve INTERFACE:76 (preserve native safe failure codes in private operation evidence) vs the closed RECOVERY evidence schema; document the decision.
- P4: integration test reaching the real 256-item custody quota refusal (currently unit-only).
- P3: any new write must follow the same rule as fix 1: a refusal never proves a prior write did not commit.
