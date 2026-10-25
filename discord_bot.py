import asyncio
import uuid
import logging
import time
import secrets
from datetime import datetime, timezone, timedelta
from io import BytesIO

import discord
from discord import app_commands
import requests

import database as db
import discord_store as ds
import keyauth
import razorpay_api as rzp
import ffglory_api as ff
from config import DISCORD_BOT_TOKEN, ADMIN_ID, PRICE_INR

log = logging.getLogger("dc")

# ============ CONFIG ============
QR_EXPIRE_SECONDS = 5 * 60
TIMER_UPDATE_EVERY = 30
PAYMENT_DONE_AUTO_DELETE = 5
AUTO_DELETE_MESSAGES = True
PROMPT_AUTO_DELETE = 15
FAST_DELETE = 3
MENU_TIMEOUT = 10 # Payment menu auto-close hone ka time
COMMISSION_PERCENT = 5 # 5% for Main Admin, 95% to Seller

# Pending user actions (guild_id / amount / feedback) — keyed by user_id
PENDING = {}


# ============ BOT CLASS ============

class DiscordBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        try:
            await self.tree.sync()
            log.info("Slash commands synced globally")
        except Exception:
            log.exception("setup_hook sync error")

        self.loop.create_task(self._poll_loop())

    async def on_guild_join(self, guild: discord.Guild):
        ds.init_db()
        ds.register_server(guild.id, guild.name)
        log.info("Discord server registered: %s (%s)", guild.name, guild.id)

    async def on_ready(self):
        ds.init_db()
        for guild in self.guilds:
            ds.register_server(guild.id, guild.name)
        log.info("Discord bot ready: %s (id=%s)", self.user, self.user.id)
        log.info("Guilds: %s", [g.name for g in self.guilds])

    # ---------- MESSAGE HANDLER ----------
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
            
        # SERVER VALIDITY CHECK
        if message.guild and not ds.get_server(message.guild.id):
            return

        # Sirf !start ya bot mention
        content = message.content.strip().lower()
        bot_mentioned = f"<@{self.user.id}>" in content or f"<@!{self.user.id}>" in content

        if content == "!start" or bot_mentioned:
            db.upsert_user(message.author.id, "dc", message.author.name, message.author.display_name)

            view = MainMenuView(message.author.id)
            embed = discord.Embed(
                title=f"👋 Welcome, {message.author.display_name}!",
                description=(
                    "⭐️ ━ **SHOP FEATURES** ━ ⭐️\n\n"
                    "├ 🔑 Premium Game Keys\n"
                    "├ ⚡️ Instant Delivery 24/7\n"
                    "├ 🔒 100% Secure Payment\n"
                    "├ 💸 Best Prices Guaranteed\n"
                    "├ 🎁 Referral Rewards\n"
                    "└ 🏆 Professional Support\n\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "🚀 **Click Shop Now to Start!**"
                ),
                color=0xff9900)
            await message.reply(embed=embed, view=view)
            return

        # Pending states
        uid = message.author.id
        state = PENDING.get(uid)
        if not state:
            return

        step = state.get("step")

        if message.guild:
            if step == "awaiting_guild_id":
                PENDING.pop(uid, None)
                await _handle_guild_input(message, message.content.strip(), state)
                return

        # DM inputs
        if not message.guild:
            if step == "awaiting_amount":
                PENDING.pop(uid, None)
                await _handle_amount_input(message, message.content.strip())
                return
            if step == "awaiting_feedback":
                PENDING.pop(uid, None)
                await _handle_feedback_input(message, message.content.strip())
                return

    # ---------- POLL LOOP ----------
    async def _poll_loop(self):
        while True:
            try:
                rows = await asyncio.to_thread(db.list_pending_orders)
                for r in rows:
                    if r["platform"] != "dc":
                        continue
                    try:
                        await self._verify(r)
                    except Exception:
                        log.exception("dc verify fail %s", r["order_id"])
            except Exception:
                log.exception("dc poll error")
            await asyncio.sleep(5)

    # ---------- VERIFY (ATOMIC) ----------
    async def _verify(self, row):
        if row["payment_status"] != "created":
            return
        if row["order_status"] not in ("awaiting_payment", "creating_payment"):
            return

        try:
            res = await asyncio.to_thread(rzp.fetch_qr_payments, row["qr_code_id"])
        except Exception:
            return

        items = res.get("items", []) if isinstance(res, dict) else []
        match = next((p for p in items if str(p.get("status", "")).lower() == "captured" and int(p.get("amount", -1)) == int(row["amount_paise"])), None)
        if not match:
            return

        rzp_id = match.get("id")

        if rzp_id and db.payment_exists(rzp_id):
            db.claim_order_payment(row["order_id"], rzp_id)
            return

        claimed = db.claim_order_payment(row["order_id"], rzp_id)
        if not claimed:
            return

        inserted = db.record_payment(rzp_id or uuid.uuid4().hex, row["order_id"], row["user_id"], "dc", row["amount_paise"], match.get("method"), rzp_id)
        if not inserted:
            return

        # QR delete
        if row["qr_message_id"] and row["qr_chat_id"]:
            try:
                ch = self.get_channel(int(row["qr_chat_id"]))
                if ch:
                    msg = await ch.fetch_message(int(row["qr_message_id"]))
                    await msg.delete()
            except Exception:
                pass

        user = self.get_user(int(row["user_id"]))
        if not user:
            try:
                user = await self.fetch_user(int(row["user_id"]))
            except Exception:
                user = None

        if user and AUTO_DELETE_MESSAGES:
            try:
                done_msg = await user.send(f"✅ **Payment Done!**\n\nOrder: `{row['order_id']}`\nAmount: **₹{row['amount_paise']//100}**")
                asyncio.create_task(_auto_delete_msg(done_msg))
            except Exception:
                pass

        if row["product_id"] == "topup":
            db.add_balance(int(row["user_id"]), "dc", row["amount_paise"])
            return

        # Discord product delivery is sourced only from the Discord Seller Admin Panel.
        seller_product = None
        try:
            seller_product = ds.get_published_product(row["product_id"], row["clan_id"] or None)
            if not seller_product:
                seller_product = ds.get_published_product(row["product_id"])
        except Exception:
            log.exception("Discord seller product lookup failed for %s", row["order_id"])
        if not seller_product:
            db.update_order(row["order_id"], order_status="key_delivery_review",
                            error="Discord seller product not found/published; Main Admin catalog is Telegram-only")
            log.error("Discord order %s product is not in seller catalog", row["order_id"])
            return
        try:
            await _deliver_seller_product(row, seller_product, user)
        except Exception as exc:
            db.update_order(row["order_id"], order_status="key_delivery_review", error=str(exc)[:500])
            log.exception("Seller KeyAuth delivery failed for %s", row["order_id"])
            if user:
                try:
                    await user.send(f"⚠️ Payment receive ho gayi, lekin delivery pending hai. Admin ko Order ID `{row['order_id']}` dein. Dobara payment na karein.")
                except Exception:
                    pass

