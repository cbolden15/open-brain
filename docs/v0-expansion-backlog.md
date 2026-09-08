# v0 expansion backlog

Status: deferred outside the approved self-hosted v0 release boundary.

This file records known product expansion. A row is not implementation authority. Moving one
into v0 requires an explicit product-contract change with replacement acceptance evidence.
Anything new receives an `EXP-*` row before design or implementation starts.

| ID | Deferred expansion | Entry condition |
|---|---|---|
| `EXP-001` | Managed control plane, billing, fleet operations, and hosted vendor stack | Self-hosted alpha gates pass and a hosted product decision fixes tenancy, operations, and publication policy. |
| `EXP-002` | Shared organizations, multiple human owners, and collaborative editing | A conflict, authorization, and export model is approved for more than one owner. |
| `EXP-003` | High availability, distributed consensus, or multiple canonical writers | Measured single-writer availability is insufficient and a new consistency contract is approved. |
| `EXP-004` | Required cloud models or hosted retrieval | Provider `none` and local operation remain complete; any required network dependency needs a new privacy and degradation contract. |
| `EXP-005` | Vector or graph retrieval as a default | A replaceable adapter beats the fixed v0 lexical fixture and preserves the no-model path. |
| `EXP-006` | Broad connector catalog and a public Connector SDK | The internal reference, event, and measurement connector proofs pass common authority, replay, isolation, and signing gates. |
| `EXP-007` | Plugin marketplace, autonomous multi-agent memory, or automatic owner-knowledge rewriting | Separate capability, review, provenance, and package-trust contracts are approved. |
| `EXP-008` | Obsidian plugin, graph visualization, and daily planner | Core Markdown compatibility and the v0 daily journey are stable enough to justify product-specific surfaces. |
| `EXP-009` | Intel macOS, Linux arm64, and Windows release artifacts | Clean-host ownership, signing, upgrade, recovery, and support evidence exists for each added target. |
| `EXP-010` | Container-first installation | Current macOS source/wheel and Linux native installation paths pass; container ownership, volumes, localhost authentication, recovery, and upgrade pass the same gates. |
| `EXP-011` | Full parity with private predecessor integrations, schedules, migration, or cutover | Each capability proves public product value and is re-authorized independently of predecessor parity. |
| `EXP-012` | Client-held end-to-end encryption and client-owned plaintext workers | The hosted execution boundary is approved without changing the common engine or Portable Brain contract. |
| `EXP-013` | Prism renderer or first-class harness plugin integration | The renderer capability remains read-only and receives its own packaging, authority, and support contract. |
| `EXP-014` | Additional payload families beyond text, reference or file, event, and measurement | A product decision proves the input cannot be represented by an existing family without losing required semantics. |
| `EXP-015` | Direct browser/Finder macOS distribution | A later decision adds Developer ID signing, notarization, Gatekeeper validation, and a non-Homebrew lifecycle before that path is advertised. |

The three hosted decisions still deferred by the approved architecture are tracked by `EXP-001`
and `EXP-012`: vendor stack, hosted repository publication policy, and the client-held-encryption
execution boundary.
