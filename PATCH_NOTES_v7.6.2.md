# X-WALLET v7.6.2 repair notes

## Fixes included

1. **2FA HTTP 500 / PostgreSQL storage width**
   - `users.totp_secret` is now `VARCHAR(255)` in the SQLAlchemy model.
   - Startup migration widens existing PostgreSQL `users.totp_secret` columns to `VARCHAR(255)`.
   - The old `VARCHAR(64)` could not store the approximately 140-character Fernet ciphertext generated for a TOTP secret.
   - Setup endpoints now return a clear HTTP 503 diagnostic if encryption/storage fails, rather than an opaque HTTP 500.
   - Existing `SESSION_ENCRYPTION_KEY` must remain unchanged; it also protects Telegram StringSession data.

2. **Preserve the earlier PostgreSQL presence migration**
   - Adds `presence_sessions.channel` when an older database is missing it.
   - Keeps the default channel as `Telegram` for existing rows.

3. **Telegram sync reliability and feedback**
   - Telegram client restoration is awaited during application startup, avoiding a race where the UI can press Sync before restored clients are available.
   - Sync buttons display a disabled `Syncing…` state while the API request runs and are restored after completion.
   - API errors continue to appear in the application modal.

4. **Employee UI / JavaScript**
   - Guards the optional `select-all-customers` checkbox before setting `.checked`, preventing the reported null-element exception.
   - Staff filtering exits safely if its monitor elements are not present.
   - Employee role labels are now `Chatting` and `Finding` (no `Employee` suffix).

5. **Theme and notification controls**
   - Theme/language/2FA top controls use the same pill treatment as the User ID badge.
   - Light theme forces readable dark text for sidebar buttons, headings, labels, tables, cards, and inputs.
   - Bell and sound icons use `currentColor` and switch between white (dark theme) and black (light theme).
   - The unread count is actually hidden when empty; previously `.notification-count` overrode the generic `.hidden` rule.
   - Language switching skips buttons containing SVG icons so it does not erase the bell/speaker SVG markup.

6. **Social Channels**
   - Removed Telegram from the Social Channels shortcut tabs; Telegram account management and Telegram chat sync remain in the admin area.
   - Added the existing local SVG logos for WhatsApp, Instagram, TikTok, and LINE.
   - The Social Channels panel no longer pretends the platform websites can be embedded. It displays an in-app API connection status instead. Real conversations require each platform's official developer app, OAuth scopes, webhook/API credentials and server integration. No desktop app or external browser is launched.

## Checks performed

- Python source compilation: passed (`compileall`).
- Inline JavaScript syntax check: passed (`node --check`).
- Fernet payload length check: 140 characters; new database field capacity 255.
- ZIP integrity: checked after packaging.

## Deployment notes

- Deploy the entire `xwallet-chat-management` folder to the existing GitHub repository used by Railway.
- Keep the existing Railway `DATABASE_URL`, `JWT_SECRET`, `SESSION_ENCRYPTION_KEY`, Telegram API credentials and volume settings unchanged.
- Do not delete or recreate PostgreSQL. The migration is additive and runs at app startup.
- This package has not been deployed against the user's live Railway project; live PostgreSQL and Telegram API verification must be done after deployment.
