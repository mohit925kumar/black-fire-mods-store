import time
import logging

import requests
from config import RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, REQUEST_TIMEOUT

log = logging.getLogger("razorpay")

# QR lifetime (seconds) — must match telegram_bot.py ka QR_EXPIRE_SECONDS
QR_LIFETIME_SECONDS = 5 * 60   # 5 min


# ============ CORE REQUEST ============

def _req(method: str, path: str, payload: dict = None, retries: int = 2):
    """Razorpay API request with retry on transient errors."""
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RuntimeError("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET .env me missing")

    url = f"https://api.razorpay.com/v1{path}"
    last_error = None

    for attempt in range(retries + 1):
        try:
            r = requests.request(
                method, url,
                auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
                json=payload,
                timeout=REQUEST_TIMEOUT)
        except requests.exceptions.Timeout:
            last_error = f"Razorpay timeout ({REQUEST_TIMEOUT}s)"
            log.warning("Razorpay timeout %s %s (attempt %d)", method, path, attempt + 1)
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(last_error)
        except requests.exceptions.ConnectionError as e:
            last_error = f"Razorpay connection error: {str(e)[:200]}"
            log.warning("Razorpay conn error %s %s", method, path)
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(last_error)

        # Parse JSON
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:500]}

        # 401 — key galat
        if r.status_code == 401:
            raise RuntimeError(
                "Razorpay 401 — API key galat ya expire. "
                ".env me RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET check karo."
            )

        # 429 — rate limit, retry
        if r.status_code == 429:
            last_error = "Razorpay rate limit (429)"
            log.warning("Razorpay 429 %s %s", method, path)
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(last_error)

        # 5xx — server error, retry
        if 500 <= r.status_code < 600:
            last_error = f"Razorpay server error {r.status_code}: {data}"
            log.warning("Razorpay %d %s %s", r.status_code, method, path)
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(last_error)

        # Other non-OK
        if not r.ok:
            raise RuntimeError(f"Razorpay HTTP {r.status_code}: {data}")

        return data

    raise RuntimeError(last_error or "Razorpay request failed")


# ============ QR CODE ============

def create_qr(order_id: str, amount_paise: int, user_label: str):
    """
    Single-use UPI QR create karo.
    QR 5 min me expire hoga (QR_LIFETIME_SECONDS).
    """
    if amount_paise < 100:
        raise RuntimeError(f"Amount too small: ₹{amount_paise/100} (minimum ₹1)")

    payload = {
        "type": "upi_qr",
        "name": f"FFGlory {order_id}"[:100],
        "usage": "single_use",
        "fixed_amount": True,
        "payment_amount": int(amount_paise),
        "description": f"Order {order_id} for {user_label}"[:255],
        "close_by": int(time.time()) + QR_LIFETIME_SECONDS,
        "notes": {
            "order_id": order_id,
            "user_label": user_label[:100],
        },
    }

    log.info("Creating QR for order %s amount ₹%d expire in %ds",
             order_id, amount_paise // 100, QR_LIFETIME_SECONDS)
    data = _req("POST", "/payments/qr_codes", payload)

    if not data.get("id") or not data.get("image_url"):
        raise RuntimeError(f"Razorpay QR response invalid: {data}")

    log.info("QR created: %s for order %s", data.get("id"), order_id)
    return data


# ============ PAYMENTS ============

def fetch_qr_payments(qr_code_id: str):
    """QR pe aane wale payments list karo."""
    return _req("GET", f"/payments/qr_codes/{qr_code_id}/payments")


def fetch_payment(payment_id: str):
    """Single payment ki details."""
    return _req("GET", f"/payments/{payment_id}")


def find_captured_payment(qr_code_id: str, expected_amount_paise: int):
    """
    QR pe captured payment dhundho jo exact amount ka ho.
    Return payment dict ya None.
    """
    try:
        result = fetch_qr_payments(qr_code_id)
    except Exception as e:
        log.warning("fetch_qr_payments failed for %s: %s", qr_code_id, str(e)[:200])
        return None

    items = result.get("items", []) if isinstance(result, dict) else []

    for p in items:
        status = str(p.get("status", "")).lower()
        amount = int(p.get("amount", -1))
        currency = str(p.get("currency", "INR")).upper()

        if status == "captured" and amount == expected_amount_paise and currency == "INR":
            return p

    return None


# ============ HEALTH CHECK ============

def health_check() -> tuple[bool, str]:
    """Razorpay keys valid hain ya nahi."""
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return False, "RAZORPAY keys .env me missing"
    try:
        # Simple call — payment QR list (koi bhi valid endpoint)
        _req("GET", "/payments/qr_codes?count=1")
        return True, "OK"
    except Exception as e:
        return False, str(e)[:200]

# ============ ORDER / PAYMENT LOOKUP ============
def fetch_order(order_id: str):
    """Fetch a Razorpay order by its Razorpay order ID (order_...)."""
    order_id = str(order_id or "").strip()
    if not order_id:
        raise ValueError("Order ID is required")
    return _req("GET", f"/orders/{order_id}")


def fetch_order_payments(order_id: str):
    """List payments associated with a Razorpay order."""
    order_id = str(order_id or "").strip()
    if not order_id:
        raise ValueError("Order ID is required")
    return _req("GET", f"/orders/{order_id}/payments")


def list_payments(count: int = 100):
    """List the most recent Razorpay payments for fallback search."""
    count = max(1, min(int(count), 100))
    return _req("GET", f"/payments?count={count}")


def search_payments_by_order_id(order_id: str, local_order=None):
    """Search Razorpay payment records using our local order ID or Razorpay order ID.

    A local QR order is searched against its own QR code first, which avoids confusing
    payments from unrelated orders. Returns a plain dict suitable for JSON serialization.
    """
    order_id = str(order_id or "").strip()
    if not order_id:
        raise ValueError("Order ID is required")
    found = []
    source = "recent_payments_fallback"
    razorpay_order = None

    if local_order and local_order.get("qr_code_id"):
        result = fetch_qr_payments(local_order["qr_code_id"])
        found = result.get("items", []) if isinstance(result, dict) else []
        source = "local_order_qr"
    elif order_id.startswith("order_"):
        razorpay_order = fetch_order(order_id)
        result = fetch_order_payments(order_id)
        found = result.get("items", []) if isinstance(result, dict) else []
        source = "razorpay_order"
    else:
        result = list_payments(100)
        items = result.get("items", []) if isinstance(result, dict) else []
        for payment in items:
            notes = payment.get("notes") or {}
            if (str(payment.get("order_id") or "") == order_id
                    or str(notes.get("order_id") or "") == order_id
                    or str(notes.get("receipt") or "") == order_id):
                found.append(payment)
        source = "recent_payments_notes_match"

    # Expand each record where possible, keeping failed payments too for diagnosis.
    expanded = []
    for payment in found[:30]:
        payment_id = payment.get("id")
        detail = payment
        if payment_id:
            try:
                detail = fetch_payment(payment_id)
            except Exception:
                detail = payment
        expanded.append(detail)

    return {
        "searched_order_id": order_id,
        "source": source,
        "local_order_found": bool(local_order),
        "local_order": dict(local_order) if local_order else None,
        "razorpay_order": razorpay_order,
        "payments": expanded,
        "payment_count": len(expanded),
    }
