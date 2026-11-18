# Portable Brain 22

Portable 22 pairs local schema 27 with runtime session 16. It adds the required file `history/browser-revisits/revisits-v1.json` to the [Portable 21](portable-brain-v21.md) evidence. The file uses `open-brain-browser-revisits.v1` and contains a `revisits` array. An empty Brain still exports the file.

Each row records an ordered set, dismiss or clear operation bound to a saved page and its workflow. A set stores the local calendar date and IANA time zone. Restore preserves both values and checks the parent capture, source, action and operation binding before it stages the destination. A syntactically valid zone unavailable on the current system remains visible for owner review and stays out of due results until that zone can be resolved.

Retraction clears a revisit’s date and zone while preserving its opaque replay evidence. A restored Brain requires fresh authorization and current parent visibility for revisit operations. Installation grants, browser configuration and local projections stay local.

Portable 21 and earlier archives open with an empty revisit store. Earlier readers refuse a Portable 22 snapshot before writing the destination.
