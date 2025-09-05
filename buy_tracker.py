# buy_tracker.py
import os
import sqlite3
import logging
import json
import aiohttp
import time
import secrets
import re
from typing import Dict, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.ext import (
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ApplicationHandlerStop,   # we'll use this in the DM gate
)

# ---------------- SETTINGS ----------------
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
HELIUS_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
PUMPFUN_URL = "https://api.pump.fun/v1/token/{}"   # best-effort (may fail)
ADD_TO_GROUP_URL = (
    "https://telegram.me/sentrip_bot"
    "?startgroup=true"
    "&admin=change_info+delete_messages+ban_users+invite_users+pin_messages"
)

DB_PATH = "tracked_tokens.db"
logging.basicConfig(level=logging.INFO)

DEFAULT_MIN_BUY_USD = 5.0
DEFAULT_EMOJI = "👀"

PAIR_CODE_TTL_SEC = 10 * 60  # 10 minutes

NATIVE_SOL_MINTS = {"So11111111111111111111111111111111111111112"}

# ---------------- PAIRING CODES (Group → DM) ----------------
# code -> {"origin_chat_id": int, "user_id": int, "ts": float}
PAIR_CODES: Dict[str, Dict] = {}

def _gen_pair_code() -> str:
    # 6-digit numeric, avoid collisions
    for _ in range(20):
        code = f"{secrets.randbelow(900000) + 100000}"
        if code not in PAIR_CODES:
            return code
    # very unlikely fallback
    return f"{secrets.randbelow(900000) + 100000}"

def _put_code(code: str, origin_chat_id: int, user_id: int):
    PAIR_CODES[code] = {"origin_chat_id": origin_chat_id, "user_id": user_id, "ts": time.time()}

def _pop_valid_code(code: str, user_id: int) -> Optional[int]:
    data = PAIR_CODES.get(code)
    if not data:
        return None
    # expire old
    if time.time() - data["ts"] > PAIR_CODE_TTL_SEC:
        PAIR_CODES.pop(code, None)
        return None
    # bind to same user
    if data["user_id"] != user_id:
        return None
    PAIR_CODES.pop(code, None)
    return int(data["origin_chat_id"])

