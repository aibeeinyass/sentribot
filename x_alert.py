# x_alert.py
import os
import sqlite3
import logging
import aiohttp
from typing import Optional, Tuple, List, Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, MessageHandler, ContextTypes, filters

# ---- TOKEN SANITIZER (fix header injection error) ----
def _clean_token(tok: Optional[str]) -> str:
    if not tok:
        return ""
    tok = tok.replace("\r", "").replace("\n", "").strip()
    # strip accidental surrounding quotes
    if (tok.startswith('"') and tok.endswith('"')) or (tok.startswith("'") and tok.endswith("'")):
        tok = tok[1:-1].strip()
    return tok

RAW_X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN")
X_BEARER_TOKEN = _clean_token(RAW_X_BEARER_TOKEN)

DB_PATH = "tracked_tokens.db"
logging.basicConfig(level=logging.INFO)

# ========= DB =========
def init_x_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS x_accounts (
            chat_id INTEGER,
            handle TEXT,        -- lowercase without @
            user_id TEXT,       -- X user id
            display TEXT,       -- cached name/display
            PRIMARY KEY (chat_id, handle)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS x_followers (
            user_id TEXT,       -- tracked account's X user id
            follower_id TEXT,   -- follower X user id
            PRIMARY KEY (user_id, follower_id)
        )
    """)
    conn.commit()
    conn.close()

def x_add_account(chat_id: int, handle: str, user_id: str, display: Optional[str]):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO x_accounts (chat_id, handle, user_id, display) VALUES (?, ?, ?, ?)",
        (chat_id, handle.lower(), user_id, display),
    )
    conn.commit()
    conn.close()

def x_remove_account(chat_id: int, handle: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # also clear follower cache for this account
    c.execute("SELECT user_id FROM x_accounts WHERE chat_id=? AND handle=?", (chat_id, handle.lower()))
    row = c.fetchone()
    if row:
        c.execute("DELETE FROM x_followers WHERE user_id=?", (row[0],))
    c.execute("DELETE FROM x_accounts WHERE chat_id=? AND handle=?", (chat_id, handle.lower()))
    conn.commit()
    conn.close()

def x_list_accounts(chat_id: int) -> List[Tuple[str, str, str]]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT handle, user_id, display FROM x_accounts WHERE chat_id=?", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def x_has_follower(tracked_user_id: str, follower_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM x_followers WHERE user_id=? AND follower_id=?", (tracked_user_id, follower_id))
    ok = c.fetchone() is not None
    conn.close()
    return ok

def x_add_follower(tracked_user_id: str, follower_id: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO x_followers (user_id, follower_id) VALUES (?, ?)", (tracked_user_id, follower_id))
    conn.commit()
    conn.close()

# ========= X API =========
X_API_BASE = "https://api.twitter.com/2"

HEADERS = {
    "Authorization": f"Bearer {X_BEARER_TOKEN}" if X_BEARER_TOKEN else "",
    "User-Agent": "Sentribot/1.0",
}

async def _x_get_json(url: str, params: dict | None = None):
    """Return (json_or_none, error_text_or_none, status_code)."""
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=HEADERS, params=params or {}) as r:
            status = r.status
            try:
                data = await r.json()
            except Exception:
                txt = await r.text()
                return None, f"HTTP {status}: {txt[:200]}", status
            if status != 200:
                err_msg = ""
                if isinstance(data, dict) and "errors" in data:
                    try:
                        err_msg = "; ".join(
                            f"{e.get('title','')}: {e.get('detail','')}" for e in data["errors"]
                        )
                    except Exception:
                        err_msg = str(data)[:200]
                if not err_msg:
                    err_msg = str(data)[:200]
                return None, f"HTTP {status}: {err_msg}", status
            return data, None, status

async def x_get_user_by_handle(handle: str):
    """Returns (data_dict, error_text). data_dict has {'id','name','username'} when OK."""
    handle = handle.lstrip("@")
    url = f"{X_API_BASE}/users/by/username/{handle}"
    params = {"user.fields": "name,username"}
    data, err, _ = await _x_get_json(url, params)
    if err:
        return None, err
    return data.get("data"), None

async def x_get_followers(user_id: str, max_results: int = 200):
    """Returns (followers_list, error_text)."""
    url = f"{X_API_BASE}/users/{user_id}/followers"
    params = {
        "max_results": str(max(10, min(max_results, 1000))),
        "user.fields": "name,username,verified,profile_image_url,public_metrics",
    }
    data, err, _ = await _x_get_json(url, params)
    if err:
        return [], err
    return (data.get("data", []) or []), None

# ========= HELPERS =========
def fmt_num(n):
    try:
        f = float(n or 0)
        if f < 1000:
            return str(int(f))
        return f"{int(f):,}"
    except Exception:
        return str(n)

# ========= COMMANDS =========
async def x_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Always answer something, even on unexpected errors
    try:
        if not X_BEARER_TOKEN:
            await update.message.reply_text("❌ X alerts are not configured. Missing X_BEARER_TOKEN.")
            return

        # Arg parsing with raw-text fallback
        text = (update.message.text or "").strip()
        parts = text.split(maxsplit=1)
        handle = None
        if len(parts) > 1 and parts[1].strip():
            handle = parts[1].strip().lstrip("@")
        elif context.args:
            handle = context.args[0].strip().lstrip("@")

        if not handle:
            await update.message.reply_text("❌ Usage: /x_track <handle>")
            return

        chat_id = update.effective_chat.id

        # Lookup the user on X
        user, err = await x_get_user_by_handle(handle)
        if err:
            await update.message.reply_text(f"❌ X lookup failed for @{handle}: {err}")
            return
        if not user:
            await update.message.reply_text(f"❌ Couldn’t find that X handle: @{handle}")
            return

        user_id = user.get("id")
        display = user.get("name") or handle

        # Save account
        x_add_account(chat_id, handle, user_id, display)

        # Seed follower cache (avoid spamming historical followers)
        followers, f_err = await x_get_followers(user_id, max_results=200)
        if f_err:
            logging.error(f"Seeding followers failed for @{handle}: {f_err}")
        else:
            for f in followers:
                fid = f.get("id")
                if fid:
                    x_add_follower(user_id, fid)

        await update.message.reply_text(f"✅ Now watching @{handle} for new followers.")

    except Exception as e:
        logging.exception(f"/x_track crashed: {e}")
        await update.message.reply_text("❌ Something went wrong while setting X alerts. Check logs and your token.")

async def x_untrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /x_untrack <handle>")
        return
    handle = context.args[0].strip().lstrip("@")
    chat_id = update.effective_chat.id
    x_remove_account(chat_id, handle)
    await update.message.reply_text(f"🗑 Stopped watching @{handle}.")

async def x_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = x_list_accounts(chat_id)
    if not rows:
        await update.message.reply_text("No X accounts tracked.")
        return
    lines = []
    for handle, user_id, display in rows:
        lines.append(f"• {display} (@{handle}) — id: {user_id}")
    await update.message.reply_text("🐦 X accounts watched:\n" + "\n".join(lines))

async def x_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    masked = "present" if X_BEARER_TOKEN else "missing"
    has_newlines = "\\n" if RAW_X_BEARER_TOKEN and any(c in RAW_X_BEARER_TOKEN for c in "\r\n") else "no-nl"
    head = (X_BEARER_TOKEN[:8] + "…") if X_BEARER_TOKEN else "—"
    await update.message.reply_text(f"🔎 X token: {masked} ({has_newlines}), starts with: {head}")

async def x_testuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not X_BEARER_TOKEN:
        await update.message.reply_text("❌ X alerts are not configured. Missing X_BEARER_TOKEN.")
        return
    text = (update.message.text or "").strip()
    parts = text.split(maxsplit=1)
    handle = parts[1].strip().lstrip("@") if len(parts) > 1 else None
    if not handle:
        await update.message.reply_text("❌ Usage: /x_testuser <handle>")
        return
    url = f"{X_API_BASE}/users/by/username/{handle}"
    params = {"user.fields": "name,username"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=HEADERS, params=params) as r:
                body = await r.text()
                preview = (body[:600] + "…") if len(body) > 600 else body
                await update.message.reply_text(
                    f"HTTP {r.status}\nURL: {url}\nAuth header ok: {bool(X_BEARER_TOKEN)}\n\nBody:\n{preview}"
                )
    except Exception as e:
        await update.message.reply_text(f"Exception during call: {e}")

# ========= POLLING JOB =========
async def poll_x_followers(context: ContextTypes.DEFAULT_TYPE):
    if not X_BEARER_TOKEN:
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id, handle, user_id, display FROM x_accounts")
    rows = c.fetchall()
    conn.close()

    for chat_id, handle, user_id, display in rows:
        try:
            followers, err = await x_get_followers(user_id, max_results=200)
            if err or not followers:
                if err:
                    logging.error(f"X followers fetch failed for @{handle}: {err}")
                continue

            for f in followers:
                fid = f.get("id")
                if not fid:
                    continue
                if x_has_follower(user_id, fid):
                    continue  # already seen

                # New follower!
                x_add_follower(user_id, fid)

                fname = f.get("name") or ""
                fuser = f.get("username") or ""
                verified = " ✅" if f.get("verified") else ""
                metrics = f.get("public_metrics") or {}
                f_count = fmt_num(metrics.get("followers_count", 0))
                p_count = fmt_num(metrics.get("tweet_count", 0))

                title = f"🐦 New Follower for @{handle}!"
                text = (
                    f"{title}\n\n"
                    f"{fname}{verified} (@{fuser}) just followed {display} (@{handle}).\n"
                    f"Followers: {f_count} | Posts: {p_count}"
                )

                profile_url = f"https://x.com/{fuser}" if fuser else f"https://x.com/i/user/{fid}"
                acct_url = f"https://x.com/{handle}"
                sentrip_url = "https://x.com/Sentrip_Bot"

                buttons = [
                    [
                        InlineKeyboardButton("View Follower", url=profile_url),
                        InlineKeyboardButton(f"@{handle}", url=acct_url),
                    ],
                    [InlineKeyboardButton("Follow Sentrip on X", url=sentrip_url)],
                ]
                markup = InlineKeyboardMarkup(buttons)

                await context.bot.send_message(chat_id, text, reply_markup=markup)

        except Exception as e:
            logging.error(f"X poll error for @{handle}: {e}")

# ========= REGISTER =========
def register_x_alert(app):
    init_x_db()
    app.add_handler(CommandHandler("x_track", x_track))
    app.add_handler(CommandHandler("x_untrack", x_untrack))
    app.add_handler(CommandHandler("x_list", x_list))
    app.add_handler(CommandHandler("x_debug", x_debug))
    app.add_handler(CommandHandler("x_testuser", x_testuser))
    app.job_queue.run_repeating(poll_x_followers, interval=120, first=10)
