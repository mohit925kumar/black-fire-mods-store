import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
# BACKUP_CHANNEL_ID hi file upload aur data backup dono ke liye use hoga.
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID", "0") or 0)

# Discord
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()

# Razorpay
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()

# FFGlory
FFGLORY_BASE = os.getenv("FFGLORY_BASE", "https://ffglory.pro").rstrip("/")
FFGLORY_API_KEY = os.getenv("FFGLORY_API_KEY", "").strip()
FFGLORY_MASTER_KEY = os.getenv("FFGLORY_MASTER_KEY", "").strip()

# Firebase (Web Login/Real-time data ke liye)
FIREBASE_DB_URL = "https://teligram-welcome-bot-default-rtdb.firebaseio.com"

# Misc
PRICE_INR = int(os.getenv("PRICE_INR", "120") or 120)
DB_PATH = os.getenv("DB_PATH", "orders.db")
POLL_SECONDS = max(10, int(os.getenv("POLL_SECONDS", "20") or 20))
REQUEST_TIMEOUT = 30
TELEGRAM_TIMEOUT = 75


def validate_config():
    missing = []
    checks = [
        ("BOT_TOKEN", BOT_TOKEN),
        ("ADMIN_ID", str(ADMIN_ID) if ADMIN_ID else ""),
        ("RAZORPAY_KEY_ID", RAZORPAY_KEY_ID),
        ("RAZORPAY_KEY_SECRET", RAZORPAY_KEY_SECRET),
        ("FFGLORY_API_KEY", FFGLORY_API_KEY),
    ]
    
    if not DISCORD_BOT_TOKEN:
        print("⚠️ Warning: DISCORD_BOT_TOKEN missing. Discord bot nahi chalega.")

    for name, value in checks:
        if not value:
            missing.append(name)
            
    if missing:
        raise SystemExit(".env me ye set karo: " + ", ".join(missing))