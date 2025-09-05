# buy_tracker.py
import os
import sqlite3
import logging
import json
import aiohttp
import asyncio
import time
import secrets
import re
from typing import Dict, Optional, Any, List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ApplicationHandlerStop,   # used in the DM gate
)

# ---------------- SETTINGS ----------------
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
HELIUS_HTTP_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else None
HELIUS_WS_URL   = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else None

DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
DEXSCREENER_TRADES_URL = "https://api.dexscreener.com/latest/dex/trades/{}"
PUMPFUN_URL = "https://api.pump.fun/v1/token/{}"   # best-effort (may fail)

DB_PATH = "tracked_tokens.db"
logging.basicConfig(level=logging.INFO)

DEFAULT_MIN_BUY_USD = 5.0
DEFAULT_EMOJI = "👀"

PAIR_CODE_TTL_SEC = 10 * 60  # 10 minutes

NATIVE_SOL_MINTS = {"So11111111111111111111111111111111111111112"}

# ---------------- PROGRAM IDS + TOGGLES ----------------
# Hard-coded mainnet defaults (can be overridden by env)
RAYDIUM_AMM_V4_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM_PROGRAM_ID   = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"

# Replace this only after you VERIFY the Pump.fun Program Id on Solscan (Program Id on a buy tx).
PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"  # <— verify & update if needed

# Feature toggles: include program filters in WS subscribe
USE_RAYDIUM_FILTER = True
USE_PUMPFUN_FILTER = True  # set True to monitor bonding-curve (pre-Raydium) buys

# Optional: allow env overrides (comma-separated)
def _split_env(name: str) -> List[str]:
    val = os.getenv(name, "").strip()
    if not val:
        return []
    return [x.strip() for x in val.split(",") if x.strip()]

# Build program id lists (env override OR defaults)
RAYDIUM_PROGRAM_IDS: List[str] = _split_env("RAYDIUM_PROGRAM_IDS") or [
    RAYDIUM_AMM_V4_PROGRAM_ID, RAYDIUM_CPMM_PROGRAM_ID
]
PUMPFUN_PROGRAM_IDS: List[str] = _split_env("PUMPFUN_PROGRAM_IDS") or (
    [PUMPFUN_PROGRAM_ID] if PUMPFUN_PROGRAM_ID else []
)
# If you want to include routers/aggregators (like Jupiter), add to env: AGGREGATOR_PROGRAM_IDS="JUPw...,..."
AGGREGATOR_PROGRAM_IDS: List[str] = _split_env("AGGREGATOR_PROGRAM_IDS")

# ---------------- PAIRING CODES (Group → DM) ----------------
# code -> {"origin_chat_id": int, "user_id": int, "ts": float}
PAIR_CODES: Dict[str, Dict] = {}

def _gen_pair_code() -> str:
    for _ in range(20):
        code = f"{secrets.randbelow(900000) + 100000}"
        if code not in PAIR_CODES:
            return code
    return f"{secrets.randbelow(900000) + 100000}"

def _put_code(code: str, origin_chat_id: int, user_id: int):
    PAIR_CODES[code] = {"origin_chat_id": origin_chat_id, "user_id": user_id, "ts": time.time()}

def _pop_valid_code(code: str, user_id: int) -> Optional[int]:
    data = PAIR_CODES.get(code)
    if not data:
        return None
    if time.time() - data["ts"] > PAIR_CODE_TTL_SEC:
        PAIR_CODES.pop(code, None)
        return None
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

def upsert_token(chat_id: int, mint: str, **fields):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
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

# ---------------- EXTERNAL LOOKUPS (HTTP) ----------------
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
            async with session.get(DEXSCREENER_TOKENS_URL.format(mint)) as resp:
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

