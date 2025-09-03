import os
from datetime import datetime

from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup  # <-- added buttons
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    CallbackQueryHandler,  # <-- added callback handler
    ContextTypes,
    filters,
)

from buy_tracker import register_buytracker   # <-- NEW
from sell_tracker import register_selltracker
from x_alert import register_x_alert

TOKEN = os.getenv("BOT_TOKEN")

# -------- SETTINGS --------
welcome_message = "🎉 Welcome {name} to the group! Please read the rules."
warn_limit = 3
warnings = {}  # Store warnings per user

# NEW: per-chat welcome storage + default
welcome_messages = {}
DEFAULT_WELCOME = welcome_message


# -------- HELP MENU (button-based) --------
def _render_help_section(section: str) -> tuple[str, InlineKeyboardMarkup]:
    section = (section or "menu").lower()

    # Buttons row
    rows = [
        [
            InlineKeyboardButton("👋 General", callback_data="help:general"),
            InlineKeyboardButton("🟢 Buy", callback_data="help:buy"),
        ],
        [
            InlineKeyboardButton("🔴 Sell", callback_data="help:sell"),
            InlineKeyboardButton("🐦 X Alerts", callback_data="help:x"),
        ],
    ]
    if section != "menu":
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="help:menu")])

    kb = InlineKeyboardMarkup(rows)

    if section == "general":
        text = (
            "✨ *SentriBot — General*\n\n"
            "• /start — Greet the bot\n"
            "• /rules — Show rules\n"
            "• /about — About the bot\n"
            "• /setwelcome <message> — Change welcome text\n"
            "• /warn — Warn a user (reply)\n"
            "• /pin — Pin the latest message\n"
        )
    elif section == "buy":
        text = (
            "🟢 *Buy Tracker*\n\n"
            "• /track <mint> — Start buy tracking\n"
            "• /untrack <mint> — Stop buy tracking\n"
            "• /list — List tracked tokens\n"
            "• /skip <txsig> — Ignore a transaction\n"
        )
    elif section == "sell":
        text = (
            "🔴 *Sell Tracker*\n\n"
            "• /track_sell <mint> — Start sell tracking\n"
            "• /sell_skip — Skip media for last /track_sell\n"
            "• /untrack_sell <mint> — Stop sell tracking\n"
            "• /list_sells — List tracked tokens (with whale threshold)\n"
            "• /sellthreshold <mint> <usd> — Set whale alert threshold\n"
        )
    elif section == "x":
        text = (
            "🐦 *X Alerts*\n\n"
            "• /x_track <handle> — Track new followers for an account\n"
            "• /x_untrack <handle> — Stop tracking\n"
            "• /x_list — List tracked X accounts\n"
            "• /x_debug — Check X API token status\n"
            "• /x_testuser <handle> — Test lookup (debug)\n\n"
            "_Followers are checked every 2 minutes._"
        )
    else:
        # Menu intro
        text = (
            "✨ *SentriBot Help*\n"
            "Tap a category below to see commands."
        )

    return text, kb

async def help_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, section = (q.data.split(":", 1) + ["menu"])[:2]
    text, kb = _render_help_section(section)
    try:
        await q.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await q.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


# -------- COMMAND HANDLERS --------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hello! I’m SentriBot — keeping your group safe and fun!")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Show button menu (supports '/help sell' to open a tab directly)
    section = (context.args[0] if context.args else "menu")
    text, kb = _render_help_section(section)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📜 Group Rules:\n"
        "1. Be respectful\n"
        "2. No spam or ads\n"
        "3. Keep chats friendly"
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Welcome to SentriBot* — Your private community monitoring and insights assistant\\.\n\n"
        "📊 *With SentriBot, you can:*\n"
        "• Track member activity and engagement\\.\n"
        "• Get alerts when members join or leave\\.\n"
        "• Monitor keywords and detect mood changes in chats\\.\n"
        "• Watch for mentions of your token or ticker on X\\.\n"
        "• Receive blockchain whale and wallet activity alerts\\.\n"
        "• Get notified when someone follows your X account\\.\n\n"
        "🔒 *You control all data\\. SentriBot is private and built for your project*\\.",
        parse_mode="MarkdownV2"
    )


# -------- ADMIN COMMANDS --------
async def set_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global welcome_message
    if context.args:
        # keep original global for backwards compat, but store per-chat override
        welcome_message = " ".join(context.args)
        # normalize {Name} -> {name}
        normalized = welcome_message.replace("{Name}", "{name}")
        chat_id = update.effective_chat.id
        welcome_messages[chat_id] = normalized
        await update.message.reply_text("✅ Welcome message updated!")
    else:
        await update.message.reply_text("❌ Usage: /setwelcome <message>")


