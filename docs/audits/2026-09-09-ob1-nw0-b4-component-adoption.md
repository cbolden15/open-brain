# NW0-B4 component adoption and frontmatter proof

- Result: **upstream-first with a maintained-patch fallback is authorized; nested frontmatter and
  literal alias handling have a working private contract.** Final packaging remains unapproved.
- Baseline: `9ace4e4`, branch `docs/ob1-native-workspace-plan`, native macOS arm64.
- Limit: 45 minutes from remaining NW0-B, synthetic fixtures, zero model calls.
- Scope: private prototypes and public documentation. No shipping code, dependency, lockfile,
  native specification, auditor, upstream submission or publication changed.

The [component contract](../plans/2026-09-09-ob1-graphify-component-contract.md) records the owner's
new dependency policy, versioned patch identity, local upstream proposal and frontmatter/cache rules.
It extends [B3](2026-09-09-ob1-nw0-b3-markdown-component.md) without restarting completed work.
OpenAI API, Anthropic API, Gemini API and Claude subscription remain the four launch paths. Codex
stays deferred; Claude note dispatch stays closed at C7's missing capabilities.

## Parser pin and source behavior

B4 retains B3's exact eight-file Graphify patch and adds an explicit private parser pin. The
[PyYAML 6.0.3 release](https://pypi.org/project/PyYAML/6.0.3/) supplies Python 3.14 Mac arm64 and Linux
x86_64 wheels. The Mac wheel is 173,809 bytes, SHA-256
`34d5fcd24b8445fadc33f9cf348c1047101756fd760b4dacb5c3e99755703310`.
The Linux wheel is 794,175 bytes, SHA-256
`c458b6d084f9b935061bc36216e8a69a7e293a2f1e68bf956dcd9e6cbcd143f5`.
All 18 Python files match between the two wheels. This is static correspondence, not Linux execution.

The original 30-distribution closure retains all 2,179 checked RECORD hashes. The added parser
verifies 27 hashed installed files. Its test tooling is reused separately from B3. No existing
private environment or upstream source was modified in place.

With PyYAML available, all 47 selected upstream Markdown/sanitization tests pass, resolving B3's
nested-frontmatter failure for this candidate. The same 47 pass with the C extension explicitly
unavailable in source mode; both upstream registry identity tests also pass. These are focused
suites, not the entire Graphify suite or the full Open Brain project check.

Additional synthetic inputs show why simply installing the parser is insufficient:

| Raw Graphify behavior | Private Open Brain preflight result |
|---|---|
| A cyclic YAML value raises `RecursionError` during sanitization. | Reject the cycle before Graphify. Bound nesting and expanded work. |
| A duplicate alias key silently keeps the last value. | Reject duplicate keys before extraction. |
| `aliases: [on, off]` becomes boolean values. | Preserve literal names through property-specific text handling. |
| An ampersand in an alias is HTML-escaped in returned metadata. | Keep canonical alias identity separate from the display projection. |

The preflight passes 20 checks in source mode and in each frozen candidate. It preserves nested
metadata, ordinary anchors and dates, retains literal and long alias lists, and rejects malformed,
ambiguous or oversized structures before dispatch. It uses the same synthetic fixtures throughout.
This is a tested private adapter boundary; no production Engine, Obsidian plugin or provider adapter
has been connected to it. The full canonical alias resolver and accepted-link/export slice remain open.

## Native candidates

Both candidates include the same Graphify component, preflight and frozen test entry points. The
only parser-profile difference is whether PyYAML's optional native accelerator is bundled.

| Candidate | Executable bytes | Archive bytes | Python modules | Content result |
|---|---:|---:|---:|---|
| C extension included | 10,262,928 | 10,117,321 | 246 | Fails one home-path finding in `yaml/_yaml.cpython-314-darwin.so`. |
| Pure Python | 10,133,360 | 9,990,282 | 245 | Passes the unchanged authoritative content audit. |

Both retain seven Graphify modules, pass the existing base-module audit and Mac arm64/ad-hoc
signature verification, and preserve all four installed license/notice files byte-for-byte. No
object/byte limit, content exception or validation setting changed. Both scans complete; the C
failure is retained. The pure profile omits the unused optional accelerator rather than modifying
its binary or weakening the audit. The pinned `safe_load` implementation uses Python `SafeLoader`
in both profiles, consistent with PyYAML's [documented interfaces](https://pyyaml.org/wiki/PyYAMLDocumentation).

Six frozen structural cases cover original, relocated and edited B1 fixtures across both candidates.
Their raw nodes/edges equal the original upstream source running with the same parser. The parser
changes frontmatter representation from B3's fallback, so B4 does not falsely compare those values
to a parser-absent oracle. Selected-source bytes, read/write boundaries, recursion limits, 16 KiB
input/output bounds, 60-second limits and one-file cleanup pass. Both frozen frontmatter suites also
pass. The private Mac profile denies network and unrelated user-file access; it is not a proof of
hostile-code or Claude-client confinement.

The pure candidate passes the existing native product smoke in 109.929 seconds under the unchanged
240-second cap: self-check, capture, status, doctor, search, export, Markdown import and local MCP.
The failed C candidate was not given a second full product smoke. No separate-helper parser build
or Linux runtime was executed in B4.

## Startup result: the warm gate remains failed

Thirty startup samples use five first-series and five repeat-series invocations per candidate,
interleaved with the existing baseline. Each invocation gets a new temporary directory; no build
or content audit overlaps the timing series. OS cache eviction is uncontrolled.

| Startup path | First-series median | Repeat-series median | Added first / repeat |
|---|---:|---:|---:|
| Existing baseline | 4.441 s | 4.348 s | Reference |
| C extension included | 4.507 s | 4.468 s | 65 / 121 ms |
| Pure Python | 4.537 s | 4.561 s | 96 / 213 ms |

The pure candidate exceeds the 200 ms repeat-startup budget by 13.4 ms. That result remains a
failure; no tolerance was added and the unchanged candidate was not rerun to seek a pass. The
first-series result does not establish controlled OS-cold acceptance. B3's 103 ms result was for
a different closure and run, and does not override B4's measurement.

The C candidate meets the observed timing comparison but fails content inspection. The pure
candidate passes content inspection but fails the observed warm comparison. Neither is a shipping
candidate yet. Prefer the pure profile for continued work because the workload uses the Python
parser; retain the separate-helper and reproducibly rebuilt accelerator options if a combined
artifact cannot satisfy all gates.

## Review and verification outcome

The exact B3 patch applies cleanly to the pinned source. Its source comparison suite passes 21
checks. Six cache-lifecycle checks reproduce stale state without invalidation and verify creation,
rename, deletion and root changes with a new serialized batch. The contract requires that lifecycle
rather than claiming concurrent shared-cache safety.

The final verifier passes 61 evidence-consistency checks, covering hashes, patch provenance,
upstream/registry tests, 38 native cases/samples, legal-file bytes, both audit outcomes, cleanup and
native smoke. This is verification of the reported result, not release approval or an independent
review. The known raw-YAML failure cases remain recorded beside the passing preflight tests.

Full project verification and Homebrew installation smoke were not run for documentation-only
repository changes. The private builds ran the real native builder, auditor, bounded runtime checks
and existing product smoke. Documentation checks accompany the local commit. No model calls,
credential inspection, upstream message, push or publication occurred.

## Next bounded experiment

NW0-B5 should investigate the startup cost against a fixed measurement protocol and make the
selected patch/parser inputs reproducible for the existing Linux CI path. Cap the next local probe
at 45 minutes from remaining NW0-B, using synthetic fixtures and zero model calls. Preserve every
B4 timing result and the 200 ms budget; make any packaging change concrete before remeasuring it.
Linux wheel correspondence is a useful starting point, but native Linux execution and controlled
cold-start evidence remain separate gates. Do not begin NW1 or the UI-heavy work before NW0 closes.
