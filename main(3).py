#!/usr/bin/env python3
"""
Telegram Bot for User Account Login, Contacts Extraction, and Group Messaging with Delay.
Supports phone + OTP + 2FA login.
Uses Telethon for MTProto user client + aiogram for bot interface.
"""

import os
import json
import asyncio
import logging
import sys
from datetime import datetime
from typing import Optional, Dict

import aiosqlite
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError,
)
from telethon.tl.functions.contacts import GetContactsRequest

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardRemove, BufferedInputFile
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from aiohttp import web

# Load environment (from .env file if present, otherwise from system/Render env vars)
load_dotenv()

# === CRITICAL: Robust environment variable loading with clear errors for Render ===
print("=== Telegram User Bot Startup Debug (Render logs) ===")
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID_STR = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

print(f"BOT_TOKEN present: {bool(BOT_TOKEN)} (len={len(BOT_TOKEN) if BOT_TOKEN else 0})")
print(f"API_ID present: {bool(API_ID_STR)} value='{API_ID_STR}'")
print(f"API_HASH present: {bool(API_HASH)} (len={len(API_HASH) if API_HASH else 0})")
print(f"PORT (for Render): {os.getenv('PORT', '8080 (default)')}")

missing = []
if not BOT_TOKEN:
    missing.append("BOT_TOKEN")
if not API_ID_STR:
    missing.append("API_ID")
if not API_HASH:
    missing.append("API_HASH")

if missing:
    error_msg = f"❌ CRITICAL ERROR: Missing required environment variables: {', '.join(missing)}\n" \
                "   → On Render: Go to your Web Service → Environment and ADD these variables:\n" \
                "     BOT_TOKEN = 8937699034:AAGWz4WcTJoxfMEOrDzii5CRVFnj8B_RHU8\n" \
                "     API_ID = (your real api_id from https://my.telegram.org)\n" \
                "     API_HASH = (your real api_hash)\n" \
                "   → Do NOT rely only on .env file (it is usually gitignored).\n" \
                "   → After adding, redeploy the service."
    print(error_msg)
    sys.exit(1)

try:
    API_ID = int(API_ID_STR)
    print(f"API_ID parsed successfully as integer: {API_ID}")
except ValueError:
    error_msg = f"❌ CRITICAL ERROR: API_ID must be a valid integer. Current value: '{API_ID_STR}'\n" \
                "   → Check in Render Environment variables that API_ID is the number only (no quotes)."
    print(error_msg)
    sys.exit(1)

print("✅ All required environment variables loaded successfully.")
print("=======================================================")

# Create Bot and Dispatcher HERE — after validation, BEFORE any @dp.message decorators run
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
print("✅ Bot and Dispatcher objects created.")

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Globals
DB_PATH = "bot_data.db"
login_clients: Dict[int, TelegramClient] = {}  # Temporary clients during login (chat_id -> client)
active_broadcasts: Dict[int, asyncio.Task] = {}  # Continuous broadcast tasks per chat_id

# FSM States
class LoginStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()

class SendStates(StatesGroup):
    waiting_groups = State()
    waiting_message = State()
    waiting_delay = State()

# Bot and Dispatcher will be created inside main() after validation

# ==================== DATABASE ====================

async def init_db():
    """Initialize SQLite database for storing user sessions."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                phone TEXT,
                session TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()
    logger.info("Database initialized.")

async def save_user(chat_id: int, phone: str, session: str):
    """Save or update user session."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO users (chat_id, phone, session, updated_at)
            VALUES (?, ?, ?, ?)
        """, (chat_id, phone, session, datetime.now().isoformat()))
        await db.commit()
    logger.info(f"Saved session for chat_id={chat_id}")

async def get_user_session(chat_id: int) -> tuple[Optional[str], Optional[str]]:
    """Get phone and session string for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT phone, session FROM users WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0], row[1]
            return None, None

