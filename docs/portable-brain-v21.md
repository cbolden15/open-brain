# Portable Brain 21

Portable 21 pairs local schema 26 with runtime session 16. It adds the required file `history/browser-annotations/annotations-v1.json` to the [Portable 20](portable-brain-v20.md) evidence. The file uses the `open-brain-browser-annotations.v1` contract and contains an `annotations` array. An empty Brain still exports the file.

Each annotation records its parent capture and saved-item source, stable workflow principal, mutation identifiers and request digest. Several distinct annotations can share a parent, including repeated selections kept under separate annotation IDs. A retained annotation includes its exact quote, bounded context and optional note. When a parent is withdrawn, its annotations retain opaque replay evidence and clear the quote, context and note.

Restore validates parent capture, source, lifecycle and operation bindings before staging the destination. The destination rebuilds its local search indexes. Access to a restored annotation requires new local authorization and visibility of its parent. Browser grants, installation credentials, endpoints and local indexes stay local.

A Portable 20 or earlier archive opens with an empty annotation store. Earlier readers refuse a Portable 21 snapshot before writing the destination.
