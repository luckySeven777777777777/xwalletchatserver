# X-WALLET CHAT SERVER v7.8.2 — Telegram profile load stability hotfix

## Fixed
- Dashboard no longer starts profile/photo requests for every connected Telegram account at once.
- Profile requests are queued in the browser with a maximum of two concurrent account lookups.
- Backend profile lookups are also limited to two concurrent Telegram operations.
- Telegram profile metadata is cached for five minutes to avoid repeating `get_me()` and photo downloads on every admin refresh.
- Profile and photo requests have explicit timeouts and return a readable error instead of leaving cards in `Loading Telegram profile...` indefinitely.
- Telegram account photos are saved to the configured upload volume when possible, so they survive a Railway process restart. An in-memory fallback remains for volume write failures.
- Manual “Refresh profile” bypasses the profile cache.

## Preserved
- No database schema migration or destructive database operation.
- No changes to employee permissions, Telegram account ownership, customer assignments, chat history, attendance, or message sending.
- Existing routes and frontend layout remain in place.

## Deployment
- Deploy this package to the existing `xwalletchatserver` Railway web service.
- Keep the existing PostgreSQL `DATABASE_URL`, Railway Volume mount, `JWT_SECRET`, `SESSION_ENCRYPTION_KEY`, Telegram API credentials, and Bot A settings unchanged.
- Do not deploy this web package to the separate Bot A worker.
- After deploy, test `/api/health/ready`, the admin Telegram account list, profile refresh on 2–3 accounts, and `/api/inbox?filter=all`.
