import os
import sqlite3
import logging
import aiohttp

from telegram import Update
from telegram.ext import CommandHandler, MessageHandler, ContextTypes, filters

# ---------------- SETTINGS ----------------
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

DB_PATH = "tracked_tokens.db"
logging.basicConfig(level=logging.INFO)

# ---------------- DATABASE ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tracked_tokens (
            chat_id INTEGER,
            mint TEXT,
            media_file_id TEXT,
            PRIMARY KEY (chat_id, mint)
        )
    """)
    conn.commit()
    conn.close()

def add_token(chat_id, mint, media_file_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO tracked_tokens VALUES (?, ?, ?)", (chat_id, mint, media_file_id))
    conn.commit()
    conn.close()

def remove_token(chat_id, mint):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM tracked_tokens WHERE chat_id=? AND mint=?", (chat_id, mint))
    conn.commit()
    conn.close()

def list_tokens(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT mint, media_file_id FROM tracked_tokens WHERE chat_id=?", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows

# ---------------- HELPERS ----------------
def fmt_num(x):
    try:
        n = float(x or 0)
        return f"{n:,.0f}"
    except Exception:
        return str(x)

def fmt_price(x):
    try:
        n = float(x or 0)
        return f"{n:.8f}" if n < 1 else f"{n:.4f}"
    except Exception:
        return str(x)

def short_wallet(addr: str) -> str:
    if not addr or len(addr) < 8:
        return addr or "?"
    return addr[:4] + "..." + addr[-4:]

# ---------------- BUYTRACKER LOGIC ----------------
pending_media = {}
last_seen = {}

async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /track <mint>")
        return
    mint = context.args[0].strip()
    chat_id = update.effective_chat.id
    pending_media[chat_id] = mint
    add_token(chat_id, mint, None)
    await update.message.reply_text(f"✅ Tracking {mint}. Send an image now or /skip.")

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in pending_media:
        del pending_media[chat_id]
        await update.message.reply_text("✅ Skipped media.")
    else:
        await update.message.reply_text("❌ No pending token.")

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in pending_media:
        return
    mint = pending_media.pop(chat_id)
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        file_id = update.message.document.file_id
    else:
        return
    add_token(chat_id, mint, file_id)
    await update.message.reply_text(f"📸 Media saved for {mint}.")

async def cmd_untrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /untrack <mint>")
        return
    mint = context.args[0].strip()
    chat_id = update.effective_chat.id
    remove_token(chat_id, mint)
    await update.message.reply_text(f"🗑 Stopped tracking {mint}.")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tokens = list_tokens(chat_id)
    if not tokens:
        await update.message.reply_text("No tokens tracked.")
    else:
        msg = "📋 Tracked tokens: " + " ".join([mint for mint, _ in tokens])
        await update.message.reply_text(msg)

# ---------------- EXTERNAL APIS ----------------
HELIUS_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
BIRDEYE_TOKEN_URL = "https://public-api.birdeye.so/defi/token/{}"

async def fetch_transactions(mint: str, limit: int = 5):
    payload = {
        "jsonrpc": "2.0",
        "id": "tx_fetch",
        "method": "getSignaturesForAddress",
        "params": [mint, {"limit": limit}],
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(HELIUS_URL, json=payload) as resp:
            data = await resp.json()
            return data.get("result", [])

async def get_transaction(signature: str):
    payload = {
        "jsonrpc": "2.0",
        "id": "tx_parse",
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed"}],
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(HELIUS_URL, json=payload) as resp:
            return (await resp.json()).get("result")

async def parse_buy(signature: str, mint: str):
    tx = await get_transaction(signature)
    if not tx:
        return None

    meta = tx.get("meta", {})
    pre = meta.get("preTokenBalances", []) or []
    post = meta.get("postTokenBalances", []) or []

    def amt(tb):
        u = tb.get("uiTokenAmount", {})
        return int(u.get("amount", 0)), int(u.get("decimals", 0) or 0)

    pre_map = {}
    for b in pre:
        if b.get("mint") == mint:
            a, d = amt(b)
            pre_map[(b.get("owner"), mint)] = (a, d)

    for b in post:
        if b.get("mint") != mint:
            continue
        a_post, dec = amt(b)
        a_pre, _ = pre_map.get((b.get("owner"), mint), (0, dec))
        delta = a_post - a_pre
        if delta > 0:
            tokens = delta / (10 ** max(dec, 0))
            return {"buyer": b.get("owner"), "amount": tokens, "decimals": dec}
    return None

async def fetch_token_info(mint: str):
    if not BIRDEYE_API_KEY:
        return {}
    headers = {"x-api-key": BIRDEYE_API_KEY}
    url = BIRDEYE_TOKEN_URL.format(mint)
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if data.get("success"):
                return data.get("data", {})
    return {}

async def poll_tracked(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id, mint, media_file_id FROM tracked_tokens")
    rows = c.fetchall()
    conn.close()

    for chat_id, mint, media_file_id in rows:
        try:
            txs = await fetch_transactions(mint, limit=5)
            if not txs:
                continue
            sig = txs[0]["signature"]
            if last_seen.get(mint) == sig:
                continue
            last_seen[mint] = sig

            token_info = await fetch_token_info(mint)
            symbol = token_info.get("symbol", "TOKEN")
            price = float(token_info.get("price", 0) or 0)
            mcap = float(token_info.get("mc", 0) or 0)

            details = await parse_buy(sig, mint)
            if details:
                amount = float(details.get("amount", 0))
                buyer = details.get("buyer")
                usd_value = amount * price if price else 0
                text = (
                    f"🔥 Buy Detected!\n\n"
                    f"Token: {symbol}\n"
                    f"Mint: {mint}\n"
                    f"Amount Bought: {fmt_num(amount)}\n"
                    f"Value: ${fmt_num(usd_value)}\n"
                    f"Price: ${fmt_price(price)}\n"
                    f"Market Cap: ${fmt_num(mcap)}\n"
                    f"Buyer: {short_wallet(buyer)}\n"
                    f"Tx: https://solscan.io/tx/{sig}"
                )
            else:
                text = (
                    f"🔥 Buy Detected!\n\n"
                    f"Token: {symbol}\n"
                    f"Mint: {mint}\n"
                    f"Price: ${fmt_price(price)}\n"
                    f"Market Cap: ${fmt_num(mcap)}\n"
                    f"Tx: https://solscan.io/tx/{sig}"
                )

            if media_file_id:
                await context.bot.send_photo(chat_id, photo=media_file_id, caption=text)
            else:
                await context.bot.send_message(chat_id, text)

        except Exception as e:
            logging.error(f"Error polling {mint}: {e}")

# ---------------- REGISTRATION FUNCTION ----------------
def register_buytracker(app):
    init_db()
    app.add_handler(CommandHandler("track", cmd_track))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("untrack", cmd_untrack))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_media))
    app.job_queue.run_repeating(poll_tracked, interval=30, first=5)
