import asyncio
import secrets
import string
import logging
import html
import time
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError, BadRequest

import database as db
import razorpay_api as rzp
import ffglory_api as ff
import keyauth as ka
from config import (
    BOT_TOKEN, ADMIN_ID, BACKUP_CHANNEL_ID, PRICE_INR,
    TELEGRAM_TIMEOUT, POLL_SECONDS, FIREBASE_DB_URL
)

log = logging.getLogger("tg")

# ============ CONFIG ============
QR_EXPIRE_SECONDS = 5 * 60
TIMER_UPDATE_EVERY = 30
PAYMENT_DONE_AUTO_DELETE = 5
AUTO_DELETE_MESSAGES = True


# ============ FIREBASE HELPER ============
def save_web_user_to_firebase(username, password):
    """Firebase Realtime DB me user save karta hai."""
    try:
        url = f"{FIREBASE_DB_URL}/web_users/{username}.json"
        payload = {"password": password, "created_at": int(time.time())}
        r = requests.put(url, json=payload, timeout=10)
        return r.ok
    except Exception as e:
        log.exception(f"Firebase save error: {e}")
        return False


# ============ BACKUP SENDER ============

_backup_app = None
_backup_warned = False


async def _backup_sender(payload: dict):
    global _backup_warned
    if not BACKUP_CHANNEL_ID:
        return
    try:
        import json
        text = "📦 <b>BACKUP</b>\n<pre>" + json.dumps(payload, ensure_ascii=False)[:3500] + "</pre>"
        await _backup_app.bot.send_message(BACKUP_CHANNEL_ID, text, parse_mode="HTML")
    except Exception as e:
        msg = str(e)
        if "Chat not found" in msg or "not enough rights" in msg or "bot was blocked" in msg:
            if not _backup_warned:
                log.warning("Backup channel invalid — backup disabled (%s)", msg[:120])
                _backup_warned = True
        else:
            log.exception("backup send failed")


# ============ AUTO-DELETE HELPER ============

def _schedule_delete(bot, chat_id: int, message_id: int, delay: int = PAYMENT_DONE_AUTO_DELETE):
    if not AUTO_DELETE_MESSAGES:
        return
    async def _do_delete():
        await asyncio.sleep(delay)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
    try:
        asyncio.create_task(_do_delete())
    except RuntimeError:
        pass


# ============ KEYBOARDS ============

def main_menu_kb(uid, is_admin=False, chat_id=None):
    rows = [
        [InlineKeyboardButton("🛒 Shop Now", callback_data="shop")],
        [InlineKeyboardButton("👤 Profile", callback_data="profile"),
         InlineKeyboardButton("🔑 My Keys", callback_data="mykeys")],
        [InlineKeyboardButton("💰 Add Balance", callback_data="addbal"),
         InlineKeyboardButton("💬 Feedback", callback_data="feedback")],
        [InlineKeyboardButton("🆘 Admin Support", callback_data="support")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def back_kb(target="home"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=target)]])


# ============ UI HELPERS ============

async def _edit_text(q, text, kb=None):
    try:
        await q.edit_message_text(text=text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        return True
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return True
        try:
            await q.message.delete()
        except Exception:
            pass
        try:
            await q.message.reply_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            pass
        return True
    except Exception:
        log.exception("edit_text fail")
        try:
            await q.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
        return False


# ============ ERROR HANDLER ============

async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, (TimedOut, NetworkError)):
        log.warning(f"Network issue detected: {context.error}")
        return
    log.error(f"Update {update} caused error {context.error}", exc_info=context.error)


# ============ START ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db.upsert_user(u.id, "tg", u.username or "", u.full_name or "")
    is_admin = (u.id == ADMIN_ID)
    chat_id = update.effective_chat.id

    text = (
        f"👋 <b>Welcome, {u.full_name}!</b>\n\n"
        "⭐️ ━ <b>SHOP FEATURES</b> ━ ⭐️\n\n"
        "├ 🔑 Premium Game Keys\n"
        "├ ⚡️ Instant Delivery 24/7\n"
        "├ 🔒 100% Secure Payment\n"
        "├ 💸 Best Prices Guaranteed\n"
        "├ 🎁 Referral Rewards\n"
        "└ 🏆 Professional Support\n\n"
        "  ━━━━━━━━━━━━━━━━━━\n"
        "🚀 <b>Click Shop Now to Start!</b>"
    )
    if update.callback_query:
        await _edit_text(update.callback_query, text, main_menu_kb(u.id, is_admin, chat_id))
    else:
        await update.effective_message.reply_text(text, reply_markup=main_menu_kb(u.id, is_admin, chat_id), parse_mode="HTML")


# ============ CALLBACK ROUTER ============

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
        
    uid = q.from_user.id
    is_admin = (uid == ADMIN_ID)
    chat_id = q.message.chat.id
    data = q.data

    if data == "home":
        await start(update, context)
    elif data == "shop":
        await _show_shop(q, context)
    elif data.startswith("prod:"):
        await _product_selected(q, context, data.split(":", 1)[1])
    elif data.startswith("durationopt:"):
        option_id = data.split(":", 1)[1]
        await _handle_duration_option_selected(q, context, option_id)
    elif data.startswith("duration:"):
        _, days, product_id = data.split(":", 2)
        await _handle_duration_selected(q, context, int(days), product_id)
    elif data.startswith("resetkey:"):
        await _reset_key(q, context, data.split(":", 1)[1])
    elif data.startswith("region:"):
        await _region_selected(q, context, data.split(":", 1)[1])
    elif data.startswith("pay_opt:"):
        parts = data.split(":")
        await _handle_payment_option(q, context, parts[1], parts[2])
    elif data == "profile":
        await _show_profile(q)
    elif data == "mykeys":
        await _show_mykeys(q, uid)
    elif data == "addbal":
        context.user_data["awaiting_amount"] = True
        await _edit_text(q, "💰 <b>Add Balance</b>\n\nKitna amount add karna hai? Sirf number bhejo (₹).", back_kb())
    elif data == "feedback":
        context.user_data["awaiting_feedback"] = True
        await _edit_text(q, "💬 <b>Feedback</b>\n\nApna feedback / screenshot bhejo. Admin tak pahunchega.", back_kb())
    elif data == "support":
        text = (f"🆘 <b>Admin Support</b>\n\nAdmin: <a href='tg://user?id={ADMIN_ID}'>Tap to chat</a>\nID: <code>{ADMIN_ID}</code>\n\nApni problem likho, admin ko message chala jayega.")
        await _edit_text(q, text, back_kb())
    elif data.startswith("checkpay:"):
        await _check_payment_manual(q, context, data.split(":", 1)[1])
    
    # ============ MAIN ADMIN PANEL ============
    elif data == "admin" and is_admin:
        await _show_admin_panel(q, context)
    elif data.startswith("adm:web_login") and is_admin:
        context.user_data["awaiting_web_user"] = {"step": "username"}
        await _edit_text(q, "🔐 <b>Create Web Login</b>\n\nWeb panel ke liye ek <b>Username</b> bhejo:", back_kb("admin"))
    elif data.startswith("adm:manage_web") and is_admin:
        await _show_manage_web_users(q, context)
    elif data.startswith("adm:del_web:") and is_admin:
        username = data.split(":", 2)[2]
        await _delete_web_user(q, context, username)
    elif data == "adm:guildglory:toggle" and is_admin:
        enabled = db.get_setting("guild_glory_enabled", "1") != "1"
        db.set_setting("guild_glory_enabled", "1" if enabled else "0")
        await _show_admin_panel(q, context, notice=("Guild Glory ordering resumed." if enabled else "Guild Glory ordering paused."))
    elif data == "adm:retry_key" and is_admin:
        context.user_data["awaiting_keyauth_retry"] = True
        await _edit_text(q, "🔁 <b>Retry Key Delivery</b>\n\nPaid order ka Order ID bhejein jisme KeyAuth key deliver nahi hui.", back_kb("admin"))