# ---------------- DB ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tracked_tokens (
            chat_id INTEGER NOT NULL,
            mint TEXT NOT NULL,
            symbol TEXT,
            media_file_id TEXT,
            emoji TEXT,
            total_supply REAL,
            min_buy_usd REAL,
            socials_json TEXT,
            active INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, mint)
        )
        """
    )
    conn.commit()
    conn.close()

def upsert_token(
    chat_id: int,
    mint: str,
    **fields,
):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Ensure row exists
    c.execute(
        "INSERT OR IGNORE INTO tracked_tokens(chat_id, mint, min_buy_usd) VALUES(?, ?, ?)",
        (chat_id, mint, DEFAULT_MIN_BUY_USD),
    )
    if fields:
        cols = ", ".join([f"{k}=?" for k in fields.keys()])
        vals = list(fields.values())
        vals.extend([chat_id, mint])
        c.execute(f"UPDATE tracked_tokens SET {cols} WHERE chat_id=? AND mint=?", vals)
    conn.commit()
    conn.close()

def set_active(chat_id: int, mint: str, active: bool):
    upsert_token(chat_id, mint, active=1 if active else 0)

def remove_token(chat_id: int, mint: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM tracked_tokens WHERE chat_id=? AND mint=?", (chat_id, mint))
    conn.commit()
    conn.close()

def list_tokens_rows(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT mint, symbol, media_file_id, emoji, total_supply, min_buy_usd, socials_json, active FROM tracked_tokens WHERE chat_id=?",
        (chat_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows

def get_token_row(chat_id: int, mint: str) -> Optional[tuple]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT mint, symbol, media_file_id, emoji, total_supply, min_buy_usd, socials_json, active FROM tracked_tokens WHERE chat_id=? AND mint=?",
        (chat_id, mint),
    )
    row = c.fetchone()
    conn.close()
    return row

# ---------------- HELPERS ----------------
def is_native_sol(mint: str) -> bool:
    return mint in NATIVE_SOL_MINTS

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

# ---------------- EXTERNAL LOOKUPS ----------------
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

async def fetch_token_info(mint: str):
    # 1) Pump.fun
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
                            "name": d.get("name") or d.get("symbol") or "TOKEN",
                        }
    except Exception:
        pass

    # 2) DexScreener
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(DEXSCREENER_URL.format(mint)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs", []) or []
                    if pairs:
                        pair = pairs[0]
                        price_usd = float(pair.get("priceUsd", 0) or 0)
                        fdv = float(pair.get("fdv", 0) or 0)
                        base = (pair.get("baseToken") or {})
                        symbol = base.get("symbol", "TOKEN")
                        name = base.get("name") or symbol or "TOKEN"
                        return {"symbol": symbol, "price": price_usd, "mc": fdv, "name": name}
    except Exception:
        pass

    return {"symbol": "TOKEN", "price": 0, "mc": 0, "name": "TOKEN"}

async def best_symbol_for_mint(mint: str) -> Optional[str]:
    try:
        info = await fetch_token_info(mint)
        sym = (info.get("symbol") or "").strip()
        return sym or None
    except Exception:
        return None

# -------- DexScreener helpers: pair + recent trades --------
DEX_PAIR_TRADES_URL = "https://api.dexscreener.com/latest/dex/trades/{}"

async def fetch_primary_pair_for_mint(mint: str) -> Optional[dict]:
    """
    Return the primary DexScreener pair object for a mint, or None.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(DEXSCREENER_URL.format(mint)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                pairs = data.get("pairs") or []
                return pairs[0] if pairs else None
    except Exception:
        return None

async def fetch_recent_trades(pair_address: str, limit: int = 25) -> list:
    """
    Return recent trades for a DexScreener pair. Each item has:
      - 'txId', 'timestamp' (ms), 'side' ('buy'/'sell'), 'priceUsd', 'amountUsd',
        'amountToken' (sometimes), and maybe maker/taker.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(DEX_PAIR_TRADES_URL.format(pair_address), params={"limit": str(limit)}) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("trades") or []
    except Exception:
        return []

# ---------------- POLLING ----------------
# NOTE: now we key by pairAddress and save last seen trade txId
last_seen: Dict[str, str] = {}  # pairAddress -> last trade txId

async def poll_tracked(context: ContextTypes.DEFAULT_TYPE):
    """
    Poll DexScreener trades per token's primary pair and alert on buys.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT chat_id, mint, media_file_id, symbol, emoji, min_buy_usd, socials_json, active
        FROM tracked_tokens WHERE active=1
    """)
    rows = c.fetchall()
    conn.close()

    for chat_id, mint, media_file_id, symbol, emoji, min_buy_usd, socials_json, active in rows:
        try:
            pair = await fetch_primary_pair_for_mint(mint)
            if not pair:
                continue

            pair_addr = pair.get("pairAddress")
            price_usd = float(pair.get("priceUsd", 0) or 0)
            fdv = float(pair.get("fdv", 0) or 0)
            base = pair.get("baseToken") or {}
            psymbol = (base.get("symbol") or "").strip() or symbol or "TOKEN"
            pname = (base.get("name") or psymbol or "TOKEN")

            trades = await fetch_recent_trades(pair_addr, limit=25)
            if not trades:
                continue

            last_txid = last_seen.get(pair_addr)
            new_trades = []
            for t in trades:  # DexScreener returns newest-first
                txid = t.get("txId")
                if not txid:
                    continue
                if txid == last_txid:
                    break
                new_trades.append(t)
            new_trades.reverse()  # alert in chronological order

            if trades:
                last_seen[pair_addr] = trades[0].get("txId") or last_txid

            threshold = float(min_buy_usd or DEFAULT_MIN_BUY_USD)
            chosen_emoji = (emoji or DEFAULT_EMOJI)
            dexs = f"https://dexscreener.com/solana/{pair_addr}"
            jup = f"https://jup.ag/swap/SOL-{mint}"

            socials_txt = ""
            try:
                soc = json.loads(socials_json) if socials_json else {}
                parts = []
                if soc.get("x"): parts.append(f"[X]({soc['x']})")
                if soc.get("instagram"): parts.append(f"[IG]({soc['instagram']})")
                if soc.get("website"): parts.append(f"[Web]({soc['website']})")
                if parts:
                    socials_txt = " " + " • ".join(parts)
            except Exception:
                pass

            for t in new_trades:
                if (t.get("side") or "").lower() != "buy":
                    continue
                usd = float(t.get("amountUsd", 0) or 0)
                if usd < threshold:
                    continue

                token_amount = t.get("amountToken")
                txid = t.get("txId")
                tx_url = f"https://solscan.io/tx/{txid}"

                # buyer often not available via DexScreener on Solana
                buyer_label = "Unknown"
                buyer_url = tx_url

                px = float(t.get("priceUsd", 0) or price_usd or 0)
                mcap = fdv

                title = f"{chosen_emoji} | {psymbol} BUY!"
                if not token_amount and px:
                    token_amount = usd / px
                token_str = fmt_amount(token_amount) if token_amount is not None else "—"

                body = (
                    f"🔷 ~SOL {fmt_amount(usd/px) if px else '—'} ({fmt_usd(usd)})\n"
                    f"🪙 {psymbol} {token_str}\n"
                    f"🔎 Position: New Holder [{buyer_label}]({buyer_url})\n"
                    f"📈 MCap: {fmt_usd(mcap)}{socials_txt}\n"
                    f"[Tx]({tx_url})"
                )
                text = f"{title}\n{body}"

                buttons = [
                    [InlineKeyboardButton("📊 Dex", url=dexs), InlineKeyboardButton("💲 Buy", url=jup)],
                    [InlineKeyboardButton("Txn", url=tx_url)],
                ]
                markup = InlineKeyboardMarkup(buttons)

                if media_file_id:
                    try:
                        await context.bot.send_photo(
                            chat_id,
                            photo=media_file_id,
                            caption=text,
                            reply_markup=markup,
                            parse_mode="Markdown",
                            disable_web_page_preview=True,
                        )
                    except Exception:
                        await context.bot.send_message(
                            chat_id, text, reply_markup=markup, parse_mode="Markdown", disable_web_page_preview=True
                        )
                else:
                    await context.bot.send_message(
                        chat_id, text, reply_markup=markup, parse_mode="Markdown", disable_web_page_preview=True
                    )

        except Exception as e:
            logging.error(f"Buy poll error for {mint}: {e}")

# ---------------- DM FLOW STATE ----------------
# user_id -> dict(stage, origin_chat_id, mint, tmp_fields)
PENDING_DM: Dict[int, Dict] = {}

def _settings_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("😀 Emoji", callback_data="bt:set:emoji")],
        [InlineKeyboardButton("📦 Total Supply", callback_data="bt:set:supply")],
        [InlineKeyboardButton("💵 Min Buy ($)", callback_data="bt:set:minbuy")],
        [InlineKeyboardButton("🖼️ Media", callback_data="bt:set:media")],
        [InlineKeyboardButton("🔗 Socials", callback_data="bt:set:socials")],
        [InlineKeyboardButton("🗑 Delete Token", callback_data="bt:set:delete")],
        [InlineKeyboardButton("✅ Done / Activate", callback_data="bt:set:done")],
    ]
    return InlineKeyboardMarkup(rows)

# ---------------- COMMANDS (GROUP → PAIRING CODE) ----------------
async def cmd_track_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group: /track → show pairing code + plain DM link and set pending DM state."""
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Use /track inside a group to configure via DM.")
        return

    me = await context.bot.get_me()
    code = _gen_pair_code()
    _put_code(code, origin_chat_id=chat.id, user_id=user.id)

    # mark user pending so DM accepts plain text immediately
    PENDING_DM[user.id] = {
        "stage": "await_code",
        "origin_chat_id": chat.id,
        "mint": None,
        "tmp": {},
        "code": code,
    }

    dm_url = f"https://t.me/{me.username}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Open DM with SentriBot", url=dm_url)]])
    await update.message.reply_text(
        "I’ll guide you in DM to set this up for this group.\n\n"
        f"🔐 Pairing code: <code>{code}</code>\n"
        "➡️ In DM, send: <b>track {code}</b> or just <b>{code}</b>\n"
        "<i>(Code expires in 10 minutes and only works for you.)</i>",
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

# ---------------- DM GATE (HIGH PRIORITY, like your welcome/rules gate) ----------------
PAIR_RX = re.compile(r"^\s*track\s+(\d{6})\s*$", re.IGNORECASE)
ANY_CODE_RX = re.compile(r"\b(\d{6})\b")

async def buy_dm_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Intercept DM while buy-tracker setup is pending; route and stop others."""
    chat = update.effective_chat
    msg = update.message
    if not chat or chat.type != "private" or not msg or not (msg.text or msg.caption):
        return

    uid = update.effective_user.id
    state = PENDING_DM.get(uid)
    if not state:
        return  # not in our flow; let others handle

    # Block bot commands while configuring, like your moderation DM gate
    if any(e.type == "bot_command" for e in (msg.entities or [])) or msg.text.strip().startswith("/"):
        await msg.reply_text("Please send the text only (no /commands).")
        raise ApplicationHandlerStop

    # If they’re at await_code, accept “track 123456” or just “123456”
    if state.get("stage") == "await_code":
        text = msg.text.strip()
        m = PAIR_RX.match(text) or ANY_CODE_RX.search(text)
        if not m:
            await msg.reply_text(
                "Please send the pairing code I gave you in the group (e.g., <code>track 123456</code> or just <code>123456</code>).",
                parse_mode="HTML",
            )
            raise ApplicationHandlerStop
        code = m.group(1)
        origin_from_code = _pop_valid_code(code, uid)
        if not origin_from_code or origin_from_code != state.get("origin_chat_id"):
            await msg.reply_text("❌ Invalid or expired code. Go back to your group and run /track again.")
            raise ApplicationHandlerStop
        # advance
        state["stage"] = "ask_mint"
        await msg.reply_text("🧭 Send the <b>mint address</b> you want to track.", parse_mode="HTML")
        raise ApplicationHandlerStop

    # Otherwise, route to our normal DM text router and stop
    await dm_text_router(update, context)
    raise ApplicationHandlerStop

# ---------------- DM ENTRY via "track <code)" (works even without pending) ----------------
async def dm_entry_by_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DM-only: user sends 'track 123456' to begin the wizard (even if no pending state)."""
    chat = update.effective_chat
    msg = update.message
    if chat.type != "private" or not msg or not msg.text:
        return

    m = PAIR_RX.match(msg.text)
    if not m:
        return  # not our trigger

    uid = update.effective_user.id
    code = m.group(1)
    origin_chat_id = _pop_valid_code(code, uid)
    if not origin_chat_id:
        await msg.reply_text("❌ Invalid or expired code. Go back to your group and run /track again.")
        return

    # Start the wizard
    PENDING_DM[uid] = {"stage": "ask_mint", "origin_chat_id": origin_chat_id, "mint": None, "tmp": {}}
    await msg.reply_text("🧭 Send the <b>mint address</b> you want to track.", parse_mode="HTML")

# ---------------- DM TEXT HANDLER ----------------
async def dm_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.message
    if chat.type != "private" or not msg or not msg.text:
        return

    uid = update.effective_user.id
    state = PENDING_DM.get(uid)
    if not state:
        return  # not in the flow

    stage = state.get("stage")
    origin = state.get("origin_chat_id")

    # Disallow commands inside the flow
    if msg.text.strip().startswith("/"):
        await msg.reply_text("Please send text (no /commands) while configuring.")
        return

    if stage == "ask_mint":
        mint = msg.text.strip()
        if is_native_sol(mint):
            await msg.reply_text("⚠️ Native SOL isn’t an SPL mint. Send a token mint address.")
            return
        try:
            info = await fetch_token_info(mint)
        except Exception:
            info = {"symbol": "TOKEN", "name": "TOKEN"}
        symbol = (info.get("symbol") or "TOKEN")
        name = (info.get("name") or symbol)
        state["mint"] = mint
        # store defaults
        upsert_token(origin, mint, symbol=symbol, min_buy_usd=DEFAULT_MIN_BUY_USD)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"✅ {name} ({symbol}) — {short_mint(mint)}", callback_data=f"bt:confirm:{mint}")],
                [InlineKeyboardButton("↩️ Send another mint", callback_data="bt:again")],
            ]
        )
        await msg.reply_text(
            f"I found: <b>{name}</b> (<b>{symbol}</b>) for <code>{short_mint(mint)}</code>\nConfirm this token?",
            reply_markup=kb,
            parse_mode="HTML",
        )
        state["stage"] = "confirming"
        return

    # Settings sub-states handled by callback + this text router
    if stage == "set_emoji":
        emoji = msg.text.strip()
        upsert_token(origin, state["mint"], emoji=emoji)
        await msg.reply_text(f"Emoji set to {emoji}")
        state["stage"] = "settings"
        await msg.reply_text("🛠 Buy Bot Settings:", reply_markup=_settings_keyboard())
        return

    if stage == "set_supply":
        try:
            supply = float(msg.text.strip().replace(",", ""))
            upsert_token(origin, state["mint"], total_supply=supply)
            await msg.reply_text(f"Total supply set to {supply:,.0f}")
        except Exception:
            await msg.reply_text("Please send a number (e.g., 1_000_000_000).")
            return
        state["stage"] = "settings"
        await msg.reply_text("🛠 Buy Bot Settings:", reply_markup=_settings_keyboard())
        return

    if stage == "set_minbuy":
        try:
            mb = float(msg.text.strip().replace("$", "").replace(",", ""))
            upsert_token(origin, state["mint"], min_buy_usd=mb)
            await msg.reply_text(f"Min buy set to ${mb:,.2f}")
        except Exception:
            await msg.reply_text("Send a dollar amount (e.g., 5 or 12.5).")
            return
        state["stage"] = "settings"
        await msg.reply_text("🛠 Buy Bot Settings:", reply_markup=_settings_keyboard())
        return

    if stage == "set_socials":
        text = msg.text.strip()
        data = {}
        try:
            if text.startswith("{"):
                data = json.loads(text)
            else:
                for part in text.splitlines():
                    if ":" in part:
                        k, v = part.split(":", 1)
                        data[k.strip().lower()] = v.strip()
        except Exception:
            await msg.reply_text("Send socials as key:value lines or a small JSON object.")
            return
        upsert_token(origin, state["mint"], socials_json=json.dumps(data))
        await msg.reply_text("Socials updated.")
        state["stage"] = "settings"
        await msg.reply_text("🛠 Buy Bot Settings:", reply_markup=_settings_keyboard())
        return

