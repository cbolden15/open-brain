# Open Brain public release readiness audit

Date: 2026-09-09. Mode: standard, read-only readiness assessment.

Candidate: `d81bb6444e5ce9a2710401eed62d8ab41454e872` on
`goal/open-brain-five-minute-install`. Authority: the final public release gate in
`docs/plans/2026-09-08-ob1-product-completion.md:592`.

## Assessment

**NOT READY for public release.** W7 is complete and both supported-platform CI jobs passed at the
candidate. Source and history safety audits pass. However, the required artifact-aware safety audit
fails on both actual CI archives. GitHub also has obsolete required checks, merge settings that
conflict with the required history-preserving merge, disabled release immutability, and no Homebrew
tap repository.

This audit changed no GitHub settings, branches, PRs, tags, releases, or tap state. Only the local
report and gotcha record were written. Downloaded CI archives were inspected locally; they were not
published or treated as approved release assets. No private denylist contents or collaborator
identities are included here.

## Required fixes, in order

1. **Repair the native artifact safety audit.** Preserve the source scanner's fail-closed limits,
   and add a bounded audit path appropriate for the native executable and its packaged contents.
   Prove it with synthetic regressions and both real archives before proceeding to publication.
2. **Make protected `main` match the release path.** Replace the five obsolete required checks with
   `Linux x86_64` and `macOS arm64`, bound to GitHub Actions app ID `15368`. Enable merge commits and
   remove the linear-history requirement so the audited goal history can be preserved. Keep strict
   checks, conversation resolution, force-push prohibition, and deletion prohibition.
3. **Enable release immutability and prepare the tap.** Create `cbolden15/homebrew-tap` with minimal
   documentation and protect its default branch before publishing any product release.
4. **Resolve the release-order wording.** Distinguish provisional CI digests from the final audited
   release manifest, or provide archive-only staging if the literal audit-before-any-hash rule is
   retained. Do not treat an existing CI manifest as owner artifact-audit approval.
5. **Execute the remaining owner release gate after those fixes.** Re-freeze and audit the candidate,
   close obsolete draft PR #8, merge to `main` without rewriting history, verify that resulting commit
   on both platforms, and produce its audited release assets. Draft publication, downloaded-hash
   verification, tap formula publication, and timed public acceptance then follow in that order.

## GitHub controls and repository state

Live GET requests were made as the repository owner. The table records observations, not proposed
changes already applied.

