# X-WALLET v7.7.4 — Attendance summary cards and Bot A isolation

## Changes
- Added five attendance summary cards (`Eat`, `Smoking`, `Pee`, `Tolied`, `Other`) to **Employee Attendance · Today**. Counts follow the selected employee filter and use Asia/Yangon day boundaries.
- Removed the management backend's attendance Bot polling task. This service no longer calls Telegram `getUpdates` for the employee check-in bot, so it cannot take polling ownership from the separate Bot A Railway service.
- Added `POST /api/integrations/attendance/events` for Bot A to push `break` and `return` events into the management database.
- The endpoint authenticates with `X-Attendance-Secret`, maps Telegram user IDs using the configured employee identity table, and updates break state/events so the existing offline timer excludes break time until the employee returns.

## Required Railway configuration
1. In the X-WALLET management Railway service, add `ATTENDANCE_INGEST_SECRET` with a long random secret (at least 32 random characters).
2. Add the exact same value to the separate Bot A Railway service.
3. Keep Bot A running in its existing service. Do not add its token to this service's polling loop; this version has no attendance polling loop.
4. In Bot A's existing `/break` and `/back` message handlers, call the push endpoint after validating the command and employee Telegram ID.

## Bot A push integration example (Python)
Add this helper to the existing Bot A service. Call it from the existing command handlers; do not start a second polling loop and do not change Bot A's current polling/webhook configuration.

```python
import os
import requests

XWALLET_ATTENDANCE_URL = os.environ["XWALLET_ATTENDANCE_URL"].rstrip("/") + "/api/integrations/attendance/events"
XWALLET_ATTENDANCE_SECRET = os.environ["ATTENDANCE_INGEST_SECRET"]

def push_xwallet_attendance(action, telegram_user_id, group_chat_id, break_type="Other"):
    response = requests.post(
        XWALLET_ATTENDANCE_URL,
        headers={"X-Attendance-Secret": XWALLET_ATTENDANCE_SECRET},
        json={
            "action": action,  # "break" or "return"
            "telegram_user_id": str(telegram_user_id),
            "group_chat_id": str(group_chat_id),
            "break_type": break_type,
        },
        timeout=5,
    )
    response.raise_for_status()
    return response.json()
```

Example calls inside Bot A's already-existing handlers:

```python
# When Bot A receives /break or /eat:
push_xwallet_attendance("break", message.from_user.id, message.chat.id, "Eat")

# When Bot A receives /back:
push_xwallet_attendance("return", message.from_user.id, message.chat.id)
```

Set `XWALLET_ATTENDANCE_URL` to the public base URL of the X-WALLET management Railway service. The Bot A service must already have `requests` installed (or add it to its own requirements).

## Important
- The management service does not receive Bot A updates directly from Telegram. It receives only authenticated event POSTs from Bot A.
- Do not configure the same Bot A token for polling in another service. Bot A remains the sole owner of its existing polling/webhook connection.
- The Bot A service must call the helper only after it has parsed a valid employee command. Configure each employee's Telegram User ID in X-WALLET so the backend can map events to accounts.
