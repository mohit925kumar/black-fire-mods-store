import asyncio
import logging
import threading
import time
import requests
import json
import uuid
from datetime import datetime, timezone

import database as db
import razorpay_api as rzp
import ffglory_api as ff
import keyauth
import discord_store as ds
from config import (
    BOT_TOKEN, DISCORD_BOT_TOKEN, validate_config, FIREBASE_DB_URL
)
from telegram_bot import build_telegram_app, post_init_tg, post_shutdown_tg
from backup_store import backup_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")

WEB_SECRET_KEY = "FFGlory_Secure_2026"

# ─────────────────────────────────────────────
# DISCORD
# ─────────────────────────────────────────────
def run_discord_in_thread():
    # Discord is intentionally paused until the Website + Telegram system is ready.
    # Do not crash or emit a traceback when discord_bot.py has not been added yet.
    if not DISCORD_BOT_TOKEN:
        log.info("Discord bot is paused (DISCORD_BOT_TOKEN is blank).")
        return
    try:
        from discord_bot import bot as dc_bot
    except ModuleNotFoundError as exc:
        if exc.name == "discord_bot":
            log.info("Discord bot is paused: discord_bot.py is not present yet. Website and Telegram will continue.")
            return
        log.exception("Discord bot dependency is missing; Discord remains paused.")
        return
    except Exception:
        log.exception("Discord bot could not be imported; Website and Telegram will continue.")
        return

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        log.info("Discord bot starting (separate thread)...")
        loop.run_until_complete(dc_bot.start(DISCORD_BOT_TOKEN))
    except Exception:
        log.exception("Discord bot stopped with an error; Website and Telegram will continue.")
    finally:
        try:
            loop.close()
        except Exception:
            pass

# ─────────────────────────────────────────────
# WEB SERVER
# ─────────────────────────────────────────────
def run_web_server():
    try:
        from app import app as flask_app
        log.info("Web Server starting on http://0.0.0.0:5000")
        flask_app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except Exception:
        log.exception("Web Server crashed")

# ─────────────────────────────────────────────
# PAYMENT ADMIN HELPERS
# ─────────────────────────────────────────────
def _payment_search_result(order_id: str, expected_utr: str = ""):
    order_id = str(order_id or "").strip()
    expected_utr = str(expected_utr or "").strip()
    if not order_id or len(order_id) > 120:
        raise ValueError("Valid Order ID daalein (max 120 characters).")
    if len(expected_utr) > 100:
        raise ValueError("UTR max 100 characters ho sakta hai.")
    row = db.get_order(order_id)
    local_order = dict(row) if row else None
    result = rzp.search_payments_by_order_id(order_id, local_order=local_order)
    result["expected_utr"] = expected_utr
    if expected_utr:
        for payment in result.get("payments", []):
            acquirer = payment.get("acquirer_data") or {}
            provider_utr = str(acquirer.get("utr") or acquirer.get("rrn") or acquirer.get("reference_number") or "").strip()
            payment["admin_utr_check"] = ("MATCH" if provider_utr == expected_utr else "MISMATCH") if provider_utr else "UTR_NOT_RETURNED_BY_RAZORPAY"
    return result


