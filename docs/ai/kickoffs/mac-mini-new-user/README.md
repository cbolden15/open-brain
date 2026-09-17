# Mac mini implementation kickoff

Use one Mac mini coordinator plus three Mac mini worker sessions. Keep this laptop out of implementation builds and tests. This kit contains instructions, not evidence that sessions, commits, pushes, PRs or merges have occurred.

Preparation baseline: repository root `.`, branch `main`, HEAD and remote `origin/main` at `5cf081aa3d591e14b89245db290d4a186ff8156a`. Origin is `https://github.com/cbolden15/open-brain.git`. The plan/assessment/review/workstream files and this kit were untracked when prepared. This kit is included in the documentation bootstrap PR; after that PR is merged, start at section 2 rather than creating it again. Recheck actual Git state before acting. Mac mini hardware, checkout, tools, account access and filesystem paths have not been inspected.

GitHub policy checked during preparation: squash merges allowed; merge commits and rebase merges disabled. Reconfirm at merge time. Local rebasing of an unfinished feature branch is different from GitHub's disabled rebase-merge method.

## 1. Transfer the plan through a small bootstrap PR

Recommended: on the laptop, publish and merge a documentation-only bootstrap PR before starting mini workers. This makes one reviewed commit the shared source of truth. It is a small Git/docs operation, not implementation/build work. Allow roughly 15–30 minutes for setup if the mini already has the required tools; installation/CI delays are additional.

Ask the laptop session to perform this bounded task when ready:

> Create a documentation-only bootstrap branch from current origin/main for the new-user implementation plan and Mac mini kickoff kit. Preserve all unrelated work. Inspect and stage only the listed plan, assessment, review, planning-workstream and kickoff paths. Check JSON, links, whitespace and private-data exposure. Commit using a message file with git commit -F and truthful attribution. Push that named branch, open a PR to main, report the exact PR/head and wait for required checks/review. Do not merge without my explicit approval or a separate approval of a named checks-gated merge policy. Do not implement product code or run native builds on this laptop.

Exact repository-relative staging scope:

```text
docs/plans/2026-09-16-new-user-functionality.md
docs/plans/2026-09-16-new-user-functionality-coverage.json
docs/plans/2026-09-16-backup-restore-follow-up.md
docs/audits/2026-09-16-new-user-functional-assessment.md
docs/audits/2026-09-16-new-user-plan-review.md
docs/audits/2026-09-16-new-user-plan-review.json
docs/ai/workstreams/20260916-open-brain-public-new-user-functionality-plan-60f7da/
docs/ai/kickoffs/mac-mini-new-user/
```

Do not use `git add -A`. Do not transfer a live Brain, credentials, `.venv`, `node_modules`, build trees or private runtime data. Do not try to copy a linked worktree from the laptop to the mini; create its worktrees from the mini's own clone. A smaller alternative is pushing only the bootstrap branch and explicitly basing all mini work on its pinned SHA, but then implementation must wait for/reconcile its merge. Merging the bootstrap first is simpler.

After the approved bootstrap squash merge, confirm the PR state is MERGED and fetch its actual merged `origin/main` SHA. Do not assume that requesting auto-merge means it has already merged.

## 2. Prepare the Mac mini and four isolated worktrees

Use a local SSD checkout, not a cloud-synced working directory. Install/sign in to the normal Codex and GitHub clients on the mini; do not copy laptop authentication files. Confirm Git, Codex, `gh`, Python 3.14/uv and the project's required Node/Rust/native toolchain. Check the installed workflow skills; do not blindly reuse laptop skill paths. Run setup/installation serially and follow the current repository instructions. Native GUI and Linux evidence remain separate gates; a Mac mini cannot certify Linux by inference.

Clone the verified origin if no clone exists. Otherwise inspect the existing checkout and preserve every dirty/untracked path before updating. The following setup is for an existing, clean normal clone. Replace the first path with the mini's actual absolute path; the other paths are derived from it. Run each block only after its precondition is met.

