# Graphify component adoption and frontmatter contract

- Authority: the owner approved **upstream-first, maintained patch allowed** after NW0-B3.
- Status: dependency policy and private contract proof recorded in NW0-B4. Shipping integration,
  startup acceptance, controlled cold starts and Linux execution remain open.
- Evidence: [B4 audit](../audits/2026-09-09-ob1-nw0-b4-component-adoption.md).

B5's [separate-resource proof](../audits/2026-09-09-ob1-nw0-a3-b5-feasibility.md) rebuilds this
component with the pure parser in a helper. Both native archives pass Mac content inspection and
the unchanged base meets the observed ordinary warm-status budget in the proposed layout.
This candidate needs explicit resource identity, asset naming, manifest/formula and install-lifecycle
support. Controlled cold/Linux and shipping integration remain open; the combined B4 failure stands.

## Dependency ownership

Prefer a supported upstream Markdown component. If upstream support is unavailable when integration
needs it, Open Brain may maintain the small component patch. That fallback is now authorized; it
is not permission to publish an upstream message, waive release checks or silently change pins.

| Identity | Reviewed input |
|---|---|
| Upstream distribution | `graphifyy==0.9.57` |
| Upstream source | `3f82bf7f837a07fb0f7668fbdbd5662801906942` |
| Maintained patch ID | `ob1-graphify-markdown-1` |
| Patch SHA-256 | `f7417ee080e1f5050f41b525f4bb0f97910c14abf6531dcf38cbd2ff28da796b` |
| Parser candidate | `PyYAML==6.0.3`, explicit pure-Python runtime profile |

The eight-file patch is unchanged from B3. It separates root context, lazy registry exports, metadata
sanitization and discovery constants. A release must identify both the upstream version and patch
ID/hash; reporting only the upstream version is insufficient. A changed patch gets a new identity
and must re-pass its affected contracts. Upgrade from the pinned upstream source, apply the exact
patch with no fuzzy matches, verify its before/after hashes, and run the fixture/registry contracts.
Do not mutate an installed dependency in place or borrow the whole upstream CLI/model runtime.

Preserve Graphify's `LICENSE`, `LICENSE-MIT` and `NOTICE`, PyYAML's license, and attribution identifying
the Open Brain modifications in distributable inputs. The B4 native verifier confirms that the
existing four legal/notice files retain their installed bytes. The maintained source release and
its modification notice are still to be materialized in project-owned packaging inputs.

