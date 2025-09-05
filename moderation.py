# modules/moderation.py
import os, json, logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    CallbackQueryHandler,
    ContextTypes,
    ApplicationHandlerStop,  # <-- ADDED
    filters,
)

# -------- SETTINGS --------
DEFAULT_WELCOME = "🎉 Welcome {name} to the group! Please read the rules."
warn_limit = 3
warnings: Dict[int, int] = {}  # Store warnings per user

# In-memory caches (persisted to JSON)
welcome_messages: Dict[int, str] = {}         # chat_id -> str
rules_texts: Dict[int, str] = {}              # chat_id -> str
filters_map: Dict[int, Dict[str, str]] = {}   # chat_id -> {trigger: reply}
known_chats: Dict[int, str] = {}              # chat_id -> title

# Pending interactive states
PENDING_WELCOME_DM: Dict[int, int] = {}  # user_id -> target_chat_id
PENDING_RULES_DM: Dict[int, int] = {}    # user_id -> target_chat_id
PENDING_FILTER_REPLY: Dict[int, Dict[str, Any]] = {}  # chat_id -> {trigger, user_id}

# Add-to-group URL with admin scopes (Telegram decides the UI ticks)
ADD_TO_GROUP_URL = (
    "https://telegram.me/sentrip_bot"
    "?startgroup=true"
    "&admin=change_info+delete_messages+ban_users+invite_users+pin_messages"
)

# -------- Namespaced persistence (set after bot username is known) --------
DATA_BASE = Path("data")
DATA_DIR = DATA_BASE  # will be overwritten by _namespace_data()
PATH_CHATS   = DATA_BASE / "chats.json"
PATH_WELCOME = DATA_BASE / "welcome.json"
PATH_RULES   = DATA_BASE / "rules.json"
PATH_FILTERS = DATA_BASE / "filters.json"
PATH_ACTIVITY = DATA_BASE / "activity.log"

def _namespace_data(bot_username: str):
    """Point JSON/log paths to data/<bot_username> and (re)load caches."""
    global DATA_DIR, PATH_CHATS, PATH_WELCOME, PATH_RULES, PATH_FILTERS, PATH_ACTIVITY
    DATA_DIR = DATA_BASE / bot_username
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PATH_CHATS   = DATA_DIR / "chats.json"
    PATH_WELCOME = DATA_DIR / "welcome.json"
    PATH_RULES   = DATA_DIR / "rules.json"
    PATH_FILTERS = DATA_DIR / "filters.json"
    PATH_ACTIVITY = DATA_DIR / "activity.log"

    def _load_json(path: Path, default):
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logging.warning("Failed loading %s: %s", path, e)
        return default

    # Reload caches from namespaced files (don’t clear; overlay)
    known_chats.update({int(k): v for k, v in _load_json(PATH_CHATS, {}).items()})
    welcome_messages.update({int(k): v for k, v in _load_json(PATH_WELCOME, {}).items()})
    rules_texts.update({int(k): v for k, v in _load_json(PATH_RULES, {}).items()})
    filters_map.update({int(k): v for k, v in _load_json(PATH_FILTERS, {}).items()})

def _save_json(path: Path, data):
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error("Failed saving %s: %s", path, e)
        raise

def _remember_chat(chat_id: int, title: str):
    if not title:
        title = str(chat_id)
    known_chats[chat_id] = title
    _save_json(PATH_CHATS, {str(k): v for k, v in known_chats.items()})

async def _is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        cm = await context.bot.get_chat_member(chat_id, user_id)
        return cm.status in ("creator", "administrator")
    except Exception:
        return False

