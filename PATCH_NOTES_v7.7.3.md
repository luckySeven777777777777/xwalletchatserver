# X-WALLET v7.7.3 — Admin layout and attendance reporting

## UI updates
- Removed the Google Translate iframe and sidebar panel entirely.
- Reorganized the six Super Admin management cards into three columns and two rows on desktop; tablet and mobile layouts collapse to two and one columns.
- Added a daily attendance count tile to Service Overview.
- Added an attendance records panel directly below Service Overview, with an employee filter and refresh action.
- Attendance rows show employee/admin username, break type, start time, break duration, and return-to-seat time. All timestamps use Asia/Yangon (MMT).

## Attendance tracking
- Added a durable `employee_attendance_events` table to preserve each break and return pair. The table is created additively on startup and does not alter existing tables.
- Supported reasons: Eat, Smoking, Pee, Tolied, Other. Commands: `/break eat`, `/break smoking`, `/break pee`, `/break tolied`, `/break other`; `/break` defaults to Other. Direct shortcuts `/eat`, `/smoking`, `/pee`, `/tolied`, `/other` are also accepted. `/back`, `/return`, and `/in` close the active break.
- The employee status table now freezes the offline-duration counter during an active break and labels it as paused. When the employee returns, the offline-duration baseline restarts at the return timestamp.
- The offline alert monitor already skips employees with an active break and, after `/back`, calculates the 15-minute threshold from the return timestamp. The monitoring window remains 20:30–09:30 Asia/Yangon.
- Daily attendance counts are based on break start times in the Asia/Yangon calendar day. The employee dropdown filters records and the filtered count without changing the overall daily summary count.

## Deployment
- Existing database data is preserved. SQLAlchemy `create_all()` creates the new attendance event table on deployment.
- Keep the dedicated attendance Bot Token, Group Chat ID, employee Telegram ID mappings, offline-alert Bot Token, and recipient Chat IDs unchanged.
- Continue running one Railway replica for the long-polling attendance bot unless a distributed polling lock is implemented.