async def delete_user(chat_id: int):
    """Delete user session."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
        await db.commit()
    logger.info(f"Deleted session for chat_id={chat_id}")

# ==================== TELETHON HELPERS ====================

async def get_authorized_client(chat_id: int) -> Optional[TelegramClient]:
    """Load a user client from saved session. Connects and checks authorization.
    Returns connected client or None. Caller must disconnect after use.
    """
    phone, session_str = await get_user_session(chat_id)
    if not session_str:
        return None

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)

    # Use start() for authorized sessions — it connects and initializes properly
    # (better for entity resolution, contacts, etc. than plain connect())
    await client.start()

    if not await client.is_user_authorized():
        logger.warning(f"Client for {chat_id} not authorized.")
        await client.disconnect()
        await delete_user(chat_id)
        return None

    return client

async def extract_contacts_numbers(client: TelegramClient) -> list[str]:
    """Extract only phone numbers from contacts (no names).
    Uses raw GetContactsRequest because high-level get_contacts() is not available
    in this Telethon version.
    """
    result = await client(GetContactsRequest(hash=0))
    # result.users contains the full User objects (with .phone if available)
    users = result.users
    numbers = []
    for user in users:
        if user.phone:
            # Telethon stores phone without leading +, add it
            phone = user.phone.strip()
            if not phone.startswith('+'):
                phone = '+' + phone
            numbers.append(phone)
    # Deduplicate while preserving order
    seen = set()
    unique_numbers = []
    for n in numbers:
        if n not in seen:
            seen.add(n)
            unique_numbers.append(n)
    return unique_numbers


async def run_continuous_broadcast(chat_id: int, groups: list, message: str, delay: float):
    """Run continuous broadcast loop until cancelled by user (/stop)."""
    client = None
    try:
        client = await get_authorized_client(chat_id)
        if not client:
            await bot.send_message(chat_id, "❌ Session lost. Broadcast stopped.")
            return

        # Populate entity cache for reliable numeric ID resolution
        try:
            await client.get_dialogs(limit=200)
        except Exception:
            pass

        cycle = 0
        while True:
            # Check if still active (user may have stopped)
            if chat_id not in active_broadcasts:
                break

            cycle += 1
            for idx, group in enumerate(groups):
                if chat_id not in active_broadcasts:
                    break

                target = group
                if isinstance(group, str) and group.lstrip("-").isdigit():
                    target = int(group)

                try:
                    await client.send_message(target, message)
                    # Success - silent (to avoid spam), user can monitor manually
                except FloodWaitError as e:
                    try:
                        await bot.send_message(chat_id, f"⏳ Flood wait on {group}: sleeping {e.seconds}s")
                    except:
                        pass
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    try:
                        await bot.send_message(chat_id, f"⚠️ Error to {group}: {str(e)[:80]}")
                    except:
                        pass

                # Delay between individual sends
                if idx < len(groups) - 1:
                    await asyncio.sleep(delay)

            if chat_id not in active_broadcasts:
                break

            # Periodic status update (every 5 cycles) to let user know it's still running
            if cycle % 5 == 0:
                try:
                    await bot.send_message(
                        chat_id,
                        f"🔄 Continuous broadcast cycle #{cycle} completed for {len(groups)} groups."
                    )
                except:
                    pass

            # Small pause between full cycles (use the same delay)
            await asyncio.sleep(delay)

    except asyncio.CancelledError:
        # Normal stop via /stop
        pass
    except Exception as e:
        try:
            await bot.send_message(chat_id, f"❌ Broadcast crashed: {str(e)}")
        except:
            pass
    finally:
        if client:
            try:
                await client.disconnect()
            except:
                pass
        if chat_id in active_broadcasts:
            del active_broadcasts[chat_id]
        try:
            await bot.send_message(chat_id, "🛑 Continuous broadcast has been stopped.")
        except:
            pass


# ==================== BOT HANDLERS ====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Welcome to the Telegram User Account Bot!\n\n"
        "This bot lets you log in with your personal Telegram account to:\n"
        "• Extract contacts' phone numbers as JSON (numbers only)\n"
        "• <b>Continuous broadcast</b>: send the same message repeatedly to multiple groups until you stop it\n\n"
        "⚠️ <b>SECURITY WARNING</b>: Logging in gives this bot full control over your account (MTProto user session). "
        "Only use with trusted setups. Sessions are stored locally in bot_data.db.\n\n"
        "Commands:\n"
        "/login - Log in with phone + OTP (+ 2FA if enabled)\n"
        "/logout - Log out and delete your session\n"
        "/status - Show login status + active broadcast info\n"
        "/get_contacts - Export contacts phone numbers as JSON\n"
        "/send - Start continuous broadcast to multiple groups (comma-separated)\n"
        "/stop - Stop the current continuous broadcast\n"
        "/cancel - Cancel current login or send setup flow\n\n"
        "Use /login to begin. After login, use /send for continuous looping broadcasts.",
        parse_mode="HTML"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await cmd_start(message)

@dp.message(Command("status"))
async def cmd_status(message: Message):
    chat_id = message.chat.id
    phone, session = await get_user_session(chat_id)
    status_lines = []
    if session:
        status_lines.append(f"✅ Logged in as: {phone or 'unknown phone'}")
    else:
        status_lines.append("❌ Not logged in.")

    if chat_id in active_broadcasts:
        status_lines.append("🔄 Continuous broadcast is RUNNING. Use /stop to halt.")
    else:
        status_lines.append("No active broadcast.")

    await message.answer("\n".join(status_lines))

@dp.message(Command("logout"))
async def cmd_logout(message: Message):
    chat_id = message.chat.id
    if chat_id in login_clients:
        try:
            await login_clients[chat_id].disconnect()
        except Exception:
            pass
        del login_clients[chat_id]

    # Stop any running continuous broadcast
    if chat_id in active_broadcasts:
        active_broadcasts[chat_id].cancel()
        del active_broadcasts[chat_id]

    await delete_user(chat_id)
    await message.answer("✅ Logged out successfully. Your session and any active broadcast have been stopped/deleted.")

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    chat_id = message.chat.id
    current_state = await state.get_state()
    if current_state:
        if chat_id in login_clients:
            try:
                await login_clients[chat_id].disconnect()
            except Exception:
                pass
            del login_clients[chat_id]
        await state.clear()
        await message.answer("❌ Operation cancelled.", reply_markup=ReplyKeyboardRemove())
    elif chat_id in active_broadcasts:
        active_broadcasts[chat_id].cancel()
        await message.answer("🛑 Broadcast stop requested via cancel.")
    else:
        await message.answer("No active operation to cancel.")


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    """Stop any active continuous broadcast for this user."""
    chat_id = message.chat.id
    if chat_id in active_broadcasts:
        task = active_broadcasts[chat_id]
        task.cancel()
        await message.answer("🛑 Stop signal sent. The broadcast will halt shortly.")
    else:
        await message.answer("No active continuous broadcast to stop. Use /send to start one.")


# ==================== LOGIN FLOW ====================

@dp.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext):
    chat_id = message.chat.id
    # Clean any previous login client
    if chat_id in login_clients:
        try:
            await login_clients[chat_id].disconnect()
        except Exception:
            pass
        del login_clients[chat_id]

    await state.set_state(LoginStates.waiting_phone)
    await message.answer(
        "📱 Please send your phone number in international format (with +).\n"
        "Example: +12345678901\n\n"
        "We will send a login code to your Telegram account.",
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(LoginStates.waiting_phone, F.text)
async def process_phone(message: Message, state: FSMContext):
    chat_id = message.chat.id
    phone = message.text.strip().replace(" ", "").replace("-", "")

    if not phone.startswith("+") or not phone[1:].isdigit() or len(phone) < 8:
        await message.answer("❌ Invalid phone format. Must start with + and contain only digits. Example: +12345678901")
        return

    await state.update_data(phone=phone)

    try:
        # Create fresh temporary client for login
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()

        # Send code request
        sent = await client.send_code_request(phone)

        # Store client for next steps (must stay connected)
        login_clients[chat_id] = client

        await state.update_data(phone_code_hash=sent.phone_code_hash)
        await state.set_state(LoginStates.waiting_code)

        await message.answer(
            "✅ Login code has been sent to your Telegram app (check notifications or the 'Telegram' chat from 777000).\n\n"
            "Please reply with the code you received (digits only, e.g. 12345).\n"
            "If you don't receive it, use /cancel and try /login again."
        )
    except PhoneNumberInvalidError:
        if chat_id in login_clients:
            try:
                await login_clients[chat_id].disconnect()
            except: pass
            del login_clients[chat_id]
        await state.clear()
        await message.answer("❌ Invalid phone number. Make sure it's correct and registered on Telegram.")
    except FloodWaitError as e:
        if chat_id in login_clients:
            try:
                await login_clients[chat_id].disconnect()
            except: pass
            del login_clients[chat_id]
        await state.clear()
        await message.answer(f"⏳ Flood wait: Please wait {e.seconds} seconds before trying again.")
    except Exception as e:
        if chat_id in login_clients:
            try:
                await login_clients[chat_id].disconnect()
            except: pass
            del login_clients[chat_id]
        await state.clear()
        await message.answer(f"❌ Error sending code: {str(e)}\nPlease try /login again later.")
        logger.error(f"Login phone error for {chat_id}: {e}")

@dp.message(LoginStates.waiting_code, F.text)
async def process_code(message: Message, state: FSMContext):
    chat_id = message.chat.id
    code = message.text.strip().replace(" ", "")

    if not code.isdigit() or len(code) < 4:
        await message.answer("❌ Code must be numeric (usually 5-6 digits). Please try again.")
        return

    data = await state.get_data()
    phone = data.get("phone")
    phone_code_hash = data.get("phone_code_hash")
    client = login_clients.get(chat_id)

    if not client or not phone or not phone_code_hash:
        await message.answer("❌ Login session expired. Please start over with /login")
        await state.clear()
        if chat_id in login_clients:
            try:
                await login_clients[chat_id].disconnect()
            except: pass
            del login_clients[chat_id]
        return

    try:
        await client.sign_in(
            phone=phone,
            code=code,
            phone_code_hash=phone_code_hash
        )

        # Success! No 2FA
        session_str = client.session.save()
        await save_user(chat_id, phone, session_str)

        # Cleanup temp client
        await client.disconnect()
        if chat_id in login_clients:
            del login_clients[chat_id]

        await state.clear()
        await message.answer(
            "✅ Login successful!\n\n"
            "You are now authorized. Use:\n"
            "/get_contacts - to export your contacts as JSON\n"
            "/send - to broadcast messages to groups with delay"
        )

    except SessionPasswordNeededError:
        # 2FA required
        await state.set_state(LoginStates.waiting_password)
        await message.answer(
            "🔐 Your account has Two-Factor Authentication (2FA) enabled.\n\n"
            "Please enter your cloud password (the 2FA password you set in Telegram settings).\n"
            "⚠️ This is NOT your SMS code."
        )
    except PhoneCodeInvalidError:
        await message.answer("❌ Invalid code. Please enter the correct code or use /cancel to restart.")
    except PhoneCodeExpiredError:
        await message.answer("❌ Code expired. Please use /cancel and start /login again to get a new code.")
    except Exception as e:
        await message.answer(f"❌ Login error: {str(e)}")
        logger.error(f"Login code error for {chat_id}: {e}")
        # Cleanup on error
        if chat_id in login_clients:
            try:
                await login_clients[chat_id].disconnect()
            except: pass
            del login_clients[chat_id]
        await state.clear()

@dp.message(LoginStates.waiting_password, F.text)
async def process_password(message: Message, state: FSMContext):
    chat_id = message.chat.id
    password = message.text.strip()

    if not password:
        await message.answer("❌ Password cannot be empty.")
        return

    data = await state.get_data()
    phone = data.get("phone")
    client = login_clients.get(chat_id)

    if not client or not phone:
        await message.answer("❌ Login session expired. Please /login again.")
        await state.clear()
        if chat_id in login_clients:
            try:
                await login_clients[chat_id].disconnect()
            except: pass
            del login_clients[chat_id]
        return

    try:
        await client.sign_in(password=password)

        # Success with 2FA
        session_str = client.session.save()
        await save_user(chat_id, phone, session_str)

        await client.disconnect()
        if chat_id in login_clients:
            del login_clients[chat_id]

        await state.clear()
        await message.answer(
            "✅ Login successful with 2FA!\n\n"
            "Your account is now linked. Use /get_contacts or /send."
        )

    except PasswordHashInvalidError:
        await message.answer("❌ Incorrect 2FA password. Please try again.")
    except Exception as e:
        await message.answer(f"❌ 2FA error: {str(e)}")
        logger.error(f"Login password error for {chat_id}: {e}")
        if chat_id in login_clients:
            try:
                await login_clients[chat_id].disconnect()
            except: pass
            del login_clients[chat_id]
        await state.clear()

# ==================== CONTACTS ====================

@dp.message(Command("get_contacts"))
async def cmd_get_contacts(message: Message):
    chat_id = message.chat.id
    client = await get_authorized_client(chat_id)

    if not client:
        await message.answer("❌ You are not logged in or session is invalid. Use /login first.")
        return

    try:
        await message.answer("⏳ Fetching your contacts... This may take a moment.")

        numbers = await extract_contacts_numbers(client)
        await client.disconnect()

        if not numbers:
            await message.answer("No contacts with phone numbers found (or your contact list is empty/sync issues).")
            return

        data = {
            "extracted_at": datetime.now().isoformat(),
            "total_contacts_with_phone": len(numbers),
            "numbers": numbers
        }
        json_str = json.dumps(data, indent=2, ensure_ascii=False)

        if len(json_str) < 3500:
            # Send as code block
            await message.answer(
                f"📋 <b>Your contacts (phone numbers only, no names):</b>\n\n"
                f"<pre><code class=\"json\">{json_str}</code></pre>\n\n"
                f"Total: {len(numbers)} numbers",
                parse_mode="HTML"
            )
        else:
            # Send as file
            document = BufferedInputFile(
                json_str.encode("utf-8"),
                filename="contacts_numbers.json"
            )
            await message.answer_document(
                document,
                caption=f"📋 Contacts phone numbers JSON (only numbers, no names). Total: {len(numbers)}"
            )

    except FloodWaitError as e:
        await message.answer(f"⏳ Flood wait error. Please wait {e.seconds} seconds and try again.")
        try:
            await client.disconnect()
        except: pass
    except Exception as e:
        await message.answer(f"❌ Error fetching contacts: {str(e)}")
        logger.error(f"get_contacts error for {chat_id}: {e}")
        try:
            await client.disconnect()
        except: pass

# ==================== SEND TO GROUPS ====================

@dp.message(Command("send"))
async def cmd_send(message: Message, state: FSMContext):
    chat_id = message.chat.id
    _, session = await get_user_session(chat_id)
    if not session:
        await message.answer("❌ Please /login first to use sending features.")
        return

    if chat_id in active_broadcasts:
        await message.answer(
            "⚠️ You already have a **continuous broadcast** running.\n"
            "Use /stop to stop the current one before starting a new broadcast."
        )
        return

    await state.set_state(SendStates.waiting_groups)
    await message.answer(
        "📤 <b>Continuous Broadcast Setup</b>\n\n"
        "This will **repeatedly** send the same message to your groups in a loop (with delay) "
        "until you stop it with /stop.\n\n"
        "Send the list of target groups/chats separated by commas.\n\n"
        "Examples:\n"
        "• @mygroup1,@mygroup2\n"
        "• @supergroup,-1001234567890\n"
        "• -1001234567890,-1009876543210\n\n"
        "You must be a member of the groups.\n"
        "Use /cancel to abort setup.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )

@dp.message(SendStates.waiting_groups, F.text)
async def process_groups(message: Message, state: FSMContext):
    groups_str = message.text.strip()
    raw_groups = [g.strip() for g in groups_str.split(",") if g.strip()]

    if not raw_groups:
        await message.answer("❌ No valid groups provided. Please try again or /cancel.")
        return

    # Normalize numeric IDs (e.g. -1003632603459) to int — this helps Telethon resolve entities reliably
    groups = []
    for g in raw_groups:
        stripped = g.lstrip("-")
        if stripped.isdigit():
            groups.append(int(g))
        else:
            groups.append(g)

    await state.update_data(groups=groups, raw_groups=raw_groups)
    await state.set_state(SendStates.waiting_message)
    await message.answer(
        f"✅ Groups received: {', '.join(raw_groups)}\n\n"
        "Now send the <b>exact message</b> you want to send to all these groups.\n"
        "(You can use Markdown if desired, but plain text is safest.)"
    )

@dp.message(SendStates.waiting_message, F.text)
async def process_message(message: Message, state: FSMContext):
    msg_text = message.text.strip()
    if not msg_text:
        await message.answer("❌ Message cannot be empty.")
        return

    await state.update_data(message=msg_text)
    await state.set_state(SendStates.waiting_delay)
    await message.answer(
        "✅ Message received.\n\n"
        "Now enter the <b>delay in seconds</b> between sending to each group.\n"
        "Recommended: 3 to 15 seconds (to avoid Telegram limits and look natural).\n"
        "Enter 0 for no delay (not recommended for many groups).\n"
        "Example: 5"
    )

@dp.message(SendStates.waiting_delay, F.text)
async def process_delay(message: Message, state: FSMContext):
    chat_id = message.chat.id
    try:
        delay = float(message.text.strip())
        if delay < 0:
            raise ValueError("Delay must be >= 0")
    except ValueError:
        await message.answer("❌ Invalid delay. Please enter a non-negative number (e.g. 5).")
        return

    data = await state.get_data()
    groups = data.get("groups", [])
    raw_groups = data.get("raw_groups", groups)  # for display
    msg_text = data.get("message", "")

    await state.clear()

    if not groups or not msg_text:
        await message.answer("❌ Session data lost. Please start over with /send.")
        return

    # Prevent duplicate broadcasts
    if chat_id in active_broadcasts:
        await message.answer("⚠️ A continuous broadcast is already running. Use /stop first.")
        return

    # Start the continuous background task
    task = asyncio.create_task(
        run_continuous_broadcast(chat_id, groups, msg_text, delay)
    )
    active_broadcasts[chat_id] = task

    display = ", ".join(raw_groups) if raw_groups else ", ".join(str(g) for g in groups)
    await message.answer(
        f"🚀 <b>Continuous broadcast STARTED!</b>\n\n"
        f"Groups ({len(groups)}): {display}\n"
        f"Delay between sends: {delay} seconds\n"
        f"Mode: Repeats the message in a loop <b>forever</b> until stopped.\n\n"
        f"✅ Use <b>/stop</b> at any time to halt the broadcast.\n"
        f"Use <b>/status</b> to check if it's running."
    )

# ==================== ERROR HANDLER ====================

@dp.error()
async def error_handler(event, exception):
    logger.error(f"Update {event} caused error: {exception}")
    # Optional: notify user on some errors


# ==================== WEB SERVER FOR RENDER / UPTIME ROBOT ====================

async def health_handler(request):
    """Simple health check endpoint for UptimeRobot and Render."""
    return web.json_response({
        "status": "ok",
        "service": "telegram-user-bot",
        "timestamp": datetime.now().isoformat(),
        "message": "Bot is running. Use /login etc in Telegram."
    })


async def start_web_server():
    """Start a minimal aiohttp web server.
    Render requires the service to listen on $PORT (injected by Render).
    UptimeRobot can ping the public URL to keep the service alive (prevents sleep on free tier).
    """
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ping", health_handler)  # Extra friendly endpoint for UptimeRobot

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info(f"🌐 Web server started on http://0.0.0.0:{port} (endpoints: /, /health, /ping)")
    logger.info("   → Use this public URL (e.g. https://your-app.onrender.com) with UptimeRobot.")


async def main():
    await init_db()

    # Start the web server in the background (non-blocking)
    web_task = asyncio.create_task(start_web_server())

    logger.info("🤖 Starting Telegram bot polling (long polling)...")
    logger.info("   The bot will stay awake thanks to the web endpoint + UptimeRobot pings.")

    try:
        # This runs forever (blocks until shutdown)
        await dp.start_polling(bot, allowed_updates=["message"])
    finally:
        # Cleanup if polling stops
        web_task.cancel()
        try:
            await web_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user (KeyboardInterrupt).")
        logger.info("Bot stopped by user.")
    except Exception as e:
        print(f"❌ FATAL ERROR during startup or runtime: {e}")
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