# ============ SHOP ============

async def _show_shop(q, context):
    products = db.list_products(enabled_only=True)
    if not products:
        await _edit_text(q, "Abhi koi product available nahi hai.", back_kb())
        return

    header = "🛒 <b>Shop Now</b>\n\n<b>Products:</b>"
    buttons = []
    for p in products:
        ptype = str(p["product_type"] or ("guild_glory" if p["is_default"] else "keyauth")).lower()
        price_label = f"₹{p['price_paise']//100}/day" if ptype == "keyauth" else f"₹{p['price_paise']//100}"
        label = f"⭐ {p['name']} — {price_label}" if p["is_default"] else f"• {p['name']} — {price_label}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"prod:{p['product_id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])

    await _edit_text(q, header, InlineKeyboardMarkup(buttons))


async def _product_selected(q, context, product_id):
    p = db.get_product(product_id)
    if not p:
        await _edit_text(q, "Product nahi mila.", back_kb("shop"))
        return
    product = dict(p)
    context.user_data["product"] = product
    product_type = str(product.get("product_type") or ("guild_glory" if product.get("is_default") else "keyauth")).lower()

    if product_type == "keyauth":
        if not product.get("keyauth_api_key"):
            await _edit_text(q, "❌ Is product ka KeyAuth API key configured nahi hai. Admin se contact karein.", back_kb("shop"))
            return
        options = [dict(o) for o in db.list_product_duration_options(product_id)]
        if not options:
            await _edit_text(q, "❌ Is product ke duration options set nahi hain. Admin website se options add karein.", back_kb("shop"))
            return
        buttons = []
        for option in options:
            days = int(option.get("duration_days") or 1)
            label = str(option.get("label") or ("Lifetime" if days >= 36500 else f"{days} Days"))
            buttons.append([InlineKeyboardButton(f"{label} · ₹{int(option.get('price_paise') or 0)//100}", callback_data=f"durationopt:{option['option_id']}")])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="shop")])
        await _edit_text(q,
            f"🔑 <b>{product['name']}</b>\n\n{product.get('description') or 'KeyAuth License'}\n\n"
            f"Validity / plan choose karein:", InlineKeyboardMarkup(buttons))
        return

    if (product.get("is_default") or product_type == "guild_glory") and db.get_setting("guild_glory_enabled", "1") != "1":
        await _edit_text(q, "⏸ <b>Guild Glory temporarily stopped hai.</b>\nAbhi naye Guild Glory orders accept nahi ho rahe.", back_kb("shop"))
        return
    if product.get("is_default") or product_type == "guild_glory":
        await _edit_text(q, f"⏳ <b>{product['name']}</b> ke regions load ho rahe hain...", None)
        try:
            regions = await asyncio.to_thread(ff.get_regions)
        except Exception as e:
            await _edit_text(q, f"❌ Region load fail: {str(e)[:300]}", back_kb("shop"))
            return
        if not regions:
            await _edit_text(q, "Koi region enabled nahi hai.", back_kb("shop"))
            return
        context.user_data["regions"] = {str(r["id"]): r for r in regions}
        buttons = [[InlineKeyboardButton(f"{r.get('region_name', r['id'])} · {str(r.get('tier','')).upper()}", callback_data=f"region:{r['id']}")] for r in regions]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="shop")])
        await _edit_text(q, f"🌍 <b>{product['name']}</b>\n\n💰 Price: <b>₹{int(product['price_paise'])//100}</b>\n\nRegion choose karo:", InlineKeyboardMarkup(buttons))
    else:
        await _edit_text(q, "❌ Unknown product provider. Admin se contact karein.", back_kb("shop"))