# ============ HELPERS ============

async def _deliver_main_keyauth_order(row, product, user=None, channel=None):
    """Deliver a Main Admin KeyAuth product using a unique 8-char username."""
    row, product = dict(row), dict(product)
    days = int(row.get("duration_days") or 0)
    if days < 1 or days > 36500:
        raise RuntimeError("Order duration missing/invalid hai; admin review zaroori hai.")
    seller_key = str(product.get("keyauth_api_key") or "").strip()
    subscription = str(product.get("keyauth_subscription") or "default").strip()
    if not seller_key:
        raise RuntimeError("Product ki KeyAuth Seller API key missing hai.")
    preferred = str(row.get("keyauth_username") or "").strip()
    result = await asyncio.to_thread(
        keyauth.create_unique_or_extend_user, seller_key, subscription, days, "1", preferred
    )
    username = str(result.get("username") or "")
    password = "1"
    expiry_unix = keyauth.subscription_expiry(result.get("data") or {}, subscription)
    if not result.get("created"):
        previous = result.get("previous_expiry")
        if previous and (expiry_unix is None or expiry_unix <= previous):
            expiry_unix = int(max(float(previous), datetime.now(timezone.utc).timestamp()) + days * 86400)
    expiry_iso = (datetime.fromtimestamp(float(expiry_unix), tz=timezone.utc).isoformat()
                  if expiry_unix else (datetime.now(timezone.utc) + timedelta(days=days)).isoformat())
    db.update_order(row["order_id"], order_status="running", delivered_key=username,
                    keyauth_username=username, keyauth_password=password,
                    keyauth_subscription=subscription, license_expires_at=expiry_iso, error="")
    try:
        db.update_keyauth_account_orders(row["user_id"], row["platform"], row["product_id"], username, expiry_iso, password, "")
    except Exception:
        log.exception("KeyAuth account history update failed for order %s", row["order_id"])
    target = user
    if target is None:
        try:
            target = bot.get_user(int(row["user_id"])) or await bot.fetch_user(int(row["user_id"]))
        except Exception:
            target = None
    message = (f"✅ **KeyAuth Product Delivered Successfully!**\n\nOrder: `{row['order_id']}`\n"
               f"Product: **{row.get('product_name') or product.get('name', 'Product')}**\n"
               f"Validity: **{row.get('duration_label') or f'{days} days'}**\n"
               f"Username: `{username}`\nPassword: `{password}`\nSubscription: `{subscription}`\n"
               f"Expiry UTC: `{expiry_iso[:19]}`")
    if product.get("product_link"):
        message += f"\nProduct Link: {product['product_link']}"
    if target:
        try:
            await target.send(message)
        except Exception:
            if channel:
                await channel.send(f"<@{row['user_id']}> payment verified, but DM is closed. Please open DM for KeyAuth credentials.")
    return username


