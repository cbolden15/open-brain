# Modified by Open Brain contributors for the NW0 bounded Markdown packaging proof.
"""Shared directory exclusions without discovery/runtime imports."""

_SKIP_DIRS = {
    "venv", ".venv",  # "env"/".env"/"*_env" are gated on venv markers below (#2058)
    "node_modules", "__pycache__", ".git",
    "dist", "build", "target", "out",
    "site-packages", "lib64",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".nox", ".eggs", "*.egg-info",  # nox is tox's successor, same .nox/ venv shape (#1804)
    "graphify-out",  # never treat the default output as source input (#524)
    # Coverage/test-artefact dirs — generated, never architecturally meaningful
    "lcov-report",                          # Vitest/Istanbul/nyc HTML reports (#870);
                                            # bare "coverage" is gated on report
                                            # artefacts below (#2339)
    "visual-tests", "visual-test",          # Playwright/visual-regression bundles (#869)
    "__snapshots__",                        # Jest/Vitest snapshot dir (unambiguous)
    "storybook-static",                     # Storybook production build output
    "dist-protected",                       # Protected dist variants (same noise as dist)
    # Framework cache/build dirs — generated, never architecturally meaningful (#873)
    ".next", ".nuxt", ".turbo", ".angular",
    ".idea", ".cache", ".parcel-cache", ".svelte-kit", ".terraform", ".serverless",
    ".graphify",  # graphify's own extraction cache — never index self-generated data
    ".obsidian", ".smart-env",  # Obsidian vault metadata and plugin caches (#2493)
    ".worktrees",  # git worktree convention (#947) — sibling checkouts, always redundant
}
