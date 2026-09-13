# Open-source repository readiness report

- Date: 2026-08-26
- Repository at audit: `vora-technology/open-brain`
- Current repository: `cbolden15/open-brain`
- Visibility at audit: private
- Audited commit: `07b8e50b586ec9ecab272b8ea4352ec9a0e9e664`

## Ownership disposition (2026-09-08)

This report preserves the 2026-08-26 repository snapshot. The repository was transferred to the
personal account `cbolden15` on 2026-09-08, and the Homebrew tap will be
`cbolden15/homebrew-tap`. Organization team and organization-plan recommendations below describe
the audit-time repository and are superseded by the ownership and release gate in
[`../plans/2026-09-08-ob1-product-completion.md`](../plans/2026-09-08-ob1-product-completion.md).
Package links intended for the first release use `https://github.com/cbolden15/open-brain`.

## Executive assessment

Open Brain has a strong engineering base, but it is not ready to invite public
contributions yet. The code is licensed, tested, packaged, and guarded by a custom public
release audit. The missing work is mostly around people and operations: contributor
expectations, governance, support, repository protection, dependency maintenance, security
intake, and releases.

The safest path is to keep the repository private while preparing these files and settings.
When the launch checklist is complete, make the repository public and immediately enable the
security and ruleset features that are only available to public repositories on the
organization's current GitHub plan.

### What is already in good shape

- Apache-2.0 is declared in `LICENSE`, `NOTICE`, and `pyproject.toml`. GitHub recognizes the
  repository license as Apache-2.0.
- `README.md`, `CONTRIBUTING.md`, and `SECURITY.md` exist. GitHub currently reports a 71%
  community profile.
- CI tests Python 3.12, 3.13, and 3.14. It runs Ruff, mypy, 2,683 tests, and a package build.
- GitHub Actions use read-only `GITHUB_TOKEN` permissions by default and cannot approve pull
  requests.
- A release audit checks source and built artifacts for private paths, credentials, private
  network addresses, prohibited file types, and private denylist terms. Gitleaks is also in
  the release-audit workflow.
- `uv.lock` is committed, generated artifacts are ignored, and release builds create both a
  wheel and source distribution.
- Dependabot alerts and Dependabot security updates are enabled in repository settings.
- Public deployment configuration, credentials, captures, and live state are intentionally
  excluded from the application repository.

## Current-state map

| Area | Current state | Assessment |
|---|---|---|
| License | Apache-2.0 license and notice are committed | Strong, pending Vora legal-owner confirmation |
| README | Architecture and development commands are present | Partial; missing install, first-use, support, and public roadmap guidance |
| Contributing | Test commands and synthetic-fixture rule are documented | Partial; missing PR process, review expectations, and contributor workflow |
| Code of conduct | Missing | Required before public launch |
| Support policy | Missing | Required before public launch |
| Governance and maintainers | No governance, maintainer list, team, or CODEOWNERS | Required before public launch |
| Issue and PR intake | No issue forms or pull-request template; nine default labels only | Required before public launch |
| Security policy | Present, but points only to a public-only reporting feature | Partial; needs a working private contact and supported-version table |
| Branch protection | No usable ruleset while this private repo is on GitHub Free for organizations | Must be enabled immediately after public launch |
| Actions security | Least-privilege token is configured | Partial; actions use mutable tags and SHA pinning policy is off |
| Dependency maintenance | Alerts and security updates are on | Partial; no Dependabot version-update configuration for uv or Actions |
| Static security analysis | Release audit and Gitleaks exist | Partial; CodeQL and Scorecard are unavailable while private and not configured |
| Releases | No tags, GitHub releases, changelog, release guide, or publishing workflow | Missing |
| Python metadata | Core package metadata is valid | Partial; project URLs, maintainer metadata, and import-name metadata are missing |
| GitHub presentation | Description exists; topics are empty; Discussions are disabled | Partial |
| Merge hygiene | All three merge methods are enabled; merged branches are retained | Needs a documented merge policy |

## Priority 0: complete before making the repository public

These items form the public-launch gate.

### 1. Replace the README's internal status with a user path

The README explains the architecture but does not tell a new user how to install or use Open
Brain. Its status section references internal phase and capability language that will not help
an outside user evaluate the project.

Add:

- a one-paragraph problem statement and a short list of intended users;
- an explicit alpha warning and supported operating systems;
- source-install instructions until a package release exists;
- a five-minute local quickstart using synthetic data;
- one concrete capture example and the expected output location;
- links to configuration, architecture, threat model, support, contributing, and security;
- a public roadmap section that distinguishes available features from planned features;
- CI, license, Python-version, and release badges once their targets exist.

