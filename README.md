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
