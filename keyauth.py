"""KeyAuth Seller API helpers for username/password accounts.

Each product stores its own Seller API key and subscription name. Keep both
server-side; never expose the Seller API key in browser code or logs.
Official docs: https://docs.keyauth.cc/sellerapi/users/create-new-user
"""
from __future__ import annotations

import logging
import secrets
import string
import re
from typing import Any

import requests

log = logging.getLogger("keyauth")
API_URL = "https://keyauth.win/api/seller/"
TIMEOUT = 25


def _raw_request(seller_key: str, **params: Any) -> dict:
    seller_key = str(seller_key or "").strip()
    if not seller_key:
        raise RuntimeError("Is product ka KeyAuth Seller API key set nahi hai.")
    query = {"sellerkey": seller_key, "format": "json", **params}
    try:
        response = requests.get(API_URL, params=query, timeout=TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except requests.Timeout as exc:
        raise RuntimeError("KeyAuth API timeout. Payment ko dobara charge na karein; admin se contact karein.") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"KeyAuth API connection fail: {str(exc)[:160]}") from exc
    except ValueError as exc:
        raise RuntimeError("KeyAuth API ne valid JSON return nahi kiya.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("KeyAuth API ka response invalid hai.")
    return data


def _request(seller_key: str, **params: Any) -> dict:
    data = _raw_request(seller_key, **params)
    if not data.get("success"):
        message = str(data.get("message") or "Unknown KeyAuth error")
        raise RuntimeError(f"KeyAuth API error: {message[:220]}")
    return data


def _days_value(days: float) -> str:
    days = float(days)
    if days <= 0:
        raise ValueError("Expiry positive days honi chahiye.")
    # Seller API's expiry is expressed in days, not seconds. Fractional days
    # preserve remaining hours when an account is reset.
    return f"{days:.6f}".rstrip("0").rstrip(".")


def create_user(seller_key: str, username: str, password: str, subscription: str, expiry_days: float) -> dict:
    username = str(username or "").strip()
    password = str(password or "")
    subscription = str(subscription or "").strip()
    if not username or len(username) > 64:
        raise ValueError("KeyAuth username missing/invalid hai.")
    if not password:
        raise ValueError("KeyAuth password blank nahi ho sakta.")
    if not subscription:
        raise ValueError("KeyAuth subscription name missing hai.")
    return _request(
        seller_key,
        type="adduser",
        user=username,
        sub=subscription,
        expiry=_days_value(expiry_days),
        **{"pass": password, "email": "", "hwidAffected": "0", "displayToken": "0"},
    )


def get_user_data(seller_key: str, username: str) -> dict | None:
    data = _raw_request(seller_key, type="userdata", user=str(username or "").strip())
    if data.get("success"):
        return data
    message = str(data.get("message") or "").lower()
    if any(term in message for term in ("not found", "does not exist", "doesn't exist", "no user", "user not exist")):
        return None
    raise RuntimeError(f"KeyAuth user lookup failed: {str(data.get('message') or 'Unknown error')[:200]}")


def subscription_expiry(user_data: dict | None, subscription: str) -> int | None:
    if not user_data:
        return None
    wanted = str(subscription or "").strip().lower()
    subs = user_data.get("subscriptions") or []
    if isinstance(subs, dict):
        subs = [subs]
    for item in subs:
        if not isinstance(item, dict):
            continue
        if str(item.get("subscription") or item.get("name") or "").strip().lower() == wanted:
            try:
                return int(float(item.get("expiry")))
            except (TypeError, ValueError):
                continue
    return None


def extend_user(seller_key: str, username: str, subscription: str, expiry_days: float) -> dict:
    return _request(
        seller_key,
        type="extend",
        user=str(username or "").strip(),
        sub=str(subscription or "").strip(),
        expiry=_days_value(expiry_days),
        activeOnly="false",
    )


def delete_user(seller_key: str, username: str) -> dict:
    return _request(seller_key, type="deluser", user=str(username or "").strip())


def create_or_extend_user(seller_key: str, username: str, password: str, subscription: str, expiry_days: float) -> dict:
    """Create account or extend the same username/subscription if it already exists."""
    existing = get_user_data(seller_key, username)
    if existing is None:
        try:
            created = create_user(seller_key, username, password, subscription, expiry_days)
        except RuntimeError as exc:
            # Handle a simultaneous purchase race only if the account now exists.
            existing = get_user_data(seller_key, username)
            if existing is None:
                raise
            created = None
        if created is not None:
            data = get_user_data(seller_key, username) or {}
            return {"created": True, "data": data, "response": created, "previous_expiry": None}
        # A race created the account between lookup and adduser.
        existing = existing or get_user_data(seller_key, username)

    if existing is None:
        raise RuntimeError("KeyAuth account create nahi ho paya.")
    existing_password = str(existing.get("password") or "")
    if existing_password and existing_password != password:
        raise RuntimeError("Is username ka KeyAuth account pehle se hai aur password '1' nahi hai. Admin ko KeyAuth dashboard par account check karna hoga.")
    previous_expiry = subscription_expiry(existing, subscription)
    if previous_expiry is None:
        raise RuntimeError("Username KeyAuth me pehle se hai, lekin required subscription nahi hai. Is product ke liye alag KeyAuth application/API use karein.")
    extended = extend_user(seller_key, username, subscription, expiry_days)
    data = get_user_data(seller_key, username) or existing
    return {"created": False, "data": data, "response": extended, "previous_expiry": previous_expiry}


def create_unique_or_extend_user(
    seller_key: str, subscription: str, expiry_days: float, password: str = "1",
    preferred_username: str | None = None, max_attempts: int = 12,
) -> dict:
    """Create a fresh 8-character alphanumeric account, or extend the same
    account when retrying an order that already owns that exact username.

    A username belonging to someone else (including an old deterministic
    username with another password) is never reused; another random username
    is generated instead.
    """
    alphabet = string.ascii_letters + string.digits
    subscription = str(subscription or "default").strip()
    password = str(password or "1")
    preferred = str(preferred_username or "").strip()
    candidates = []
    if re.fullmatch(r"[A-Za-z0-9]{8}", preferred):
        candidates.append(preferred)

    last_error = None
    for _ in range(max(1, int(max_attempts))):
        username = candidates.pop(0) if candidates else "".join(secrets.choice(alphabet) for _ in range(8))
        existing = get_user_data(seller_key, username)
        if existing is not None:
            previous_expiry = subscription_expiry(existing, subscription)
            # KeyAuth's userdata password field is hashed, not the plaintext password.
            # A stored 8-char preferred username belongs to this retrying order; if its
            # requested subscription exists, extend it without comparing hash to '1'.
            if username == preferred and previous_expiry is not None:
                extended = extend_user(seller_key, username, subscription, expiry_days)
                data = get_user_data(seller_key, username) or existing
                return {"created": False, "username": username, "data": data, "response": extended,
                        "previous_expiry": previous_expiry}
            # Random collision or an existing account not owned by this order: skip it.
            continue
        try:
            created = create_user(seller_key, username, password, subscription, expiry_days)
            data = get_user_data(seller_key, username)
            if data is None:
                raise RuntimeError("KeyAuth API ne account create kiya bataya, lekin userdata se username verify nahi hua. API key/app check karein.")
            if subscription_expiry(data, subscription) is None:
                raise RuntimeError(f"KeyAuth account bana, lekin subscription '{subscription}' verify nahi hui. Product ka subscription name KeyAuth dashboard se milayein.")
            return {"created": True, "username": username, "data": data, "response": created,
                    "previous_expiry": None}
        except RuntimeError as exc:
            last_error = exc
            # A concurrent request may have claimed this username. Try another
            # random value only if lookup confirms that it now exists.
            try:
                raced = get_user_data(seller_key, username)
            except Exception:
                raise exc
            if raced is None:
                raise exc
            previous_expiry = subscription_expiry(raced, subscription)
            if username == preferred and previous_expiry is not None:
                extended = extend_user(seller_key, username, subscription, expiry_days)
                data = get_user_data(seller_key, username) or raced
                return {"created": False, "username": username, "data": data, "response": extended,
                        "previous_expiry": previous_expiry}
    raise RuntimeError(f"Unique KeyAuth username generate nahi hua. {str(last_error or 'Dobara try karein.')[:180]}")



def reset_user_for_days(seller_key: str, username: str, password: str, subscription: str, expiry_days: int) -> dict:
    """Permanently recreate one KeyAuth user with a fresh full validity period.

    Only call this for a username stored against the selected order. The Seller API
    userdata password is normally hashed, so it must never be compared to plaintext.
    """
    username = str(username or "").strip()
    subscription = str(subscription or "default").strip()
    password = str(password or "1")
    days = int(expiry_days)
    if not username or not subscription or days < 1:
        raise ValueError("Username, subscription aur valid duration required hain.")
    existing = get_user_data(seller_key, username)
    if existing is not None:
        delete_user(seller_key, username)
    created = create_user(seller_key, username, password, subscription, days)
    verified = get_user_data(seller_key, username)
    if verified is None:
        raise RuntimeError("KeyAuth ne account create success diya, lekin userdata verification mein account nahi mila.")
    if subscription_expiry(verified, subscription) is None:
        raise RuntimeError(f"Account bana, lekin subscription '{subscription}' verify nahi hui. KeyAuth app/subscription settings check karein.")
    return {"created": created, "data": verified}

def health_check(seller_key: str) -> tuple[bool, str]:
    if not str(seller_key or "").strip():
        return False, "Seller API key missing"
    return True, "Configured; actual validation happens on a real account operation"


def create_license(seller_key: str, expiry_seconds: int, note: str = "") -> str:
    """Legacy license creation retained for orders created by the older build."""
    expiry_seconds = int(expiry_seconds)
    if expiry_seconds <= 0:
        raise ValueError("License expiry positive seconds honi chahiye.")
    data = _request(seller_key, type="add", expiry=_days_value(expiry_seconds / 86400), amount=1, level=1,
                    mask="", owner="", character="", note=str(note or "")[:250], displayToken="false")
    key = data.get("key")
    if isinstance(key, list):
        key = key[0] if key else ""
    if not isinstance(key, str) or not key.strip():
        keys = data.get("keys")
        if isinstance(keys, list) and keys:
            first = keys[0]
            key = first.get("key") if isinstance(first, dict) else first
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError("KeyAuth response me legacy license key nahi mili.")
    return key.strip()

def delete_license(seller_key: str, license_key: str, user_too: bool = False) -> None:
    _request(seller_key, type="del", key=str(license_key or "").strip(), userToo="true" if user_too else "false")