Move the Goal #24 and capability-row explanation into an internal history document or remove it
from the public branch.

### 2. Add the missing community health files

GitHub's community checklist looks for a README, license, contributing guide, code of conduct,
security policy, and valid issue templates. The repo currently has three of those contributor
files plus its license. GitHub documents these files as the standard way to help people decide
whether and how to participate: [community profile guidance](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories).

Add:

- `CODE_OF_CONDUCT.md`, preferably Contributor Covenant 2.1, with a company-controlled
  enforcement address and a real enforcement owner;
- `SUPPORT.md`, separating bug reports, usage questions, feature requests, and security reports;
- `.github/ISSUE_TEMPLATE/bug.yml`;
- `.github/ISSUE_TEMPLATE/feature.yml`;
- `.github/ISSUE_TEMPLATE/config.yml`, with blank issues disabled and links to support and
  security guidance;
- `.github/PULL_REQUEST_TEMPLATE.md`, covering tests, privacy impact, docs, compatibility,
  release notes, and synthetic fixtures.

GitHub recommends adopting a code that the maintainers are prepared to enforce, including a
clear process for handling abuse: [code of conduct guidance](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/adding-a-code-of-conduct-to-your-project).

### 3. Establish ownership and governance

At audit time, only `cbolden15` had repository access, as an administrator. There was no ownership
map or decision process.

Add:

- `GOVERNANCE.md` with project scope, decision rights, maintainer responsibilities, release
  authority, conflict resolution, and the process for adding or removing maintainers;
- `MAINTAINERS.md` listing `@cbolden15` and any future collaborators with their roles;
- `.github/CODEOWNERS`, initially owned by `@cbolden15`, with explicit ownership for `.github/`,
  `SECURITY.md`, packaging metadata, release code, privacy code, and migration code.

GitHub can automatically request owners for changed files and can require their approval.
GitHub also recommends protecting the CODEOWNERS file itself: [CODEOWNERS guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners).

For a one-maintainer launch, document an interim exception instead of creating a rule that no one
can satisfy. A personal repository has only owner and collaborator roles, so the long-term
two-maintainer setup uses a named collaborator and required review rather than an organization team.
Do not use routine owner bypass.

### 4. Make the security policy usable before and after launch

`SECURITY.md` currently tells researchers to use GitHub private vulnerability reporting, but
that feature is only available for public repositories and is not enabled now. There is no
fallback contact.

Update `SECURITY.md` with:

- a monitored Vora security email address;
- a supported-versions table;
- what information a report should contain;
- target acknowledgement and status-update timelines;
- coordinated-disclosure expectations;
- scope boundaries for local deployments and third-party providers;
- the GitHub private-reporting link once it is enabled.

Private vulnerability reporting provides a structured, non-public intake path and should be
enabled immediately after publication: [GitHub private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository).

### 5. Resolve the company and contribution-policy decisions

Before publication, Vora should record:

- the exact legal entity that owns the initial copyright;
- whether `NOTICE` should say “Vora Technology” or a specific legal name instead of only
  “Open Brain contributors”;
- whether contributions use the current inbound-equals-outbound Apache-2.0 statement, a
  Developer Certificate of Origin, or a contributor license agreement;
- who may approve releases and security advisories;
- whether “Open Brain” needs a trademark statement or a name-conflict review.

Recommendation: keep inbound-equals-outbound and add DCO sign-off only if Vora wants an explicit
contributor attestation. Use a CLA only if counsel identifies a real need such as dual licensing
or copyright assignment. This is a company policy decision, not a tooling default.

### 6. Prepare the public `main` ruleset

The API currently rejects ruleset and branch-protection configuration because this is a private
organization repository on GitHub Free. GitHub makes rulesets available to public organization
repositories on the free plan: [GitHub rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets).

Prepare the ruleset definition while private, then activate it immediately after changing
visibility. Target `main` and require:

- no force pushes or branch deletion;
- changes through pull requests;
- successful CI checks for every supported Python version;
- successful release-audit checks when release-sensitive paths change;
- resolved review conversations;
- linear history;
- one approving review and dismissal of stale approvals once a second maintainer exists;
- Code Owner approval for security, workflow, packaging, and release changes;
- tightly scoped, auditable bypass access.