async def fetch_primary_pair_for_mint(mint: str) -> Optional[dict]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(DEXSCREENER_TOKENS_URL.format(mint)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                pairs = data.get("pairs") or []
                return pairs[0] if pairs else None
    except Exception:
        return None

async def fetch_recent_trades(pair_address: str, limit: int = 25) -> list:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(DEXSCREENER_TRADES_URL.format(pair_address), params={"limit": str(limit)}) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("trades") or []
    except Exception:
        return []

# ---------------- REALTIME: HELIUS WS MANAGER ----------------
def _accounts_in_notif(notif: dict) -> List[str]:
    accs = set(notif.get("accounts") or [])
    try:
        keys = ((notif.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
        accs.update(keys)
    except Exception:
        pass
    return list(accs)

def _delta_for_mint(notif: dict, base_mint: str) -> float:
    """
    Sum (post - pre) of base_mint across token balances.
    Positive delta => net base_mint credited => BUY for the token.
    """
    meta = notif.get("meta") or {}
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []

    def amt(tb):
        u = tb.get("uiTokenAmount") or {}
        return int(u.get("amount", 0) or 0), int(u.get("decimals", 0) or 0)

    pre_map = {}
    for b in pre:
        if b.get("mint") == base_mint:
            a, d = amt(b)
            pre_map[(b.get("owner"), base_mint)] = (a, d)

    total = 0.0
    dec = 0
    for b in post:
        if b.get("mint") != base_mint:
            continue
        a_post, dec = amt(b)
        a_pre, _ = pre_map.get((b.get("owner"), base_mint), (0, dec))
        total += (a_post - a_pre) / (10 ** max(dec, 0))
    return total  # positive => buy, negative => sell

class HeliusWS:
    """
    Single connection to Helius Enhanced Websocket.
    - Raydium path: subscribe per pair (account include) + optional program filter
    - Pump.fun path: subscribe per mint (account include) + Pump.fun program filter
    Automatic handoff: if a token has no Raydium pair yet, subscribe via Pump.fun; once pair appears, switch.
    """
    def __init__(self):
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.ready = asyncio.Event()
        self._id = 1

        # raydium: pair_addr -> payload
        self.r_subs: Dict[str, Dict[str, Any]] = {}
        # pumpfun: mint -> payload
        self.p_subs: Dict[str, Dict[str, Any]] = {}

        # last tx dedupe
        self.last_txid: Dict[str, str] = {}

        # reconnect backoff
        self._reconnect_delay = 2

    async def ensure_connected(self):
        if not HELIUS_WS_URL:
            logging.warning("HELIUS_API_KEY not set; WS disabled.")
            return
        if self.ws and not self.ws.closed:
            return
        await self._connect()

    async def _connect(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        logging.info("Connecting to Helius WS...")
        self.ws = await self.session.ws_connect(HELIUS_WS_URL, heartbeat=20)
        self.ready.set()
        self._reconnect_delay = 2
        await self._resubscribe_all()
        asyncio.create_task(self._receiver_loop())

    async def _receiver_loop(self):
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(json.loads(msg.data))
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        except Exception as e:
            logging.error(f"WS receiver error: {e}")
        finally:
            await self._schedule_reconnect()

    async def _schedule_reconnect(self):
        self.ready.clear()
        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass
        logging.warning("WS disconnected; scheduling reconnect...")
        await asyncio.sleep(self._reconnect_delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, 30)
        try:
            await self._connect()
        except Exception as e:
            logging.error(f"Reconnect failed: {e}")
            asyncio.create_task(self._schedule_reconnect())

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _send(self, payload: dict):
        if not self.ws or self.ws.closed:
            return
        await self.ws.send_str(json.dumps(payload))

    async def _resubscribe_all(self):
        # Raydium pairs
        for pair_addr in list(self.r_subs.keys()):
            await self.subscribe_raydium_pair(pair_addr)
        # Pump.fun mints
        for mint in list(self.p_subs.keys()):
            await self.subscribe_pumpfun_mint(mint)

    # ----- Subscribe builders -----
    async def subscribe_raydium_pair(self, pair_addr: str):
        if not HELIUS_WS_URL:
            return
        params = {
            "commitment": "confirmed",
            "encoding": "jsonParsed",
            "accounts": {"include": [pair_addr]},
            "accountInclude": [pair_addr],
        }
        # Tighten with program filters if enabled
        program_ids: List[str] = []
        if USE_RAYDIUM_FILTER:
            program_ids.extend(RAYDIUM_PROGRAM_IDS)
        # Optional aggregators (e.g., routers that still touch Raydium accounts)
        if AGGREGATOR_PROGRAM_IDS:
            program_ids.extend(AGGREGATOR_PROGRAM_IDS)
        if program_ids:
            params["programIds"] = program_ids

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "transactionSubscribe",
            "params": [params],
        }
        await self._send(payload)

    async def subscribe_pumpfun_mint(self, mint: str):
        if not HELIUS_WS_URL:
            return
        params = {
            "commitment": "confirmed",
            "encoding": "jsonParsed",
            "accounts": {"include": [mint]},
            "accountInclude": [mint],
        }
        if USE_PUMPFUN_FILTER and PUMPFUN_PROGRAM_IDS:
            params["programIds"] = PUMPFUN_PROGRAM_IDS

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "transactionSubscribe",
            "params": [params],
        }
        await self._send(payload)

    # ----- Incoming notifications -----
    async def _handle_message(self, data: dict):
        if "result" in data and "id" in data and "method" not in data:
            # subscribe ack, ignore
            return

        if data.get("method") != "transactionNotification":
            return

        notif = (data.get("params") or {}).get("result") or {}
        accs = _accounts_in_notif(notif)

        # Raydium (by pair)
        for pair_addr, sub in list(self.r_subs.items()):
            if pair_addr in accs:
                await self._handle_swap_raydium(pair_addr, sub, notif)

        # Pump.fun (by mint)
        for mint, sub in list(self.p_subs.items()):
            if mint in accs:
                await self._handle_buy_pumpfun(mint, sub, notif)

    # ----- Decoders / handlers -----
    async def _handle_swap_raydium(self, pair_addr: str, sub: Dict[str, Any], notif: dict):
        txid = (notif.get("transaction") or {}).get("signatures", [None])[0] or notif.get("signature")
        if not txid:
            return
        key = f"r:{pair_addr}"
        if self.last_txid.get(key) == txid:
            return
        self.last_txid[key] = txid

        # Classify by base mint delta
        base_mint = sub["mint"]
        delta = _delta_for_mint(notif, base_mint)
        side = "buy" if delta > 0 else "sell" if delta < 0 else None
        if side != "buy":
            return

        usd = None
        amount_token = abs(delta) if delta else None

        # Try Helius enhanced swap events first
        events = (notif.get("events") or {})
        swap_evs = events.get("swap") or []
        if not isinstance(swap_evs, list):
            swap_evs = [swap_evs]
        for ev in swap_evs:
            info = ev.get("swapInfo") or {}
            if usd is None:
                usd = info.get("nativeUsd") or info.get("usdValue")

        # Fallback: DexScreener trade for this tx
        if usd is None or amount_token is None:
            trades = await fetch_recent_trades(pair_addr, limit=15)
            tmatch = next((t for t in trades if (t.get("txId") == txid)), None)
            if tmatch:
                try:
                    usd = float(tmatch.get("amountUsd") or 0) if usd is None else usd
                except Exception:
                    pass
                if amount_token is None:
                    try:
                        amount_token = float(tmatch.get("amountToken") or 0)
                    except Exception:
                        pass

        # If still no USD, estimate with cached price
        if usd is None:
            px = float(sub.get("price_usd") or 0)
            if px and amount_token is not None:
                usd = amount_token * px

        if usd is None:
            return
        if usd < float(sub.get("min_buy_usd") or DEFAULT_MIN_BUY_USD):
            return

        await self._send_alert(pair_addr, sub, txid, usd, amount_token)

    async def _handle_buy_pumpfun(self, mint: str, sub: Dict[str, Any], notif: dict):
        txid = (notif.get("transaction") or {}).get("signatures", [None])[0] or notif.get("signature")
        if not txid:
            return
        key = f"p:{mint}"
        if self.last_txid.get(key) == txid:
            return
        self.last_txid[key] = txid

        # Delta of base mint; Pump.fun “buy” increases base token
        delta = _delta_for_mint(notif, mint)
        if delta <= 0:
            return
        amount_token = delta

        # Price (pre-listing): try Pump.fun API; cache
        px = float(sub.get("price_usd") or 0)
        if not px:
            info = await fetch_token_info(mint)
            try:
                px = float(info.get("price") or 0)
                if px:
                    sub["price_usd"] = px
            except Exception:
                px = 0.0

        usd = amount_token * px if px else None
        if usd is None:
            return
        if usd < float(sub.get("min_buy_usd") or DEFAULT_MIN_BUY_USD):
            return

        await self._send_alert("pumpfun", sub, txid, usd, amount_token)

    async def _send_alert(self, key: str, sub: Dict[str, Any], txid: str, usd: float, amount_token: Optional[float]):
        chat_id = sub["chat_id"]
        mint = sub["mint"]
        symbol = sub.get("symbol") or "TOKEN"
        emoji = sub.get("emoji") or DEFAULT_EMOJI
        media = sub.get("media_file_id")
        mcap = float(sub.get("mcap") or 0)

        token_str = fmt_amount(amount_token) if amount_token is not None else "—"
        tx_url = f"https://solscan.io/tx/{txid}"
        if key == "pumpfun":
            dexs = f"https://dexscreener.com/solana/{mint}"  # may be empty pre-listing
        else:
            dexs = f"https://dexscreener.com/solana/{key}"
        jup = f"https://jup.ag/swap/SOL-{mint}"

        socials_txt = ""
        try:
            soc = json.loads(sub.get("socials_json") or "{}")
            parts = []
            if soc.get("x"): parts.append(f"[X]({soc['x']})")
            if soc.get("instagram"): parts.append(f"[IG]({soc['instagram']})")
            if soc.get("website"): parts.append(f"[Web]({soc['website']})")
            if parts:
                socials_txt = " " + " • ".join(parts)
        except Exception:
            pass

        title = f"{emoji} | {symbol} BUY!"
        body = (
            f"🔷 ~USD {fmt_usd(usd)}\n"
            f"🪙 {symbol} {token_str}\n"
            f"🔎 Position: New Holder [Unknown]({tx_url})\n"
            f"📈 MCap: {fmt_usd(mcap)}{socials_txt}\n"
            f"[Tx]({tx_url})"
        )
        text = f"{title}\n{body}"

        buttons = [
            [InlineKeyboardButton("📊 Dex", url=dexs), InlineKeyboardButton("💲 Buy", url=jup)],
            [InlineKeyboardButton("Txn", url=tx_url)],
        ]
        markup = InlineKeyboardMarkup(buttons)

        try:
            if media:
                await ws_context.bot.send_photo(
                    chat_id,
                    photo=media,
                    caption=text,
                    reply_markup=markup,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            else:
                await ws_context.bot.send_message(
                    chat_id, text, reply_markup=markup, parse_mode="Markdown", disable_web_page_preview=True
                )
        except Exception as e:
            logging.error(f"Send alert failed: {e}")

    # ----- Public API -----
    async def add_or_update_token(self, sub_payload: Dict[str, Any]):
        """
        sub_payload: {chat_id, mint, symbol, emoji, min_buy_usd, socials_json, media_file_id}
        - If Raydium pair exists => subscribe on Raydium (pair).
        - Else => subscribe on Pump.fun (by mint) and start a handoff task that watches for Raydium listing.
        """
        mint = sub_payload["mint"]

        # If we were already Pump.fun-subscribing for this mint, keep payload updated
        if mint in self.p_subs:
            self.p_subs[mint].update(sub_payload)

        pair = await fetch_primary_pair_for_mint(mint)
        if pair:
            pair_addr = pair.get("pairAddress")
            if pair_addr:
                # cache pricing/meta
                try:
                    base = pair.get("baseToken") or {}
                    sub_payload["symbol"] = (sub_payload.get("symbol") or base.get("symbol") or "TOKEN")
                    sub_payload["price_usd"] = float(pair.get("priceUsd", 0) or 0)
                    sub_payload["mcap"] = float(pair.get("fdv", 0) or 0)
                except Exception:
                    pass

                # register Raydium sub
                self.r_subs[pair_addr] = sub_payload
                await self.ensure_connected()
                await self.subscribe_raydium_pair(pair_addr)

                # If we were on Pump.fun for this mint, drop it
                if mint in self.p_subs:
                    del self.p_subs[mint]
                return

        # No Raydium yet — register Pump.fun subscription by mint
        self.p_subs[mint] = sub_payload
        await self.ensure_connected()
        await self.subscribe_pumpfun_mint(mint)

        # Start/refresh a handoff watcher for this mint
        asyncio.create_task(self._handoff_to_raydium_when_listed(mint))

    async def _handoff_to_raydium_when_listed(self, mint: str):
        # Periodically check if a Raydium pair appears; then add raydium sub and drop pumpfun sub.
        for _ in range(60):  # ~10 min if sleep 10s
            await asyncio.sleep(10)
            if mint not in self.p_subs:
                return  # already switched
            pair = await fetch_primary_pair_for_mint(mint)
            if not pair:
                continue
            pair_addr = pair.get("pairAddress")
            if not pair_addr:
                continue

            sub_payload = self.p_subs.get(mint)
            if not sub_payload:
                return
            # cache price/meta
            try:
                base = pair.get("baseToken") or {}
                sub_payload["symbol"] = (sub_payload.get("symbol") or base.get("symbol") or "TOKEN")
                sub_payload["price_usd"] = float(pair.get("priceUsd", 0) or 0)
                sub_payload["mcap"] = float(pair.get("fdv", 0) or 0)
            except Exception:
                pass

            self.r_subs[pair_addr] = sub_payload
            await self.subscribe_raydium_pair(pair_addr)
            # remove pumpfun sub
            if mint in self.p_subs:
                del self.p_subs[mint]
            logging.info(f"Switched {short_mint(mint)} to Raydium pair {pair_addr}")
            return

# Global WS manager instance and a lightweight context handle for bot send access
ws_manager = HeliusWS()
ws_context: ContextTypes.DEFAULT_TYPE  # set at runtime in starter job

# ---------------- POLLING FAILSAFE (DexScreener) ----------------
fallback_last_seen: Dict[str, str] = {}  # pairAddress -> last trade txId

async def fallback_poll(context: ContextTypes.DEFAULT_TYPE):
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
            if not pair_addr:
                continue

            trades = await fetch_recent_trades(pair_addr, limit=25)
            if not trades:
                continue

            last_txid = fallback_last_seen.get(pair_addr)
            new_trades = []
            for t in trades:  # newest-first
                txid = t.get("txId")
                if not txid:
                    continue
                if txid == last_txid:
                    break
                new_trades.append(t)
            new_trades.reverse()

            if trades:
                fallback_last_seen[pair_addr] = trades[0].get("txId") or last_txid

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
                px = float(t.get("priceUsd", 0) or pair.get("priceUsd", 0) or 0)
                mcap = float(pair.get("fdv", 0) or 0)
                psymbol = (symbol or (pair.get("baseToken") or {}).get("symbol") or "TOKEN")

                if not token_amount and px:
                    token_amount = usd / px
                token_str = fmt_amount(token_amount) if token_amount is not None else "—"

                title = f"{chosen_emoji} | {psymbol} BUY!"
                body = (
                    f"🔷 ~USD {fmt_usd(usd)}\n"
                    f"🪙 {psymbol} {token_str}\n"
                    f"🔎 Position: New Holder [Unknown]({tx_url})\n"
                    f"📈 MCap: {fmt_usd(mcap)}{socials_txt}\n"
                    f"[Tx]({tx_url})"
                )
                text = f"{title}\n{body}"

                buttons = [
                    [InlineKeyboardButton("📊 Dex", url=dexs), InlineKeyboardButton("💲 Buy", url=jup)],
                    [InlineKeyboardButton("Txn", url=tx_url)],
                ]
                markup = InlineKeyboardMarkup(buttons)

                try:
                    if media_file_id:
                        await context.bot.send_photo(
                            chat_id, photo=media_file_id, caption=text, reply_markup=markup,
                            parse_mode="Markdown", disable_web_page_preview=True
                        )
                    else:
                        await context.bot.send_message(
                            chat_id, text, reply_markup=markup, parse_mode="Markdown", disable_web_page_preview=True
                        )
                except Exception as e:
                    logging.error(f"Fallback send failed: {e}")

        except Exception as e:
            logging.error(f"Fallback poll error for {mint}: {e}")

# ---------------- DM FLOW STATE ----------------
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
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Use /track inside a group to configure via DM.")
        return

    me = await context.bot.get_me()
    code = _gen_pair_code()
    _put_code(code, origin_chat_id=chat.id, user_id=user.id)

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

# ---------------- DM GATE (HIGH PRIORITY) ----------------
PAIR_RX = re.compile(r"^\s*track\s+(\d{6})\s*$", re.IGNORECASE)
ANY_CODE_RX = re.compile(r"\b(\d{6})\b")

async def buy_dm_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.message
    if not chat or chat.type != "private" or not msg or not (msg.text or msg.caption):
        return

    uid = update.effective_user.id
    state = PENDING_DM.get(uid)
    if not state:
        return

    entities = (msg.entities or []) + (msg.caption_entities or [])
    if any(getattr(e, "type", "") == "bot_command" for e in entities) or (msg.text and msg.text.strip().startswith("/")):
        await msg.reply_text("Please send the text only (no /commands).")
        raise ApplicationHandlerStop

    if state.get("stage") == "await_code":
        text = (msg.text or msg.caption or "").strip()
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
        state["stage"] = "ask_mint"
        await msg.reply_text("🧭 Send the <b>mint address</b> you want to track.", parse_mode="HTML")
        raise ApplicationHandlerStop

    await dm_text_router(update, context)
    raise ApplicationHandlerStop

# ---------------- DM ENTRY via "track <code)" ----------------
async def dm_entry_by_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.message
    if chat.type != "private" or not msg or not msg.text:
        return

    m = PAIR_RX.match(msg.text)
    if not m:
        return

    uid = update.effective_user.id
    code = m.group(1)
    origin_chat_id = _pop_valid_code(code, uid)
    if not origin_chat_id:
        await msg.reply_text("❌ Invalid or expired code. Go back to your group and run /track again.")
        return

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
        return

    stage = state.get("stage")
    origin = state.get("origin_chat_id")

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

# ---------------- DM MEDIA HANDLER ----------------
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

# ---------------- CALLBACKS ----------------
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
        PENDING_DM.pop(uid, None)
        return

    if data == "bt:set:done":
        if not mint:
            await q.message.reply_text("Missing token context. Please start over with /track in your group.")
            PENDING_DM.pop(uid, None)
        else:
            set_active(origin, mint, True)
            row = get_token_row(origin, mint)
            # best-effort symbol refresh
            symbol = (row[1] if row else None) or (await fetch_token_info(mint)).get("symbol") or "TOKEN"
            try:
                await context.bot.send_message(
                    origin,
                    f"✅ SentriBot Buy Tracker has been successfully activated!\nTracking <b>{symbol}</b> (<code>{short_mint(mint)}</code>).",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            await q.message.reply_text("All set! Alerts will post in your group when buys meet your settings.")
            # Kick WS subscribe for this token now
            await prime_ws_for_chat_token(origin, mint)
            PENDING_DM.pop(uid, None)
        return

# ---------------- LEGACY COMMANDS ----------------
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

# ---------------- WS STARTUP / PRIMING ----------------
async def prime_ws_for_chat_token(chat_id: int, mint: str):
    row = get_token_row(chat_id, mint)
    if not row:
        return
    (rmint, symbol, media, emoji, supply, min_buy, socials_json, active) = row
    if not active:
        return
    payload = {
        "chat_id": chat_id,
        "mint": rmint,
        "symbol": symbol or "TOKEN",
        "emoji": emoji or DEFAULT_EMOJI,
        "min_buy_usd": float(min_buy or DEFAULT_MIN_BUY_USD),
        "socials_json": socials_json or "{}",
        "media_file_id": media
    }
    await ws_manager.add_or_update_token(payload)

async def ws_bootstrap(context: ContextTypes.DEFAULT_TYPE):
    global ws_context
    ws_context = context  # allow ws_manager to send via bot
    await ws_manager.ensure_connected()

    # Subscribe all active tokens
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT chat_id, mint FROM tracked_tokens WHERE active=1""")
    rows = c.fetchall()
    conn.close()

    for chat_id, mint in rows:
        await prime_ws_for_chat_token(chat_id, mint)

# ---------------- REGISTER ----------------
def register_buytracker(app):
    init_db()

    # GROUP command
    app.add_handler(CommandHandler("track", cmd_track_group, filters.ChatType.GROUPS))

    # DM entry via "track <code>" — VERY high priority
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, dm_entry_by_code), group=-300)

    # High-priority DM gate for buy tracker (accept text + media w/ captions)
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL),
            buy_dm_gate
        ),
        group=-250
    )

    # DM callbacks & text/media during configuration
    app.add_handler(CallbackQueryHandler(bt_callback, pattern=r"^bt:(confirm|again|set:.*)"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, dm_text_router))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO | filters.Document.ALL), dm_media_router))

    # Optional legacy group utilities
    app.add_handler(CommandHandler("untrack", cmd_untrack))
    app.add_handler(CommandHandler("list", cmd_list))

    # Start WS once app is up
    app.job_queue.run_once(ws_bootstrap, when=2)

    # Failsafe low-frequency poller (kept, but you can remove if you want pure WS)
    app.job_queue.run_repeating(fallback_poll, interval=60, first=10)
