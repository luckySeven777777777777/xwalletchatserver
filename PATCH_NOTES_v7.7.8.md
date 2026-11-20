# X-WALLET CHAT SERVER v7.7.8

## Added
- Super Admin-only `取消连接` action beside `Refresh profile` for each Telegram account.
- New `POST /api/admin/telegram/accounts/{account_id}/disconnect` endpoint.
- Disconnect stops only the selected in-memory Telethon client and marks the account `disconnected`.
- The encrypted Telegram StringSession in the database is retained. Customer rows, message history, assignments, labels, and media files are not deleted.
- An audit event is recorded for each disconnect.

## Storage findings from v7.7.7 source
- Telegram StringSession is encrypted and stored in `telegram_accounts.session_ciphertext` in the configured database; it is not stored as a session file under `/data`.
- `/data` is used by the configured upload directory (`/data/uploads`) for downloaded Telegram media and related uploaded files.
- The Railway Volume screenshot confirms the `xwallet-storage` mount at `/data`, which supports media persistence. It does not by itself prove database/session persistence.
- Production config requires `DATABASE_URL`; confirm it points to the intended Railway Postgres service.

## Safety
- No account deletion endpoint is added in this release.
- No Telegram Session is cleared or re-encrypted.
- No changes to Bot A, attendance ingest, `ATTENDANCE_INGEST_SECRET`, offline notifications, or employee access rules.
- Deploy to `xwalletchatserver` only. Keep the existing Postgres connection, Volume mount `/data`, `SESSION_ENCRYPTION_KEY`, and other Railway Variables unchanged.
