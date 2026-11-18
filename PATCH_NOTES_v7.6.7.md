# X-WALLET v7.6.7 — Chat media and voice transcription

## Changes
- Chat history now uses a flex layout so the message viewport expands to the available height and the composer/actions no longer leave a large unused gap.
- Incoming Telegram photos, videos, audio and voice notes are downloaded with a MIME-derived extension when Telegram does not provide a file extension. Media downloads are allowed up to 200 MB.
- Background history sync now backfills missing media for messages that were previously imported as text only (for example when the Railway volume was full).
- Media responses are served inline with the detected content type.
- Voice notes render an audio player and a **Transcribe voice** action. Transcripts are stored in PostgreSQL and shared with users who can access that customer.
- Existing databases receive an additive `messages.transcript` column migration. Existing messages, users, assignments, Telegram sessions and customer records are not deleted.

## Required Railway variable for transcription
Set `OPENAI_API_KEY` to a valid OpenAI API key. `OPENAI_TRANSCRIPTION_MODEL` defaults to `gpt-4o-mini-transcribe`. Without the key, voice playback still works, but clicking transcription returns a clear configuration message.

## Deployment notes
- Keep the existing PostgreSQL `DATABASE_URL`, Telegram API variables, encryption key, JWT secret and `UPLOAD_DIR` unchanged.
- Ensure the Railway Volume mounted at `/data` has free capacity. Previous logs showed `No space left on device`; if storage is full, media cannot be downloaded until space is available.
- After deploying and freeing/upgrading storage, use **Sync all chats** or **Sync selected account** to backfill attachments from Telegram history.
- Telegram only returns media still available in the source chat and accessible to the connected account.
