# X-WALLET v7.6.6 — Administrator login routing and branding repair

Built from the supplied v7.6.5 project. This patch is narrowly scoped to the Administrator login failure and incorrect role branding.

## Root cause

Administrator login links were generated as `/login/<username>`. The frontend treated every such route as a Chatting-only link and rejected a successfully authenticated Administrator with the message that the link was only for Chatting employees.

## Changes

- Added a dedicated `/admin-login/<username>` route.
- Administrator account creation now returns the dedicated Administrator login URL.
- The Login Link action in the employee/admin list now generates role-correct links:
  - Administrator: `/admin-login/<username>`
  - Finding: `/finding/<username>`
  - Chatting/employee: `/login/<username>`
- Legacy `/login/<username>` links continue to work for existing Administrators, Chatting employees and legacy employee accounts. They still require the correct username and password; the URL does not grant access.
- Administrator login and post-login application branding now display exactly `X-WALLET ADMIN`.
- Finding and Chatting branding remains unchanged.
- Super Admin branding remains `X-WALLET CHAT SERVER`.

## Validation

- Python source compilation checked.
- Inline JavaScript syntax checked.
- ZIP archive integrity checked.
- No database schema changes, data deletion, Telegram session changes, or environment-variable changes are included.
- This package has not been deployed to or tested against the user's live Railway environment.
