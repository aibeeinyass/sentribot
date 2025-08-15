import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ChatMemberHandler, ContextTypes, filters

TOKEN = os.getenv("BOT_TOKEN")

# ----------- COMMAND HANDLERS -----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hello! I’m SentriBot — here to keep your group safe and fun!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 Available commands:\n"
        "/start - Greet the bot\n"
        "/help - Show this help menu\n"
        "/rules - Display group rules\n"
        "/about - Learn about this bot"
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
        "🤖 SentriBot was built to welcome members, keep order, and share info.\n"
        "Created by Ibrahim."
    )

# ----------- AUTO WELCOME & GOODBYE -----------

async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        await update.message.reply_text(f"🎉 Welcome {member.mention_html()} to the group!", parse_mode="HTML")

async def goodbye(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.left_chat_member:
        await update.message.reply_text(f"👋 Goodbye {update.message.left_chat_member.full_name}!")

# ----------- MAIN -----------

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("about", about))

    # Welcome / Goodbye
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, goodbye))

    app.run_polling()

if __name__ == "__main__":
    main()
