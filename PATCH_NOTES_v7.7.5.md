# X-WALLET v7.7.5 — Security hardening and attendance delivery reliability

## Included changes
- Removed the public fallback JWT signing key. Railway startup now requires `JWT_SECRET` with at least 32 characters.
- Restricted CORS to exact configured origins; wildcard `*` is ignored and falls back to the APP_BASE_URL origin.
- Added security response headers and `Cache-Control: no-store` on authentication endpoints.
- Added login and 2FA failure throttling: 5 failed attempts per username/IP per 15-minute window (process-local; keep one web replica unless using a shared limiter).
- Hardened attendance ingestion: exact required group ID match, numeric Telegram user ID validation, strict event type validation, bounded timestamp window, and optional unique event IDs.
- Added durable attendance ingestion receipts so Bot A retries do not duplicate events.
- Updated Bot A's HTTP push worker to attach unique event IDs and retry temporary network, 429, and 5xx failures up to five times. It does not create another Telegram polling process.
- Updated application version to 7.7.5.
- Added tests for event validation, rate limiting, and database persistence.

## Railway Variables
1. Keep `ATTENDANCE_INGEST_SECRET` unchanged in both services for this update. It must be at least 32 characters and exactly match on both services.
2. Add `JWT_SECRET` to the X-WALLET management service if it is not already present. Use a unique random value of at least 32 characters. Do not reuse `ATTENDANCE_INGEST_SECRET`, `SESSION_ENCRYPTION_KEY`, or any Telegram token.
3. Set `CORS_ORIGINS` only if the browser frontend uses a different origin from `APP_BASE_URL`; enter exact origins separated by commas, for example `https://crm.example.com`. Do not use `*`.
4. Keep `DATABASE_URL` linked to Railway PostgreSQL. Do not replace or delete it.
5. Keep the existing `SESSION_ENCRYPTION_KEY` unchanged; changing it would make previously encrypted Telegram sessions and bot tokens unreadable.

## Deployment safety
- This package does not change Bot A's BOT_TOKEN, GROUP_CHAT_ID, Telegram polling configuration, or existing attendance data files.
- The management backend still does not call Telegram getUpdates for Bot A.
- Deploy the management service first, then deploy Bot A. The new backend accepts both old events without event_id and new idempotent events with event_id.
- Existing attendance records remain in the existing database; the new receipt table is additive.
