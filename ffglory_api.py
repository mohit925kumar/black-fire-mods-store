import logging
import time

import requests
from config import (
    FFGLORY_BASE, FFGLORY_API_KEY, FFGLORY_MASTER_KEY,
    REQUEST_TIMEOUT,
)

log = logging.getLogger("ffglory")


# ============ CORE REQUEST ============

def _req(method: str, path: str, payload: dict = None, params: dict = None,
         retries: int = 2):
    """FFGlory API request. Master key optional hai — sirf tab bhejo jab ho."""
    if not FFGLORY_API_KEY:
        raise RuntimeError("FFGLORY_API_KEY .env me set nahi hai")

    q = {"api_key": FFGLORY_API_KEY}
    if params:
        q.update(params)

    # Master key optional — agar .env me hai to bhejo, warna skip
    headers = {"Accept": "application/json"}
    if FFGLORY_MASTER_KEY:
        headers["x-api-key"] = FFGLORY_MASTER_KEY

    url = f"{FFGLORY_BASE}{path}"
    last_error = None

    for attempt in range(retries + 1):
        try:
            r = requests.request(
                method, url, params=q, headers=headers,
                json=payload, timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.Timeout:
            last_error = f"FFGlory request timeout ({REQUEST_TIMEOUT}s)"
            log.warning("FFGlory timeout on %s %s (attempt %d)", method, path, attempt + 1)
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(last_error)
        except requests.exceptions.ConnectionError as e:
            last_error = f"FFGlory connection error: {str(e)[:200]}"
            log.warning("FFGlory conn error on %s %s", method, path)
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(last_error)

        # Parse JSON safely
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:500]}

        # 401 — clear message
        if r.status_code == 401:
            raise RuntimeError(
                "FFGlory 401 Unauthorized — API key galat ya expire ho gaya hai. "
                "FFGlory dashboard se naya key lo aur .env me FFGLORY_API_KEY update karo."
            )

        # 403 — permission issue
        if r.status_code == 403:
            raise RuntimeError(
                f"FFGlory 403 Forbidden — key valid hai lekin is endpoint ka "
                f"permission nahi. Response: {data}"
            )

        # 429 — rate limit, retry
        if r.status_code == 429:
            last_error = "FFGlory rate limit (429)"
            log.warning("FFGlory 429 on %s %s", method, path)
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(last_error)

        # 5xx — server error, retry
        if 500 <= r.status_code < 600:
            last_error = f"FFGlory server error {r.status_code}: {data}"
            log.warning("FFGlory %d on %s %s", r.status_code, method, path)
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(last_error)

        # Other non-OK
        if not r.ok:
            raise RuntimeError(f"FFGlory HTTP {r.status_code}: {data}")

        return data

    raise RuntimeError(last_error or "FFGlory request failed")


# ============ PUBLIC FUNCTIONS ============

def get_regions():
    """Public endpoint — koi API key nahi chahiye."""
    try:
        r = requests.get(f"{FFGLORY_BASE}/api/pricing", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except requests.exceptions.Timeout:
        raise RuntimeError("FFGlory pricing timeout — server slow hai")
    except requests.exceptions.ConnectionError:
        raise RuntimeError("FFGlory pricing — connection fail, internet check karo")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"FFGlory pricing HTTP {e.response.status_code}")

    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("FFGlory pricing — invalid JSON response")

    regions = data.get("regions", [])
    enabled = [x for x in regions if x.get("enabled")]
    log.info("FFGlory regions loaded: %d enabled / %d total", len(enabled), len(regions))
    return enabled


def get_account():
    """Admin account info — credits check."""
    return _req("GET", "/api/auth/me")


def launch_group(region: str, clan_id: str):
    """Guild Glory group launch karo."""
    if not region or not clan_id:
        raise RuntimeError("launch_group: region aur clan_id dono chahiye")
    return _req("POST", "/api/client/auto-clan-group",
                {"region": region, "clan_id": clan_id})


def check_api_credit() -> dict:
    """Alias with normalized output — admin panel ke liye."""
    info = get_account()
    return {
        "username": info.get("username", "?"),
        "basic_credits": info.get("basic_credits", 0),
        "premium_credits": info.get("premium_credits", 0),
        "max_groups": info.get("max_groups", 0),
        "raw": info,
    }


def health_check() -> tuple[bool, str]:
    """Bot start hote waqt API key valid hai ya nahi check karo."""
    if not FFGLORY_API_KEY:
        return False, "FFGLORY_API_KEY missing in .env"
    try:
        get_account()
        return True, "OK"
    except Exception as e:
        return False, str(e)[:200]