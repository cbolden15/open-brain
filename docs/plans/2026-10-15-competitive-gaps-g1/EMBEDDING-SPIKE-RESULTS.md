# G4 embedding feasibility spike: results and selection

Date: 2026-10-17. Goal contract cbolden15/agent-config#111. Branch `goal/offline-semantic-search`.
Owner authorization (2026-10-17): download the pinned candidates, build ONNX Runtime from source with
telemetry compiled out, synthetic text only, no real Brain. Every measurement below used synthetic
inputs. Model execution ran with network access denied (`sandbox-exec (deny network*)` on macOS; a
network namespace with only loopback on Linux).

## Selection

| Item | Selected |
|---|---|
| Model | `sentence-transformers/all-MiniLM-L6-v2@1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, `onnx/model.onnx` |
| Profile | `ob.minilm-wordpiece-mean.v1` (masked mean pooling, L2 normalize, 224-token windows with 32 overlap, 254 query tokens, no prefix) |
| Fusion | `ob.hybrid-rrf.v2` (EMBEDDING-RESOURCE.md section 8 amendment) |
| Runtime | ONNX Runtime 1.31.0, commit `402979ac1bef89dcd55add41eaa7277c407599eb`, CPU only, `onnxruntime_USE_TELEMETRY=OFF` |
| Tokenizer | Rust `tokenizers =0.22.1`, `default-features = false`, feature `fancy-regex` (no Hub, HTTP or progress features) |
| Helper | `release/open-brain-embedding`, protocol `open-brain.embedding-helper.v1`, ONNX Runtime loaded from the staged absolute path |
| Floors | macOS arm64 11.0 (helper and runtime `minos 11.0`, matching the shipped base); Linux x86_64 runtime requires GLIBC 2.38, helper GLIBC 2.34, consistent with the base built on `ubuntu-latest` |
| Distribution | Bundled with the core formula. All product budgets passed on both platforms, so the optional separately installed resource fallback is not needed |

BGE (`BAAI/bge-small-en-v1.5@5c38ec7c…`) was measured as the comparison and not selected: 90%
paraphrase hit@5 versus 100%, about half the indexing throughput, and a 47% larger graph.

## Downloaded-byte evidence

`tools/g4_embedding_probe/fetch.py` checks each file against its pinned byte count and the publisher's
LFS SHA-256 or git blob SHA-1, then records the measured SHA-256. Pins: `tools/g4_embedding_probe/assets.json`.

| File (MiniLM) | Bytes | Measured SHA-256 |
|---|---:|---|
| `onnx/model.onnx` | 90,405,214 | `6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452` |
| `tokenizer.json` | 466,247 | `be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037` |
| `config.json` | 612 | `953f9c0d463486b10a6871cc2fd59f223b2c70184f49815e7efbcab5d8908b41` |
| `vocab.txt` | 231,508 | `07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3` |
| `tokenizer_config.json` | 350 | `acb92769e8195aabd29b7b2137a9e6d6e25c476a4f15aa4355c233426c61576b` |
| `special_tokens_map.json` | 112 | `303df45a03609e4ead04bc3dc1536d0ab19b5358db685b6f3da123d05ec200e3` |
| `1_Pooling/config.json` | 190 | `4be450dde3b0273bb9787637cfbd28fe04a7ba6ab9d36ac48e92b11e350ffc23` |
| `README.md` (model card) | 10,502 | `dcd602d2fd35c203a247304a06fec6654a12f7941b739f9221a064fe8dc3b7f0` |

The ONNX graph's measured hash equals the publisher LFS hash. Reference-only PyTorch weights for the
parity check (never shipped): MiniLM `model.safetensors` `53aa5117…d9db`, BGE `3c9f3166…21ad`
(`tools/g4_embedding_probe/reference-assets.json`). The MiniLM repository has no license file; its card
designates Apache-2.0, so distribution must carry the full Apache-2.0 text plus model attribution (T3.2).

## ONNX Runtime build and telemetry audit

Recipe `tools/g4_embedding_probe/ort-build-recipe.json` (SHA-256 `e39a06bde97be2f6cdf36998caf3d05e56318977f6e72e40b1615173f22abf27`).
`--no_telemetry` maps to `-Donnxruntime_USE_TELEMETRY=OFF`, which leaves the 1DS client unlinked.

| Check | macOS arm64 | Linux x86_64 (X1) |
|---|---|---|
| Library SHA-256 | `ad50b487ba86e83d2c8983b3363de39b81cd054d471443324e341c05ddb58f9b` | `d02b770e5f2b09ffde120af0f4daa2ed4a9aa9b94db74f718d2a0c0ff66520a5` |
| Size | 27.5 MB | 33.8 MB |
| CMake cache | `USE_TELEMETRY=OFF`, `CMAKE_OSX_DEPLOYMENT_TARGET=11.0` | `USE_TELEMETRY=OFF` |
| Telemetry endpoint strings | 0 | 0 |
| 1DS client classes | 0 | 0 |
| Imported socket/connect/getaddrinfo/NSURLSession/CFNetwork/curl | 0 | 0 |
| Linked libraries | Foundation, CoreFoundation, libiconv, libc++, libSystem | libstdc++, libm, libgcc_s, libc |

The only telemetry strings left are the no-op base `Telemetry` class's flush messages. Foundation
linkage alone does not prove absence of network use; the network-denied runs below are the behavioral
proof.

## Graph and publisher parity

Both graphs take `input_ids`, `attention_mask` and `token_type_ids` (int64 `[batch, seq]`) and return
`last_hidden_state` float32 `[batch, seq, 384]`: raw token states, so pooling is ours.

| Comparison | Texts / chunks | Min cosine | Max abs component diff |
|---|---:|---:|---:|
| ONNX path vs PyTorch sentence-transformers, MiniLM | 150 | 0.99999988 | 2.1e-7 |
| ONNX path vs PyTorch sentence-transformers, BGE | 150 | 0.99999988 | 3.0e-7 |
| Native helper vs Python reference, macOS (11.0 runtime) | 22 chunks, incl. a 3-window document | 1.000000000 | 5.6e-8 |
| Native helper vs Python reference, Linux, network namespace | 22 chunks | 1.000000000 | 7.9e-8 |

Token and byte ranges were identical between helper and reference.

## Relevance (frozen synthetic corpus)

Corpus `tools/g4_embedding_probe/corpus/relevance-v1.json`: 200 records, 40 paraphrase queries sharing
at most one stemmed content word with their targets (all 40 have a lexical decoy record), 20 exact
identifier queries. Lexical is an exact emulation of the engine's `fts5-match-tier-binary-v2`
(same FTS5 tokenizer and query compilation).

| Mode | MiniLM paraphrase | MiniLM exact-ID | BGE paraphrase | BGE exact-ID |
|---|---:|---:|---:|---:|
| Lexical | 5% | 100% | 5% | 100% |
| Semantic only | 100% | 90% | 90% | 90% |
| Hybrid `ob.hybrid-rrf.v1` | 20% | 100% | 22.5% | 100% |
| Hybrid `ob.hybrid-rrf.v2` | **100%** | **100%** | 90% | 100% |

Exact-ID regressions against lexical: none for either fusion. v1 fails because lexical tier 5 is ordered
only by record identity; v2 drops that tier from the fusion input (decision D5).

## Performance (native helper, full product path)

Each query re-hashes every staged byte against the pinned hashes, launches one helper process, sends one
`embed` request and validates the response (`tools/g4_embedding_probe/bench.py`, persistent verified
stage, decision D7). Rebuild = 10,009 chunks in 86 bounded exchanges, re-hashing before each.

| Budget | Limit | macOS arm64 (M1 Pro) | Linux x86_64 (X1) |
|---|---:|---:|---:|
| Warm query p95 | ≤ 500 ms | 181 ms | 198 ms |
| Cold (stage creation + first query) | ≤ 3 s | 1055 ms | 570 ms |
| Rebuild, 10,000 chunks | ≤ 10 min | 374 s | 242 s |
| Child peak RSS | ≤ 600 MiB | 430 MiB | 408 MiB |
| Installed resource (staged) | ≤ 250 MiB | 117.1 MiB | 122.5 MiB |

Conditions: the macOS run shared the machine with concurrent test suites (load average ~10) and the
Linux run used `nice -n 10` alongside other services, so both are conservative. Cold numbers do not
purge the OS page cache (needs administrator rights) and are a lower bound on a truly cold disk.

The original per-invocation staging model was measured too: warm p95 1213 ms on macOS, because the first
execution of freshly written code costs about 600 ms of system code validation and freshly written model
data about 320 ms. That evidence drove the persistent verified stage amendment to section 4.

## Outstanding for later gates

Native acceptance of the exact packaged candidate (gate 5) re-measures these budgets from the installed
resource, through the engine and CLI, on both platforms. Distribution notices (T3.2) must include the
model license text and attribution, ONNX Runtime LICENSE and ThirdPartyNotices, and every Rust crate
license. The staged `component.json` must be the canonical manifest-v2 `component` object without a
trailing newline so the helper's self-reported ID equals the manifest `component_id`.
