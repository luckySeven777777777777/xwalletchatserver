# X-WALLET v7.7.1 — Fast admin actions and Telegram employee offline alerts

## Changes
- Admin mutations no longer wait for a full dashboard/customer/profile reload before reporting success. The refresh runs in the background.
- Admin page loading no longer blocks on the Telegram customer list request; customer data refreshes independently.
- Telegram account history sync is queued and returns immediately. Dialog discovery and message-history import continue in the background.
- The Telegram employee binding action reports success as soon as the database update commits, then refreshes the account panels independently.
- Added a Super Admin-only Telegram employee offline alert settings card beside the Telegram personal-account connection card.
- Bot Token is encrypted at rest with the existing `SESSION_ENCRYPTION_KEY`. The token is never returned to the browser after saving.
- Multiple numeric Telegram Chat IDs are supported, so group IDs and private chat IDs can be configured separately or together.
- Added a background monitor that checks employee/admin presence every 60 seconds. During 20:30–09:30 Asia/Yangon, it sends one English Telegram notification after an account has been offline for at least 10 minutes.
- Notification text includes employee username, offline start time, elapsed offline duration, and alert timestamp in Myanmar Time.
- Presence heartbeat continues while the browser tab is hidden, reducing false offline detection when an employee switches tabs.

## Telegram notification setup
1. Create a Telegram bot with BotFather and copy its Bot Token.
2. Add the bot to the target group (if using a group) and allow it to send messages.
3. Obtain the numeric group Chat ID and/or private Chat ID. The private user must start the bot first.
4. In Super Admin → Admin Dashboard, enter the Bot Token and one or more Chat IDs, then save.
5. Keep `SESSION_ENCRYPTION_KEY` unchanged. It is used to encrypt the Bot Token in the database.

## Timing and limitations
- The monitor polls every 60 seconds, so a notification may arrive shortly after the 10-minute threshold, not at an exact second.
- Alerts are only sent from 20:30 through 09:29 Myanmar Time. Offline episodes already in progress at 20:30 are eligible if they have exceeded 10 minutes.
- Telegram authorization and Telegram's remote sync speed depend on Telegram/network response. The UI now returns quickly and runs history sync in the background, but external Telegram operations cannot be guaranteed to finish within five seconds.
- If a Railway deployment runs multiple replicas, each replica may run the monitor. Use one app replica unless a distributed scheduler/DB locking layer is added.

## Database
- Added `employee_alert_config` and `employee_alert_states` tables through SQLAlchemy `create_all`; existing users, Telegram accounts, sessions, customers, messages, and assignments are preserved.

## Validation
- Python syntax compilation passed for `app/main.py` and `app/models.py`.
- Frontend JavaScript passed `node --check`.
- Live Telegram Bot API delivery, Railway latency, and real employee offline transitions still require deployment testing.