```sh
OB_REPO="/absolute/path/on/mac-mini/open-brain-public"
OB_WT="${OB_REPO}-worktrees"
git -C "$OB_REPO" status --short --branch
git -C "$OB_REPO" worktree list
git -C "$OB_REPO" remote -v
```

Stop if this is the wrong repository, contains work that would be displaced, or has existing assignments with the same branch/path names. A clean status is a precondition, not something these inspection commands enforce. Once inspected:

```sh
git -C "$OB_REPO" fetch origin
git -C "$OB_REPO" switch main
git -C "$OB_REPO" merge --ff-only origin/main
git -C "$OB_REPO" rev-parse HEAD
```

Confirm the bootstrap plan/kit exists in that commit. If fast-forward fails, stop and inspect divergence; no reset, force, automatic stash or conflict guessing. Then create unique worktree branches from the same fetched commit:

```sh
git -C "$OB_REPO" worktree add -b ob-new-user/coordinator "$OB_WT/coordinator" origin/main
git -C "$OB_REPO" worktree add -b ob-new-user/m1-search "$OB_WT/m1-search" origin/main
git -C "$OB_REPO" worktree add -b ob-new-user/contracts "$OB_WT/contracts" origin/main
git -C "$OB_REPO" worktree add -b ob-new-user/recall-research "$OB_WT/recall-research" origin/main
```

The plain clone can remain on `main` with no coding session in it. These four worktrees each own a distinct branch. Commands intentionally fail rather than overwrite existing branches/paths. Do not add `-B`, force flags, or cleanup commands to make them succeed.

## 3. Start the four sessions on the mini

Open four local terminal tabs on the mini, or run them inside a persistent terminal multiplexer on the mini over an already-configured SSH connection. If using SSH, the agent process must run on the mini, not locally on the laptop with a mounted project. Do not expose an unauthenticated remote agent server. Screen sharing is also a way to control sessions already running on the mini.

| Session | Preferred model / effort | Initial assignment | Prompt file within its worktree |
|---|---|---|---|
| Coordinator | gpt-6-astra / high | Assign, review, integrate, verify; no broad implementation | docs/ai/kickoffs/mac-mini-new-user/coordinator.md |
| Search fix | gpt-6-astra / high | M1 T01–T02 only | docs/ai/kickoffs/mac-mini-new-user/m1-search.md |
| Contracts | gpt-6-astra / high | T03 design/fixtures only, no migration | docs/ai/kickoffs/mac-mini-new-user/contracts.md |
| Recall research | gpt-5.6-sol / high | Preliminary T16 feasibility/fixture design | docs/ai/kickoffs/mac-mini-new-user/recall-research.md |

These pins follow the plan's risk allocation. Confirm availability in the mini's client; if missing, have the coordinator approve an available equivalent rather than silently inherit a default. Later bounded adapter work uses Sol and mechanical inventory/docs checks can use Luna. Do not use a premium research worker merely because its parent session is premium.

Launch the coordinator first. The CLI flags below were verified against laptop Codex CLI 0.154.0 and official documentation; confirm `codex --help` on the mini before use. In each new tab, redefine the two path variables because shell variables are not automatically shared between tabs.

```sh
OB_REPO="/absolute/path/on/mac-mini/open-brain-public"
OB_WT="${OB_REPO}-worktrees"
codex -C "$OB_WT/coordinator" -m gpt-6-astra -c 'model_reasoning_effort="high"' -s workspace-write -a on-request --add-dir "$OB_REPO/.git"
```

Its first chat message is a pointer to the file, not the whole plan pasted into argv:

```text
Read docs/ai/kickoffs/mac-mini-new-user/coordinator.md and follow its first-wave scope. Prepare the assignment ledger and confirm the three worker write scopes before I start them. No push, PR or merge yet.
```

Once the coordinator publishes the pinned base and ownership assignments, open the other three tabs. Run one line per tab after setting the same path variables:

```sh
codex -C "$OB_WT/m1-search" -m gpt-6-astra -c 'model_reasoning_effort="high"' -s workspace-write -a on-request --add-dir "$OB_REPO/.git"
codex -C "$OB_WT/contracts" -m gpt-6-astra -c 'model_reasoning_effort="high"' -s workspace-write -a on-request --add-dir "$OB_REPO/.git"
codex -C "$OB_WT/recall-research" -m gpt-5.6-sol -c 'model_reasoning_effort="high"' -s workspace-write -a on-request --add-dir "$OB_REPO/.git"
```

In each, ask it to read its corresponding prompt file, then supply the coordinator's assignment path/commit. The common Git metadata write allowance is needed for commits in linked worktrees; it does not authorize changing another worker's branch. No approval/sandbox bypass flags are needed. GUI alternative: open each existing worktree as a separate project/chat and select its model; do not accidentally create an additional worktree or detached HEAD over it.

Resource and coordination contract:

1. One heavy job at a time: full verification, native/Rust build, large indexing or model benchmark. The coordinator owns this token. Other sessions may research/edit or run bounded targeted checks within the agreed memory budget.
2. Keep independent `.venv`, build outputs and mutable runtime/test roots per worktree. Use synthetic Brain paths. Avoid four simultaneous dependency installs or watchers. Start conservative; raise concurrency only after observing the mini's memory pressure.
3. Four sessions are the total initial fleet. No nested fleets. Each worker commits only its owned changes and writes its own generated workstream report. The coordinator alone changes master coverage/assignments.
4. Separate interactive sessions do not automatically inherit this conversation or relay completion messages to each other. The coordinator discovers reports through the explicit assignment ledger and absolute worktree paths; use the available completion-monitoring mechanism or a bounded monitor. Do not spin-poll or leave an unwatched background job.
5. Workers return exact local commit SHAs, test evidence and concerns, never pushes. An idle non-author worker can review another worker's patch after a fresh bounded assignment. The coordinator is not a substitute for every independent review.

Published project paths are repository-relative; `.` denotes the checkout root. Any historical absolute project reference should be resolved by its repository-relative suffix in the mini worktree. Never run an old scratch validator path or claim the old handoff baseline is the new current HEAD. Create fresh per-session workstream state.

## 4. Commit, integrate, push, PR and squash merge

Use a small PR per verified concern/milestone, starting with M1. Do not hold the entire 23-task program on one long-lived branch. T03 and research reports need not be bundled with the M1 product fix. After the first wave, record a checkpoint and issue new bounded assignments from the actual merged main; opening four sessions is not authorization for every migration or external action in the plan.

Workers stage explicit owned paths and commit using a message file with `git commit -F`. The coordinator reads each diff/report, arranges independent review, and cherry-picks the selected tested worker commits into a fresh review branch based on current `origin/main` in its own worktree. It must be clean before switching/rebasing. If origin moved, integrate the new base before final verification, not after.

Coordinator checks on the integrated candidate: `make verify`, `git diff --check`, and `actionlint .github/workflows/ci.yml`; native/package changes also need `make native-audit` and `make homebrew-smoke`. Run required checks with suitable scoped approval if sandbox restrictions prevent real verification. Report failures/skips honestly. Docs-only PRs use appropriate document/fixture validation. Do not claim a full suite passed because a worker ran one file.

When ready, give the coordinator explicit authority, for example:

> For the named M1 review branch in cbolden15/open-brain, you may push, open/update its PR to main, and squash-merge after independent review, all required checks and approvals pass on the exact candidate head. Do not bypass protections. This does not authorize releases, deployment, credential/OAuth changes, live Brain operations, unrelated PRs or writing to the laptop.

The checks-gated sequence is:

```sh
git -C "$OB_WT/coordinator" push -u origin "$OB_REVIEW_BRANCH"
gh pr create --repo cbolden15/open-brain --base main --head "$OB_REVIEW_BRANCH" --title "$OB_PR_TITLE" --body-file "$OB_PR_BODY"
gh pr checks "$OB_PR_NUMBER" --repo cbolden15/open-brain --watch
gh pr view "$OB_PR_NUMBER" --repo cbolden15/open-brain --json headRefOid,reviewDecision,mergeStateStatus,statusCheckRollup
```

