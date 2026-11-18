# X-WALLET v7.7.0 — Telegram employee binding and faster admin statistics

## Changes
- Added a persistent `assigned_user_id` link to each Telegram personal account. Existing accounts remain intact and start as unassigned.
- Added an optional employee selector to the Telegram connection form; newly authorized Telegram accounts can be linked to the selected employee immediately.
- Added employee filtering and employee binding controls beside the Telegram account selector. Account cards show the currently assigned employee.
- Added an employee selector beside the Telegram customer account filter. Choosing an employee automatically selects the Telegram account linked to that employee.
- Added `PUT /api/admin/telegram/accounts/{account_id}/owner` with server-side permission and active employee validation.
- Kept delegated Administrator creation protected. The Administrator role remains visible but disabled with an authorization hint when the current admin lacks `users.create_admin`; Super Admin can grant this permission in the account permission editor.
- Optimized `/api/admin/stats` to batch-load presence, assignments, messages and audit activity instead of issuing multiple per-user database queries.
- The Refresh Admin Data modal opens immediately and fetches only statistics/diagnostics, with a 4.5-second client timeout. Other admin panels refresh in the background after the summary appears.

## Database migration
- On startup, the app adds nullable `telegram_accounts.assigned_user_id` and an index if they do not exist.
- Existing Telegram sessions, account records, customers, messages, assignments, and credentials are preserved.

## Deployment
1. Replace the source with this complete project and deploy on Railway.
2. Keep the existing PostgreSQL `DATABASE_URL`, Telegram credentials, encrypted sessions, `JWT_SECRET`, and `SESSION_ENCRYPTION_KEY`.
3. Keep Railway Volume mounted at `/data` and preserve `UPLOAD_DIR=/data/uploads`.
4. Log in as Super Admin and grant `users.create_admin` to an Administrator if that Administrator should create other Administrators.
5. In the Telegram connection form, select the employee before connecting a new account, or select an existing Telegram account and employee in the Telegram account section and click **绑定员工**.
6. In Telegram customers, choose an employee to automatically switch to that employee's linked Telegram account.

## Validation
- Python syntax compilation passed.
- Frontend JavaScript syntax check passed with Node.js.
- Full pytest suite could not run in this environment because `python-jose` is not installed. Live Railway timing and Telegram account linking still require deployment verification.
