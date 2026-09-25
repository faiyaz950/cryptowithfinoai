#!/usr/bin/env python3
"""
Backend API for Crypto Trading Website
EMA aur Candle Data ke liye API endpoints
"""

from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request, Response, g
from flask_cors import CORS
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
import time
from fetch_trading_data import BASE_URL, CryptoAPIClient, to_delta_symbol
from exchanges import SUPPORTED_EXCHANGES, catalogue, exchange_name, get_adapter, message_for
from exchanges.market import get_market_source, market_catalogue
from options_backtest import create_options_backtest_blueprint
import os
import base64
import hashlib
import importlib
import json
import re
import requests
import secrets
import smtplib
import ssl
import threading
from functools import wraps
from email.message import EmailMessage
from werkzeug.security import generate_password_hash, check_password_hash
from django_orm import (
    init_database,
    database_backend,
    save_demo_order_entry,
    fetch_recent_orders,
    create_user_account,
    get_user_account_by_username,
    get_user_account_by_email,
    get_user_account_by_id,
    get_user_account_by_tv_token,
    update_user_account_fields,
    create_user_session,
    get_active_session_by_token,
    deactivate_session,
    create_exchange_account,
    list_exchange_accounts_for_user,
    get_exchange_account_for_user,
    get_exchange_account_by_fingerprint,
    update_exchange_account_status,
    update_exchange_account_credentials,
    delete_exchange_account_for_user,
    get_latest_exchange_account_for_user,
    save_byok_order_entry,
    fetch_byok_orders,
    create_email_change_otp,
    verify_email_change_otp,
)
try:
    _fernet_module = importlib.import_module("cryptography.fernet")
    Fernet = getattr(_fernet_module, "Fernet", None)
except Exception:
    Fernet = None

app = Flask(__name__)

# Merged backend (backend/main.py) mein CORS FastAPI ki CORSMiddleware handle karti hai.
# Dono jagah lagane se browser ko do Access-Control-Allow-Origin headers milte hain aur
# wo request block kar deta hai. Standalone Flask chalane par yahi CORS lagti hai.
if os.getenv("MERGED_BACKEND") != "1":
    CORS(app)

# API credentials (Delta Exchange only) — supplied via environment.
# See .env.example; fetch_trading_data loads the local .env on import.
API_KEY = os.environ.get("DELTA_API_KEY", "")
SECRET_KEY = os.environ.get("DELTA_SECRET_KEY", "")

# Global client
client = CryptoAPIClient(API_KEY, SECRET_KEY)


DB_READY = True


def _bootstrap_database():
    """
    Django ORM sync hai aur agar current thread mein event loop chal raha ho to
    `SynchronousOnlyOperation` phenk deta hai. Merged backend (backend/main.py)
    ko uvicorn event loop ke andar import karta hai, isliye seedha yahan call
    karne par tables banne se pehle hi fail ho jaata tha — aur DB_READY False
    hone se saare auth endpoints band ho jaate the.

    Alag thread mein koi running loop nahi hota, isliye guard trigger nahi hota.
    Standalone Flask run par bhi ye bilkul theek chalta hai.
    """
    global DB_READY
    try:
        init_database()
    except Exception as exc:
        DB_READY = False
        print(f"⚠️ Database initialization failed: {exc}")


_db_thread = threading.Thread(target=_bootstrap_database, name="db-init")
_db_thread.start()
_db_thread.join()


SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "24"))
EMAIL_OTP_TTL_MINUTES = int(os.getenv("EMAIL_OTP_TTL_MINUTES", "10"))
EMAIL_OTP_DEBUG = os.getenv("EMAIL_OTP_DEBUG", "true").lower() == "true"
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", SMTP_USERNAME).strip()
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
PASSWORD_HASH_METHOD = os.getenv("PASSWORD_HASH_METHOD", "pbkdf2:sha256")


def _build_fernet_key(raw_value):
    if not raw_value:
        return None
    raw_value = raw_value.strip()
    if len(raw_value) == 44:
        return raw_value.encode("utf-8")
    digest = hashlib.sha256(raw_value.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _get_cipher():
    if Fernet is None:
        raise RuntimeError("cryptography package missing. Install requirements first.")
    configured_key = os.getenv("BYOK_ENCRYPTION_KEY")
    if not configured_key:
        # Local fallback for development; production must set BYOK_ENCRYPTION_KEY.
        configured_key = f"fallback-{SECRET_KEY}"
    return Fernet(_build_fernet_key(configured_key))


def encrypt_secret(plain_value):
    cipher = _get_cipher()
    return cipher.encrypt((plain_value or "").encode("utf-8")).decode("utf-8")


def decrypt_secret(encrypted_value):
    cipher = _get_cipher()
    return cipher.decrypt((encrypted_value or "").encode("utf-8")).decode("utf-8")


def key_hint(api_key):
    api_key = (api_key or "").strip()
    if len(api_key) <= 6:
        return "***"
    return f"{api_key[:4]}...{api_key[-4:]}"


def api_key_fingerprint(exchange, api_key):
    raw = f"{(exchange or '').lower().strip()}::{(api_key or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_unique_username(base):
    normalized = re.sub(r"[^a-zA-Z0-9_.-]", "", (base or "").strip().lower())[:28]
    if len(normalized) < 3:
        normalized = f"user{secrets.randbelow(9000) + 1000}"
    if not get_user_account_by_username(normalized):
        return normalized
    for i in range(1, 500):
        candidate = f"{normalized[:24]}_{i}"
        if not get_user_account_by_username(candidate):
            return candidate
    return f"user_{secrets.token_hex(4)}"


def hash_password(password):
    # Force pbkdf2 by default because some Python builds lack hashlib.scrypt.
    return generate_password_hash(password, method=PASSWORD_HASH_METHOD)


def parse_bearer_token(req):
    auth_header = req.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()
    return (req.headers.get("X-Session-Token") or "").strip()


def require_auth(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not DB_READY:
            return jsonify({"success": False, "error": "Database unavailable"}), 503
        token = parse_bearer_token(request)
        if not token:
            return jsonify({"success": False, "error": "Missing auth token"}), 401
        session = get_active_session_by_token(token)
        if not session:
            return jsonify({"success": False, "error": "Invalid/expired session"}), 401
        user = get_user_account_by_id(session["user_id"])
        if not user or not user.get("is_active", False):
            return jsonify({"success": False, "error": "User not active"}), 401
        g.auth_token = token
        g.user = user
        g.session = session
        return func(*args, **kwargs)

    return wrapper


_EGRESS_IP_CACHE = {'ip': None, 'at': 0.0}
_EGRESS_IP_TTL = 3600.0


def _configured_egress_ips():
    raw = os.getenv('BYOK_EGRESS_IPS', '') or ''
    seen, ips = set(), []
    for part in re.split(r'[,\s]+', raw):
        part = part.strip()
        if part and part not in seen:
            seen.add(part)
            ips.append(part)
    return ips


def _detect_egress_ip():
    """Apna public IP — ek ghante cache, aur fail ho to chup-chaap None."""
    now = time.time()
    if _EGRESS_IP_CACHE['ip'] and now - _EGRESS_IP_CACHE['at'] < _EGRESS_IP_TTL:
        return _EGRESS_IP_CACHE['ip']
    for url in ('https://api.ipify.org?format=json', 'https://ifconfig.co/json'):
        try:
            data = requests.get(url, timeout=4).json()
            ip = (data.get('ip') or '').strip()
            if ip:
                _EGRESS_IP_CACHE.update(ip=ip, at=now)
                return ip
        except Exception:
            continue
    return None


def validate_exchange_credentials(exchange, api_key, secret_key):
    """Key sach mein chalti hai? Kaunsa exchange hai, ye adapter tay karta hai."""
    adapter = get_adapter(exchange, api_key, secret_key)
    if adapter is None:
        return {
            "success": False,
            "can_trade": False,
            "can_withdraw": False,
            "permissions_verified": False,
            "reason": "adapter_missing",
            "error": message_for("adapter_missing", exchange=exchange_name(exchange)),
        }
    return adapter.verify()


def fetch_exchange_profile(exchange, api_key, secret_key):
    adapter = get_adapter(exchange, api_key, secret_key)
    return adapter.profile() if adapter else {}


def _deep_find_first(data, keys):
    normalized_targets = {str(k).strip().lower().replace("-", "_") for k in (keys or set())}
    if isinstance(data, dict):
        for key, value in data.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if normalized_key in normalized_targets and value not in (None, "", []):
                return value
        for value in data.values():
            found = _deep_find_first(value, keys)
            if found not in (None, "", []):
                return found
    elif isinstance(data, list):
        for item in data:
            found = _deep_find_first(item, keys)
            if found not in (None, "", []):
                return found
    return None


def _deep_find_all(data, keys):
    normalized_targets = {str(k).strip().lower().replace("-", "_") for k in (keys or set())}
    results = []
    if isinstance(data, dict):
        for k, value in data.items():
            normalized_key = str(k).strip().lower().replace("-", "_")
            if normalized_key in normalized_targets and value not in (None, ""):
                if isinstance(value, list):
                    for v in value:
                        if v not in (None, ""):
                            results.append(str(v))
                else:
                    results.append(str(value))
            results.extend(_deep_find_all(value, keys))
    elif isinstance(data, list):
        for item in data:
            results.extend(_deep_find_all(item, keys))
    deduped = []
    seen = set()
    for item in results:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def extract_user_profile_from_exchange(profile_data):
    if not isinstance(profile_data, dict):
        return "", ""
    first_name = _deep_find_first(profile_data, {"first_name", "firstname", "given_name"})
    last_name = _deep_find_first(profile_data, {"last_name", "lastname", "family_name"})
    combined = " ".join([str(first_name or "").strip(), str(last_name or "").strip()]).strip()
    profile_name = (
        combined
        or _deep_find_first(profile_data, {"name", "full_name", "display_name", "account_name", "client_name"})
        or ""
    )
    profile_email = (
        _deep_find_first(profile_data, {"email", "user_email", "registered_email", "primary_email", "mail"})
        or ""
    )
    return str(profile_name).strip(), str(profile_email).strip().lower()


def extract_exchange_profile_snapshot(profile_data):
    if not isinstance(profile_data, dict):
        return {}
    account_name = _deep_find_first(
        profile_data,
        {
            "account_name",
            "accountName",
            "name",
            "display_name",
            "client_name",
            "api_key_name",
            "key_name",
            "label",
            "title",
        },
    ) or ""
    exchange_email = _deep_find_first(profile_data, {"email", "user_email", "registered_email"}) or ""
    exchange_phone = _deep_find_first(
        profile_data,
        {"phone", "phone_no", "phone_number", "mobile", "mobile_no", "mobile_number", "contact_number"},
    ) or ""
    exchange_username = _deep_find_first(
        profile_data,
        {"username", "user_name", "login_id", "login", "uid", "user_id", "client_code"},
    ) or ""
    permissions = _deep_find_all(profile_data, {"permissions", "permission", "scope", "scopes", "access", "access_type"})
    whitelist_ips = _deep_find_all(
        profile_data,
        {"whitelisted_ip", "whitelisted_ips", "whitelisted_ip_addresses", "ip_whitelist", "ip", "ips"},
    )
    created_at = _deep_find_first(profile_data, {"created_at", "created_on", "created_time", "created"}) or ""
    return {
        "account_name": str(account_name).strip()[:120],
        "exchange_email": str(exchange_email).strip().lower()[:180],
        "exchange_username": str(exchange_username).strip()[:120],
        "exchange_phone": str(exchange_phone).strip()[:40],
        "permissions": permissions[:20],
        "whitelisted_ips": whitelist_ips[:20],
        "created_at": str(created_at).strip()[:80] if created_at else "",
        "has_profile_data": True,
    }


def _mask_fingerprint(value):
    raw = (value or "").strip()
    if len(raw) < 12:
        return raw
    return f"{raw[:8]}...{raw[-8:]}"


def extract_wallet_snapshot(wallet_data):
    # Build minimal, non-sensitive, user-verifiable wallet summary.
    if isinstance(wallet_data, dict):
        if isinstance(wallet_data.get("balances"), list):
            rows = wallet_data.get("balances") or []
        elif isinstance(wallet_data.get("result"), list):
            rows = wallet_data.get("result") or []
        else:
            rows = [wallet_data]
    elif isinstance(wallet_data, list):
        rows = wallet_data
    else:
        rows = []

    non_zero_assets = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = (
            row.get("asset_symbol")
            or row.get("symbol")
            or row.get("asset")
            or row.get("currency")
            or row.get("code")
            or ""
        )
        amount = (
            row.get("balance")
            or row.get("available_balance")
            or row.get("available")
            or row.get("free")
            or row.get("equity")
            or 0
        )
        try:
            amount_num = float(amount)
        except Exception:
            amount_num = 0.0
        if symbol and abs(amount_num) > 0:
            non_zero_assets.append(str(symbol).upper())

    deduped_assets = []
    seen = set()
    for asset in non_zero_assets:
        if asset not in seen:
            deduped_assets.append(asset)
            seen.add(asset)

    return {
        "balance_rows": len(rows),
        "non_zero_assets": deduped_assets[:10],
        "has_wallet_data": len(rows) > 0,
    }


def fetch_live_delta_metadata(account_full):
    """Legacy /api/profile ke liye — ab adapter se, sirf delta se nahi."""
    exchange_profile, wallet_snapshot = {}, {}
    auth_proof = {"private_api_access": False, "last_auth_error": ""}
    fingerprint_masked = _mask_fingerprint(account_full.get("api_key_fingerprint", ""))
    encrypted_key = account_full.get("api_key_encrypted") or ""
    encrypted_secret = account_full.get("secret_key_encrypted") or ""
    if not encrypted_key or not encrypted_secret:
        return exchange_profile, wallet_snapshot, auth_proof, fingerprint_masked

    try:
        adapter = get_adapter(
            account_full.get("exchange") or "delta",
            decrypt_secret(encrypted_key),
            decrypt_secret(encrypted_secret),
        )
        if adapter:
            exchange_profile = extract_exchange_profile_snapshot(adapter.profile() or {})
            wallet_snapshot = extract_wallet_snapshot(adapter.balances() or [])
            auth_proof = {
                "private_api_access": bool(exchange_profile.get("has_profile_data"))
                or bool(wallet_snapshot.get("has_wallet_data")),
                "last_auth_error": (adapter.last_error or "")[:300],
            }
    except Exception as e:
        auth_proof = {"private_api_access": False, "last_auth_error": str(e)[:300]}

    return exchange_profile, wallet_snapshot, auth_proof, fingerprint_masked


def validate_username(username):
    return bool(re.fullmatch(r"[a-zA-Z0-9_.-]{3,32}", username or ""))


def validate_password(password):
    return len(password or "") >= 8


def validate_full_name(full_name):
    name = (full_name or "").strip()
    return 2 <= len(name) <= 50


def validate_email(email):
    if not email:
        return False
    email = email.strip().lower()
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email))


def map_api_connection_status(account):
    if not account:
        return "not_added", "Not Added ⚪"
    if not account.get("is_active", False):
        return "not_added", "Not Added ⚪"
    if account.get("permissions_verified") and account.get("can_trade") and not account.get("can_withdraw"):
        return "connected", "Connected ✅"
    return "invalid", "Invalid ❌"


def send_otp_email(to_email, otp_code, ttl_minutes):
    """
    Send OTP via SMTP.
    Returns (success: bool, error_message: str).
    """
    if not SMTP_HOST or not SMTP_FROM_EMAIL:
        return False, "SMTP is not configured"

    subject = "Your Email Verification OTP"
    body_text = (
        f"Your OTP is: {otp_code}\n"
        f"This code expires in {ttl_minutes} minutes.\n\n"
        "If you did not request this, please ignore this email."
    )
    body_html = f"""
    <html>
      <body>
        <p>Your OTP is: <b>{otp_code}</b></p>
        <p>This code expires in <b>{ttl_minutes} minutes</b>.</p>
        <p>If you did not request this, please ignore this email.</p>
      </body>
    </html>
    """

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body_text)
    msg.add_alternative(body_html, subtype="html")

    try:
        if SMTP_USE_TLS:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                if SMTP_USERNAME:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15, context=ssl.create_default_context()) as server:
                if SMTP_USERNAME:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def calculate_ema(data, period):
    """Exponential Moving Average (EMA) calculate karta hai"""
    if isinstance(data, list):
        data = pd.Series(data)
    ema = data.ewm(span=period, adjust=False).mean()
    return ema


def calculate_rsi(data, period=14):
    """Relative Strength Index (RSI) calculate karta hai"""
    if isinstance(data, list):
        data = pd.Series(data)
    
    # Calculate price changes
    delta = data.diff()
    
    # Separate gains and losses
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    
    # Calculate RS and RSI
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi


def prepare_candle_data_with_ema(df, ema_periods=[9, 21, 50], rsi_period=14, include_rsi=False):
    """Candle data ko format karta hai aur EMA add karta hai"""
    # Get close prices
    if 'Close' in df.columns:
        close_prices = df['Close']
    elif 'close' in df.columns:
        close_prices = df['close']
    else:
        close_prices = df.iloc[:, 4]
    
    # Calculate EMAs
    ema_data = {}
    for period in ema_periods:
        if len(close_prices) >= period:
            ema_values = calculate_ema(close_prices, period)
            ema_data[f'EMA_{period}'] = ema_values.tolist()
        else:
            ema_data[f'EMA_{period}'] = [None] * len(df)
    
    # Calculate RSI if requested
    rsi_data = {}
    if include_rsi:
        rsi_values = calculate_rsi(close_prices, rsi_period)
        rsi_data['RSI'] = rsi_values.tolist()
    
    # Format candle data for frontend
    candles = []
    for i, (_idx, row) in enumerate(df.iterrows()):
        # Handle different column name formats
        open_price = row.get('Open', row.get('open', row.iloc[1] if len(row) > 1 else None))
        high_price = row.get('High', row.get('high', row.iloc[2] if len(row) > 2 else None))
        low_price = row.get('Low', row.get('low', row.iloc[3] if len(row) > 3 else None))
        close_price = row.get('Close', row.get('close', row.iloc[4] if len(row) > 4 else None))
        volume = row.get('Volume', row.get('volume', row.iloc[5] if len(row) > 5 else None))
        
        # Handle timestamp
        if 'Open Time' in row.index:
            timestamp = pd.Timestamp(row['Open Time']).timestamp() * 1000
        elif 'open_time' in row.index:
            timestamp = pd.Timestamp(row['open_time']).timestamp() * 1000
        elif 'Start Time' in row.index:
            timestamp = pd.Timestamp(row['Start Time']).timestamp() * 1000
        else:
            timestamp = int(time.time() * 1000)
        
        candle = {
            'time': int(timestamp),
            'open': float(open_price) if open_price else None,
            'high': float(high_price) if high_price else None,
            'low': float(low_price) if low_price else None,
            'close': float(close_price) if close_price else None,
            'volume': float(volume) if volume else 0
        }
        
        # Add EMA values
        for ema_key, ema_values in ema_data.items():
            if i < len(ema_values):
                val = ema_values[i]
                candle[ema_key.lower()] = float(val) if val is not None and pd.notna(val) else None
        
        # Add RSI values if requested
        if include_rsi:
            for rsi_key, rsi_values in rsi_data.items():
                if i < len(rsi_values):
                    val = rsi_values[i]
                    candle[rsi_key.lower()] = float(val) if val is not None and pd.notna(val) else None
        
        candles.append(candle)
    
    return {
        'candles': candles,
        'ema_periods': ema_periods,
        'total_candles': len(candles),
        'rsi_period': rsi_period if include_rsi else None,
        'rsi_enabled': include_rsi
    }


def _market_candles_to_df(rows):
    """MarketSource ke candle dicts → prepare_candle_data_with_ema wala DataFrame."""
    data = []
    for c in rows or []:
        t = c.get('time')
        if t is None:
            continue
        data.append({
            'Open Time': pd.Timestamp(int(t), unit='ms'),
            'Open': float(c.get('open') or 0),
            'High': float(c.get('high') or 0),
            'Low': float(c.get('low') or 0),
            'Close': float(c.get('close') or 0),
            'Volume': float(c.get('volume') or 0),
        })
    return pd.DataFrame(data)


@app.route('/api/market/sources', methods=['GET'])
def market_sources():
    """Chart kis-kis exchange se aa sakta hai, aur kaun se timeframes."""
    return jsonify({'success': True, 'data': market_catalogue()})


@app.route('/api/candles', methods=['GET'])
def get_candles():
    """
    Candle data with EMA.

    `exchange` query se chart ka source chunta hai (delta / coindcx / bybit).
    Har exchange ka apna bhaav hota hai — jo user trade karta hai usi ka chart
    dikhana chahiye, isliye default ab bhi delta hai par connected exchange
    frontend khud bhejta hai.
    """
    try:
        symbol = request.args.get('symbol', 'BTCUSDT')
        interval = request.args.get('interval', '1h')
        limit = int(request.args.get('limit', 100))
        exchange = (request.args.get('exchange') or 'delta').strip().lower()

        ema_periods_str = request.args.get('ema_periods', '9,21,50')
        ema_periods = [int(p.strip()) for p in ema_periods_str.split(',') if p.strip()]

        rsi_period = int(request.args.get('rsi_period', 14))
        include_rsi = request.args.get('include_rsi', 'false').lower() == 'true'

        src = get_market_source(exchange)
        if src is None:
            return jsonify({
                'error': f'Chart source "{exchange}" support nahi hai.',
                'success': False,
            }), 400

        if interval not in src.INTERVALS:
            return jsonify({
                'error': f'{src.name} par {interval} timeframe nahi hai.',
                'success': False,
                'exchange': src.id,
                'intervals': src.intervals(),
            }), 400

        rows = src.candles(symbol, interval, limit)
        if rows is None:
            return jsonify({
                'error': f'{src.name} se data nahi mila. Symbol ya network check karein.',
                'success': False,
                'exchange': src.id,
            }), 400

        df = _market_candles_to_df(rows)
        if df is None or len(df) == 0:
            return jsonify({
                'error': 'Candle data empty hai. Symbol ya network check karein.',
                'success': False,
                'exchange': src.id,
            }), 400

        result = prepare_candle_data_with_ema(df, ema_periods, rsi_period, include_rsi)

        return jsonify({
            'success': True,
            'symbol': symbol,
            'interval': interval,
            'exchange': src.id,
            'exchange_name': src.name,
            **result,
        })

    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({
            'error': str(e),
            'success': False
        }), 500


SCREENER_UNIVERSE = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT', 'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT',
    'LINKUSDT', 'LTCUSDT', 'UNIUSDT', 'AAVEUSDT', 'ARBUSDT', 'ZECUSDT', 'BCHUSDT', 'DASHUSDT',
    'ENAUSDT', 'TRUMPUSDT', 'DOTUSDT', 'SUIUSDT', 'FILUSDT', 'TRXUSDT', 'APTUSDT', 'INJUSDT',
    'NEARUSDT', 'XLMUSDT', 'TIAUSDT', 'POLUSDT', '1000PEPEUSDT', 'PENGUUSDT',
]

SCREENER_MAX_SYMBOLS = 40
SCREENER_CACHE_TTL = 60  # seconds
SCREENER_WORKERS = 6

# { (symbol, interval, limit): (fetched_at, candles) }
_screener_cache = {}


def _screener_fetch(symbol, interval, limit):
    """Ek symbol ki candles — cache hit ho to exchange ko dobara nahi poochhte."""
    key = (symbol, interval, limit)
    hit = _screener_cache.get(key)
    now = time.time()
    if hit and (now - hit[0]) < SCREENER_CACHE_TTL:
        return symbol, hit[1], None, True

    try:
        data = client.get_historical_data(
            symbol=symbol,
            interval=interval,
            limit=limit,
            exchange_name='delta',
        )
        if not data or 'dataframe' not in data or data['dataframe'] is None or len(data['dataframe']) == 0:
            return symbol, None, 'No candle data', False
        # Indicators frontend compute karta hai (wahi math strategy pages use karte hain),
        # isliye yahan sirf raw OHLC bhejte hain — payload chhota rehta hai.
        prepared = prepare_candle_data_with_ema(data['dataframe'], ema_periods=[], include_rsi=False)
        candles = prepared['candles']
        _screener_cache[key] = (now, candles)
        return symbol, candles, None, False
    except Exception as exc:
        return symbol, None, str(exc), False