# ---------------- DM MEDIA HANDLER (for media setting) ----------------
async def dm_media_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.message
    if chat.type != "private" or not msg:
        return
    uid = update.effective_user.id
    state = PENDING_DM.get(uid)
    if not state or state.get("stage") != "set_media":
        return

    origin = state["origin_chat_id"]
    mint = state["mint"]

    file_id = None
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.video:
        file_id = msg.video.file_id
    elif msg.document and (msg.document.mime_type or "").startswith(("image/", "video/")):
        file_id = msg.document.file_id

    if not file_id:
        await msg.reply_text("Please send an image or video.")
        return

    upsert_token(origin, mint, media_file_id=file_id)
    await msg.reply_text("Media saved.")
    state["stage"] = "settings"
    await msg.reply_text("🛠 Buy Bot Settings:", reply_markup=_settings_keyboard())

# ---------------- CALLBACKS (CONFIRM & SETTINGS) ----------------
async def bt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id
    state = PENDING_DM.get(uid)
    if not state:
        return

    origin = state["origin_chat_id"]
    mint = state.get("mint")

    if data == "bt:again":
        state["stage"] = "ask_mint"
        await q.message.reply_text("Okay, send the mint address again.")
        return

    if data.startswith("bt:confirm:"):
        # Confirmed the mint — show settings
        state["stage"] = "settings"
        await q.message.reply_text("⚙️ Choose from the following options to customize your Buy Bot:", reply_markup=_settings_keyboard())
        return

    if data == "bt:set:emoji":
        state["stage"] = "set_emoji"
        await q.message.reply_text("Send the emoji you want to use.")
        return

    if data == "bt:set:supply":
        state["stage"] = "set_supply"
        await q.message.reply_text("Send the total supply (number).")
        return

    if data == "bt:set:minbuy":
        state["stage"] = "set_minbuy"
        await q.message.reply_text(f"Send the minimum buy in USD (default {DEFAULT_MIN_BUY_USD}).")
        return

    if data == "bt:set:media":
        state["stage"] = "set_media"
        await q.message.reply_text("Send an image or video to attach to every alert.")
        return

    if data == "bt:set:socials":
        state["stage"] = "set_socials"
        await q.message.reply_text(
            "Send your socials as key:value on separate lines, e.g.\n"
            "x:https://x.com/your\ninstagram:https://instagram.com/your\nwebsite:https://yoursite.xyz"
        )
        return

    if data == "bt:set:delete":
        if mint:
            remove_token(origin, mint)
        await q.message.reply_text("Token deleted from tracking.")
        # end the flow
        PENDING_DM.pop(uid, None)
        return

    if data == "bt:set:done":
        if not mint:
            await q.message.reply_text("Missing token context. Please start over with /track in your group.")
            PENDING_DM.pop(uid, None)
            return
        set_active(origin, mint, True)
        row = get_token_row(origin, mint)
        symbol = (row[1] if row else None) or (await best_symbol_for_mint(mint) or "TOKEN")
        try:
            await context.bot.send_message(
                origin,
                f"✅ SentriBot Buy Tracker has been successfully activated!\nTracking <b>{symbol}</b> (<code>{short_mint(mint)}</code>).",
                parse_mode="HTML"
            )
        except Exception:
            pass
        await q.message.reply_text("All set! Alerts will post in your group when buys meet your settings.")
        PENDING_DM.pop(uid, None)
        return