def _verify_manual_credit(cmd: dict):
    """Only credit after Razorpay confirms a captured INR payment of exact amount."""
    user_id = int(cmd.get("user_id") or 0)
    platform = str(cmd.get("platform") or "").strip().lower()
    amount_paise = int(cmd.get("amount_paise") or 0)
    order_id = str(cmd.get("order_id") or "").strip()
    payment_id = str(cmd.get("razorpay_payment_id") or "").strip()
    utr = str(cmd.get("utr") or "").strip()

    if user_id <= 0 or platform not in ("tg", "dc"):
        raise ValueError("User ID aur platform (Telegram/Discord) sahi daalein.")
    if amount_paise < 100:
        raise ValueError("Amount minimum ₹1 hona chahiye.")
    if not order_id or not payment_id:
        raise ValueError("Order ID aur captured Razorpay Payment ID zaroori hain.")

    payment = rzp.fetch_payment(payment_id)
    if str(payment.get("status", "")).lower() != "captured":
        raise ValueError(f"Payment captured nahi hai. Razorpay status: {payment.get('status', 'unknown')}")
    if str(payment.get("currency", "INR")).upper() != "INR":
        raise ValueError("Payment currency INR nahi hai; balance add nahi kiya.")
    if int(payment.get("amount", -1)) != amount_paise:
        raise ValueError("Dashboard amount aur Razorpay payment amount match nahi karte.")

    local = db.get_order(order_id)
    local = dict(local) if local else None
    notes = payment.get("notes") or {}
    razorpay_order_id = str(payment.get("order_id") or "")
    notes_order_id = str(notes.get("order_id") or notes.get("receipt") or "")
    related = (notes_order_id == order_id or razorpay_order_id == order_id)
    if local and local.get("qr_code_id"):
        try:
            qr_result = rzp.fetch_qr_payments(local["qr_code_id"])
            qr_items = qr_result.get("items", []) if isinstance(qr_result, dict) else []
            related = related or any(str(item.get("id") or "") == payment_id for item in qr_items)
        except Exception as exc:
            log.warning("QR association check failed for %s: %s", order_id, str(exc)[:160])
    if not related:
        raise ValueError("Payment is captured, lekin diye gaye Order ID se link verify nahi hua. Search result me sahi order/payment ID check karein.")

    # If a UTR is supplied and Razorpay exposes one, ensure it matches.
    acquirer = payment.get("acquirer_data") or {}
    provider_utr = str(acquirer.get("utr") or acquirer.get("rrn") or acquirer.get("reference_number") or "").strip()
    if utr and provider_utr and utr != provider_utr:
        raise ValueError("Entered UTR Razorpay ke UTR/RRN se match nahi karta.")

    ok, message, transaction = db.add_balance_with_ledger(
        user_id=user_id, platform=platform, paise=amount_paise,
        order_id=order_id, utr=utr or provider_utr,
        razorpay_payment_id=payment_id,
        note=f"Admin verified Razorpay payment; status={payment.get('status')}"
    )
    if not ok:
        raise ValueError(message)
    user = db.get_user(user_id, platform)
    return {
        "message": message,
        "transaction": transaction,
        "new_balance_paise": int(user["balance_paise"]) if user else None,
        "verified_payment": {
            "id": payment.get("id"), "status": payment.get("status"),
            "amount": payment.get("amount"), "currency": payment.get("currency"),
            "method": payment.get("method"), "order_id": payment.get("order_id"),
            "created_at": payment.get("created_at"),
        }
    }