The pure-Python profile omits `yaml._yaml`, `yaml.cyaml` and the compatibility accelerator package.
It retains PyYAML's parser and `SafeLoader`; it does not substitute a handwritten YAML parser or
remove validation. PyYAML documents its [Python and LibYAML interfaces](https://pyyaml.org/wiki/PyYAMLDocumentation).
The pinned implementation's `safe_load` selects the Python `SafeLoader` in either profile. The
C-containing candidate remains rejected by content inspection. The pure profile is the direction
for further work, subject to its measured startup failure and the remaining release gates.

## Job boundary and cache ownership

The tested component callable is `extract_markdown(path, *, scan_root=snapshot_root)`. It returns
upstream intermediate nodes/edges; it does not produce accepted Portable Brain records. Keep existing
single-argument calls and full-extractor registry identities compatible when maintaining the patch.

One Engine-owned job must select a consistent eligible snapshot and record its revisions, digest,
Graphify patch identity, parser profile and adapter version before extraction. Each job gets a private
worker lifetime or serialized batch boundary. Clear the link index at the start of every reused
batch. Do not run overlapping batches against that mutable global cache, or reuse the full extractor's
persistent file cache as a substitute for Engine revision/eligibility checks. A public per-job context
would be a better upstream API; B4 does not claim to have implemented or verified that new API.

The cache tests reproduce stale lookup without invalidation and verify creation, rename, deletion
and root changes after the batch reset. Startup isolation still needs production integration; B4's
native parser comparison executes direct private probe modes. B3's separate/self-invoked boundaries
remain historical evidence, not newly executed B4 parser-helper acceptance.

## Canonical frontmatter

Preserve note bytes as source material. Parse frontmatter before Graphify or provider dispatch, using
an explicit dependency and bounded preflight. The prototype reuses Graphify's frontmatter boundary
splitter and PyYAML's parser; it does not write a second Markdown parser.

| Input | Required adapter behavior demonstrated privately |
|---|---|
| Nested mappings/lists, ordinary scalars and dates | Preserve canonical JSON-compatible structure; dates/date-times become ISO strings. Keep original source bytes. |
| `aliases` | Require a list of nonempty scalar names. Interpret this field as text, preserving punctuation, Unicode and scalar spellings such as `on` and `off`. Do not derive alias identity from YAML's implicit boolean/number conversion. |
| Shared YAML anchors | Accept finite structures within expansion limits; reject cycles. |
| Duplicate keys, custom object tags, unsupported values, malformed or unterminated frontmatter | Reject before extraction with a bounded category. Do not silently choose a duplicate value or fall back to incomplete metadata. |
| Limits | Keep the existing 16 KiB selected-input/accepted-output and 60-second job bounds. The private preflight additionally caps depth at 32 and parsing/expanded-value work at 4,096 items. These are internal candidate bounds, not new user settings. |

The property-specific alias interpretation is an Open Brain adapter rule, not a claim that PyYAML
implements Obsidian's complete scalar schema. Generic metadata retains PyYAML scalar interpretation;
unsupported non-JSON types and nonfinite numbers are rejected. Scalar `aliases`, merge keys with
non-string tags and complex alias values are not accepted by this prototype. Actual Obsidian
round-trip coverage remains a desktop integration gate. Obsidian's documentation requires unique
[property names](https://obsidian.md/help/properties) and list-form [aliases](https://obsidian.md/help/aliases).

Graphify's sanitized metadata is a display projection. It escapes an alias such as `Sun & Heat` and
caps strings/lists; those values cannot be the canonical alias catalog. The prototype preserves
literal aliases and more than 50 aliases in its canonical result while leaving Graphify's existing
sanitizer unchanged. Engine-owned mapping must resolve the canonical catalog against eligible Brain
IDs, retain ambiguous/unresolved diagnostics, and preserve heading/display syntax. Alias-list
preservation is demonstrated; full alias-to-Brain-ID resolution is still Engine adapter work.

Frontmatter cannot grant cloud consent, change exclusions or supply authentication authority.
Provider permission, selection and private-state exclusion remain Engine-owned checks. Rejection
must leave source/accepted records unchanged, retain the previous graph with a stale diagnostic,
and consume no provider attempt. B4 demonstrates rejection before Graphify in the private harness;
the durable graph/Engine/provider integration is not implemented by this contract.

## Local upstream proposal (not submitted)

Proposed topic: **Expose root-aware Markdown extraction without importing language runtimes.**

Request the existing Markdown parser with explicit root context; lazy compatibility exports for the
language registry; pure shared sanitization and directory rules; and a declared Markdown/parser
profile. Prefer a public per-job context or batch API over requiring callers to clear a private
cache. Preserve existing extractor identities and relative-only calls. The attached local patch is
the eight-file B3 prototype, with source, fixture and registry comparisons retained privately.

The proposal should distinguish upstream structural/display behavior from Open Brain's canonical
frontmatter, consent, alias mapping and persistence rules. Keep those application-specific rules out
of the upstream patch. Ask upstream to verify optional-parser behavior and finite/cyclic metadata
handling in its own accepted API. No upstream agreement, publication or independent review is claimed.

Before release, materialize reproducible patch/parser inputs, apply the same adapter contract on
Mac and Linux, pass the existing content/signature/startup checks, and run the project verification
and Homebrew checks after shipping code changes. The separate-helper option remains available if
one-executable packaging cannot meet the base-startup budget; it needs a distinct release contract.
