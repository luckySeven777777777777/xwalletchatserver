## Current release: v7.7.5

See `PATCH_NOTES_v7.7.1.md` for fast admin action behavior and Telegram employee offline notifications.

# Telegram CRM Management (Railway starter)

A deployable first-stage CRM scaffold for ordinary Telegram user accounts using MTProto, with separate admin/employee logins, assignment-based access, employee-private tags, team-shared tags, persistent message storage, and external social-site shortcuts.

## Important status and limitations

This is a functional starter implementation, not a certified production release. It has not been authorized against the user's live Telegram account and cannot be claimed to pass live Telegram tests. Before production, perform the test checklist below with test accounts. Telegram Secret Chats are not supported by cloud-history synchronization. The privacy filter is best-effort only: it cannot guarantee removal of names, phone numbers, usernames embedded in images/audio/files, contact cards, or identity disclosed in message context. For strict anonymity, do not assign media or messages that reveal identity, or add a human review/redaction workflow.

The current MVP persists text messages and message metadata. Media download/storage and delivery of inbound/outbound media are not yet implemented. The initial dialog sync imports up to 1,000 messages per dialog. It uses in-memory Telegram client connections; for high availability and 50+ accounts, move MTProto workers to a separate Railway worker service and use a durable queue/lease system before scaling.

## Railway deployment

1. Create a Railway project and add a PostgreSQL service.
2. Deploy this repository from GitHub or Railway CLI (Dockerfile is included).
3. Add variables to the app service:
   - `DATABASE_URL=${{Postgres.DATABASE_URL}}`
   - `JWT_SECRET`: generate a long random secret.
   - `SESSION_ENCRYPTION_KEY`: generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
   - `ADMIN_USERNAME`, `ADMIN_PASSWORD` (long unique password).
   - `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` from your Telegram developer app.
4. Add a persistent Railway volume if you later enable media storage. Telegram sessions are encrypted in PostgreSQL in this version.
5. Generate a public domain. Open it and sign in using the bootstrap superadmin credentials.
6. Add Telegram accounts from Admin → Connect Telegram. Enter phone in international format, submit the code, and if requested submit the 2FA password.

Do not paste API HASH, phone login codes, Telegram passwords, or StringSession into source code, browser local storage, or chat. Because API HASH was previously exposed in a conversation, consider creating a fresh Telegram app credential set and update Railway variables.

## Roles and behavior

- `superadmin` / `admin`: see Telegram identity fields, create employees/admins/teams, assign/reassign/revoke customers, see accounts and audit log.
- `employee`: sees only currently assigned customers; Telegram identity fields are not serialized in employee endpoints; can send text and manage own private tags.
- `private` tags belong to one employee and are not visible to another employee.
- `team` tags are visible to members of the same team.
- Reassignment revokes previous active assignment and grants new employee access to the same stored history. Revoke does not delete messages.
- Employee logout does not delete assignments or history.

## UI branding and diagnostics

- Admin sidebar brand: `X-WALLET CHAT MANAGEMENT`.
- Employee sidebar and login brand: `X-WALLET SERVER`.
- Social Channels uses recognizable brand marks for Telegram, WhatsApp, Instagram, TikTok and LINE (loaded from Simple Icons CDN).
- Admin data loading reports the failing API path and HTTP status instead of showing only a generic `Request failed` alert.
- `GET /api/health/ready` performs a database readiness check; authenticated admins can use `GET /api/admin/diagnostics` to check database and Telegram configuration presence without exposing secrets.

## Automated checks

Run `pytest -q` after installing requirements. Also run `python -m compileall app`.

## Required live acceptance tests

Use two Telegram test accounts, one admin, and two employees:
1. Authorize two Telegram accounts and verify both remain connected after app restart.
2. Send and receive text messages from an external Telegram client.
3. Import previous cloud chat history; verify messages are present.
4. Assign customer to Employee A; verify A can see and reply.
5. Verify Employee A API responses contain no Telegram username, phone, peer ID, or access hash.
6. Log A out and back in; verify history and tags remain.
7. Reassign customer to Employee B; verify B sees the full stored history and A gets 404.
8. Verify A's private tags are invisible to B; verify team tags are visible only to same-team members.
9. Revoke customer; verify employee access ends while records remain.
10. Restart/redeploy Railway; verify database history and encrypted Telegram sessions survive.
11. Verify messages containing usernames/phone numbers are redacted in employee views; separately test images, files, contact cards, voice messages and context leakage.

## Known production work before 50+ accounts

