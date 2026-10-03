import os
import sqlite3
import asyncio
import logging
import subprocess
import httpx
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------
# Logging & Configuration
# ---------------------------------------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = "8751864548:AAFhkXymbSEgyfT_F20L0u_WD1PzyA9bCR0"
ADMIN_ID = 8861377143
LOG_CHANNEL_ID = -1004291729847

# States for token entry conversation
WAITING_TOKEN = 1

# Shared runtime state
SETTINGS = {
    "pocket_fm_token": None
}

# Temporary cache for pending updates
PENDING_CODE_UPDATES = {}

# ---------------------------------------------------------
# Render Health Check Server
# ---------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        # Silence default HTTP server logging to keep logs clean
        return

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# ---------------------------------------------------------
# Database Setup
# ---------------------------------------------------------
def init_db():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT,
            story_title TEXT,
            episode_no INTEGER,
            episode_title TEXT,
            file_id TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ---------------------------------------------------------
# 1. /start Command & Story Navigation
# ---------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT story_id, story_title FROM episodes")
    stories = cursor.fetchall()
    conn.close()

    keyboard = []
    for story_id, story_title in stories:
        keyboard.append([InlineKeyboardButton(story_title, callback_data=f"story:{story_id}")])

    if user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Control Panel", callback_data="open_admin")])

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    msg_text = "🎧 **Select a Pocket FM Story to listen:**" if stories else "🎧 **Welcome!** No stories synced yet. Use `/admin` to setup."

    await update.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")

