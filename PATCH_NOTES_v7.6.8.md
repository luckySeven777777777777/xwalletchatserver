# X-WALLET v7.6.8 — Media recovery and admin monitor fixes

## Fixes
- Telegram history sync now retries media when a message already exists in PostgreSQL but its saved file path is empty or the file is missing from the persistent upload directory.
- Incoming Telegram updates also retry saving media for an existing message whose attachment is missing.
- Message API now returns an explicit media URL when the file exists, while retaining the existing authenticated media endpoint and assignment access checks.
- Super Admin staff monitoring now includes created Administrator accounts as well as Chatting/Finding/Employee accounts. Delegated administrators only see staff monitoring when granted `stats.view`; their monitor remains limited to employee roles.
- Admin page heading is now exactly `管理后台`.
- Delegated administrators without `stats.view` no longer trigger or display the yellow permission-warning banner for stats/diagnostics endpoints.

## Important deployment notes
- This patch cannot restore a media file if Telegram no longer makes it available to the connected account, or if the Railway volume is still full.
- Confirm the Railway Volume is mounted at `/data` and `UPLOAD_DIR=/data/uploads` is set.
- After deploying, use **Sync all chats** / **Sync selected account** and allow background history sync to finish. The sync now retries previously missing media files for existing message rows.
- Keep the existing `DATABASE_URL`, `SESSION_ENCRYPTION_KEY`, `JWT_SECRET`, Telegram API credentials and Telegram sessions unchanged.
- `OPENAI_API_KEY` is only required for voice transcription; it is not required to download or display Telegram photos/videos/audio.

## Validation
- Python syntax compilation passed for `app/main.py` and `app/telegram_service.py`.
- Inline frontend JavaScript passed `node --check`.
- Full pytest collection could not run in this build environment because `python-jose` is not installed (`ModuleNotFoundError: jose`). Install `requirements.txt` in the deployment/test environment before running the suite.
