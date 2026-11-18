# X-WALLET v7.6.9 — Admin UI, delegated admin creation and Telegram media recovery

## Changes
- The account/statistics panel no longer prints the full JSON response inside the page. The Refresh Admin Data button opens a centered modal with summary metrics and expandable raw details.
- The yellow admin API/permission status banner is permanently hidden in the administrator UI.
- Administrators can create other Administrator accounts only when the Super Admin grants `users.create_admin`.
- A delegated administrator can grant only permissions that the creating administrator already holds; the backend enforces this restriction.
- The Employee Accounts list hides the Super Admin record from delegated administrators. The Super Admin retains the full account list.
- Existing PostgreSQL/SQLite installations receive `media_type` and `media_pending` columns without replacing existing message records.
- Telegram history sync records the type of incoming media and marks media that could not be downloaded as pending, so the chat UI can still attempt recovery.
- Opening a pending/missing attachment now asks the connected Telegram account to retrieve the original Telegram message and save the media to the persistent upload volume before serving it.
- Media classification now explicitly recognizes common photo, video, audio and voice-note file extensions.

## Deployment
1. Upload/deploy this ZIP as the project source.
2. Keep the Railway Volume mounted at `/data` and set `UPLOAD_DIR=/data/uploads`.
3. Keep the existing `DATABASE_URL`, Telegram API credentials, Telegram sessions, `JWT_SECRET` and `SESSION_ENCRYPTION_KEY` unchanged.
4. After deployment, use **Sync selected account** for the Telegram account, wait for history sync to complete, then refresh the customer chat.
5. Media recovery requires the Telegram account to remain connected and Telegram to still provide the original message. Files already deleted from Telegram cannot be restored.
6. `OPENAI_API_KEY` is not needed for photo/video/audio playback. It is only needed if you want voice-to-text transcription.

## Validation
- Python syntax compilation passed for `app/main.py`, `app/telegram_service.py` and `app/models.py`.
- Frontend JavaScript passed `node --check`.
- A live Telegram account and Railway volume are required for end-to-end media recovery testing.
