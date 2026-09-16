# iMessage Source Adapter

The D4 iMessage adapter is local-only and opt-in. It requires an explicit SQLite database path, an explicit conversation identifier, and an owner permission status before any preview or import can run. It refuses the default `~/Library/Messages/chat.db` path in this worker implementation because real native access still needs a separately recorded selected-source consent and macOS permission gate.

The adapter reads a bounded page from a supported Messages-like SQLite schema using read-only SQLite mode. Preview output contains metadata only: delivery IDs, source references, titles, and content type. Message text is submitted only during import through the connector capture sink. Attachments are excluded; the adapter may notice attachment presence through message metadata, but it does not read attachment tables or payload files.

Supported synthetic schema for this increment:

- `chat(ROWID, guid, display_name)`
- `message(ROWID, guid, text, date, date_edited, date_deleted, cache_has_attachments)`
- `chat_message_join(chat_id, message_id)`

Revision identity is derived from the selected message row identity and mutable message fields, including edit and deletion markers. Replaying the same revision is empty; an edit or deletion keeps the stable delivery identity and commits a new revision identity after capture acknowledgement. Deleted messages are imported as deletion markers without preserving the prior message text.

This synthetic fixture-backed adapter does not prove the current macOS Messages schema, Full Disk Access/TCC behavior, or owner-selected test-conversation experience. Integration must add shared source registration separately and keep the real native gate open until a supported Mac lane verifies selected conversation access, denied or missing permission behavior, edits, deletions, and attachment exclusion against an explicitly designated test conversation.
