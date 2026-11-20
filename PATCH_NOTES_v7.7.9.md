# X-WALLET v7.7.9 — Offline alert diagnostics and test

## Changes
- Added a Super Admin **Send test notification** action to the Telegram employee offline-alert settings.
- Added `POST /api/admin/alerts/offline-test`, which sends a one-off test message to the configured recipients and returns each recipient's Telegram HTTP status and API description.
- Improved background offline-alert logs to include the HTTP status and Telegram error description, while masking recipient Chat IDs.
- Separates network failures from Telegram API rejections and unexpected local errors.
- Never logs the bot token or encrypted session values.
- Preserves the existing 15-minute threshold, 20:30–09:30 Asia/Yangon monitoring window, retry behavior, attendance integration, database records, Telegram accounts, and chat history.

## Important
This update exposes the exact reason Telegram rejected the notification. It cannot automatically correct an invalid Bot Token, incorrect Chat ID, blocked bot, or missing group permission. After deployment, use **Send test notification** in Super Admin → Telegram Employee Offline Alerts. The result identifies which recipient failed and the Telegram-provided reason.
