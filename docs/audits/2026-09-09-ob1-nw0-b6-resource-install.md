# NW0-B6 native resource installation proof

- Date: 2026-09-09.
- Baseline: `306803d` on `docs/ob1-native-workspace-plan`.
- Scope: private macOS arm64 feasibility, synthetic fixtures, zero model calls.
- Result: paired-resource installation passed and was independently reviewed. NW0-B remains open.

## Candidate distribution contract

One Homebrew formula owns both native resources. The base installs at `bin/open-brain`; Graphify
installs at `libexec/open-brain-graphify`. The helper gets no global command link. Both executable
payloads are identical to the reviewed [B5 candidate](2026-09-09-ob1-nw0-a3-b5-feasibility.md).

Each distinctly named archive contains one regular executable named `open-brain`, preserving the
unchanged native content auditor and its existing size and work limits. Both pass arm64 signature
checks and reproduce from identical binaries and archive filenames. This does not establish
bit-for-bit compiler reproducibility.

| Resource | Archive | SHA-256 |
|---|---|---|
| Base | `open-brain-0.1.0-macos-arm64.tar.gz` | `92a8e145b7071dec8546720296dfa45d41785d65a8c018d3a86e180b24a5733f` |
| Graphify | `open-brain-graphify-0.1.0-macos-arm64.tar.gz` | `a3d9e04063fdbcc7abab329b35c390cc7d0db9925942a5d3a16c4c1cba88d556` |

The pair totals 18,285,272 compressed bytes. The helper's archive digest changes with its new gzip
filename; its executable digest remains
`80cc7cd520ce63d90d258bef63d40937df5614e7ad3518ca0d7dffe9b2af5973`.

A closed private component manifest records role, platform, release, archive and executable digests,
and fixed installation destination. It rejects incomplete or duplicate pairs, unknown roles and
platforms, mismatched filenames and versions, invalid digests, arbitrary destinations and unsafe
formula origins. The current shipping manifest rejects this pair. Shipping support therefore needs
an explicit manifest and renderer change; the private format is neither shipping schema nor signed
release authority.

The alternatives remain visible. The combined B4 candidate fails its observed warm-start budget.
Independent formulas introduce version skew. A two-executable archive fails the current archive
contract. The paired-resource candidate retains Homebrew ownership and separate content inspection
without weakening either contract.

## Actual Homebrew lifecycle

The existing ownership guard restricted the run to the reserved `open-brain-local/smoke` tap and
keg-only `open-brain-smoke` formula. The real lifecycle exercised:

1. Install fixture revision 0 and verify both exact executable digests and destinations.
2. Upgrade to revision 1 and verify activation of the complete pair in a new keg.
3. Reject a bad helper digest while preserving the previous active pair.
4. Reject an injected failure after the base copy and before helper staging, preserving the old pair
   and removing the incomplete new keg; then recover with a successful revision 2 upgrade.
5. Remove the owned smoke kegs and tap, and verify that the installed product and global helper-link
   state are unchanged.

All formula revisions use identical B5 binaries. This proves Homebrew resource management, not
cross-version helper compatibility or atomicity under every crash. The unchanged base does not
discover or invoke the helper.

The installed base passed the complete native smoke: self-check, capture, search, export, status,
doctor, Markdown import and local MCP. The installed helper passed all 20 frontmatter checks with
the pure parser and no native YAML accelerator.

Local-file fixture installation took 2.794 seconds; the complete base smoke took 101.216 seconds.
These are diagnostic elapsed times on this Mac, not cold-download, clean-machine, native Linux,
startup-series or five-minute setup evidence. No new startup measurements were taken.

## Preserved failed attempt and recovery

The first private harness set `HOMEBREW_NO_INSTALL_FROM_API`. While indexing the local tap,
Homebrew unexpectedly began cloning its shared core tap. The coordinator stopped the owned
harness, identified and terminated the orphan clone and its descendants, and verified that git
removed the incomplete checkout. No matching clone process remained. Product verification passed.
The failed scripts, logs and receipt were retained, and the interrupted effort remains charged.

The successful attempt omitted that flag, kept auto-update disabled, used the synthetic home and
private Homebrew cache/log directories, and forbade core/cask formula installation. It completed
without a core checkout. This establishes recovery from the observed incident, not general
Homebrew sandboxing or complete descendant-process cancellation.

## Verification and remaining gates

Four grouped manifest/formula tests and Ruff passed. The artifact proof recorded 13 checks; the
Homebrew lifecycle recorded 14. A verifier reconciled 48 checks against exact hashes, generated
formula content, raw failures, smoke results, installed-pair receipts and final cleanup. An independent
review reproduced the four tests and 48 evidence checks, inspected the underlying receipts and
found no new must-fix issue.

No shipping code, spec, formula, manifest, dependency or lockfile changed. No build was repeated.
Public documentation received link, private-content, whitespace and workflow checks before commit.

Native Linux execution, controlled cold timings, Linux warm timings and shipping integration remain
open. The helper still exposes only private diagnostic modes. Runtime work must resolve one concrete
installation, verify the selected helper's identity and protocol, reject absence or mismatch, and
avoid mixing a running base with a moving Homebrew pointer during upgrades. That boundary has not
been implemented by B6.

The coordinator owns that next private discovery/compatibility proof. A4 authority work and C9
synthetic API transport work proceed in separately owned private scopes. Claude remains closed at
C7, Codex remains deferred, and NW1 cannot begin before all required NW0 gates pass.
