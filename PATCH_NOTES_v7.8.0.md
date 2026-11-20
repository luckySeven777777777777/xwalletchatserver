# X-WALLET CHAT SERVER v7.8.0 — Admin dashboard reporting update

## Added
- Shows the number of Telegram personal accounts whose current status is `connected` beside the Telegram Connected Accounts heading. Disconnected, reauthorization-required, and archived accounts are not counted.
- Adds four employee-specific KPI cards below the existing global service overview. The existing employee selector drives these cards; selecting one employee shows that employee's numbers, while selecting All Employees shows the aggregate.
- Per-employee metrics:
  - Members received today: active customer assignments created since midnight Asia/Yangon.
  - Processing conversations: the number of customers still actively assigned to that employee.
  - Online members: 1 when that employee's CRM presence is online, otherwise 0; for All Employees it is the number of online employees.
  - Attendance today: today's attendance event count plus a breakdown for Eat, Smoking, Pee, Tolied, and Other.
- Removes the literal `\\n` characters that were visible above the Business Label field.
- Hides soft-deleted employee accounts from the employee account list, quick-jump dropdown, staff overview, and attendance employee selector. Disabled (not deleted) accounts remain visible and retain the Restore Account action.

## Data preservation and compatibility
- No database schema changes or migrations.
- Employee deletion remains a soft delete in the database so historical assignments, audit events, attendance events, and related business records keep their foreign-key/history references. Deleted accounts are no longer shown in the active employee UI.
- Disabling login is unchanged; disabled accounts remain visible and can be restored.
- Telegram sessions, customer records, avatars/media, message history, customer labels, assignments, and attendance records are not deleted or rewritten.
- Existing global overview cards and staff table remain in place.
- Telegram sync, external Attendance Bot A push ingestion, group offline notifications, JWT/2FA, encryption, PostgreSQL, and Railway Volume configuration are unchanged.

## Deployment
- Deploy this package to `xwalletchatserver` only.
- Keep existing Railway Variables, PostgreSQL connection, Volume mount `/data`, `SESSION_ENCRYPTION_KEY`, `JWT_SECRET`, and `ATTENDANCE_INGEST_SECRET` unchanged.
- Do not deploy this package to `nexbitsafeworkbot` / external Attendance Bot A.
