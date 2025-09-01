# buy_tracker.py
import os
import sqlite3
import logging
import asyncio
from typing import Any, Dict, Optional

import aiohttp
from aiohttp import ClientTimeout, TCPConnector

from telegram import Update
from telegram.ext import CommandHandler, MessageHandler, ContextTypes, filters

# ---------------- SETTINGS ----------------
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
# Birdeye is optional now (we try Pump.fun and DexScreener first)
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

DB_PATH = "tracked_tokens.db"
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("buytracker")

# Polling
POLL_INTERVAL_SEC = int(os.getenv("BT_POLL_INTERVAL", "20"))   # default 20s
POLL_FIRST_DELAY = int(os.getenv("BT_POLL_FIRST", "5"))        # default 5s
MAX_SIG_AGE_SEC = int(os.getenv("BT_MAX_SIG_AGE", "0"))        # 0 = no age check

# ---------------- DATABASE ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tracked_tokens (
            chat_id INTEGER,
            mint TEXT,
            media_file_id TEXT,
            PRIMARY KEY (chat_id, mint)
        )
        """
    )
    conn.commit()
    conn.close()

def add_token(chat_id, mint, media_file_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO tracked_tokens VALUES (?, ?, ?)",
        (chat_id, mint, media_file_id),
    )
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

# ---------------- HTTP (shared session + retries) ----------------
_session: Optional[aiohttp.ClientSession] = None

def _session_headers() -> Dict[str, str]:
    h = {
        "User-Agent": "SentriBot/1.0 (+https://t.me/)",
        "Accept": "application/json",
    }
    if BIRDEYE_API_KEY:
        h["x-api-key"] = BIRDEYE_API_KEY
    return h

async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session and not _session.closed:
        return _session
    timeout = ClientTimeout(total=10)
    _session = aiohttp.ClientSession(
        timeout=timeout,
        connector=TCPConnector(ssl=False),
        headers=_session_headers(),
    )
    return _session

async def http_get(url: str) -> Optional[Dict[str, Any]]:
    s = await get_session()
    for attempt in range(3):
        try:
            async with s.get(url) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                log.warning(f"GET {url} -> {resp.status}")
        except Exception as e:
            log.warning(f"GET {url} failed (attempt {attempt+1}/3): {e}")
        await asyncio.sleep(0.5 * (attempt + 1))
    return None

async def http_post(url: str, json: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    s = await get_session()
    for attempt in range(3):
        try:
            async with s.post(url, json=json) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                log.warning(f"POST {url} -> {resp.status}")
        except Exception as e:
            log.warning(f"POST {url} failed (attempt {attempt+1}/3): {e}")
        await asyncio.sleep(0.5 * (attempt + 1))
    return None

# ---------------- BUYTRACKER LOGIC ----------------
pending_media = {}   # chat_id -> mint awaiting image
last_seen = {}       # mint -> last signature string

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
        msg = "📋 Tracked tokens:\n" + "\n".join([mint for mint, _ in tokens])
        await update.message.reply_text(msg)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok_helius = bool(HELIUS_API_KEY)
    ok_birdeye = bool(BIRDEYE_API_KEY)
    chat_id = update.effective_chat.id
    tokens = list_tokens(chat_id)
    await update.message.reply_text(
        "🩺 Status:\n"
        f"• Helius API key: {'✅' if ok_helius else '❌'}\n"
        f"• Birdeye API key (optional): {'✅' if ok_birdeye else '❌'}\n"
        f"• Poll every: {POLL_INTERVAL_SEC}s (first in {POLL_FIRST_DELAY}s)\n"
        f"• Tracked here: {len(tokens)} mint(s)"
    )

# ---------------- EXTERNAL APIS ----------------
HELIUS_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/solana/contract/{}"
PUMPFUN_URL = "https://api.pump.fun/v1/token/{}"  # unofficial; may not work for all

async def fetch_transactions(mint: str, limit: int = 5):
    payload = {
        "jsonrpc": "2.0",
        "id": "tx_fetch",
        "method": "getSignaturesForAddress",
        "params": [mint, {"limit": limit}],
    }
    data = await http_post(HELIUS_URL, payload)
    return (data or {}).get("result", [])

async def get_transaction(signature: str):
    payload = {
        "jsonrpc": "2.0",
        "id": "tx_parse",
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed"}],
    }
    data = await http_post(HELIUS_URL, payload)
    return (data or {}).get("result")

async def parse_buy(signature: str, mint: str):
    tx = await get_transaction(signature)
    if not tx:
        return None

    meta = tx.get("meta", {}) or {}
    pre = meta.get("preTokenBalances", []) or []
    post = meta.get("postTokenBalances", []) or []

    def amt(tb):
        u = tb.get("uiTokenAmount", {}) or {}
        # amounts from RPC may be gigantic; keep as int
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

# ---------------- FETCH TOKEN INFO (Pump.fun → DexScreener → CoinGecko) ----------------
async def fetch_token_info(mint: str):
    # 1) Pump.fun
    try:
        data = await http_get(PUMPFUN_URL.format(mint))
        if data:
            d = data.get("data") or {}
            if d:
                return {
                    "symbol": d.get("symbol", "TOKEN"),
                    "price": float(d.get("price", 0) or 0),
                    "mc": float(d.get("marketCap", 0) or 0),
                }
    except Exception as e:
        log.warning(f"Pump.fun parse failed for {mint}: {e}")

    # 2) DexScreener
    try:
        data = await http_get(DEXSCREENER_URL.format(mint))
        if data:
            pairs = data.get("pairs") or []
            if pairs:
                pair = pairs[0]
                return {
                    "symbol": (pair.get("baseToken") or {}).get("symbol", "TOKEN"),
                    "price": float(pair.get("priceUsd", 0) or 0),
                    # DexScreener doesn't give market cap reliably; use liquidityUsd as a rough proxy
                    "mc": float(pair.get("liquidityUsd", 0) or 0),
                }
    except Exception as e:
        log.warning(f"DexScreener parse failed for {mint}: {e}")

    # 3) CoinGecko
    try:
        data = await http_get(COINGECKO_URL.format(mint))
        if data:
            md = data.get("market_data") or {}
            return {
                "symbol": str(data.get("symbol", "TOKEN")).upper(),
                "price": float((md.get("current_price") or {}).get("usd", 0) or 0),
                "mc": float((md.get("market_cap") or {}).get("usd", 0) or 0),
            }
    except Exception as e:
        log.warning(f"CoinGecko parse failed for {mint}: {e}")

    return {"symbol": "TOKEN", "price": 0.0, "mc": 0.0}

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

            latest = txs[0]  # newest first
            sig = latest.get("signature")
            if not sig:
                continue

            # optional: skip very old signatures
            if MAX_SIG_AGE_SEC and latest.get("blockTime"):
                import time
                age = int(time.time()) - int(latest["blockTime"])
                if age > MAX_SIG_AGE_SEC:
                    log.info(f"Skipping old sig for {mint}: {sig} (age {age}s)")
                    continue

            if last_seen.get(mint) == sig:
                continue  # nothing new

            details = await parse_buy(sig, mint)
            if not details:
                # not a buy (could be transfer, burn, etc.)
                last_seen[mint] = sig
                continue

            last_seen[mint] = sig

            token_info = await fetch_token_info(mint)
            symbol = token_info.get("symbol", "TOKEN")
            price = float(token_info.get("price", 0) or 0)
            mcap = float(token_info.get("mc", 0) or 0)

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

            if media_file_id:
                await context.bot.send_photo(chat_id, photo=media_file_id, caption=text)
            else:
                await context.bot.send_message(chat_id, text)

        except Exception as e:
            log.error(f"Error polling {mint}: {e}")

# ---------------- REGISTRATION ----------------
def register_buytracker(app, interval: int = POLL_INTERVAL_SEC, first: int = POLL_FIRST_DELAY):
    init_db()
    app.add_handler(CommandHandler("track", cmd_track))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("untrack", cmd_untrack))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_media))

    if getattr(app, "job_queue", None) is None:
        log.warning(
            "No JobQueue set up. Install PTB with job-queue extra: "
            "`pip install python-telegram-bot[job-queue]`"
        )
    else:
        app.job_queue.run_repeating(poll_tracked, interval=interval, first=first)
        log.info(f"BuyTracker polling scheduled every {interval}s (first in {first}s)")