@app.route('/api/screener', methods=['GET'])
def screener_candles():
    """
    Ek request mein kai symbols ki candles — AI coin screener ke liye.
    Browser se 30 alag calls maarne se accha hai: yahan parallel fetch + 60s cache hota hai.
    """
    try:
        raw_symbols = (request.args.get('symbols') or '').strip()
        if raw_symbols:
            symbols = [s.strip().upper() for s in raw_symbols.split(',') if s.strip()]
        else:
            symbols = list(SCREENER_UNIVERSE)

        # Duplicate hata do par order wahi rakho jo client ne bheja.
        seen = set()
        symbols = [s for s in symbols if not (s in seen or seen.add(s))]

        if not symbols:
            return jsonify({'success': False, 'error': 'No symbols requested'}), 400
        if len(symbols) > SCREENER_MAX_SYMBOLS:
            return jsonify({
                'success': False,
                'error': f'Too many symbols ({len(symbols)}). Max {SCREENER_MAX_SYMBOLS} per request.'
            }), 400

        interval = request.args.get('interval', '1h')
        try:
            limit = int(request.args.get('limit', 200))
        except (TypeError, ValueError):
            limit = 200
        limit = max(60, min(limit, 500))

        started = time.time()
        with ThreadPoolExecutor(max_workers=SCREENER_WORKERS) as pool:
            outcomes = list(pool.map(lambda sym: _screener_fetch(sym, interval, limit), symbols))

        results = []
        failed = []
        cached_count = 0
        for symbol, candles, error, was_cached in outcomes:
            if error or not candles:
                failed.append({'symbol': symbol, 'error': error or 'No candle data'})
                continue
            if was_cached:
                cached_count += 1
            results.append({'symbol': symbol, 'candles': candles})

        print(f"🔎 Screener: {len(results)} ok, {len(failed)} failed, "
              f"{cached_count} from cache, {time.time() - started:.1f}s ({interval}, {limit} bars)")

        return jsonify({
            'success': True,
            'interval': interval,
            'limit': limit,
            'requested': len(symbols),
            'results': results,
            'failed': failed,
        })
    except Exception as e:
        print(f"❌ Screener error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


OPTIONS_CACHE_TTL = 60  # seconds
_options_cache = {}


def _parse_option_expiry(symbol):
    """`C-BTC-80000-070926` ka aakhri hissa DDMMYY hai -> datetime (UTC)."""
    try:
        tail = symbol.rsplit('-', 1)[-1]
        if len(tail) != 6 or not tail.isdigit():
            return None
        day, month, year = int(tail[:2]), int(tail[2:4]), 2000 + int(tail[4:])
        # Delta ke options 12:00 UTC par settle hote hain.
        return datetime(year, month, day, 12, 0, 0)
    except Exception:
        return None


def _fnum(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fetch_option_tickers(option_type):
    """Delta se call ya put options ke tickers — 60s cache ke saath."""
    contract_type = 'call_options' if option_type == 'call' else 'put_options'
    hit = _options_cache.get(contract_type)
    now = time.time()
    if hit and (now - hit[0]) < OPTIONS_CACHE_TTL:
        return hit[1]

    res = requests.get(
        f"{BASE_URL}/v2/tickers",
        params={'contract_types': contract_type},
        timeout=30,
    )
    res.raise_for_status()
    rows = res.json().get('result') or []
    _options_cache[contract_type] = (now, rows)
    return rows


@app.route('/api/options/chain', methods=['GET'])
def options_chain():
    """
    Directional option buying ke liye option chain + strike selection.

    Strategy A (OTM directional) fixed "5% OTM" nahi, balki Delta band se strike
    chunti hai, aur liquidity filters lagati hai. Ye endpoint wahi kaam karta hai:
    candidates deta hai aur ek `selected` pick bhi, taaki UI ko dobara logic na
    likhna pade.
    """
    try:
        underlying = (request.args.get('underlying') or 'BTC').upper()
        option_type = (request.args.get('option_type') or 'call').lower()
        if option_type not in ('call', 'put'):
            return jsonify({'success': False, 'error': "option_type must be 'call' or 'put'"}), 400

        min_delta = abs(float(request.args.get('min_delta', 0.25)))
        max_delta = abs(float(request.args.get('max_delta', 0.35)))
        if min_delta > max_delta:
            min_delta, max_delta = max_delta, min_delta

        # Liquidity filters — doc: tight bid/ask, good volume, no abnormal premium.
        max_spread_pct = float(request.args.get('max_spread_pct', 5))
        min_oi = float(request.args.get('min_oi', 0))
        # Expiry itni door honi chahiye ki time-exit rule (2h pehle) sensible rahe.
        min_hours = float(request.args.get('min_hours_to_expiry', 4))

        rows = _fetch_option_tickers(option_type)
        now = datetime.utcnow()

        candidates = []
        for r in rows:
            if str(r.get('underlying_asset_symbol', '')).upper() != underlying:
                continue

            greeks = r.get('greeks') or {}
            delta = _fnum(greeks.get('delta'))
            if delta is None:
                continue

            expiry = _parse_option_expiry(str(r.get('symbol', '')))
            hours_left = (expiry - now).total_seconds() / 3600 if expiry else None

            quotes = r.get('quotes') or {}
            bid = _fnum(quotes.get('best_bid'))
            ask = _fnum(quotes.get('best_ask'))
            mid = (bid + ask) / 2 if bid and ask else None
            spread_pct = ((ask - bid) / mid * 100) if (mid and mid > 0) else None

            entry = {
                'symbol': r.get('symbol'),
                'strike': _fnum(r.get('strike_price')),
                'spot': _fnum(greeks.get('spot')) or _fnum(r.get('spot_price')),
                'expiry': expiry.isoformat() + 'Z' if expiry else None,
                'hours_to_expiry': round(hours_left, 2) if hours_left is not None else None,
                # Puts par delta negative hota hai; comparison ke liye magnitude use karte hain.
                'delta': round(delta, 4),
                'abs_delta': round(abs(delta), 4),
                # Theta per din hai (USD), vega per 1% IV point, gamma per $1 spot.
                # OTM buying mein theta hi sabse bada risk hai, isliye ye zaroori hain.
                'theta': _fnum(greeks.get('theta')),
                'vega': _fnum(greeks.get('vega')),
                'gamma': _fnum(greeks.get('gamma')),
                'rho': _fnum(greeks.get('rho')),
                'iv': _fnum(quotes.get('mark_iv')) or _fnum(r.get('mark_vol')),
                'premium': _fnum(r.get('mark_price')),
                'best_bid': bid,
                'best_ask': ask,
                'spread_pct': round(spread_pct, 3) if spread_pct is not None else None,
                'oi': _fnum(r.get('oi'), 0),
                'oi_value_usd': _fnum(r.get('oi_value_usd'), 0),
                'volume': _fnum(r.get('volume'), 0),
                'turnover_usd': _fnum(r.get('turnover_usd'), 0),
            }

            # Har filter ka reason rakhte hain taaki UI bata sake kyun reject hua.
            reasons = []
            if not (min_delta <= entry['abs_delta'] <= max_delta):
                reasons.append('delta out of band')
            if hours_left is not None and hours_left < min_hours:
                reasons.append('expiry too close')
            if spread_pct is None:
                reasons.append('no two-sided quote')
            elif spread_pct > max_spread_pct:
                reasons.append('spread too wide')
            if entry['oi'] < min_oi:
                reasons.append('open interest too low')

            entry['rejected_for'] = reasons
            candidates.append(entry)

        eligible = [c for c in candidates if not c['rejected_for']]
        # Sabse liquid pehle — doc ka filter "high option volume + tight spread" hai.
        eligible.sort(key=lambda c: (c['turnover_usd'], -(c['spread_pct'] or 999)), reverse=True)

        in_band = [c for c in candidates if 'delta out of band' not in c['rejected_for']]
        in_band.sort(key=lambda c: c['turnover_usd'], reverse=True)

        return jsonify({
            'success': True,
            'underlying': underlying,
            'option_type': option_type,
            'delta_band': [min_delta, max_delta],
            'filters': {
                'max_spread_pct': max_spread_pct,
                'min_oi': min_oi,
                'min_hours_to_expiry': min_hours,
            },
            'selected': eligible[0] if eligible else None,
            'candidates': eligible[:12],
            # Band mein the par filter par atke — UI inhe "kyun nahi liya" dikha sakta hai.
            'rejected': [c for c in in_band if c['rejected_for']][:12],
            'total_scanned': len(candidates),
        })
    except Exception as e:
        print(f"❌ Options chain error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def _option_entry(row, now):
    """Ticker row -> normalised contract dict (chain aur spread dono use karte hain)."""
    greeks = row.get('greeks') or {}
    delta = _fnum(greeks.get('delta'))
    if delta is None:
        return None
    expiry = _parse_option_expiry(str(row.get('symbol', '')))
    hours_left = (expiry - now).total_seconds() / 3600 if expiry else None
    quotes = row.get('quotes') or {}
    bid = _fnum(quotes.get('best_bid'))
    ask = _fnum(quotes.get('best_ask'))
    mid = (bid + ask) / 2 if bid and ask else None
    spread_pct = ((ask - bid) / mid * 100) if (mid and mid > 0) else None
    return {
        'symbol': row.get('symbol'),
        'strike': _fnum(row.get('strike_price')),
        'spot': _fnum(greeks.get('spot')) or _fnum(row.get('spot_price')),
        'expiry': expiry.isoformat() + 'Z' if expiry else None,
        'expiry_key': str(row.get('symbol', '')).rsplit('-', 1)[-1],
        'hours_to_expiry': round(hours_left, 2) if hours_left is not None else None,
        'delta': round(delta, 4),
        'abs_delta': round(abs(delta), 4),
        'theta': _fnum(greeks.get('theta')),
        'vega': _fnum(greeks.get('vega')),
        'gamma': _fnum(greeks.get('gamma')),
        'iv': _fnum(quotes.get('mark_iv')) or _fnum(row.get('mark_vol')),
        'premium': _fnum(row.get('mark_price')),
        'best_bid': bid,
        'best_ask': ask,
        'spread_pct': round(spread_pct, 3) if spread_pct is not None else None,
        'oi': _fnum(row.get('oi'), 0),
        'volume': _fnum(row.get('volume'), 0),
        'turnover_usd': _fnum(row.get('turnover_usd'), 0),
    }


@app.route('/api/options/spread', methods=['GET'])
def options_spread():
    """
    Debit spread (Strategy B) ke liye do legs chunta hai.

    Bull call: near-the-money call BUY + usse `width` upar wali call SELL.
    Bear put:  near-the-money put BUY  + usse `width` neeche wali put SELL.

    Dono legs ek hi expiry ke hone chahiye, warna wo spread hai hi nahi. Debit
    marketable prices se nikalta hai (long ka ask do, short ka bid lo) kyunki
    asli mein wahi bharna padta hai — mark-based number optimistic hota hai.
    """
    try:
        underlying = (request.args.get('underlying') or 'BTC').upper()
        option_type = (request.args.get('option_type') or 'call').lower()
        if option_type not in ('call', 'put'):
            return jsonify({'success': False, 'error': "option_type must be 'call' or 'put'"}), 400

        width = float(request.args.get('width', 2000))
        if width <= 0:
            return jsonify({'success': False, 'error': 'width must be greater than 0'}), 400
        # Long leg near-the-money hota hai; doc ka example spot par hi buy karta hai.
        long_delta = abs(float(request.args.get('long_delta', 0.5)))
        max_spread_pct = float(request.args.get('max_spread_pct', 8))
        min_hours = float(request.args.get('min_hours_to_expiry', 4))
        min_oi = float(request.args.get('min_oi', 0))

        rows = _fetch_option_tickers(option_type)
        now = datetime.utcnow()

        by_expiry = {}
        for r in rows:
            if str(r.get('underlying_asset_symbol', '')).upper() != underlying:
                continue
            entry = _option_entry(r, now)
            if not entry or entry['strike'] is None:
                continue
            if entry['hours_to_expiry'] is None or entry['hours_to_expiry'] < min_hours:
                continue
            by_expiry.setdefault(entry['expiry_key'], []).append(entry)

        def tradable(c):
            return (
                c['best_bid'] and c['best_ask']
                and c['spread_pct'] is not None and c['spread_pct'] <= max_spread_pct
                and c['oi'] >= min_oi
            )

        spreads = []
        for expiry_key, legs in by_expiry.items():
            usable = [c for c in legs if tradable(c)]
            if len(usable) < 2:
                continue

            # Long leg: target delta ke sabse kareeb.
            long_leg = min(usable, key=lambda c: abs(c['abs_delta'] - long_delta))
            target_strike = long_leg['strike'] + width if option_type == 'call' else long_leg['strike'] - width
            # Short leg: sahi taraf ka, target strike ke sabse kareeb.
            if option_type == 'call':
                side = [c for c in usable if c['strike'] > long_leg['strike']]
            else:
                side = [c for c in usable if c['strike'] < long_leg['strike']]
            if not side:
                continue
            short_leg = min(side, key=lambda c: abs(c['strike'] - target_strike))
            actual_width = abs(short_leg['strike'] - long_leg['strike'])
            if actual_width <= 0:
                continue

            # Marketable: long ka ask bharo, short ka bid milta hai.
            debit = long_leg['best_ask'] - short_leg['best_bid']
            debit_mark = (long_leg['premium'] or 0) - (short_leg['premium'] or 0)
            if debit <= 0:
                continue
            max_profit = actual_width - debit
            if max_profit <= 0:
                continue

            breakeven = (
                long_leg['strike'] + debit if option_type == 'call'
                else long_leg['strike'] - debit
            )

            def net(field):
                a = long_leg.get(field)
                b = short_leg.get(field)
                return round(a - b, 6) if a is not None and b is not None else None

            spreads.append({
                'expiry': long_leg['expiry'],
                'hours_to_expiry': long_leg['hours_to_expiry'],
                'spot': long_leg['spot'],
                'long_leg': long_leg,
                'short_leg': short_leg,
                'width': actual_width,
                'requested_width': width,
                'net_debit': round(debit, 4),
                'net_debit_mark': round(debit_mark, 4),
                'max_loss': round(debit, 4),
                'max_profit': round(max_profit, 4),
                'risk_reward': round(max_profit / debit, 3) if debit else None,
                'breakeven': round(breakeven, 2),
                # Spread ke net greeks — short leg long ka theta kaafi kaat deta hai,
                # yahi debit spread ka naked buying par sabse bada fayda hai.
                'net_delta': net('delta'),
                'net_theta': net('theta'),
                'net_vega': net('vega'),
                'net_gamma': net('gamma'),
                'liquidity_usd': round((long_leg['turnover_usd'] or 0) + (short_leg['turnover_usd'] or 0), 2),
            })

        # Behtar risk/reward pehle, phir liquidity.
        spreads.sort(key=lambda s: ((s['risk_reward'] or 0), s['liquidity_usd']), reverse=True)

        return jsonify({
            'success': True,
            'underlying': underlying,
            'option_type': option_type,
            'strategy': 'bull_call_spread' if option_type == 'call' else 'bear_put_spread',
            'requested_width': width,
            'long_delta_target': long_delta,
            'selected': spreads[0] if spreads else None,
            'alternatives': spreads[1:6],
            'expiries_scanned': len(by_expiry),
        })
    except Exception as e:
        print(f"❌ Options spread error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/options/condor', methods=['GET'])
def options_condor():
    """
    Iron Condor (Strategy C) ke chaar legs.

    SELL call + BUY usse door call (upar ka wing), SELL put + BUY usse door put
    (neeche ka wing) — sab ek hi expiry mein. Short legs ~0.15-0.20 delta par,
    long protection ~0.05-0.10 par, jaisa spec kehta hai.

    Credit marketable prices se: shorts ka bid milta hai, longs ka ask bharna
    padta hai. Max loss = bada wing - credit (dono wings ek saath hit nahi hote).
    """
    try:
        underlying = (request.args.get('underlying') or 'BTC').upper()
        short_delta = abs(float(request.args.get('short_delta', 0.175)))
        long_delta = abs(float(request.args.get('long_delta', 0.075)))
        if long_delta >= short_delta:
            return jsonify({'success': False, 'error': 'long_delta must be smaller than short_delta'}), 400

        max_spread_pct = float(request.args.get('max_spread_pct', 10))
        min_hours = float(request.args.get('min_hours_to_expiry', 12))
        min_oi = float(request.args.get('min_oi', 0))

        now = datetime.utcnow()
        by_expiry = {}
        for opt_type in ('call', 'put'):
            for r in _fetch_option_tickers(opt_type):
                if str(r.get('underlying_asset_symbol', '')).upper() != underlying:
                    continue
                entry = _option_entry(r, now)
                if not entry or entry['strike'] is None:
                    continue
                if entry['hours_to_expiry'] is None or entry['hours_to_expiry'] < min_hours:
                    continue
                if not (entry['best_bid'] and entry['best_ask']):
                    continue
                if entry['spread_pct'] is None or entry['spread_pct'] > max_spread_pct:
                    continue
                if entry['oi'] < min_oi:
                    continue
                entry['option_type'] = opt_type
                by_expiry.setdefault(entry['expiry_key'], {'call': [], 'put': []})[opt_type].append(entry)

        condors = []
        for expiry_key, legs in by_expiry.items():
            calls, puts = legs['call'], legs['put']
            if len(calls) < 2 or len(puts) < 2:
                continue

            def nearest(items, target):
                return min(items, key=lambda c: abs(c['abs_delta'] - target))

            short_call = nearest(calls, short_delta)
            short_put = nearest(puts, short_delta)
            # Protection short strike se aur door honi chahiye, warna wing hi nahi banta.
            outer_calls = [c for c in calls if c['strike'] > short_call['strike']]
            outer_puts = [p for p in puts if p['strike'] < short_put['strike']]
            if not outer_calls or not outer_puts:
                continue
            long_call = nearest(outer_calls, long_delta)
            long_put = nearest(outer_puts, long_delta)

            # Short call short put ke upar hona chahiye, warna ye condor nahi hai.
            if short_call['strike'] <= short_put['strike']:
                continue

            call_wing = long_call['strike'] - short_call['strike']
            put_wing = short_put['strike'] - long_put['strike']
            if call_wing <= 0 or put_wing <= 0:
                continue

            credit = (
                short_call['best_bid'] + short_put['best_bid']
                - long_call['best_ask'] - long_put['best_ask']
            )
            credit_mark = (
                (short_call['premium'] or 0) + (short_put['premium'] or 0)
                - (long_call['premium'] or 0) - (long_put['premium'] or 0)
            )
            if credit <= 0:
                continue

            # Dono wings ek saath test nahi hote — max loss bade wing par bandhta hai.
            max_loss = max(call_wing, put_wing) - credit
            if max_loss <= 0:
                continue

            def net(field):
                vals = [
                    short_call.get(field), short_put.get(field),
                    long_call.get(field), long_put.get(field),
                ]
                if any(v is None for v in vals):
                    return None
                # Shorts negative, longs positive.
                return round(-vals[0] - vals[1] + vals[2] + vals[3], 6)

            condors.append({
                'expiry': short_call['expiry'],
                'hours_to_expiry': short_call['hours_to_expiry'],
                'spot': short_call['spot'],
                'short_call': short_call,
                'long_call': long_call,
                'short_put': short_put,
                'long_put': long_put,
                'call_wing': call_wing,
                'put_wing': put_wing,
                'net_credit': round(credit, 4),
                'net_credit_mark': round(credit_mark, 4),
                'max_profit': round(credit, 4),
                'max_loss': round(max_loss, 4),
                'risk_reward': round(credit / max_loss, 3) if max_loss else None,
                'breakeven_low': round(short_put['strike'] - credit, 2),
                'breakeven_high': round(short_call['strike'] + credit, 2),
                'profit_zone_low': short_put['strike'],
                'profit_zone_high': short_call['strike'],
                'net_delta': net('delta'),
                'net_theta': net('theta'),
                'net_vega': net('vega'),
                'net_gamma': net('gamma'),
                'liquidity_usd': round(sum(
                    (c['turnover_usd'] or 0) for c in (short_call, long_call, short_put, long_put)
                ), 2),
            })

        # Credit-to-risk pehle, phir liquidity.
        condors.sort(key=lambda c: ((c['risk_reward'] or 0), c['liquidity_usd']), reverse=True)

        return jsonify({
            'success': True,
            'underlying': underlying,
            'short_delta_target': short_delta,
            'long_delta_target': long_delta,
            'selected': condors[0] if condors else None,
            'alternatives': condors[1:6],
            'expiries_scanned': len(by_expiry),
        })
    except Exception as e:
        print(f"❌ Iron condor error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def _max_pain(strikes_oi):
    """
    Max pain — wo strike jahan expiry par option kharidne walon ki total value
    sabse kam bachti hai (writers ka nuksaan sabse kam).

    Har candidate strike K par: us se neeche wale saare calls in-the-money honge
    aur upar wale saare puts. Dono ka total intrinsic value jodte hain, aur jahan
    ye sabse kam ho wahi max pain hai. Traders ise expiry ke aas-paas dekhte hain
    — ye koi niyam nahi, sirf ek widely-watched level hai.
    """
    if not strikes_oi:
        return None
    best_strike, best_value = None, None
    for k in strikes_oi:
        total = 0.0
        for strike, data in strikes_oi.items():
            if strike < k:
                total += (k - strike) * data['call_oi']
            elif strike > k:
                total += (strike - k) * data['put_oi']
        if best_value is None or total < best_value:
            best_strike, best_value = k, total
    return {'strike': best_strike, 'value': round(best_value, 2)}


@app.route('/api/options/analytics', methods=['GET'])
def options_analytics():
    """
    Poore option chain ka aggregate view — IV smile, term structure, open interest
    distribution, put/call ratio aur max pain.

    Ye batata hai ki market khud kya soch raha hai: dar kis taraf hai (skew),
    paisa kis strike par pada hai (OI), aur premium mehnga hai ya sasta (IV).
    Iske bina Iron Condor bechna ya OTM buy karna andaaza hi rehta hai.
    """
    try:
        underlying = (request.args.get('underlying') or 'BTC').upper()
        now = datetime.utcnow()

        legs = {'call': [], 'put': []}
        for opt_type in ('call', 'put'):
            for r in _fetch_option_tickers(opt_type):
                if str(r.get('underlying_asset_symbol', '')).upper() != underlying:
                    continue
                entry = _option_entry(r, now)
                if not entry or entry['strike'] is None or entry['hours_to_expiry'] is None:
                    continue
                if entry['hours_to_expiry'] <= 0:
                    continue
                legs[opt_type].append(entry)

        if not legs['call'] and not legs['put']:
            return jsonify({'success': False, 'error': f'No live options for {underlying}'}), 404

        spot = next((c['spot'] for c in legs['call'] + legs['put'] if c['spot']), None)

        # ── Expiry ke hisaab se todo ───────────────────────
        expiries = {}
        for opt_type, items in legs.items():
            for c in items:
                key = c['expiry_key']
                slot = expiries.setdefault(key, {
                    'expiry_key': key,
                    'expiry': c['expiry'],
                    'hours_to_expiry': c['hours_to_expiry'],
                    'strikes': {},
                    'call_oi': 0.0, 'put_oi': 0.0,
                    'call_volume': 0.0, 'put_volume': 0.0,
                })
                strike = slot['strikes'].setdefault(c['strike'], {
                    'strike': c['strike'],
                    'call_iv': None, 'put_iv': None,
                    'call_oi': 0.0, 'put_oi': 0.0,
                })
                strike[f'{opt_type}_iv'] = c['iv']
                strike[f'{opt_type}_oi'] += c['oi'] or 0
                slot[f'{opt_type}_oi'] += c['oi'] or 0
                slot[f'{opt_type}_volume'] += c['volume'] or 0

        chains = []
        for key, slot in expiries.items():
            strikes = dict(sorted(slot['strikes'].items()))
            # ATM = spot ke sabse kareeb ka strike; uski IV hi "market ka dar" hai.
            atm_strike = min(strikes, key=lambda k: abs(k - spot)) if (strikes and spot) else None
            atm = strikes.get(atm_strike) if atm_strike else None
            atm_ivs = [v for v in ((atm or {}).get('call_iv'), (atm or {}).get('put_iv')) if v]
            atm_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else None

            chains.append({
                'expiry_key': key,
                'expiry': slot['expiry'],
                'hours_to_expiry': slot['hours_to_expiry'],
                'atm_strike': atm_strike,
                'atm_iv': atm_iv,
                'call_oi': round(slot['call_oi'], 2),
                'put_oi': round(slot['put_oi'], 2),
                'call_volume': round(slot['call_volume'], 2),
                'put_volume': round(slot['put_volume'], 2),
                'pcr_oi': round(slot['put_oi'] / slot['call_oi'], 3) if slot['call_oi'] else None,
                'pcr_volume': round(slot['put_volume'] / slot['call_volume'], 3) if slot['call_volume'] else None,
                'max_pain': _max_pain(strikes),
                'strikes': list(strikes.values()),
            })

        chains.sort(key=lambda c: c['hours_to_expiry'])

        total_call_oi = sum(c['call_oi'] for c in chains)
        total_put_oi = sum(c['put_oi'] for c in chains)
        total_call_vol = sum(c['call_volume'] for c in chains)
        total_put_vol = sum(c['put_volume'] for c in chains)

        return jsonify({
            'success': True,
            'underlying': underlying,
            'spot': spot,
            'totals': {
                'call_oi': round(total_call_oi, 2),
                'put_oi': round(total_put_oi, 2),
                'pcr_oi': round(total_put_oi / total_call_oi, 3) if total_call_oi else None,
                'call_volume': round(total_call_vol, 2),
                'put_volume': round(total_put_vol, 2),
                'pcr_volume': round(total_put_vol / total_call_vol, 3) if total_call_vol else None,
                'contracts': len(legs['call']) + len(legs['put']),
            },
            # Term structure: har expiry ki ATM IV — aage ka dar vs abhi ka.
            'term_structure': [
                {'expiry_key': c['expiry_key'], 'hours_to_expiry': c['hours_to_expiry'], 'atm_iv': c['atm_iv']}
                for c in chains
            ],
            'chains': chains,
        })
    except Exception as e:
        print(f"❌ Options analytics error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/funding', methods=['GET'])
def funding_rates():
    """
    Perpetuals ka funding rate — har kuch ghante ek side doosre ko fees deti hai.
    Positive matlab long walon ko dena pad raha hai (bheed upar ke daaon par),
    negative matlab ulta. Ye crowd positioning ka seedha signal hai.
    """
    try:
        raw = (request.args.get('symbols') or '').strip()
        wanted = {to_delta_symbol(s.strip()) for s in raw.split(',') if s.strip()} if raw else None

        res = requests.get(
            f"{BASE_URL}/v2/tickers",
            params={'contract_types': 'perpetual_futures'},
            timeout=30,
        )
        res.raise_for_status()

        rows = []
        for r in res.json().get('result') or []:
            symbol = str(r.get('symbol') or '')
            if wanted is not None and symbol not in wanted:
                continue
            if wanted is None and not symbol.endswith('USD'):
                continue
            rows.append({
                'symbol': symbol,
                'mark_price': _fnum(r.get('mark_price')),
                # Delta percent mein deta hai (0.01 = 0.01%).
                'funding_rate': _fnum(r.get('funding_rate')),
                'mark_basis': _fnum(r.get('mark_basis')),
                'oi_value_usd': _fnum(r.get('oi_value_usd'), 0),
                'turnover_usd': _fnum(r.get('turnover_usd'), 0),
                'change_24h': _fnum(r.get('mark_change_24h')),
            })

        rows.sort(key=lambda x: abs(x['funding_rate'] or 0), reverse=True)
        return jsonify({'success': True, 'count': len(rows), 'rates': rows[:60]})
    except Exception as e:
        print(f"❌ Funding error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def _market_info_from_candles(symbol, exchange='delta'):
    """
    Ticker na mile to fallback — poore 24 ghante ki candles se stats banao.

    Ye 24 x 1h candles maangta hai, ek 1m candle nahi. Purana code ek hi 1m
    candle par "24h high/low/volume" bana raha tha, isliye range ek minute ki
    hoti thi aur change hamesha 0.
    """
    src = get_market_source(exchange) or get_market_source('delta')
    if src is None:
        return None
    rows = src.candles(symbol, '1h', 24)
    if not rows:
        return None

    closes = [c['close'] for c in rows if c.get('close')]
    highs = [c['high'] for c in rows if c.get('high')]
    lows = [c['low'] for c in rows if c.get('low')]
    vols = [c.get('volume') or 0 for c in rows]
    if not closes or not highs or not lows:
        return None

    first_open = float(rows[0].get('open') or closes[0])
    close = float(closes[-1])

    return {
        'success': True,
        'symbol': symbol,
        'exchange': src.id,
        'exchange_name': src.name,
        'source': 'candles',
        'current_price': close,
        'high_24h': float(max(highs)),
        'low_24h': float(min(lows)),
        'volume_24h': float(sum(vols)),
        'turnover_24h': None,
        'mark_price': None,
        'change_24h': ((close - first_open) / first_open * 100) if first_open else 0.0,
    }


@app.route('/api/market-info', methods=['GET'])
def get_market_info():
    """
    24 ghante ke market stats — selected exchange ke ticker se.

    Exchange khud rolling-24h stats maintain karta hai, isliye pehle wahi;
    candles sirf fallback hain.
    """
    symbol = request.args.get('symbol', 'BTCUSDT')
    exchange = (request.args.get('exchange') or 'delta').strip().lower()
    src = get_market_source(exchange)
    if src is None:
        return jsonify({
            'error': f'Chart source "{exchange}" support nahi hai.',
            'success': False,
        }), 400

    try:
        t = src.ticker(symbol)
        if t and t.get('price') and t.get('high_24h') and t.get('low_24h'):
            return jsonify({
                'success': True,
                'symbol': symbol,
                'exchange': src.id,
                'exchange_name': src.name,
                'source': 'ticker',
                'current_price': t['price'],
                'high_24h': t['high_24h'],
                'low_24h': t['low_24h'],
                'volume_24h': t.get('volume_24h') or 0.0,
                'turnover_24h': t.get('turnover_24h'),
                'mark_price': t.get('mark_price'),
                'change_24h': t.get('change_24h') or 0.0,
            })
        print(f"⚠️ Market info: {src.name}/{symbol} ka ticker adhoora aaya, candles par ja rahe hain")
    except Exception as e:
        print(f"⚠️ Market info ticker fail ({src.name}/{symbol}): {e} — candles par ja rahe hain")

    try:
        fallback = _market_info_from_candles(symbol, exchange=src.id)
        if fallback:
            return jsonify(fallback)
        return jsonify({'error': 'Data fetch nahi hua', 'success': False, 'exchange': src.id}), 400
    except Exception as e:
        print(f"❌ Market info error ({src.name}/{symbol}): {e}")
        return jsonify({'error': str(e), 'success': False}), 500


# Default credentials (demo mode)


@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    """Create a real user account for BYOK trading."""
    if not DB_READY:
        return jsonify({'success': False, 'error': 'Database unavailable'}), 503
    try:
        payload = request.get_json(silent=True) or {}
        username = (payload.get('username') or '').strip()
        password = payload.get('password') or ''
        email = (payload.get('email') or '').strip().lower()
        full_name = (payload.get('full_name') or '').strip()

        # Desk ka sign-up form naam + email + password maangta hai, username
        # nahi. Username na aaye to email se ek unique bana lo.
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return jsonify({'success': False, 'error': 'Valid email address daalein'}), 400
        if not username:
            if not email:
                return jsonify({'success': False, 'error': 'Email ya username zaroori hai'}), 400
            username = make_unique_username(email.split('@', 1)[0])

        if not validate_username(username):
            return jsonify({
                'success': False,
                'error': 'Username must be 3-32 chars: letters, numbers, _ . -'
            }), 400
        if not validate_password(password):
            return jsonify({
                'success': False,
                'error': 'Password must be at least 8 characters'
            }), 400
        if get_user_account_by_username(username):
            return jsonify({'success': False, 'error': 'Username already exists'}), 409
        # Email se login hota hai, isliye ek email ek hi account.
        if email and get_user_account_by_email(email):
            return jsonify({'success': False, 'error': 'Is email se account pehle se bana hai — login karein'}), 409

        password_hash = hash_password(password)
        created_user = create_user_account(username, password_hash, email=email, full_name=full_name)

        session_token = secrets.token_urlsafe(48)
        expires_at = datetime.now() + timedelta(hours=SESSION_TTL_HOURS)
        create_user_session(
            user_id=created_user['id'],
            token=session_token,
            expires_at=expires_at,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent', ''),
        )

        return jsonify({
            'success': True,
            'message': 'User registered successfully',
            'token': session_token,
            'expires_at': expires_at.isoformat(),
            'user': {
                'id': created_user['id'],
                'username': created_user['username'],
                'full_name': created_user.get('full_name', ''),
                'email': created_user.get('email', ''),
                'email_verified': created_user.get('email_verified', False),
                'created_at': created_user.get('created_at'),
            }
        }), 201
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    """Login for BYOK-authenticated APIs."""
    if not DB_READY:
        return jsonify({'success': False, 'error': 'Database unavailable'}), 503
    try:
        payload = request.get_json(silent=True) or {}
        identifier = (payload.get('username') or payload.get('email') or '').strip()
        password = payload.get('password') or ''

        # Username ya email — dono se login. '@' ho to email maano.
        user = (
            get_user_account_by_email(identifier)
            if '@' in identifier
            else get_user_account_by_username(identifier)
        )
        if not user:
            return jsonify({'success': False, 'error': 'Invalid username or password'}), 401
        if not user.get('is_active', False):
            return jsonify({'success': False, 'error': 'User disabled'}), 401
        if not check_password_hash(user.get('password_hash', ''), password):
            return jsonify({'success': False, 'error': 'Invalid username or password'}), 401

        session_token = secrets.token_urlsafe(48)
        expires_at = datetime.now() + timedelta(hours=SESSION_TTL_HOURS)
        create_user_session(
            user_id=user['id'],
            token=session_token,
            expires_at=expires_at,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent', ''),
        )

        return jsonify({
            'success': True,
            'message': 'Login successful',
            'token': session_token,
            'expires_at': expires_at.isoformat(),
            'user': {
                'id': user['id'],
                'username': user['username'],
                'full_name': user.get('full_name', ''),
                'email': user.get('email', ''),
                'email_verified': user.get('email_verified', False),
                # Login ke turant baad bhi profile "member since" dikha sake.
                'created_at': user.get('created_at'),
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500



@app.route('/api/auth/me', methods=['GET'])
@require_auth
def auth_me():
    return jsonify({
        'success': True,
        'user': {
            'id': g.user['id'],
            'username': g.user['username'],
            'full_name': g.user.get('full_name', ''),
            'email': g.user.get('email', ''),
            'email_verified': g.user.get('email_verified', False),
            # Profile page "member since" isse dikhata hai.
            'created_at': g.user.get('created_at'),
        },
        'session': {
            'id': g.session.get('id'),
            'expires_at': g.session.get('expires_at'),
        }
    })


@app.route('/api/profile', methods=['GET'])
@require_auth
def get_profile():
    try:
        latest_delta = get_latest_exchange_account_for_user(g.user['id'], 'delta')
        status_key, status_label = map_api_connection_status(latest_delta)
        exchange_profile = {}
        wallet_snapshot = {}
        auth_proof = {"private_api_access": False, "last_auth_error": ""}
        fingerprint_masked = ""
        if latest_delta and latest_delta.get('id'):
            account_full = get_exchange_account_for_user(latest_delta['id'], g.user['id'])
            if account_full:
                (
                    exchange_profile,
                    wallet_snapshot,
                    auth_proof,
                    fingerprint_masked,
                ) = fetch_live_delta_metadata(account_full)
        delta_data = {
            'status': status_key,
            'status_label': status_label,
            'account_id': latest_delta.get('id') if latest_delta else None,
            'exchange': latest_delta.get('exchange') if latest_delta else 'delta',
            'label': latest_delta.get('label') if latest_delta else None,
            'key_hint': latest_delta.get('key_hint') if latest_delta else None,
            'is_active': bool(latest_delta.get('is_active')) if latest_delta else False,
            'can_trade': bool(latest_delta.get('can_trade')) if latest_delta else False,
            'can_withdraw': bool(latest_delta.get('can_withdraw')) if latest_delta else False,
            'permissions_verified': bool(latest_delta.get('permissions_verified')) if latest_delta else False,
            'last_error': latest_delta.get('last_error') if latest_delta else '',
            'last_verified_at': latest_delta.get('last_verified_at') if latest_delta else None,
            'created_at': latest_delta.get('created_at') if latest_delta else None,
            'updated_at': latest_delta.get('updated_at') if latest_delta else None,
            'api_key_fingerprint_masked': fingerprint_masked,
            'exchange_profile': exchange_profile,
            'wallet_snapshot': wallet_snapshot,
            'auth_proof': auth_proof,
        }
        return jsonify({
            'success': True,
            'data': {
                'user': {
                    'id': g.user['id'],
                    'username': g.user['username'],
                    'full_name': g.user.get('full_name', ''),
                    'email': g.user.get('email', ''),
                    'email_verified': g.user.get('email_verified', False),
                },
                'delta_api': delta_data,
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/profile/name', methods=['PATCH'])
@require_auth
def update_profile_name():
    try:
        payload = request.get_json(silent=True) or {}
        full_name = (payload.get('full_name') or '').strip()
        if not validate_full_name(full_name):
            return jsonify({'success': False, 'error': 'Name length must be 2 to 50 chars'}), 400
        update_user_account_fields(g.user['id'], full_name=full_name)
        g.user = get_user_account_by_id(g.user['id']) or g.user
        return jsonify({
            'success': True,
            'message': 'Name updated successfully',
            'data': {'full_name': g.user.get('full_name', full_name)}
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/profile/email/request-otp', methods=['POST'])
@require_auth
def request_email_change_otp():
    try:
        payload = request.get_json(silent=True) or {}
        new_email = (payload.get('new_email') or '').strip().lower()
        if not validate_email(new_email):
            return jsonify({'success': False, 'error': 'Invalid email format'}), 400
        existing = get_user_account_by_username(g.user['username']) or {}
        if (existing.get('email') or '').strip().lower() == new_email:
            return jsonify({'success': False, 'error': 'New email must be different'}), 400

        otp_code = f"{secrets.randbelow(1000000):06d}"
        expires_at = datetime.now() + timedelta(minutes=EMAIL_OTP_TTL_MINUTES)
        create_email_change_otp(
            user_id=g.user['id'],
            new_email=new_email,
            otp_code=otp_code,
            expires_at=expires_at,
        )

        sent, send_error = send_otp_email(new_email, otp_code, EMAIL_OTP_TTL_MINUTES)
        if not sent:
            print(f"❌ Email OTP send failed for {new_email}: {send_error}")
            if not EMAIL_OTP_DEBUG:
                return jsonify({
                    'success': False,
                    'error': 'OTP email send failed. Check SMTP configuration.',
                }), 500
        else:
            print(f"✅ Email OTP sent to {new_email}")

        resp = {
            'success': True,
            'message': f'OTP sent to {new_email}',
            'expires_at': expires_at.isoformat(),
            'delivery': 'email' if sent else 'debug_fallback',
        }
        if EMAIL_OTP_DEBUG:
            resp['debug_otp'] = otp_code
            if send_error and not sent:
                resp['debug_delivery_error'] = send_error[:300]
        return jsonify(resp)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/profile/email/verify-otp', methods=['POST'])
@require_auth
def verify_email_change():
    try:
        payload = request.get_json(silent=True) or {}
        new_email = (payload.get('new_email') or '').strip().lower()
        otp = (payload.get('otp') or '').strip()
        if not validate_email(new_email):
            return jsonify({'success': False, 'error': 'Invalid email format'}), 400
        if not re.fullmatch(r"\d{6}", otp):
            return jsonify({'success': False, 'error': 'OTP must be 6 digits'}), 400

        ok = verify_email_change_otp(g.user['id'], new_email, otp)
        if not ok:
            return jsonify({'success': False, 'error': 'Invalid or expired OTP'}), 400

        update_user_account_fields(g.user['id'], email=new_email, email_verified=True)
        g.user = get_user_account_by_id(g.user['id']) or g.user
        return jsonify({
            'success': True,
            'message': 'Email updated and verified successfully',
            'data': {
                'email': g.user.get('email', new_email),
                'email_verified': bool(g.user.get('email_verified', True)),
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/profile/password/change', methods=['POST'])
@require_auth
def change_profile_password():
    try:
        payload = request.get_json(silent=True) or {}
        current_password = payload.get('current_password') or ''
        new_password = payload.get('new_password') or ''
        if not current_password or not new_password:
            return jsonify({'success': False, 'error': 'current_password and new_password required'}), 400
        if not validate_password(new_password):
            return jsonify({'success': False, 'error': 'New password must be at least 8 chars'}), 400

        user_with_hash = get_user_account_by_username(g.user['username'])
        if not user_with_hash or not check_password_hash(user_with_hash.get('password_hash', ''), current_password):
            return jsonify({'success': False, 'error': 'Current password is incorrect'}), 401
        if check_password_hash(user_with_hash.get('password_hash', ''), new_password):
            return jsonify({'success': False, 'error': 'New password must be different'}), 400

        update_user_account_fields(
            g.user['id'],
            password_hash=hash_password(new_password),
        )
        return jsonify({'success': True, 'message': 'Password changed successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/profile/delta-api/status', methods=['GET'])
@require_auth
def profile_delta_api_status():
    try:
        latest_delta = get_latest_exchange_account_for_user(g.user['id'], 'delta')
        status_key, status_label = map_api_connection_status(latest_delta)
        exchange_profile = {}
        wallet_snapshot = {}
        auth_proof = {"private_api_access": False, "last_auth_error": ""}
        fingerprint_masked = ""
        if latest_delta and latest_delta.get('id'):
            account_full = get_exchange_account_for_user(latest_delta['id'], g.user['id'])
            if account_full:
                (
                    exchange_profile,
                    wallet_snapshot,
                    auth_proof,
                    fingerprint_masked,
                ) = fetch_live_delta_metadata(account_full)
        delta_data = {
            'status': status_key,
            'status_label': status_label,
            'account_id': latest_delta.get('id') if latest_delta else None,
            'exchange': latest_delta.get('exchange') if latest_delta else 'delta',
            'label': latest_delta.get('label') if latest_delta else None,
            'key_hint': latest_delta.get('key_hint') if latest_delta else None,
            'is_active': bool(latest_delta.get('is_active')) if latest_delta else False,
            'can_trade': bool(latest_delta.get('can_trade')) if latest_delta else False,
            'can_withdraw': bool(latest_delta.get('can_withdraw')) if latest_delta else False,
            'permissions_verified': bool(latest_delta.get('permissions_verified')) if latest_delta else False,
            'last_error': latest_delta.get('last_error') if latest_delta else '',
            'last_verified_at': latest_delta.get('last_verified_at') if latest_delta else None,
            'created_at': latest_delta.get('created_at') if latest_delta else None,
            'updated_at': latest_delta.get('updated_at') if latest_delta else None,
            'api_key_fingerprint_masked': fingerprint_masked,
            'exchange_profile': exchange_profile,
            'wallet_snapshot': wallet_snapshot,
            'auth_proof': auth_proof,
        }
        return jsonify({
            'success': True,
            'data': delta_data,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/profile/delta-api', methods=['POST'])
@require_auth
def profile_add_delta_api():
    """Add/update Delta API keys with trade-only validation constraints."""
    try:
        payload = request.get_json(silent=True) or {}
        api_key = (payload.get('api_key') or '').strip()
        secret_key = (payload.get('secret_key') or '').strip()
        label = (payload.get('label') or 'Delta Primary').strip()
        if not api_key or not secret_key:
            return jsonify({'success': False, 'error': 'api_key and secret_key are required'}), 400

        verify = validate_exchange_credentials('delta', api_key, secret_key)
        if not verify['success']:
            return jsonify({'success': False, 'error': verify.get('error') or 'Delta credentials invalid'}), 400
        if verify.get('can_withdraw', False):
            return jsonify({'success': False, 'error': 'Only Trade+Read permissions allowed. Withdrawal must be disabled.'}), 400
        if not verify.get('can_trade', False):
            return jsonify({'success': False, 'error': 'Trade permission is required'}), 400

        account_id = create_exchange_account(
            user_id=g.user['id'],
            exchange='delta',
            api_key_encrypted=encrypt_secret(api_key),
            secret_key_encrypted=encrypt_secret(secret_key),
            api_key_fingerprint=api_key_fingerprint('delta', api_key),
            label=label,
            key_hint=key_hint(api_key),
            can_trade=True,
            can_withdraw=False,
            permissions_verified=True,
            last_error='',
        )
        return jsonify({
            'success': True,
            'message': 'Delta API key added successfully',
            'data': {
                'account_id': account_id,
                'status': 'connected',
                'status_label': 'Connected ✅',
                'key_hint': key_hint(api_key),
            }
        }), 201
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/profile/delta-api', methods=['DELETE'])
@require_auth
def profile_delete_delta_api():
    try:
        payload = request.get_json(silent=True) or {}
        confirm = bool(payload.get('confirm', False))
        if not confirm:
            return jsonify({'success': False, 'error': 'Please confirm deletion by sending {"confirm": true}'}), 400

        latest_delta = get_latest_exchange_account_for_user(g.user['id'], 'delta')
        if not latest_delta:
            return jsonify({'success': False, 'error': 'No Delta API key found'}), 404

        deleted = delete_exchange_account_for_user(latest_delta['id'], g.user['id'])
        if not deleted:
            return jsonify({'success': False, 'error': 'Delete failed'}), 400
        return jsonify({
            'success': True,
            'message': 'Delta API key deleted successfully',
            'data': {'status': 'not_added', 'status_label': 'Not Added ⚪'}
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/auth/logout', methods=['POST'])
@require_auth
def auth_logout():
    deactivate_session(g.auth_token)
    return jsonify({'success': True, 'message': 'Logged out successfully'})


@app.route('/api/byok/exchange-accounts', methods=['POST'])
@require_auth
def byok_connect_exchange():
    """Connect user-owned exchange credentials (BYOK)."""
    try:
        payload = request.get_json(silent=True) or {}
        exchange = (payload.get('exchange') or '').strip().lower()
        api_key = (payload.get('api_key') or '').strip()
        secret_key = (payload.get('secret_key') or '').strip()
        label = ((payload.get('label') or 'Primary').strip() or 'Primary')[:40]

        if exchange not in SUPPORTED_EXCHANGES:
            return jsonify({'success': False, 'error': f'Unsupported exchange: {exchange}'}), 400
        if not api_key or not secret_key:
            return jsonify({'success': False, 'error': 'api_key and secret_key are required'}), 400

        verify = validate_exchange_credentials(exchange, api_key, secret_key)
        if not verify['success']:
            return jsonify({
                'success': False,
                'error': verify.get('error') or 'Credential verification failed',
                'reason': verify.get('reason'),
                'client_ip': verify.get('client_ip'),
                'exchange': exchange,
            }), 400

        fingerprint = api_key_fingerprint(exchange, api_key)
        existing = get_exchange_account_by_fingerprint(exchange, fingerprint)
        if existing and existing.get('user_id') != g.user['id']:
            # Ek hi key do accounts se nahi judni chahiye — warna ek user doosre
            # ke naam par trade kar sakta hai.
            return jsonify({
                'success': False,
                'error': 'Ye API key pehle se kisi aur account se judi hai.',
                'reason': 'key_in_use',
            }), 409

        status = dict(
            label=label,
            is_active=True,
            can_trade=verify.get('can_trade', False),
            can_withdraw=verify.get('can_withdraw', False),
            permissions_verified=verify.get('permissions_verified', False),
            last_error='',
            last_verified_at=datetime.now(timezone.utc),
        )
        if existing:
            # Wahi user wahi key dobara jod raha hai — naya record nahi, purana
            # taaza credentials ke saath wapas chalu.
            account_id = existing['id']
            update_exchange_account_credentials(
                account_id,
                g.user['id'],
                api_key_encrypted=encrypt_secret(api_key),
                secret_key_encrypted=encrypt_secret(secret_key),
                api_key_fingerprint=fingerprint,
                key_hint=key_hint(api_key),
            )
        else:
            account_id = create_exchange_account(
                user_id=g.user['id'],
                exchange=exchange,
                api_key_encrypted=encrypt_secret(api_key),
                secret_key_encrypted=encrypt_secret(secret_key),
                api_key_fingerprint=fingerprint,
                label=label,
                key_hint=key_hint(api_key),
            )
        update_exchange_account_status(account_id, g.user['id'], **status)
        # Nayi key par purana (kharab key wala) nateeja nahi dikhna chahiye.
        _invalidate_private_cache(account_id)

        return jsonify({
            'success': True,
            'message': 'Exchange account connected',
            'data': {
                'exchange_account_id': account_id,
                'exchange': exchange,
                'label': label,
                'key_hint': key_hint(api_key),
                'can_trade': verify.get('can_trade', False),
                'can_withdraw': verify.get('can_withdraw', False),
                'permissions_verified': verify.get('permissions_verified', False),
                'notice': 'Ensure withdrawal permission is disabled from exchange dashboard.',
            }
        }), 201
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/byok/exchange-accounts', methods=['GET'])
@require_auth
def byok_list_exchange_accounts():
    try:
        accounts = list_exchange_accounts_for_user(g.user['id'])
        return jsonify({'success': True, 'data': accounts})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/byok/exchanges', methods=['GET'])
def byok_exchanges():
    """
    Kaun se exchange jud sakte hain.

    UI ye list yahin se leta hai, apne andar hardcode nahi karta — warna naya
    adapter jodne par frontend purani list dikhata rehta, ya aisa exchange
    dikha deta jise backend accept hi nahi karta.
    """
    return jsonify({'success': True, 'data': catalogue()})


PNL_MAX_DAYS = 400


@app.route('/api/byok/pnl', methods=['GET'])
@require_auth
def byok_pnl():
    """
    Din-ba-din aur mahine-ba-mahine realized P&L, har jude exchange ka alag
    aur sabka milakar.

    Data exchange se aata hai, apne records se nahi — apne paas sirf wahi
    orders hain jo is desk se lage, aur user ne exchange par seedha bhi
    trade kiya ho sakta hai. Sach wahi hai jo exchange ke wallet mein likha
    hai.

    Jo exchange ye nahi de sakta uske liye `supported: false` jaata hai —
    "0 ka munafa" aur "pata nahi" ek baat nahi hai.
    """
    try:
        days = int(_fnum(request.args.get('days'), 30))
        days = max(1, min(days, PNL_MAX_DAYS))
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

        accounts = [a for a in list_exchange_accounts_for_user(g.user['id']) if a.get('is_active')]
        by_day, by_exchange = {}, []

        for row in accounts:
            account = get_exchange_account_for_user(row['id'], g.user['id'])
            if not account:
                continue
            adapter = get_adapter(
                account['exchange'],
                decrypt_secret(account['api_key_encrypted']),
                decrypt_secret(account['secret_key_encrypted']),
            )
            entry = {
                'account_id': account['id'],
                'exchange': account['exchange'],
                'label': account.get('label') or '',
                'name': exchange_name(account['exchange']),
                'pnl': 0.0,
                'supported': True,
                'error': '',
            }
            if adapter is None:
                entry.update(supported=False, error=message_for('adapter_missing', exchange=entry['name']))
                by_exchange.append(entry)
                continue

            rows = _cached_private(
                account['id'], f'pnl_{days}',
                lambda a=adapter: (a.pnl_history(start_ms, end_ms), a.error_message() if a.last_reason else ''),
            )
            history, err = rows if isinstance(rows, tuple) else (rows, '')
            if history is None:
                # None ke do matlab hain aur dono alag dikhane chahiye:
                # adapter ye de hi nahi sakta (supported=False), ya call fail
                # hui (error). Dono mein number nahi dikhana — "0 ka munafa"
                # aur "pata nahi chala" ek baat nahi hai.
                entry.update(supported=bool(err), error=err, pnl=None)
                by_exchange.append(entry)
                continue

            total = 0.0
            for item in history:
                amount = _fnum(item.get('amount'))
                day = item.get('date') or ''
                if not day:
                    continue
                by_day[day] = by_day.get(day, 0.0) + amount
                total += amount
            entry['pnl'] = round(total, 2)
            by_exchange.append(entry)

        # Khaali din bhi chart mein aane chahiye, warna bar chart jhooti
        # tasveer banata hai (do din ki doori ek jaisi nahi dikhti).
        series = []
        cursor = start.date()
        last = end.date()
        while cursor <= last:
            key = cursor.isoformat()
            series.append({'date': key, 'pnl': round(by_day.get(key, 0.0), 2)})
            cursor += timedelta(days=1)

        months = {}
        for item in series:
            months.setdefault(item['date'][:7], 0.0)
            months[item['date'][:7]] += item['pnl']

        traded = [d for d in series if d['pnl']]
        wins = [d for d in traded if d['pnl'] > 0]
        best = max(traded, key=lambda d: d['pnl'], default=None)
        worst = min(traded, key=lambda d: d['pnl'], default=None)

        return jsonify({
            'success': True,
            'data': {
                'days': days,
                'by_day': series,
                'by_month': [{'month': m, 'pnl': round(v, 2)} for m, v in sorted(months.items())],
                'by_exchange': by_exchange,
                'totals': {
                    'realized': round(sum(d['pnl'] for d in series), 2),
                    'traded_days': len(traded),
                    'win_days': len(wins),
                    'loss_days': len(traded) - len(wins),
                    'best_day': best,
                    'worst_day': worst,
                },
                'note': 'Realized P&L — fees aur funding jodkar. Deposit/withdrawal shaamil nahi.',
                'fetched_at': datetime.now(timezone.utc).isoformat(),
            },
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/byok/egress-ips', methods=['GET'])
@require_auth
def byok_egress_ips():
    """
    Is server ke outbound IP — exchange par API key ki IP allowlist mein yahi
    daalne padte hain.

    BYOK_EGRESS_IPS set ho to wahi poori list (hosting dashboard se li gayi).
    Warna hum apna dikhne wala IP khud detect karte hain: sahi hota hai, par
    server ek se zyada IP se bahar ja sakta hai, isliye tab list ko "adhoori"
    mark karte hain taaki UI IP restriction par zid na karaye.
    """
    configured = _configured_egress_ips()
    if configured:
        return jsonify({'success': True, 'data': {'ips': configured, 'source': 'configured', 'complete': True}})
    detected = _detect_egress_ip()
    return jsonify({
        'success': True,
        'data': {
            'ips': [detected] if detected else [],
            'source': 'detected' if detected else 'unknown',
            'complete': False,
        },
    })


@app.route('/api/byok/exchange-accounts/<int:account_id>/verify', methods=['POST'])
@require_auth
def byok_verify_exchange(account_id):
    try:
        _invalidate_private_cache(account_id)
        account = get_exchange_account_for_user(account_id, g.user['id'])
        if not account:
            return jsonify({'success': False, 'error': 'Exchange account not found'}), 404
        if not account.get('is_active'):
            return jsonify({'success': False, 'error': 'Exchange account is inactive'}), 400

        api_key = decrypt_secret(account['api_key_encrypted'])
        secret_key = decrypt_secret(account['secret_key_encrypted'])
        verify = validate_exchange_credentials(account['exchange'], api_key, secret_key)

        status_update = dict(
            can_trade=verify.get('can_trade', False),
            can_withdraw=verify.get('can_withdraw', False),
            permissions_verified=verify.get('permissions_verified', False),
            last_error=verify.get('error', ''),
        )
        if verify['success']:
            status_update['last_verified_at'] = datetime.now(timezone.utc)
        update_exchange_account_status(account_id, g.user['id'], **status_update)

        if not verify['success']:
            return jsonify({
                'success': False,
                'error': verify.get('error') or 'Credential verification failed',
                'reason': verify.get('reason'),
                'client_ip': verify.get('client_ip'),
            }), 400
        return jsonify({
            'success': True,
            'message': 'Exchange account verified',
            'data': {
                'exchange_account_id': account_id,
                'can_trade': verify.get('can_trade', False),
                'can_withdraw': verify.get('can_withdraw', False),
                'permissions_verified': verify.get('permissions_verified', False),
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/byok/exchange-accounts/<int:account_id>/revoke', methods=['POST'])
@require_auth
def byok_revoke_exchange(account_id):
    try:
        account = get_exchange_account_for_user(account_id, g.user['id'])
        if not account:
            return jsonify({'success': False, 'error': 'Exchange account not found'}), 404
        update_exchange_account_status(
            account_id,
            g.user['id'],
            is_active=False,
            can_trade=False,
            permissions_verified=False,
            last_error='revoked_by_user',
        )
        return jsonify({'success': True, 'message': 'Exchange account revoked'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/byok/exchange-accounts/<int:account_id>', methods=['DELETE'])
@require_auth
def byok_delete_exchange(account_id):
    _invalidate_private_cache(account_id)
    """
    Disconnect = encrypted key aur secret database se poori tarah hatao.

    `revoke` sirf account ko inactive karta tha aur credentials DB mein pade
    rehte the. User "disconnect" dabaye to uski trading key humare paas nahi
    rehni chahiye.
    """
    try:
        if not delete_exchange_account_for_user(account_id, g.user['id']):
            return jsonify({'success': False, 'error': 'Exchange account not found'}), 404
        return jsonify({'success': True, 'message': 'Exchange disconnected and credentials deleted'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


_STABLE_ASSETS = {'USD', 'USDT', 'USDC', 'DAI'}

# Private exchange calls ke liye ek hi pool. Pehle har request apna pool
# banata tha (thread create + destroy), jo zyada users par bekaar kharcha hai.
_PRIVATE_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix='byok')

# Desk har 10 second poll karta hai aur profile page bhi wahi data maangta
# hai. Bina cache ke ek user ke do tab hi exchange ka rate limit kha jaate.
# TTL itna chhota hai ki "live" ka matlab nahi badalta.
PRIVATE_CACHE_TTL = 5.0
_private_cache = {}
_private_cache_lock = threading.Lock()


def _cached_private(account_id, kind, fetch):
    """
    Ek account ke private call ka nateeja TTL tak yaad rakho.

    Key mein account_id hai, aur account ek hi user ka hota hai — isliye kisi
    doosre user ko ye data nahi mil sakta. Error bhi cache hota hai (warna
    kharab key har poll par exchange ko hit karti rehti), par TTL ke baad
    apne aap taaza ho jaata hai.
    """
    key = (account_id, kind)
    now = time.time()
    with _private_cache_lock:
        hit = _private_cache.get(key)
        if hit and (now - hit[0]) < PRIVATE_CACHE_TTL:
            return hit[1]
    value = fetch()
    with _private_cache_lock:
        _private_cache[key] = (time.time(), value)
        if len(_private_cache) > 5000:
            # Bahut purane entries hata do — cache memory leak na bane.
            cutoff = time.time() - PRIVATE_CACHE_TTL
            for k in [k for k, v in _private_cache.items() if v[0] < cutoff]:
                _private_cache.pop(k, None)
    return value


def _invalidate_private_cache(account_id):
    """Connect / re-verify / disconnect ke baad purana nateeja na dikhe."""
    with _private_cache_lock:
        for k in [k for k in _private_cache if k[0] == account_id]:
            _private_cache.pop(k, None)


def _primary_exchange_account(user_id):
    """
    User ka default exchange account — sabse haal mein update hua active.

    Desk ko iski zarurat hai taaki use pehle list fetch karke id dhoondhni na
    pade; ek hi account wale (yaani lagbhag sab) users ke liye ek call bachta hai.
    """
    for account in list_exchange_accounts_for_user(user_id):
        if account.get('is_active'):
            return get_exchange_account_for_user(account['id'], user_id)
    return None


@app.route('/api/byok/exchange-accounts/<int:account_id>/overview', methods=['GET'])
@require_auth
def byok_exchange_overview(account_id):
    """
    Profile page ka ek hi call: wallet, positions, open orders aur exchange ka
    apna profile.

    Har hissa alag try hota hai. Positions fail hone se wallet nahi chhupta —
    jo mila wo dikhta hai, jo nahi mila uska reason usi hisse par likha hota
    hai. Isi liye HTTP 200 rehta hai jab tak key khud kaam kar rahi ho.
    """
    try:
        account = get_exchange_account_for_user(account_id, g.user['id'])
        if not account:
            return jsonify({'success': False, 'error': 'Exchange account not found'}), 404
        if not account.get('is_active'):
            return jsonify({'success': False, 'error': 'Exchange account is inactive'}), 400

        exchange = account['exchange']
        api_key = decrypt_secret(account['api_key_encrypted'])
        secret_key = decrypt_secret(account['secret_key_encrypted'])
        if get_adapter(exchange, api_key, secret_key) is None:
            return jsonify({'success': False, 'error': 'Exchange adapter not available'}), 400

        def call(method_name):
            """
            Ek call, apne alag adapter par.

            Adapter `last_error` khud par rakhta hai — ek hi adapter par chaar
            parallel calls chalane se error kisi doosre section par chipak
            jaata. Isliye har call ka apna adapter, aur error usi ke saath wapas.
            """
            adapter = get_adapter(exchange, api_key, secret_key)
            try:
                data = getattr(adapter, method_name)()
            except Exception as exc:
                return None, str(exc)[:400], None
            if data is None:
                return None, adapter.error_message(), {'client_ip': adapter.last_client_ip}
            return data, '', None

        # Chaar private call — ek ke baad ek karne par ye page 1-2 second
        # baithta tha, isliye saath-saath (shared pool par).
        jobs = {
            name: _PRIVATE_POOL.submit(_cached_private, account_id, name, lambda n=name: call(n))
            for name in ('balances', 'positions', 'open_orders', 'profile')
        }
        # Adapter pehle se desk ke common shape mein deta hai — yahan kisi
        # exchange-specific normalizer ki zarurat nahi.
        balances, balances_error, auth_issue = jobs['balances'].result()
        positions, positions_error, _ = jobs['positions'].result()
        orders, orders_error, _ = jobs['open_orders'].result()
        profile_raw, _, _ = jobs['profile'].result()

        balances = balances or []
        positions = positions or []
        orders = orders or []
        exchange_profile = (
            extract_exchange_profile_snapshot(profile_raw) if isinstance(profile_raw, dict) and profile_raw else {}
        )

        # Key theek hai ya nahi — iska faisla wallet se hota hai, wahi sabse
        # seedha private call hai. Kharab ho to card par dikhne wala last_error
        # bhi taaza kar dete hain.
        if balances_error:
            update_exchange_account_status(account['id'], g.user['id'], last_error=balances_error[:400])
        elif account.get('last_error'):
            update_exchange_account_status(account['id'], g.user['id'], last_error='')

        cash = sum(b['balance'] for b in balances if b['asset'].upper() in _STABLE_ASSETS)
        cash_available = sum(b['available'] for b in balances if b['asset'].upper() in _STABLE_ASSETS)
        reported_pnl = [p['unrealized_pnl'] for p in positions if p['unrealized_pnl'] is not None]

        return jsonify({
            'success': True,
            'data': {
                'account': {
                    'id': account['id'],
                    'exchange': account['exchange'],
                    'label': account.get('label') or '',
                    'key_hint': account.get('key_hint') or '',
                    'can_trade': bool(account.get('can_trade')),
                    'permissions_verified': bool(account.get('permissions_verified')),
                    # Wallet call se abhi jo pata chala — page ka status pill
                    # isi se taaza rehta hai, purani list row se nahi.
                    'last_error': balances_error or '',
                    'last_verified_at': account.get('last_verified_at'),
                    'created_at': account.get('created_at'),
                },
                'balances': {'items': balances, 'error': balances_error},
                'positions': {'items': positions, 'error': positions_error},
                'orders': {'items': orders, 'error': orders_error},
                'exchange_profile': exchange_profile or None,
                'totals': {
                    'cash': cash,
                    'cash_available': cash_available,
                    'open_positions': len(positions),
                    'open_orders': len(orders),
                    'unrealized_pnl': sum(reported_pnl) if reported_pnl else None,
                    'realized_pnl': sum(p['realized_pnl'] for p in positions),
                },
                'client_ip': (auth_issue or {}).get('client_ip'),
                'fetched_at': datetime.now(timezone.utc).isoformat(),
            },
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/byok/exchange-accounts/<int:account_id>/balances', methods=['GET'])
@require_auth
def byok_exchange_balances(account_id):
    """Card par wallet balance — saboot ki key sach mein kaam kar rahi hai."""
    try:
        account = get_exchange_account_for_user(account_id, g.user['id'])
        if not account or not account.get('is_active'):
            return jsonify({'success': False, 'error': 'Exchange account not found'}), 404
        adapter = get_adapter(
            account['exchange'],
            decrypt_secret(account['api_key_encrypted']),
            decrypt_secret(account['secret_key_encrypted']),
        )
        if adapter is None:
            return jsonify({'success': False, 'error': 'Exchange not supported'}), 400
        rows = adapter.balances()
        if rows is None:
            return jsonify({
                'success': False,
                'error': adapter.error_message(),
                'reason': adapter.last_reason,
                'client_ip': adapter.last_client_ip,
            }), 400
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# Ek order ki upper limit (quote currency mein), jab user ne apni limit na
# rakhi ho. Ye jaanbujh kar chhoti hai: live trading mein galti ki keemat
# asli paisa hai, aur badhana user ke haath mein hai.
DEFAULT_MAX_ORDER_NOTIONAL = _fnum(os.getenv("LIVE_ORDER_MAX_NOTIONAL"), 500.0)
# Isse upar koi account nahi ja sakta, chahe user ne kitni bhi limit rakhi ho.
HARD_MAX_ORDER_NOTIONAL = _fnum(os.getenv("LIVE_ORDER_HARD_MAX_NOTIONAL"), 25000.0)


class OrderRejected(Exception):
    """Order exchange tak gaya hi nahi — reason user ko dikhane layak hai."""

    def __init__(self, message, status=400, reason=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.reason = reason


# Asli order tabhi exchange tak jaata hai jab ye env flag on ho. Abhi band
# hai: poora rasta (desk, webhook, saare guards) paper mode mein chalta aur
# test hota hai, aur execution ek alag, soch-samajh kar khola jaane wala
# switch hai. Iske bina mode="live" maanga bhi jaye to paper hi chalega.
LIVE_ORDERS_ENABLED = (os.getenv("LIVE_ORDERS_ENABLED", "").strip().lower() in ("1", "true", "yes"))


def prepare_order(*, account, payload, require_live_toggle=True):
    """
    Order ko jaanchta hai aur "kya bhejna hai" ka poora hisaab banata hai —
    par bhejta kuch nahi.

    Desk aur webhook dono isi se hokar jaate hain, taaki koi bhi guard sirf
    ek jagah lagana pade. Jaanch exchange par nahi chhodi jaati: hum khud
    rok dete hain, taaki galti exchange tak pahunche hi na.
    """
    if not account.get('is_active'):
        raise OrderRejected('Ye exchange account band hai.')
    if require_live_toggle and not account.get('live_trading_enabled'):
        raise OrderRejected(
            'Is account par live trading band hai. Exchanges page par ise chaalu karein.',
            reason='live_trading_disabled',
        )
    if not account.get('can_trade'):
        raise OrderRejected('Is key ko trading permission nahi hai.', reason='unauthorized')
    if account.get('can_withdraw'):
        # Aisi key se order lagana matlab ek hi key se paisa nikaalna bhi
        # mumkin hai — wo risk hum lete hi nahi.
        raise OrderRejected('Withdrawal-enabled key se order nahi lagta. Nayi key banayein.')

    symbol = str(payload.get('symbol') or '').strip().upper()
    side = str(payload.get('side') or '').strip().lower()
    order_type = str(payload.get('order_type') or 'limit').strip().lower()
    # Do naam chalte hain. `size` naya hai aur default **contracts** hai.
    # `quantity` purana desk ticket bhejta hai aur wahan wo **coins** mein hai
    # (ticket par likha hi "Quantity · BTC" hai), isliye uska default unit
    # base rakha gaya hai. Ye farak maayne rakhta hai: Delta par 0.001 BTC =
    # 1 contract, to galat unit maan lene par order 1000 guna galat hota.
    if payload.get('size') not in (None, ''):
        size = _fnum(payload.get('size'), 0.0)
        size_unit = str(payload.get('size_unit') or 'contracts').strip().lower()
    else:
        size = _fnum(payload.get('quantity'), 0.0)
        size_unit = str(payload.get('size_unit') or 'base').strip().lower()
    price = _fnum(payload.get('price'), 0.0) or None
    reduce_only = bool(payload.get('reduce_only'))

    if not symbol:
        raise OrderRejected('symbol chahiye.')
    if side not in ('buy', 'sell'):
        raise OrderRejected("side 'buy' ya 'sell' hona chahiye.")
    if order_type not in ('limit', 'market'):
        raise OrderRejected("order_type 'limit' ya 'market' hona chahiye.")
    if size <= 0:
        raise OrderRejected('size 0 se bada hona chahiye.')
    if order_type == 'limit' and not price:
        raise OrderRejected('Limit order ke liye price chahiye.')

    adapter = get_adapter(account['exchange'], decrypt_secret(account['api_key_encrypted']),
                          decrypt_secret(account['secret_key_encrypted']))
    if adapter is None:
        raise OrderRejected(message_for('adapter_missing', exchange=exchange_name(account['exchange'])))

    info = adapter.contract_info(symbol)
    if not info:
        raise OrderRejected(
            f'{adapter.name} par {symbol} ke liye contract size nahi mila — order nahi bhej sakte.',
            reason='contract_unknown',
        )

    # Coins ko contracts mein badlo. BTCUSD par 1 contract = 0.001 BTC, to
    # "0.01 BTC" = 10 contracts. Aadha contract nahi hota, isliye adhoora
    # number chup-chaap round karne ke bajaye mana kar dete hain — round
    # karne par user ko jo mila wo uske maange se alag hota.
    contract_value = _fnum(info.get('contract_value'), 0.0)
    if size_unit in ('base', 'coin', 'coins'):
        if not contract_value:
            raise OrderRejected('Contract size nahi mila, coins ko contracts mein nahi badal sakte.')
        exact = size / contract_value
        contracts = round(exact)
        if abs(exact - contracts) > 1e-9:
            step = contract_value
            raise OrderRejected(
                f'{symbol} par quantity {step} {info.get("unit") or ""} ke multiple mein honi chahiye '
                f'(1 contract = {step}).',
                reason='size_step',
            )
    else:
        contracts = round(size)
        if abs(size - contracts) > 1e-9:
            raise OrderRejected('Contracts poore number mein hone chahiye.', reason='size_step')
    if contracts < 1:
        raise OrderRejected('Itni chhoti quantity par ek bhi contract nahi banta.', reason='size_step')

    # Notional kis bhaav par — limit ka apna price, market ka abhi ka mark.
    reference = price if order_type == 'limit' else adapter.mark_price(symbol)
    if not reference:
        raise OrderRejected(
            f'{symbol} ka abhi ka bhaav nahi mila, isliye order ka size check nahi kar sakte.',
            reason='no_price',
        )
    notional = contracts * contract_value * reference

    account_cap = _fnum(account.get('max_order_notional'), 0.0) or DEFAULT_MAX_ORDER_NOTIONAL
    cap = min(account_cap, HARD_MAX_ORDER_NOTIONAL)
    if notional > cap:
        raise OrderRejected(
            f'Ye order lagbhag {notional:,.2f} ka hai, aur is account ki limit {cap:,.2f} hai. '
            f'Limit Exchanges page par badal sakte hain.',
            reason='notional_cap',
        )

    # Yahan tak aane ka matlab: order har jaanch paar kar chuka hai. Kya
    # bhejna hai wo poora tay hai — bhejna hai ya nahi, wo caller tay karta hai.
    return {
        'adapter': adapter,
        'symbol': symbol,
        'side': side,
        'order_type': order_type,
        'contracts': contracts,
        'price': price,
        'reduce_only': reduce_only,
        'client_order_id': payload.get('client_order_id'),
        'base_quantity': round(contracts * contract_value, 10),
        'base_unit': info.get('unit') or '',
        'reference_price': reference,
        'notional': round(notional, 2),
        'cap': cap,
    }


def submit_order(*, user, account, payload, source, mode='paper'):
    """
    Jaanche hue order ko aage badhata hai.

    `mode="paper"` par exchange ko chhua tak nahi jaata — sirf record banta
    hai. Poora rasta (desk, webhook, saare guards, contract ka hisaab) isi
    mode mein chalta aur test hota hai.

    `mode="live"` tabhi sach mein live hai jab `LIVE_ORDERS_ENABLED` bhi on
    ho. Ye switch jaanbujh kar env mein hai, code mein nahi: asli paise wala
    execution ek alag, soch-samajh kar liya gaya faisla hona chahiye — koi
    aisi cheez nahi jo galti se on ho jaye.
    """
    live = mode == 'live' and LIVE_ORDERS_ENABLED
    plan = prepare_order(account=account, payload=payload, require_live_toggle=live)
    adapter = plan.pop('adapter')

    if live:
        result = adapter.place_order(
            symbol=plan['symbol'],
            side=plan['side'],
            order_type=plan['order_type'],
            contracts=plan['contracts'],
            price=plan['price'],
            reduce_only=plan['reduce_only'],
            client_order_id=plan['client_order_id'],
        )
        if result is None:
            err = adapter.error_message()
            update_exchange_account_status(account['id'], user['id'], last_error=err[:400])
            raise OrderRejected(err, reason=adapter.last_reason)
        order_id, status = result.get('order_id') or '', result.get('state') or 'open'
    else:
        result = {}
        order_id = f"paper_{int(time.time() * 1000)}"
        status = 'paper'

    record = {
        'user_id': user['id'],
        'exchange_account_id': account['id'],
        'order_id': order_id,
        'symbol': plan['symbol'],
        'side': plan['side'],
        'order_type': plan['order_type'],
        'quantity': plan['contracts'],
        'price': plan['price'],
        'status': status,
        'source': source,
        'timestamp': datetime.now(timezone.utc),
        'exchange_response': json.dumps(result)[:5000],
    }
    try:
        save_byok_order_entry(record)
    except Exception as exc:
        # Live mode mein order exchange par lag chuka hota hai — record fail
        # hone par use "fail" nahi keh sakte, warna user dobara laga dega.
        print(f"⚠️ Order submitted but not recorded: {exc}")

    if live:
        _invalidate_private_cache(account['id'])
    return {
        **plan,
        'order_id': order_id,
        'status': status,
        'mode': 'live' if live else 'paper',
        'source': source,
    }


# TradingView webhook par kitni requests — ek token, ek minute.
# Alert loop mein fans jaye to ye use exchange tak pahunchne se pehle rok
# deta hai; bina iske ek galat strategy sau orders bhej sakti hai.
TV_RATE_LIMIT = int(_fnum(os.getenv("TV_WEBHOOK_PER_MINUTE"), 20))
_tv_hits = {}
_tv_hits_lock = threading.Lock()


def _tv_rate_ok(token_key):
    now = time.time()
    with _tv_hits_lock:
        hits = [t for t in _tv_hits.get(token_key, []) if now - t < 60]
        if len(hits) >= TV_RATE_LIMIT:
            _tv_hits[token_key] = hits
            return False
        hits.append(now)
        _tv_hits[token_key] = hits
        return True


def _tv_webhook_url(token):
    base = (os.getenv("PUBLIC_API_URL") or request.host_url.rstrip('/') + '/api').rstrip('/')
    return f"{base}/webhooks/tv/{token}"


def _ensure_tv_token(user):
    """Token pehli baar maangne par hi banta hai, aur DB mein rehta hai."""
    token = (user.get('tv_token') or '').strip()
    if len(token) >= 32:
        return token
    token = secrets.token_urlsafe(32)
    update_user_account_fields(user['id'], tv_token=token)
    return token


TV_SAMPLE_MESSAGE = {
    "symbol": "BTCUSD",
    "side": "buy",
    "order_type": "limit",
    "size": 1,
    "size_unit": "contracts",
    "price": "{{close}}",
}


@app.route('/api/automation/tradingview', methods=['GET'])
@require_auth
def automation_tradingview():
    """
    TradingView automation ka setup — URL, token aur alert ka message.

    TradingView session nahi bhej sakta, isliye URL ka token hi is request
    ki poori pehchan hai. Usi wajah se ise password jaisa samjhein: jise
    URL mil gaya, wo aapke naam se signal bhej sakta hai.
    """
    try:
        token = _ensure_tv_token(g.user)
        return jsonify({
            'success': True,
            'data': {
                'webhook_url': _tv_webhook_url(token),
                'token': token,
                'message_template': json.dumps(TV_SAMPLE_MESSAGE, indent=2),
                'mode': 'live' if LIVE_ORDERS_ENABLED else 'paper',
                'rate_limit_per_minute': TV_RATE_LIMIT,
            },
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/automation/tradingview/regenerate', methods=['POST'])
@require_auth
def automation_tradingview_regenerate():
    """Purana URL turant bekaar ho jaata hai — leak hone par yahi bachav hai."""
    try:
        token = secrets.token_urlsafe(32)
        update_user_account_fields(g.user['id'], tv_token=token)
        return jsonify({'success': True, 'data': {'webhook_url': _tv_webhook_url(token), 'token': token}})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/webhooks/tv/<token>', methods=['POST'])
def tradingview_webhook(token):
    """
    TradingView alert yahan girta hai.

    Yahan session nahi hota — URL ka token hi user batata hai. Uske baad
    order bilkul wahi rasta lete hain jo desk ka button leta hai, isliye
    saare guards (limit, contract ka hisaab, key ki permissions) yahan bhi
    apne aap lagte hain.

    Jawab hamesha 200-ish rakha jaata hai jahan tak ho sake, kyunki
    TradingView error par alert band kar deta hai; galti ka detail body
    mein jaata hai aur order history mein dikhta hai.
    """
    try:
        user = get_user_account_by_tv_token(token)
        if not user:
            return jsonify({'success': False, 'error': 'Unknown webhook token'}), 404
        if not _tv_rate_ok(token[:12]):
            return jsonify({'success': False, 'error': 'Bahut zyada signals — ek minute mein '
                                                       f'{TV_RATE_LIMIT} se zyada nahi.'}), 429

        payload = request.get_json(silent=True)
        if payload is None:
            # TradingView plain text bhi bhej sakta hai; JSON hi support hai,
            # aur galti saaf batana behtar hai.
            raw = (request.get_data(as_text=True) or '')[:200]
            try:
                payload = json.loads(raw)
            except Exception:
                return jsonify({
                    'success': False,
                    'error': 'Alert message JSON hona chahiye. Settings mein diya template use karein.',
                    'got': raw,
                }), 400

        raw_id = payload.get('exchange_account_id')
        account = (
            get_exchange_account_for_user(int(raw_id), user['id']) if raw_id
            else _primary_exchange_account(user['id'])
        )
        if not account:
            return jsonify({'success': False, 'error': 'Is account se koi exchange juda nahi hai.'}), 400

        mode = 'live' if str(payload.get('mode') or 'live').lower() == 'live' else 'paper'
        result = submit_order(user=user, account=account, payload=payload, source='tradingview', mode=mode)
        return jsonify({'success': True, 'data': result}), 200
    except OrderRejected as rej:
        return jsonify({'success': False, 'error': rej.message, 'reason': rej.reason}), 400
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'exchange_account_id galat hai'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/byok/orders', methods=['POST'])
@require_auth
def byok_place_order():
    """
    Desk se order — abhi paper mode mein.

    Jaanch bilkul wahi hoti hai jo live order par hogi (contract ka hisaab,
    order value ki limit, key ki permissions), sirf exchange ko call nahi
    jaati. Isse poora rasta aaj test ho jaata hai, aur live karne ke liye
    sirf ek switch khulna baaki rehta hai.
    """
    try:
        payload = request.get_json(silent=True) or {}
        raw_id = payload.get('exchange_account_id')
        account = (
            get_exchange_account_for_user(int(raw_id), g.user['id']) if raw_id
            else _primary_exchange_account(g.user['id'])
        )
        if not account:
            return jsonify({
                'success': False,
                'error': 'Koi exchange juda nahi hai. Pehle Exchanges page se key jodein.',
                'reason': 'not_connected',
            }), 404

        mode = 'live' if str(payload.get('mode') or '').lower() == 'live' else 'paper'
        result = submit_order(user=g.user, account=account, payload=payload, source='desk', mode=mode)
        message = (
            'Order exchange par bhej diya gaya'
            if result['mode'] == 'live'
            else 'Paper order record ho gaya (exchange par nahi bheja gaya)'
        )
        return jsonify({'success': True, 'message': message, 'data': result}), 201
    except OrderRejected as rej:
        return jsonify({'success': False, 'error': rej.message, 'reason': rej.reason}), rej.status
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'exchange_account_id galat hai'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/byok/orders/preview', methods=['POST'])
@require_auth
def byok_preview_order():
    """
    Bhejne se pehle: ye order asli mein kya hai.

    UI isse confirm screen bharta hai — kitne contracts, kitne coins, aur
    lagbhag kitne ka. Order value chhupi na rahe, isliye yahi hisaab jo
    submit ke waqt lagta hai.
    """
    try:
        payload = request.get_json(silent=True) or {}
        raw_id = payload.get('exchange_account_id')
        account = (
            get_exchange_account_for_user(int(raw_id), g.user['id']) if raw_id
            else _primary_exchange_account(g.user['id'])
        )
        if not account:
            return jsonify({'success': False, 'error': 'Koi exchange juda nahi hai.', 'reason': 'not_connected'}), 404

        plan = prepare_order(account=account, payload=payload, require_live_toggle=False)
        plan.pop('adapter', None)
        plan['live_orders_enabled'] = LIVE_ORDERS_ENABLED and bool(account.get('live_trading_enabled'))
        return jsonify({'success': True, 'data': plan})
    except OrderRejected as rej:
        return jsonify({'success': False, 'error': rej.message, 'reason': rej.reason}), rej.status
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'exchange_account_id galat hai'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/byok/exchange-accounts/<int:account_id>/trading', methods=['PATCH'])
@require_auth
def byok_update_trading_settings(account_id):
    """
    Live trading on/off aur per-order limit.

    Alag endpoint isliye ki "key jod di" aur "is key se asli order laga
    sakte ho" do alag faisle hain — dusra user ko jaan-boojh kar dena padta
    hai, aur uske baad bhi server ka apna switch (LIVE_ORDERS_ENABLED) khula
    hona chahiye.
    """
    try:
        account = get_exchange_account_for_user(account_id, g.user['id'])
        if not account:
            return jsonify({'success': False, 'error': 'Exchange account not found'}), 404

        payload = request.get_json(silent=True) or {}
        fields = {}
        if 'live_trading_enabled' in payload:
            enable = bool(payload['live_trading_enabled'])
            if enable and not account.get('permissions_verified'):
                return jsonify({
                    'success': False,
                    'error': 'Pehle key verify hone dein, phir live trading chaalu karein.',
                }), 400
            if enable and account.get('can_withdraw'):
                return jsonify({
                    'success': False,
                    'error': 'Withdrawal-enabled key par live trading chaalu nahi hoti.',
                }), 400
            fields['live_trading_enabled'] = enable
        if 'max_order_notional' in payload:
            raw = payload['max_order_notional']
            if raw in (None, ''):
                fields['max_order_notional'] = None
            else:
                value = _fnum(raw, 0.0)
                if value <= 0:
                    return jsonify({'success': False, 'error': 'Limit 0 se badi honi chahiye.'}), 400
                fields['max_order_notional'] = min(value, HARD_MAX_ORDER_NOTIONAL)
        if not fields:
            return jsonify({'success': False, 'error': 'Badalne ke liye kuch nahi bheja gaya.'}), 400

        update_exchange_account_status(account_id, g.user['id'], **fields)
        updated = get_exchange_account_for_user(account_id, g.user['id']) or {}
        return jsonify({
            'success': True,
            'data': {
                'live_trading_enabled': bool(updated.get('live_trading_enabled')),
                'max_order_notional': updated.get('max_order_notional'),
                'default_max_order_notional': DEFAULT_MAX_ORDER_NOTIONAL,
                'hard_max_order_notional': HARD_MAX_ORDER_NOTIONAL,
                # Server ka apna switch — user ke toggle se alag. Dono on hon
                # tabhi order exchange tak jaata hai.
                'server_live_orders_enabled': LIVE_ORDERS_ENABLED,
            },
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/byok/orders', methods=['GET'])
@require_auth
def byok_get_orders():
    try:
        limit = int(request.args.get('limit', 50))
        exchange_account_id = request.args.get('exchange_account_id')
        if exchange_account_id is not None and str(exchange_account_id).strip() != '':
            exchange_account_id = int(exchange_account_id)
        else:
            exchange_account_id = None
        rows = fetch_byok_orders(
            user_id=g.user['id'],
            limit=max(1, min(limit, 200)),
            exchange_account_id=exchange_account_id,
        )
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/byok/positions', methods=['GET'])
@require_auth
def byok_positions():
    """
    Logged-in user ke apne exchange account ki khuli positions.

    `exchange_account_id` optional hai — na do to user ka primary (sabse haal
    mein update hua active) account use hota hai. Desk ko isse pehle list
    fetch karne ki zarurat nahi padti.

    Koi account juda hi nahi ho to ye error nahi hai: `connected: false`
    jaata hai, taaki UI "positions nahi hain" ke bajaye "exchange jodein"
    dikha sake — dono baaton ka matlab alag hai.
    """
    try:
        raw_id = request.args.get('exchange_account_id')
        if raw_id:
            try:
                account = get_exchange_account_for_user(int(raw_id), g.user['id'])
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': 'exchange_account_id galat hai'}), 400
            if not account:
                return jsonify({'success': False, 'error': 'Exchange account not found'}), 404
        else:
            account = _primary_exchange_account(g.user['id'])

        if not account or not account.get('is_active'):
            return jsonify({
                'success': True,
                'data': {'connected': False, 'positions': [], 'error': ''},
            })

        exchange = account['exchange']
        api_key = decrypt_secret(account['api_key_encrypted'])
        secret_key = decrypt_secret(account['secret_key_encrypted'])

        def fetch():
            adapter = get_adapter(exchange, api_key, secret_key)
            if adapter is None:
                return None, message_for('adapter_missing', exchange=exchange_name(exchange))
            rows = adapter.positions()
            if rows is None:
                return None, adapter.error_message()
            return rows, ''

        raw, error = _cached_private(account['id'], 'get_margined_positions', fetch)
        if error:
            update_exchange_account_status(account['id'], g.user['id'], last_error=error[:400])

        return jsonify({
            'success': True,
            'data': {
                'connected': True,
                'account_id': account['id'],
                'exchange': exchange,
                'label': account.get('label') or '',
                'positions': raw if raw is not None else [],
                'error': error,
            },
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/byok/orders/cancel', methods=['POST'])
@require_auth
def byok_cancel_order():
    try:
        payload = request.get_json(silent=True) or {}
        exchange_account_id = payload.get('exchange_account_id')
        order_id = (payload.get('order_id') or '').strip()

        if not exchange_account_id or not order_id:
            return jsonify({'success': False, 'error': 'exchange_account_id and order_id are required'}), 400

        account = get_exchange_account_for_user(int(exchange_account_id), g.user['id'])
        if not account:
            return jsonify({'success': False, 'error': 'Exchange account not found'}), 404

        api_key = decrypt_secret(account['api_key_encrypted'])
        secret_key = decrypt_secret(account['secret_key_encrypted'])
        exchange_client = get_adapter(account['exchange'], api_key, secret_key)
        if exchange_client is None:
            return jsonify({'success': False, 'error': 'Exchange adapter not available'}), 400

        result = exchange_client.cancel_order(order_id)
        if not result:
            err = (exchange_client.error_message() or 'Order cancel failed').strip()
            update_exchange_account_status(
                account['id'],
                g.user['id'],
                last_error=err[:400],
            )
            return jsonify({'success': False, 'error': err[:400]}), 400

        return jsonify({'success': True, 'message': 'Order cancel request submitted', 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500



# Delta error code -> (reason slug, user-facing message template).
# Ye sab "keys setup/usable nahi hain" wale cases hain — inka matlab "no open positions" NAHI hai.
@app.route('/api/place-order', methods=['POST'])
@require_auth
def place_order():
    """
    Paper (demo) order — ye exchange par nahi jaata, sirf record banta hai.

    Auth zaroori hai kyunki order us user ke naam se save hota hai; pehle ye
    khula tha aur sab orders ek hi common list mein chale jaate the.
    """
    try:
        data = request.get_json()
        symbol = data.get('symbol', '')
        side = data.get('side', 'buy')
        order_type = data.get('order_type', 'market')
        quantity = float(data.get('quantity', 0))
        price = data.get('price', None)
        
        if not symbol or quantity <= 0:
            return jsonify({
                'success': False,
                'error': 'Symbol aur quantity required hain'
            }), 400
        
        if order_type == 'limit' and (not price or price <= 0):
            return jsonify({
                'success': False,
                'error': 'Price required for limit orders'
            }), 400
        
        # Demo mode: Simulate order placement
        order_id = f"ORD_{int(time.time() * 1000)}"
        
        order_entry = {
            'order_id': order_id,
            'symbol': symbol,
            'side': side,
            'order_type': order_type,
            'quantity': quantity,
            'price': price,
            'status': 'filled' if order_type == 'market' else 'pending',
            'timestamp': datetime.now().isoformat()
        }

        save_demo_order_entry(order_entry, user_id=g.user['id'])
        
        print(f"✅ Order placed: {side} {quantity} {symbol} @ {price or 'Market'}")
        
        return jsonify({
            'success': True,
            'message': 'Paper order record ho gaya (exchange par nahi bheja gaya)',
            'mode': 'paper',
            'order_id': order_id,
            'data': order_entry
        })
        
    except Exception as e:
        print(f"❌ Order error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/orders', methods=['GET'])
@require_auth
def get_orders():
    """Sirf isi user ke paper orders."""
    try:
        orders = fetch_recent_orders(limit=50, user_id=g.user['id'])
        return jsonify({
            'success': True,
            'data': orders
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/backtest', methods=['GET'])
def backtest_strategy():
    """Backtest strategies (EMA crossover + Range Breakout) with user-specified days"""
    try:
        strategy_name = (request.args.get('strategy') or 'ema-crossover').strip().lower()
        symbol = request.args.get('symbol', 'BTCUSDT')
        sl_points = float(request.args.get('sl_points', 400))
        target_points = float(request.args.get('target_points', 800))
        lots = max(0.01, float(request.args.get('lots', 1)))  # Position size (lots); default 1
        ema9 = int(request.args.get('ema9', 9))
        ema21 = int(request.args.get('ema21', 21))
        ema50 = int(request.args.get('ema50', 50))
        exchange = request.args.get('exchange', 'delta')  # Default to Delta Exchange
        days = int(request.args.get('days', 30))  # Default 30 days
        timeframe = request.args.get('timeframe', '5m')  # Default 5 minutes
        
        # RSI parameters
        rsi_period = int(request.args.get('rsi_period', 14))  # Default RSI period 14
        rsi_overbought = float(request.args.get('rsi_overbought', 60))  # Default overbought 60
        rsi_oversold = float(request.args.get('rsi_oversold', 40))  # Default oversold 40
        use_rsi_filter = request.args.get('use_rsi_filter', 'false').lower() == 'true'  # Default false
        use_no_entry_window = request.args.get('use_no_entry_window', 'true').lower() == 'true'  # Default true

        # Range Breakout parameters (IST time window)
        range_start = (request.args.get('range_start') or '11:00').strip()
        range_end = (request.args.get('range_end') or '13:00').strip()
        range_timezone = (request.args.get('range_timezone') or 'Asia/Kolkata').strip()
        
        # Validate inputs - support up to 700 days
        if days <= 0:
            return jsonify({
                'success': False,
                'error': 'Days must be greater than 0'
            }), 400
        if days > 700:
            return jsonify({
                'success': False,
                'error': 'Days must be 700 or less'
            }), 400
        
        print(f"📊 Starting professional backtest for {symbol}")
        print(f"   Strategy: {strategy_name}")
        print(f"   Parameters: {days} days, {timeframe} timeframe, Lots: {lots}, Exchange: {exchange}")
        if strategy_name == "ema-crossover":
            print(f"   EMA: ({ema9}, {ema21}, {ema50}), SL: {sl_points}, Target: {target_points}")
            print(f"   No-entry window (11:00-14:00 IST): {'ON' if use_no_entry_window else 'OFF'}")
            if use_rsi_filter:
                print(f"   RSI Filter: Period={rsi_period}, Overbought={rsi_overbought}, Oversold={rsi_oversold}")
        elif strategy_name in {"range-breakout", "range_breakout", "range breakout"}:
            print(f"   Range window ({range_timezone}): {range_start} → {range_end}")
            print(f"   RSI: Period={rsi_period}, Upper={rsi_overbought}, Lower={rsi_oversold}")
            print(f"   SL / Target (points from entry): {sl_points} / {target_points}")
        else:
            return jsonify({
                'success': False,
                'error': f"Unknown strategy '{strategy_name}'. Use 'ema-crossover' or 'range-breakout'."
            }), 400
        
        all_candles = []
        
        # Fetch data using batch fetching (handles API limits automatically)
        print(f"📈 Fetching historical data: {symbol}, {timeframe}, {days} days from {exchange}")
        historical_data = client.get_historical_data_batch(
            symbol=symbol,
            interval=timeframe,
            days=days,
            exchange_name=exchange
        )
        
        if not historical_data:
            return jsonify({
                'success': False,
                'error': 'Failed to fetch historical data from Delta Exchange. Check symbol (e.g. BTCUSDT maps to BTCUSD) and network.'
            }), 400
        
        if 'dataframe' not in historical_data:
            return jsonify({
                'success': False,
                'error': 'No dataframe in historical data response'
            }), 400
        
        df = historical_data['dataframe']
        # Get actual days covered from the data, not from request
        actual_days_covered = historical_data.get('actual_days', 0)
        print(f"✅ Fetched {len(df)} candles")
        if actual_days_covered > 0:
            print(f"   Actual days covered: {actual_days_covered:.2f} days")
        print(f"   Requested: {days} days")
        
        if len(df) == 0:
            return jsonify({
                'success': False,
                'error': 'No candles returned for this period. Try fewer days or different timeframe (Delta India: BTCUSD, ETHUSD).'
            }), 400
        
        # Convert to list format
        for idx, row in df.iterrows():
            try:
                all_candles.append({
                    'time': int(pd.Timestamp(row['Open Time']).timestamp() * 1000),
                    'open': float(row['Open']),
                    'high': float(row['High']),
                    'low': float(row['Low']),
                    'close': float(row['Close']),
                    'volume': float(row['Volume'])
                })
            except Exception as e:
                print(f"⚠️ Error processing candle {idx}: {e}")
                continue
        
        if not all_candles:
            return jsonify({
                'success': False,
                'error': 'No valid candles processed for backtesting'
            }), 400
        
        print(f"✅ Processed {len(all_candles)} candles for backtest")
        
        # Sort by time (oldest to newest) - CRITICAL for backtest
        all_candles.sort(key=lambda x: x['time'])
        
        if all_candles:
            first_time = pd.Timestamp(all_candles[0]['time']/1000, unit='s')
            last_time = pd.Timestamp(all_candles[-1]['time']/1000, unit='s')
            time_span = (last_time - first_time).total_seconds() / (60 * 60 * 24)
            print(f"   First candle: {first_time} ({all_candles[0]['time']})")
            print(f"   Last candle: {last_time} ({all_candles[-1]['time']})")
            print(f"   Time span: {time_span:.2f} days")
            print(f"   Expected: {days} days, Got: {len(all_candles)} candles")
        else:
            print(f"   ⚠️ No candles to process!")
        
        # Calculate EMAs with custom periods
        closes = [c['close'] for c in all_candles]
        if strategy_name == "ema-crossover":
            ema9_values = calculate_ema(pd.Series(closes), ema9).tolist()
            ema21_values = calculate_ema(pd.Series(closes), ema21).tolist()
            ema50_values = calculate_ema(pd.Series(closes), ema50).tolist()

            # Calculate RSI if filter is enabled
            rsi_values = []
            if use_rsi_filter:
                rsi_values = calculate_rsi(pd.Series(closes), rsi_period).tolist()

            # Add EMAs to candles with dynamic keys
            for i, candle in enumerate(all_candles):
                candle[f'ema_{ema9}'] = ema9_values[i] if i < len(ema9_values) else None
                candle[f'ema_{ema21}'] = ema21_values[i] if i < len(ema21_values) else None
                candle[f'ema_{ema50}'] = ema50_values[i] if i < len(ema50_values) else None
                # Also add with standard keys for compatibility (use lowercase with underscore)
                if ema9 == 9:
                    candle['ema_9'] = candle[f'ema_{ema9}']
                if ema21 == 21:
                    candle['ema_21'] = candle[f'ema_{ema21}']
                if ema50 == 50:
                    candle['ema_50'] = candle[f'ema_{ema50}']

                # Add RSI values if filter is enabled
                if use_rsi_filter and i < len(rsi_values):
                    candle['rsi'] = rsi_values[i]
        else:
            # Range breakout always uses RSI
            rsi_values = calculate_rsi(pd.Series(closes), rsi_period).tolist()
            for i, candle in enumerate(all_candles):
                candle['rsi'] = rsi_values[i] if i < len(rsi_values) else None
        
        # Run backtest
        trades = []
        position = None  # {'side': 'buy'/'sell', 'entry_price': float, 'entry_time': int, 'sl': float, 'target': float}
        total_trades = 0
        winning_trades = 0
        losing_trades = 0
        sl_hits = 0
        target_hits = 0
        total_profit = 0.0

        def _append_trade(pos, exit_time_ms, exit_price, status):
            nonlocal total_profit, winning_trades, losing_trades, sl_hits, target_hits
            if pos['side'] == 'buy':
                pnl_points = exit_price - pos['entry_price']
            else:
                pnl_points = pos['entry_price'] - exit_price
            pnl = pnl_points * lots * 0.001
            trades.append({
                'entry_time': pos['entry_time'],
                'exit_time': exit_time_ms,
                'side': pos['side'],
                'entry_price': pos['entry_price'],
                'exit_price': exit_price,
                'stop_loss': pos.get('sl'),
                'target': pos.get('target'),
                'pnl': pnl,
                'pnl_points': pnl_points,
                'status': status
            })
            total_profit += pnl
            if status == 'TARGET_HIT':
                winning_trades += 1
                target_hits += 1
            elif status == 'SL_HIT':
                losing_trades += 1
                sl_hits += 1
            else:
                if pnl > 0:
                    winning_trades += 1
                else:
                    losing_trades += 1

        if strategy_name == "ema-crossover":
            for i in range(1, len(all_candles)):
                current = all_candles[i]
                previous = all_candles[i-1]
                current_time_ist = pd.Timestamp(current['time'], unit='ms', tz='UTC').tz_convert('Asia/Kolkata')
                current_minutes_ist = current_time_ist.hour * 60 + current_time_ist.minute
                in_no_entry_window = use_no_entry_window and ((11 * 60) <= current_minutes_ist < (14 * 60))

                # Check if we have valid EMAs (use dynamic keys)
                current_ema9_val = current.get(f'ema_{ema9}') or current.get('ema_9')
                current_ema21_val = current.get(f'ema_{ema21}') or current.get('ema_21')
                current_ema50_val = current.get(f'ema_{ema50}') or current.get('ema_50')
                prev_ema9_val = previous.get(f'ema_{ema9}') or previous.get('ema_9')
                prev_ema21_val = previous.get(f'ema_{ema21}') or previous.get('ema_21')
                prev_ema50_val = previous.get(f'ema_{ema50}') or previous.get('ema_50')

                if not all([current_ema9_val, current_ema21_val, current_ema50_val,
                           prev_ema9_val, prev_ema21_val, prev_ema50_val]):
                    continue

                current_ema9 = current_ema9_val
                current_ema21 = current_ema21_val
                current_ema50 = current_ema50_val
                current_price = current['close']

                prev_ema9 = prev_ema9_val
                prev_ema21 = prev_ema21_val
                prev_ema50 = prev_ema50_val

                ema9_above_both_now = current_ema9 > current_ema21 and current_ema9 > current_ema50
                ema9_below_both_now = current_ema9 < current_ema21 and current_ema9 < current_ema50
                ema9_above_both_prev = prev_ema9 > prev_ema21 and prev_ema9 > prev_ema50
                ema9_below_both_prev = prev_ema9 < prev_ema21 and prev_ema9 < prev_ema50

                # Check existing position for SL/Target
                if position:
                    if position['side'] == 'buy':
                        if current['low'] <= position['sl']:
                            _append_trade(position, current['time'], position['sl'], 'SL_HIT')
                            position = None
                        elif current['high'] >= position['target']:
                            _append_trade(position, current['time'], position['target'], 'TARGET_HIT')
                            position = None
                    elif position['side'] == 'sell':
                        if current['high'] >= position['sl']:
                            _append_trade(position, current['time'], position['sl'], 'SL_HIT')
                            position = None
                        elif current['low'] <= position['target']:
                            _append_trade(position, current['time'], position['target'], 'TARGET_HIT')
                            position = None

                # Check for new signals
                if not position:
                    if in_no_entry_window:
                        continue

                    if not ema9_above_both_prev and ema9_above_both_now:
                        rsi_filter_pass = True
                        if use_rsi_filter:
                            current_rsi = current.get('rsi')
                            if current_rsi is None:
                                rsi_filter_pass = False
                            else:
                                if rsi_oversold < current_rsi < rsi_overbought:
                                    rsi_filter_pass = False

                        if rsi_filter_pass:
                            entry_price = current_price
                            position = {
                                'side': 'buy',
                                'entry_price': entry_price,
                                'entry_time': current['time'],
                                'sl': entry_price - sl_points,
                                'target': entry_price + target_points
                            }
                            total_trades += 1

                    elif not ema9_below_both_prev and ema9_below_both_now:
                        rsi_filter_pass = True
                        if use_rsi_filter:
                            current_rsi = current.get('rsi')
                            if current_rsi is None:
                                rsi_filter_pass = False
                            else:
                                if rsi_oversold < current_rsi < rsi_overbought:
                                    rsi_filter_pass = False

                        if rsi_filter_pass:
                            entry_price = current_price
                            position = {
                                'side': 'sell',
                                'entry_price': entry_price,
                                'entry_time': current['time'],
                                'sl': entry_price + sl_points,
                                'target': entry_price - target_points
                            }
                            total_trades += 1
        else:
            # Range Breakout implementation
            def _parse_hhmm(value):
                m = re.fullmatch(r"(\d{1,2}):(\d{2})", (value or "").strip())
                if not m:
                    raise ValueError(f"Invalid time '{value}'. Use HH:MM (e.g. 11:00).")
                hh = int(m.group(1))
                mm = int(m.group(2))
                if hh < 0 or hh > 23 or mm < 0 or mm > 59:
                    raise ValueError(f"Invalid time '{value}'.")
                return hh * 60 + mm

            start_min = _parse_hhmm(range_start)
            end_min = _parse_hhmm(range_end)
            if start_min >= end_min:
                return jsonify({
                    'success': False,
                    'error': 'range_start must be earlier than range_end (same day, IST).'
                }), 400
            if sl_points <= 0 or target_points <= 0:
                return jsonify({
                    'success': False,
                    'error': 'sl_points and target_points must be greater than 0 (range breakout).'
                }), 400

            current_day = None
            range_high = None
            range_low = None
            range_finalized = False
            breakout_long = None  # {'time': ms, 'high': float, 'low': float}
            breakout_short = None
            traded_long = False
            traded_short = False

            for i in range(1, len(all_candles)):
                current = all_candles[i]
                ts_local = pd.Timestamp(current['time'], unit='ms', tz='UTC').tz_convert(range_timezone)
                day_key = ts_local.date()
                minutes_local = ts_local.hour * 60 + ts_local.minute

                if current_day != day_key:
                    current_day = day_key
                    range_high = None
                    range_low = None
                    range_finalized = False
                    breakout_long = None
                    breakout_short = None
                    traded_long = False
                    traded_short = False

                # Build the range inside the time window (inclusive start, exclusive end)
                if start_min <= minutes_local < end_min:
                    h = float(current['high'])
                    l = float(current['low'])
                    range_high = h if range_high is None else max(range_high, h)
                    range_low = l if range_low is None else min(range_low, l)
                    continue

                # Finalize range once window ends and we have values
                if (minutes_local >= end_min) and (range_high is not None) and (range_low is not None):
                    range_finalized = True

                # Manage open position exits first
                if position:
                    if position['side'] == 'buy':
                        if current['low'] <= position['sl']:
                            _append_trade(position, current['time'], position['sl'], 'SL_HIT')
                            position = None
                        elif current['high'] >= position['target']:
                            _append_trade(position, current['time'], position['target'], 'TARGET_HIT')
                            position = None
                    else:
                        if current['high'] >= position['sl']:
                            _append_trade(position, current['time'], position['sl'], 'SL_HIT')
                            position = None
                        elif current['low'] <= position['target']:
                            _append_trade(position, current['time'], position['target'], 'TARGET_HIT')
                            position = None

                if not range_finalized or position:
                    continue

                # Identify breakout candle (close beyond range) once per day per side
                if breakout_long is None and current['close'] > range_high:
                    breakout_long = {'time': current['time'], 'high': float(current['high']), 'low': float(current['low'])}
                    continue
                if breakout_short is None and current['close'] < range_low:
                    breakout_short = {'time': current['time'], 'high': float(current['high']), 'low': float(current['low'])}
                    continue

                # Entry after breakout candle: price breaks breakout candle high/low + RSI condition
                current_rsi = current.get('rsi')
                if current_rsi is None or pd.isna(current_rsi):
                    continue

                if breakout_long and not position:
                    if (not traded_long) and (current['time'] > breakout_long['time']) and current['high'] > breakout_long['high'] and float(current_rsi) > rsi_overbought:
                        entry_price = float(breakout_long['high'])
                        position = {
                            'side': 'buy',
                            'entry_price': entry_price,
                            'entry_time': current['time'],
                            'sl': entry_price - sl_points,
                            'target': entry_price + target_points
                        }
                        total_trades += 1
                        traded_long = True
                        breakout_long = None

                if breakout_short and not position:
                    if (not traded_short) and (current['time'] > breakout_short['time']) and current['low'] < breakout_short['low'] and float(current_rsi) < rsi_oversold:
                        entry_price = float(breakout_short['low'])
                        position = {
                            'side': 'sell',
                            'entry_price': entry_price,
                            'entry_time': current['time'],
                            'sl': entry_price + sl_points,
                            'target': entry_price - target_points
                        }
                        total_trades += 1
                        traded_short = True
                        breakout_short = None
        
        # Close any open position at the end
        if position and all_candles:
            last_candle = all_candles[-1]
            exit_price = last_candle['close']
            _append_trade(position, last_candle['time'], exit_price, 'CLOSED_AT_END')
        
        win_rate = (winning_trades / len(trades) * 100) if trades else 0
        
        print(f"📊 Backtest Results:")
        print(f"   Total candles processed: {len(all_candles)}")
        print(f"   Total entry signals: {total_trades}")
        print(f"   Completed trades: {len(trades)}")
        print(f"   Winning trades: {winning_trades} (Target hits: {target_hits})")
        print(f"   Losing trades: {losing_trades} (SL hits: {sl_hits})")
        print(f"   Win rate: {win_rate:.2f}%")
        print(f"   Total profit: {total_profit:.2f} (lots: {lots})")
        
        # Calculate actual period covered
        if all_candles:
            start_time = all_candles[0]['time']
            end_time = all_candles[-1]['time']
            actual_days_covered = (end_time - start_time) / (1000 * 60 * 60 * 24)
        else:
            actual_days_covered = 0
        
        return jsonify({
            'success': True,
            'strategy': 'range-breakout' if strategy_name != 'ema-crossover' else 'ema-crossover',
            'symbol': symbol,
            'exchange': exchange,
            'timeframe': timeframe,
            'days_requested': days,
            'days_covered': round(actual_days_covered, 2),
            'total_candles': len(all_candles),
            'ema_periods': {
                'ema9': ema9,
                'ema21': ema21,
                'ema50': ema50
            } if strategy_name == 'ema-crossover' else None,
            'rsi_settings': {
                'enabled': True if strategy_name != 'ema-crossover' else use_rsi_filter,
                'period': rsi_period,
                'overbought': rsi_overbought,
                'oversold': rsi_oversold
            },
            'range_settings': {
                'timezone': range_timezone,
                'start': range_start,
                'end': range_end,
                'sl_points': sl_points,
                'target_points': target_points,
            } if strategy_name != 'ema-crossover' else None,
            'total_trades': len(trades),  # Completed trades only
            'total_signals': total_trades,  # All entry signals (including open positions)
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'sl_hits': sl_hits,
            'target_hits': target_hits,
            'win_rate': round(win_rate, 2),
            'total_profit': round(total_profit, 2),
            'lots': lots,
            'sl_points': sl_points,
            'target_points': target_points,
            'use_no_entry_window': use_no_entry_window,
            'trades': trades  # Return full trade list for complete reporting/export
        })
        
    except Exception as e:
        print(f"❌ Backtest error: {e}")
        import traceback
        error_trace = traceback.format_exc()
        print(error_trace)
        return jsonify({
            'success': False,
            'error': f'Backtest failed: {str(e)}',
            'details': error_trace.split('\n')[-5:] if len(error_trace) > 200 else error_trace
        }), 500


app.register_blueprint(create_options_backtest_blueprint(client))


@app.route('/api/health', methods=['GET'])
def health_check():
    """API health check"""
    try:
        database = database_backend()
    except Exception:
        database = None
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        # Engine ka naam hi — accounts permanent DB mein ja rahe hain ya
        # deploy par mit jane wali file mein, ye bahar se dikhna chahiye.
        'database': database,
    })


@app.route('/')
def index():
    """
    Service info.

    Pehle yahan purana single-file dashboard serve hota tha. Wo ab hata diya
    gaya hai: uske saare private calls server ki apni Delta key se chalte the
    aur bina login khule the, isliye koi bhi us page se account ka data padh
    aur order laga sakta tha. Asli UI alag frontend hai.
    """
    return jsonify({
        'service': 'Finowings Desk API',
        'status': 'ok',
        'frontend': os.getenv('FRONTEND_URL', ''),
        'docs': '/api/health',
    })


if __name__ == '__main__':
    os.makedirs('static', exist_ok=True)
    
    index_path = os.path.join('static', 'index.html')
    if not os.path.exists(index_path):
        print(f"⚠️ Warning: {index_path} not found!")
    
    print("=" * 70)
    print("🚀 Crypto Trading Website Backend Starting...")
    print("=" * 70)
    print("\n📡 API Endpoints:")
    print("   GET /api/candles - Candle data with EMA")
    print("   GET /api/market-info - Market information")
    print("   POST /api/auth/register | /api/auth/login - User accounts")
    print("   GET  /api/byok/exchange-accounts - User ki apni exchange keys")
    print("   GET  /api/byok/positions - User ke apne account ki positions")
    print("   GET /api/backtest - Backtest strategy with 1 month data")
    print("   GET /api/health - Health check")
    print("⚠️  Server stop karne ke liye Ctrl+C press karein\n")
    
    # Set port to 2000
    port = 2000
    
    print(f"\n🌐 Frontend: http://localhost:{port}")
    print("=" * 70)
    print(f"\n✅ Server ready! Browser mein http://localhost:{port} open karein")
    print("⚠️  Server stop karne ke liye Ctrl+C press karein\n")
    
    app.run(debug=True, host='127.0.0.1', port=port, use_reloader=False)