OpenSSF treats branch protection, reviews, and required status checks as high-value controls
against malicious or accidental changes: [Scorecard check documentation](https://github.com/ossf/scorecard/blob/main/docs/checks.md#branch-protection).

### 7. Harden GitHub Actions dependencies and triggers

The workflows correctly declare `contents: read`, but all three external actions use mutable
major-version tags. Repository policy allows all actions and does not require SHA pinning.
GitHub states that a full commit SHA is the only immutable way to reference an action:
[secure use of GitHub Actions](https://docs.github.com/en/actions/reference/security/secure-use#using-third-party-actions).

Change:

- pin `actions/checkout`, `astral-sh/setup-uv`, and `gitleaks/gitleaks-action` to reviewed full
  commit SHAs, retaining the release tag in a comment;
- require full-SHA pinning in repository or organization Actions policy;
- restrict allowed actions to GitHub-owned actions and an explicit allowlist of reviewed
  third-party actions;
- add job timeouts and workflow concurrency cancellation for pull requests;
- keep workflow permissions read-only except for narrowly scoped release jobs;
- run the release audit on pushes to `main`, release tags, and all changes to public docs,
  workflows, package metadata, security policy, and community files;
- keep the private denylist gate outside public CI while retaining the generic public scanner
  and Gitleaks in CI.

## Priority 1: enable on the public-launch day

Do these immediately after the visibility change. Public visibility unlocks several controls on
the organization's current plan.

1. Activate the prepared `main` ruleset and confirm direct pushes, force pushes, and branch
   deletion are blocked as intended.
2. Enable CodeQL default setup for Python and GitHub Actions. GitHub recommends starting with
   default setup: [CodeQL default setup](https://docs.github.com/en/code-security/code-scanning/enabling-code-scanning/configuring-default-setup-for-code-scanning).
3. Confirm secret scanning is active, enable push protection if available, and resolve any
   historical alerts. GitHub automatically provides secret scanning for public repositories:
   [secret scanning](https://docs.github.com/en/code-security/secret-scanning/introduction/about-secret-scanning).
4. Enable private vulnerability reporting and subscribe at least two maintainers to security
   notifications.
5. Add `.github/dependabot.yml` with weekly `uv` and `github-actions` updates, grouped to avoid
   notification noise. Dependabot supports both ecosystems and scheduled update pull requests:
   [Dependabot configuration](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference).
6. Add the OpenSSF Scorecard workflow and publish results through code scanning. Scorecard checks
   branch protection, token permissions, pinned dependencies, security policy, SAST, dependency
   updates, and release practices: [OpenSSF Scorecard checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md).
7. Set repository topics such as `knowledge-management`, `local-first`, `privacy`, `python`,
   `second-brain`, and `provenance`.
8. Enable Discussions for usage questions and design proposals. Keep Issues for actionable bugs
   and accepted feature work.
9. Select one merge policy. Recommendation: squash merge for contributor pull requests, disable
   merge commits, and automatically delete merged branches.

## Priority 2: complete before the first package release

### Package identity and metadata

The package metadata is valid, but it lacks links and maintainer information. Add:

```toml
[project]
maintainers = [{ name = "Vora Technology" }]
import-names = ["open_brain"]

[project.urls]
Homepage = "https://github.com/cbolden15/open-brain"
Documentation = "https://github.com/cbolden15/open-brain#readme"
Repository = "https://github.com/cbolden15/open-brain"
Issues = "https://github.com/cbolden15/open-brain/issues"
Changelog = "https://github.com/cbolden15/open-brain/blob/main/CHANGELOG.md"
```

The Python packaging specification defines maintainers, project URLs, license expressions, and
import names as standard project metadata: [pyproject metadata](https://packaging.python.org/en/latest/specifications/pyproject-toml/#project-metadata).

Remove the deprecated `License :: OSI Approved :: Apache Software License` classifier because
the SPDX `license = "Apache-2.0"` expression already carries the license metadata.

As of this audit, the PyPI and TestPyPI JSON APIs return 404 for `open-brain`, so no release is
visible under that exact distribution name. Verify availability again immediately before the
first release. A separate project named `openbrain` already exists, so include name-confusion
and trademark checks in the release decision.

### Release process

Add:

- `CHANGELOG.md` with a clear policy for unreleased changes;
- `docs/releasing.md` covering version selection, release notes, rollback or yanking, and the
  security-release path;
- a decision on semantic versioning and the stability guarantees attached to `0.x` releases;
- a release workflow triggered by an approved GitHub release, not an arbitrary branch push;
- a protected `pypi` environment with required review;
- PyPI Trusted Publishing through GitHub OIDC instead of a long-lived API token;
- wheel and source-distribution attestations;
- a GitHub release containing the exact artifacts published to PyPI, their SHA-256 checksums,
  and release notes;
- an SBOM attached to each release if Open Brain will be distributed as an application as well
  as a Python package.

The Python Packaging User Guide documents GitHub Actions publishing, and PyPI Trusted Publishing
uses short-lived identities instead of stored upload tokens:
[publishing from GitHub Actions](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/),
[PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
PyPI attestations bind release files to a publisher identity and digest, but they do not replace
review and release controls.

## Priority 3: ongoing maintainer operations

- Triage issues and dependency alerts on a published schedule.
- Publish a support window and deprecation policy before declaring a stable release.
- Review maintainer, team, ruleset, Actions, and security-notification access quarterly.
- Run Scorecard and release-audit checks on a schedule, not only when code changes.
- Rotate or remove unused repository and organization secrets. Prefer OIDC where supported.
- Keep `NOTICE` current when vendored assets or dependencies require attribution.
- Maintain `good first issue` and `help wanted` labels only when a maintainer can respond.
- Publish release notes and changelog entries for every user-visible change.
- Measure response time, stale issues, dependency-update latency, and release frequency. Avoid
  collecting metrics that the team will not review.

## Recommended implementation sequence

### Phase A: contributor-ready while private

Estimated effort: one focused day for files and workflow updates, plus legal and ownership
decisions.

1. Rewrite the README and expand `CONTRIBUTING.md`.
2. Add community templates, support, conduct, governance, maintainers, and CODEOWNERS.
3. Update `SECURITY.md` with a working company contact and response policy.
4. Pin Actions, add Dependabot configuration, and widen release-audit triggers.
5. Prepare ruleset and public-launch scripts without changing visibility.

### Phase B: controlled public launch

Estimated effort: one to two hours after the required company identities and settings exist.

1. Run `make verify` and the private release audit against a clean checkout and built artifacts.
2. Review the full Git history, open branches, tags, Actions logs, and repository metadata for
   information that should not become public.
3. Make the repository public.
4. Immediately activate branch rules, CodeQL, secret scanning and push protection, private
   vulnerability reporting, and Scorecard.
5. Open one test issue and one pull request from a fork to verify the contributor path.

### Phase C: first release

Estimated effort: half a day after the PyPI organization, package identity, and Vora release
owners are settled.

1. Finalize package metadata, changelog, and release policy.
2. Configure the protected PyPI environment and Trusted Publisher.
3. Publish to TestPyPI and verify installation in a clean environment.
4. Create an approved GitHub release and publish the same attested artifacts to PyPI.
5. Verify package links, hashes, install instructions, and the documented rollback or yank path.

## Public-launch definition of done

- [ ] README gets a new user from clone to first synthetic capture.
- [ ] Code of conduct, support policy, governance, maintainers, issue forms, PR template, and
      CODEOWNERS are committed.
- [ ] Security reports have a working private path and two notified maintainers.
- [ ] Vora's legal owner, contribution policy, release authority, and naming decision are
      recorded.
- [ ] Actions are pinned by full SHA and restricted by policy.
- [ ] CI and release audit pass from a clean checkout.
- [ ] Full history, branches, logs, artifacts, and metadata pass the private publication audit.
- [ ] `main` rules prevent force pushes and deletion and require the intended checks and reviews.
- [ ] Dependabot, CodeQL, secret scanning, push protection, private vulnerability reporting, and
      Scorecard are enabled and tested.
- [ ] Topics, merge policy, branch deletion, Discussions, and labels match the documented support
      model.
- [ ] A fork-based pull request proves that templates, permissions, checks, and reviews work.
- [ ] The first release remains blocked until the release checklist is separately complete.

## Primary references

- [GitHub community profiles](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories)
- [GitHub default community health files](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file)
- [GitHub rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)
- [GitHub secure use of Actions](https://docs.github.com/en/actions/reference/security/secure-use)
- [GitHub CODEOWNERS](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners)
- [GitHub Dependabot configuration](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference)
- [GitHub CodeQL default setup](https://docs.github.com/en/code-security/code-scanning/enabling-code-scanning/configuring-default-setup-for-code-scanning)
- [GitHub secret scanning](https://docs.github.com/en/code-security/secret-scanning/introduction/about-secret-scanning)
- [GitHub private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository)
- [OpenSSF Scorecard checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md)
- [Python `pyproject.toml` specification](https://packaging.python.org/en/latest/specifications/pyproject-toml/)
- [PyPA GitHub Actions publishing guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)
- [PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