- Move Telegram connection workers to a dedicated worker service with supervised reconnects and account leases.
- Add database migrations (Alembic), rate limits, CSRF/origin policy, audit-log retention, backups and restore drills.
- Add per-employee concurrent-session limits and session revocation.
- Add media download/storage, media privacy review, outbound media, edit/delete/read receipts.
- Add pagination and incremental history sync; initial sync is capped at 1,000 messages/dialog.
- Add robust per-account rate limiting and Telegram flood-wait handling.
- Add websocket push for realtime UI (current UI refreshes on demand).
- Add assignment quotas and daily reception counters with proper business timezone configuration.


## v3 repair notes
- Team list is loaded in the admin panel; new employees can be assigned to a team at creation, and existing users can be moved between teams.
- Employee/admin creation returns a dedicated `/login/<username>` URL. The URL pre-fills the username only; the user must still enter their password.
- Admin notices now use a centered in-app modal instead of browser alert/prompt dialogs.
- Social platform logo assets are bundled locally under `static/logos/`; no external logo CDN is required.
- Telegram authorization now submits the one-time code first. If Telegram requests 2-step verification, the second submission uses the password stage. Start a fresh code request if the in-memory challenge has expired or the Railway service restarted.
- API validation errors now display the structured response details instead of `[object Object]`.


## v4 repair notes (role, assignment, time and profile)
- Added employee account types `chatting` and `finding`. Chatting employees see Chat Workspace and Logout only; Finding employees also see Social Channels; admin and superadmin retain all sections.
- Added User ID identity pill to the upper-right of every signed-in page and IDs in the administrator employee list.
- Replaced manual customer User ID entry with an employee dropdown. Admin customer records now show the assigned employee username and User ID.
- Employee inbox refreshes automatically every 10 seconds while open and refreshes again when the browser window regains focus.
- Enter sends a chat message; Shift+Enter inserts a new line. Message timestamps are rendered in Asia/Yangon (Myanmar Time).
- Replaced generic private tags input with fixed English customer profile fields: Nickname, Age, Retired, Currently Working, Marital Status, Living Alone, and Location. These fields are saved as shared tags and shown to authorized assigned employees and administrators. Record only information voluntarily provided by the customer.
- Expanded Telegram account cards for admins and superadmins with account ID, label, masked phone, status, customer count, and connection time. Encrypted Telegram sessions are never exposed.
- Fixed timezone-aware/naive datetime handling in the admin statistics endpoint.
- Employee login links use `APP_BASE_URL` (or `PUBLIC_APP_URL`) when configured. Set this to a custom domain you own and have connected to the hosting service to avoid exposing the hosting-provider hostname. Without it, the UI shows a relative `/login/<username>` path.


## v5 follow-up fixes
- Added a permanent, unique six-digit `public_id` for every user account. Existing users are backfilled at startup; internal database IDs and assignment foreign keys remain unchanged.
- `/api/me`, login responses, and administrator user lists now include the public ID. The signed-in header displays `User ID: 637368` without a `#` prefix or username chip.
- Employee creation and login-link dialogs use the Railway URL `https://xwalletchatserver-production.up.railway.app` by default for direct testing. `APP_BASE_URL` can still override it if needed.
- Added the `Current Job Title / Occupation` text field directly below `Currently Working`, saved as shared customer profile information.
- Fixed action-row spacing so account buttons have visible gaps and do not touch or overlap.


## v5 testing URL and public ID details
- The employee login URL is intentionally set to the Railway host shown in the supplied screenshots: `https://xwalletchatserver-production.up.railway.app/login/<username>`.
- Public account IDs are six-digit random values stored in `users.public_id`; they remain stable on subsequent logins and are backfilled for existing accounts on application startup.
- Internal numeric user primary keys are retained only for database relations and API operations.


## v6 feature set
- Role-based navigation: Chatting users see Chat Workbench and Logout; Finding users see Chat Workbench, Social Channels and Logout. Admin roles retain the admin panel.
- Yangon time: account timestamps are serialized as UTC and rendered in the browser with the `Asia/Yangon` time zone (MMT, UTC+06:30).
- TOTP 2FA: self-service enrollment for every role, QR provisioning URI issuer `X-Wallet`, account label based on the user's login nickname, encrypted TOTP secret at rest, and mandatory one-time-code challenge on subsequent logins once enabled. Pending 2FA challenge tokens are rejected by normal authenticated API routes.
- Chat attachments: photo/video/audio uploads are sent through the linked Telegram account; incoming Telegram media is downloaded for the CRM chat history. `.ogg` / `.opus` audio is sent as a Telegram voice note.
- Telegram customers: admin-only account selector filters the customer list by Telegram account. Telegram display name, username, phone and profile photo are shown only in admin endpoints/pages. Employee inbox names remain masked and no profile-photo endpoint is exposed to employees.
- Social Channels are restricted to a sandboxed iframe inside the CRM. The application does not invoke desktop-app URI schemes, open popups, or navigate the top-level page. Some providers intentionally block iframe embedding; this CRM cannot override their security headers. If blocked, the platform cannot be displayed inside the CRM without an approved provider API/SDK or an authorized embeddable integration.