Here the coordinator sets the named branch, PR body file and exact PR number from verified results. After checking approvals, branch protections and the same reviewed head, use:

```sh
gh pr merge "$OB_PR_NUMBER" --repo cbolden15/open-brain --squash --match-head-commit "$OB_VERIFIED_HEAD"
gh pr view "$OB_PR_NUMBER" --repo cbolden15/open-brain --json state,mergedAt,mergeCommit
```

Do not use `--admin`, force-push main, or append automatic branch/worktree deletion. If checks are missing, failing or a merge queue prevents immediate completion, wait through the supported mechanism and verify actual MERGED state. A permission/approval failure is not a reason to bypass policy. For an authorized update to a rebased published review branch, inspect its remote head first and use an explicit expected-OID force-with-lease; never a blind force. Starting a fresh review branch avoids that rewrite when practical.

After squash merge, the worker commit IDs are not the canonical main history. Mark that task complete, retain its receipt, and start the next task branch from new `origin/main`. Do not blindly rebase/reuse the completed worker or integration branch; that can replay already-merged work. For partially completed stacked work, first identify the precise unmerged commits and port only those to a fresh branch, or perform an explicitly reviewed `rebase --onto` with verified boundaries.

## 5. Synchronize both machines without touching unrelated work

Pause writers in the specific checkout before updating it. Other laptop projects need not stop. Confirm there is no pending rebase/cherry-pick and no dirty/untracked work that would be displaced. If there is unfinished work, preserve it on its own branch before proceeding; never use reset/clean or an automatic stash to manufacture a clean status.

Run this separately in the normal clone on each machine, with that machine's own absolute path, after confirming it is safe to switch to main:

```sh
OB_REPO="/absolute/path/on/this/machine/open-brain-public"
git -C "$OB_REPO" status --short --branch
git -C "$OB_REPO" fetch origin
git -C "$OB_REPO" switch main
git -C "$OB_REPO" merge --ff-only origin/main
git -C "$OB_REPO" rev-parse HEAD
git -C "$OB_REPO" rev-parse origin/main
git -C "$OB_REPO" status --short --branch
```

Both hashes must match on each machine and match the same fetched remote snapshot. Recheck origin if another merge occurred during synchronization. If `main` has local-only commits or fast-forward fails, stop and reconcile them onto an appropriate feature branch; do not rewrite main. The mini coordinator cannot claim the laptop is synchronized without actual laptop evidence or explicitly authorized remote access.

Only an unfinished feature branch based on old main, containing no already-squashed task history, normally needs:

```sh
git -C "$OB_ACTIVE_WORKTREE" fetch origin
git -C "$OB_ACTIVE_WORKTREE" rebase origin/main
```

Do this only while that worktree's session is paused and clean. Resolve conflicts in its owned changes, rerun affected tests, and update the handoff. If the feature branch was published, coordinate with its owner before any expected-OID force-with-lease. Completed task branches instead get replaced by fresh task branches from updated main; cleanup is a separate verified step.

The final receipt names the PR/merged commit, exact tested candidate, mini main SHA, laptop main SHA, outstanding feature branches and any failed/skipped checks. A stale remote-tracking ref or matching branch name is not synchronization evidence.

## Source and preparation notes

Official [worktree guidance](https://learn.chatgpt.com/docs/environments/git-worktrees) confirms that repository commands stay on the machine hosting the worktree and that branches are exclusive to a worktree. [CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli) documents the model, working-directory, sandbox, approval and extra writable-directory flags used here. The local CLI and GitHub help were also checked; the mini installation is still a preflight item.

The prompts were rendered with the workflow-governance phase template. Fan-out rules define exclusive ownership and worker reporting; commit-push-PR rules define explicit staging, verification and value-first PR bodies. No code implementation, remote session, Git branch, commit, push, PR or merge was performed while preparing this kit.