# ---------------------------------------------------------
# 2. Interactive Button Handler
# ---------------------------------------------------------
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Open Admin Menu
    if data == "open_admin":
        if query.from_user.id != ADMIN_ID:
            return
        status = "✅ Connected" if SETTINGS["pocket_fm_token"] else "❌ Not Connected"
        admin_kb = [
            [InlineKeyboardButton("🔑 Set Pocket FM Token", callback_data="set_token")],
            [InlineKeyboardButton("🔄 Sync Stories Now", callback_data="run_sync")],
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")]
        ]
        await query.edit_message_text(
            f"⚙️ **Admin Control Panel**\n\nStatus: {status}\nLog Channel: `{LOG_CHANNEL_ID}`",
            reply_markup=InlineKeyboardMarkup(admin_kb),
            parse_mode="Markdown"
        )

    # Show Episodes for Selected Story
    elif data.startswith("story:"):
        story_id = data.split(":")[1]
        conn = sqlite3.connect("bot_data.db")
        cursor = conn.cursor()
        cursor.execute("SELECT episode_no, episode_title, file_id FROM episodes WHERE story_id=?", (story_id,))
        episodes = cursor.fetchall()
        conn.close()

        keyboard = []
        for ep_no, ep_title, file_id in episodes:
            keyboard.append([InlineKeyboardButton(f"Ep {ep_no}: {ep_title}", callback_data=f"play:{file_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")])

        await query.edit_message_text("📖 **Select an episode:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # Send Audio File
    elif data.startswith("play:"):
        file_id = data.split(":")[1]
        await query.message.reply_audio(audio=file_id)

    # Return to Main Menu
    elif data == "main_menu":
        conn = sqlite3.connect("bot_data.db")
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT story_id, story_title FROM episodes")
        stories = cursor.fetchall()
        conn.close()

        keyboard = [[InlineKeyboardButton(st[1], callback_data=f"story:{st[0]}")] for st in stories]
        if query.from_user.id == ADMIN_ID:
            keyboard.append([InlineKeyboardButton("⚙️ Admin Control Panel", callback_data="open_admin")])

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        msg_text = "🎧 **Select a Pocket FM Story to listen:**" if stories else "🎧 **Welcome!** No stories synced yet."
        await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")

# ---------------------------------------------------------
# 3. Admin Command & Manual Token Input
# ---------------------------------------------------------
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    status = "✅ Connected" if SETTINGS["pocket_fm_token"] else "❌ Not Connected"
    keyboard = [
        [InlineKeyboardButton("🔑 Set Pocket FM Token", callback_data="set_token")],
        [InlineKeyboardButton("🔄 Sync Stories Now", callback_data="run_sync")]
    ]
    await update.message.reply_text(
        f"⚙️ **Admin Control Panel**\n\nStatus: {status}\nLog Channel: `{LOG_CHANNEL_ID}`\n\n💡 *Tip: Send a `.py` file here to trigger code updates.*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def set_token_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Paste your **Pocket FM Authorization Bearer Token** below:\n\nSend /cancel to exit.")
    return WAITING_TOKEN

async def save_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    SETTINGS["pocket_fm_token"] = update.message.text.strip()
    await update.message.reply_text("✅ Pocket FM token saved successfully! Use /admin to sync stories.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Action canceled.")
    return ConversationHandler.END

# ---------------------------------------------------------
# 4. Remote Code Update via Button
# ---------------------------------------------------------
async def handle_code_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    doc = update.message.document
    if not doc.file_name.endswith(".py"):
        await update.message.reply_text("⚠️ Please upload a valid `.py` script.")
        return

    PENDING_CODE_UPDATES[user_id] = doc.file_id

    keyboard = [
        [InlineKeyboardButton("🔄 Apply Code Update & Restart", callback_data="apply_code_update")],
        [InlineKeyboardButton("❌ Cancel Update", callback_data="cancel_code_update")]
    ]

    await update.message.reply_text(
        f"📦 **New Code File Received:** `{doc.file_name}`\n\nClick the button below to apply this update and restart:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_update_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if user_id != ADMIN_ID:
        return

    data = query.data

    if data == "apply_code_update":
        file_id = PENDING_CODE_UPDATES.get(user_id)
        if not file_id:
            await query.edit_message_text("❌ Update session expired. Upload your `.py` file again.")
            return

        await query.edit_message_text("⏳ Downloading new script and updating target file...")

        try:
            tg_file = await context.bot.get_file(file_id)
            target_filename = os.path.basename(__file__) if __file__ else "main.py"
            
            await tg_file.download_to_drive(target_filename)

            await context.bot.send_message(
                chat_id=user_id,
                text="✅ **Code Updated Successfully!**\n\nRestarting process...",
                parse_mode="Markdown"
            )

            PENDING_CODE_UPDATES.pop(user_id, None)

            # Terminate current process; Render will automatically restart the web service
            os._exit(0)

        except Exception as e:
            logger.error(f"Failed to apply code update: {e}")
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ **Update Failed:** `{e}`",
                parse_mode="Markdown"
            )

    elif data == "cancel_code_update":
        PENDING_CODE_UPDATES.pop(user_id, None)
        await query.edit_message_text("❌ Code update canceled.")

# ---------------------------------------------------------
# 5. Sync Background Pipeline
# ---------------------------------------------------------
async def run_sync_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not SETTINGS["pocket_fm_token"]:
        await query.edit_message_text("❌ No token saved! Set your Pocket FM token first.")
        return

    await query.edit_message_text("⏳ Sync processing... Uploading episodes to storage channel.")
    asyncio.create_task(process_sync(context))

async def process_sync(context: ContextTypes.DEFAULT_TYPE):
    mock_episodes = [
        {
            "story_id": "secret_millionaire",
            "story_title": "Secret Millionaire",
            "ep_no": 1,
            "ep_title": "The Encounter",
            "url": "https://example.com/stream1.m3u8"
        }
    ]

    for ep in mock_episodes:
        temp_file = "temp_episode.mp3"
        try:
            conn = sqlite3.connect("bot_data.db")
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM episodes WHERE story_id=? AND episode_no=?", (ep["story_id"], ep["ep_no"]))
            exists = cursor.fetchone()
            conn.close()

            if exists:
                continue

            cmd = ["ffmpeg", "-y", "-i", ep["url"], "-acodec", "libmp3lame", "-ab", "128k", temp_file]
            subprocess.run(cmd, check=True)

            with open(temp_file, "rb") as audio:
                sent = await context.bot.send_audio(
                    chat_id=LOG_CHANNEL_ID,
                    audio=audio,
                    caption=f"{ep['story_title']} - Ep {ep['ep_no']}: {ep['ep_title']}"
                )
            
            conn = sqlite3.connect("bot_data.db")
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO episodes (story_id, story_title, episode_no, episode_title, file_id)
                VALUES (?, ?, ?, ?, ?)
            """, (ep["story_id"], ep["story_title"], ep["ep_no"], ep["ep_title"], sent.audio.file_id))
            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Error processing {ep['ep_title']}: {e}")
        finally:
            if os.path.exists(temp_file):
                os.remove(temp_file)

    await context.bot.send_message(chat_id=ADMIN_ID, text="🎉 **Sync complete!** All new stories are saved to the bot database.")

# ---------------------------------------------------------
# Main Execution Loop
# ---------------------------------------------------------
async def main():
    # Start background HTTP server for Render health checks
    threading.Thread(target=run_health_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    token_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_token_start, pattern="^set_token$")],
        states={
            WAITING_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_token)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(token_conv)
    
    # Handlers for Code Updates
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_code_upload))
    app.add_handler(CallbackQueryHandler(handle_update_button, pattern="^(apply_code_update|cancel_code_update)$"))

    app.add_handler(CallbackQueryHandler(run_sync_callback, pattern="^run_sync$"))
    app.add_handler(CallbackQueryHandler(handle_buttons))

    logger.info("Initializing bot and starting polling...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
