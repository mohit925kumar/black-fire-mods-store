"""Periodic encrypted-channel-independent Telegram document backup.
The archive contains a SQLite snapshot plus Firebase web/live data snapshots.
Keep BACKUP_CHANNEL_ID private and grant the bot permission to post documents.
"""
import json, logging, os, sqlite3, tempfile, time, zipfile
from datetime import datetime, timezone
from pathlib import Path
import requests
from config import BOT_TOKEN, BACKUP_CHANNEL_ID, DB_PATH, FIREBASE_DB_URL

log = logging.getLogger('backup')
INTERVAL_SECONDS = max(300, int(os.getenv('BACKUP_INTERVAL_SECONDS', '1800')))

def _db_snapshot(destination):
    src_path = Path(DB_PATH)
    if not src_path.exists():
        raise FileNotFoundError(f'Database file not found: {src_path.resolve()}')
    src = sqlite3.connect(str(src_path), timeout=30)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()

def _firebase_snapshot():
    result = {}
    for name in ('live_data', 'web_users'):
        try:
            r = requests.get(f'{FIREBASE_DB_URL}/{name}.json', timeout=20)
            r.raise_for_status()
            result[name] = r.json()
        except Exception as e:
            result[name] = {'_backup_error': str(e)[:200]}
    return result

def backup_to_telegram():
    if not BOT_TOKEN or not BACKUP_CHANNEL_ID:
        log.warning('Telegram backup skipped: BOT_TOKEN/BACKUP_CHANNEL_ID not configured.')
        return False
    with tempfile.TemporaryDirectory(prefix='blackfire-backup-') as tmp:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        db_file = os.path.join(tmp, 'orders.db')
        _db_snapshot(db_file)
        firebase_file = os.path.join(tmp, 'firebase_snapshot.json')
        with open(firebase_file, 'w', encoding='utf-8') as f:
            json.dump({'created_at_utc': stamp, **_firebase_snapshot()}, f, ensure_ascii=False, indent=2)
        archive = os.path.join(tmp, f'black_fire_store_backup_{stamp}.zip')
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            z.write(db_file, 'orders.db')
            z.write(firebase_file, 'firebase_snapshot.json')
        with open(archive, 'rb') as f:
            r = requests.post(
                f'https://api.telegram.org/bot{BOT_TOKEN}/sendDocument',
                data={'chat_id': str(BACKUP_CHANNEL_ID), 'caption': f'Black Fire Store backup | {stamp} | DB + Firebase snapshot'},
                files={'document': (os.path.basename(archive), f, 'application/zip')}, timeout=120)
        if not r.ok or not r.json().get('ok'):
            raise RuntimeError(f'Telegram backup upload failed: HTTP {r.status_code} {r.text[:250]}')
        log.info('Telegram backup uploaded: %s', os.path.basename(archive))
        return True

def backup_loop():
    # Delay the first upload slightly so all services can initialize.
    while True:
        time.sleep(60)
        try:
            backup_to_telegram()
        except Exception:
            log.exception('Telegram backup failed; will retry next interval')
        time.sleep(max(0, INTERVAL_SECONDS - 60))
