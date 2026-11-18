# Portable Brain 19

Portable 19 pairs local schema 24 with runtime session 16. It adds one required file to the Portable 18 evidence families, `history/saved-item-sources/sources-v1.json`. Its `contract` is `open-brain-saved-item-sources.v1`. Each entry in `sources` holds the connector name `saved_item`, the opaque `connection_id` and the opaque `resource_id` for a selected folder. An empty registry travels as an empty list. Distinct sources retain distinct identities even when they contain the same URL.

The source tuple lets an authorized installation derive a delivery ID from a page’s normalized URL. Paths, folder permissions, remote endpoints and browser grants stay local to the installation. After restore, the owner must select a source and grant access again before a browser client can read or change its records.

A Portable 18 archive still restores its saved captures and delivery IDs. Identifying a legacy capture by its URL requires the original source binding. Without it, the restored capture remains available through ordinary search. Recreating a folder at the same path does not reconstruct that binding.

See [saved items](integrations/saved-items.md) for URL identity rules and [Portable 18](portable-brain-v18.md) for the inherited evidence.