async def _deliver_seller_product(row, product, user=None, channel=None):
    """Deliver a Discord Seller Admin product with a unique username and password 1."""
    row, product = dict(row), dict(product)
    days = int(row.get("duration_days") or 0)
    if days < 1 or days > 36500:
        raise RuntimeError("Order duration missing. Admin ko order check karna hoga.")
    seller_key = str(product.get("seller_key") or "").strip()
    subscription = str(product.get("subscription") or "default").strip()
    if not seller_key:
        raise RuntimeError("Seller product ki KeyAuth Seller API key missing hai.")
    preferred = str(row.get("keyauth_username") or "").strip()
    result = await asyncio.to_thread(
        keyauth.create_unique_or_extend_user, seller_key, subscription, days, "1", preferred
    )
    username = str(result.get("username") or "")
    password = "1"
    expiry_unix = keyauth.subscription_expiry(result.get("data") or {}, subscription)
    if not result.get("created"):
        previous = result.get("previous_expiry")
        if previous and (expiry_unix is None or expiry_unix <= previous):
            expiry_unix = int(max(float(previous), datetime.now(timezone.utc).timestamp()) + days * 86400)
    expiry = (datetime.fromtimestamp(float(expiry_unix), tz=timezone.utc).isoformat()
              if expiry_unix else (datetime.now(timezone.utc) + timedelta(days=days)).isoformat())
    db.update_order(row["order_id"], order_status="running", delivered_key=username,
                    keyauth_username=username, keyauth_password=password, keyauth_subscription=subscription,
                    license_expires_at=expiry, error="")
    try:
        db.update_keyauth_account_orders(row["user_id"], row["platform"], row["product_id"], username, expiry, password, "")
    except Exception:
        log.exception("Seller KeyAuth history update failed for order %s", row["order_id"])
    message = (f"✅ **Product Delivered Successfully!**\n\nOrder: `{row['order_id']}`\n"
               f"Product: **{product['name']}**\nUsername: `{username}`\nPassword: `{password}`\n"
               f"Validity: **{row.get('duration_label') or f'{days} days'}**")
    target = user
    if target is None:
        try:
            target = bot.get_user(int(row["user_id"])) or await bot.fetch_user(int(row["user_id"]))
        except Exception:
            target = None
    if target:
        try:
            await target.send(message)
        except Exception:
            if channel:
                await channel.send(f"<@{row['user_id']}> payment verified; delivery ready. Please open DM.")
    return username


async def _auto_delete_msg(msg: discord.Message, delay: int = PAYMENT_DONE_AUTO_DELETE):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


async def _qr_timer(channel, message_id, order_id, original_content):
    expires_at = time.time() + QR_EXPIRE_SECONDS
    while True:
        remaining = int(expires_at - time.time())
        if remaining <= 0:
            break

        row = db.get_order(order_id)
        if not row or row["payment_status"] != "created":
            return

        mins, secs = divmod(remaining, 60)
        try:
            if hasattr(channel, "fetch_message"):
                msg = await channel.fetch_message(message_id)
                # Timer update via embed description
                embed = msg.embeds[0] if msg.embeds else None
                if embed:
                    new_desc = embed.description.split("\n\n⏱")[0] # Remove old timer
                    embed.description = f"{new_desc}\n\n⏱ **QR Expires in:** `{mins:02d}:{secs:02d}`"
                    await msg.edit(embed=embed)
        except Exception:
            pass

        await asyncio.sleep(TIMER_UPDATE_EVERY)

    row = db.get_order(order_id)
    if not row or row["payment_status"] != "created":
        return

    try:
        if hasattr(channel, "fetch_message"):
            msg = await channel.fetch_message(message_id)
            await msg.delete()
    except Exception:
        pass

    db.update_order(order_id, order_status="expired", payment_status="expired", error="QR expired after 5 min")

    try:
        expire_text = (f"⏰ **QR Expire ho gaya**\n\nOrder `{order_id}` cancel.")
        if hasattr(channel, "send"):
            expire_msg = await channel.send(expire_text)
            if AUTO_DELETE_MESSAGES:
                asyncio.create_task(_auto_delete_msg(expire_msg, 15))
    except Exception:
        pass


