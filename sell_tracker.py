import os
import sqlite3
import logging
import aiohttp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, MessageHandler, ContextTypes, filters

# ---------- SETTINGS ----------
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
DB_PATH = "tracked_tokens.db"
logging.basicConfig(level=logging.INFO)

# Native SOL placeholder (not an SPL mint)
NATIVE_SOL_MINTS = {"So11111111111111111111111111111111111111112"}
def is_native_sol(mint: str) -> bool:
    return mint in NATIVE_SOL_MINTS

# External APIs (same as buy tracker for consistency)
HELIUS_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/solana/contract/{}"
PUMPFUN_URL = "https://api.pump.fun/v1/token/{}"  # best-effort

# ---------- DB ----------
def init_sell_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # table for sell tracking + per-token threshold
    c.execute("""
        CREATE TABLE IF NOT EXISTS sell_tracked (
            chat_id INTEGER,
            mint TEXT,
            media_file_id TEXT,
            symbol TEXT,
            usd_threshold REAL,      -- per-token whale threshold (USD)
            PRIMARY KEY (chat_id, mint)
        )
    """)
    # migrate: add columns if missing
    cols = {row[1] for row in c.execute("PRAGMA table_info(sell_tracked)").fetchall()}
    if "symbol" not in cols:
        try: c.execute("ALTER TABLE sell_tracked ADD COLUMN symbol TEXT")
        except Exception: pass
    if "usd_threshold" not in cols:
        try: c.execute("ALTER TABLE sell_tracked ADD COLUMN usd_threshold REAL")
        except Exception: pass
    conn.commit()
    conn.close()

def sell_add_token(chat_id, mint, media_file_id=None, symbol=None, usd_threshold=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO sell_tracked (chat_id, mint, media_file_id, symbol, usd_threshold) VALUES (?, ?, ?, ?, COALESCE(?, usd_threshold))",
        (chat_id, mint, media_file_id, symbol, usd_threshold),
    )
    conn.commit()
    conn.close()