# -------- HELP MENU (HTML; commands left plain so they’re tappable) --------
def _render_help_section(section: str) -> Tuple[str, InlineKeyboardMarkup]:
    section = (section or "menu").lower()
    rows = [
        [
            InlineKeyboardButton("👋 General", callback_data="help:general"),
            InlineKeyboardButton("🟢 Buy", callback_data="help:buy"),
        ],
        [
            InlineKeyboardButton("🔴 Sell", callback_data="help:sell"),
            InlineKeyboardButton("🐦 X Alerts", callback_data="help:x"),
        ],
    ]
    if section != "menu":
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="help:menu")])
    kb = InlineKeyboardMarkup(rows)

    if section == "general":
        text = (
            "<b>✨ SentriBot — General</b>\n\n"
            "/start — Greet the bot\n"
            "/rules — Show rules\n"
            "/about — About the bot\n"
            "/setwelcome — Set welcome message (DM flow)\n"
            "/setrules — Set /rules text (DM flow)\n"
            "/filter &lt;trigger&gt; — Add a filter (interactive)\n"
            "/filters — List filters\n"
            "/delfilter &lt;trigger&gt; — Delete a filter\n"
            "/warn — Warn a user (reply)\n"
            "/pin — Pin the latest message\n"
        )
    elif section == "buy":
        text = (
            "<b>🟢 Buy Tracker</b>\n\n"
            "/track &lt;mint&gt; — Start buy tracking\n"
            "/untrack &lt;mint&gt; — Stop buy tracking\n"
            "/list — List tracked tokens\n"
            "/skip &lt;txsig&gt; — Ignore a transaction\n"
        )
    elif section == "sell":
        text = (
            "<b>🔴 Sell Tracker</b>\n\n"
            "/track_sell &lt;mint&gt; — Start sell tracking\n"
            "/sell_skip — Skip media for last /track_sell\n"
            "/untrack_sell &lt;mint&gt; — Stop sell tracking\n"
            "/list_sells — List tracked tokens (with whale threshold)\n"
            "/sellthreshold &lt;mint&gt; &lt;usd&gt; — Set whale alert threshold\n"
        )
    elif section == "x":
        text = (
            "<b>🐦 X Alerts</b>\n\n"
            "/x_track &lt;handle&gt; — Track new followers for an account\n"
            "/x_untrack &lt;handle&gt; — Stop tracking\n"
            "/x_list — List tracked X accounts\n"
            "/x_debug — Check X API token status\n"
            "/x_testuser &lt;handle&gt; — Test lookup (debug)\n\n"
            "<i>Followers are checked every 2 minutes.</i>\n"
        )
    else:
        text = "<b>✨ SentriBot Help</b>\nTap a category below to see commands."
    return text, kb

async def help_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, section = (q.data.split(":", 1) + ["menu"])[:2]
    text, kb = _render_help_section(section)
    try:
        await q.edit_message_text(text=text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await q.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

# -------- START / CONTINUE / CONFIG MENU --------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    DM:
      - If payload cfg_welcome_* or cfg_rules_*: start that DM flow.
      - Else: ALWAYS show intro + buttons: [Add me] [Configure groups]
    Group:
      - Hint to /help
    """
    chat = update.effective_chat
    user = update.effective_user
    args = context.args or []

    if chat and chat.type == "private":
        # Handle deep-link payloads first
        if args:
            payload = args[0]
            if payload.startswith("cfg_welcome_"):
                try:
                    target = int(payload.replace("cfg_welcome_", "", 1))
                    PENDING_WELCOME_DM[user.id] = target
                    title = known_chats.get(target, str(target))
                    await update.message.reply_text(
                        f"✏️ Send the <b>new welcome message</b> for <b>{title}</b> now.\n\n"
                        "Note: I will always tag the new member first.",
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    pass
            if payload.startswith("cfg_rules_"):
                try:
                    target = int(payload.replace("cfg_rules_", "", 1))
                    PENDING_RULES_DM[user.id] = target
                    title = known_chats.get(target, str(target))
                    await update.message.reply_text(
                        f"✏️ Send the <b>new /rules text</b> for <b>{title}</b> now.",
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    pass

        # No payload → ALWAYS show intro (no auto config list)
        text = (
            "🎩 <b>Welcome to SentriBot!</b>\n"
            "Your private, project-only community monitoring & alerts bot.\n\n"
            "<b>What you get:</b>\n"
            "• Real-time Buy & Sell alerts (wallet + TX links)\n"
            "• X follower alerts (track your project account)\n"
            "• Member joins/leaves, spam control, keyword tracking\n"
            "• Fully private — you control every feature\n\n"
            "<b>How to start:</b>\n"
            "Add SentriBot to your group as <b>Admin</b> with <b>write + pin + delete + ban + invite</b> permissions.\n"
            "Make sure all admin permissions are <b>enabled</b> before confirming.\n\n"
            "<i>If no confirmation appears after adding, type</i> /continue <i>in your group.</i>\n\n"
            "Work with SentriBot? DM @brhm_sol"
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Add me to your group", url=ADD_TO_GROUP_URL)],
                [InlineKeyboardButton("⚙️ Configure groups", callback_data="cfgmenu")],
            ]
        )
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
        return

    # In groups
    await update.message.reply_text("Hi! Use /help to see what I can do.")

async def cfgmenu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the groups list only when user taps 'Configure groups' in DM."""
    q = update.callback_query
    await q.answer()
    if not known_chats:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("➕ Add me to your group", url=ADD_TO_GROUP_URL)]]
        )
        await q.message.reply_text("No groups yet. Add me to a group first:", reply_markup=kb)
        return

    rows: List[List[InlineKeyboardButton]] = []
    for cid, title in list(known_chats.items())[:20]:
        rows.append([InlineKeyboardButton(f"⚙️ Set Welcome: {title}", callback_data=f"cfgpick:welcome:{cid}")])
    for cid, title in list(known_chats.items())[:20]:
        rows.append([InlineKeyboardButton(f"⚙️ Set Rules: {title}", callback_data=f"cfgpick:rules:{cid}")])
    kb = InlineKeyboardMarkup(rows)
    await q.message.reply_text("Select a group to configure:", reply_markup=kb)

