import json
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Any

from config import DB_PATH

log = logging.getLogger("db")

# Backup sender — telegram_bot.py ise set karega
_backup_sender = None  # async fn(dict) -> None


def set_backup_sender(fn):
    global _backup_sender
    _backup_sender = fn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    # WAL mode — concurrent read/write safe (poll + user actions)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return con


def init_db() -> None:
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            username TEXT,
            display_name TEXT,
            balance_paise INTEGER NOT NULL DEFAULT 0,
            joined_at TEXT NOT NULL,
            PRIMARY KEY (user_id, platform)
        );

        CREATE TABLE IF NOT EXISTS products (
            product_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            price_paise INTEGER NOT NULL,
            region TEXT,
            tier TEXT,
            is_default INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            keyauth_api_key TEXT,
            keyauth_subscription TEXT NOT NULL DEFAULT 'default',
            product_type TEXT NOT NULL DEFAULT 'guild_glory',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS product_duration_options (
            option_id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            label TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            price_paise INTEGER NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(product_id, label),
            FOREIGN KEY(product_id) REFERENCES products(product_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            username TEXT,
            product_id TEXT,
            product_name TEXT,
            region TEXT,
            region_name TEXT,
            tier TEXT,
            clan_id TEXT,
            amount_paise INTEGER NOT NULL,
            qr_code_id TEXT,
            qr_image_url TEXT,
            qr_message_id INTEGER,
            qr_chat_id INTEGER,
            payment_status TEXT NOT NULL DEFAULT 'created',
            order_status TEXT NOT NULL DEFAULT 'awaiting_payment',
            group_id TEXT,
            delivered_key TEXT,
            duration_days INTEGER NOT NULL DEFAULT 0,
            license_expires_at TEXT,
            keyauth_username TEXT,
            keyauth_password TEXT,
            keyauth_subscription TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payments (
            payment_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            amount_paise INTEGER NOT NULL,
            method TEXT,
            razorpay_payment_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS feedback (
            feedback_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            text TEXT,
            file_id TEXT,
            file_type TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- ============ NAYE TABLES (Sellers, Projects, Resellers) ============
        
        CREATE TABLE IF NOT EXISTS sellers (
            seller_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            display_name TEXT,
            channel_id INTEGER,
            balance_paise INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            PRIMARY KEY (seller_id, platform)
        );

        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            seller_id INTEGER NOT NULL,
            seller_platform TEXT NOT NULL,
            name TEXT NOT NULL,
            keyauth_app_name TEXT,
            keyauth_api_key TEXT,
            price_paise INTEGER NOT NULL,
            is_public INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resellers (
            reseller_id INTEGER NOT NULL,
            reseller_platform TEXT NOT NULL,
            seller_id INTEGER NOT NULL,
            seller_platform TEXT NOT NULL,
            project_id TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (reseller_id, reseller_platform, project_id)
        );

        -- NEW: Withdrawals Table
        CREATE TABLE IF NOT EXISTS withdrawals (
            withdraw_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            amount_paise INTEGER NOT NULL,
            method TEXT,
            details TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS api_credit_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            credit_type TEXT NOT NULL,
            action TEXT NOT NULL,
            credits_added REAL NOT NULL DEFAULT 0,
            credits_used REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            balance_before REAL,
            balance_after REAL NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS api_credit_state (
            credit_type TEXT PRIMARY KEY,
            current_balance REAL NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS manual_balance_transactions (
            transaction_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            amount_paise INTEGER NOT NULL CHECK(amount_paise > 0),
            order_id TEXT NOT NULL,
            utr TEXT,
            razorpay_payment_id TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        );

        -- NEW: Rules Table
        CREATE TABLE IF NOT EXISTS rules (
            rule_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_manual_credit_payment_unique
            ON manual_balance_transactions(razorpay_payment_id)
            WHERE razorpay_payment_id IS NOT NULL AND razorpay_payment_id <> '';
        CREATE UNIQUE INDEX IF NOT EXISTS idx_manual_credit_utr_unique
            ON manual_balance_transactions(utr)
            WHERE utr IS NOT NULL AND utr <> '';

        CREATE INDEX IF NOT EXISTS idx_api_credit_history_created
            ON api_credit_history(created_at DESC);

        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, platform, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(order_status);
        CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id);
        CREATE INDEX IF NOT EXISTS idx_projects_seller ON projects(seller_id, seller_platform);
        CREATE INDEX IF NOT EXISTS idx_resellers_seller ON resellers(seller_id, seller_platform);
        CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status);
        """)

        # ============ CRITICAL: UNIQUE index on razorpay_payment_id ============
        try:
            con.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_rzp_unique
                ON payments(razorpay_payment_id)
                WHERE razorpay_payment_id IS NOT NULL
            """)
        except sqlite3.OperationalError:
            log.warning("Unique index create failed — cleaning duplicates...")
            con.execute("""
                DELETE FROM payments
                WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM payments
                    WHERE razorpay_payment_id IS NOT NULL
                    GROUP BY razorpay_payment_id
                )
                AND razorpay_payment_id IS NOT NULL
            """)
            con.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_rzp_unique
                ON payments(razorpay_payment_id)
                WHERE razorpay_payment_id IS NOT NULL
            """)
            log.info("Duplicates cleaned, unique index created")

        # Safe additive migrations for existing databases.
        product_columns = {r[1] for r in con.execute("PRAGMA table_info(products)").fetchall()}
        if "product_type" not in product_columns:
            con.execute("ALTER TABLE products ADD COLUMN product_type TEXT NOT NULL DEFAULT 'guild_glory'")
        con.execute("UPDATE products SET product_type='keyauth' WHERE is_default=0 AND COALESCE(keyauth_api_key,'')<>'' AND product_type='guild_glory'")
        product_columns = {r[1] for r in con.execute("PRAGMA table_info(products)").fetchall()}
        if "keyauth_subscription" not in product_columns:
            con.execute("ALTER TABLE products ADD COLUMN keyauth_subscription TEXT NOT NULL DEFAULT 'default'")
        if "product_link" not in product_columns:
            con.execute("ALTER TABLE products ADD COLUMN product_link TEXT NOT NULL DEFAULT ''")
        order_columns = {r[1] for r in con.execute("PRAGMA table_info(orders)").fetchall()}
        for column, declaration in (
            ("duration_days", "INTEGER NOT NULL DEFAULT 0"),
            ("duration_label", "TEXT NOT NULL DEFAULT ''"),
            ("license_expires_at", "TEXT"),
            ("keyauth_username", "TEXT"),
            ("keyauth_password", "TEXT"),
            ("keyauth_subscription", "TEXT"),
        ):
            if column not in order_columns:
                con.execute(f"ALTER TABLE orders ADD COLUMN {column} {declaration}")
        con.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('guild_glory_enabled','1')")

        # default product
        cur = con.execute("SELECT 1 FROM products WHERE is_default=1")
        if not cur.fetchone():
            import uuid
            con.execute("""INSERT INTO products
                (product_id,name,description,price_paise,region,tier,is_default,enabled,keyauth_api_key,product_type,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, "FF Guild Glory", "Default Guild Glory product", 12000,
                 "ind", "basic", 1, 1, "", "guild_glory", now_iso()))

        # Seed default duration choices for existing KeyAuth products only.
        import uuid as _uuid
        _defaults = [("1 Day", 1, 1), ("3 Days", 3, 3), ("1 Week", 7, 7), ("15 Days", 15, 15), ("1 Month", 30, 30), ("Lifetime", 36500, 365)]
        _rows = con.execute("SELECT product_id, price_paise FROM products WHERE product_type='keyauth' AND is_default=0").fetchall()
        for _p in _rows:
            _count = con.execute("SELECT COUNT(*) FROM product_duration_options WHERE product_id=?", (_p["product_id"],)).fetchone()[0]
            if not _count:
                for _i, (_label, _days, _multiplier) in enumerate(_defaults):
                    con.execute("INSERT OR IGNORE INTO product_duration_options(option_id,product_id,label,duration_days,price_paise,sort_order,created_at) VALUES(?,?,?,?,?,?,?)",
                        (_uuid.uuid4().hex, _p["product_id"], _label, _days, int(_p["price_paise"]) * _multiplier, _i, now_iso()))


# ---------- USERS ----------

def upsert_user(user_id: int, platform: str, username: str = "", display_name: str = ""):
    with db() as con:
        con.execute("""INSERT INTO users (user_id,platform,username,display_name,balance_paise,joined_at)
            VALUES (?,?,?,?,0,?)
            ON CONFLICT(user_id,platform) DO UPDATE SET
              username=excluded.username,
              display_name=excluded.display_name""",
            (user_id, platform, username, display_name, now_iso()))


def get_user(user_id: int, platform: str):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=? AND platform=?",
                           (user_id, platform)).fetchone()


def add_balance(user_id: int, platform: str, paise: int):
    with db() as con:
        cur = con.execute(
            "UPDATE users SET balance_paise=balance_paise+? WHERE user_id=? AND platform=?",
            (paise, user_id, platform))
        if cur.rowcount == 0:
            con.execute("""INSERT INTO users
                (user_id,platform,username,display_name,balance_paise,joined_at)
                VALUES (?,?,?,?,?,?)""",
                (user_id, platform, "", "", paise, now_iso()))
    _fire_backup("balance_update", {"user_id": user_id, "platform": platform, "delta": paise})


def add_balance_with_ledger(user_id: int, platform: str, paise: int, order_id: str,
                            utr: str = "", razorpay_payment_id: str = "", note: str = ""):
    """Atomically credit a user and record an idempotency-protected admin transaction.

    Returns (success, message, transaction_dict). A payment ID or UTR can only be
    credited once across Telegram and Discord accounts.
    """
    import uuid
    user_id = int(user_id)
    platform = str(platform).lower().strip()
    paise = int(paise)
    order_id = str(order_id or "").strip()
    utr = str(utr or "").strip()
    razorpay_payment_id = str(razorpay_payment_id or "").strip()
    if platform not in ("tg", "dc"):
        return False, "Platform must be tg or dc", None
    if user_id <= 0 or paise <= 0 or not order_id:
        return False, "Valid user ID, amount and order ID are required", None
    if not razorpay_payment_id and not utr:
        return False, "Razorpay payment ID or UTR is required for duplicate protection", None

    txn_id = "MC-" + uuid.uuid4().hex[:12].upper()
    created_at = now_iso()
    try:
        with db() as con:
            con.execute("BEGIN IMMEDIATE")
            if razorpay_payment_id:
                existing = con.execute("SELECT transaction_id FROM manual_balance_transactions WHERE razorpay_payment_id=?", (razorpay_payment_id,)).fetchone()
                if existing:
                    con.rollback()
                    return False, f"This Razorpay payment was already credited ({existing['transaction_id']})", None
            if utr:
                existing = con.execute("SELECT transaction_id FROM manual_balance_transactions WHERE utr=?", (utr,)).fetchone()
                if existing:
                    con.rollback()
                    return False, f"This UTR was already credited ({existing['transaction_id']})", None

            con.execute("""INSERT INTO users (user_id,platform,username,display_name,balance_paise,joined_at)
                VALUES (?,?,?,?,0,?) ON CONFLICT(user_id,platform) DO NOTHING""",
                (user_id, platform, "", "", created_at))
            con.execute("UPDATE users SET balance_paise=balance_paise+? WHERE user_id=? AND platform=?",
                        (paise, user_id, platform))
            con.execute("""INSERT INTO manual_balance_transactions
                (transaction_id,user_id,platform,amount_paise,order_id,utr,razorpay_payment_id,note,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (txn_id, user_id, platform, paise, order_id, utr or None,
                 razorpay_payment_id or None, note, created_at))
            con.commit()
    except Exception as e:
        log.exception("Manual balance credit failed")
        return False, f"Could not credit balance: {str(e)[:180]}", None

    _fire_backup("manual_balance_credit", {
        "transaction_id": txn_id, "user_id": user_id, "platform": platform,
        "amount_paise": paise, "order_id": order_id, "utr": utr,
        "razorpay_payment_id": razorpay_payment_id, "created_at": created_at
    })
    return True, "Balance credited successfully", {
        "transaction_id": txn_id, "user_id": user_id, "platform": platform,
        "amount_paise": paise, "order_id": order_id, "utr": utr,
        "razorpay_payment_id": razorpay_payment_id, "created_at": created_at
    }


def list_manual_balance_transactions(limit=100):
    with db() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM manual_balance_transactions ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 500)),)).fetchall()]


def set_balance(user_id: int, platform: str, paise: int):
    with db() as con:
        con.execute("UPDATE users SET balance_paise=? WHERE user_id=? AND platform=?",
                    (paise, user_id, platform))
    _fire_backup("balance_set", {"user_id": user_id, "platform": platform, "value": paise})


def deduct_balance(user_id: int, platform: str, paise: int):
    """Web/Wallet payment ke liye balance deduct karta hai."""
    with db() as con:
        con.execute("UPDATE users SET balance_paise=balance_paise-? WHERE user_id=? AND platform=?",
                    (paise, user_id, platform))
    _fire_backup("balance_deduct", {"user_id": user_id, "platform": platform, "delta": -paise})


def deduct_balance_atomic(user_id: int, platform: str, paise: int) -> bool:
    """Deduct only if the wallet has enough funds; safe under concurrent orders."""
    user_id, paise = int(user_id), int(paise)
    if paise <= 0:
        return False
    with db() as con:
        cur = con.execute(
            "UPDATE users SET balance_paise=balance_paise-? WHERE user_id=? AND platform=? AND balance_paise>=?",
            (paise, user_id, platform, paise),
        )
        ok = cur.rowcount == 1
    if ok:
        _fire_backup("balance_deduct", {"user_id": user_id, "platform": platform, "delta": -paise})
    return ok


# ---------- PRODUCTS ----------

def list_products(enabled_only=True):
    with db() as con:
        q = "SELECT * FROM products"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY is_default DESC, created_at DESC"
        return con.execute(q).fetchall()


def get_product(product_id: str):
    with db() as con:
        return con.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()


def add_product(name: str, description: str, price_paise: int, region: str = "", tier: str = "", keyauth_api_key: str = "", product_type: str = "keyauth", keyauth_subscription: str = "default", product_link: str = ""):
    """Add a product with its own provider, Seller API key, and KeyAuth subscription."""
    import uuid
    name = str(name or "").strip()
    product_type = str(product_type or "keyauth").strip().lower()
    if product_type not in ("keyauth", "guild_glory"):
        raise ValueError("Product type keyauth ya guild_glory hona chahiye.")
    price_paise = int(price_paise)
    if not name or len(name) > 100:
        raise ValueError("Product name 1–100 characters ka hona chahiye.")
    if price_paise < 100:
        raise ValueError("Product ka 1-day/base price minimum ₹1 hona chahiye.")
    keyauth_subscription = str(keyauth_subscription or "default").strip()
    if product_type == "keyauth" and not str(keyauth_api_key or "").strip():
        raise ValueError("KeyAuth product ke liye Seller API key zaroori hai.")
    if product_type == "keyauth" and (not keyauth_subscription or len(keyauth_subscription) > 100):
        raise ValueError("KeyAuth subscription name 1–100 characters hona chahiye.")
    pid = uuid.uuid4().hex
    with db() as con:
        con.execute("""INSERT INTO products
            (product_id,name,description,price_paise,region,tier,is_default,enabled,keyauth_api_key,keyauth_subscription,product_type,product_link,created_at)
            VALUES (?,?,?,?,?,?,0,1,?,?,?,?,?)""",
            (pid, name, str(description or "")[:1000], price_paise, region, tier,
             str(keyauth_api_key or "").strip(), keyauth_subscription, product_type, str(product_link or "").strip()[:500], now_iso()))
    if product_type == "keyauth":
        _seed_product_duration_options(pid, price_paise)
    _fire_backup("product_added", {"product_id": pid, "name": name, "product_type": product_type})
    return pid


def _seed_product_duration_options(product_id: str, base_price_paise: int):
    import uuid
    defaults = [("1 Day", 1, 1), ("3 Days", 3, 3), ("1 Week", 7, 7), ("15 Days", 15, 15), ("1 Month", 30, 30), ("Lifetime", 36500, 365)]
    with db() as con:
        for i, (label, days, multiplier) in enumerate(defaults):
            con.execute("INSERT OR IGNORE INTO product_duration_options(option_id,product_id,label,duration_days,price_paise,sort_order,created_at) VALUES(?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, product_id, label, days, int(base_price_paise) * multiplier, i, now_iso()))


def list_product_duration_options(product_id: str):
    with db() as con:
        return con.execute("SELECT * FROM product_duration_options WHERE product_id=? ORDER BY sort_order, duration_days", (product_id,)).fetchall()


def get_product_duration_option(option_id: str):
    with db() as con:
        return con.execute("SELECT * FROM product_duration_options WHERE option_id=?", (option_id,)).fetchone()


def add_product_duration_option(product_id: str, label: str, duration_days: int, price_paise: int):
    import uuid
    label = str(label or '').strip()[:60]
    duration_days, price_paise = int(duration_days), int(price_paise)
    product = get_product(product_id)
    if not product or str(product['product_type']).lower() != 'keyauth' or product['is_default']:
        raise ValueError('Duration options sirf KeyAuth products ke liye hain; Guild Glory unchanged rahega.')
    if not label or duration_days < 1 or duration_days > 36500 or price_paise < 100:
        raise ValueError('Option name, valid duration (1–36500 days), aur price minimum ₹1 zaroori hai.')
    option_id = uuid.uuid4().hex
    with db() as con:
        sort_order = con.execute('SELECT COALESCE(MAX(sort_order),-1)+1 FROM product_duration_options WHERE product_id=?',(product_id,)).fetchone()[0]
        con.execute('INSERT INTO product_duration_options(option_id,product_id,label,duration_days,price_paise,sort_order,created_at) VALUES(?,?,?,?,?,?,?)',
                    (option_id, product_id, label, duration_days, price_paise, sort_order, now_iso()))
    _fire_backup('product_duration_option_added', {'product_id': product_id, 'label': label, 'duration_days': duration_days, 'price_paise': price_paise})
    return option_id


def update_product_duration_option(option_id: str, price_paise: int):
    price_paise = int(price_paise)
    if price_paise < 100:
        raise ValueError('Price minimum ₹1 hona chahiye.')
    with db() as con:
        cur = con.execute('UPDATE product_duration_options SET price_paise=? WHERE option_id=?', (price_paise, option_id))
        if cur.rowcount == 0:
            raise ValueError('Duration option nahi mila.')
    _fire_backup('product_duration_option_updated', {'option_id': option_id, 'price_paise': price_paise})


def delete_product_duration_option(option_id: str):
    with db() as con:
        cur = con.execute('DELETE FROM product_duration_options WHERE option_id=?', (option_id,))
        if cur.rowcount == 0:
            raise ValueError('Duration option nahi mila.')
    _fire_backup('product_duration_option_deleted', {'option_id': option_id})


def update_product_price(product_id: str, price_paise: int):
    with db() as con:
        con.execute("UPDATE products SET price_paise=? WHERE product_id=?",
                    (price_paise, product_id))
    _fire_backup("product_price_updated", {"product_id": product_id, "price_paise": price_paise})


def update_product_name(product_id: str, new_name: str):
    with db() as con:
        con.execute("UPDATE products SET name=? WHERE product_id=?",
                    (new_name, product_id))
    _fire_backup("product_name_updated", {"product_id": product_id, "name": new_name})


def update_product_link(product_id: str, product_link: str):
    link = str(product_link or "").strip()
    if link and not link.startswith(("https://", "http://")):
        raise ValueError("Product link http:// ya https:// se start hona chahiye.")
    with db() as con:
        con.execute("UPDATE products SET product_link=? WHERE product_id=?", (link[:500], product_id))
    _fire_backup("product_link_updated", {"product_id": product_id, "product_link": link[:500]})


def update_product_api_key(product_id: str, new_api_key: str):
    """Product ka KeyAuth API Key update karo."""
    with db() as con:
        con.execute("UPDATE products SET keyauth_api_key=? WHERE product_id=?",
                    (new_api_key, product_id))
    _fire_backup("product_api_key_updated", {"product_id": product_id})


def set_product_enabled(product_id: str, enabled: bool):
    with db() as con:
        con.execute("UPDATE products SET enabled=? WHERE product_id=?",
                    (1 if enabled else 0, product_id))


def delete_product(product_id: str):
    with db() as con:
        con.execute("DELETE FROM product_duration_options WHERE product_id=?", (product_id,))
        con.execute("DELETE FROM products WHERE product_id=? AND is_default=0", (product_id,))


# ---------- ORDERS ----------

def create_order(order_id, user_id, platform, username, product_id, product_name,
                 region, region_name, tier, clan_id, amount_paise, duration_days: int = 0, duration_label: str = ""):
    with db() as con:
        con.execute("""INSERT INTO orders
            (order_id,user_id,platform,username,product_id,product_name,region,region_name,tier,
             clan_id,amount_paise,duration_days,duration_label,payment_status,order_status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id, user_id, platform, username, product_id, product_name,
             region, region_name, tier, clan_id, amount_paise, int(duration_days or 0), str(duration_label or ""),
             "created", "creating_payment", now_iso(), now_iso()))
    _fire_backup("order_created", {"order_id": order_id, "user_id": user_id, "platform": platform})


def get_order(order_id: str):
    with db() as con:
        return con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()


def list_user_orders(user_id, platform, limit=10):
    with db() as con:
        return con.execute(
            "SELECT * FROM orders WHERE user_id=? AND platform=? ORDER BY created_at DESC LIMIT ?",
            (user_id, platform, limit)).fetchall()


def list_all_orders(limit=100):
    with db() as con:
        return con.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()


def list_pending_orders():
    with db() as con:
        return con.execute("""SELECT * FROM orders
            WHERE payment_status='created'
              AND order_status IN ('awaiting_payment','creating_payment')
              AND qr_code_id IS NOT NULL
            ORDER BY created_at ASC LIMIT 100""").fetchall()


def update_order(order_id: str, **fields):
    if not fields:
        return
    fields["updated_at"] = now_iso()
    keys = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [order_id]
    with db() as con:
        con.execute(f"UPDATE orders SET {keys} WHERE order_id=?", values)
    _fire_backup("order_updated", {"order_id": order_id,
                                    "fields": {k: str(v) for k, v in fields.items()}})


def update_keyauth_account_orders(user_id: int, platform: str, product_id: str, keyauth_username: str, expiry_iso: str, password: str = "1", error: str = ""):
    """Keep all purchases of the same KeyAuth account synchronized to its remote expiry."""
    with db() as con:
        con.execute(
            """UPDATE orders SET license_expires_at=?, keyauth_password=?, updated_at=?, error=?
               WHERE user_id=? AND platform=? AND product_id=? AND keyauth_username=? AND delivered_key IS NOT NULL AND keyauth_password IS NOT NULL""",
            (expiry_iso, password, now_iso(), error, int(user_id), str(platform), str(product_id), str(keyauth_username)))


def claim_order_payment(order_id: str, rzp_payment_id: str = None) -> bool:
    with db() as con:
        cur = con.execute(
            """UPDATE orders
               SET payment_status='paid',
                   order_status='paid_pending_launch',
                   updated_at=?
               WHERE order_id=?
                 AND payment_status='created'
                 AND order_status IN ('awaiting_payment','creating_payment')""",
            (now_iso(), order_id))
        return cur.rowcount > 0


# ---------- PAYMENTS ----------

def record_payment(payment_id, order_id, user_id, platform, amount_paise, method, rzp_id):
    with db() as con:
        try:
            cur = con.execute("""INSERT OR IGNORE INTO payments
                (payment_id,order_id,user_id,platform,amount_paise,method,razorpay_payment_id,created_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (payment_id, order_id, user_id, platform, amount_paise, method, rzp_id, now_iso()))
            inserted = cur.rowcount > 0
        except sqlite3.IntegrityError:
            inserted = False
    if inserted:
        _fire_backup("payment_recorded", {"payment_id": payment_id, "order_id": order_id})
    return inserted


def payment_exists(rzp_payment_id: str) -> bool:
    if not rzp_payment_id:
        return False
    with db() as con:
        r = con.execute("SELECT 1 FROM payments WHERE razorpay_payment_id=? LIMIT 1",
                        (rzp_payment_id,)).fetchone()
        return r is not None


def list_user_payments(user_id, platform, limit=20):
    with db() as con:
        return con.execute(
            "SELECT * FROM payments WHERE user_id=? AND platform=? ORDER BY created_at DESC LIMIT ?",
            (user_id, platform, limit)).fetchall()


# ---------- FEEDBACK ----------

def save_feedback(fid, user_id, platform, text, file_id, file_type):
    with db() as con:
        con.execute("""INSERT INTO feedback (feedback_id,user_id,platform,text,file_id,file_type,created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (fid, user_id, platform, text, file_id, file_type, now_iso()))
    _fire_backup("feedback", {"feedback_id": fid, "user_id": user_id, "platform": platform})


# ---------- SETTINGS ----------

def set_setting(key: str, value: str):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))


def get_setting(key: str, default=None):
    with db() as con:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


# ---------- RULES ----------

def add_rule(title: str, content: str):
    import uuid
    rule_id = uuid.uuid4().hex[:12]
    with db() as con:
        con.execute(
            "INSERT INTO rules (rule_id, title, content, created_at) VALUES (?, ?, ?, ?)",
            (rule_id, title, content, now_iso())
        )
    _fire_backup("rule_added", {"rule_id": rule_id, "title": title})
    return rule_id


def list_rules():
    with db() as con:
        return con.execute("SELECT * FROM rules ORDER BY created_at DESC").fetchall()


def delete_rule(rule_id: str):
    with db() as con:
        con.execute("DELETE FROM rules WHERE rule_id=?", (rule_id,))
    _fire_backup("rule_deleted", {"rule_id": rule_id})


# ---------- SELLERS / PROJECTS / RESELLERS ----------

def add_seller(seller_id: int, platform: str, display_name: str, channel_id: int):
    with db() as con:
        con.execute("""INSERT OR REPLACE INTO sellers 
                       (seller_id, platform, display_name, channel_id, balance_paise, is_active, created_at)
                       VALUES (?,?,?,?, COALESCE((SELECT balance_paise FROM sellers WHERE seller_id=? AND platform=?), 0), 1, ?)""",
                    (seller_id, platform, display_name, channel_id, seller_id, platform, now_iso()))
    _fire_backup("seller_added", {"seller_id": seller_id, "platform": platform, "channel_id": channel_id})


def get_seller(seller_id: int, platform: str):
    with db() as con:
        return con.execute("SELECT * FROM sellers WHERE seller_id=? AND platform=?", (seller_id, platform)).fetchone()


def list_sellers():
    with db() as con:
        return con.execute("SELECT * FROM sellers WHERE is_active=1").fetchall()


def add_project(seller_id: int, platform: str, name: str, keyauth_app_name: str, keyauth_api_key: str, price_paise: int):
    import uuid
    pid = uuid.uuid4().hex
    with db() as con:
        con.execute("""INSERT INTO projects 
                       (project_id, seller_id, seller_platform, name, keyauth_app_name, keyauth_api_key, price_paise, is_public, enabled, created_at)
                       VALUES (?,?,?,?,?,?,?,0,1,?)""",
                    (pid, seller_id, platform, name, keyauth_app_name, keyauth_api_key, price_paise, now_iso()))
    _fire_backup("project_added", {"project_id": pid, "seller_id": seller_id, "platform": platform, "name": name, "price_paise": price_paise})
    return pid


def list_seller_projects(seller_id: int, platform: str):
    with db() as con:
        return con.execute("SELECT * FROM projects WHERE seller_id=? AND seller_platform=?", (seller_id, platform)).fetchall()


def add_reseller(reseller_id: int, reseller_platform: str, seller_id: int, seller_platform: str, project_id: str, channel_id: int, expires_at: str):
    with db() as con:
        con.execute("""INSERT OR REPLACE INTO resellers 
                       (reseller_id, reseller_platform, seller_id, seller_platform, project_id, channel_id, expires_at, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (reseller_id, reseller_platform, seller_id, seller_platform, project_id, channel_id, expires_at, now_iso()))
    _fire_backup("reseller_added", {"reseller_id": reseller_id, "reseller_platform": reseller_platform, "project_id": project_id, "channel_id": channel_id, "expires_at": expires_at})


def get_reseller(reseller_id: int, reseller_platform: str, project_id: str):
    with db() as con:
        return con.execute("SELECT * FROM resellers WHERE reseller_id=? AND reseller_platform=? AND project_id=?", (reseller_id, reseller_platform, project_id)).fetchone()


# ---------- WITHDRAWALS (Web ke liye) ----------

def add_withdrawal(user_id: int, platform: str, amount_paise: int, method: str, details: str):
    import uuid
    wid = "WD" + uuid.uuid4().hex[:8].upper()
    with db() as con:
        con.execute("""INSERT INTO withdrawals (withdraw_id, user_id, platform, amount_paise, method, details, status, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (wid, user_id, platform, amount_paise, method, details, "pending", now_iso()))
    _fire_backup("withdrawal_requested", {"withdraw_id": wid, "user_id": user_id, "amount_paise": amount_paise})
    return wid


def get_pending_withdrawals():
    with db() as con:
        return con.execute("SELECT * FROM withdrawals WHERE status='pending' ORDER BY created_at ASC").fetchall()


def approve_withdrawal(wid: str):
    with db() as con:
        con.execute("UPDATE withdrawals SET status='approved' WHERE withdraw_id=?", (wid,))
    _fire_backup("withdrawal_approved", {"withdraw_id": wid})


# ---------- ADMIN DATA OPS ----------

def delete_user_data(user_id: int, platform: str):
    with db() as con:
        for t in ("orders", "payments", "feedback"):
            con.execute(f"DELETE FROM {t} WHERE user_id=? AND platform=?", (user_id, platform))
        con.execute("DELETE FROM users WHERE user_id=? AND platform=?", (user_id, platform))
        con.execute("DELETE FROM sellers WHERE seller_id=? AND platform=?", (user_id, platform))
    _fire_backup("user_data_deleted", {"user_id": user_id, "platform": platform})


def track_api_credit_snapshot(info: dict) -> None:
    """Track observed changes to FFGlory credits without inventing transactions.

    The first snapshot establishes a baseline. Later differences are recorded as
    added/used events; timestamps represent when the bot observed the change.
    """
    if not isinstance(info, dict):
        return
    now = now_iso()
    fields = (
        ("basic", "basic_credits"),
        ("premium", "premium_credits"),
    )
    with db() as con:
        for credit_type, field in fields:
            raw = info.get(field)
            if raw is None:
                continue
            try:
                current = float(raw)
            except (TypeError, ValueError):
                continue
            previous = con.execute(
                "SELECT current_balance FROM api_credit_state WHERE credit_type=?",
                (credit_type,),
            ).fetchone()
            if previous is None:
                con.execute(
                    "INSERT INTO api_credit_state (credit_type,current_balance,updated_at) VALUES (?,?,?)",
                    (credit_type, current, now),
                )
                continue
            before = float(previous["current_balance"])
            delta = current - before
            # Avoid noisy float comparisons for integer/decimal API credits.
            if abs(delta) > 1e-9:
                action = "added" if delta > 0 else "used"
                amount = abs(delta)
                con.execute(
                    """INSERT INTO api_credit_history
                       (credit_type,action,credits_added,credits_used,amount,balance_before,balance_after,note,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        credit_type, action,
                        amount if delta > 0 else 0,
                        amount if delta < 0 else 0,
                        amount, before, current,
                        "Observed balance change by periodic API snapshot; exact transaction time may differ.",
                        now,
                    ),
                )
                con.execute(
                    "UPDATE api_credit_state SET current_balance=?,updated_at=? WHERE credit_type=?",
                    (current, now, credit_type),
                )


def list_api_credit_history(limit: int = 100) -> list[dict]:
    with db() as con:
        rows = con.execute(
            "SELECT * FROM api_credit_history ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [dict(row) for row in rows]


def get_usage_history(days: int = 14) -> list[dict]:
    """Return actual daily order/user/revenue aggregates for the dashboard graph."""
    from datetime import timedelta
    days = max(1, min(int(days), 90))
    start_day = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
    with db() as con:
        orders = con.execute(
            """SELECT substr(created_at,1,10) AS day,
                      COUNT(*) AS usage,
                      SUM(CASE WHEN payment_status='paid' THEN amount_paise ELSE 0 END) AS revenue_paise,
                      SUM(CASE WHEN payment_status='paid' THEN 1 ELSE 0 END) AS paid_orders
               FROM orders WHERE substr(created_at,1,10)>=?
               GROUP BY substr(created_at,1,10) ORDER BY day ASC""",
            (start_day,),
        ).fetchall()
        users = con.execute(
            """SELECT substr(joined_at,1,10) AS day, COUNT(*) AS new_users
               FROM users WHERE substr(joined_at,1,10)>=?
               GROUP BY substr(joined_at,1,10)""",
            (start_day,),
        ).fetchall()
    user_counts = {row["day"]: int(row["new_users"] or 0) for row in users}
    result = []
    for row in orders:
        day = row["day"]
        result.append({
            "date": day,
            "timestamp": day + "T12:00:00+00:00",
            "event": "Daily orders",
            "type": "Daily orders",
            "usage": int(row["usage"] or 0),
            "count": int(row["usage"] or 0),
            "new_users": user_counts.get(day, 0),
            "revenue": round(int(row["revenue_paise"] or 0) / 100, 2),
            "paid_orders": int(row["paid_orders"] or 0),
            "details": f"{int(row['paid_orders'] or 0)} paid orders; {user_counts.get(day, 0)} new users",
        })
    # Include days with new users even when there were no orders.
    known_days = {item["date"] for item in result}
    for day, count in user_counts.items():
        if day not in known_days:
            result.append({
                "date": day, "timestamp": day + "T12:00:00+00:00",
                "event": "New users", "type": "New users",
                "usage": 0, "count": 0, "new_users": count,
                "revenue": 0, "paid_orders": 0,
                "details": f"{count} new users; no orders recorded",
            })
    return sorted(result, key=lambda item: item["date"])[-days:]


def get_stats() -> dict:
    with db() as con:
        stats = {}
        stats["tg_users"] = con.execute("SELECT COUNT(*) n FROM users WHERE platform='tg'").fetchone()["n"]
        stats["dc_users"] = con.execute("SELECT COUNT(*) n FROM users WHERE platform='dc'").fetchone()["n"]
        stats["tg_orders"] = con.execute("SELECT COUNT(*) n FROM orders WHERE platform='tg'").fetchone()["n"]
        stats["dc_orders"] = con.execute("SELECT COUNT(*) n FROM orders WHERE platform='dc'").fetchone()["n"]
        paid = con.execute("SELECT COUNT(*) n, COALESCE(SUM(amount_paise),0) s FROM orders WHERE payment_status='paid'").fetchone()
        stats["paid_orders"] = paid["n"]
        stats["total_revenue_paise"] = paid["s"]
        stats["pending_orders"] = con.execute(
            "SELECT COUNT(*) n FROM orders WHERE payment_status='created' AND order_status IN ('awaiting_payment','creating_payment')").fetchone()["n"]
        stats["failed_orders"] = con.execute(
            "SELECT COUNT(*) n FROM orders WHERE order_status='launch_review'").fetchone()["n"]
        stats["expired_orders"] = con.execute(
            "SELECT COUNT(*) n FROM orders WHERE order_status='expired'").fetchone()["n"]
        return stats


# ---------- BACKUP (Telegram channel) ----------

def _fire_backup(event: str, payload: dict):
    if _backup_sender is None:
        return
    try:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            loop.create_task(_backup_sender({"event": event, "ts": now_iso(), **payload}))
    except Exception:
        log.exception("backup dispatch failed")


def export_all_data() -> dict:
    with db() as con:
        out = {}
        tables = ("users", "products", "product_duration_options", "orders", "payments", "feedback", "settings", "sellers", "projects", "resellers", "withdrawals", "manual_balance_transactions", "api_credit_history", "api_credit_state", "rules")
        for t in tables:
            out[t] = [dict(r) for r in con.execute(f"SELECT * FROM {t}").fetchall()]
        return out


def restore_from_export(data: dict):
    with db() as con:
        tables = ("users", "products", "product_duration_options", "orders", "payments", "feedback", "settings", "sellers", "projects", "resellers", "withdrawals", "manual_balance_transactions", "api_credit_history", "api_credit_state", "rules")
        for t in tables:
            rows = data.get(t, [])
            if not rows:
                continue
            cols = list(rows[0].keys())
            placeholders = ",".join("?" * len(cols))
            for r in rows:
                try:
                    con.execute(
                        f"INSERT OR REPLACE INTO {t} ({','.join(cols)}) VALUES ({placeholders})",
                        [r.get(c) for c in cols])
                except Exception:
                    log.exception("restore row failed in %s", t)


def wipe_all_data():
    with db() as con:
        for t in ("orders", "payments", "feedback", "users", "sellers", "projects", "resellers", "withdrawals", "manual_balance_transactions", "api_credit_history", "api_credit_state", "rules"):
            con.execute(f"DELETE FROM {t}")
        con.execute("DELETE FROM product_duration_options")
        con.execute("DELETE FROM products WHERE is_default=0")
        con.execute("DELETE FROM settings")
    _fire_backup("wipe_all", {"by": "admin"})

def list_admin_keyauth_accounts(limit: int = 1000):
    """Orders for KeyAuth accounts sold through Telegram or created by Main Admin."""
    with db() as con:
        rows = con.execute(
            """SELECT o.*, COALESCE(p.name, o.product_name, 'Deleted product') AS display_product_name,
                      COALESCE(p.product_type, 'keyauth') AS display_product_type
               FROM orders o LEFT JOIN products p ON p.product_id=o.product_id
               WHERE o.platform IN ('tg','admin')
                 AND COALESCE(o.keyauth_username,'') <> ''
                 AND COALESCE(o.keyauth_password,'') <> ''
                 AND COALESCE(o.order_status,'') <> 'deleted'
               ORDER BY o.created_at DESC LIMIT ?""", (max(1, min(int(limit), 5000)),)
        ).fetchall()
        # One visible row per account/product/platform; latest expiry/credentials win.
        result, seen = [], set()
        for row in rows:
            d = dict(row)
            k = (str(d.get('platform')), str(d.get('product_id')), str(d.get('keyauth_username')))
            if k in seen:
                continue
            seen.add(k)
            result.append(d)
        return result