# ============ INPUT HANDLERS ============

async def _handle_amount_input(message: discord.Message, text: str):
    if not text.isdigit() or int(text) <= 0 or int(text) > 100000:
        err = await message.reply("❌ Valid amount bhejo (1–100000).")
        asyncio.create_task(_auto_delete_msg(err, FAST_DELETE))
        return

    u = message.author
    amount_paise = int(text) * 100
    topup_id = "TU" + uuid.uuid4().hex[:10].upper()

    db.upsert_user(u.id, "dc", u.name, u.display_name)
    db.create_order(topup_id, u.id, "dc", u.name, "topup", "Wallet Top-up", "", "", "", "", amount_paise)

    loading = await message.reply("⏳ **QR generate ho raha hai...** `0%`")

    try:
        qr = await asyncio.to_thread(rzp.create_qr, topup_id, amount_paise, u.display_name or u.name)
        db.update_order(topup_id, qr_code_id=qr["id"], qr_image_url=qr["image_url"], order_status="awaiting_payment", payment_status="created")
        try:
            await loading.delete()
        except Exception:
            pass

        img_bytes = requests.get(qr["image_url"], timeout=30).content
        file = discord.File(BytesIO(img_bytes), filename="qr.png")

        embed = discord.Embed(
            title="💰 Wallet Top-up",
            description=f"**Topup ID:** `{topup_id}`\n**Amount:** ₹{amount_paise//100}\n\n⏱ **QR Expires in:** `05:00`\n👇 Scan the QR below.",
            color=0xf1c40f
        )
        embed.set_image(url="attachment://qr.png")

        qr_msg = await message.channel.send(embed=embed, file=file)
        db.update_order(topup_id, qr_message_id=qr_msg.id, qr_chat_id=qr_msg.channel.id)

        asyncio.create_task(_qr_timer(qr_msg.channel, qr_msg.id, topup_id, "payment qr"))
    except Exception as e:
        log.exception("dc topup fail")
        err = await message.reply(f"❌ Fail: {str(e)[:200]}")
        asyncio.create_task(_auto_delete_msg(err, FAST_DELETE))


async def _handle_guild_input(message: discord.Message, guild_id: str, state: dict):
    if not guild_id.isdigit() or not (5 <= len(guild_id) <= 20):
        err = await message.reply("❌ Guild ID valid nahi (5–20 digits). Dobara bhejo.")
        asyncio.create_task(_auto_delete_msg(err, FAST_DELETE))
        PENDING[message.author.id] = state
        return

    product = state.get("product")
    region = state.get("region")
    if not product or not region:
        err = await message.reply("⚠️ Session expire. `!start` dabao.")
        asyncio.create_task(_auto_delete_msg(err, FAST_DELETE))
        return

    u = message.author
    amount_paise = product["price_paise"]

    # Delete the previous prompt message
    if "msg_to_delete" in state:
        try:
            old_msg = await message.channel.fetch_message(state["msg_to_delete"])
            await old_msg.delete()
        except Exception:
            pass

    # Payment Options Embed
    embed = discord.Embed(
        title="💳 Select Payment Method",
        description=f"**Product:** {product['name']}\n"
                    f"**Region:** {region.get('region_name')}\n"
                    f"**Guild ID:** `{guild_id}`\n"
                    f"**Amount:** ₹{amount_paise//100}\n\n"
                    f"Kindly choose your preferred payment method below.",
        color=0x5865F2
    )
    
    # Auto-redirect logic for Payment Menu (10 seconds)
    async def timeout_redirect():
        await asyncio.sleep(MENU_TIMEOUT)
        try:
            # Delete the payment options message
            await msg.delete()
            # Send back main menu
            main_embed = discord.Embed(
                title="⏰ Timeout",
                description="Aapne koi option select nahi kiya. Wapas main menu par redirect kar diya gaya hai.",
                color=0xff0000
            )
            timeout_msg = await message.channel.send(embed=main_embed)
            await asyncio.sleep(3)
            await timeout_msg.delete()
            
            # Show main menu
            view = MainMenuView(u.id)
            menu_embed = discord.Embed(
                title=f"👋 Welcome, {u.display_name}!",
                description="🚀 **Click Shop Now to Start!**",
                color=0xff9900)
            await message.channel.send(embed=menu_embed, view=view)
        except:
            pass

    view = PaymentMethodView(u.id, product, region, guild_id, amount_paise)
    msg = await message.channel.send(embed=embed, view=view)
    
    # Start the auto-redirect timer
    asyncio.create_task(timeout_redirect())


