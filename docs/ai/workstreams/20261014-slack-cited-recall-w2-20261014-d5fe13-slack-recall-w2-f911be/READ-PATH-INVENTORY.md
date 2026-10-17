# W2 MCP read-path closure

Candidate inventory; required full checks and frozen-HEAD review remain pending. All paths below are relative to this workstream's recorded absolute worktree. No client wire request controls SessionOrigin.

| MCP surface | Eligibility boundary | Evidence |
|---|---|---|
| Legacy brain_search | local_entrypoints binds RetrievalTasks.with_authority; retrieval.py installs shared SQL predicate before search ranking/limit; fetch/read_page bind the same authority | test_w2_mcp_eligibility; real test_w2_mcp_stdio owner elevation |
| Negotiated search/read/history/source route | paging.py before ranking and authorized generations; records.py before revision errors/projection; sharing.py before owner shortcut; history.py reuses projector; sources.py before replay/mutation | original/copy/retained/mixed canonical tests and real stdio; existing Slack external-release surfaces |
| Selection descriptor/member expansion | slack_sharing.py requires anchor eligibility before descriptor; each member read uses shared eligibility | selection direct-read and record-read refusal tests |
| Inbox list/route and space metadata | spaces.py SQL eligibility before LIMIT/OFFSET and capture eligibility before route/replay; space names are independently owner-created metadata | auxiliary list/route refusal; unrelated inbox control |
| Review list/show/propose/approve/reject/edit | review.py list SQL checks every review source; show checks full inherited provenance; propose guards selected capture + target canonical; decisions show/guard before replay or reservation | mixed-source inherited update denial and terminal approve/reject/edit replay cases |
| Facet subjects/selections/assignments/history/publication | facet_queries.py uses RecordProjector for every subject; facet_publication.py checks inherited provenance; facets.py implicit continuation commits authorized generations + visible facet items | assignment/history/replay refusals and hidden-only mutation continuation |
| Workspace status/snapshot, graph suggestions | managed_workspace.py filters before counts/body and hashes visible source versions; suppresses global policy counter for implicit sessions; managed_inference.py guards BOTH endpoints via all retained canonical/managed revision and link-target closure | ordinary canonical control, protected retained endpoint after deactivation, hidden-only policy exclusion equality |
| Structural graph cache | local_operations.graph_projection refuses whole-workspace cache before load for implicit MCP; returns constant missing structural metadata and authorized engine suggestions | test_w2_graph_boundary proves identical output when invalid protected cache appears; explicit owner still uses existing cache |
| Graph refresh | MCP callback returns fixed provider_not_configured/unavailable without reading graph content or invoking a provider | existing managed CLI/MCP regressions |
| Capture and destination-bound submit | returns bounded identifiers/custody receipts for submitted content, not retained Slack text; scoped setup never grants writes | existing capture/submit authority and compatibility suites |
| Catalog/tool discovery, spaces and facet vocabulary | closed schemas and independently owner-created names; no record previews, source titles, graph quotes or protected counts | existing catalog/facet/space regressions |
| Optional HarnessMcpAdapter | mandatory host-pinned HarnessPolicy and current consent/destination loaded for each operation through harness_bridge/workflow; provider/local read authority explicitly carries MCP_POLICY; source decoding accepts harness memory only | existing harness_mcp/workflow/bridge regressions; no unscoped harness fallback |

## Boundary and limitations

Direct owner CLI operations retain LOCAL_OWNER. Policy-omitted local MCP including owner elevation carries MCP_IMPLICIT. Trusted launcher/HarnessPolicy reads carry MCP_POLICY. Existing external egress, consent, space, tier, sharing and output-boundary revalidation remain in force. Raw local-only originals cannot be released externally by explicit policy.

Retained namespaces/privacy, managed custody, recognized sharing commitments, canonical revision members, development dependency receipts and retained graph provenance establish lineage. Message text and source-reference substrings do not grant source authority. Corrupt or unresolved recognizable lineage fails closed. Traversals are bounded; no new persistence is introduced.

The structural cache has whole-workspace extraction semantics: even a hidden same-name note can affect extraction ambiguity. Endpoint-only filtering would leave a metadata channel. Implicit MCP therefore withholds this cache while retaining ordinary eligible search/read, inbox/review/facet/workspace and inferred-suggestion behavior. Per-source structural extraction provenance or independent visible-snapshot extraction is future work; it is not silently added here.

The upgraded-runtime gate does not fence older binaries or confine a hostile same-user process. Compatibility remains schema/runtime/Portable 17/12/12, frozen T03 and existing sharing formats. Settings observation is finite configuration evidence, not proof of another client's transport. Actual-client injection status is not_run; W3 and private pilot are not authorized.
