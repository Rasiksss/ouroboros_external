## Decisions

- Preserve exact ID/path precedence in the existing catalog query; the artifact consumer supplies its fallback path.
- Keep temporary-file lifetime outside download admission, as in attachment extraction. Upload admission encloses catalog reservation, storage and completion; a capacity refusal creates no catalog record.
- Keep one upload queue per chat controller, including subsequent selections and manual retries. Shared admission remains in PostgreSQL.
- Retry only explicit owner/process capacity refusals, up to three retries, respecting Retry-After. Network failures remain visible for manual retry because publication may already have happened.
- Leaving a chat or removing an entry skips its queued work; a late successful upload is released in its original scope.

## Verification

PostgreSQL-backed contention and artifact resolution tests; UI tests of batches, retries, failures and navigation; a real browser against the running BFF/backend; independent diff review.
