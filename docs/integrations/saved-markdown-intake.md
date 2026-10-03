# Saved Markdown intake

The optional saved-Markdown adapter accepts one caller-bound absolute root. It
reads stable regular `.md` files only, excluding dot directories, temporary or
sync-conflict names, symlinks, and paths escaping the selected root. It returns
local refusal codes for malformed frontmatter, secret-bearing material,
unsupported formats, unstable reads, and all configured bounds. Refusals never
become capture intakes.

Continuous normalization is versioned as `saved-markdown-continuous.v2`. It
removes owner frontmatter and `Why Saved` sections outside fenced code, while
the historical migration format remains outside this adapter. Delivery identity
binds the caller-supplied destination and accepted source identities, relative
item identity, original and transformed digests, normalization version, and
privacy-policy version.

Version 2 uses the optional connector's pinned CommonMark parser and source-line
ranges to remove owner sections through the next top-level heading of equal or
lower depth. Fenced code, indented code, lists, blockquotes and thematic breaks
cannot masquerade as section boundaries. Heading labels support inline markup
and character entities; retained source lines are not rendered or rewritten.
Body inline parsing is disabled to bound work on large discarded sections.
Document reference definitions are shared with heading parsing. Image alt text,
code labels, comments and line breaks have explicit projections. Inline HTML
allows attribute-free `em`, `strong`, `b`, `i`, `span`, `code` wrappers and `br`
breaks only. Attributes, executable or unknown HTML and unsupported heading
tokens refuse intake with `saved_markdown_unsupported_heading_markup`; the
adapter does not guess CSS-dependent visible text. Ordinary fenced examples
remain source content, not heading markup.

Retained version-1 observations and immutable sharing evidence remain readable and
restorable without recomputing their transforms. New scans use version 2;
their changed normalization identity requires a new source observation and
separate approval. Historical receipts are not rewritten or silently reseeded.

The collector stages the exact normalized intake before calling the injected
public-job boundary. A queued custody receipt is pending, not completed: the
active run and staged intake remain until a terminal receipt is returned. The
adapter requires a public-tier privacy decision with cloud and external egress
both disabled. It does not publish, reconcile historical receipts, seed
checkpoints, or withdraw disappeared files; those remain Phase 3 and private
binding work.
