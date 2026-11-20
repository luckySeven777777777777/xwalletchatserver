# X-WALLET CHAT SERVER v7.7.7

## Added
- Super Admin employee-account quick selector in the Employee Accounts panel. Selecting an account scrolls to and briefly highlights its row; the selector includes username, role, active/disabled status, and public User ID.
- Shared `Business Label` field in Customer Profile, editable only by administrators and visible to employees who are authorized to access the customer.
- Business Label badge in the employee inbox customer list and active chat header. The label follows the customer record and remains independent of employee assignment.

## Security and data behavior
- Reuses the existing shared customer `Tag` storage; no new database table or schema migration is required.
- Employees can read shared Business Labels for customers they can access, but the API rejects employee attempts to modify this field.
- Existing private/team tags and other customer profile fields are unchanged.
- No changes to Telegram polling, Bot A, attendance ingest, offline notifications, or Telegram client synchronization behavior in this update.

## Deployment
- Deploy this package to the `xwalletchatserver` service only.
- Keep existing Railway Variables, PostgreSQL, and persistent Volume unchanged.
- Do not deploy this package to `nexbitsafeworkbot`.
