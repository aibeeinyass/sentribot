# bot.py
import os
from telegram.ext import ApplicationBuilder

from moderation import register_moderation   # moderation + help menu + spam + joins/leaves
from buy_tracker import register_buytracker          # your existing module
from sell_tracker import register_selltracker        # your existing module
from x_alert import register_x_alert                 # your existing module

TOKEN = os.getenv("BOT_TOKEN")

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN env var is missing")
    app = ApplicationBuilder().token(TOKEN).build()

    # Core moderation/features
    register_moderation(app)

    # Trackers (plug-in style)
    register_buytracker(app)
    register_selltracker(app)
    register_x_alert(app)

    app.run_polling()

if __name__ == "__main__":
    main()