async def _handle_duration_option_selected(q, context, option_id: str):
    option_row = db.get_product_duration_option(option_id)
    if not option_row:
        await _edit_text(q, "Ye plan ab available nahi hai. Product dobara select karein.", back_kb("shop")); return
    option = dict(option_row)
    product_id = str(option["product_id"])
    row = db.get_product(product_id)
    if not row:
        await _edit_text(q, "Product nahi mila.", back_kb("shop")); return
    product = dict(row)
    if str(product.get("product_type") or "").lower() != "keyauth" or not product.get("keyauth_api_key"):
        await _edit_text(q, "Product KeyAuth ke liye configured nahi hai.", back_kb("shop")); return
    days = int(option["duration_days"])
    amount = int(option["price_paise"])
    if days < 1 or days > 36500 or amount < 100:
        await _edit_text(q, "Plan configuration invalid hai. Admin se contact karein.", back_kb("shop")); return
    context.user_data["product"] = product
    context.user_data["duration_days"] = days
    context.user_data["duration_label"] = str(option["label"])
    context.user_data["duration_amount_paise"] = amount
    user = db.get_user(q.from_user.id, "tg")
    balance = int(user["balance_paise"]) if user else 0
    buttons = [
        [InlineKeyboardButton("💰 Pay with Wallet", callback_data=f"pay_opt:wallet:{product_id}")],
        [InlineKeyboardButton("📱 Pay with UPI QR", callback_data=f"pay_opt:upi:{product_id}")],
    ]
    if q.from_user.id == ADMIN_ID:
        buttons.append([InlineKeyboardButton("🛠 Admin Bypass — Get Key", callback_data=f"pay_opt:admin:{product_id}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"prod:{product_id}")])
    day_text = "Lifetime" if days >= 36500 else f"{days} day(s)"
    await _edit_text(q,
        f"🔑 <b>{product['name']}</b>\n\nPlan: <b>{option['label']}</b>\nValidity: <b>{day_text}</b>\n"
        f"Final Price: <b>₹{amount/100:.2f}</b>\nWallet balance: <b>₹{balance/100:.2f}</b>\n\nPayment method choose karein:",
        InlineKeyboardMarkup(buttons))


async def _handle_duration_selected(q, context, days: int, product_id: str):
    if days < 1 or days > 36500:
        await _edit_text(q, "Invalid duration. Dobara product select karein.", back_kb("shop"))
        return
    row = db.get_product(product_id)
    if not row:
        await _edit_text(q, "Product nahi mila.", back_kb("shop"))
        return
    product = dict(row)
    if str(product.get("product_type") or "").lower() != "keyauth" or not product.get("keyauth_api_key"):
        await _edit_text(q, "Product KeyAuth ke liye configured nahi hai.", back_kb("shop"))
        return
    context.user_data["product"] = product
    context.user_data["duration_days"] = days
    context.user_data["duration_label"] = ("Lifetime" if days >= 36500 else f"{days} Days")
    amount = int(product["price_paise"]) * days
    context.user_data["duration_amount_paise"] = amount
    user = db.get_user(q.from_user.id, "tg")
    balance = int(user["balance_paise"]) if user else 0
    buttons = [
        [InlineKeyboardButton("💰 Pay with Wallet", callback_data=f"pay_opt:wallet:{product_id}")],
        [InlineKeyboardButton("📱 Pay with UPI QR", callback_data=f"pay_opt:upi:{product_id}")],
    ]
    if q.from_user.id == ADMIN_ID:
        buttons.append([InlineKeyboardButton("🛠 Admin Bypass — Get Key", callback_data=f"pay_opt:admin:{product_id}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"prod:{product_id}")])
    await _edit_text(q,
        f"🔑 <b>{product['name']}</b>\n\nValidity: <b>{days} day(s)</b>\n"
        f"Price: ₹{int(product['price_paise'])//100} × {days} = <b>₹{amount//100}</b>\n"
        f"Wallet balance: <b>₹{balance//100}</b>\n\nPayment method choose karein:",
        InlineKeyboardMarkup(buttons))


async def _region_selected(q, context, region_id):
    region = context.user_data.get("regions", {}).get(region_id)
    if not region:
        await _edit_text(q, "Region expire. Dobara try karo.", back_kb("shop"))
        return
    context.user_data["region"] = region
    context.user_data["awaiting_clan_id"] = True
    await _edit_text(q, f"✅ Region: <b>{region.get('region_name')}</b>\n\nAb apni Free Fire <b>Guild ID</b> bhejo (sirf numeric).", back_kb())


# ============ TEXT HANDLER ============

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()

    if context.user_data.get("awaiting_clan_id"):
        await _handle_clan_id(update, context, text)
        return
    if context.user_data.get("awaiting_amount"):
        context.user_data["awaiting_amount"] = False
        await _handle_add_balance(update, context, text)
        return
    if context.user_data.get("awaiting_feedback"):
        context.user_data["awaiting_feedback"] = False
        await _handle_feedback(update, context, text=text, file_id=None, file_type=None)
        return

    await update.effective_message.reply_text("Menu ke liye /start dabao.")


# ============ LOADING ANIMATION ============

LOADING_FRAMES = ["▰▱▱▱▱▱▱▱▱▱", "▰▰▱▱▱▱▱▱▱▱", "▰▰▰▱▱▱▱▱▱▱", "▰▰▰▰▱▱▱▱▱▱", "▰▰▰▰▰▱▱▱▱▱", "▰▰▰▰▰▰▱▱▱▱", "▰▰▰▰▰▰▰▱▱▱", "▰▰▰▰▰▰▰▰▱▱", "▰▰▰▰▰▰▰▰▰▱", "▰▰▰▰▰▰▰▰▰▰"]

async def _run_loading_animation(msg, task, title="Processing..."):
    for i, frame in enumerate(LOADING_FRAMES):
        if task.done(): break
        pct = (i + 1) * 10
        try:
            await msg.edit_text(f"⏳ <b>{title}</b>\n\n{frame} {pct}%", parse_mode="HTML")
        except Exception:
            pass
        await asyncio.sleep(0.15)


# ============ QR TIMER ============

async def _qr_timer(bot, chat_id, message_id, order_id, caption, kb):
    expires_at = time.time() + QR_EXPIRE_SECONDS
    while True:
        remaining = int(expires_at - time.time())
        if remaining <= 0: break
        row = db.get_order(order_id)
        if not row or row["payment_status"] != "created": return
        if row["order_status"] not in ("awaiting_payment", "creating_payment"): return

        mins, secs = divmod(remaining, 60)
        try:
            await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=caption + f"\n\n⏱ <b>QR Expires in:</b> {mins:02d}:{secs:02d}", parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            if "not modified" not in str(e).lower(): return
        await asyncio.sleep(TIMER_UPDATE_EVERY)

    row = db.get_order(order_id)
    if not row or row["payment_status"] != "created": return

    try: await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception: pass

    db.update_order(order_id, order_status="expired", payment_status="expired", error="QR expired after 5 min")
    try:
        expire_msg = await bot.send_message(chat_id=chat_id, text=(f"⏰ <b>QR Expire ho gaya</b>\n\nOrder <code>{order_id}</code> cancel.\nNaya order banane ke liye /start dabao."), parse_mode="HTML")
        _schedule_delete(bot, chat_id, expire_msg.message_id)
    except Exception: pass


# ============ CLAN/GUILD ID HANDLER ============

async def _handle_clan_id(update, context, clan_id):
    if not clan_id.isdigit() or not (5 <= len(clan_id) <= 20):
        await update.effective_message.reply_text("Guild ID valid nahi. 5–20 digit numeric bhejo.")
        return
    
    context.user_data["awaiting_clan_id"] = False
    product = context.user_data.get("product")
    region = context.user_data.get("region")
    if not product or not region:
        await update.effective_message.reply_text("Session expire. /start karo.")
        return

    u = update.effective_user

    # Guild Glory can be paused without stopping the Telegram/Web service.
    if db.get_setting("guild_glory_enabled", "1") != "1":
        await update.effective_message.reply_text("⏸ Guild Glory orders abhi paused hain. Baad me try karein.")
        return

    # API Credit Check for Default Product
    if product["is_default"]:
        loading = await update.effective_message.reply_text("🔍 <b>Checking API Credits...</b>", parse_mode="HTML")
        try:
            info = await asyncio.to_thread(ff.get_account)
            basic = info.get('basic_credits', 0)
            if basic <= 0:
                try: await loading.delete()
                except Exception: pass
                await update.effective_message.reply_text(
                    "❌ <b>Guild Glory Stock Out!</b>\n\n"
                    "Abhi Guild Glory ke credits khatam hain. Kripya thodi der baad try karein ya Admin se contact karein.\n\n"
                    "(Admin: /start → Admin Support)",
                    parse_mode="HTML"
                )
                return
        except Exception as e:
            try: await loading.delete()
            except Exception: pass
            await update.effective_message.reply_text(f"⚠️ API Error: {str(e)[:200]}")
            return
        try: await loading.delete()
        except Exception: pass

    # Ask Payment Method (Wallet or UPI)
    context.user_data["clan_id"] = clan_id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Pay with Wallet", callback_data=f"pay_opt:wallet:{product['product_id']}")],
        [InlineKeyboardButton("📱 Pay with UPI QR", callback_data=f"pay_opt:upi:{product['product_id']}")],
        [InlineKeyboardButton("⬅️ Cancel", callback_data="shop")]
    ])
    
    user_data = db.get_user(u.id, "tg")
    balance = user_data["balance_paise"] if user_data else 0
    
    await update.effective_message.reply_text(
        f"✅ Guild ID: <code>{clan_id}</code>\n"
        f"Product: <b>{product['name']}</b>\n"
        f"Amount: <b>₹{product['price_paise']//100}</b>\n"
        f"Your Wallet Balance: <b>₹{balance//100}</b>\n\n"
        f"<b>Payment method choose karo:</b>",
        parse_mode="HTML", reply_markup=kb
    )


# ============ KEYAUTH USER ACCOUNT PURCHASE / DELIVERY ============

KEYAUTH_PASSWORD = "1"


def _keyauth_username(user=None, platform="tg"):
    # Fresh 8-character username candidate; KeyAuth helper retries on collision.
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))


def _expiry_iso(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=float(days))).isoformat()


def _unix_to_iso(timestamp: int | float | None) -> str | None:
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


async def _create_or_extend_keyauth_account(product: dict, days: float, username: str):
    """Create a unique 8-char alphanumeric account for this order. Password is always 1."""
    seller_key = product.get("keyauth_api_key", "")
    subscription = str(product.get("keyauth_subscription") or "default").strip()
    result = await asyncio.to_thread(
        ka.create_unique_or_extend_user, seller_key, subscription, float(days), KEYAUTH_PASSWORD, username
    )
    username = str(result.get("username") or username)
    expiry_unix = ka.subscription_expiry(result.get("data") or {}, subscription)
    if not result.get("created"):
        previous = result.get("previous_expiry")
        if previous and (expiry_unix is None or expiry_unix <= previous):
            expiry_unix = int(max(float(previous), datetime.now(timezone.utc).timestamp()) + float(days) * 86400)
    expiry = _unix_to_iso(expiry_unix) or _expiry_iso(days)
    return username, KEYAUTH_PASSWORD, subscription, expiry


async def _create_fresh_keyauth_account(product: dict, username: str, password: str, subscription: str, remaining_seconds: float):
    days = max(remaining_seconds / 86400.0, 0.0001)
    response = await asyncio.to_thread(
        ka.create_user, product.get("keyauth_api_key", ""), username, password,
        subscription or product.get("keyauth_subscription") or "default", days
    )
    data = await asyncio.to_thread(ka.get_user_data, product.get("keyauth_api_key", ""), username)
    expiry_unix = ka.subscription_expiry(data, subscription or product.get("keyauth_subscription") or "default")
    expiry = _unix_to_iso(expiry_unix) or _expiry_iso(days)
    return expiry, response


async def _handle_keyauth_payment_option(q, context, method: str, product: dict):
    u = q.from_user
    await _edit_text(q, "⏳ <b>Payment option process ho raha hai...</b>", None)
    days = int(context.user_data.get("duration_days") or 0)
    if days < 1 or days > 36500:
        await _edit_text(q, "Session expire. Product aur duration dobara select karein.", back_kb("shop"))
        return
    amount = int(context.user_data.get("duration_amount_paise") or (int(product["price_paise"]) * days))
    duration_label = str(context.user_data.get("duration_label") or ("Lifetime" if days >= 36500 else f"{days} day(s)"))
    order_id = "KA" + uuid.uuid4().hex[:12].upper()
    username = _keyauth_username(u, "tg")
    subscription = str(product.get("keyauth_subscription") or "default").strip()
    db.create_order(order_id, u.id, "tg", username, product["product_id"], product["name"], "", "", "keyauth", "", amount, duration_days=days, duration_label=duration_label)
    db.update_order(order_id, keyauth_username=username, keyauth_password=KEYAUTH_PASSWORD, keyauth_subscription=subscription)

    if method == "wallet":
        user_data = db.get_user(u.id, "tg")
        balance = int(user_data["balance_paise"]) if user_data else 0
        if balance < amount:
            db.update_order(order_id, order_status="cancelled", payment_status="failed", error="Insufficient wallet balance")
            await _edit_text(q, f"❌ Wallet balance kam hai.\nBalance: ₹{balance//100}\nRequired: ₹{amount//100}\n\nPehle Add Balance karein.", back_kb("shop"))
            return
        if not db.deduct_balance_atomic(u.id, "tg", amount):
            db.update_order(order_id, order_status="cancelled", payment_status="failed", error="Wallet balance changed before debit")
            await _edit_text(q, "❌ Wallet balance change ho gaya. Dobara try karein.", back_kb("shop"))
            return
        loading = await q.message.reply_text("⏳ KeyAuth account create/extend ho raha hai...")
        try:
            username, password, subscription, expiry = await _create_or_extend_keyauth_account(product, days, username)
        except Exception as exc:
            db.add_balance(u.id, "tg", amount)  # refund on provider failure
            db.update_order(order_id, order_status="key_delivery_review", payment_status="failed", error=str(exc)[:500])
            try: await loading.delete()
            except Exception: pass
            await q.message.reply_text(f"❌ KeyAuth API fail hui. Wallet se ₹{amount//100} refund kar diya gaya.\nOrder: <code>{order_id}</code>\nAdmin se contact karein.", parse_mode="HTML")
            return
        db.update_order(order_id, payment_status="paid", order_status="running", delivered_key=username,
                        keyauth_username=username, keyauth_password=password, keyauth_subscription=subscription,
                        license_expires_at=expiry, error="")
        db.update_keyauth_account_orders(u.id, "tg", product["product_id"], username, expiry, password, "")
        db.record_payment("WALLET-" + order_id, order_id, u.id, "tg", amount, "wallet", None)
        try: await loading.delete()
        except Exception: pass
        await q.message.reply_text(
            f"✅ <b>KeyAuth Account Ready!</b>\n\nProduct: <b>{product['name']}</b>\nValidity added: <b>{duration_label}</b>\n"
            f"Order: <code>{order_id}</code>\nUsername: <code>{username}</code>\nPassword: <code>{password}</code>\n"
            f"Subscription: <code>{subscription}</code>\nExpiry UTC: <code>{expiry[:19]}</code>\n"
            + (f"Product Link: {html.escape(str(product.get('product_link') or ''), quote=True)}\n" if product.get("product_link") else "")
            + "\nMy Keys se account reset kar sakte hain. Reset par username/password wahi rahenge; account/session reset hoga.", parse_mode="HTML")
        return

    if method == "admin":
        if u.id != ADMIN_ID:
            await _edit_text(q, "❌ Admin only option.", back_kb("shop"))
            return
        loading = await q.message.reply_text("🛠 Admin bypass: KeyAuth account create/extend ho raha hai...")
        try:
            username, password, subscription, expiry = await _create_or_extend_keyauth_account(product, days, username)
            db.update_order(order_id, payment_status="admin_bypass", order_status="running", delivered_key=username,
                            keyauth_username=username, keyauth_password=password, keyauth_subscription=subscription,
                            license_expires_at=expiry, error="")
            db.update_keyauth_account_orders(u.id, "tg", product["product_id"], username, expiry, password, "")
            try: await loading.delete()
            except Exception: pass
            await q.message.reply_text(
                f"🛠 <b>Admin Bypass Successful</b>\nProduct: <b>{product['name']}</b>\nValidity added: {duration_label}\n"
                f"Order: <code>{order_id}</code>\nUsername: <code>{username}</code>\nPassword: <code>{password}</code>\n"
                f"Expiry UTC: <code>{expiry[:19]}</code>\n"
                + (f"Product Link: {html.escape(str(product.get('product_link') or ''), quote=True)}" if product.get("product_link") else ""), parse_mode="HTML")
        except Exception as exc:
            db.update_order(order_id, order_status="key_delivery_review", payment_status="failed", error=str(exc)[:500])
            try: await loading.delete()
            except Exception: pass
            await q.message.reply_text(f"❌ KeyAuth error: {str(exc)[:250]}")
        return

    if method == "upi":
        loading = await q.message.reply_text("⏳ UPI QR generate ho raha hai...")
        qr_task = asyncio.create_task(asyncio.to_thread(rzp.create_qr, order_id, amount, u.full_name or str(u.id)))
        await _run_loading_animation(loading, qr_task, "Generating UPI QR")
        try:
            qr = await qr_task
            db.update_order(order_id, qr_code_id=qr["id"], qr_image_url=qr["image_url"], order_status="awaiting_payment", payment_status="created")
            try: await loading.delete()
            except Exception: pass
            img = await asyncio.to_thread(_download_qr, qr["image_url"])
            caption = (f"🧾 <b>Order:</b> <code>{order_id}</code>\nProduct: <b>{product['name']}</b>\n"
                       f"Validity: <b>{duration_label}</b>\nAmount: <b>₹{amount//100}</b>\n\n"
                       f"QR ko scan karke exact amount pay karein. Payment auto-check hoga.")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Check Payment", callback_data=f"checkpay:{order_id}")],
                                       [InlineKeyboardButton("🔳 Open QR", url=qr["image_url"])],
                                       [InlineKeyboardButton("⬅️ Cancel", callback_data="shop")]])
            sent = await q.message.reply_photo(img, caption=caption, parse_mode="HTML", reply_markup=kb)
            db.update_order(order_id, qr_message_id=sent.message_id, qr_chat_id=u.id)
            asyncio.create_task(_qr_timer(context.application.bot, u.id, sent.message_id, order_id, caption, kb))
        except Exception as exc:
            db.update_order(order_id, order_status="payment_setup_failed", error=str(exc)[:500])
            try: await loading.delete()
            except Exception: pass
            await q.message.reply_text(f"❌ QR fail: {str(exc)[:220]}")
        return

    await _edit_text(q, "Payment method valid nahi hai.", back_kb("shop"))


