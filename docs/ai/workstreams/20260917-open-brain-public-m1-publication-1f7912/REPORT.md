# M1 publication evidence

M1 restores search visibility for approved multi-source pages and makes managed
workspace observation, cancellation and explicit owner recovery durable. The
publication contains the reviewed M1 search/recovery concern; T03 and T16 are
separate work.

The product/source tree exactly matches the final reviewed worker tree
b4029e89ff1698077438fe958861a0d015b1a86b. That tree passed make verify.
The later combined integration also passed make verify, native audit, Homebrew
smoke and packaged recovery with identical M1 source files. The publication adds
only this evidence directory. No unchanged local suite or review was repeated.

See EVIDENCE.json for exact heads and results. RECOVERY-REVIEW.md records the
original two findings; RECOVERY-REVIEW-FOLLOWUP.md resolves both.
INDEPENDENT-M1-REVIEW.md preserves the final M1 readiness assessment verbatim.

Local results: 1,930 Python tests passed with five existing macOS filesystem
skips, 23 plugin tests, 13 desktop frontend tests, one desktop Python check and
17 Rust tests. Native audit/Homebrew and packaged owner-recovery checks passed.
CI status belongs to the published PR, not this pre-publication document.

Schema6 is forward-only. Older binaries refuse the newer Brain schema.
Reverting code alone does not downgrade state. Recovery is owner CLI only;
preview is read-only and abandonment requires exact digest-bound revalidation.

This publication is authorized. Merge, release, deployment, live Brain actions,
and synchronization of main or the laptop remain separate.