async def continue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = _render_help_section("menu")
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    section = (context.args[0] if context.args else "menu")
    text, kb = _render_help_section(section)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    txt = rules_texts.get(chat_id) or (
        "📜 Group Rules:\n"
        "1. Be respectful\n"
        "2. No spam or ads\n"
        "3. Keep chats friendly"
    )
    await update.message.reply_text(txt)

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Welcome to SentriBot</b> — Your private community monitoring and insights assistant.\n\n"
        "📊 <b>With SentriBot, you can:</b>\n"
        "• Track member activity and engagement.\n"
        "• Get alerts when members join or leave.\n"
        "• Monitor keywords and detect mood changes in chats.\n"
        "• Watch for mentions of your token or ticker on X.\n"
        "• Receive blockchain whale and wallet activity alerts.\n"
        "• Get notified when someone follows your X account.\n\n"
        "🔒 <b>You control all data.</b> SentriBot is private and built for your project.",
        parse_mode="HTML"
    )

# -------- ADMIN COMMANDS --------
async def set_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type in ("group", "supergroup"):
        if not await _is_admin(context, chat.id, user.id):
            await update.message.reply_text("❌ Only admins can set the welcome message.")
            return
        deep = f"https://t.me/sentrip_bot?start=cfg_welcome_{chat.id}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Open in DM to set welcome", url=deep)]])
        await update.message.reply_text(
            "Open me in a private chat to set the welcome message for this group.",
            reply_markup=kb
        )
        return
    await update.message.reply_text("Use /start → “⚙️ Configure groups” to pick a group.")

async def set_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type in ("group", "supergroup"):
        if not await _is_admin(context, chat.id, user.id):
            await update.message.reply_text("❌ Only admins can set /rules.")
            return
        deep = f"https://t.me/sentrip_bot?start=cfg_rules_{chat.id}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Open in DM to set /rules", url=deep)]])
        await update.message.reply_text(
            "Open me in a private chat to set the /rules text for this group.",
            reply_markup=kb
        )
        return
    await update.message.reply_text("Use /start → “⚙️ Configure groups” to pick a group.")

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Reply to a user's message to warn them.")
        return
    user = update.message.reply_to_message.from_user
    uid = user.id
    warnings[uid] = warnings.get(uid, 0) + 1
    await update.message.reply_text(f"⚠ {user.first_name} has been warned! ({warnings[uid]}/{warn_limit})")
    if warnings[uid] >= warn_limit:
        await update.message.chat.ban_member(uid)
        await update.message.reply_text(f"🚫 {user.first_name} was banned after too many warnings.")

async def pin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        await update.message.reply_to_message.pin()
        await update.message.reply_text("📌 Message pinned!")
    else:
        await update.message.reply_text("❌ Reply to a message to pin it.")

# -------- AUTO FEATURES (message-based) --------
async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    message_template = welcome_messages.get(chat_id, DEFAULT_WELCOME)
    for member in update.message.new_chat_members:
        if member.is_bot:
            await update.message.chat.ban_member(member.id)
            await update.message.reply_text(f"🤖 Bot {member.first_name} was removed.")
            return
        await update.message.reply_text(
            message_template.format(name=member.mention_html()),
            parse_mode="HTML"
        )
        await log_activity(f"User joined: {member.full_name}")

