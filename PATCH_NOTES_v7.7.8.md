# X-WALLET CHAT SERVER v7.7.8

## Offline notification routing fix
- Employee offline alerts now accept and send only to negative numeric Telegram group/supergroup chat IDs.
- Legacy positive/private Telegram user IDs saved in the old offline-recipient field are ignored at runtime and are not returned as notification recipients.
- The offline notification UI now labels the field as group-only and explains that employee private Telegram IDs belong in the separate Attendance Bot employee identity map.
- Added a group-only test notification endpoint and dashboard button.
- Existing 15-minute threshold, Myanmar time window, Bot token encryption, and employee presence logic are unchanged.

## Telegram account lifecycle
- Added **取消连接**: disconnects the Telethon client but retains its encrypted session and all customer/history data.
- Added **永久删除账号** for superadmins: clears the Telegram session credentials, marks the account as archived, removes it from the active account list, and releases the employee assignment.
- The database account row is retained as a history anchor because customer rows reference it. Customer profiles, avatars, messages, tags, assignments, media and business history are not deleted.
- Archived accounts remain selectable in the admin customer-account filter as **历史归档** so historical customer records remain accessible.
- No database schema migration is required.

## Preserved behavior
- Attendance Bot A remains external and is not polled by this service.
- Employee Telegram User ID mappings, attendance events, attendance counts and break/return handling are unchanged.
- Telegram personal-account sync behavior from v7.7.6 is unchanged (no automatic bulk sync at startup).
- JWT, CORS, login security, ATTENDANCE_INGEST_SECRET, PostgreSQL and Railway Volume configuration are unchanged.

## Deployment
- Deploy this package to `xwalletchatserver` only.
- Do not deploy it to `nexbitsafeworkbot` / external Attendance Bot A service.
- Keep existing Railway Variables, PostgreSQL and persistent Volume attachments unchanged.

## Verification notes
- Python source files pass `py_compile` syntax compilation.
- Automated pytest execution in the packaging environment is blocked because the environment does not have the pinned `python-jose` dependency installed; run `pip install -r requirements.txt` before running the project's test suite.