### Railway media persistence
Create a Railway Volume mounted at `/data` and set the Railway variable `UPLOAD_DIR=/data/uploads`. Without a persistent volume, local uploaded media and cached Telegram photos can be lost when Railway replaces the container. Keep the existing `DATABASE_URL`, `JWT_SECRET`, and `SESSION_ENCRYPTION_KEY`; do not reset the PostgreSQL database or Telegram sessions.


## v6.1 repair update (uploaded project patch)

- Added account-scoped customer selection with visible per-customer checkboxes, select-all, bulk assign and bulk unassign controls. Bulk operations apply only to the currently selected Telegram account list; history is retained.
- Added administrator-managed TOTP enrollment for employee accounts. The administrator displays a QR code, the target authenticator verifies a 6-digit code, and only then is 2FA enabled.
- Added a superadmin-only reset endpoint for another account's 2FA. A reset clears the old authenticator binding; the account can sign in without 2FA until re-enrolled. A superadmin cannot reset their own authenticator through this endpoint and must use their own security settings.
- Employee workspaces no longer show the 2FA settings control. Chatting employees see Chat Workbench and Logout; Finding employees additionally see Social Channels. Employee branding is `X-WALLET CHATTING`.
- Compact chat typography and controls; attachment picker is limited to photos and videos; incoming voice/audio remains playable and Telegram's native forward operation supports message media. Emoji picker is collapsed until clicked; send action label is `Sent`.
- Added animated layered background and glass-style panels, respecting reduced-motion preferences.
- Social Channels remains an embedded iframe workspace. This does not guarantee that third-party sites permit embedding or protect credentials entered into those sites; use official apps or a managed enterprise integration for sensitive account access.

## v6.1 deployment notes

Deploy the full updated project, not only `static/index.html`. The backend adds three routes: `POST /api/admin/users/{uid}/2fa/setup`, `POST /api/admin/users/{uid}/2fa/confirm`, and `POST /api/admin/users/{uid}/2fa/reset`. Existing databases are updated in-place by the current startup migration; no table schema change is required for these routes. Keep the Railway Volume mounted at `/data` and set `UPLOAD_DIR=/data/uploads` if you want uploaded media to survive redeployments.

- Added `/superadmin-login` as a management-branded login URL. Employee login links continue to use the employee-facing X-WALLET CHATTING branding.
- Restricted self-service 2FA setup, confirmation and disable endpoints to administrator roles; employees cannot enable or alter 2FA through hidden API calls.


## v6.2 hotfix (2026-11-18)

This release addresses the issues reported after v6.1:

- Sidebar navigation buttons now respect the `hidden` state; Chatting employees see only Chat Workspace and Logout, while Finding employees see Chat Workspace, Social Channels, and Logout. Admin navigation remains available only to administrator roles.
- The main application and admin workspace use the full available viewport; chat panes are visually unified and use more compact typography.
- A disabled bootstrap Super Admin can be recovered only with the matching Railway `ADMIN_USERNAME` and `ADMIN_PASSWORD` environment credentials. Set these variables in Railway before signing in again. The recovery path does not reactivate employee or ordinary administrator accounts.
- Telegram sync now reads both the main dialog folder and Archived Chats. Admins can explicitly sync all dialogs for a selected Telegram account.
- Admin Telegram account cards can retrieve and display the connected account's current Telegram profile name, username, phone, numeric Telegram ID, and profile photo.
- Social Channels no longer uses an iframe that Telegram/Meta/TikTok may block. It provides official desktop-app protocol launch attempts where supported, plus official download/site links. A browser cannot embed or guarantee launching another vendor's desktop program; Windows/browser permission prompts may appear.

### Important deployment notes