async def goodbye(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.left_chat_member:
        await update.message.reply_text(f"👋 Goodbye {update.message.left_chat_member.full_name}!")
        await log_activity(f"User left: {update.message.left_chat_member.full_name}")

# -------- ChatMember updates (users) --------
def _status_change(old, new):
    try:
        return (old.status != new.status) or (old.is_member != new.is_member)
    except Exception:
        return True

async def user_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu:
        return
    old, new = cmu.old_chat_member, cmu.new_chat_member
    if not _status_change(old, new):
        return

    joined = (not getattr(old, "is_member", False) and getattr(new, "is_member", False)) or (
        old.status in ("left", "kicked") and new.status in ("member", "administrator", "creator")
    )
    left = (getattr(old, "is_member", False) and not getattr(new, "is_member", False)) or (
        new.status in ("left", "kicked")
    )

    if joined:
        chat = cmu.chat
        _remember_chat(chat.id, chat.title or str(chat.id))
        user = cmu.from_user
        if user.is_bot:
            await context.bot.ban_chat_member(chat.id, user.id)
            await context.bot.send_message(chat.id, f"🤖 Bot {user.first_name} was removed.")
        else:
            template = welcome_messages.get(chat.id, DEFAULT_WELCOME)
            await context.bot.send_message(chat.id, template.format(name=user.mention_html()), parse_mode="HTML")
            await log_activity(f"User joined: {user.full_name}")
    elif left:
        user = cmu.from_user
        await context.bot.send_message(cmu.chat.id, f"👋 Goodbye {user.full_name}!")
        await log_activity(f"User left: {user.full_name}")

# -------- My Chat Member updates (bot itself added/removed) --------
async def my_bot_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mcu = update.my_chat_member
    if not mcu:
        return
    old, new = mcu.old_chat_member, mcu.new_chat_member
    became_member = new.status in ("member", "administrator")
    was_outside = old.status in ("left", "kicked") or not getattr(old, "is_member", False)

    chat = mcu.chat
    if chat:
        _remember_chat(chat.id, chat.title or str(chat.id))

    if became_member and was_outside:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Continue", callback_data="help:menu")]])
        await context.bot.send_message(
            chat_id=mcu.chat.id,
            text="✅ <b>SentriBot added to the group successfully!</b>\n\nTap <b>Continue</b> to open the help menu and see everything I can do.",
            reply_markup=kb,
            parse_mode="HTML",
        )

# -------- FILTERS (group triggers) --------
async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Use /filter inside a group.")
        return
    if not await _is_admin(context, chat.id, user.id):
        await update.message.reply_text("❌ Only admins can set filters.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /filter <trigger>")
        return
    trigger = " ".join(context.args).strip().lower()
    if not trigger:
        await update.message.reply_text("Trigger cannot be empty.")
        return
    PENDING_FILTER_REPLY[chat.id] = {"trigger": trigger, "user_id": user.id}
    await update.message.reply_text(f"✏️ Send the reply text for trigger <b>{trigger}</b> now.", parse_mode="HTML")

async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Use /filters inside a group.")
        return
    fmap = filters_map.get(chat.id, {})
    if not fmap:
        await update.message.reply_text("📭 No filters set in this group.")
        return
    lines = [f"• {k}" for k in sorted(fmap.keys())]
    await update.message.reply_text("🗂 Filters\n" + "\n".join(lines))

async def cmd_delfilter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Use /delfilter inside a group.")
        return
    if not await _is_admin(context, chat.id, user.id):
        await update.message.reply_text("❌ Only admins can delete filters.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /delfilter <trigger>")
        return
    trigger = " ".join(context.args).strip().lower()
    fmap = filters_map.setdefault(chat.id, {})
    if trigger in fmap:
        del fmap[trigger]
        _save_json(PATH_FILTERS, {str(k): v for k, v in filters_map.items()})
        await update.message.reply_text(f"🗑 Removed filter <b>{trigger}</b>.", parse_mode="HTML")
    else:
        await update.message.reply_text("Not found.")

async def handle_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pending filter replies + trigger matches."""
    msg = update.message
    if not msg or not (msg.text or msg.caption):
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return

    # 1) Pending filter reply?
    pending = PENDING_FILTER_REPLY.get(chat.id)
    if pending and pending.get("user_id") == update.effective_user.id and msg.text:
        trigger = pending["trigger"]
        reply = msg.text
        fmap = filters_map.setdefault(chat.id, {})
        fmap[trigger] = reply
        _save_json(PATH_FILTERS, {str(k): v for k, v in filters_map.items()})
        del PENDING_FILTER_REPLY[chat.id]
        await msg.reply_text(f"✅ Saved filter for <b>{trigger}</b>.", parse_mode="HTML")
        return

    # 2) Trigger match (exact, case-insensitive)
    text = (msg.text or "").strip().lower()
    fmap = filters_map.get(chat.id, {})
    if text and text in fmap:
        await msg.reply_text(fmap[text], disable_web_page_preview=True)

# -------- DM text capture for welcome/rules flows --------
async def handle_dm_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    chat = update.effective_chat
    user = update.effective_user
    if chat.type != "private":
        return

    # Welcome flow
    target_chat = PENDING_WELCOME_DM.get(user.id)
    if target_chat:
        text = msg.text.strip()
        final = "{name} " + text  # ensure tag first
        welcome_messages[target_chat] = final
        try:
            _save_json(PATH_WELCOME, {str(k): v for k, v in welcome_messages.items()})
            title = known_chats.get(target_chat, str(target_chat))
            await msg.reply_text(f"✅ Welcome message set for <b>{title}</b>.", parse_mode="HTML")
        except Exception as e:
            await msg.reply_text(f"⚠️ Failed to save welcome: <code>{e}</code>", parse_mode="HTML")
        finally:
            PENDING_WELCOME_DM.pop(user.id, None)
        return

    # Rules flow
    target_rules = PENDING_RULES_DM.get(user.id)
    if target_rules:
        text = msg.text.strip()
        rules_texts[target_rules] = text
        try:
            _save_json(PATH_RULES, {str(k): v for k, v in rules_texts.items()})
            title = known_chats.get(target_rules, str(target_rules))
            await msg.reply_text(f"✅ /rules updated for <b>{title}</b>.", parse_mode="HTML")
        except Exception as e:
            await msg.reply_text(f"⚠️ Failed to save rules: <code>{e}</code>", parse_mode="HTML")
        finally:
            PENDING_RULES_DM.pop(user.id, None)
        return

# -------- HIGH-PRIORITY DM GATE (new) --------
async def dm_pending_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Intercept DM while we're waiting for welcome/rules text, and block others."""
    chat = update.effective_chat
    msg  = update.message
    if not chat or chat.type != "private" or not msg or not msg.text:
        return  # not our case

    uid = update.effective_user.id
    pending = (uid in PENDING_WELCOME_DM) or (uid in PENDING_RULES_DM)
    if not pending:
        return  # let other handlers process normally

    # Guard against commands/forwards in DM while pending
    if any(e.type == "bot_command" for e in (msg.entities or [])) or msg.text.strip().startswith("/"):
        await msg.reply_text("Please send the text only (no /commands).")
        raise ApplicationHandlerStop

    if getattr(msg, "forward_origin", None) or getattr(msg, "forward_date", None) or getattr(msg, "forward_from_chat", None):
        await msg.reply_text("Please type the message — don’t forward/quote.")
        raise ApplicationHandlerStop

    # Route straight to the saver and block others
    await handle_dm_text(update, context)
    raise ApplicationHandlerStop

# Optional exit for DM flow
async def cancel_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    PENDING_WELCOME_DM.pop(uid, None)
    PENDING_RULES_DM.pop(uid, None)
    await update.message.reply_text("✖️ Cancelled. No changes saved.")

# -------- Config pick & menu callbacks (DM) --------
async def cfgpick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    try:
        _, kind, cid = data.split(":", 2)
        cid = int(cid)
    except Exception:
        return
    title = known_chats.get(cid, str(cid))
    if kind == "welcome":
        PENDING_WELCOME_DM[q.from_user.id] = cid
        await q.message.reply_text(
            f"✏️ Send the <b>new welcome message</b> for <b>{title}</b> now.\n\n"
            "Note: I will always tag the new member first.",
            parse_mode="HTML"
        )
    elif kind == "rules":
        PENDING_RULES_DM[q.from_user.id] = cid
        await q.message.reply_text(
            f"✏️ Send the <b>new /rules text</b> for <b>{title}</b> now.",
            parse_mode="HTML"
        )

# -------- SPAM DETECTION --------
SPAM_KEYWORDS = os.getenv("SPAM_KEYWORDS", "")
SPAM_KEYWORDS = [w.strip().lower() for w in SPAM_KEYWORDS.split(",") if w.strip()]
if not SPAM_KEYWORDS:
    logging.info("SPAM_KEYWORDS is empty; spam detection is effectively disabled.")

def _normalize_spaces(s: str) -> str:
    return " ".join(s.split()).lower()

async def detect_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    content = (msg.text or msg.caption or "")
    content_norm = _normalize_spaces(content)
    if not content_norm or not SPAM_KEYWORDS:
        return
    matched = None
    for kw in SPAM_KEYWORDS:
        if kw and kw in content_norm:
            matched = kw
            break
    if matched:
        try:
            await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
            await context.bot.send_message(chat_id=msg.chat_id, text=f"🚫 Spam detected ({matched}) from {msg.from_user.first_name}")
            # Reuse warn flow
            await warn_user(update, context)
        except Exception as e:
            logging.warning("Spam delete failed: %s", e)

# -------- LOGGING & UTILS --------
async def log_activity(text):
    try:
        with PATH_ACTIVITY.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now()}] {text}\n")
    except Exception as e:
        logging.warning("Failed to write activity log: %s", e)

