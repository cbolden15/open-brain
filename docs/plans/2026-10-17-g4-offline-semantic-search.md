# G4 offline semantic search: implementation plan (gates 2–5)

Date: 2026-10-17. Goal contract: cbolden15/agent-config#111. Branch `goal/offline-semantic-search`
(local only). Base: origin/main `de4a6dd` (schema/runtime/Portable 20/15/15).

Authority, in order: this plan's decisions; `2026-10-15-competitive-gaps-g1/EMBEDDING-RESOURCE.md`
(with its 2026-10-17 amendments: `ob.hybrid-rrf.v2`, persistent verified stage); `PERSISTENCE.md`
sections on eligibility and embedding consent. Spike evidence:
`2026-10-15-competitive-gaps-g1/EMBEDDING-SPIKE-RESULTS.md`.

## Fixed decisions

| Topic | Decision |
|---|---|
| Model / profile | MiniLM-L6-v2 at `1110a243…`, profile `ob.minilm-wordpiece-mean.v1` (mean pooling, 224/32 windows, 254 query tokens, no prefix) |
| Runtime | ONNX Runtime 1.31.0 commit `402979ac…`, CPU only, telemetry compiled out, recipe `tools/g4_embedding_probe/ort-build-recipe.json`; macOS floor 11.0, Linux glibc as built on `ubuntu-latest` |
| Helper | Rust crate `release/open-brain-embedding/`, protocol `open-brain.embedding-helper.v1`, loads ONNX Runtime from staged absolute path |
| Staging | Persistent per-component stage `.open-brain/cache/embedding-v1/stage/<component_id>/`, full re-hash before every launch (D7) |
| Fusion | `ob.hybrid-rrf.v2` (D5) |
| Distribution | Bundled with the core formula (budgets passed on both platforms); embedding resource is a fourth manifest-v2 role |
| Coordinates | Next allocation 21/16/16 for the embedding policy table and its Portable evidence (coordinator-allocated; flagged for review) |

## Gate 3 — distribution

- **T3.1 Manifest v2 and inventory v1** (`tools/open_brain_dev/component_manifest_v2.py`, tests): parser,
  canonical renderer and validation exactly per section 3; v1 code and fixtures byte-unchanged; every
  rejection fixture listed in section 9's distribution row that does not need real archives.
- **T3.2 Embedding resource build** (`tools/open_brain_dev/embedding_native.py`, `make embedding-native`):
  consumes a recipe-built ONNX Runtime library, builds the helper with `--locked`, assembles the archive
  at final installed prefix-relative paths, writes inventory + embedding `component`, and gathers the
  complete notices (model Apache-2.0 text and attribution, ONNX Runtime LICENSE + ThirdPartyNotices,
  every Rust crate license from `cargo metadata`). Linkage audit: allowlisted system libraries only, no
  network symbols, no telemetry endpoint strings.
- **T3.3 Formula, install and audits**: renderer emits the v2 formula with the embedding resource;
  `make native-audit` covers the embedding archive; `make homebrew-smoke` installs and checks it without
  touching the production keg.

## Gate 4 — engine and app

- **T4.1 Embedding policy (21/16/16)**: owner-only versioned policy (Brain/issuer, enabled, component ID,
  monotone consent version, policy generation); migration defaults disabled; Portable16 adds the
  `ob.embedding-policy.v1` evidence family `history/embedding-policy/embedding-policy.jsonl`; restore
  installs history but leaves execution disabled; old readers refuse 16. CLI
  `open-brain embedding status|enable|disable --json`.
- **T4.2 Helper transport** (`packages/app/src/open_brain/services/embedding_helper.py`): installed-prefix
  discovery (as `graphify_projection.discover_graphify_executable`), manifest/inventory verification,
  persistent verified stage, one bounded child per exchange (deadlines, output/stderr caps, kill/reap),
  strict response validation, `ob.embedding-error.v1` categories; exposed to the engine through a typed
  backend protocol (engine never imports the app).
- **T4.3 Derived cache** (engine): generation manifest, view key, chunk keys, float32 vectors, staging +
  atomic activation, quotas, crash/cancel recovery; foreground `open-brain embedding rebuild`.
- **T4.4 Hybrid search** (`paging.search_page`): semantic candidates only from the existing authorized
  clause, re-projection checks, currentness revalidation, v2 fusion, hybrid ranking/component/policy/cache
  generation in the cursor binding, `hybrid_required`/`hybrid_preferred`/`lexical` semantics.
- **T4.5 Health and surfaces**: `ob.embedding-health.v1` in `status --json` and doctor; MCP/plugin/desktop
  `mode_used:"hybrid"` handling; docs (`docs/portable-brain-v16.md`, search/embedding docs, README note).

## Gate 5 — acceptance

End-to-end on a disposable synthetic Brain through the real CLI: import the frozen corpus, enable
embedding, rebuild, run all 60 queries in `hybrid_required`; paraphrase hit@5 ≥ 90%, no exact-ID
regression vs lexical; benchmark budgets on macOS arm64 and X1 linux-x86_64; full `make verify`,
`make native-audit`, `make homebrew-smoke`, `make native-integration-smoke` on the Mac; `make verify` and
`make native-audit` on X1; independent read-only review of the final SHA.