def sell_update_symbol(mint: str, symbol: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE sell_tracked SET symbol=? WHERE mint=?", (symbol, mint))
    conn.commit()
    conn.close()

def sell_update_threshold(chat_id: int, mint: str, usd_threshold: float):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE sell_tracked SET usd_threshold=? WHERE chat_id=? AND mint=?", (usd_threshold, chat_id, mint))
    conn.commit()
    conn.close()

def sell_remove_token(chat_id, mint):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM sell_tracked WHERE chat_id=? AND mint=?", (chat_id, mint))
    conn.commit()
    conn.close()

def sell_list_rows(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT mint, media_file_id, symbol, usd_threshold FROM sell_tracked WHERE chat_id=?", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows

# ---------- HELPERS ----------
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

def fmt_amount(x):
    try:
        n = float(x or 0)
        if n == 0:
            return "0"
        if n < 1:
            return f"{n:.6f}".rstrip("0").rstrip(".")
        if n < 1000:
            return f"{n:.4f}".rstrip("0").rstrip(".")
        return f"{n:,.2f}"
    except Exception:
        return str(x)

def fmt_usd(x):
    try:
        n = float(x or 0)
        return f"${n:,.2f}"
    except Exception:
        return f"${x}"

def short_wallet(addr: str) -> str:
    if not addr or len(addr) < 8:
        return addr or "?"
    return addr[:4] + "..." + addr[-4:]

def short_mint(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + "…" + mint[-4:]

# ---------- SELL TRACKER STATE ----------
pending_sell_media = {}    # chat_id -> mint (awaiting image)
sell_last_seen = {}        # mint -> last tx sig processed
DEFAULT_WHALE_USD = 1000.0 # fallback threshold if none set per token

# ---------- COMMANDS ----------
async def sell_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /track_sell <mint>")
        return
    mint = context.args[0].strip()
    chat_id = update.effective_chat.id

    if is_native_sol(mint):
        await update.message.reply_text("⚠️ Native SOL isn't an SPL mint. Use a token mint address.")
        return

    symbol = await best_symbol_for_mint(mint)
    pending_sell_media[chat_id] = mint
    sell_add_token(chat_id, mint, None, symbol, None)
    await update.message.reply_text(f"✅ Sell-tracking {mint} ({symbol or 'TOKEN'}). Send an image now or /sell_skip.")

async def sell_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in pending_sell_media:
        del pending_sell_media[chat_id]
        await update.message.reply_text("✅ Skipped media for sell tracker.")
    else:
        await update.message.reply_text("❌ No pending token for sell tracker.")

async def sell_handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in pending_sell_media:
        return
    mint = pending_sell_media.pop(chat_id)
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        file_id = update.message.document.file_id
    else:
        return
    sell_add_token(chat_id, mint, file_id, None, None)
    await update.message.reply_text(f"📸 Media saved for sell tracker: {mint}.")

async def sell_untrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /untrack_sell <mint>")
        return
    mint = context.args[0].strip()
    chat_id = update.effective_chat.id
    sell_remove_token(chat_id, mint)
    await update.message.reply_text(f"🗑 Stopped sell-tracking {mint}.")

async def sell_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = sell_list_rows(chat_id)
    if not rows:
        await update.message.reply_text("No tokens sell-tracked.")
        return
    enriched = []
    for mint, _media, symbol, thr in rows:
        if not symbol:
            symbol = await best_symbol_for_mint(mint)
            if symbol:
                sell_update_symbol(mint, symbol)
        enriched.append((symbol or "TOKEN", mint, thr))

    enriched.sort(key=lambda t: t[0].upper())
    lines = []
    for symbol, mint, thr in enriched:
        link = f"https://solscan.io/token/{mint}"
        ttext = f"{fmt_usd(thr)}" if thr and thr > 0 else f"{fmt_usd(DEFAULT_WHALE_USD)}*"
        lines.append(f"• {symbol} — [{short_mint(mint)}]({link}) — whale: {ttext}")
    await update.message.reply_text(
        "📋 Sell-tracked tokens:\n" + "\n".join(lines) + "\n\n*default if not set",
        disable_web_page_preview=True,
        parse_mode="Markdown"
    )

async def sell_setthreshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Set per-token whale threshold in USD for sell alerts.
    Usage: /sellthreshold <mint> <usd>
    """
    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: /sellthreshold <mint> <usd>")
        return
    mint = context.args[0].strip()
    try:
        usd = float(context.args[1].replace(",", ""))
    except Exception:
        await update.message.reply_text("❌ Invalid USD amount.")
        return
    chat_id = update.effective_chat.id
    sell_update_threshold(chat_id, mint, usd)
    await update.message.reply_text(f"✅ Whale threshold for {short_mint(mint)} set to {fmt_usd(usd)}.")

# ---------- RPC ----------
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

# ---------- SELL PARSER ----------
async def parse_sell(signature: str, mint: str):
    """
    Detect net decrease of token balance for a holder (i.e., a sell).
    Returns {"seller": <address>, "amount": tokens_sold, "decimals": d} or None.
    """
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
        if delta < 0:  # net decrease -> sold
            tokens = (-delta) / (10 ** max(dec, 0))
            return {"seller": b.get("owner"), "amount": tokens, "decimals": dec}
    return None

# ---------- TOKEN INFO ----------
async def fetch_token_info(mint: str):
    # 1) Pump.fun (best effort)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(PUMPFUN_URL.format(mint)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    d = data.get("data") or {}
                    if d:
                        return {
                            "symbol": d.get("symbol", "TOKEN"),
                            "price": float(d.get("price", 0) or 0),
                            "mc": float(d.get("marketCap", 0) or 0),
                        }
    except Exception:
        pass

    # 2) DexScreener (prefer FDV as MC proxy)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(DEXSCREENER_URL.format(mint)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", []) or []
                    if pairs:
                        pair = pairs[0]
                        return {
                            "symbol": (pair.get("baseToken") or {}).get("symbol", "TOKEN"),
                            "price": float(pair.get("priceUsd", 0) or 0),
                            "mc": float(pair.get("fdv", 0) or 0),
                        }
    except Exception:
        pass

    # 3) CoinGecko
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(COINGECKO_URL.format(mint)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    md = data.get("market_data") or {}
                    return {
                        "symbol": (data.get("symbol") or "TOKEN").upper(),
                        "price": float(((md.get("current_price") or {}).get("usd", 0)) or 0),
                        "mc": float(((md.get("market_cap") or {}).get("usd", 0)) or 0),
                    }
    except Exception:
        pass

    return {"symbol": "TOKEN", "price": 0, "mc": 0}

async def best_symbol_for_mint(mint: str):
    try:
        info = await fetch_token_info(mint)
        sym = (info.get("symbol") or "").strip()
        return sym or None
    except Exception:
        return None

# ---------- POLLING ----------
async def poll_sells(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id, mint, media_file_id, symbol, usd_threshold FROM sell_tracked")
    rows = c.fetchall()
    conn.close()

    for chat_id, mint, media_file_id, symbol, usd_threshold in rows:
        if is_native_sol(mint):
            continue

        try:
            txs = await fetch_transactions(mint, limit=5)
            if not txs:
                continue

            sig = txs[0].get("signature")
            if not sig or sell_last_seen.get(mint) == sig:
                continue
            sell_last_seen[mint] = sig

            if not symbol:
                symbol = await best_symbol_for_mint(mint)
                if symbol:
                    sell_update_symbol(mint, symbol)

            info = await fetch_token_info(mint)
            disp_symbol = symbol or info.get("symbol", "TOKEN")
            price = float(info.get("price", 0) or 0)
            mcap = float(info.get("mc", 0) or 0)

            details = await parse_sell(sig, mint)
            if not details:
                continue

            amount = float(details.get("amount", 0) or 0)
            seller = details.get("seller") or "?"
            usd_value = amount * price if price else 0.0

            # Whale gating (only alert if >= threshold)
            threshold = float(usd_threshold) if usd_threshold and usd_threshold > 0 else DEFAULT_WHALE_USD
            if usd_value < threshold:
                continue

            # Styled alert (sell)
            skulls = "💀" * 14
            title = f"{disp_symbol} [{disp_symbol}] 💀Sell!"
            tx_url = f"https://solscan.io/tx/{sig}"

            text = (
                f"{title}\n\n"
                f"{skulls}\n\n"
                f"💀| {fmt_usd(usd_value)}\n"
                f"💀| Sold: {fmt_amount(amount)} {disp_symbol}\n"
                f"💀| Seller | Txn\n"
                f"💀| Market Cap: {fmt_usd(mcap)}\n"
            )

            # Buttons (mirroring buy tracker style)
            jup = f"https://jup.ag/swap/SOL-{mint}"
            dexs = f"https://dexscreener.com/solana/{mint}"
            twitter = "https://x.com/sentrip_bot"
            seller_url = f"https://solscan.io/account/{seller}" if seller else tx_url

            buttons = [
                [
                    InlineKeyboardButton("🐴 Buy", url=jup),
                    InlineKeyboardButton("💀 DexS", url=dexs),
                    InlineKeyboardButton("💀 Twitter", url=twitter),
                ],
                [
                    InlineKeyboardButton("Seller", url=seller_url),
                    InlineKeyboardButton("Txn", url=tx_url),
                ],
            ]
            markup = InlineKeyboardMarkup(buttons)

            if media_file_id:
                await context.bot.send_photo(chat_id, photo=media_file_id, caption=text, reply_markup=markup)
            else:
                await context.bot.send_message(chat_id, text, reply_markup=markup)

        except Exception as e:
            logging.error(f"Error polling sells for {mint}: {e}")

# ---------- REGISTER ----------
def register_selltracker(app):
    init_sell_db()
    # commands
    app.add_handler(CommandHandler("track_sell", sell_track))
    app.add_handler(CommandHandler("sell_skip", sell_skip))
    app.add_handler(CommandHandler("untrack_sell", sell_untrack))
    app.add_handler(CommandHandler("list_sells", sell_list))
    app.add_handler(CommandHandler("sellthreshold", sell_setthreshold))
    # media capture
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, sell_handle_media))
    # background job
    app.job_queue.run_repeating(poll_sells, interval=30, first=5)
