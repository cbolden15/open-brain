# Saved Markdown intake

The optional saved-Markdown adapter accepts one caller-bound absolute root. It
reads stable regular `.md` files only, excluding dot directories, temporary or
sync-conflict names, symlinks, and paths escaping the selected root. It returns
local refusal codes for malformed frontmatter, secret-bearing material,
unsupported formats, unstable reads, and all configured bounds. Refusals never
become capture intakes.

Continuous normalization is versioned as `saved-markdown-continuous.v1`. It
removes owner frontmatter and `Why Saved` sections outside fenced code, while
the historical migration format remains outside this adapter. Delivery identity
binds the caller-supplied destination and accepted source identities, relative
item identity, original and transformed digests, normalization version, and
privacy-policy version.

The collector stages the exact normalized intake before calling the injected
public-job boundary. A queued custody receipt is pending, not completed: the
active run and staged intake remain until a terminal receipt is returned. The
adapter requires a public-tier privacy decision with cloud and external egress
both disabled. It does not publish, reconcile historical receipts, seed
checkpoints, or withdraw disappeared files; those remain Phase 3 and private
binding work.
