# Telegram User Login Bot

This is a Telegram bot that allows users to log in with their personal Telegram account (using phone + OTP + 2FA support) via the bot interface. Once logged in, the bot can:

- Extract all your Telegram contacts' phone numbers (only numbers, no names) and export as JSON.
- Send the same message to multiple groups/chats with a configurable delay between sends (to help avoid spam filters).

**⚠️ IMPORTANT WARNINGS:**
- Logging in with this bot gives the bot FULL ACCESS to your Telegram account (messages, contacts, groups, etc.) because it uses the MTProto API as a user client.
- This is for educational/personal use only. Mass sending messages may violate Telegram's Terms of Service and lead to account bans.
- Never share your 2FA password or login details with untrusted parties.
- The bot stores your session string in a local database (bot_data.db). Keep this file secure.
- Use at your own risk. The creator assumes no liability.

## Features
- Login with phone number + OTP (code sent to your Telegram app).
- Full support for 2FA (cloud password).
- Get contacts as JSON: only phone numbers.
- Broadcast message to multiple groups with delay.
- Multi-user support (each bot user logs in their own account).
- Logout support.

## Prerequisites
1. A Telegram bot token (already provided in .env.example).
2. API ID and API Hash from https://my.telegram.org/apps (create a new app if needed; use the same for the bot).
3. Python 3.10+ (tested on 3.13).

## Setup Instructions

1. **Clone or download the files** to your machine.

2. **Install dependencies**:
   ```
   pip install -r requirements.txt
   ```

3. **Configure environment**:
   - Copy the example:
     ```
     cp .env.example .env
     ```
   - Edit `.env`:
     - `BOT_TOKEN` is pre-filled with the one you provided.
     - Replace `API_ID` and `API_HASH` with your real values from my.telegram.org.
     - Keep them secret.

4. **Run the bot**:
   ```
   python main.py
   ```

   The bot will use long polling. Keep it running (use screen, tmux, or nohup on server).

5. **Interact with the bot**:
   - Find your bot on Telegram (search by username you set in BotFather).
   - Start with `/start`.
   - Use `/login` to authorize your account.

## How Login Works
- `/login` → enter phone (e.g. +12345678901)
- Bot sends code request to your Telegram app.
- Enter the 5-6 digit code.
- If 2FA enabled, enter your 2FA cloud password.
- Session is saved securely for future use.

## Commands
- `/start` - Welcome and help.
- `/login` - Log in your Telegram account.
- `/logout` - Log out and delete session.
- `/status` - Check if logged in and show phone.
- `/get_contacts` - Export contacts phone numbers as JSON (file or code block).
- `/send` - Interactive: send same message to multiple groups with delay.
  - Enter groups: `@group1,@group2,-100123456789` (comma separated; use usernames or numeric chat IDs for private/supergroups).
  - Enter message text.
  - Enter delay in seconds (e.g. 3).
- `/cancel` - Cancel ongoing login or send flow.

## Notes
- For groups, you must be a member (and have send permissions for channels/supergroups).
- Delay helps prevent rate limits/floods. Recommended 2-10 seconds.
- Contacts extraction gets your synced phone contacts that have Telegram accounts.
- If you get "Flood wait" errors, wait and try later.
- Sessions are per chat_id (per user chatting with the bot).
- To run persistently on VPS: use `nohup python main.py &` or systemd service.
- Database: `bot_data.db` is created automatically (SQLite).

## Troubleshooting
- "Session invalid": re-login.
- Can't login: make sure API_ID/HASH match what you used for the app.
- Code not received: check your Telegram app notifications; sometimes resend by re-logging.
- 2FA issues: ensure password is correct; Telegram may require recovery if forgotten.
- For production: consider using webhook instead of polling (advanced).

## Security
- Do not commit .env or bot_data.db to git.
- Run on trusted server.
- Periodically logout if not needed.

If you need enhancements (e.g. list groups, send to contacts, scheduling), let me know!

Created for you with the provided bot token.