# ─────────────────────────────────────────────
# FIREBASE BRIDGE (Web <-> Bot)
# ─────────────────────────────────────────────
def firebase_bridge_loop():
    time.sleep(2) # Bot start hone do
    log.info("🔥 Firebase Bridge started! (Connecting Web <-> Bot)")
    
    while True:
        try:
            # ==========================================
            # 1. BOT TO WEB (Live Data Push)
            # ==========================================
            try:
                ds.init_db()
                stats = db.get_stats()
                api_info = ff.get_account()

                # Track actual observed API credit changes and publish real history.
                # The first snapshot is a baseline, not a fabricated addition.
                db.track_api_credit_snapshot(api_info)
                api_credit_history = db.list_api_credit_history(limit=100)
                usage_history = db.get_usage_history(days=14)
                manual_credit_history = db.list_manual_balance_transactions(limit=100)

                # Products list
                products_list = {}
                for p in db.list_products(enabled_only=False):
                    products_list[p["product_id"]] = {
                        "name": p["name"],
                        "price_paise": p["price_paise"],
                        "product_type": p["product_type"] if "product_type" in p.keys() else ("guild_glory" if p["is_default"] else "keyauth"),
                        "enabled": bool(p["enabled"]),
                        "product_link": p["product_link"] if "product_link" in p.keys() else "",
                        "duration_options": [dict(o) for o in db.list_product_duration_options(p["product_id"])] if str(p["product_type"] or "").lower() == "keyauth" else [],
                    }

                # Users List (Web Table ke liye)
                with db.db() as con:
                    users_rows = con.execute("SELECT * FROM users ORDER BY joined_at DESC LIMIT 50").fetchall()
                    users_list = [dict(u) for u in users_rows]

                # Product Stats (Sales Data)
                product_stats = []
                try:
                    with db.db() as con:
                        # Yahan se 'WHERE is_default=0' HATA DIYA HAI
                        prods = con.execute("SELECT * FROM products").fetchall()
                        for p in prods:
                            # Total orders for this product
                            sales = con.execute(
                                "SELECT COUNT(*) as total_orders, COALESCE(SUM(amount_paise), 0) as total_sales FROM orders WHERE product_id=? AND payment_status='paid'",
                                (p["product_id"],)
                            ).fetchone()
                            
                            product_stats.append({
                                "product_id": p["product_id"],
                                "name": p["name"],
                                "price_paise": p["price_paise"],
                                "created_at": p["created_at"],
                                "total_orders": sales["total_orders"],
                                "total_sales_paise": sales["total_sales"]
                            })
                except Exception as e:
                    log.error(f"Product stats error: {e}")

                live_data = {
                    "stats": stats,
                    "api_credit": api_info,
                    "api_credit_history": api_credit_history,
                    "usage_history": usage_history,
                    "manual_credit_history": manual_credit_history,
                    "products": products_list,
                    "discord_servers": ds.list_servers(),
                    "discord_sellers": ds.list_sellers(),
                    "users": users_list,
                    "product_stats": product_stats,
                    "bot_status": "online",
                    "guild_glory_enabled": db.get_setting("guild_glory_enabled", "1") == "1",
                    "last_update": int(time.time())
                }
                
                r = requests.put(f"{FIREBASE_DB_URL}/live_data.json", json=live_data, timeout=10)
                
                if not r.ok:
                    log.error(f"❌ Live data push failed (Status: {r.status_code}): {r.text[:200]}")
                else:
                    log.info(f"📤 Live Data Pushed successfully! ({len(users_list)} users, {len(product_stats)} products)")
                    
            except Exception as e:
                log.error(f"❌ Live data push error: {e}")

            # ==========================================
            # 2. WEB TO BOT (Command Listener)
            # ==========================================
            try:
                r = requests.get(f"{FIREBASE_DB_URL}/admin_requests.json", timeout=10)
                if r.ok and r.json():
                    requests_data = r.json()
                    
                    for key, cmd in requests_data.items():
                        if cmd.get("status") == "pending" and cmd.get("secret") == WEB_SECRET_KEY:
                            action = cmd.get("action")
                            log.info(f"📥 Web Command Received: {action}")
                            
                            try:
                                if action == "search_payment_by_order":
                                    search_result = _payment_search_result(cmd.get("order_id", ""), cmd.get("utr", ""))
                                    requests.patch(
                                        f"{FIREBASE_DB_URL}/admin_requests/{key}.json",
                                        json={"status": "completed", "result": search_result}, timeout=10
                                    )
                                    log.info("Payment search completed for order %s (%s results)",
                                             cmd.get("order_id"), search_result.get("payment_count", 0))
                                    continue

                                elif action == "manual_add_payment":
                                    credit_result = _verify_manual_credit(cmd)
                                    # Notify Telegram users after the balance ledger commits. Discord DM
                                    # support will be connected when discord_bot.py is added later.
                                    txn = credit_result.get("transaction") or {}
                                    if txn.get("platform") == "tg":
                                        try:
                                            requests.post(
                                                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                                json={"chat_id": txn.get("user_id"),
                                                      "text": (f"✅ Wallet balance added: ₹{txn.get('amount_paise', 0) / 100:.2f}\n"
                                                               f"Transaction: {txn.get('transaction_id')}\n"
                                                               f"Order ID: {txn.get('order_id')}" )},
                                                timeout=8
                                            )
                                        except Exception as notify_error:
                                            log.warning("Balance credited but Telegram DM failed: %s", str(notify_error)[:160])
                                    requests.patch(
                                        f"{FIREBASE_DB_URL}/admin_requests/{key}.json",
                                        json={"status": "completed", "result": credit_result}, timeout=10
                                    )
                                    log.info("Manual payment credit completed: %s", credit_result["transaction"]["transaction_id"])
                                    continue

                                elif action == "broadcast":
                                    msg = cmd.get("message")
                                    with db.db() as con:
                                        users = con.execute("SELECT user_id, platform FROM users").fetchall()
                                    for u in users:
                                        if u["platform"] == "tg":
                                            try: requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": u["user_id"], "text": msg}, timeout=5)
                                            except: pass
                                
                                elif action == "list_keyauth_accounts":
                                    accounts = db.list_admin_keyauth_accounts(1000)
                                    safe_accounts = []
                                    for account in accounts:
                                        safe_accounts.append({
                                            "order_id": account.get("order_id"),
                                            "platform": account.get("platform"),
                                            "product_id": account.get("product_id"),
                                            "product_name": account.get("display_product_name") or account.get("product_name"),
                                            "username": account.get("keyauth_username"),
                                            "password": account.get("keyauth_password"),
                                            "created_at": account.get("created_at"),
                                            "expiry": account.get("license_expires_at"),
                                            "days": int(account.get("duration_days") or 0),
                                            "status": account.get("order_status"),
                                        })
                                    requests.patch(f"{FIREBASE_DB_URL}/admin_requests/{key}.json",
                                                   json={"status": "completed", "result": {"accounts": safe_accounts, "count": len(safe_accounts)}}, timeout=10)
                                    continue

                                elif action in ("reset_keyauth_account", "delete_keyauth_account"):
                                    order_id = str(cmd.get("order_id") or "").strip()
                                    row = db.get_order(order_id)
                                    if not row or row["platform"] not in ("tg", "admin") or not row["keyauth_username"]:
                                        raise ValueError("KeyAuth account record nahi mila.")
                                    order = dict(row)
                                    product_row = db.get_product(str(order.get("product_id") or ""))
                                    if not product_row or str(product_row["product_type"] or "").lower() != "keyauth":
                                        raise ValueError("Original KeyAuth product/API configuration nahi mila.")
                                    product = dict(product_row)
                                    seller_key = str(product.get("keyauth_api_key") or "").strip()
                                    username = str(order.get("keyauth_username") or "").strip()
                                    password = "1"
                                    subscription = str(order.get("keyauth_subscription") or product.get("keyauth_subscription") or "default")
                                    if not seller_key:
                                        raise ValueError("Product ki KeyAuth Seller API key missing hai.")
                                    if action == "reset_keyauth_account":
                                        days = int(order.get("duration_days") or 0)
                                        if days < 1:
                                            raise ValueError("Original purchase duration record nahi hai; reset nahi kar sakte.")
                                        result = keyauth.reset_user_for_days(seller_key, username, password, subscription, days)
                                        expiry_unix = keyauth.subscription_expiry(result.get("data") or {}, subscription)
                                        if not expiry_unix:
                                            raise RuntimeError("Reset ke baad expiry KeyAuth se verify nahi hui.")
                                        expiry_iso = datetime.fromtimestamp(float(expiry_unix), tz=timezone.utc).isoformat()
                                        with db.db() as con:
                                            matches = con.execute("SELECT order_id FROM orders WHERE platform IN ('tg','admin') AND product_id=? AND keyauth_username=? AND order_status!='deleted'", (order.get("product_id"), username)).fetchall()
                                        for match in matches:
                                            db.update_order(match["order_id"], order_status="running", delivered_key=username, keyauth_username=username, keyauth_password=password, keyauth_subscription=subscription, license_expires_at=expiry_iso, error="Admin reset: full purchased duration restored")
                                        requests.patch(f"{FIREBASE_DB_URL}/admin_requests/{key}.json", json={"status":"completed","result":{"message":f"{username} reset ho gaya; {days} din ki validity dobara start hui.","expiry":expiry_iso}}, timeout=10)
                                    else:
                                        keyauth.delete_user(seller_key, username)
                                        with db.db() as con:
                                            matches = con.execute("SELECT order_id FROM orders WHERE platform IN ('tg','admin') AND product_id=? AND keyauth_username=? AND order_status!='deleted'", (order.get("product_id"), username)).fetchall()
                                        for match in matches:
                                            db.update_order(match["order_id"], order_status="deleted", delivered_key=None, keyauth_password=None, error="Permanently deleted by Main Admin")
                                        requests.patch(f"{FIREBASE_DB_URL}/admin_requests/{key}.json", json={"status":"completed","result":{"message":f"KeyAuth user {username} permanently deleted.","deleted":True}}, timeout=10)
                                    continue

                                elif action == "generate_keyauth_user":
                                    product_id = str(cmd.get("product_id") or "").strip()
                                    days = int(cmd.get("days") or 0)
                                    product_row = db.get_product(product_id)
                                    if not product_row:
                                        raise ValueError("Selected product/project nahi mila.")
                                    product = dict(product_row)
                                    if str(product.get("product_type") or "").lower() != "keyauth" or not product.get("keyauth_api_key"):
                                        raise ValueError("Sirf valid KeyAuth product/project select karein.")
                                    if days < 1 or days > 36500:
                                        raise ValueError("Days 1 se 36500 ke beech hone chahiye.")
                                    import secrets, string
                                    alphabet = string.ascii_letters + string.digits
                                    username = ""
                                    for _ in range(20):
                                        candidate = "".join(secrets.choice(alphabet) for _ in range(8))
                                        if keyauth.get_user_data(product["keyauth_api_key"], candidate) is None:
                                            username = candidate
                                            break
                                    if not username:
                                        raise RuntimeError("Unique username nahi mila. Dobara try karein.")
                                    password = "1"
                                    subscription = str(product.get("keyauth_subscription") or "default")
                                    keyauth.create_user(product["keyauth_api_key"], username, password, subscription, days)
                                    verify = keyauth.get_user_data(product["keyauth_api_key"], username)
                                    expiry_unix = keyauth.subscription_expiry(verify or {}, subscription)
                                    if verify is None or expiry_unix is None:
                                        raise RuntimeError("KeyAuth user/subscription verification fail hui. Subscription/API key check karein.")
                                    expiry_iso = datetime.fromtimestamp(float(expiry_unix), tz=timezone.utc).isoformat()
                                    order_id = "ADM" + uuid.uuid4().hex[:10].upper()
                                    db.create_order(order_id, 0, "admin", "Main Admin", product_id, product.get("name"), "", "", "", "", int(product.get("price_paise") or 100), duration_days=days, duration_label=f"{days} Days")
                                    db.update_order(order_id, payment_status="paid", order_status="running", delivered_key=username, keyauth_username=username, keyauth_password=password, keyauth_subscription=subscription, license_expires_at=expiry_iso, error="Created from Main Admin panel")
                                    requests.patch(f"{FIREBASE_DB_URL}/admin_requests/{key}.json", json={"status":"completed","result":{"username":username,"password":password,"product":product.get("name"),"days":days,"order_id":order_id,"expiry":expiry_iso}}, timeout=10)
                                    log.info("KeyAuth account created from admin web: product=%s username=%s days=%s", product_id, username, days)
                                    continue

                                elif action == "add_product":
                                    name = str(cmd.get("name") or "").strip()
                                    price = int(cmd.get("price") or 0)
                                    product_type = str(cmd.get("product_type") or "keyauth").lower()
                                    api_key = str(cmd.get("api_key") or "").strip()
                                    subscription = str(cmd.get("keyauth_subscription") or "default").strip()
                                    description = str(cmd.get("description") or "").strip()
                                    product_id = db.add_product(name, description, price * 100,
                                                                keyauth_api_key=api_key,
                                                                product_type=product_type,
                                                                keyauth_subscription=subscription,
                                                                product_link=str(cmd.get("product_link") or ""))
                                    log.info("Product added: %s (%s)", product_id, product_type)

                                elif action == "set_guild_glory_enabled":
                                    enabled = bool(cmd.get("enabled"))
                                    db.set_setting("guild_glory_enabled", "1" if enabled else "0")
                                    log.warning("Guild Glory new-order processing %s by web admin", "ENABLED" if enabled else "PAUSED")
                                
                                elif action == "update_price":
                                    db.update_product_price(cmd.get("product"), int(cmd.get("new_value")) * 100)

                                elif action == "add_duration_option":
                                    db.add_product_duration_option(cmd.get("product"), cmd.get("label"), int(cmd.get("duration_days")), int(float(cmd.get("price") or 0) * 100))
                                    log.info("Duration option added for product %s", cmd.get("product"))

                                elif action == "update_duration_option":
                                    db.update_product_duration_option(cmd.get("option_id"), int(float(cmd.get("price") or 0) * 100))

                                elif action == "delete_duration_option":
                                    db.delete_product_duration_option(cmd.get("option_id"))

                                elif action == "update_name":
                                    db.update_product_name(cmd.get("product"), cmd.get("new_value"))

                                elif action == "update_product_link":
                                    db.update_product_link(cmd.get("product"), cmd.get("product_link", ""))

                                # NAYA: Delete Product Handler
                                elif action == "delete_product":
                                    db.delete_product(cmd.get("product_id"))
                                    log.info(f"🗑 Product deleted: {cmd.get('product_id')}")

                                elif action == "add_seller":
                                    if cmd.get("platform") == "dc":
                                        ds.init_db()
                                        guild_id = str(cmd.get("server_id", "")).strip()
                                        known_servers = ds.list_servers()
                                        matched = next((x for x in known_servers if guild_id == str(x.get("guild_id")) or guild_id.casefold() == str(x.get("guild_name", "")).casefold()), None)
                                        if matched:
                                            guild_id = str(matched["guild_id"])
                                        ds.activate_server(guild_id)
                                        if not cmd.get("seller_username") or not cmd.get("seller_password"):
                                            raise ValueError("Seller username aur password required hain.")
                                        # Create web login first so a duplicate cannot leave a misleading legacy seller row.
                                        ds.create_seller(str(cmd.get("user_id")), guild_id, cmd.get("seller_username"), cmd.get("seller_password"))
                                        try:
                                            db.add_seller(int(cmd.get("user_id")), "dc", "Seller Admin", int(cmd.get("server_id")))
                                        except Exception as legacy_error:
                                            log.warning("Legacy seller record already exists or could not be added: %s", legacy_error)
                                        try:
                                            requests.patch(f"{FIREBASE_DB_URL}/admin_requests/{key}.json", json={"seller_password": None}, timeout=8)
                                        except Exception:
                                            log.warning("Could not clear plaintext seller password from Firebase command %s", key)
                                    else:
                                        db.add_seller(int(cmd.get("user_id")), cmd.get("platform"), "Seller Admin", int(cmd.get("server_id")))

                                elif action == "delete_dc_seller":
                                    ds.init_db()
                                    if not ds.delete_seller(int(cmd.get("seller_id"))):
                                        raise ValueError("Seller login nahi mila ya pehle hi delete ho chuka hai.")
                                    log.info("Discord seller login deleted: %s", cmd.get("seller_id"))

                                elif action == "reset_dc_seller_password":
                                    ds.init_db()
                                    ds.reset_seller_password(int(cmd.get("seller_id")), cmd.get("new_password"))
                                    log.info("Discord seller password reset: seller_id=%s", cmd.get("seller_id"))

                                elif action == "set_dc_server_status":
                                    ds.init_db()
                                    ds.set_server_active(str(cmd.get("server_id")), bool(cmd.get("active")))
                                    log.info("Discord server %s active=%s", cmd.get("server_id"), cmd.get("active"))

                                elif action == "delete_user":
                                    db.delete_user_data(int(cmd.get("user_id")), cmd.get("platform"))

                                elif action == "wipe_data":
                                    db.wipe_all_data()
                                
                                elif action == "export_data":
                                    data = db.export_all_data()
                                    requests.put(f"{FIREBASE_DB_URL}/live_data/export_file.json", json=data, timeout=10)
                                    log.info("✅ Export Data pushed to Firebase")

                                elif action == "restore_data":
                                    data = cmd.get("data")
                                    if data:
                                        db.restore_from_export(data)
                                        log.info("✅ Restore Data processed")
                                
                                requests.patch(f"{FIREBASE_DB_URL}/admin_requests/{key}.json", json={"status": "completed"}, timeout=10)
                                log.info(f"✅ Command {action} completed.")
                                
                            except Exception as e:
                                log.error(f"❌ Command {action} fail: {e}")
                                requests.patch(f"{FIREBASE_DB_URL}/admin_requests/{key}.json", json={"status": "failed", "error": str(e)}, timeout=10)
            except Exception as e:
                log.error(f"❌ Command fetch error: {e}")

        except Exception as e:
            pass
        
        time.sleep(2)


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
async def run_telegram():
    validate_config()
    db.init_db()

    tg_app = build_telegram_app()
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(allowed_updates=None)
    await post_init_tg(tg_app)
    log.info("Telegram bot started")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await post_shutdown_tg(tg_app)
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        log.info("Telegram bot stopped")


def main():
    threading.Thread(target=backup_loop, daemon=True, name="telegram-channel-backup").start()
    threading.Thread(target=run_discord_in_thread, daemon=True, name="discord-bot").start()
    threading.Thread(target=run_web_server, daemon=True, name="web-server").start()
    threading.Thread(target=firebase_bridge_loop, daemon=True, name="firebase-bridge").start()

    try:
        asyncio.run(run_telegram())
    except KeyboardInterrupt:
        log.info("Stopped by user")


if __name__ == "__main__":
    main()