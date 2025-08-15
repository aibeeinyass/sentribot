from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = "8347456458:AAGJxKK9VyqrepeG4NTJ-aIParSi_oYsphI"

# /start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to SentriBot!\n"
        "I track members, activity, and can give insights into your community."
    )

# Main code
if __name__ == "__main__":
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.run_polling()
