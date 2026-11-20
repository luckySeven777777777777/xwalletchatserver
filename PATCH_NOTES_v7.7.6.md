# X-WALLET v7.7.6 — Telegram startup sync and graceful shutdown hotfix

## Why this hotfix exists
The v7.7.5 source archive still automatically launched `sync_dialogs()` for every connected personal Telegram account during application startup (`restore_clients`) and immediately after a new account was linked (`finish_login`). That sync enumerated every dialog and downloaded profile photos. On large accounts this can create a burst of Telegram `GetFileRequest` operations. The history sync path can also download every historical attachment when explicitly queued. During Railway restarts, Telethon background tasks could be cancelled while its network receive/send loops were still active.

## Changes
- Startup now restores authorized Telegram personal-account connections and incoming-message handlers only. It does not start bulk dialog/avatar/history downloads automatically.
- Linking a new personal Telegram account no longer triggers an automatic bulk sync.
- Full dialog/history synchronization remains available through the administrator's existing explicit **Sync selected account** action.
- Added graceful Telethon client disconnection during FastAPI shutdown to reduce abandoned `Connection._recv_loop` / `Connection._send_loop` tasks.
- No Bot A token, Bot A polling, attendance integration endpoint, attendance secret, attendance database tables, JWT, CORS, or login-throttling behavior was changed.

## Deployment
- Deploy this package only to the `xwalletchatserver` management service.
- Do not deploy it to `nexbitsafeworkbot` (Bot A).
- Keep all existing Railway Variables and PostgreSQL/Volume attachments unchanged.
- After deployment, verify the admin dashboard, incoming Telegram text messages, outgoing replies, and attendance records. If new dialogs need indexing, use **Sync selected account** manually; do not repeatedly click it while a sync is running.

## Expected behavior
Routine Railway restarts should no longer launch a bulk Telegram profile-photo sync. Existing indexed customers and message history remain in the database. Telegram user-account connections are restored and their incoming-message handlers are attached. Manual sync may still encounter Telegram network timeouts for inaccessible or large media; those failures should not prevent the web API from starting.