| Requirement | Status | Evidence and required action |
| --- | --- | --- |
| Owner identity and two-factor authentication | COMPLETE | Authenticated account is `cbolden15`; `two_factor_authentication=true`; repository admin access is present. |
| Canonical repository and transfer redirect | COMPLETE | Public `cbolden15/open-brain`, repository ID `1351980089`. The old API path resolves to the same ID; the old web URL returns HTTP 301 to the canonical repository. |
| Collaborator access | COMPLETE | One collaborator, the owner, with admin role; no outstanding invitations. No additional collaborator access was found. |
| Secret scanning and push protection | COMPLETE | Both enabled; the authenticated, paginated open secret-scanning alert inventory returns zero alerts. This does not replace the owner denylist audits. |
| Actions and pinned actions | COMPLETE | Actions enabled; SHA pinning required. The checked-in workflow pins action commits and explicitly uses `contents: read`. |
| Required checks on protected `main` | BLOCKED | Still requires `verify (3.12)`, `verify (3.13)`, `verify (3.14)`, `public-artifacts`, and `phase4-contracts`. The current workflow produces only `Linux x86_64` and `macOS arm64`. Strict status checks are enabled. |
| History-preserving merge to `main` | BLOCKED | Repository allows squash only; merge commits and rebase merges are disabled. `main` requires linear history. Both the merge-method setting and the linear-history rule must change for the planned PR merge. |
| Force-push and deletion protection | COMPLETE | Both disabled on `main`; conversation resolution required. No additional repository rulesets exist. |
| Release immutability | MISSING | `GET /repos/cbolden15/open-brain/immutable-releases` returns `enabled=false`, `enforced_by_owner=false`. Enable before publishing the first release. |
| Tap availability and protection | MISSING | Authenticated GET of `cbolden15/homebrew-tap` returns 404. The name appears available under the owner, but a read-only audit cannot reserve it or guarantee creation. No tap branch exists to protect. |
| Release/tag collision check | COMPLETE | No releases, drafts, or tags are present in the authenticated inventory. `v0.1.0` is not already allocated there. |
| Candidate ancestry | COMPLETE | `main` is `10769806361e7ed419b653534e1303b623d84673`; the candidate is 45 commits ahead and zero behind. The merge base equals current `main`. |
| Current candidate CI | COMPLETE | [Run 34348556945](https://github.com/cbolden15/open-brain/actions/runs/34348556945) passed both platform jobs at exact candidate `d81bb64`. Both checks report GitHub Actions app ID `15368`. |
| Open PR disposition | PENDING OWNER ACTION | Draft [PR #8](https://github.com/cbolden15/open-brain/pull/8) is still open and is the obsolete draft named by the plan. Dependabot [PR #7](https://github.com/cbolden15/open-brain/pull/7) is also open, modifying root `pyproject.toml` against old `main`; triage it separately after the current product reaches `main`. Neither PR was changed. |

GitHub documents that linear-history enforcement excludes merge commits, so enabling the repository
merge method alone would not resolve the conflict. See [protected branch behavior](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).

The goal branch itself is unprotected. Freeze release work by full commit ID and recheck its identity
before each gate; a moving branch name alone is insufficient evidence. This is an operational
constraint, not a newly introduced requirement to protect another branch.

Two nonblocking policy choices remain for the owner: `enforce_admins=false` permits administrator
bypass of `main` protection, and repository-default workflow permissions allow writes and PR review
approval. The current CI explicitly restricts its token to read-only access. Applying main protection
to administrators and reducing workflow defaults to read-only/no approval would make future workflows
follow the same boundary. These are recommendations, not asserted prerequisites from the plan.

## Reproduced artifact-audit blocker

Both artifacts were downloaded from successful goal-head run `34348556945` and inspected without
executing their binaries. Each archive contains exactly one regular file named `open-brain`.

| CI archive | Archive bytes | Executable bytes | Independent checks |
| --- | ---: | ---: | --- |
| `open-brain-0.1.0-linux-x86_64.tar.gz` | 13,455,418 | 13,659,936 | SHA-256 equals its CI manifest; ELF64 x86_64 header verified. |
| `open-brain-0.1.0-macos-arm64.tar.gz` | 9,861,448 | 10,000,784 | SHA-256 equals its CI manifest; `lipo` reports arm64; strict codesign verification passes and signature details identify ad hoc signing. |

A disposable single-branch clone at exact candidate `d81bb64` passed both owner source/history
audits. The following additional artifact-aware invocation, using the same unmodified owner denylist,
exited **1**:

```sh
uv run python -m tools.open_brain_dev.release_audit \
  --root . --private-denylist /absolute/path/to/private-denylist.txt \
  --artifacts /absolute/path/to/open-brain-0.1.0-linux-x86_64.tar.gz \
              /absolute/path/to/open-brain-0.1.0-macos-arm64.tar.gz
```

Its complete finding set was:

```text
open-brain-0.1.0-linux-x86_64.tar.gz:open-brain: content-scan-limit-exceeded
open-brain-0.1.0-macos-arm64.tar.gz:open-brain: content-scan-limit-exceeded
```

`tools/open_brain_dev/release_audit.py:194` reads tar members, and `:239` routes them through the
same content policy as source files. `_content_rules` at `:138` rejects every member over the 2 MiB
text limit and returns before the normalized owner-denylist check. This is a confirmed incompatibility
with the shipping artifact, not evidence of a private-content match and not permission to skip the
audit. The oversized-source test at `tests/security/test_release_audit.py:151` covers the fail-closed
rule; the archive fixtures around `:285` do not exercise the actual native size or format.

The recommended fix is a bounded native-artifact audit with explicit format, member, decompression,
and content coverage. Preserve the existing source-file limit and canonical denylist. The current
native audit in `tools/open_brain_dev/base_native.py:165` inventories PyInstaller module names and
checks architecture/signature; it does not apply the owner denylist to packaged module contents.
Simply accepting larger raw byte strings would not establish coverage of compressed PyInstaller
payloads. Regression evidence should include safe large native content, private markers in packaged
content, malformed or excessive extraction, and both real platform archives. No bypass or new
history exception is proposed.

## Release-order interpretation

The plan at `docs/plans/2026-09-08-ob1-product-completion.md:611` says to run the artifact-aware audit
before hashing or publishing. The existing `make native` path calls `build_base_artifact` and
`write_release_assets` (`tools/open_brain_dev/base_native.py:250`, `:384`), which archive the verified
executable and immediately hash it into a one-platform manifest. The artifact contract explicitly
permits that provisional manifest (`docs/artifact-characterization.md:33`).

This is a plan/tooling mismatch, not proof of unsafe published bytes. The recommended resolution is
to retain normal CI manifests as provisional build outputs and require the owner artifact audit to
pass before generating the final combined manifest, formula, or release. Recompute the final digests
from exactly those audited bytes. If the owner instead requires the literal no-hash-before-audit
sequence, split archive staging from manifest generation. Both preserve final-byte identity; the
first fits the existing contributor path without adding another release subsystem. This audit does
not silently revise the accepted plan.

The sampled macOS artifact satisfies the ad hoc requirement. The validator can also accept a valid
identity signature, but that alternate path did not occur in either the recorded CI build or this
inspection and is not a blocker to this candidate.

## Remaining release evidence

The two CI archives are useful readiness specimens, not final release assets built from the eventual
protected-main commit. Their Actions retention expires on 2026-09-16. After any auditor repair or
other source change, freeze a new candidate and repeat its applicable tests, CI, and owner audits.

The final release operation must still produce both platform archives from the chosen main commit,
pass the artifact-aware audit, generate the combined exact-version manifest and formula, create a
complete draft release, and publish it with immutability enabled. GitHub's documented [draft-first
workflow](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)
allows attaching all assets before publication locks them. The [repository REST API](https://docs.github.com/en/rest/repos/repos#check-if-immutable-releases-are-enabled-for-a-repository)
provides the immutability read-back used in this audit.

After publication, download both public assets and compare their hashes with both manifest and
formula before committing the generated formula to the protected tap. Then run the exact public
acceptance block on macOS arm64 and Linux x86_64 with no prior product formula or Brain data, recording
ordinary elapsed time from `brew install` through verified export and requiring each result below
300 seconds. Runner job duration and local temporary-tap smoke do not establish that public result.

The review initially suggested adding a timer to the acceptance block. The independent refutation
correctly rejected that as a required code change: `docs/acceptance/five-minute-install.md:44` and
`docs/product-family.md:176` deliberately use ordinary elapsed timing and exclude a custom watchdog
or timing-evidence format. An owner-recorded elapsed result satisfies that requirement.

## Audit verification and limits

One coordinator owned all edits and git state. One read-only reviewer traced the release tooling;
a second independent reviewer tried to refute the findings. The coordinator reproduced the native
artifact failure using both actual CI archives. The timer finding was rejected, and the early-hashing
finding was narrowed to a plan-conformance decision. Account and repository observations came from
live GET requests; no setting was inferred from a stale handoff.

No implementation code changed, so the full 3,605-test suite was not rerun for this report. Its
successful exact-goal-head CI result was rechecked. Source/history owner audits were rerun on the
frozen candidate, and archive hashes, layout, architecture, and macOS signature were independently
checked. The artifact-aware audit failed as recorded above. Report checks and the final local audit
commit are recorded in the external workstream handoff.
