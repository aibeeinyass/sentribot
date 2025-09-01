import os
import sqlite3
import logging
import aiohttp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, MessageHandler, ContextTypes, filters

# ---------------- SETTINGS ----------------
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")

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
        # show 2 decimals for small money amounts, otherwise no decimals
        if n < 1000:
            return f"{n:,.2f}"
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
        # Prettier list: show a bullet with symbol [short_mint]
        # We’ll fetch names/symbols quickly (best-effort, not blocking hard)
        lines = []
        for mint, _ in tokens:
            sym, name = mint[-4:], None
            try:
                info = await quick_token_identity(mint)
                sym = info.get("symbol") or sym
                name = info.get("name")
            except Exception:
                pass
            short_mint = mint[:4] + "..." + mint[-4:]
            label = f"{(name or sym)} [{sym}] — `{short_mint}`"
            lines.append(f"• {label}")
        await update.message.reply_text("📋 Tracked tokens:\n" + "\n".join(lines), parse_mode=None)

# ---------------- EXTERNAL APIS ----------------
HELIUS_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/solana/contract/{}"
PUMPFUN_URL = "https://api.pump.fun/v1/token/{}"  # placeholder

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
            data = await resp.json()
            return data.get("result")

async def parse_buy(signature: str, mint: str):
    tx = await get_transaction(signature)
    if not tx:
        return None

    meta = tx.get("meta", {}) or {}
    pre = meta.get("preTokenBalances", []) or []
    post = meta.get("postTokenBalances", []) or []

    def amt(tb):
        u = tb.get("uiTokenAmount", {}) or {}
        return int(u.get("amount", 0) or 0), int(u.get("decimals", 0) or 0)

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

# -------- quick identity for /list (best-effort, fast) --------
async def quick_token_identity(mint: str):
    # Try DexScreener only (fast and free). Return {symbol, name} minimal.
    async with aiohttp.ClientSession() as session:
        async with session.get(DEXSCREENER_URL.format(mint)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            pairs = data.get("pairs", []) or []
            if not pairs:
                return {}
            bt = pairs[0].get("baseToken", {}) or {}
            return {"symbol": bt.get("symbol"), "name": bt.get("name")}

# ---------------- FETCH TOKEN INFO ----------------
async def fetch_token_info(mint: str):
    # 1) Pump.fun (pre-liquidity tokens)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(PUMPFUN_URL.format(mint)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    d = data.get("data", {}) or {}
                    if d:
                        return {
                            "symbol": d.get("symbol", "TOKEN"),
                            "name": d.get("name") or d.get("symbol") or "TOKEN",
                            "price": float(d.get("price", 0) or 0),
                            "mc": float(d.get("marketCap", 0) or 0)
                        }
    except Exception:
        pass

    # 2) DexScreener (liquidity/live pairs)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(DEXSCREENER_URL.format(mint)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", []) or []
                    if pairs:
                        pair = pairs[0]
                        base = pair.get("baseToken", {}) or {}
                        return {
                            "symbol": base.get("symbol", "TOKEN"),
                            "name": base.get("name") or base.get("symbol") or "TOKEN",
                            "price": float(pair.get("priceUsd", 0) or 0),
                            # DexScreener doesn't return MC directly; using liquidityUsd as a proxy
                            "mc": float(pair.get("liquidityUsd", 0) or 0)
                        }
    except Exception:
        pass

    # 3) CoinGecko (established tokens)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(COINGECKO_URL.format(mint)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    market = data.get("market_data", {}) or {}
                    return {
                        "symbol": (data.get("symbol") or "TOKEN").upper(),
                        "name": data.get("name") or data.get("symbol") or "TOKEN",
                        "price": float((market.get("current_price", {}) or {}).get("usd", 0) or 0),
                        "mc": float((market.get("market_cap", {}) or {}).get("usd", 0) or 0),
                    }
    except Exception:
        pass

    return {"symbol": "TOKEN", "name": "TOKEN", "price": 0.0, "mc": 0.0}

# ---------------- POLLING ----------------
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
            sig = txs[0].get("signature")
            if not sig:
                continue
            if last_seen.get(mint) == sig:
                continue
            last_seen[mint] = sig

            token_info = await fetch_token_info(mint)
            symbol = token_info.get("symbol", "TOKEN")
            name = token_info.get("name") or symbol
            price = float(token_info.get("price", 0) or 0)
            mcap = float(token_info.get("mc", 0) or 0)

            details = await parse_buy(sig, mint)
            if not details:
                continue  # only alert on actual positive token deltas

            amount = float(details.get("amount", 0) or 0)
            buyer = details.get("buyer")
            usd_value = amount * price if price else 0.0

            # ---------- Fancy message (template style) ----------
            skulls = "💀" * 14
            title = f"{name} [{symbol}] 💀Buy!"
            short_buyer = short_wallet(buyer)

            text = (
                f"{title}\n\n"
                f"{skulls}\n\n"
                f"💀| {fmt_num(usd_value)} USDC (${fmt_num(usd_value)})\n"
                f"💀| Got: {fmt_num(amount)} {symbol}\n"
                f"💀| Buyer | Txn\n"
                f"💀| Position: New\n"
                f"💀| Market Cap: ${fmt_num(mcap)}"
            )

            # ---------- Buttons ----------
            jup = f"https://jup.ag/swap/SOL-{mint}"
            dexs = f"https://dexscreener.com/solana/{mint}"
            twitter = f"https://x.com/sentrip_bot"
            buyer_url = f"https://solscan.io/account/{buyer}" if buyer else None
            tx_url = f"https://solscan.io/tx/{sig}"
            # Placeholder for "Boost with Odin" – change to your promo/CTA link
            boost_url = "https://t.me/"

            buttons = [
                [
                    InlineKeyboardButton("🐴 Buy", url=jup),
                    InlineKeyboardButton("💀 DexS", url=dexs),
                    InlineKeyboardButton("💀 Twitter", url=twitter),
                    InlineKeyboardButton("⚡️ Boost with Odin", url=boost_url),
                ],
                [
                    InlineKeyboardButton("Buyer", url=buyer_url or tx_url),
                    InlineKeyboardButton("Txn", url=tx_url),
                ],
            ]
            markup = InlineKeyboardMarkup(buttons)

            if media_file_id:
                await context.bot.send_photo(chat_id, photo=media_file_id, caption=text, reply_markup=markup)
            else:
                await context.bot.send_message(chat_id, text, reply_markup=markup)

        except Exception as e:
            logging.error(f"Error polling {mint}: {e}")

def register_buytracker(app):
    init_db()
    app.add_handler(CommandHandler("track", cmd_track))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("untrack", cmd_untrack))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_media))
    app.job_queue.run_repeating(poll_tracked, interval=30, first=5)
