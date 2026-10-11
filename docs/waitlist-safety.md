# Waitlist safety and rollout

The queue is scoped to an owner, restaurant, and branch. An absent branch represents the unbranched location, never all branches. Restaurant settings remain shared across branches.

## Behavior

- Concurrent joins serialize in a database transaction. Active duplicate phone numbers (including local / +91 variants) are rejected within the same location. Queue positions are unique through application serialization.
- Notify Next selects a fitting vacant table and reserves it for exactly one notified party. A database unique index protects the reservation. Normal session creation and both QR session paths respect the reservation. No fitting table returns HTTP 409 and leaves the party waiting; an empty queue returns HTTP 404.
- Seating requires a table in the same restaurant and branch, sufficient capacity, and no conflicting session or reservation. Waitlist state, session creation, table occupancy, audit, and queue renumbering commit together. Failures roll everything back. Seating retries return the original session and never reoccupy a freed table or join a different party's session. Expired/terminal entries cannot be seated.
- Cancel, skip and expiry release reservations, renumber the queue, invalidate restaurant waitlist caches, and publish `waitlist.updated`. Reordering publishes the same event with `action: reordered` and `ordered_ids`.
- Reordering requires every active entry in that location exactly once. Duplicates return HTTP 422; incomplete/stale/foreign sets return HTTP 409 with no partial write.
- Add, cancel and expire-check require `waitlist.manage`. Queue reads use `waitlist.read`; reorder and settings retain `waitlist.admin`.
- Public join/status responses use an allowlist. They exclude phone, notes, owner/restaurant/branch identifiers, internal session identifiers, and session tokens. Status URLs still use the unguessable entry UUID; they are not authenticated customer accounts. Session credentials are also excluded from waitlist broadcasts.
- Public displays honor `display_screen_enabled` and count the whole location queue, rather than only the first 20 rows.

## Automatic operation

The application lifespan starts a maintenance worker and cancels it at shutdown. It expires overdue notifications even when auto-notify is off. With auto-notify enabled (including the default when no settings row exists), it allocates available tables to fitting waiting parties.

Table session-ended/status events and queue changes wake the worker immediately; a 30-second sweep covers missed events. Database locks and reservation uniqueness make concurrent workers safe. Realtime updates publish after commit and are best-effort. Browser push is queued transactionally in PostgreSQL and retried by the worker; browser polling remains a fallback. SMS/WhatsApp settings cannot be enabled because these providers are not integrated.

## Migration and clients

Apply `migrations/087_waitlist_safety.sql` and then `migrations/088_waitlist_delivery_and_sessions.sql` before deploying the application. It adds the original seated-session reference, reservation uniqueness, queue/expiry indexes, and table branch storage where absent. Existing duplicate table notifications retain the first assignment and return later parties to waiting; customer history is not deleted. Unassigned legacy tables and QR entries are backfilled only to an explicit main or sole active branch. Already assigned entries inherit their table's branch.

Before rollout, review legacy active entries/tables with `branch_id IS NULL` in multi-branch restaurants without an explicit main branch. Their location cannot be inferred safely; assign the correct branch explicitly. Migration and application changes have only been exercised against isolated PostgreSQL, not a live database.

Frontend changes:

1. Generate branch QR images with `GET /api/v1/waitlist/qr/{restaurant_id}.png?branch_id={branch_id}`. Both `/q/{restaurant_id}` and the long QR landing URL preserve this parameter. Restaurant-only links resolve to an explicit main/sole active branch; ambiguous locations return HTTP 422.
2. Custom public join clients send optional `branch_id` in the JSON body. Public displays accept the same query parameter. The built-in QR page sends and stores state per branch.
3. Seat requests must supply `table_id` or use the entry's notified assignment. Show HTTP 409 conflicts instead of treating them as successful seating. `guest_count` cannot be smaller than the waiting party or exceed table capacity.
4. Send the complete current active queue to reorder; refresh after HTTP 409. Listen for `waitlist.updated` and refetch positions after any queue change.
5. Enable `display_screen_enabled` explicitly for public display boards. Public responses no longer include staff-only fields.

## Integration fixes (review findings 1–9)

