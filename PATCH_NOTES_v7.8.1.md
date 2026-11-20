# X-WALLET CHAT SERVER v7.8.1 — Database connection stability hotfix

## Fixed
- Telegram dialog indexing, message-history sync, incoming-message handling, and startup account restoration no longer keep SQLAlchemy sessions open while waiting for Telegram network operations.
- Employee offline-alert delivery no longer holds a database connection while making Telegram Bot API HTTP requests.
- Inbox loading now batch-loads latest messages, Business Labels, and unread counts instead of issuing multiple per-customer queries.
- Admin customer listing now batch-loads active assignments and employee names instead of querying separately for every customer.
- PostgreSQL engine now uses pre-ping, connection recycling, LIFO pool reuse, and configurable bounded pool settings (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`, `DB_POOL_RECYCLE`).

## Preserved
- No database schema migration or destructive data operation was added.
- Telegram sessions, customer records, message history, avatars/media, assignments, tags, attendance events, and audit history are preserved.
- Existing UI, account management, staff permissions, Telegram binding, employee KPI cards, attendance, notifications, and login behavior are unchanged.

## Deployment
- Deploy this package to the existing `xwalletchatserver` Railway service.
- Keep the existing PostgreSQL `DATABASE_URL`, Railway Volume, `JWT_SECRET`, `SESSION_ENCRYPTION_KEY`, Telegram API credentials, and Bot A shared secret unchanged.
- Do not deploy this package to the separate Bot A worker service.
- After deployment, verify `/api/health/ready`, `/api/inbox?filter=all`, and `/api/admin/customers?account_id=<connected-account-id>`.


## v7.8.1 follow-up finding from production logs
- The Telegram account profile endpoint previously retained its request-scoped SQLAlchemy session while awaiting Telegram `get_me()` and profile-photo download calls.
- Under concurrent traffic this could keep a pool connection checked out during network waits and contribute to `QueuePool limit ... reached` errors.
- The profile endpoint now snapshots the account label in a short-lived session, closes it before Telegram I/O, and opens a new short-lived session only if the label must be updated.
