# Portable Brain 23

Portable 23 pairs local schema 28 with runtime session 16. It adds `history/browser-filing/witnesses-v1.json` to the evidence in [Portable 22](portable-brain-v22.md). An empty Brain exports an empty witness array.

When Chrome files a saved item, the Brain can retain the route ID, the stable workflow principal, the source identity tuple, and the opaque item and delivery keys. After restore, those fields let a newly authorized browser verify that the original source owned the filed page. This matters for folder captures whose stored URL intentionally omitted its fragment.

Validation checks the source registration, capture, retained route, operation binding and delivery key before restore. Retraction removes the filing witness. Browser tokens, machine authorization, file paths and private URL fragments remain outside this evidence.

Earlier archives retain their original format. If an older archive contains a folder-only fragment page without a verifiable item key, the owner needs the original source and URL to re-establish that proof. A schema 27 upgrade can recover a filing witness only where the original workflow registration still provides scope evidence. Schema 28 writes the witness when filing, so later revocation preserves that evidence. Browser access after any restore requires a fresh grant.
