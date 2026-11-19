# X-WALLET v7.7.2 — Employee onboarding, workspace translator, assignment alerts and attendance breaks

## Changes
- Employee/admin creation now defaults the team to the matching role team (Chatting, Finding, or Admin). The backend also resolves a matching team when auto-team mode is requested. Explicit No Team remains available.
- Duplicate usernames are detected in the current admin list before submission; the backend still enforces case-insensitive uniqueness.
- Added a Google Translate panel below Log Out in the sidebar. It is visible only while Chat Workspace is active, with a direct fallback link if Google blocks iframe embedding.
- Updated text inputs, selects, and textareas to use the navy/blue outlined control style matching the User ID pills.
- Customer assignments now create persistent in-app notifications for the target employee. The employee UI polls every four seconds and attempts an English female-voice browser speech alert. The browser must allow speech and provide an available female voice; otherwise it uses an English voice fallback.
- Added a separate attendance bot configuration (encrypted token, bot ID, group Chat ID) and employee Telegram numeric ID mapping. In the configured group, mapped employees use `/break` (or `/out`) when leaving and `/back` (or `/return`) when returning.
- While a break is active, the offline monitor pauses that employee's alert timer. On return, the offline timer restarts from the return timestamp. The offline threshold is now 15 minutes; the night monitoring window remains 20:30–09:30 Asia/Yangon.

## Attendance bot setup
- Create a dedicated bot with BotFather; do not reuse the offline-alert bot.
- Add it to the attendance group and ensure it can read group messages (disable Group Privacy in BotFather). Do not configure a webhook for this dedicated polling bot.
- Enter token, bot ID, group Chat ID, and each employee mapping as `username,TelegramUserID`, one per line.
- Employees send `/break` and `/back` in the configured group. The bot polling uses `getUpdates`; do not configure a webhook for this dedicated bot.

## Important behavior
- The screenshot's account creation response showed HTTP 409 because username `aabb` already exists (case-insensitive). This is not a role permission failure; use a new unique username. The UI now warns before submitting if the username is already in the loaded user list.
- Browser speech synthesis depends on browser/OS voices and user-gesture autoplay policy. Employees should enable sound once in the workspace.
- The Google Translate site may block iframe embedding in some browsers; the panel includes an official direct link fallback.
- Use one Railway replica for the attendance bot polling loop unless a distributed lock is added.