async def _handle_payment_option(q, context, method, product_id):
    u = q.from_user
    product = db.get_product(product_id)
    clan_id = context.user_data.get("clan_id")
    region = context.user_data.get("region")
    if not product:
        await _edit_text(q, "Session expire. /start karo.", back_kb("shop"))
        return
    product = dict(product)
    product_type = str(product.get("product_type") or ("guild_glory" if product.get("is_default") else "keyauth")).lower()
    if product_type == "keyauth":
        await _handle_keyauth_payment_option(q, context, method, product)
        return
    if db.get_setting("guild_glory_enabled", "1") != "1":
        await _edit_text(q, "⏸ Guild Glory ordering abhi paused hai.", back_kb("shop"))
        return
    if not clan_id or not region:
        await _edit_text(q, "Session expire. /start karo.", back_kb("shop"))
        return

    amount = product["price_paise"]
    order_id = "FG" + uuid.uuid4().hex[:12].upper()

    if method == "wallet":
        user_data = db.get_user(u.id, "tg")
        current_balance = user_data["balance_paise"] if user_data else 0
        
        if current_balance < amount:
            await _edit_text(q, f"❌ <b>Insufficient Balance!</b>\n\nAapka balance: ₹{current_balance//100}\nZaroorat: ₹{amount//100}\n\nPehle wallet me balance add karo.", back_kb("shop"))
            return

        # Deduct Balance & Create Order
        db.deduct_balance(u.id, "tg", amount)
        db.create_order(order_id, u.id, "tg", u.username or "", product["product_id"], product["name"], str(region["id"]), region.get("region_name", ""), str(region.get("tier", "")), clan_id, amount)
        
        # Mark as paid immediately
        db.update_order(order_id, payment_status="paid", order_status="paid_pending_launch")
        
        await _edit_text(q, f"✅ <b>Payment Successful via Wallet!</b>\n\nProcessing order...", None)
        
        try:
            res = await asyncio.to_thread(ff.launch_group, region["id"], clan_id)
            if res.get("success") and res.get("group_id"):
                key = res["group_id"]
                db.update_order(order_id, order_status="running", group_id=key, delivered_key=key)
                await q.message.reply_text(f"✅ <b>Order Complete!</b>\n\nOrder: <code>{order_id}</code>\nGroup ID: <code>{key}</code>", parse_mode="HTML")
            else:
                db.update_order(order_id, order_status="launch_review", error=str(res)[:500])
                await q.message.reply_text(f"⚠️ Launch fail. Admin contact karo.\nOrder: <code>{order_id}</code>", parse_mode="HTML")
        except Exception as e:
            db.update_order(order_id, order_status="launch_review", error=str(e)[:500])
            await q.message.reply_text(f"⚠️ Launch fail. Order: <code>{order_id}</code>", parse_mode="HTML")

    elif method == "upi":
        db.create_order(order_id, u.id, "tg", u.username or "", product["product_id"], product["name"], str(region["id"]), region.get("region_name", ""), str(region.get("tier", "")), clan_id, amount)
        
        loading = await q.message.reply_text("⏳ <b>Generating UPI QR...</b>", parse_mode="HTML")
        qr_task = asyncio.create_task(asyncio.to_thread(rzp.create_qr, order_id, amount, u.full_name or str(u.id)))
        await _run_loading_animation(loading, qr_task)
        
        try:
            qr = await qr_task
            db.update_order(order_id, qr_code_id=qr["id"], qr_image_url=qr["image_url"], order_status="awaiting_payment", payment_status="created")
            try: await loading.delete()
            except Exception: pass
            
            img = await asyncio.to_thread(_download_qr, qr["image_url"])
            caption = (f"🧾 <b>Order:</b> <code>{order_id}</code>\nProduct: <b>{product['name']}</b>\nGuild: <code>{clan_id}</code>\nAmount: <b>₹{amount//100}</b>\n\n⏱ <b>QR Expires in:</b> 05:00\n\nUPI se QR scan karke exact amount pay karo.")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Check Payment", callback_data=f"checkpay:{order_id}")], [InlineKeyboardButton("🔳 Open QR", url=qr["image_url"])]])
            sent = await q.message.reply_photo(img, caption=caption, parse_mode="HTML", reply_markup=kb)
            db.update_order(order_id, qr_message_id=sent.message_id, qr_chat_id=u.id)
            asyncio.create_task(_qr_timer(context.application.bot, u.id, sent.message_id, order_id, caption, kb))
        except Exception as e:
            await q.message.reply_text(f"❌ QR Fail: {str(e)[:200]}")


