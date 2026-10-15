# W3 local acceptance

Accepted at `b36dfed44f063b5b1d98d4f0130e1a460a945147` on `feat/slack-cited-recall-w3-20261015-86014d`. Independent GPT-6 Astra approved this exact HEAD with no unresolved findings and independently matched all 1,897 frozen input hashes. W3 is tests and documentation; runtime, native, packaging and compatibility inputs are unchanged from accepted W2.

| Check | Result |
| --- | --- |
| make verify | Passed in 2241.94s: 7474 Python tests with 5 filesystem skips; 700 plugin; 286 desktop frontend; 30 desktop Python; 26 Rust; package builds, lint and 673-file typecheck passed |
| diff check/actionlint | Passed on the exact tested HEAD |
| Focused review | 73 combined cases; final deterministic repair 1; independent 50 plus final partial-line 1 |
| Guide | 18 metadata-only help surfaces, 19 actual-parser commands, five JSON templates and four links verified under an empty synthetic HOME |
| Source freeze | All 1897 input hashes/inventory unchanged across complete gate; digest d90ecee75ed92693190f96acfacaf3b88da17cc3f1bcaa67a8e94828eaa172c2 |

The five skips reflect filesystem rejection of non-UTF-8 names and inability to preserve distinct NFC/NFD names. An existing multithreaded-fork deprecation warning was reported. Complete failed/focused logs and retained synthetic fixtures remain outside the checkout; exact paths and receipts are in ACCEPTANCE.json.

The first full gate passed all 7474 Python cases and package builds, then failed eight plugin children because their existing fixture launches checkout/.venv/bin/open-brain directly. The fresh worktree used only external UV_PROJECT_ENVIRONMENT. An actual ignored .venv directory now contains a bin alias to /tmp/ob-w3-venv/bin, preserving the same current-worktree source and environment. All eight failed cases passed after that local prerequisite, then the full gate passed again. No test or runtime limit was weakened. The ignored environment alias is rebuildable and was not committed.

The previous W2 worktree was removed only after its five dirty/untracked continuity files were SHA-256 matched in the old tree, new tree and preservation commit b39c2ef. Its tip 8eb36e5ac97318537ab19aa4163c38cd0d03ff0d is an ancestor. Dirty inventory was unchanged, ignored files rebuildable and no process used it. Exact removal and unchanged-ref evidence are in CLEANUP.json.

Removed worktree: /Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w2-20261014-d5fe13

Retained checkpoint branch: feat/slack-cited-recall-w2-20261014-d5fe13

Cited recall tests use real Slack parsing, collector admission, engine sharing and a real scoped stdio child with synthetic HTTP only. They prove bounded search/full reads, structured selection traversal, exact returned copy/revision/quote validation, hostile nested JSON remaining inert, Unicode, partial/budget failures and 9/24-member contexts with single-member reapproval repair. These are deterministic protocol checks, not proof of real-model reasoning or injection resistance.

No actual-client check, provider inference, real Slack access, private pilot, W4, push, merge or deployment occurred. Actual-client status remains not_run. The separately recorded injection-resistance pilot gate remains mandatory. Native audit/Homebrew smoke were not rerun for this tests/docs-only milestone; W2's native results remain historical evidence, not a new W3 native receipt.

Final HANDOFF.json/MD, STATE.md and these ACCEPTANCE documents were written after the checks and remain local continuity bound to the unchanged tested HEAD. Historical W2 packets remain byte-exact. The next action is owner review of the local W3 packet before separately authorizing further work.
