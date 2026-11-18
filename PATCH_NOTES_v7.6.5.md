# X-WALLET v7.6.5 — Administrator permissions and employee account controls

Built on the supplied v7.6.3 backup plus the v7.6.4 avatar/header repair.

## Administrator permission controls

- Added a persistent `users.permissions` field with an additive startup migration; existing database records, Telegram sessions, messages, assignments and teams are not reset.
- Super Admin retains full access.
- Super Admin can assign fine-grained permissions when creating an Administrator and can edit permissions later from the Administrator list.
- Supported delegated permissions:
  - Create Chatting / Finding employees
  - Create and manage teams
  - Connect a Telegram personal account
  - Sync Telegram chats
  - Assign / unassign Telegram customers
  - Disable / restore employee login
  - Soft-delete employee accounts
  - Reset employee passwords
  - Change employee team membership
  - View employee statistics
- Backend API routes validate permissions. Hiding a card or button is only a UI convenience; it is not the security boundary.
- Only Super Admin can create Administrator accounts or change another Administrator's permission matrix.
- Existing Administrator accounts receive the legacy full permission set on the first migration to preserve their current access. Super Admin can narrow those permissions afterward.

## Employee account controls

- Added working Disable Login / Restore Account actions.
- Disabling an account immediately closes its active presence sessions; subsequent API requests are rejected because the user is inactive.
- Added Delete Account action as a soft delete: login is disabled, the username is internally reserved with a deleted marker, and historical chats, assignments and audit records are retained.
- A soft-deleted account can be restored if its original username has not been reused. If that username has been reused, restore returns a conflict instead of overwriting another account.
- Super Admin accounts and the current administrator's own account cannot be deleted or disabled by these actions.

## Validation

- Python source compilation passed.
- Inline JavaScript syntax check passed with Node.js.
- ZIP archive integrity test passed after packaging.
- Existing pytest smoke suite could not be run in this build container because the `python-jose` dependency is not installed. No live Railway database or credentials were available, so production migration and API behavior must be confirmed after deployment.

## Deployment

Deploy the entire `xwallet-chat-management` project folder. Keep the existing Railway `DATABASE_URL`, `SESSION_ENCRYPTION_KEY`, Telegram API credentials and Telegram sessions unchanged. Do not delete/recreate PostgreSQL or Telegram account sessions. The permissions migration is additive and runs at application startup.

## Permission behavior notes

- Telegram account selection remains visible to an administrator with customer-assignment permission so that they can allocate customers without being granted account-connection or sync privileges.
- Team lookup is available to administrators who can manage team membership, while creating teams remains a separate permission.
- Employee 2FA enrollment is separately permission-gated. Only Super Admin can reset another Administrator's 2FA or change Administrator permissions.
