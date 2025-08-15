import os
from telegram import Update, ChatPermissions
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from datetime import datetime

TOKEN = os.getenv("BOT_TOKEN")

# -------- SETTINGS --------
welcome_message = "🎉 Welcome {name} to the group! Please read the rules."
warn_limit = 3
warnings = {}  # Store warnings per user

# -------- COMMAND HANDLERS --------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hello! I’m SentriBot — keeping your group safe and fun!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 Commands:\n"
        "/start - Greet the bot\n"
        "/help - Show this menu\n"
        "/rules - Show rules\n"
        "/about - About the bot\n"
        "/setwelcome <message> - Change welcome text\n"
        "/warn - Warn a user (reply to a message)\n"
        "/pin - Pin the latest message"
    )

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
        welcome_message = " ".join(context.args)
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

# -------- AUTO FEATURES --------
async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot:
            await update.message.chat.ban_member(member.id)
            await update.message.reply_text(f"🤖 Bot {member.first_name} was removed.")
            return
        await update.message.reply_text(welcome_message.format(name=member.mention_html()), parse_mode="HTML")
        await log_activity(f"User joined: {member.full_name}")

async def goodbye(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.left_chat_member:
        await update.message.reply_text(f"👋 Goodbye {update.message.left_chat_member.full_name}!")
        await log_activity(f"User left: {update.message.left_chat_member.full_name}")

async def detect_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.lower() in ["buy now", "click here", "free money"]:
        # Delete the spam message
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
        # Notify group
        await update.message.reply_text(f"🚫 Spam detected from {update.message.from_user.first_name}")
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

    # Auto actions
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, goodbye))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, detect_spam))

    app.run_polling()

if __name__ == "__main__":
    main()