async def _handle_feedback_input(message: discord.Message, text: str):
    fid = uuid.uuid4().hex
    u = message.author
    db.save_feedback(fid, u.id, "dc", text, None, None)
    try:
        admin = bot.get_user(ADMIN_ID) or await bot.fetch_user(ADMIN_ID)
        if admin:
            embed = discord.Embed(title="💬 New Feedback", description=text or "(empty)", color=0xffaa00)
            embed.add_field(name="From", value=f"{u.mention} (`{u.id}`)", inline=False)
            await admin.send(embed=embed)
        await message.reply("✅ Feedback bhej diya. Dhanyavaad!")
    except Exception:
        await message.reply("⚠️ Feedback save hua lekin admin tak nahi pahuncha.")


bot = DiscordBot()


# ============ MAIN MENU VIEW ============

class MainMenuView(discord.ui.View):
    def __init__(self, uid):
        super().__init__(timeout=300)
        self.uid = uid

    @discord.ui.button(label="🛒 Shop Now", style=discord.ButtonStyle.primary, row=0)
    async def shop(self, inter, btn):
        await _send_shop(inter)

    @discord.ui.button(label="👤 Profile", style=discord.ButtonStyle.secondary, row=1)
    async def profile(self, inter, btn):
        u = db.get_user(inter.user.id, "dc")
        if not u:
            db.upsert_user(inter.user.id, "dc", inter.user.name, inter.user.display_name)
            u = db.get_user(inter.user.id, "dc")
        orders = db.list_user_orders(inter.user.id, "dc", 100)
        bought = sum(1 for o in orders if o["payment_status"] == "paid" and o["order_status"] == "running")
        embed = discord.Embed(title="👤 Profile", color=0x00bfff)
        embed.add_field(name="Name", value=u["display_name"] or "?", inline=True)
        embed.add_field(name="ID", value=f"`{u['user_id']}`", inline=True)
        embed.add_field(name="Balance", value=f"₹{u['balance_paise']//100}", inline=True)
        embed.add_field(name="Products bought", value=str(bought), inline=True)
        embed.add_field(name="Joined", value=u["joined_at"][:10], inline=True)
        await inter.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🔑 My Keys", style=discord.ButtonStyle.secondary, row=1)
    async def mykeys(self, inter, btn):
        orders = db.list_user_orders(inter.user.id, "dc", 50)
        keys = [o for o in orders if o["delivered_key"]]
        if not keys:
            await inter.response.send_message("Koi key nahi.", ephemeral=True)
            return
        txt = "\n".join(f"• {o['product_name']} — `{o['delivered_key']}`\n  Order: `{o['order_id']}`" for o in keys)
        await inter.response.send_message(f"🔑 **My Keys**\n{txt}"[:2000], ephemeral=True)

    @discord.ui.button(label="💰 Add Balance", style=discord.ButtonStyle.success, row=2)
    async def addbal(self, inter, btn):
        PENDING[inter.user.id] = {"step": "awaiting_amount"}
        await inter.response.send_message("💰 **Add Balance**\n\n**DM me jaake amount type karke bhejo.**", ephemeral=True)
        try:
            await inter.user.send("💰 **Add Balance**\n\nKitna amount add karna hai? Sirf number bhejo (₹).")
        except Exception:
            pass

    @discord.ui.button(label="💬 Feedback", style=discord.ButtonStyle.secondary, row=2)
    async def feedback(self, inter, btn):
        PENDING[inter.user.id] = {"step": "awaiting_feedback"}
        await inter.response.send_message("💬 **Feedback**\n\n**DM me jaake apna feedback type karke bhejo.**", ephemeral=True)
        try:
            await inter.user.send("💬 **Feedback**\n\nApna feedback type karke bhejo. Admin tak pahunchega.")
        except Exception:
            pass

    @discord.ui.button(label="🆘 Admin Support", style=discord.ButtonStyle.danger, row=3)
    async def support(self, inter, btn):
        await inter.response.send_message(f"🆘 **Admin Support**\n\nAdmin: <@{ADMIN_ID}>\n\nApni problem likho, admin ko message chala jayega.", ephemeral=True)


# ============ SHOP UI (ALL PRODUCTS FIX) ============