async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Reply to a user's message to warn them.")
        return

    user = update.message.reply_to_message.from_user
    uid = user.id
    warnings[uid] = warnings.get(uid, 0) + 1
    await update.message.reply_text(f"⚠ {user.first_name} has been warned! ({warnings[uid]}/{warn_limit})")
    if warnings[uid] >= warn_limit:
        await update.message.chat.ban_member(uid)
        await update.message.reply_text(f"🚫 {user.first_name} was banned after too many warnings.")


async def pin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        await update.message.reply_to_message.pin()
        await update.message.reply_text("📌 Message pinned!")
    else:
        await update.message.reply_text("❌ Reply to a message to pin it.")


# -------- AUTO FEATURES (message-based) --------
async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    message_template = welcome_messages.get(chat_id, DEFAULT_WELCOME)

    for member in update.message.new_chat_members:
        if member.is_bot:
            await update.message.chat.ban_member(member.id)
            await update.message.reply_text(f"🤖 Bot {member.first_name} was removed.")
            return
        await update.message.reply_text(
            message_template.format(name=member.mention_html()),
            parse_mode="HTML"
        )
        await log_activity(f"User joined: {member.full_name}")


async def goodbye(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.left_chat_member:
        await update.message.reply_text(f"👋 Goodbye {update.message.left_chat_member.full_name}!")
        await log_activity(f"User left: {update.message.left_chat_member.full_name}")


# -------- ChatMember updates (minimal addition to catch all joins/leaves) --------
def _status_change(old, new):
    try:
        return (old.status != new.status) or (old.is_member != new.is_member)
    except Exception:
        return True


async def user_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu:
        return
    old, new = cmu.old_chat_member, cmu.new_chat_member
    if not _status_change(old, new):
        return

    joined = (not getattr(old, "is_member", False) and getattr(new, "is_member", False)) or (
        old.status in ("left", "kicked") and new.status in ("member", "administrator", "creator")
    )
    left = (getattr(old, "is_member", False) and not getattr(new, "is_member", False)) or (
        new.status in ("left", "kicked")
    )

    if joined:
        chat_id = cmu.chat.id
        message_template = welcome_messages.get(chat_id, DEFAULT_WELCOME)

        user = cmu.from_user
        if user.is_bot:
            await context.bot.ban_chat_member(cmu.chat.id, user.id)
            await context.bot.send_message(cmu.chat.id, f"🤖 Bot {user.first_name} was removed.")
        else:
            await context.bot.send_message(
                cmu.chat.id,
                message_template.format(name=user.mention_html()),
                parse_mode="HTML",
            )
            await log_activity(f"User joined: {user.full_name}")

    elif left:
        user = cmu.from_user
        await context.bot.send_message(cmu.chat.id, f"👋 Goodbye {user.full_name}!")
        await log_activity(f"User left: {user.full_name}")


# -------- SPAM DETECTION --------
SPAM_KEYWORDS = os.getenv("SPAM_KEYWORDS", "")
SPAM_KEYWORDS = [word.strip().lower() for word in SPAM_KEYWORDS.split(",") if word.strip()]


async def detect_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text and any(
        word in update.message.text.lower() for word in SPAM_KEYWORDS
    ):
        user_name = update.message.from_user.first_name
        chat_id = update.effective_chat.id
        msg_id = update.message.message_id

        # Delete the spam message
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)

        # Notify the group
        await context.bot.send_message(chat_id=chat_id, text=f"🚫 Spam detected from {user_name}")

        # Warn the user
        await warn_user(update, context)


# -------- LOGGING --------
async def log_activity(text):
    with open("activity.log", "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now()}] {text}\n")


# -------- MAIN --------
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("setwelcome", set_welcome))
    app.add_handler(CommandHandler("warn", warn_user))
    app.add_handler(CommandHandler("pin", pin_message))

    # Auto actions (message-based)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, goodbye))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, detect_spam))

    # Also catch join/leave delivered as ChatMember updates (minimal addition)
    app.add_handler(ChatMemberHandler(user_member_update, ChatMemberHandler.CHAT_MEMBER))
    # If you also care about updates to the bot itself, uncomment:
    # app.add_handler(ChatMemberHandler(user_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

    # -------- BUY/SELL TRACKERS --------
    register_buytracker(app)   # <-- plug in all /track, /untrack, /list etc.
    register_selltracker(app)
    register_x_alert(app)

    # -------- HELP menu callback (must be registered) --------
    app.add_handler(CallbackQueryHandler(help_menu_cb, pattern=r"^help:"))

    app.run_polling()


if __name__ == "__main__":
    main()