def _download_qr(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    bio = BytesIO(r.content)
    bio.name = "qr.png"
    bio.seek(0)
    return bio


# ============ ADD BALANCE ============

async def _handle_add_balance(update, context, text):
    u = update.effective_user
    if not text.isdigit() or int(text) <= 0 or int(text) > 100000:
        await update.effective_message.reply_text("Valid amount bhejo (1–100000).")
        return
    amount_paise = int(text) * 100
    topup_id = "TU" + uuid.uuid4().hex[:10].upper()
    db.create_order(topup_id, u.id, "tg", u.username or "", "topup", "Wallet Top-up", "", "", "", "", amount_paise)

    loading = await update.effective_message.reply_text("⏳ <b>QR generate ho raha hai...</b>\n\n▱▱▱▱▱▱▱▱▱▱ 0%", parse_mode="HTML")
    qr_task = asyncio.create_task(asyncio.to_thread(rzp.create_qr, topup_id, amount_paise, u.full_name or str(u.id)))
    await _run_loading_animation(loading, qr_task)

    try:
        qr = await qr_task
        db.update_order(topup_id, qr_code_id=qr["id"], qr_image_url=qr["image_url"], order_status="awaiting_payment", payment_status="created")
        try: await loading.delete()
        except Exception: pass
        
        img = await asyncio.to_thread(_download_qr, qr["image_url"])
        caption = (f"💰 <b>Wallet Top-up</b>\nTopup ID: <code>{topup_id}</code>\nAmount: <b>₹{amount_paise//100}</b>\n\n⏱ <b>QR Expires in:</b> 05:00\n\nQR scan karke pay karo.")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Check Payment", callback_data=f"checkpay:{topup_id}")]])
        sent = await update.effective_message.reply_photo(img, caption=caption, parse_mode="HTML", reply_markup=kb)
        db.update_order(topup_id, qr_message_id=sent.message_id, qr_chat_id=u.id)
        asyncio.create_task(_qr_timer(context.application.bot, u.id, sent.message_id, topup_id, caption, kb))
    except Exception as e:
        try: await loading.edit_text(f"❌ Top-up fail: {str(e)[:300]}")
        except Exception: pass


# ============ FEEDBACK ============

async def _handle_feedback(update, context, text=None, file_id=None, file_type=None):
    u = update.effective_user
    fid = uuid.uuid4().hex
    db.save_feedback(fid, u.id, "tg", text or "", file_id, file_type)
    try:
        header = f"💬 <b>Feedback</b> from <a href='tg://user?id={u.id}'>{u.full_name}</a> (<code>{u.id}</code>)"
        body = header + (f"\n\n{text}" if text else "")
        if file_id and file_type == "photo":
            await _backup_app.bot.send_photo(ADMIN_ID, file_id, caption=body, parse_mode="HTML")
        else:
            await _backup_app.bot.send_message(ADMIN_ID, body, parse_mode="HTML")
        await update.effective_message.reply_text("✅ Feedback bhej diya. Dhanyavaad!")
    except Exception:
        await update.effective_message.reply_text("⚠️ Feedback save hua lekin admin tak nahi pahuncha.")


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_feedback"):
        context.user_data["awaiting_feedback"] = False
        photo = update.effective_message.photo[-1]
        await _handle_feedback(update, context, text=update.effective_message.caption or "", file_id=photo.file_id, file_type="photo")


# ============ PROFILE / KEYS ============

async def _show_profile(q):
    u = db.get_user(q.from_user.id, "tg")
    if not u:
        db.upsert_user(q.from_user.id, "tg", q.from_user.username or "", q.from_user.full_name or "")
        u = db.get_user(q.from_user.id, "tg")
    orders = db.list_user_orders(q.from_user.id, "tg", limit=100)
    bought = sum(1 for o in orders if o["payment_status"] == "paid" and o["order_status"] == "running")
    text = (f"👤 <b>Profile</b>\n\nName: <b>{u['display_name']}</b>\nID: <code>{u['user_id']}</code>\nUsername: @{u['username'] or '-'}\n💰 Balance: <b>₹{u['balance_paise']//100}</b>\n📦 Products bought: <b>{bought}</b>\n📅 Joined: {u['joined_at'][:10]}")
    await _edit_text(q, text, back_kb())


async def _show_mykeys(q, uid):
    orders = [dict(o) for o in db.list_user_orders(uid, "tg", limit=100) if o["delivered_key"]]
    if not orders:
        await _edit_text(q, "Abhi koi key/account nahi hai.", back_kb())
        return
    lines = ["🔑 <b>My Keys / Accounts</b>\n"]
    buttons = []
    now = datetime.now(timezone.utc)
    seen_accounts = set()
    for order in orders[:60]:
        is_account = bool(order.get("keyauth_password") and order.get("keyauth_username"))
        account_id = (str(order.get("product_id") or ""), str(order.get("keyauth_username") or ""))
        if is_account and account_id in seen_accounts:
            continue
        if is_account:
            seen_accounts.add(account_id)
        expiry = order.get("license_expires_at")
        expiry_text = "Expiry not tracked"
        can_reset = bool(order.get("keyauth_username") and order.get("product_id"))
        if expiry:
            try:
                exp_dt = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                remaining = max(0, int((exp_dt - now).total_seconds()))
                expiry_text = f"{remaining // 86400}d {(remaining % 86400) // 3600}h remaining"
                can_reset = can_reset and remaining > 0
            except Exception:
                expiry_text = "Expiry record invalid"
                can_reset = False
        if is_account:
            lines.append(
                f"• <b>{order['product_name']}</b>\n  Username: <code>{order['keyauth_username']}</code>\n"
                f"  Password: <code>{order['keyauth_password']}</code>\n  Order: <code>{order['order_id']}</code> · {expiry_text}"
            )
            product = db.get_product(order.get("product_id"))
            if product and str(product["product_type"] or "").lower() == "keyauth" and can_reset:
                buttons.append([InlineKeyboardButton(f"🔄 Reset account · {order['product_name'][:22]}", callback_data=f"resetkey:{order['order_id']}")])
        else:
            # Compatibility display for legacy license-key orders from older builds.
            lines.append(f"• <b>{order['product_name']}</b> — License: <code>{order['delivered_key']}</code>\n  Order: <code>{order['order_id']}</code> · {expiry_text}")
            product = db.get_product(order.get("product_id"))
            if product and str(product["product_type"] or "").lower() == "keyauth" and can_reset:
                buttons.append([InlineKeyboardButton(f"🔄 Reset legacy key · {order['product_name'][:20]}", callback_data=f"resetkey:{order['order_id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    await _edit_text(q, "\n\n".join(lines), InlineKeyboardMarkup(buttons))


async def _reset_key(q, context, order_id: str):
    row = db.get_order(order_id)
    if not row or int(row["user_id"]) != int(q.from_user.id) or row["platform"] != "tg":
        await _edit_text(q, "Order nahi mila.", back_kb("mykeys"))
        return
    order = dict(row)
    product_row = db.get_product(order.get("product_id"))
    if not product_row or str(product_row["product_type"] or "").lower() != "keyauth":
        await _edit_text(q, "Ye KeyAuth product nahi hai; reset nahi kar sakte.", back_kb("mykeys"))
        return
    product = dict(product_row)
    if not order.get("delivered_key") or not order.get("license_expires_at"):
        await _edit_text(q, "Is account ki expiry record nahi mili. Admin se contact karein.", back_kb("mykeys"))
        return
    try:
        expiry = datetime.fromisoformat(str(order["license_expires_at"]).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        remaining = 0
        expiry = datetime.now(timezone.utc)
    if remaining <= 0:
        await _edit_text(q, "⏰ Ye account expire ho chuka hai; reset nahi ho sakta.", back_kb("mykeys"))
        return

    # New username/password orders reset by deleting and recreating the same
    # KeyAuth account for only the remaining validity. Credentials remain the
    # same because this selected mode uses Telegram username + fixed password 1.
    if order.get("keyauth_password") and order.get("keyauth_username"):
        username = str(order["keyauth_username"])
        password = str(order["keyauth_password"] or KEYAUTH_PASSWORD)
        subscription = str(order.get("keyauth_subscription") or product.get("keyauth_subscription") or "default")
        seller_key = product.get("keyauth_api_key", "")
        await _edit_text(q, "⏳ Account reset ho raha hai; remaining validity preserve ki ja rahi hai...", None)
        deleted = False
        try:
            await asyncio.to_thread(ka.delete_user, seller_key, username)
            deleted = True
            new_expiry, _ = await _create_fresh_keyauth_account(product, username, password, subscription, remaining)
            db.update_order(order_id, delivered_key=username, keyauth_username=username, keyauth_password=password,
                            keyauth_subscription=subscription, license_expires_at=new_expiry,
                            error="KeyAuth account reset; remaining validity preserved")
            db.update_keyauth_account_orders(order["user_id"], order["platform"], order["product_id"], username,
                                            new_expiry, password, "KeyAuth account reset; remaining validity preserved")
            await _edit_text(q,
                f"✅ <b>Account Reset Successful</b>\nProduct: <b>{order['product_name']}</b>\n"
                f"Order: <code>{order_id}</code>\nUsername: <code>{username}</code>\nPassword: <code>{password}</code>\n"
                f"Remaining validity: <b>{max(0, int(remaining)) // 86400} day(s), {(max(0, int(remaining)) % 86400) // 3600} hour(s)</b>\n"
                f"Expiry UTC: <code>{new_expiry[:19]}</code>\n\nCredentials same rahenge; KeyAuth account/session recreate hua hai.", back_kb("mykeys"))
        except Exception as exc:
            recovery = ""
            if deleted:
                try:
                    restored_expiry, _ = await _create_fresh_keyauth_account(product, username, password, subscription, remaining)
                    db.update_keyauth_account_orders(order["user_id"], order["platform"], order["product_id"], username,
                                                    restored_expiry, password, "Reset failed; old account recreated for remaining validity")
                    recovery = " Purana account same credentials aur remaining validity ke saath recreate kar diya gaya hai."
                except Exception as recovery_exc:
                    recovery = f" Recovery bhi fail hui: {str(recovery_exc)[:140]}. Admin ko turant contact karein."
            await _edit_text(q, f"❌ Account reset fail hua: {str(exc)[:180]}.{recovery}", back_kb("mykeys"))
        return

    # Legacy license key reset support for existing orders from the previous build.
    old_key = str(order["delivered_key"])
    username = str(order.get("keyauth_username") or _keyauth_username(q.from_user, "tg"))
    await _edit_text(q, "⏳ Legacy license key reset ho rahi hai...", None)
    new_key = ""
    try:
        new_key = await asyncio.to_thread(ka.create_license, product.get("keyauth_api_key", ""), int(remaining),
                                          f"user={username};user_id={q.from_user.id};order={order_id};reset=true")
        await asyncio.to_thread(ka.delete_license, product.get("keyauth_api_key", ""), old_key)
        new_expiry = expiry.isoformat()
        db.update_order(order_id, delivered_key=new_key, keyauth_username=username,
                        license_expires_at=new_expiry, error="Legacy license reset; original expiry preserved")
        await _edit_text(q, f"✅ <b>Legacy Key Reset Successful</b>\nNew Key: <code>{new_key}</code>\nExpiry UTC: <code>{new_expiry[:19]}</code>", back_kb("mykeys"))
    except Exception as exc:
        if new_key:
            try: await asyncio.to_thread(ka.delete_license, product.get("keyauth_api_key", ""), new_key)
            except Exception: log.exception("Legacy KeyAuth rollback failed")
        await _edit_text(q, f"❌ Key reset fail hua: {str(exc)[:220]}\nPurani key database me rakhi gayi hai.", back_kb("mykeys"))


# ============ MANUAL PAYMENT CHECK ============


async def _check_payment_manual(q, context, order_id):
    row = db.get_order(order_id)
    if not row or row["user_id"] != q.from_user.id:
        await _edit_text(q, "Order nahi mila.", back_kb())
        return
    if row["payment_status"] == "paid":
        await _edit_text(q, f"✅ Already paid. Status: {row['order_status']}", back_kb())
        return
    if row["order_status"] == "expired":
        await _edit_text(q, "⏰ QR expire ho gaya. Naya order banao.", back_kb())
        return
    try:
        await _verify_order_payment(context.application, order_id)
        fresh = db.get_order(order_id)
        if fresh["payment_status"] == "paid":
            await _edit_text(q, f"✅ Payment verified! Status: {fresh['order_status']}", back_kb())
        else:
            await _edit_text(q, "⏳ Abhi payment confirm nahi hua.", back_kb())
    except Exception as e:
        await _edit_text(q, f"⚠️ Check fail: {str(e)[:300]}", back_kb())


# ============ MAIN ADMIN PANEL ============

async def _show_admin_panel(q, context, notice=""):
    enabled = db.get_setting("guild_glory_enabled", "1") == "1"
    status = "🟢 RUNNING (new orders enabled)" if enabled else "⏸ PAUSED (new orders blocked)"
    toggle_label = "⏸ Stop Guild Glory Orders" if enabled else "▶️ Start Guild Glory Orders"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_label, callback_data="adm:guildglory:toggle")],
        [InlineKeyboardButton("🔁 Retry KeyAuth Delivery", callback_data="adm:retry_key")],
        [InlineKeyboardButton("🔐 Create Web Login", callback_data="adm:web_login")],
        [InlineKeyboardButton("👥 Manage Web Users", callback_data="adm:manage_web")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])
    msg = f"🛠 <b>Main Admin Panel</b>\n\n<b>Guild Glory:</b> {status}\n\nYahan se Web Users aur Guild Glory order availability manage karein."
    if notice:
        msg = f"✅ {notice}\n\n" + msg
    await _edit_text(q, msg, kb)


# ============ MANAGE WEB USERS ============

async def _show_manage_web_users(q, context):
    try:
        r = requests.get(f"{FIREBASE_DB_URL}/web_users.json", timeout=10)
        if r.ok and r.json():
            users_data = r.json()
            buttons = []
            for username in users_data.keys():
                buttons.append([InlineKeyboardButton(
                    f"🗑 Delete {username}",
                    callback_data=f"adm:del_web:{username}"
                )])
            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="admin")])
            
            if len(buttons) == 1:
                await _edit_text(q, "Abhi koi Web User nahi hai.", InlineKeyboardMarkup(buttons))
            else:
                await _edit_text(q, "👥 <b>Manage Web Users</b>\n\nKis user ko delete karna hai?", InlineKeyboardMarkup(buttons))
        else:
            await _edit_text(q, "Koi Web User nahi mila.", back_kb("admin"))
    except Exception as e:
        await _edit_text(q, f"❌ Error: {str(e)[:200]}", back_kb("admin"))