async def _send_shop(inter: discord.Interaction, page: int = 0):
    """Discord shop shows only products published from the Discord Seller Admin Panel."""
    if not inter.response.is_done():
        await inter.response.defer(ephemeral=True)
    try:
        guild_id = inter.guild.id if inter.guild else None
        catalog = [dict(p) for p in ds.get_published_products(guild_id)]
        if not catalog:
            await inter.edit_original_response(content="Koi Seller Admin product published nahi hai.", embed=None, view=None)
            return

        per_page = 20
        total_pages = (len(catalog) + per_page - 1) // per_page
        page = max(0, min(int(page), total_pages - 1))
        page_products = catalog[page * per_page:(page + 1) * per_page]
        options = []
        for product in page_products:
            price = int(product.get("price_paise") or 0) / 100
            options.append(discord.SelectOption(
                label=f"{product['name']} — ₹{price:.2f}"[:100],
                value=f"seller_{product['product_id']}"[:100],
                description="Discord Seller · Validity plans"[:100]
            ))

        view = discord.ui.View(timeout=300)
        select = discord.ui.Select(placeholder="Choose a seller product to buy...", options=options)
        async def select_callback(interaction: discord.Interaction):
            if interaction.user.id != inter.user.id:
                await interaction.response.send_message("Ye menu aapka nahi hai.", ephemeral=True)
                return
            selected = interaction.data["values"][0]
            if not selected.startswith("seller_"):
                await interaction.response.send_message("Invalid product selection. Shop dobara kholein.", ephemeral=True)
                return
            pid = selected[len("seller_"):]
            product = ds.get_published_product(pid, guild_id)
            if not product:
                await interaction.response.send_message("Seller product ab available/published nahi hai.", ephemeral=True)
                return
            await _handle_keyauth_product_selected(interaction, product, seller_product=True)

        select.callback = select_callback
        view.add_item(select)
        if page > 0:
            prev_btn = discord.ui.Button(label="⬅️ Prev", style=discord.ButtonStyle.primary)
            async def prev_cb(inter2):
                await _send_shop(inter2, page - 1)
            prev_btn.callback = prev_cb
            view.add_item(prev_btn)
        if page < total_pages - 1:
            next_btn = discord.ui.Button(label="Next ➡️", style=discord.ButtonStyle.primary)
            async def next_cb(inter2):
                await _send_shop(inter2, page + 1)
            next_btn.callback = next_cb
            view.add_item(next_btn)

        embed = discord.Embed(
            title="🛒 Discord Seller Shop",
            description=f"Seller Admin Panel se publish kiye gaye products\n\n**Product choose karein (Page {page+1}/{total_pages}):**",
            color=0x00ff00)
        await inter.edit_original_response(content=None, embed=embed, view=view)
    except Exception:
        log.exception("Discord shop load failed")
        await inter.edit_original_response(content="Shop load nahi ho paya. Dobara try karein.", embed=None, view=None)


async def _handle_keyauth_product_selected(inter: discord.Interaction, product, seller_product: bool = False):
    """Show duration options for a Discord Seller Admin product, then payment methods.

    This function was missing in the previous build, causing NameError on every
    seller-product selection. Product management remains Seller Admin-only.
    """
    product = dict(product)
    pid = str(product.get("product_id") or "")
    if not pid:
        await inter.response.send_message("Product ID missing hai. Seller Admin Panel check karein.", ephemeral=True)
        return
    try:
        options = ds.get_duration_options(pid) if seller_product else db.list_product_duration_options(pid)
        options = [dict(x) for x in options if int(x.get("duration_days") or 0) > 0 and int(x.get("price_paise") or 0) >= 100]
    except Exception:
        log.exception("Duration options load failed for product %s", pid)
        options = []
    if not options:
        # Older seller products without duration rows get a safe 1-day option at base price.
        price = int(product.get("price_paise") or 0)
        if price < 100:
            await inter.response.send_message("Is product ka price/duration set nahi hai. Seller Admin Panel mein duration option add karein.", ephemeral=True)
            return
        options = [{"duration_days": 1, "label": "1 Day", "price_paise": price}]
    view = discord.ui.View(timeout=300)
    select = discord.ui.Select(placeholder="Validity choose karein...", min_values=1, max_values=1, options=[
        discord.SelectOption(
            label=str(o.get("label") or f"{o.get('duration_days')} Days")[:65],
            description=f"₹{int(o.get('price_paise') or 0)/100:.2f}"[:100],
            value=str(i)
        ) for i, o in enumerate(options[:25])
    ])
    async def duration_callback(interaction: discord.Interaction):
        if interaction.user.id != inter.user.id:
            await interaction.response.send_message("Ye menu aapka nahi hai.", ephemeral=True)
            return
        try:
            selected = options[int(interaction.data["values"][0])]
            days = int(selected.get("duration_days") or 0)
            amount = int(selected.get("price_paise") or 0)
            label = str(selected.get("label") or f"{days} Days")
            if days < 1 or amount < 100:
                raise ValueError("Invalid duration/price")
            await interaction.response.send_message(
                f"**{product.get('name','Product')}**\nValidity: **{label}** · Price: **₹{amount/100:.2f}**\nPayment method choose karein:",
                view=PaymentMethodView(interaction.user.id, product, None, str(interaction.guild.id) if interaction.guild else "", amount, days, label, seller_product=True),
                ephemeral=True
            )
        except Exception as exc:
            log.exception("Discord duration selection failed for product %s", pid)
            if not interaction.response.is_done():
                await interaction.response.send_message("Duration select nahi ho paya. Seller Admin Panel check karein.", ephemeral=True)
    select.callback = duration_callback
    view.add_item(select)
    if not inter.response.is_done():
        await inter.response.send_message(f"**{product.get('name','Product')}** — validity choose karein:", view=view, ephemeral=True)
    else:
        await inter.followup.send(f"**{product.get('name','Product')}** — validity choose karein:", view=view, ephemeral=True)

