# Secure Node historical archive

This directory preserves the first appliance-style Open Brain implementation. It includes the
HTTP server, daemon, scheduler, supervisor integration, lifecycle management, Phase 1 adapters,
Brain Protocol code, ledger code, Secure Node portability mapping, tests, and synthetic evidence.
Historical Secure Node-specific design and CLI characterization documents are kept under `docs/`.

It is not a workspace member, build target, installable extra, or supported runtime. Nothing in
`packages/app` or `packages/engine` imports it. The preserved source keeps its original module names
so commits before and after the move remain easy to compare with `git log --follow`.

Future Secure Node work should begin as a separate `open-brain-secure-node` repository or package
with its own namespace, dependency graph, entry points, tests, releases, and operating contract.
Do not restore these files to the base Open Brain distributions.
