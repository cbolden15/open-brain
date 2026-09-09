# NW0 source binding and separate-helper feasibility

Status: independently reviewed private A3/B5 continuation; full NW0 remains open.
The baseline is `9153dab`. Completed experiments and cumulative effort remain charged.
No shipping runtime, schema, dependency, lockfile, auditor, formula or build specification changed.
No model calls, desktop activity, push or publication occurred.

## A3: bind the private composition to real records

A3 starts with a genuine validated v1 export. Its private binder records the complete inventory,
manifest digest, tenant/owner labels, exact page and capture bytes, stable identities, body digests
and immutable privacy. A changed page rehashed into an otherwise valid v1 export cannot replay
a composition bound to the original snapshot. The source binding is stronger than A2's synthetic
origins, but hashes and owner labels still do not authenticate approval.

The private replay exercises accepted edits and links, a retained conflict, inactive state,
explicit restore, exclusions and consent/revocation audit. Destination consent stays inactive.
It requires active base pages and rejects an archived base; this is a conservative proof rule,
not a selected future archival format.

The current Engine boundary is tested directly. An unchanged base imports, reconciles and retrieves.
An overlay's private deactivation does not affect current Engine retrieval. Adding the typed file
to the v1 manifest makes the current validator/importer refuse it before creating the destination.
The overlay plus separately supplied v1 base is not a standalone future Portable export, shared
record format, production importer or authenticated authority boundary.

Review found that empty quotes satisfied substring-based evidence checks. The private validator now
rejects empty, whitespace-only and wrong-type quotes at both endpoints. Six grouped tests and Ruff
pass after this correction. Full conflict resolution/promotion, crash recovery,
serialized release/accounting and complete future-format import remain open.

## B5: move Graphify out of base startup

The new candidate keeps the base executable unchanged and freezes Graphify with the pure Python
parser in a separate helper. The base has 218 modules and no Graphify; the helper has 158 modules,
including seven Graphify modules. The helper contains no app/engine, tree-sitter, NumPy, networkx
or optional YAML accelerator modules. Both executables pass arm64/ad-hoc signature checks.

| Artifact | Archive bytes | Content result |
|---|---:|---|
| Unchanged base | 9,837,828 | Passes unchanged authoritative inspection. |
| Rebuilt pure-parser helper | 8,447,435 | Passes unchanged authoritative inspection. |

The two archives total 18,285,263 bytes. Each uses the existing one-executable native archive shape.
Four Graphify/PyYAML legal and notice files retain their original bytes. Repackaging the same binary
under the same filename produces identical archive bytes; this does not prove compiler output is
bit-for-bit reproducible.

Five ordinary warm `status --json` samples per layout use the same base bytes and initialized
synthetic Brain, with one warmup each and alternating order. Median times are 4.066433 seconds for
the baseline layout and 4.086936 seconds with the helper beside it. The added 20.503 ms passes the
200 ms warm budget. Owned heavy work was paused during measurement. These calls do not invoke the
helper, so this is base-startup evidence for the proposed layout, not graph-refresh latency.

B4's 213.40075 ms failure remains unchanged. Its series used the private installer self-check;
this series uses ordinary status. The two deltas are not a direct performance-gain comparison.
Neither series establishes controlled cold starts or native Linux performance.

The final helper passes 20 frontmatter checks, original/relocated/incremental extraction cases,
stable page mapping, source preservation, byte/time bounds, unknown-mode refusal and temporary-file
cleanup. Its graph matches B4 after explicit root/ID normalization. Base initialization and status
work in the candidate layout. The final evidence verifier passes 35 checks.

## Distribution and reproducible inputs

A first attempt to put two executables into one tarball fails the unchanged auditor. Generic
archives cap individual members at 2 MiB; recognized native archives require one executable named
`open-brain`. The revised proposal uses separately audited archives and a named Homebrew resource
to install the helper. Resource identity, asset naming, manifest/formula support and install/update/
uninstall behavior still require a concrete contract and proof. No shipping layout was selected.

The private reconstruction verifies the pinned Graphify wheel, all eight patch targets before and
after application, and the parser wheel hashes. It reproduces 253 prepared Mac files and prepares
the corresponding Linux metadata/source inputs. All 18 parser Python files match between wheels.
A relocatable spec builds the final audited Mac helper from the reconstructed inputs. Linux replay
commands are recorded privately; preparing those inputs is not Linux execution.

The existing upstream-first, maintained-patch policy remains in force. The separate resource is
the recommended next packaging candidate because it keeps Graphify outside the base module and
startup path. Its distribution cost and remaining lifecycle work must be reviewed before adoption.

## D2: desktop prerequisites

Read-only checks confirm the supported UTM CLI and the preserved arm64 guest. They do not establish
the required Ubuntu x86_64 GNOME guest or either product GUI journey. The isolated Mac account and
separate UTM guest were already authorized; the worker's suggestion to obtain authorization again
is not adopted. The proposed test-account name is absent and noninteractive administrator access
is unavailable in this session. Other accounts were not enumerated. Safe capacity and the actual
desktop environments must be verified before dependent setup work.

## Verification and remaining work

B5 ran the real native builder, unchanged artifact/signature checks, frozen fixture cases,
bounded startup measurements and its evidence verifier. Private script undefined-name checks pass.
Separate reviewers verified A3's six grouped tests and B5's 35-check evidence receipt. The coordinator
reran A3 after its evidence fix. Public content/link, diff and workflow checks pass (12 checks).
Historical base product smoke remains evidence for unchanged base bytes. Full project verification
and Homebrew installation smoke were not rerun for documentation-only repository changes.

Recorded harness failures remain visible: an absent helper sysconfig module, gzip filename
differences, verbose diagnostics exceeding 16 KiB, case-sensitive root normalization, and a base
initialization refused by the extraction-only sandbox. Corrections preserve the product limits.
Helper extraction remains sandboxed; base init/status use synthetic directories without that
profile. None of this is subscription-client confinement evidence.

Next packaging work is a bounded resource/install-contract proof, preserving these exact candidate
hashes. Controlled cold starts, native Linux execution, the complete durable representation,
release/accounting, desktop journeys and the real-provider matrix remain open. Claude stays closed
at C7, Codex stays deferred, and NW1 cannot start before all required NW0 gates pass.