async def _delete_web_user(q, context, username):
    try:
        url = f"{FIREBASE_DB_URL}/web_users/{username}.json"
        r = requests.delete(url, timeout=10)
        if r.ok:
            await _edit_text(q, f"✅ Web User <b>{username}</b> delete ho gaya.", back_kb("admin"))
        else:
            await _edit_text(q, f"❌ Delete fail: {r.text[:200]}", back_kb("admin"))
    except Exception as e:
        await _edit_text(q, f"❌ Error: {str(e)[:200]}", back_kb("admin"))


# ============ ADMIN TEXT FLOWS ============

async def on_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return False

    text = (update.effective_message.text or "").strip()

    if context.user_data.get("awaiting_keyauth_retry"):
        context.user_data.pop("awaiting_keyauth_retry", None)
        order = db.get_order(text)
        if not order:
            await update.effective_message.reply_text("❌ Order ID nahi mila.")
            return True
        order = dict(order)
        product_row = db.get_product(order.get("product_id"))
        product = dict(product_row) if product_row else {}
        if order.get("payment_status") != "paid" or order.get("delivered_key") or str(product.get("product_type") or "").lower() != "keyauth":
            await update.effective_message.reply_text("❌ Sirf paid, undelivered KeyAuth orders ka retry ho sakta hai.")
            return True
        days = int(order.get("duration_days") or 0)
        if days < 1 or days > 36500:
            await update.effective_message.reply_text("❌ Order duration missing/invalid hai.")
            return True
        username = str(order.get("keyauth_username") or order.get("username") or f"tg_{order.get('user_id')}").strip().lstrip("@").lower()
        subscription = str(order.get("keyauth_subscription") or product.get("keyauth_subscription") or "default")
        try:
            username, password, subscription, expiry = await _create_or_extend_keyauth_account(product, days, username)
            db.update_order(text, order_status="running", delivered_key=username, keyauth_username=username,
                            keyauth_password=password, keyauth_subscription=subscription,
                            license_expires_at=expiry, error="")
            db.update_keyauth_account_orders(order["user_id"], order["platform"], order["product_id"], username, expiry, password, "")
            await update.effective_message.reply_text(
                f"✅ KeyAuth account delivery retry successful.\nOrder: <code>{text}</code>\nUsername: <code>{username}</code>\nPassword: <code>{password}</code>\nExpiry UTC: <code>{expiry[:19]}</code>", parse_mode="HTML")
            if order.get("platform") == "tg":
                try:
                    await context.bot.send_message(order["user_id"], f"✅ Your KeyAuth account is ready.\nOrder: <code>{text}</code>\nUsername: <code>{username}</code>\nPassword: <code>{password}</code>", parse_mode="HTML")
                except Exception: pass
        except Exception as exc:
            db.update_order(text, order_status="key_delivery_review", error=str(exc)[:500])
            await update.effective_message.reply_text(f"❌ Retry fail: {str(exc)[:250]}")
        return True

    if context.user_data.get("awaiting_web_user"):
        state = context.user_data["awaiting_web_user"]
        step = state["step"]

        if step == "username":
            if len(text) < 4:
                await update.effective_message.reply_text("❌ Username kam se kam 4 characters ka hona chahiye. Dobara bhejo:")
                return True
            state["username"] = text
            state["step"] = "password"
            context.user_data["awaiting_web_user"] = state
            await update.effective_message.reply_text(f"✅ Username: <b>{text}</b>\n\nAb iska <b>Password</b> bhejo:", parse_mode="HTML")
            return True

        elif step == "password":
            if len(text) < 4:
                await update.effective_message.reply_text("❌ Password kam se kam 4 characters ka hona chahiye. Dobara bhejo:")
                return True
            state["password"] = text
            context.user_data.pop("awaiting_web_user", None)

            success = save_web_user_to_firebase(state["username"], state["password"])
            
            if success:
                await update.effective_message.reply_text(
                    f"✅ <b>Web Login Created!</b>\n\n"
                    f"🌐 URL: <code>yourwebsite.com/login.html</code>\n"
                    f"👤 Username: <code>{state['username']}</code>\n"
                    f"🔑 Password: <code>{state['password']}</code>\n\n"
                    f"Ab aap in details se web pe login kar sakte hain.",
                    parse_mode="HTML"
                )
            else:
                await update.effective_message.reply_text("❌ Firebase me save nahi ho paya. Internet ya config check karein.")
            return True

    return False


