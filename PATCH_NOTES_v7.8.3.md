# X-WALLET CHAT SERVER v7.8.3 — Hobby concurrency and Telegram account sync hardening

## Changes
- Default PostgreSQL pool reduced to 5 base + 5 overflow connections for a conservative Railway Hobby footprint; all values remain configurable with Railway Variables.
- Added composite indexes for inbox latest-message lookups, employee assignments, presence, and notifications.
- Telegram session restoration now runs after HTTP startup, with bounded concurrency (3) and transient retries. Health checks no longer wait for every Telegram session to reconnect.
- On startup, every restored connected Telegram account is queued for dialog/customer indexing. Newly connected Telegram accounts are queued automatically too.
- Account sync jobs are deduplicated and limited to 2 simultaneous account syncs. Full history sync is queued behind the dialog/customer index step.
- Telegram account profile requests use a per-account lock and a bounded global limit of 3; the admin UI retries transient restore/network errors rather than leaving a permanent Loading state.
- Dialog/customer metadata is indexed before avatar downloads. Avatar downloads run in a separate bounded background path; inbound message persistence no longer waits for profile-photo downloads.
- Per-account outbound Telegram sends are limited to 3 simultaneous operations; inbound event processing is limited to 6 simultaneous events per Telegram account.
- Inbox refresh and assignment-notification polling are coalesced in the browser to prevent overlapping requests from piling up.
- Text, media, forwarding, lazy media recovery, transcription, Telegram account connect/disconnect/delete, and alert-test routes release PostgreSQL connections before long external network operations wherever applicable.
- Added sync status endpoint: `/api/admin/telegram/accounts/{account_id}/sync-status`.
- Admin diagnostics now reports restored Telegram clients and current SQLAlchemy pool usage.

## Preserved
- Existing PostgreSQL data, encrypted Telegram sessions, user accounts, roles, permissions, team assignments, customer assignments, message history, attendance, tags, and audit records are retained.
- Existing frontend design and workflows remain in place.
- No Railway credentials, database URL, Telegram API keys, bot tokens, or encryption keys are changed by this package.

## Important deployment constraints
- Keep one Railway web replica / one Uvicorn process for the current in-process Telethon client architecture. Docker now explicitly starts one Uvicorn worker. Do not scale multiple web replicas until Telegram clients and background queues are moved into a dedicated single-owner worker service; multiple replicas can contend for the same Telegram sessions.
- In Railway Variables, set `DB_POOL_SIZE=5`, `DB_MAX_OVERFLOW=5`, `DB_POOL_TIMEOUT=15`, and `DB_POOL_RECYCLE=1200` for the Hobby database footprint. If these variables already exist with older values, update them; `.env.example` does not automatically change Railway Variables.
- Keep the existing PostgreSQL service and Railway Volume. Do not delete/recreate either.
- These changes reduce known application-side contention, but do not constitute proof of 50-user capacity. Run the 50-user acceptance test below on the actual Hobby resources before opening production access to all staff.

## Acceptance test
1. Confirm `/api/health/ready` returns 200 and admin diagnostics shows DB connected.
2. Confirm `telegram_accounts_connected` equals the number of intended active Telegram accounts and `telegram_clients_restored` reaches the same number.
3. Confirm every connected account card loads name, username, Telegram ID and avatar where Telegram has a profile photo.
4. Confirm all connected accounts reach sync status `completed` for dialog/customer indexing; verify customer counts against Telegram dialogs.
5. Use 50 separate employee accounts and independent browsers to log in concurrently.
6. Have all 50 open inboxes, then send/receive test messages concurrently across assigned Telegram accounts.
7. Monitor Railway CPU, memory, PostgreSQL connections, response latency, and 5xx logs for at least 30 minutes, then run a 24-hour soak test.
8. Do not declare 50-user production-ready if there are any QueuePool timeouts, HTTP 500/502/503/504 errors, duplicate messages, missed incoming messages, or account-session disconnects.