async def cmd_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admins only: /activity [N] -> show last N lines (default 50)."""
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Use this in a group you admin.")
        return
    if not await _is_admin(context, chat.id, user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    try:
        n = int(context.args[0]) if context.args else 50
        n = max(1, min(n, 500))
    except Exception:
        n = 50
    if not PATH_ACTIVITY.exists():
        await update.message.reply_text("No activity yet.")
        return
    try:
        with PATH_ACTIVITY.open("r", encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        text = "".join(lines) or "No activity."
        # Send as a file if long
        if len(text) > 3500:
            tmp = DATA_DIR / "activity_tail.txt"
            with tmp.open("w", encoding="utf-8") as f:
                f.write(text)
            await update.message.reply_document(tmp)
        else:
            await update.message.reply_text(f"Last {n} lines:\n\n<pre>{text}</pre>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed to read activity: <code>{e}</code>", parse_mode="HTML")

async def cmd_spamtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DM only: /spamtest <text> -> shows which keyword would match."""
    chat = update.effective_chat
    if chat.type != "private":
        return
    sample = " ".join(context.args) if context.args else ""
    if not sample:
        await update.message.reply_text("Usage: /spamtest your sample text")
        return
    content_norm = _normalize_spaces(sample)
    hits = [kw for kw in SPAM_KEYWORDS if kw and kw in content_norm]
    if hits:
        await update.message.reply_text("Matched keywords: " + ", ".join(hits))
    else:
        await update.message.reply_text("No matches.")