1. Back up the Railway PostgreSQL database and keep the existing Railway Volume mounted at `/data`.
2. Keep the existing `JWT_SECRET`, `SESSION_ENCRYPTION_KEY`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `DATABASE_URL` values unchanged.
3. Confirm `UPLOAD_DIR=/data/uploads`.
4. For the bootstrap Super Admin recovery, set `ADMIN_USERNAME` to the existing Super Admin username (for example `superadmin`) and set a new strong `ADMIN_PASSWORD` in Railway Variables. Do not paste these secrets into chat or commit them to source control. Redeploy, then sign in using those credentials.
5. After deployment, sign in as an administrator, select each Telegram account, and click **Sync selected account**. Archived Chats are included. The sync imports dialog customers and up to the latest 1,000 messages per dialog; it can take time for accounts with many chats.
6. Verify employee permissions using one Chatting account and one Finding account before assigning live customers.

This release has Python and JavaScript syntax checks completed. Runtime integration tests against the live Railway database and Telegram accounts must still be performed after deployment.


## v7.3 repair notes (based on the supplied v7.2 backup)

- Login tokens are stored in per-tab `sessionStorage` keys scoped to the current employee login URL; `/login/<username>` rejects a session whose authenticated username does not match the link, and `/superadmin-login` accepts only the super administrator role.
- Login background layers now cover the complete viewport, including dynamic viewport height.
- Employee creation now validates and commits the user record separately from best-effort audit logging, handles uniqueness conflicts, and returns a clear server error while writing the underlying exception to Railway logs.
- Telegram synchronization indexes regular and archived chat dialogs first, commits the full customer index, and starts message-history import in the background so the admin customer list can appear without waiting for message downloads.
- Message history import runs in the background after the complete customer/dialog index is committed. It requests all message history available from Telegram; completion time depends on the number of dialogs, message volume, and media downloads.
- Social Channels can request launch of registered desktop-app protocols or open official websites. A browser page cannot embed/control another vendor's native desktop application inside the web page; Windows/browser protocol permissions still apply.

Run the existing deployment with the same Railway service, PostgreSQL database, volume, and environment variables. Do not delete or recreate the database or Telegram session volume.


## v7.4 reliability notes (Railway)

- The root login page and `/superadmin-login` identify as **X-WALLET CHAT SERVER**. Employee links under `/login/{username}` identify as **X-WALLET CHATTING**.
- Login sessions use `sessionStorage` with a dedicated key for the super administrator and a separate key per employee username. Refreshing a page keeps that tab's session; one employee link does not overwrite another employee or the administrator session.
- If `DATABASE_URL` is configured, the app uses that database (recommended: Railway PostgreSQL reference variable). If it is not configured and a Railway Volume is mounted at `/data`, the app now uses `/data/xwallet.db` rather than the disposable container filesystem. A pre-existing `./telegram_crm.db` is copied once to the volume on first startup when applicable.
- Set `UPLOAD_DIR=/data/uploads` when using the Railway Volume so uploaded media also persists.
- Employee usernames are resolved case-insensitively; the password remains case-sensitive. Passwords must be at least 10 characters and no more than 72 UTF-8 bytes with the current bcrypt hash scheme.
- An HTTP 401 during employee login means the account was not found in the active database, is disabled, or the supplied password does not match. The server log now records which category occurred without logging the password. An administrator can use **Reset Password** in the employee account list and share the new password securely.

### Railway database setup recommendation

In the X-Wallet service Variables, set `DATABASE_URL` to the Railway PostgreSQL service reference (for example `${{Postgres.DATABASE_URL}}`) if PostgreSQL is attached. Do not point different app services or replicas at separate SQLite files. If using the mounted Volume fallback instead, keep the volume mounted at `/data` for every deployment and retain `UPLOAD_DIR=/data/uploads`.


## v7.5 role branding and embedded-only Social Channels
- Finding employees receive a dedicated `/finding/<username>` login link and display `X-WALLET FINDING` on login and in the workspace. Chatting employees continue to use `/login/<username>` and `X-WALLET CHATTING`.
- Login links generated by employee management now select the correct route from the employee role. A role mismatch is rejected after authentication.
- Social Channels no longer launch Telegram/WhatsApp/Instagram/LINE desktop apps and no longer open external tabs. All platform destinations are loaded only inside a sandboxed iframe in the CRM. Providers that block iframe embedding remain unavailable in this in-app view; this restriction cannot be bypassed by frontend code.

## v7.6 operations and deployment notes

