# systematic_bot.py
import telebot
from telebot import types
import sqlite3
from datetime import datetime, timedelta
import time
import threading
import logging
import re
import os
from flask import Flask, request, abort

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8213222692:AAGQPfCzQpCKspfHy9SKd8zsWFxuZlvAYKA")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6506705983"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@pratik_cmd")
# Webhook配置
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://your-domain.com")  # 你的域名
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", 443))  # 通常443或8443
WEBHOOK_LISTEN = os.getenv("WEBHOOK_LISTEN", "0.0.0.0")  # 监听所有IP
WEBHOOK_URL_BASE = f"{WEBHOOK_HOST}:{WEBHOOK_PORT}"
WEBHOOK_URL_PATH = f"/{BOT_TOKEN}/"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ---------------- DATABASE ----------------
# (保持原有的数据库代码不变)
conn = sqlite3.connect("systematic_promo.db", check_same_thread=False)
cur = conn.cursor()

# Users table
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    joined TEXT,
    active INTEGER DEFAULT 0,
    plan TEXT DEFAULT '',
    plan_expiry TEXT DEFAULT '',
    referral TEXT,
    wallet INTEGER DEFAULT 0,
    referred_by INTEGER DEFAULT NULL
)
""")

# Saved materials
cur.execute("""
CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    text_data TEXT,
    created_at TEXT
)
""")

# Registered groups where bot is admin (chat_id unique)
cur.execute("""
CREATE TABLE IF NOT EXISTS groups (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    registered_by INTEGER,
    registered_at TEXT
)
""")

# referrals (to ensure one-time credit)
cur.execute("""
CREATE TABLE IF NOT EXISTS referrals (
    new_user_id INTEGER PRIMARY KEY,
    referrer_user_id INTEGER,
    credited INTEGER DEFAULT 0,
    created_at TEXT
)
""")

# saved selections per user (selected groups for promotion)
cur.execute("""
CREATE TABLE IF NOT EXISTS selections (
    user_id INTEGER PRIMARY KEY,
    group_ids TEXT  -- comma separated
)
""")

conn.commit()

# ---------------- HELPERS ----------------
# (保持所有原有的辅助函数不变)
def save_user(user, ref_code=None):
    """
    Save new user. If ref_code is provided (format REF{user_id}) then set referred_by.
    """
    cur.execute("SELECT 1 FROM users WHERE user_id=?", (user.id,))
    if cur.fetchone():
        return

    ref = f"REF{user.id}"
    referred_by = None
    if ref_code:
        m = re.match(r"REF(\d+)", ref_code)
        if m:
            referred_by = int(m.group(1))

    cur.execute(
        "INSERT INTO users(user_id, username, first_name, joined, referral, referred_by) VALUES(?,?,?,?,?,?)",
        (user.id, user.username or "", user.first_name or "", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), ref, referred_by)
    )
    conn.commit()

    # If referred_by is present, create a referral record (credited later immediately if possible)
    if referred_by:
        try:
            cur.execute("INSERT OR IGNORE INTO referrals(new_user_id, referrer_user_id, credited, created_at) VALUES(?,?,?,?)",
                        (user.id, referred_by, 0, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
        except Exception as e:
            logging.exception("referral create failed: %s", e)


def get_user(uid):
    cur.execute("SELECT user_id,username,first_name,joined,active,plan,plan_expiry,referral,wallet,referred_by FROM users WHERE user_id=?", (uid,))
    row = cur.fetchone()
    if not row:
        return None
    keys = ["user_id","username","first_name","joined","active","plan","plan_expiry","referral","wallet","referred_by"]
    return dict(zip(keys, row))

def update_user(uid, field, value):
    cur.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, uid))
    conn.commit()

def add_wallet(uid, amount):
    u = get_user(uid)
    if not u:
        return False
    new = (u['wallet'] or 0) + int(amount)
    update_user(uid, "wallet", new)
    return True

def save_material(uid, text):
    cur.execute("INSERT INTO materials(user_id, text_data, created_at) VALUES(?,?,?)", (uid, text, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()

def get_materials(uid):
    cur.execute("SELECT id, text_data, created_at FROM materials WHERE user_id=? ORDER BY id DESC", (uid,))
    return cur.fetchall()

def delete_material(uid, mid=None):
    if mid:
        cur.execute("DELETE FROM materials WHERE user_id=? AND id=?", (uid, mid))
    else:
        cur.execute("DELETE FROM materials WHERE user_id=?", (uid,))
    conn.commit()

def register_group_record(chat_id, title, registered_by):
    cur.execute("INSERT OR REPLACE INTO groups(chat_id,title,registered_by,registered_at) VALUES(?,?,?,?)",
                (chat_id, title or "", registered_by, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()

def get_registered_groups():
    cur.execute("SELECT chat_id, title FROM groups ORDER BY title")
    return cur.fetchall()

def remove_group(chat_id):
    cur.execute("DELETE FROM groups WHERE chat_id=?", (chat_id,))
    conn.commit()

def save_selection(user_id, group_ids):
    gid_str = ",".join([str(int(x)) for x in group_ids]) if group_ids else ""
    cur.execute("INSERT OR REPLACE INTO selections(user_id, group_ids) VALUES(?,?)", (user_id, gid_str))
    conn.commit()

def get_selection(user_id):
    cur.execute("SELECT group_ids FROM selections WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        return []
    if not row[0]:
        return []
    return [int(x) for x in row[0].split(",") if x.strip()]

def process_pending_referral(new_user_id):
    """
    If a referral record exists for this new user and not credited, credit referrer 10 points once.
    """
    cur.execute("SELECT referrer_user_id, credited FROM referrals WHERE new_user_id=?", (new_user_id,))
    row = cur.fetchone()
    if not row:
        return False
    referrer_id, credited = row
    if credited:
        return False
    # Add 10 points to referrer wallet
    add_wallet(referrer_id, 10)
    cur.execute("UPDATE referrals SET credited=1 WHERE new_user_id=?", (new_user_id,))
    conn.commit()
    try:
        bot.send_message(referrer_id, f"🎉 You earned <b>10 points</b> because a new user joined using your referral! Your wallet updated.", parse_mode="HTML")
    except Exception:
        logging.info("Could not notify referrer %s", referrer_id)
    return True

def bot_is_admin_in(chat_id):
    """
    Check whether the bot is admin in a chat (returns True/False)
    """
    try:
        member = bot.get_chat_member(chat_id, bot.get_me().id)
        # statuses: 'administrator', 'creator', 'member', 'left', 'kicked'
        return member.status in ("administrator", "creator")
    except Exception as e:
        logging.debug("get_chat_member failed for %s: %s", chat_id, e)
        return False

# ---------------- KEYBOARDS ----------------
# (保持所有原有的键盘函数不变)
def main_menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📊 My Account", "💰 Wallet & Referral")
    kb.row("📝 Materials", "🚀 Promotion Panel")
    kb.row("💳 Subscription Plans", "❓ Support")
    return kb

def materials_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Save New Text", callback_data="mat_save"))
    kb.add(types.InlineKeyboardButton("📦 View Saved Texts", callback_data="mat_view"),
           types.InlineKeyboardButton("🗑 Clear Materials", callback_data="mat_clear"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

def wallet_ref_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💰 Wallet Balance", callback_data="wallet_balance"))
    kb.add(types.InlineKeyboardButton("👥 Referral Link", callback_data="ref_link"),
           types.InlineKeyboardButton("🏆 Referral Stats", callback_data="ref_stats"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

def promotion_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📌 Show Registered Groups", callback_data="prom_show_groups"))
    kb.add(types.InlineKeyboardButton("📤 Start Promotion", callback_data="prom_start"))
    kb.add(types.InlineKeyboardButton("♻️ Clear Selected Groups", callback_data="prom_clear"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

def support_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Contact Admin", callback_data="contact_admin"))
    kb.add(types.InlineKeyboardButton("Bot Status", callback_data="bot_status"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

# ---------------- HANDLERS ----------------
# (保持所有原有的处理函数不变)
@bot.message_handler(commands=["start"])
def start(m):
    # parse referral param if present
    ref_code = None
    args = m.text.split(maxsplit=1)
    if len(args) > 1:
        ref_code = args[1].strip()
    save_user(m.from_user, ref_code)
    # process referral credit if any
    process_pending_referral(m.from_user.id)

    kb = main_menu_kb()
    bot.send_message(m.chat.id, f"Welcome <b>{m.from_user.first_name}</b>!\n\nUse the menu below to manage account, wallet, promotion and saved materials.", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "📊 My Account")
def my_account(m):
    u = get_user(m.from_user.id)
    if not u:
        bot.send_message(m.chat.id, "User not found (please send /start)")
        return
    status = "ACTIVE ✔" if u["active"] else "INACTIVE ❌"
    bot.send_message(m.chat.id,
                     f"📊 <b>Account</b>\n\n"
                     f"👤 Name: {u['first_name']}\n"
                     f"🆔 ID: {u['user_id']}\n"
                     f"📛 Username: @{u['username'] if u['username'] else 'none'}\n"
                     f"📅 Joined: {u['joined']}\n"
                     f"📌 Status: {status}\n"
                     f"💎 Plan: {u['plan']}\n"
                     f"⏳ Expiry: {u['plan_expiry']}\n"
                     f"💰 Wallet: {u['wallet']} points")
    
@bot.message_handler(func=lambda m: m.text == "💳 Subscription Plans")
def subscription(m):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("1 Week - $2", callback_data="plan_1W"))
    kb.add(types.InlineKeyboardButton("1 Month - $6", callback_data="plan_1M"))
    kb.add(types.InlineKeyboardButton("3 Months - $15", callback_data="plan_3M"),
           types.InlineKeyboardButton("1 Year - $30", callback_data="plan_1Y"))
    bot.send_message(m.chat.id, "Choose a plan (contact admin to purchase):", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "💰 Wallet & Referral")
def wallet_ref(m):
    bot.send_message(m.chat.id, "Wallet & Referral Menu:", reply_markup=wallet_ref_kb())

@bot.message_handler(func=lambda m: m.text == "📝 Materials")
def materials(m):
    bot.send_message(m.chat.id, "Materials Menu:", reply_markup=materials_kb())

@bot.message_handler(func=lambda m: m.text == "🚀 Promotion Panel")
def promotion(m):
    u = get_user(m.from_user.id)
    if not u or not u['active']:
        bot.send_message(m.chat.id, f"❌ Subscription inactive.\nContact admin: {ADMIN_USERNAME}")
        return
    bot.send_message(m.chat.id, "Promotion Panel:", reply_markup=promotion_kb())

@bot.message_handler(func=lambda m: m.text == "❓ Support")
def support(m):
    bot.send_message(m.chat.id, "Support:", reply_markup=support_kb())

# ---------------- INLINE CALLBACKS ----------------
@bot.callback_query_handler(func=lambda c: True)
def cb_handler(c):
    data = c.data
    uid = c.from_user.id

    # ----- materials -----
    if data == "mat_save":
        msg = bot.send_message(c.message.chat.id, "Send me the text you want to save (I will store it):")
        bot.register_next_step_handler(msg, handle_save_text)
        bot.answer_callback_query(c.id)

    elif data == "mat_view":
        rows = get_materials(uid)
        if not rows:
            bot.send_message(uid, "No saved texts.")
            bot.answer_callback_query(c.id)
            return
        for r in rows:
            text_preview = (r[1][:600] + "...") if len(r[1])>600 else r[1]
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("Send Now", callback_data=f"sendmat_{r[0]}"))
            kb.add(types.InlineKeyboardButton("Delete", callback_data=f"delmat_{r[0]}"))
            bot.send_message(uid, f"📝 <b>Saved #{r[0]}</b>\n{r[2]}\n\n{text_preview}", reply_markup=kb)
        bot.answer_callback_query(c.id)

    elif data.startswith("delmat_"):
        mid = int(data.split("_",1)[1])
        delete_material(uid, mid)
        bot.send_message(uid, f"Deleted material #{mid}.")
        bot.answer_callback_query(c.id)

    elif data.startswith("sendmat_"):
        mid = int(data.split("_",1)[1])
        cur.execute("SELECT text_data FROM materials WHERE id=? AND user_id=?", (mid, uid))
        row = cur.fetchone()
        if not row:
            bot.send_message(uid, "Material not found.")
            bot.answer_callback_query(c.id)
            return
        bot.send_message(uid, row[0])
        bot.answer_callback_query(c.id)

    elif data == "mat_clear":
        delete_material(uid)
        bot.send_message(uid, "All your saved materials have been cleared.")
        bot.answer_callback_query(c.id)

    # ----- wallet/ref -----
    elif data == "wallet_balance":
        u = get_user(uid)
        bot.send_message(uid, f"💰 Your wallet balance: <b>{u['wallet']} points</b>")
        bot.answer_callback_query(c.id)

    elif data == "ref_link":
        u = get_user(uid)
        link = f"https://t.me/{bot.get_me().username}?start={u['referral']}"
        bot.send_message(uid, f"👥 Your Referral Link:\n{link}\n\nShare it — you'll get 10 points for each new user who joins with your link (credited once per user).")
        bot.answer_callback_query(c.id)

    elif data == "ref_stats":
        cur.execute("SELECT COUNT(*) FROM referrals WHERE referrer_user_id=?",(uid,))
        total = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(*) FROM referrals WHERE referrer_user_id=? AND credited=1",(uid,))
        credited = cur.fetchone()[0] or 0
        bot.send_message(uid, f"Referral stats:\nTotal referred (joined): {total}\nCredited: {credited}")
        bot.answer_callback_query(c.id)

    # ----- promotion -----
    elif data == "prom_show_groups":
        groups = get_registered_groups()
        if not groups:
            bot.send_message(uid, "No groups registered yet. Invite the bot to a group and run /register_group in that group or ask admin to add groups.")
            bot.answer_callback_query(c.id)
            return
        kb = types.InlineKeyboardMarkup()
        for gid, title in groups:
            label = f"{title or str(gid)}"
            kb.add(types.InlineKeyboardButton(label, callback_data=f"selgroup_{gid}"))
        kb.add(types.InlineKeyboardButton("✅ Confirm selection", callback_data="sel_confirm"))
        kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
        bot.send_message(uid, "Select groups to toggle selection (press again to deselect):", reply_markup=kb)
        bot.answer_callback_query(c.id)

    elif data.startswith("selgroup_"):
        gid = int(data.split("_",1)[1])
        sel = set(get_selection(uid))
        if gid in sel:
            sel.remove(gid)
        else:
            sel.add(gid)
        save_selection(uid, list(sel))
        bot.answer_callback_query(c.id, text="Updated selection.")

    elif data == "sel_confirm":
        sel = get_selection(uid)
        if not sel:
            bot.send_message(uid, "No groups selected. Use the group buttons to select groups first.")
            bot.answer_callback_query(c.id)
            return
        bot.send_message(uid, f"Selected {len(sel)} groups for promotion. When you start promotion the bot will check admin permission on each group before sending.")
        bot.answer_callback_query(c.id)

    elif data == "prom_clear":
        save_selection(uid, [])
        bot.send_message(uid, "Cleared selected groups.")
        bot.answer_callback_query(c.id)

    elif data == "prom_start":
        sel = get_selection(uid)
        if not sel:
            bot.send_message(uid, "No groups selected. Select groups first.")
            bot.answer_callback_query(c.id)
            return
        rows = get_materials(uid)
        if not rows:
            bot.send_message(uid, "You have no saved materials to promote. Save texts first.")
            bot.answer_callback_query(c.id)
            return

        # Ask for which material to send or "all"
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("📌 Send ALL saved texts", callback_data="prom_send_all"))
        for r in rows[:10]:
            kb.add(types.InlineKeyboardButton(f"Send #{r[0]}", callback_data=f"prom_send_{r[0]}"))
        bot.send_message(uid, "Choose which material to promote:", reply_markup=kb)
        bot.answer_callback_query(c.id)

    elif data == "prom_send_all" or data.startswith("prom_send_"):
        uid = c.from_user.id
        sel = get_selection(uid)
        if not sel:
            bot.send_message(uid, "No groups selected.")
            bot.answer_callback_query(c.id)
            return

        # choose messages
        messages = []
        if data == "prom_send_all":
            mats = get_materials(uid)
            messages = [r[1] for r in mats]
        else:
            mid = int(data.split("_",2)[2])
            cur.execute("SELECT text_data FROM materials WHERE id=? AND user_id=?", (mid, uid))
            row = cur.fetchone()
            if not row:
                bot.send_message(uid, "Material not found.")
                bot.answer_callback_query(c.id)
                return
            messages = [row[0]]

        bot.send_message(uid, f"Starting promotion to {len(sel)} groups. The bot will verify admin permission before sending and will skip where not admin.")
        bot.answer_callback_query(c.id)
        # run promotion in a thread so not block the callback (still single process)
        threading.Thread(target=perform_promotion, args=(uid, sel, messages)).start()

    # ----- support -----
    elif data == "contact_admin":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Contact Admin via Telegram", url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}"))
        bot.send_message(uid, "Contact admin:", reply_markup=kb)
        bot.answer_callback_query(c.id)

    elif data == "bot_status":
        bot.send_message(uid, "Bot is running.")
        bot.answer_callback_query(c.id)

    # ----- plans -----
    elif data.startswith("plan_"):
        code = data.split("_",1)[1]
        plans = {"1W":7,"1M":30,"3M":90,"1Y":365}
        names = {"1W":"1 Week ($2)", "1M":"1 Month ($6)", "3M":"3 Months ($15)", "1Y":"1 Year ($30)"}
        if code not in plans:
            bot.send_message(uid, "Invalid plan.")
            bot.answer_callback_query(c.id)
            return
        bot.send_message(uid, f"You selected <b>{names[code]}</b>. Contact admin to purchase: {ADMIN_USERNAME}")
        bot.answer_callback_query(c.id)

    elif data == "back_main":
        bot.send_message(uid, "Back to main menu.", reply_markup=main_menu_kb())
        bot.answer_callback_query(c.id)

    else:
        bot.answer_callback_query(c.id, text="Unknown callback.")

# ---------------- PROMOTION WORKER ----------------
def perform_promotion(user_id, group_ids, messages):
    """
    For each group, check if bot is admin; if yes, send messages sequentially.
    """
    total_groups = len(group_ids)
    success_count = 0
    fail_count = 0

    for gid in group_ids:
        try:
            # verify the bot is admin
            if not bot_is_admin_in(gid):
                bot.send_message(user_id, f"Skipping {gid}: bot is not admin or cannot access the group.")
                fail_count += 1
                continue

            # Attempt to send all messages
            any_sent = False
            for m in messages:
                try:
                    bot.send_message(gid, m)
                    any_sent = True
                    time.sleep(1)  # small delay to avoid flood
                except Exception as e:
                    logging.warning("Failed to send to %s: %s", gid, e)
                    # if a message fails in this group, continue with next message
                    continue

            if any_sent:
                success_count += 1
            else:
                fail_count += 1

        except Exception as e:
            logging.exception("Error during promotion to %s: %s", gid, e)
            fail_count += 1

    bot.send_message(user_id, f"Promotion finished.\nSuccessful groups: {success_count}\nFailed/Skipped groups: {fail_count}\nTotal attempted: {total_groups}")

# ---------------- ADMIN COMMANDS ----------------
@bot.message_handler(commands=["active"])
def admin_activate(m):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        parts = m.text.split()
        if len(parts) != 3:
            bot.send_message(m.chat.id, "Usage: /active <PLAN_CODE> <USER_ID>\nPlan codes: 1W,1M,3M,1Y")
            return
        _, code, uid = parts
        uid = int(uid)
        plans = {"1W":7,"1M":30,"3M":90,"1Y":365}
        if code not in plans:
            bot.send_message(m.chat.id, "Invalid plan code.")
            return
        expiry = datetime.utcnow() + timedelta(days=plans[code])
        expiry_str = expiry.strftime("%Y-%m-%d %H:%M:%S")
        update_user(uid, "active", 1)
        update_user(uid, "plan", code)
        update_user(uid, "plan_expiry", expiry_str)
        bot.send_message(m.chat.id, f"Activated {uid}\nPlan: {code}\nExpiry (UTC): {expiry_str}")
    except Exception as e:
        bot.send_message(m.chat.id, f"Error: {e}")

@bot.message_handler(commands=["stats"])
def admin_stats(m):
    if m.from_user.id != ADMIN_ID:
        return
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE active=1")
    active_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM groups")
    total_groups = cur.fetchone()[0]
    bot.send_message(m.chat.id, f"Users total: {total_users}\nActive users: {active_users}\nRegistered groups: {total_groups}")

# register_group should be run inside the group to register it safely
@bot.message_handler(commands=["register_group"])
def register_group(m):
    chat = m.chat
    # only allow in groups and supergroups
    if chat.type not in ("group","supergroup"):
        bot.reply_to(m, "This command must be run inside the group you want to register.")
        return

    # check bot admin status
    try:
        member = bot.get_chat_member(chat.id, bot.get_me().id)
        if member.status not in ("administrator", "creator"):
            bot.reply_to(m, "Bot is not an administrator in this group. Make the bot admin and then run /register_group again.")
            return
    except Exception as e:
        bot.reply_to(m, f"Failed to verify bot admin status: {e}")
        return

    register_group_record(chat.id, chat.title or "", m.from_user.id)
    bot.reply_to(m, f"Group registered: {chat.title or chat.id}. You can now select it in Promotion Panel.")

@bot.message_handler(commands=["addgroup"])
def add_group_command(m):
    """
    Admin-only: /addgroup <chat_id>
    Attempts to verify bot admin status and registers the group in DB.
    """
    if m.from_user.id != ADMIN_ID:
        return
    parts = m.text.split()
    if len(parts) != 2:
        bot.reply_to(m, "Usage: /addgroup <chat_id>")
        return
    try:
        gid = int(parts[1])
        # try to get chat info
        try:
            info = bot.get_chat(gid)
            # verify bot is admin
            if not bot_is_admin_in(gid):
                bot.reply_to(m, "Bot is not admin in that chat or cannot access it.")
                return
            register_group_record(gid, info.title or str(gid), m.from_user.id)
            bot.reply_to(m, f"Group added: {info.title or gid}")
        except Exception as e:
            bot.reply_to(m, f"Failed to add group: {e}")
    except:
        bot.reply_to(m, "Invalid chat id.")

@bot.message_handler(commands=["removegroup"])
def remove_group_command(m):
    if m.from_user.id != ADMIN_ID:
        return
    parts = m.text.split()
    if len(parts) != 2:
        bot.reply_to(m, "Usage: /removegroup <chat_id>")
        return
    try:
        gid = int(parts[1])
        remove_group(gid)
        bot.reply_to(m, f"Removed group {gid} from registry.")
    except:
        bot.reply_to(m, "Invalid chat id.")

# ---------------- SAVE TEXT HANDLER ----------------
def handle_save_text(m):
    txt = m.text.strip() if m.text else ""
    if not txt:
        bot.send_message(m.chat.id, "Empty text. Cancelled.")
        return
    save_material(m.from_user.id, txt)
    total = len(get_materials(m.from_user.id))
    bot.send_message(m.chat.id, f"Saved! Total saved texts: {total}")

# ---------------- CATCH-ALL FOR RAW TEXT (saving) ----------------
@bot.message_handler(func=lambda m: True, content_types=["text"])
def catch_all_save(m):
    # ignore commands and menu texts
    menu_texts = {"📊 My Account","💳 Subscription Plans","💰 Wallet & Referral","📝 Materials","🚀 Promotion Panel","💳 Subscription Plans","❓ Support"}
    if m.text in menu_texts or m.text.startswith("/"):
        return
    # Save as material automatically
    save_material(m.from_user.id, m.text)
    bot.reply_to(m, f"Saved automatically! Total saved texts: {len(get_materials(m.from_user.id))}")

# ---------------- WEBHOOK ENDPOINTS ----------------
@app.route(WEBHOOK_URL_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    else:
        abort(403)

@app.route('/')
def index():
    return 'Bot is running!'

@app.route('/set_webhook', methods=['GET', 'POST'])
def set_webhook():
    try:
        # 移除现有webhook
        bot.remove_webhook()
        time.sleep(0.1)
        
        # 设置新的webhook
        s = bot.set_webhook(url=WEBHOOK_HOST + WEBHOOK_URL_PATH)
        
        if s:
            return f"Webhook setup successful! URL: {WEBHOOK_HOST + WEBHOOK_URL_PATH}"
        else:
            return "Webhook setup failed"
    except Exception as e:
        return f"Error setting webhook: {str(e)}"

@app.route('/remove_webhook', methods=['GET', 'POST'])
def remove_webhook():
    try:
        s = bot.remove_webhook()
        if s:
            return "Webhook removed successfully"
        else:
            return "Failed to remove webhook"
    except Exception as e:
        return f"Error removing webhook: {str(e)}"

# ---------------- START WEBHOOK ----------------
if __name__ == "__main__":
    logging.info("Bot started in webhook mode.")
    
    # 启动时自动设置webhook
    try:
        bot.remove_webhook()
        time.sleep(0.5)
        bot.set_webhook(url=WEBHOOK_HOST + WEBHOOK_URL_PATH)
        logging.info(f"Webhook set to: {WEBHOOK_HOST + WEBHOOK_URL_PATH}")
    except Exception as e:
        logging.error(f"Failed to set webhook: {e}")
    
    # 启动Flask应用
    app.run(host=WEBHOOK_LISTEN, port=WEBHOOK_PORT)