1. **Realtime access:** restaurant channels are restricted to the current restaurant's owner; branch channels require the exact authenticated branch. Arbitrary entity channels are denied. Waitlist events additionally require `waitlist.read`. Events omit phone, notes, session credentials and push credentials; authorized screens fetch full details through the staff API.
2. **One table visit:** waitlist seating creates legacy and canonical dine-in records atomically, sharing the ID and token and recording `legacy_session_id`. Migration 088 links old matching visits and creates missing canonical records for active waitlist seats. Ending either linked visit closes the other, using the token and table so a later party is unaffected. The legacy End Session operation delegates linked visits to the canonical unpaid-balance check. Table lists expose the canonical session immediately.
3. **Production QR:** request signing exemptions match only the public route/method combinations. Staff operations remain signed. QR inline code and styles use per-response CSP nonces; pages are not cached. Public QR origins come from `PUBLIC_API_BASE_URL`, not forwarded request headers.
4. **Table scope:** list, mutations, session lookup, join/start and staff session operations validate the current restaurant and branch. Live table lists bypass occupancy caching and expose `is_reserved` / `status: reserved`. Reserved or occupied tables cannot be edited/deleted until released. Session inserts inherit the table branch through database triggers; QR lookups reject mismatched restaurant/table pairs.
5. **Browser push navigation:** short pages use service-worker scope `/q/`; long pages use `/api/v1/waitlist/qr/`. Activation has a bounded wait. Notification clicks open the restaurant/branch page with the exact entry ID, including after the original tab is closed.
6. **Durable delivery:** transition to `notified` atomically creates `waitlist_push_jobs`. Workers claim at most four jobs with `SKIP LOCKED`, use a 90-second recoverable lease and five attempts with backoff. Provider calls use a 10-second timeout and reject redirects. Late subscriptions requeue failed/pending jobs. Seated, cancelled, skipped and expired entries stop pending delivery; requests already accepted by a provider cannot be recalled. Delivery is at least once: a crash after provider acceptance can retry, and the stable notification tag replaces the prior browser notification. `status: notified` means the table is reserved; `delivery_status` separately reports `pending`, `sending`, `delivered`, `failed` or `cancelled`. A delivered state means provider acceptance, not proof the customer read it.
7. **Public input:** push endpoints are restricted to HTTPS FCM, Mozilla and Apple push hosts; keys must be a valid P-256 point and 16-byte auth secret. At most three browsers may subscribe per entry. Public join returns a one-time `push_token`; subscription requests must send it. The server stores only its hash and never returns it through status/staff/realtime reads. Existing entries without a token continue polling, but cannot enroll a new push subscription. Restaurant/table names render as text, including quotes and HTML-looking values.
8. **History and estimates:** history accepts ISO dates/datetimes; date-only end bounds include the whole local day. Statistics use `WAITLIST_TIMEZONE` (default `Asia/Kolkata`; one deployment timezone because the schema has no restaurant timezone field). ETAs recompute on queue/settings mutations and each worker sweep using fitting table capacities, occupancy, reservations and queue order. Zero means a fitting table is currently available; null means no fitting configured table. These are turnover estimates, not guaranteed seating times.
9. **Channel capabilities:** settings return `delivery_capabilities` with browser push available and SMS/WhatsApp unavailable. Enabling either unsupported channel returns HTTP 422. Migration 088 resets previously enabled unsupported flags and prevents them being re-enabled directly in the database.

## Client compatibility

- Store `push_token` from the public join response with the entry ID. Include it in `POST /api/v1/waitlist/push/subscribe/{entry_id}`. The built-in QR page handles this automatically. Never put that token in shareable URLs.
- Staff clients should use their branch channel; owners may use their current restaurant channel. Refetch authorized waitlist details on an event. Generic entity-channel subscriptions are no longer accepted.
- Treat `reserved` tables as unavailable. Render zero-minute ETA as zero and null as unknown/unavailable. Distinguish a reserved table from successful browser delivery.
- Disable SMS/WhatsApp controls using `delivery_capabilities`. Configure `PUBLIC_API_BASE_URL` to the actual public HTTPS API origin before printing QR codes.
- Deploy migrations and application together. No live database migration was run by this change. Review ambiguous old branch assignments before rollout as described above.

## Validation

`python -m pytest tests/test_waitlist_safety.py tests/test_waitlist_integration.py -q`

Tests use isolated PostgreSQL and mocked Redis/provider HTTP. Coverage includes concurrency, rollback, scopes and permissions, production signing/CSP, atomic session linkage, real payment/vacate service execution, push proof/limits, encrypted Web Push payload construction, retry/lease recovery, terminal cancellation, local dates and ETA changes. Both migrations are reapplied in tests to check idempotence.

Headless Chrome also verified both short and long QR pages under the production CSP: join, safe text rendering, service-worker activation and push-subscription submission. Browser/provider responses were local mocks; no real customer notification was sent.

The full-suite check still has five pre-existing failures in `tests/test_checkout_idempotency.py` (four unmocked database-pool accesses and one date-string expectation), previously reproduced against committed HEAD `28f9c7c`. Unrelated legacy `app/domains/*` files also contain pre-existing non-UTF-8 source bytes and fail a repository-wide compile check; changed waitlist files compile successfully.

Final targeted validation: **64 tests passed**. The latest full-suite run (before the final added unpaid-balance regression test) reported **251 passed, 5 pre-existing failures**.