- Railway production must reference the Postgres service variable `DATABASE_URL` in the `xwalletchatserver` service. The application now fails fast with a clear message rather than silently falling back to an ephemeral SQLite database.
- `/api/health/ready` reports the active database dialect. `/api/admin/diagnostics` reports the dialect and whether `DATABASE_URL` is configured (never the secret value).
- Employee presence is tracked per browser tab/session using a unique session key, a 20-second heartbeat, and an explicit logout event. The admin dashboard marks a session offline after 90 seconds without a heartbeat.
- Daily activity counters use the Asia/Yangon business day. Message counts are derived from persisted Telegram messages and assignments.
- Browser voice notifications use the browser's speech synthesis voices. A female-sounding English voice is selected when the browser provides one; the exact voice depends on the operating system/browser voice pack.
- Social channel views remain inside the CRM iframe. Platforms that block iframe embedding cannot be forced to render inside the CRM by client-side code.
- 2FA setup continues to require a valid current authenticator code. The QR rendering was reduced for faster display, but secure enrollment cannot be completed before the employee scans the QR and verifies a code.


## v7.6.3 repair notes

- Admin Telegram synchronization now indexes the main chat list, Archived Chats and Saved Messages, and stores dialog-folder metadata.
- Saved Messages is administrator-private: it is excluded from employee inboxes and assignment operations, and employee access checks reject it even if a stale assignment exists.
- Admin Telegram account profile refresh persists the latest available account avatar and refreshes it without browser cache.
- Telegram connected-account cards are placed above the customer list; team management is combined with the Create Team card; the 2FA information card is placed below Social Channels.
- The migration adds `customers.dialog_folder` and `customers.is_saved_messages` without dropping existing records.
- Live Telegram sync still requires deployment with valid Telegram credentials and an authorized account. Telegram privacy settings may prevent access to some profile fields or media.


## v7.6.4 operational note
Railway reported `OSError(28): No space left on device` during Telegram account avatar writes. This release serves connected-account profile photos from process memory to avoid that particular write. It is not a substitute for freeing/increasing the Railway volume: message media and other uploads still need disk space. The in-memory avatar cache is lost when the process restarts and is repopulated when an admin refreshes the connected account profile.


## v7.7.2 additions
See `PATCH_NOTES_v7.7.2.md` for the employee role/team auto-mapping, Google Translate sidebar panel, persistent assignment notifications with browser speech, and Telegram attendance bot setup. Attendance commands are `/break` and `/back`; break periods pause offline alerts and the threshold is 15 minutes.


## v7.7.3 additions
See `PATCH_NOTES_v7.7.3.md` for removal of the Google Translate sidebar, the 3-column by 2-row admin module layout, daily attendance counts and per-employee attendance records. Attendance break commands accept `/break eat`, `/break smoking`, `/break pee`, `/break tolied`, `/break other` (or direct `/eat`, `/smoking`, `/pee`, `/tolied`, `/other`) and `/back`. Break time is excluded from offline duration and offline-alert timing resumes from the return timestamp.

## v7.7.4 attendance integration (separate Bot A Railway service)

The X-WALLET management service no longer polls the employee attendance bot. It never calls `getUpdates` for Bot A. Keep Bot A running in its current Railway service and push parsed `/break` and `/back` events to `POST /api/integrations/attendance/events` using the shared `ATTENDANCE_INGEST_SECRET` header. See `PATCH_NOTES_v7.7.4.md` for the exact Python helper and Railway variables. The Employee Attendance · Today panel now includes separate daily count cards for Eat, Smoking, Pee, Tolied, and Other.


## v7.7.5 security and reliability update
See `PATCH_NOTES_v7.7.5.md`. This release requires a unique `JWT_SECRET` (at least 32 characters) in Railway Variables. It narrows CORS to the configured application origin, adds security headers and API no-store caching, throttles repeated login/2FA failures, validates the configured attendance group strictly, and deduplicates Bot A event retries with persistent database receipts. Bot A adds event IDs and retries transient HTTP failures without starting another Telegram polling process.

Keep the existing `ATTENDANCE_INGEST_SECRET` unchanged for this deployment if it is already configured and at least 32 characters. Keep `SESSION_ENCRYPTION_KEY` unchanged. Do not change Bot A's `BOT_TOKEN`, `GROUP_CHAT_ID`, or polling settings. The login throttle is process-local; run one management web replica unless a shared rate-limit store is added.


## v7.7.6 hotfix
See `PATCH_NOTES_v7.7.6.md`. Startup restores Telegram personal-account connections without automatically downloading every dialog avatar/history. Telegram clients disconnect gracefully on service shutdown. Manual admin sync remains available.