# ============ PAYMENT VERIFY ============

async def _verify_order_payment(app, order_id):
    row = db.get_order(order_id)
    if not row or not row["qr_code_id"]: return
    if row["payment_status"] != "created": return
    if row["order_status"] not in ("awaiting_payment", "creating_payment"): return

    result = await asyncio.to_thread(rzp.fetch_qr_payments, row["qr_code_id"])
    items = result.get("items", []) if isinstance(result, dict) else []
    match = next((p for p in items if str(p.get("status", "")).lower() == "captured" and int(p.get("amount", -1)) == int(row["amount_paise"])), None)
    if not match: return

    rzp_id = match.get("id")
    if rzp_id and db.payment_exists(rzp_id):
        db.claim_order_payment(order_id, rzp_id)
        return

    claimed = db.claim_order_payment(order_id, rzp_id)
    if not claimed: return

    inserted = db.record_payment(rzp_id or uuid.uuid4().hex, order_id, row["user_id"], row["platform"], row["amount_paise"], match.get("method"), rzp_id)
    if not inserted: 
        return
        
    if row["qr_message_id"]:
        try: await app.bot.delete_message(row["qr_chat_id"], row["qr_message_id"])
        except Exception: pass

    try:
        done_msg = await app.bot.send_message(row["user_id"], f"✅ <b>Payment Done!</b>\n\nOrder: <code>{order_id}</code>\nAmount: <b>₹{row['amount_paise']//100}</b>", parse_mode="HTML")
        _schedule_delete(app.bot, row["user_id"], done_msg.message_id)
    except Exception: pass

    if row["product_id"] == "topup":
        db.add_balance(row["user_id"], row["platform"], row["amount_paise"])
        return

    product_row = db.get_product(row["product_id"])
    product = dict(product_row) if product_row else {}
    product_type = str(product.get("product_type") or ("guild_glory" if product.get("is_default") else "keyauth")).lower()
    if product_type == "keyauth":
        try:
            days = int(row["duration_days"] or 0)
            if days < 1 or days > 36500:
                raise RuntimeError("Order duration record missing/invalid; admin review required.")
            username = str(row["keyauth_username"] or row["username"] or f"tg_{row['user_id']}").strip().lstrip("@").lower()
            username, password, subscription, expiry = await _create_or_extend_keyauth_account(product, days, username)
            db.update_order(order_id, order_status="running", delivered_key=username, keyauth_username=username,
                            keyauth_password=password, keyauth_subscription=subscription,
                            license_expires_at=expiry, error="")
            db.update_keyauth_account_orders(row["user_id"], row["platform"], row["product_id"], username, expiry, password, "")
            try:
                await app.bot.send_message(row["user_id"],
                    f"✅ <b>KeyAuth Payment Verified & Account Delivered</b>\n\nOrder: <code>{order_id}</code>\n"
                    f"Product: <b>{row['product_name']}</b>\nValidity added: <b>{row['duration_label'] or ('Lifetime' if days >= 36500 else f'{days} day(s)')}</b>\n"
                    f"👤 Username: <code>{username}</code>\n🔑 Password: <code>{password}</code>\n"
                    f"Subscription: <code>{subscription}</code>\nExpiry UTC: <code>{expiry[:19]}</code>\n"
                    + (f"Product Link: {html.escape(str(product.get('product_link') or ''), quote=True)}\n" if product.get("product_link") else "")
                    + "\nMy Keys se account reset kar sakte hain.", parse_mode="HTML")
            except Exception: pass
        except Exception as exc:
            db.update_order(order_id, order_status="key_delivery_review", error=str(exc)[:500])
            log.exception("KeyAuth paid order delivery failed: %s", order_id)
            try:
                await app.bot.send_message(row["user_id"], f"⚠️ Payment receive ho gayi, lekin KeyAuth account delivery pending hai. Admin ko Order ID <code>{order_id}</code> bhejein.", parse_mode="HTML")
            except Exception: pass
        return

    if product_type == "guild_glory" and db.get_setting("guild_glory_enabled", "1") != "1":
        db.update_order(order_id, order_status="launch_review", error="Guild Glory is paused; paid order needs admin review")
        return

    try:
        res = await asyncio.to_thread(ff.launch_group, row["region"], row["clan_id"])
        if res.get("success") and res.get("group_id"):
            key = res["group_id"]
            db.update_order(order_id, order_status="running", group_id=key, delivered_key=key)
            try:
                launch_msg = await app.bot.send_message(row["user_id"], f"✅ <b>Order Complete!</b>\nOrder: <code>{order_id}</code>\nGroup ID: <code>{key}</code>", parse_mode="HTML")
                _schedule_delete(app.bot, row["user_id"], launch_msg.message_id)
            except Exception: pass
        else:
            db.update_order(order_id, order_status="launch_review", error=str(res)[:500])
    except Exception as e:
        db.update_order(order_id, order_status="launch_review", error=str(e)[:500])


# ============ POLL LOOP ============

async def _poll_loop(app):
    while True:
        try:
            rows = await asyncio.to_thread(db.list_pending_orders)
            for r in rows:
                try: await _verify_order_payment(app, r["order_id"])
                except Exception: log.exception("poll fail %s", r["order_id"])
        except Exception: log.exception("poll loop error")
        await asyncio.sleep(5)


# ============ BUILD APP ============

def build_telegram_app():
    global _backup_app
    req = HTTPXRequest(connection_pool_size=20, connect_timeout=20, read_timeout=TELEGRAM_TIMEOUT, write_timeout=TELEGRAM_TIMEOUT)
    app = Application.builder().token(BOT_TOKEN).request(req).build()
    _backup_app = app

    app.add_error_handler(_error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    # on_document handler hataya gaya hai kyunki file upload ab web se hoga
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _route_text))
    return app


async def _route_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    handled = await on_admin_text(update, context)
    if handled: return
    await on_text(update, context)


async def post_init_tg(app):
    db.set_backup_sender(_backup_sender)
    app.bot_data["poll_task"] = asyncio.create_task(_poll_loop(app))


async def post_shutdown_tg(app):
    t = app.bot_data.get("poll_task")
    if t: t.cancel()