# ============ PAYMENT METHOD VIEW (UPDATED FOR KEYAUTH) ============

class PaymentMethodView(discord.ui.View):
    def __init__(self, uid, product, region, guild_id, amount_paise, duration_days=0, duration_label="", seller_product=False):
        super().__init__(timeout=None)
        self.uid = uid
        self.product = dict(product)
        self.region = region
        self.guild_id = guild_id
        self.amount_paise = int(amount_paise)
        self.duration_days = int(duration_days or 0)
        self.duration_label = str(duration_label or "")
        self.seller_product = bool(seller_product or product.get("seller_key"))

    @discord.ui.button(label="💳 Pay with Wallet", style=discord.ButtonStyle.success, row=0)
    async def pay_wallet(self, inter: discord.Interaction, btn: discord.ui.Button):
        if inter.user.id != self.uid:
            await inter.response.send_message("This isn't your menu.", ephemeral=True)
            return
            
        user_data = db.get_user(inter.user.id, "dc")
        current_balance = user_data["balance_paise"] if user_data else 0
        
        if current_balance < self.amount_paise:
            err_msg = await inter.response.send_message(f"❌ **Insufficient Balance!**\n\nYour Balance: ₹{current_balance//100}\nRequired: ₹{self.amount_paise//100}\n\nPlease add balance from the Main Menu.", ephemeral=True)
            async def delete_err():
                await asyncio.sleep(FAST_DELETE)
                try: await inter.delete_original_response()
                except: pass
            asyncio.create_task(delete_err())
            return

        # Deduct Balance & Create Order
        db.deduct_balance(inter.user.id, "dc", self.amount_paise)
        order_id = "FG" + uuid.uuid4().hex[:12].upper()
        
        region_id = str(self.region.get("id")) if self.region else ""
        region_name = self.region.get("region_name", "") if self.region else ""
        tier = str(self.region.get("tier", "")) if self.region else ""

        db.create_order(order_id, inter.user.id, "dc", inter.user.name, self.product["product_id"], self.product["name"],
                        region_id, region_name, tier, self.guild_id, self.amount_paise,
                        duration_days=self.duration_days, duration_label=self.duration_label)
        
        db.update_order(order_id, payment_status="paid", order_status="paid_pending_launch")
        
        embed = discord.Embed(
            title="✅ Payment Successful!",
            description=f"Order ID: `{order_id}`\nAmount Deducted: **₹{self.amount_paise//100}**\n\n⏳ Processing your order...",
            color=0x2ecc71
        )
        await inter.response.edit_message(embed=embed, view=None)
        
        try:
            product_type = str(self.product.get("product_type") or ("guild_glory" if self.product.get("is_default") else "keyauth")).lower()
            if product_type == "guild_glory":
                if db.get_setting("guild_glory_enabled", "1") != "1":
                    raise RuntimeError("Guild Glory ke naye orders paused hain.")
                if not self.region or not self.guild_id:
                    raise RuntimeError("Guild Glory order ke liye Region aur Guild ID required hai.")
                res = await asyncio.to_thread(ff.launch_group, self.region["id"], self.guild_id)
                if res.get("success") and res.get("group_id"):
                    key = res["group_id"]
                    db.update_order(order_id, order_status="running", group_id=key, delivered_key=key, error="")
                    msg = await inter.channel.send(embed=discord.Embed(title="🎉 Order Complete!", description=f"Order ID: `{order_id}`\nGroup ID: `{key}`", color=0x2ecc71))
                    asyncio.create_task(_auto_delete_msg(msg, 5))
                else:
                    raise RuntimeError(str(res)[:500])
            elif self.seller_product:
                row = dict(db.get_order(order_id))
                await _deliver_seller_product(row, self.product, inter.user, inter.channel)
                msg = await inter.channel.send(embed=discord.Embed(title="🎉 Order Complete!", description=f"Credentials DM mein bhej diye gaye. Order ID: `{order_id}`", color=0x2ecc71))
                asyncio.create_task(_auto_delete_msg(msg, 5))
            elif product_type == "keyauth":
                row = dict(db.get_order(order_id))
                await _deliver_main_keyauth_order(row, self.product, inter.user, inter.channel)
                msg = await inter.channel.send(embed=discord.Embed(title="🎉 KeyAuth Account Delivered!", description=f"Credentials DM mein bhej diye gaye. Order ID: `{order_id}`", color=0x2ecc71))
                asyncio.create_task(_auto_delete_msg(msg, 5))
            else:
                raise RuntimeError(f"Unknown product provider: {product_type}")
        except Exception as e:
            ptype = str(self.product.get("product_type") or ("guild_glory" if self.product.get("is_default") else "keyauth")).lower()
            review_status = "launch_review" if ptype == "guild_glory" else "key_delivery_review"
            db.update_order(order_id, order_status=review_status, error=str(e)[:500])
            log.exception("Discord wallet order delivery failed: %s", order_id)
            msg = await inter.channel.send(f"⚠️ Payment ho gayi, lekin delivery pending hai. Admin Order ID `{order_id}` check karega. Dobara payment na karein.")
            asyncio.create_task(_auto_delete_msg(msg, 5))

    @discord.ui.button(label="📱 Pay with UPI QR", style=discord.ButtonStyle.primary, row=0)
    async def pay_upi(self, inter: discord.Interaction, btn: discord.ui.Button):
        if inter.user.id != self.uid:
            await inter.response.send_message("This isn't your menu.", ephemeral=True)
            return
            
        await inter.response.defer()
        
        order_id = "FG" + uuid.uuid4().hex[:12].upper()
        
        region_id = str(self.region.get("id")) if self.region else ""
        region_name = self.region.get("region_name", "") if self.region else ""
        tier = str(self.region.get("tier", "")) if self.region else ""

        db.create_order(order_id, inter.user.id, "dc", inter.user.name, self.product["product_id"], self.product["name"],
                        region_id, region_name, tier, self.guild_id, self.amount_paise,
                        duration_days=self.duration_days, duration_label=self.duration_label)

        loading = await inter.followup.send("⏳ **QR generate ho raha hai...** `0%`")

        try:
            qr = await asyncio.to_thread(rzp.create_qr, order_id, self.amount_paise, inter.user.display_name or inter.user.name)
            db.update_order(order_id, qr_code_id=qr["id"], qr_image_url=qr["image_url"], order_status="awaiting_payment", payment_status="created")
            try:
                await loading.delete()
            except Exception:
                pass

            img_bytes = requests.get(qr["image_url"], timeout=30).content
            f = discord.File(BytesIO(img_bytes), filename="qr.png")

            embed = discord.Embed(
                title="💳 Complete Your Payment",
                description=f"**Order ID:** `{order_id}`\n"
                            f"**Product:** {self.product['name']}\n"
                            f"**Amount:** ₹{self.amount_paise//100}\n\n"
                            f"⏱ **QR Expires in:** `05:00`\n"
                            f"👇 Scan the QR code below to pay.",
                color=0xf1c40f
            )
            embed.set_image(url="attachment://qr.png")
            
            qr_msg = await inter.channel.send(embed=embed, file=f)
            db.update_order(order_id, qr_message_id=qr_msg.id, qr_chat_id=qr_msg.channel.id)

            asyncio.create_task(_qr_timer(qr_msg.channel, qr_msg.id, order_id, "payment qr"))
            
            try:
                await inter.message.delete()
            except:
                pass

        except Exception as e:
            log.exception("dc order fail")
            try:
                await inter.channel.send(content=f"❌ Fail: {str(e)[:200]}")
            except Exception:
                pass


# ============ INTERACTION ROUTER ============

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.guild and not ds.get_server(interaction.guild.id):
        if interaction.type == discord.InteractionType.component:
            try: await interaction.response.send_message("⛔ Is server ke liye bot abhi activate nahi hai.", ephemeral=True)
            except Exception: pass
        return
    if interaction.type != discord.InteractionType.component:
        return
    cid = interaction.data.get("custom_id", "")

    if cid == "back_shop":
        await _send_shop(interaction)
        return


# ============ FACTORY ============

def run_discord_bot():
    ds.init_db()
    if not DISCORD_BOT_TOKEN:
        log.warning("Discord token missing — Discord bot skip")
        return None
    return bot