# ---------------- LEGACY COMMANDS (OPTIONAL) ----------------
async def cmd_untrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /untrack <mint>")
        return
    mint = context.args[0].strip()
    chat_id = update.effective_chat.id
    remove_token(chat_id, mint)
    await update.message.reply_text(f"🗑 Stopped tracking {short_mint(mint)}.")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = list_tokens_rows(chat_id)
    if not rows:
        await update.message.reply_text("No tokens tracked.")
        return

    lines = []
    for (mint, symbol, media_file_id, emoji, supply, min_buy, socials_json, active) in rows:
        status = "ON" if active else "OFF"
        symbol = symbol or "TOKEN"
        link = f"https://solscan.io/token/{mint}"
        lines.append(f"• {symbol} — [{short_mint(mint)}]({link}) — min ${min_buy or DEFAULT_MIN_BUY_USD:.2f} — {status}")

    await update.message.reply_text(
        "📋 Tracked tokens:\n" + "\n".join(lines),
        disable_web_page_preview=True,
        parse_mode="Markdown"
    )

# ---------------- REGISTER ----------------
def register_buytracker(app):
    init_db()

    # GROUP command to kick off pairing
    app.add_handler(CommandHandler("track", cmd_track_group, filters.ChatType.GROUPS))

    # DM entry via "track <code>" — VERY high priority
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, dm_entry_by_code), group=-300)

    # 🔴 High-priority DM gate for buy tracker (runs after the code-catcher above)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.TEXT | filters.Caption()), buy_dm_gate), group=-250)

    # DM callbacks & text/media during configuration
    app.add_handler(CallbackQueryHandler(bt_callback, pattern=r"^bt:(confirm|again|set:.*)"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, dm_text_router))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO | filters.Document.ALL), dm_media_router))

    # Optional legacy group utilities
    app.add_handler(CommandHandler("untrack", cmd_untrack))
    app.add_handler(CommandHandler("list", cmd_list))

    # Poller
    app.job_queue.run_repeating(poll_tracked, interval=30, first=5)
