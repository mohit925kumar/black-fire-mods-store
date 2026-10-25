import sqlite3
import uuid
import hashlib
import hmac
import os
from datetime import datetime, timezone, timedelta
from config import DB_PATH

def now():
    return datetime.now(timezone.utc).isoformat()

def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA busy_timeout=10000')
    c.execute('PRAGMA journal_mode=WAL')
    return c

def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS dc_servers(
            guild_id TEXT PRIMARY KEY,
            guild_name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dc_seller_logins(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT NOT NULL,
            guild_id TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(discord_user_id, guild_id)
        );
        CREATE TABLE IF NOT EXISTS dc_products(
            product_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            seller_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            subscription TEXT NOT NULL,
            seller_key TEXT NOT NULL,
            price_paise INTEGER NOT NULL DEFAULT 0,
            product_link TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            is_published INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dc_product_duration_options(
            option_id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            label TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            price_paise INTEGER NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(product_id, label)
        );
        CREATE TABLE IF NOT EXISTS dc_payout_accounts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER NOT NULL, guild_id TEXT NOT NULL,
            method TEXT NOT NULL, upi_id TEXT NOT NULL DEFAULT '', account_name TEXT NOT NULL DEFAULT '',
            bank_name TEXT NOT NULL DEFAULT '', account_number TEXT NOT NULL DEFAULT '', ifsc TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL, UNIQUE(seller_id,guild_id,method)
        );
        CREATE TABLE IF NOT EXISTS dc_withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id INTEGER NOT NULL, guild_id TEXT NOT NULL,
            amount_paise INTEGER NOT NULL, account_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL, paid_at TEXT NOT NULL DEFAULT '', admin_note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS dc_keys(
            key_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            seller_id INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            username TEXT NOT NULL,
            password_hint TEXT NOT NULL DEFAULT '',
            duration_label TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_dc_products_guild ON dc_products(guild_id, enabled);
        CREATE INDEX IF NOT EXISTS idx_dc_keys_seller ON dc_keys(seller_id, guild_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_dc_duration_product ON dc_product_duration_options(product_id);
        """)
        
        # === AUTO MIGRATION: Add is_published if missing ===
        try:
            c.execute("ALTER TABLE dc_products ADD COLUMN is_published INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Already exists

# ============ SERVER MANAGEMENT ============

def register_server(guild_id, guild_name):
    with conn() as c:
        c.execute(
            'INSERT INTO dc_servers (guild_id, guild_name, active, created_at) VALUES (?, ?, 0, ?) '
            'ON CONFLICT(guild_id) DO UPDATE SET guild_name=excluded.guild_name',
            (str(guild_id), str(guild_name)[:100], now())
        )

def list_servers():
    with conn() as c:
        return [dict(r) for r in c.execute('SELECT * FROM dc_servers ORDER BY guild_name')]

def set_server_active(guild_id, active):
    with conn() as c:
        c.execute('UPDATE dc_servers SET active=? WHERE guild_id=?', (1 if active else 0, str(guild_id)))
        if not active:
            c.execute('UPDATE dc_seller_logins SET active=0 WHERE guild_id=?', (str(guild_id),))
            c.execute('UPDATE dc_products SET enabled=0 WHERE guild_id=?', (str(guild_id),))

def activate_server(guild_id):
    with conn() as c:
        c.execute('UPDATE dc_servers SET active=1 WHERE guild_id=?', (str(guild_id),))
        c.execute('UPDATE dc_seller_logins SET active=1 WHERE guild_id=?', (str(guild_id),))

def get_server(guild_id):
    with conn() as c:
        r = c.execute('SELECT * FROM dc_servers WHERE guild_id=? AND active=1', (str(guild_id),)).fetchone()
        return dict(r) if r else None

# ============ SELLER MANAGEMENT ============

def list_sellers():
    with conn() as c:
        rows = c.execute("""
            SELECT l.id, l.discord_user_id, l.guild_id, l.username, l.active, l.created_at,
                   s.guild_name, s.active as server_active
            FROM dc_seller_logins l 
            LEFT JOIN dc_servers s ON s.guild_id = l.guild_id
            ORDER BY l.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]

def delete_seller(seller_id):
    with conn() as c:
        row = c.execute('SELECT id FROM dc_seller_logins WHERE id=?', (int(seller_id),)).fetchone()
        if not row:
            return False
        c.execute('DELETE FROM dc_keys WHERE seller_id=?', (int(seller_id),))
        c.execute('DELETE FROM dc_products WHERE seller_id=?', (int(seller_id),))
        c.execute('DELETE FROM dc_seller_logins WHERE id=?', (int(seller_id),))
        return True

def reset_seller_password(seller_id, new_password):
    new_password = str(new_password or '')
    if len(new_password) < 8:
        raise ValueError('Password minimum 8 characters ka hona chahiye.')
    with conn() as c:
        cur = c.execute('UPDATE dc_seller_logins SET password_hash=? WHERE id=?', (_hash(new_password), int(seller_id)))
        if cur.rowcount == 0:
            raise ValueError('Seller login nahi mila.')
        return True

def create_seller(discord_user_id, guild_id, username, password):
    uid, gid = str(discord_user_id), str(guild_id)
    username = str(username or '').strip().lower()
    password = str(password or '')
    
    if not uid.isdigit() or not gid.isdigit():
        raise ValueError('Discord User ID aur Server ID numeric hone chahiye.')
    if len(username) < 3 or len(username) > 40 or not all(x.isalnum() or x in '._-@' for x in username):
        raise ValueError('Username 3–40 characters ka ho; sirf letters, numbers, @, dot (.), underscore (_) aur hyphen (-) allowed hain.')
    if len(password) < 8:
        raise ValueError('Password minimum 8 characters ka hona chahiye.')
    if not get_server(gid):
        raise ValueError('Bot ne ye Discord server abhi register nahi kiya. Bot ko server mein add karke restart/sync karein.')
        
    with conn() as c:
        if c.execute('SELECT 1 FROM dc_seller_logins WHERE username=?', (username,)).fetchone():
            raise ValueError('Ye username pehle se kisi seller ko diya gaya hai. Unique username choose karein.')
        if c.execute('SELECT 1 FROM dc_seller_logins WHERE discord_user_id=? AND guild_id=?', (uid, gid)).fetchone():
            raise ValueError('Is Discord user aur server ka seller login pehle se hai.')
            
        try:
            c.execute(
                'INSERT INTO dc_seller_logins (discord_user_id, guild_id, username, password_hash, created_at) VALUES (?, ?, ?, ?, ?)',
                (uid, gid, username, _hash(password), now())
            )
        except sqlite3.IntegrityError:
            raise ValueError('Username duplicate hai ya seller login abhi-abhi create hua hai.')

def authenticate(username, password):
    with conn() as c:
        r = c.execute('SELECT * FROM dc_seller_logins WHERE username=? AND active=1', (str(username or '').strip().lower(),)).fetchone()
        if not r or not _check(str(password or ''), r['password_hash']):
            return None
        s = c.execute('SELECT * FROM dc_servers WHERE guild_id=? AND active=1', (r['guild_id'],)).fetchone()
        if not s:
            return None
        return {
            'id': r['id'],
            'discord_user_id': r['discord_user_id'],
            'guild_id': r['guild_id'],
            'username': r['username'],
            'guild_name': s['guild_name']
        }

def seller_by_discord(user_id, guild_id):
    with conn() as c:
        r = c.execute(
            'SELECT l.*, s.guild_name FROM dc_seller_logins l '
            'JOIN dc_servers s ON s.guild_id=l.guild_id '
            'WHERE l.discord_user_id=? AND l.guild_id=? AND l.active=1 AND s.active=1',
            (str(user_id), str(guild_id))
        ).fetchone()
        return dict(r) if r else None

# ============ PRODUCT MANAGEMENT ============

def add_product(guild_id, seller_id, name, subscription, seller_key, price_paise, product_link):
    name = str(name or '').strip()
    sub = str(subscription or '').strip()
    key = str(seller_key or '').strip()
    link = str(product_link or '').strip()
    
    if not name or len(name) > 80 or not sub or len(sub) > 80 or not key:
        raise ValueError('Product name, subscription aur Seller API key required hain.')
    if int(price_paise) < 0:
        raise ValueError('Price negative nahi ho sakti.')
    if link and not link.startswith(('https://', 'http://')):
        raise ValueError('Product link http:// ya https:// se start hona chahiye.')
        
    pid = uuid.uuid4().hex[:16]
    with conn() as c:
        c.execute(
            'INSERT INTO dc_products (product_id, guild_id, seller_id, name, subscription, seller_key, price_paise, product_link, enabled, is_published, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)',
            (pid, str(guild_id), int(seller_id), name, sub, key, int(price_paise), link, now())
        )
    return pid

def list_products(guild_id, seller_id=None, enabled_only=False):
    q = 'SELECT product_id, guild_id, seller_id, name, subscription, price_paise, product_link, enabled, is_published, created_at FROM dc_products WHERE guild_id=?'
    a = [str(guild_id)]
    if seller_id is not None:
        q += ' AND seller_id=?'
        a.append(int(seller_id))
    if enabled_only:
        q += ' AND enabled=1'
    q += ' ORDER BY created_at DESC'
    with conn() as c:
        return [dict(r) for r in c.execute(q, a)]

def get_published_products(guild_id=None):
    with conn() as c:
        if guild_id is None:
            rows = c.execute('SELECT * FROM dc_products WHERE enabled=1 AND is_published=1 ORDER BY created_at DESC').fetchall()
        else:
            rows = c.execute('SELECT * FROM dc_products WHERE enabled=1 AND is_published=1 AND guild_id=? ORDER BY created_at DESC', (str(guild_id),)).fetchall()
        return [dict(r) for r in rows]

def get_published_product(pid, guild_id=None):
    with conn() as c:
        if guild_id is None:
            row = c.execute('SELECT * FROM dc_products WHERE product_id=? AND enabled=1 AND is_published=1', (str(pid),)).fetchone()
        else:
            row = c.execute('SELECT * FROM dc_products WHERE product_id=? AND guild_id=? AND enabled=1 AND is_published=1', (str(pid), str(guild_id))).fetchone()
        return dict(row) if row else None

def get_product(pid, guild_id):
    with conn() as c:
        r = c.execute('SELECT * FROM dc_products WHERE product_id=? AND guild_id=? AND enabled=1', (str(pid), str(guild_id))).fetchone()
        return dict(r) if r else None

def get_product_owned(pid, seller_id, guild_id):
    with conn() as c:
        r = c.execute('SELECT * FROM dc_products WHERE product_id=? AND seller_id=? AND guild_id=?', (str(pid), int(seller_id), str(guild_id))).fetchone()
        return dict(r) if r else None

def update_product(pid, seller_id, guild_id, **v):
    allowed = {'name', 'subscription', 'seller_key', 'price_paise', 'product_link'}
    f, a = [], []
    for k, x in v.items():
        if k in allowed:
            f.append(k + '=?')
            a.append(x)
    if f:
        a += [str(pid), int(seller_id), str(guild_id)]
        with conn() as c:
            c.execute('UPDATE dc_products SET ' + ','.join(f) + ' WHERE product_id=? AND seller_id=? AND guild_id=?', a)

def delete_product(pid, seller_id, guild_id):
    with conn() as c:
        c.execute('DELETE FROM dc_products WHERE product_id=? AND seller_id=? AND guild_id=?', (str(pid), int(seller_id), str(guild_id)))
        c.execute('UPDATE dc_keys SET status="inactive" WHERE product_id=? AND seller_id=? AND guild_id=?', (str(pid), int(seller_id), str(guild_id)))

def publish_product(pid, seller_id, guild_id):
    with conn() as c:
        c.execute('UPDATE dc_products SET is_published=1 WHERE product_id=? AND seller_id=? AND guild_id=?', (str(pid), int(seller_id), str(guild_id)))

def unpublish_product(pid, seller_id, guild_id):
    with conn() as c:
        c.execute('UPDATE dc_products SET is_published=0 WHERE product_id=? AND seller_id=? AND guild_id=?', (str(pid), int(seller_id), str(guild_id)))

# ============ DURATION OPTIONS ============

def get_duration_options(pid):
    """Fetch all duration options for a product"""
    with conn() as c:
        rows = c.execute(
            'SELECT * FROM dc_product_duration_options WHERE product_id=? ORDER BY sort_order, duration_days',
            (str(pid),)
        ).fetchall()
        return [dict(r) for r in rows]

def add_duration_option(product_id, label, duration_days, price_paise):
    duration_days, price_paise = int(duration_days), int(price_paise)
    if duration_days < 1 or duration_days > 36500:
        raise ValueError('Duration 1 se 36500 days ke beech honi chahiye.')
    if price_paise < 100:
        raise ValueError('Price minimum ₹1 honi chahiye.')
    label = duration_label(duration_days)
    with conn() as c:
        # Get next sort_order
        max_order = c.execute(
            'SELECT COALESCE(MAX(sort_order), -1) FROM dc_product_duration_options WHERE product_id=?',
            (str(product_id),)
        ).fetchone()[0]
        
        option_id = uuid.uuid4().hex[:12]
        c.execute(
            'INSERT INTO dc_product_duration_options (option_id, product_id, label, duration_days, price_paise, sort_order, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (option_id, str(product_id), duration_label(duration_days), int(duration_days), int(price_paise), max_order + 1, now())
        )
        return option_id

def update_duration_price(option_id, price_paise):
    with conn() as c:
        c.execute('UPDATE dc_product_duration_options SET price_paise=? WHERE option_id=?', (int(price_paise), str(option_id)))

def delete_duration_option(option_id):
    with conn() as c:
        c.execute('DELETE FROM dc_product_duration_options WHERE option_id=?', (str(option_id),))

def duration_label(days):
    days = int(days)
    if days >= 36500:
        return "Lifetime"
    if days % 30 == 0:
        n = days // 30
        return f"{n} Month" + ("s" if n != 1 else "")
    if days % 7 == 0 and days < 30:
        n = days // 7
        return f"{n} Week" + ("s" if n != 1 else "")
    return f"{days} Day" + ("s" if days != 1 else "")

def seed_default_options(product_id):
    """Idempotently seed the five standard Discord seller durations only."""
    defaults = [("1 Day", 1), ("3 Days", 3), ("1 Week", 7), ("15 Days", 15), ("1 Month", 30)]
    with conn() as c:
        existing = {int(r[0]) for r in c.execute('SELECT duration_days FROM dc_product_duration_options WHERE product_id=?', (str(product_id),)).fetchall()}
        max_order = int(c.execute('SELECT COALESCE(MAX(sort_order),-1) FROM dc_product_duration_options WHERE product_id=?', (str(product_id),)).fetchone()[0])
        for label, days in defaults:
            if days in existing:
                continue
            max_order += 1
            c.execute('INSERT OR IGNORE INTO dc_product_duration_options(option_id,product_id,label,duration_days,price_paise,sort_order,created_at) VALUES(?,?,?,?,?,?,?)',
                      (uuid.uuid4().hex[:12], str(product_id), label, days, max(100, days * 100), max_order, now()))

# ============ KEY MANAGEMENT ============

def add_key(guild_id, seller_id, pid, username, password, label, days):
    kid = uuid.uuid4().hex[:16]
    expiry = (datetime.now(timezone.utc) + timedelta(days=int(days))).isoformat()
    with conn() as c:
        c.execute(
            'INSERT INTO dc_keys (key_id, guild_id, seller_id, product_id, username, password_hint, duration_label, duration_days, expires_at, status, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "active", ?, ?)',
            (kid, str(guild_id), int(seller_id), str(pid), str(username), str(password), str(label), int(days), expiry, now(), now())
        )
    return kid

def list_keys(guild_id, seller_id):
    with conn() as c:
        return [dict(r) for r in c.execute(
            'SELECT k.*, p.name as product_name FROM dc_keys k '
            'LEFT JOIN dc_products p ON p.product_id=k.product_id '
            'WHERE k.guild_id=? AND k.seller_id=? ORDER BY k.created_at DESC LIMIT 500',
            (str(guild_id), int(seller_id))
        )]

def get_key(kid, seller_id, guild_id):
    with conn() as c:
        r = c.execute(
            'SELECT k.*, p.seller_key, p.subscription, p.name as product_name FROM dc_keys k '
            'JOIN dc_products p ON p.product_id=k.product_id '
            'WHERE k.key_id=? AND k.seller_id=? AND k.guild_id=?',
            (str(kid), int(seller_id), str(guild_id))
        ).fetchone()
        return dict(r) if r else None

def update_key_password(kid, seller_id, guild_id, password):
    with conn() as c:
        c.execute('UPDATE dc_keys SET password_hint=?, updated_at=? WHERE key_id=? AND seller_id=? AND guild_id=?', (password, now(), str(kid), int(seller_id), str(guild_id)))

def mark_key(kid, seller_id, guild_id, status):
    with conn() as c:
        c.execute('UPDATE dc_keys SET status=?, updated_at=? WHERE key_id=? AND seller_id=? AND guild_id=?', (status, now(), str(kid), int(seller_id), str(guild_id)))

# ============ PASSWORD HASHING ============

def _hash(p):
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac('sha256', p.encode(), salt.encode(), 240000).hex()
    return f'{salt}${digest}'

def _check(p, stored):
    try:
        salt, digest = stored.split('$', 1)
        return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256', p.encode(), salt.encode(), 240000).hex(), digest)
    except Exception:
        return False

# ============ PAYOUT / WITHDRAWALS ============

def save_payout_account(seller_id, guild_id, method, data):
    method = str(method or '').lower()
    if method not in ('upi','bank'): raise ValueError('UPI ya Bank choose karein.')
    if method == 'upi' and not str(data.get('upi_id','')).strip(): raise ValueError('UPI ID required hai.')
    if method == 'bank' and not all(str(data.get(k,'')).strip() for k in ('account_name','bank_name','account_number','ifsc')): raise ValueError('Bank ke sabhi details required hain.')
    sql = "INSERT INTO dc_payout_accounts(seller_id,guild_id,method,upi_id,account_name,bank_name,account_number,ifsc,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(seller_id,guild_id,method) DO UPDATE SET upi_id=excluded.upi_id,account_name=excluded.account_name,bank_name=excluded.bank_name,account_number=excluded.account_number,ifsc=excluded.ifsc,updated_at=excluded.updated_at"
    with conn() as c:
        c.execute(sql, (int(seller_id),str(guild_id),method,str(data.get('upi_id','')).strip(),str(data.get('account_name','')).strip(),str(data.get('bank_name','')).strip(),str(data.get('account_number','')).strip(),str(data.get('ifsc','')).strip().upper(),now()))

def list_payout_accounts(seller_id,guild_id):
    with conn() as c: return [dict(r) for r in c.execute('SELECT * FROM dc_payout_accounts WHERE seller_id=? AND guild_id=? ORDER BY method',(int(seller_id),str(guild_id)))]

def request_withdrawal(seller_id,guild_id,amount_paise,account_id):
    if int(amount_paise)<=0: raise ValueError('Amount zero se zyada hona chahiye.')
    with conn() as c:
        acct=c.execute('SELECT id FROM dc_payout_accounts WHERE id=? AND seller_id=? AND guild_id=?',(int(account_id),int(seller_id),str(guild_id))).fetchone()
        if not acct: raise ValueError('Payout account nahi mila.')
        cur=c.execute('INSERT INTO dc_withdrawals(seller_id,guild_id,amount_paise,account_id,status,created_at) VALUES(?,?,?,?,?,?)',(int(seller_id),str(guild_id),int(amount_paise),int(account_id),'pending',now()))
        return cur.lastrowid

def list_withdrawals(seller_id=None,guild_id=None):
    q='SELECT w.*, a.method,a.upi_id,a.account_name,a.bank_name,a.account_number,a.ifsc FROM dc_withdrawals w LEFT JOIN dc_payout_accounts a ON a.id=w.account_id WHERE 1=1'; args=[]
    if seller_id is not None: q+=' AND w.seller_id=?'; args.append(int(seller_id))
    if guild_id is not None: q+=' AND w.guild_id=?'; args.append(str(guild_id))
    q+=' ORDER BY w.created_at DESC'
    with conn() as c: return [dict(r) for r in c.execute(q,args)]