# -------- REGISTRATION (called from main.py) --------
def register_moderation(app: Application):
    # Post-init: set namespaced storage once bot username is known
    async def _post_init(application: Application):
        me = await application.bot.get_me()
        _namespace_data(me.username)

    app.post_init = _post_init

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("continue", continue_cmd))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("about", about))

    # Editable commands
    app.add_handler(CommandHandler("setwelcome", set_welcome))
    app.add_handler(CommandHandler("setrules", set_rules))

    # Filters
    app.add_handler(CommandHandler("filter", cmd_filter))
    app.add_handler(CommandHandler("filters", cmd_filters))
    app.add_handler(CommandHandler("delfilter", cmd_delfilter))

    # Moderation
    app.add_handler(CommandHandler("warn", warn_user))
    app.add_handler(CommandHandler("pin", pin_message))
    app.add_handler(CommandHandler("activity", cmd_activity))
    app.add_handler(CommandHandler("spamtest", cmd_spamtest))

    # Help menu + Config menu callbacks
    app.add_handler(CallbackQueryHandler(help_menu_cb, pattern=r"^help:"))
    app.add_handler(CallbackQueryHandler(cfgmenu_cb, pattern=r"^cfgmenu$"))
    app.add_handler(CallbackQueryHandler(cfgpick_cb, pattern=r"^cfgpick:(welcome|rules):\d+$"))

    # Auto actions
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, goodbye))

    # Group text handler first (filters & interactive replies), then spam
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_text))
    app.add_handler(MessageHandler((filters.TEXT | filters.Caption()) & ~filters.COMMAND, detect_spam))
    group=-10
   
   )
    # Membership updates
    app.add_handler(ChatMemberHandler(user_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(my_bot_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

    # ===== HIGH-PRIORITY DM GATE & CANCEL (NEW) =====
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, dm_pending_gate),
        group=-100,  # run first, stop others if pending
    )
    app.add_handler(CommandHandler("cancel", cancel_dm))

    # DM text capture (normal DM traffic that isn't pending)
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, handle_dm_text))
