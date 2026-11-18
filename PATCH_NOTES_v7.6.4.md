# X-WALLET v7.6.4 — Telegram sync, admin layout and privacy repair

## Changes based on the supplied v7.6.2.2 backup

1. **Telegram account avatar and profile**
   - The admin profile refresh now ensures the upload directory exists, refreshes the account's real Telegram display name, and preserves the last known avatar if Telegram temporarily fails to return a new photo.
   - Avatar requests use no-cache refreshes in the admin UI so the browser does not keep showing the Telegram placeholder after a successful download.

2. **Main chats, Archived Chats and Saved Messages**
   - Dialog indexing now explicitly reads Telegram folder 0 (main chats) and folder 1 (Archived Chats), preserving the folder ID in PostgreSQL.
   - Saved Messages is explicitly indexed using the connected account's own Telegram identity, including its profile photo and available message history.
   - The admin customer list labels archived chats and Saved Messages distinctly.
   - Saved Messages is marked administrator-private, is excluded from employee inboxes, cannot be assigned to employees, and is blocked by the employee customer-access check even if an old assignment exists.
   - Existing customer assignments, messages and tags are not deleted by the additive migration.

3. **Admin layout**
   - Telegram connected-account cards now appear above the Telegram customer list.
   - Team list is embedded in the Create Team card; the separate Team Management card was removed.
   - The account 2FA information card was moved below the Social Channels workspace. The existing top 2FA shortcut remains available.

4. **Database migration**
   - Added `customers.dialog_folder` and `customers.is_saved_messages` with additive startup migrations for SQLite and PostgreSQL.
   - No database reset or table recreation is required.

## Verification performed

- Python source compilation: passed (`python -m compileall`).
- Inline JavaScript syntax check: passed (`node --check`).
- ZIP archive integrity: checked after packaging.
- Live Telegram account verification was not possible in this offline build environment. Telegram only returns profile fields and media that the connected account is authorized and permitted to access.

## Deployment caution

Deploy the complete project folder. Keep the existing Railway `DATABASE_URL`, `SESSION_ENCRYPTION_KEY`, Telegram API ID/hash, Telegram account sessions, and persistent volume unchanged. Do not delete or recreate PostgreSQL. After deployment, use the admin panel's **Sync all chats** action and allow the background history import to finish.


## v7.6.4 follow-up repair from Railway logs

- Railway deploy logs explicitly reported `OSError(28, 'No space left on device')` while saving the connected Telegram account avatar.
- Connected-account avatar refresh now downloads the Telegram profile photo into an in-memory cache and serves it without requiring a filesystem write. Existing disk-cached avatars remain a fallback.
- Admin top-right controls are aligned on one center line; icon controls center their glyphs. Admin account/user action buttons use the same outlined treatment as the header controls instead of solid blue backgrounds.
- This avoids the specific disk-write failure for connected-account profile photos, but does not create more Railway storage. Other uploads/media/history files can still fail while the volume is full; free space or increase the Railway volume.
- No live Railway/Telegram credentials were available here. Social provider APIs are not activated by embedding websites: WhatsApp, Instagram, TikTok and LINE require the relevant official developer apps, OAuth credentials, webhook URLs and provider approval. This archive does not claim those external APIs are connected.
- Fine-grained admin permissions were not added in this patch. They require backend authorization checks for each protected route, not only hiding buttons in the browser; apply a defined permission matrix before enabling delegated admins.
