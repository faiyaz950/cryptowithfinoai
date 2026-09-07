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
from datetime import datetime, timedelta
import time
from fetch_trading_data import BASE_URL, CryptoAPIClient, DeltaExchangeClient
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
    save_login_entry,
    fetch_login_history,
    save_broker_login_entry,
    get_latest_broker_login,
    save_demo_order_entry,
    fetch_recent_orders,
    create_user_account,
    get_user_account_by_username,
    get_user_account_by_id,
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


SUPPORTED_EXCHANGES = {"delta", "binance", "bybit"}
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "24"))
MAX_ORDER_QTY = float(os.getenv("BYOK_MAX_ORDER_QTY", "1000"))
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


def validate_exchange_credentials(exchange, api_key, secret_key):
    exchange = (exchange or "").lower().strip()
    if exchange != "delta":
        return {
            "success": False,
            "can_trade": False,
            "can_withdraw": False,
            "permissions_verified": False,
            "error": f"{exchange} BYOK adapter not implemented yet. Use delta for now.",
        }
    delta_client = DeltaExchangeClient(api_key, secret_key)
    probe = delta_client.get_positions(underlying_asset_symbol="BTC")
    if probe is not None:
        return {
            "success": True,
            "can_trade": True,
            "can_withdraw": False,
            "permissions_verified": True,
            "error": "",
        }
    err = (delta_client.last_error or "Credential verification failed").strip()
    return {
        "success": False,
        "can_trade": False,
        "can_withdraw": False,
        "permissions_verified": False,
        "error": err[:400],
    }


def get_exchange_client(exchange, api_key, secret_key):
    exchange = (exchange or "").lower().strip()
    if exchange == "delta":
        return DeltaExchangeClient(api_key, secret_key)
    return None


def fetch_exchange_profile(exchange, api_key, secret_key):
    client_obj = get_exchange_client(exchange, api_key, secret_key)
    if client_obj is None:
        return {}
    try:
        if hasattr(client_obj, "get_account_profile"):
            profile = client_obj.get_account_profile()
            if isinstance(profile, dict):
                return profile
    except Exception:
        pass
    return {}


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
    exchange_profile = {}
    wallet_snapshot = {}
    auth_proof = {"private_api_access": False, "last_auth_error": ""}
    fingerprint_masked = _mask_fingerprint(account_full.get("api_key_fingerprint", ""))
    encrypted_key = account_full.get("api_key_encrypted") or ""
    encrypted_secret = account_full.get("secret_key_encrypted") or ""
    if not encrypted_key or not encrypted_secret:
        return exchange_profile, wallet_snapshot, auth_proof, fingerprint_masked

    try:
        api_key = decrypt_secret(encrypted_key)
        secret_key = decrypt_secret(encrypted_secret)
        client_obj = get_exchange_client("delta", api_key, secret_key)
        if client_obj:
            live_profile = client_obj.get_account_profile()
            exchange_profile = extract_exchange_profile_snapshot(live_profile if isinstance(live_profile, dict) else {})

            wallet_data = {}
            if hasattr(client_obj, "get_wallet_balances"):
                wallet_data = client_obj.get_wallet_balances()
            wallet_snapshot = extract_wallet_snapshot(wallet_data)

            auth_ok = bool(exchange_profile.get("has_profile_data")) or bool(wallet_snapshot.get("has_wallet_data"))
            auth_proof = {
                "private_api_access": auth_ok,
                "last_auth_error": (client_obj.last_error or "")[:300],
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


@app.route('/api/candles', methods=['GET'])
def get_candles():
    """Candle data with EMA fetch karta hai"""
    try:
        symbol = request.args.get('symbol', 'BTCUSDT')
        interval = request.args.get('interval', '1h')
        limit = int(request.args.get('limit', 100))
        exchange = request.args.get('exchange', 'delta')
        
        ema_periods_str = request.args.get('ema_periods', '9,21,50')
        ema_periods = [int(p.strip()) for p in ema_periods_str.split(',')]
        
        # RSI parameters
        rsi_period = int(request.args.get('rsi_period', 14))
        include_rsi = request.args.get('include_rsi', 'false').lower() == 'true'
        
        # Fetch historical data
        historical_data = client.get_historical_data(
            symbol=symbol,
            interval=interval,
            limit=limit,
            exchange_name=exchange
        )
        
        if not historical_data or 'dataframe' not in historical_data:
            return jsonify({
                'error': 'Data fetch nahi hua. API credentials check karein.',
                'success': False
            }), 400
        
        df = historical_data['dataframe']
        if df is None or len(df) == 0:
            return jsonify({
                'error': 'Candle data empty hai. Symbol ya network check karein.',
                'success': False
            }), 400
        
        # Prepare data with EMA and RSI
        result = prepare_candle_data_with_ema(df, ema_periods, rsi_period, include_rsi)
        
        return jsonify({
            'success': True,
            'symbol': symbol,
            'interval': interval,
            **result
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


@app.route('/api/market-info', methods=['GET'])
def get_market_info():
    """Market information fetch karta hai"""
    try:
        symbol = request.args.get('symbol', 'BTCUSDT')
        exchange = request.args.get('exchange', 'delta')
        
        historical_data = client.get_historical_data(
            symbol=symbol,
            interval='1m',
            limit=1,
            exchange_name=exchange
        )
        
        if not historical_data or 'dataframe' not in historical_data:
            return jsonify({
                'error': 'Data fetch nahi hua',
                'success': False
            }), 400
        
        df = historical_data['dataframe']
        latest = df.iloc[-1]
        
        change_24h = 0.0
        if len(df) > 1:
            change_24h = float(((latest['Close'] - df.iloc[0]['Open']) / df.iloc[0]['Open']) * 100)
        
        return jsonify({
            'success': True,
            'symbol': symbol,
            'current_price': float(latest['Close']),
            'high_24h': float(df['High'].max()),
            'low_24h': float(df['Low'].min()),
            'volume_24h': float(df['Volume'].sum()),
            'change_24h': change_24h
        })
        
    except Exception as e:
        return jsonify({
            'error': str(e),
            'success': False
        }), 500


# Default credentials (demo mode)
DEFAULT_CREDENTIALS = {
    'admin': 'admin123',
    'user': 'user123',
    'demo': 'demo123'
}

@app.route('/api/login', methods=['POST', 'OPTIONS'])
def login():
    """Login endpoint - stores login data"""
    if request.method == 'OPTIONS':
        return '', 204
    try:
        data = request.get_json(silent=True) or {}
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        
        if not username or not password:
            print(f"⚠️ Login rejected: username/password empty (username={repr(username)[:20]})")
            return jsonify({
                'success': False,
                'error': 'Username aur password required hain'
            }), 400
        
        # Demo mode: Accept any credentials OR default credentials
        # In production, validate against database
        is_valid = False
        
        # Check default credentials
        if username in DEFAULT_CREDENTIALS and DEFAULT_CREDENTIALS[username] == password:
            is_valid = True
        else:
            # Demo mode: accept any credentials
            is_valid = True
        
        if not is_valid:
            return jsonify({
                'success': False,
                'error': 'Invalid username or password'
            }), 401
        
        # Save to DB
        try:
            login_time = save_login_entry(
                username=username,
                password=password,  # In production, hash this!
                login_type='app',
                ip_address=request.remote_addr
            )
            print(f"✅ Login recorded in DB: {username} at {login_time.isoformat()}")
        except Exception as db_err:
            print(f"❌ Login DB save failed: {db_err}")
            return jsonify({
                'success': False,
                'error': f'Login save failed: {db_err}'
            }), 500
        
        return jsonify({
            'success': True,
            'message': 'Login successful',
            'username': username
        })
        
    except Exception as e:
        print(f"❌ Login error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/delta-demo-login', methods=['POST', 'GET', 'OPTIONS'])
def delta_demo_login():
    """Delta Exchange Demo account login endpoint"""
    if request.method == 'OPTIONS':
        return '', 204
    # Handle GET request for testing
    if request.method == 'GET':
        return jsonify({
            'success': True,
            'message': 'Delta Demo Login endpoint is working!',
            'endpoint': '/api/delta-demo-login'
        })
    
    try:
        data = request.get_json(silent=True) or {}
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        
        if not username or not password:
            return jsonify({
                'success': False,
                'error': 'Username aur password required hain'
            }), 400
        
        # Delta Exchange Demo login - authenticate with Delta Exchange demo API
        # Note: Delta Exchange demo typically requires API keys, but we'll accept username/password
        # In production, this would authenticate with Delta Exchange demo API
        
        # For demo purposes, accept any credentials
        # In production, validate against Delta Exchange demo API
        is_valid = True
        
        try:
            login_time = save_login_entry(
                username=username,
                password=password,  # In production, hash this!
                login_type='delta_demo',
                ip_address=request.remote_addr
            )
            print(f"✅ Delta Exchange Demo login recorded in DB: {username} at {login_time.isoformat()}")
        except Exception as db_err:
            print(f"❌ Delta demo login DB save failed: {db_err}")
            return jsonify({
                'success': False,
                'error': f'Delta demo login save failed: {db_err}'
            }), 500
        
        # Initialize Delta Exchange client with default credentials
        # User can later update API keys if needed
        delta_client = DeltaExchangeClient(
            "2NifBsEb6rIH2xM7dapTZr1wBSv8Ua",
            "vDJairU3fNWEyVJOqtmdKwK2iL8eH4M0ifH4ViK1rEPmvhGylvPg6RK6Ll8Z"
        )
        
        # Test connection
        delta_warning = None
        try:
            market_data = delta_client.get_market_data()
            if market_data:
                print(f"✅ Delta Exchange connection successful")
            else:
                print(f"⚠️ Delta Exchange connection test failed, but login accepted")
                if getattr(delta_client, 'last_error', None) and 'expired_signature' in (delta_client.last_error or ''):
                    delta_warning = 'Delta session expired. Positions/orders load nahi honge - valid API keys use karein ya baad mein dubara login karein.'
        except Exception as e:
            print(f"⚠️ Delta Exchange connection test error: {e}, but login accepted")
            delta_warning = 'Delta connection check fail. Positions/orders load nahi ho sakte.'
        
        out = {
            'success': True,
            'message': 'Delta Exchange Demo login successful',
            'username': username,
            'login_type': 'delta_demo'
        }
        if delta_warning:
            out['warning'] = delta_warning
        return jsonify(out)
        
    except Exception as e:
        print(f"❌ Delta Demo login error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/login-history', methods=['GET'])
def get_login_history():
    """Get login history (admin endpoint)"""
    try:
        total_logins, logins = fetch_login_history(limit=50)
        return jsonify({
            'success': True,
            'total_logins': total_logins,
            'logins': logins
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    """Create a real user account for BYOK trading."""
    if not DB_READY:
        return jsonify({'success': False, 'error': 'Database unavailable'}), 503
    try:
        payload = request.get_json(silent=True) or {}
        username = (payload.get('username') or '').strip()
        password = payload.get('password') or ''
        email = (payload.get('email') or '').strip()

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

        password_hash = hash_password(password)
        created_user = create_user_account(username, password_hash, email=email)

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
        username = (payload.get('username') or '').strip()
        password = payload.get('password') or ''

        user = get_user_account_by_username(username)
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
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/auth/key-login', methods=['POST'])
def auth_key_login():
    """
    Login/signup using exchange API key + secret.
    If key already linked, logs into existing user.
    If new key, creates user and links account.
    """
    if not DB_READY:
        return jsonify({'success': False, 'error': 'Database unavailable'}), 503
    try:
        payload = request.get_json(silent=True) or {}
        exchange = (payload.get('exchange') or 'delta').strip().lower()
        api_key = (payload.get('api_key') or '').strip()
        secret_key = (payload.get('secret_key') or '').strip()
        label = (payload.get('label') or 'Primary').strip()

        if exchange not in SUPPORTED_EXCHANGES:
            return jsonify({'success': False, 'error': f'Unsupported exchange: {exchange}'}), 400
        if not api_key or not secret_key:
            return jsonify({'success': False, 'error': 'api_key and secret_key are required'}), 400

        verify = validate_exchange_credentials(exchange, api_key, secret_key)
        if not verify.get('success'):
            return jsonify({
                'success': False,
                'error': verify.get('error') or 'Credential verification failed'
            }), 401
        if verify.get('can_withdraw', False):
            return jsonify({
                'success': False,
                'error': 'Withdrawal-enabled API keys are not allowed. Use Trade+Read only.'
            }), 400

        fingerprint = api_key_fingerprint(exchange, api_key)
        existing_account = get_exchange_account_by_fingerprint(exchange, fingerprint)
        user = None
        account_id = None

        profile_data = fetch_exchange_profile(exchange, api_key, secret_key)
        profile_name, profile_email = extract_user_profile_from_exchange(profile_data)

        if existing_account:
            user = get_user_account_by_id(existing_account['user_id'])
            if not user or not user.get('is_active', False):
                return jsonify({'success': False, 'error': 'Linked user is not active'}), 401
            account_id = existing_account['id']
            update_exchange_account_credentials(
                account_id,
                user['id'],
                api_key_encrypted=encrypt_secret(api_key),
                secret_key_encrypted=encrypt_secret(secret_key),
                api_key_fingerprint=fingerprint,
                key_hint=key_hint(api_key),
            )
            update_exchange_account_status(
                account_id,
                user['id'],
                is_active=True,
                can_trade=verify.get('can_trade', False),
                can_withdraw=verify.get('can_withdraw', False),
                permissions_verified=verify.get('permissions_verified', False),
                last_error='',
            )
            updates = {}
            if profile_name and not (user.get('full_name') or '').strip():
                updates['full_name'] = profile_name[:50]
            if profile_email and validate_email(profile_email) and not (user.get('email') or '').strip():
                updates['email'] = profile_email.strip().lower()
                updates['email_verified'] = True
            if updates:
                update_user_account_fields(user['id'], **updates)
                user = get_user_account_by_id(user['id']) or user
        else:
            username_base = (
                _deep_find_first(profile_data, {"username", "user_name", "login_id"})
                or f"{exchange}_key_{api_key[-6:]}"
            )
            username = make_unique_username(username_base)
            random_password = secrets.token_urlsafe(18)
            created_user = create_user_account(
                username=username,
                password_hash=hash_password(random_password),
                email=profile_email if validate_email(profile_email) else "",
            )
            user = get_user_account_by_id(created_user['id']) or created_user
            user_updates = {}
            if profile_name:
                user_updates['full_name'] = profile_name[:50]
            if profile_email and validate_email(profile_email):
                user_updates['email'] = profile_email.strip().lower()
                user_updates['email_verified'] = True
            if user_updates:
                update_user_account_fields(user['id'], **user_updates)
                user = get_user_account_by_id(user['id']) or user
            account_id = create_exchange_account(
                user_id=user['id'],
                exchange=exchange,
                api_key_encrypted=encrypt_secret(api_key),
                secret_key_encrypted=encrypt_secret(secret_key),
                api_key_fingerprint=fingerprint,
                label=label or 'Primary',
                key_hint=key_hint(api_key),
                can_trade=verify.get('can_trade', False),
                can_withdraw=verify.get('can_withdraw', False),
                permissions_verified=verify.get('permissions_verified', False),
                last_error='',
            )

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
            'message': 'Logged in with exchange key',
            'token': session_token,
            'expires_at': expires_at.isoformat(),
            'user': {
                'id': user['id'],
                'username': user['username'],
                'full_name': user.get('full_name', ''),
                'email': user.get('email', ''),
                'email_verified': user.get('email_verified', False),
            },
            'exchange_account': {
                'id': account_id,
                'exchange': exchange,
                'key_hint': key_hint(api_key),
                'permissions_verified': verify.get('permissions_verified', False),
                'can_trade': verify.get('can_trade', False),
                'can_withdraw': verify.get('can_withdraw', False),
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
        label = (payload.get('label') or 'Primary').strip()

        if exchange not in SUPPORTED_EXCHANGES:
            return jsonify({'success': False, 'error': f'Unsupported exchange: {exchange}'}), 400
        if not api_key or not secret_key:
            return jsonify({'success': False, 'error': 'api_key and secret_key are required'}), 400

        verify = validate_exchange_credentials(exchange, api_key, secret_key)
        if not verify['success']:
            return jsonify({
                'success': False,
                'error': verify.get('error') or 'Credential verification failed',
                'exchange': exchange,
            }), 400

        account_id = create_exchange_account(
            user_id=g.user['id'],
            exchange=exchange,
            api_key_encrypted=encrypt_secret(api_key),
            secret_key_encrypted=encrypt_secret(secret_key),
            api_key_fingerprint=api_key_fingerprint(exchange, api_key),
            label=label,
            key_hint=key_hint(api_key),
            can_trade=verify.get('can_trade', False),
            can_withdraw=verify.get('can_withdraw', False),
            permissions_verified=verify.get('permissions_verified', False),
            last_error='',
        )

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


@app.route('/api/byok/exchange-accounts/<int:account_id>/verify', methods=['POST'])
@require_auth
def byok_verify_exchange(account_id):
    try:
        account = get_exchange_account_for_user(account_id, g.user['id'])
        if not account:
            return jsonify({'success': False, 'error': 'Exchange account not found'}), 404
        if not account.get('is_active'):
            return jsonify({'success': False, 'error': 'Exchange account is inactive'}), 400

        api_key = decrypt_secret(account['api_key_encrypted'])
        secret_key = decrypt_secret(account['secret_key_encrypted'])
        verify = validate_exchange_credentials(account['exchange'], api_key, secret_key)

        update_exchange_account_status(
            account_id,
            g.user['id'],
            can_trade=verify.get('can_trade', False),
            can_withdraw=verify.get('can_withdraw', False),
            permissions_verified=verify.get('permissions_verified', False),
            last_error=verify.get('error', ''),
        )

        if not verify['success']:
            return jsonify({
                'success': False,
                'error': verify.get('error') or 'Credential verification failed',
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


@app.route('/api/byok/orders', methods=['POST'])
@require_auth
def byok_place_order():
    """Place an order using the authenticated user's linked exchange keys."""
    try:
        payload = request.get_json(silent=True) or {}
        exchange_account_id = payload.get('exchange_account_id')
        symbol = (payload.get('symbol') or '').strip()
        side = (payload.get('side') or 'buy').strip().lower()
        order_type = (payload.get('order_type') or 'market').strip().lower()
        quantity = float(payload.get('quantity', 0) or 0)
        price = payload.get('price')
        reduce_only = bool(payload.get('reduce_only', False))

        if not exchange_account_id:
            return jsonify({'success': False, 'error': 'exchange_account_id is required'}), 400
        if not symbol or side not in {'buy', 'sell'} or order_type not in {'market', 'limit'}:
            return jsonify({'success': False, 'error': 'Invalid symbol/side/order_type'}), 400
        if quantity <= 0 or quantity > MAX_ORDER_QTY:
            return jsonify({'success': False, 'error': f'quantity must be in range (0, {MAX_ORDER_QTY}]'}), 400
        if order_type == 'limit':
            if price is None:
                return jsonify({'success': False, 'error': 'price is required for limit orders'}), 400
            price = float(price)
            if price <= 0:
                return jsonify({'success': False, 'error': 'price must be > 0'}), 400
        else:
            price = None

        account = get_exchange_account_for_user(exchange_account_id, g.user['id'])
        if not account:
            return jsonify({'success': False, 'error': 'Exchange account not found'}), 404
        if not account.get('is_active', False):
            return jsonify({'success': False, 'error': 'Exchange account is inactive'}), 400
        if not account.get('can_trade', False):
            return jsonify({'success': False, 'error': 'Trading permission unavailable'}), 400
        if account.get('can_withdraw', False):
            return jsonify({'success': False, 'error': 'Withdrawal-enabled keys are not allowed'}), 400

        api_key = decrypt_secret(account['api_key_encrypted'])
        secret_key = decrypt_secret(account['secret_key_encrypted'])
        exchange_client = get_exchange_client(account['exchange'], api_key, secret_key)
        if exchange_client is None:
            return jsonify({'success': False, 'error': 'Exchange adapter not available'}), 400

        result = exchange_client.place_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            reduce_only=reduce_only,
        )
        if not result:
            err = (exchange_client.last_error or 'Order placement failed').strip()
            update_exchange_account_status(
                exchange_account_id,
                g.user['id'],
                last_error=err[:400],
            )
            return jsonify({'success': False, 'error': err[:400]}), 400

        result_payload = result.get('result') if isinstance(result, dict) else None
        exchange_order_id = (
            (result_payload or {}).get('id')
            or (result_payload or {}).get('order_id')
            or f"byok_{int(time.time() * 1000)}"
        )

        order_record = {
            'user_id': g.user['id'],
            'exchange_account_id': int(exchange_account_id),
            'order_id': str(exchange_order_id),
            'symbol': symbol,
            'side': side,
            'order_type': order_type,
            'quantity': quantity,
            'price': price,
            'status': 'submitted',
            'exchange_response': json.dumps(result)[:5000],
            'timestamp': datetime.now().isoformat(),
        }
        save_byok_order_entry(order_record)

        return jsonify({
            'success': True,
            'message': 'BYOK order placed',
            'data': {
                'order_id': order_record['order_id'],
                'exchange_account_id': exchange_account_id,
                'status': 'submitted',
                'exchange': account['exchange'],
                'raw': result,
            }
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
    try:
        exchange_account_id = request.args.get('exchange_account_id')
        if not exchange_account_id:
            return jsonify({'success': False, 'error': 'exchange_account_id is required'}), 400

        account = get_exchange_account_for_user(int(exchange_account_id), g.user['id'])
        if not account:
            return jsonify({'success': False, 'error': 'Exchange account not found'}), 404
        if not account.get('is_active', False):
            return jsonify({'success': False, 'error': 'Exchange account is inactive'}), 400

        api_key = decrypt_secret(account['api_key_encrypted'])
        secret_key = decrypt_secret(account['secret_key_encrypted'])
        exchange_client = get_exchange_client(account['exchange'], api_key, secret_key)
        if exchange_client is None:
            return jsonify({'success': False, 'error': 'Exchange adapter not available'}), 400

        symbol = (request.args.get('underlying_asset_symbol') or 'BTC').strip().upper()
        positions = exchange_client.get_positions(underlying_asset_symbol=symbol)
        if positions is None:
            err = (exchange_client.last_error or 'Could not fetch positions').strip()
            update_exchange_account_status(
                account['id'],
                g.user['id'],
                last_error=err[:400],
            )
            return jsonify({'success': False, 'error': err[:400]}), 400
        return jsonify({'success': True, 'data': positions})
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
        exchange_client = get_exchange_client(account['exchange'], api_key, secret_key)
        if exchange_client is None:
            return jsonify({'success': False, 'error': 'Exchange adapter not available'}), 400

        result = exchange_client.cancel_order(order_id)
        if not result:
            err = (exchange_client.last_error or 'Order cancel failed').strip()
            update_exchange_account_status(
                account['id'],
                g.user['id'],
                last_error=err[:400],
            )
            return jsonify({'success': False, 'error': err[:400]}), 400

        return jsonify({'success': True, 'message': 'Order cancel request submitted', 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/broker-login', methods=['POST'])
def broker_login():
    """Delta Exchange Demo broker login endpoint"""
    try:
        data = request.get_json()
        api_key = data.get('api_key', '')
        secret_key = data.get('secret_key', '')
        broker = data.get('broker', 'delta_demo')
        
        if not api_key or not secret_key:
            return jsonify({
                'success': False,
                'error': 'API Key aur Secret Key required hain'
            }), 400
        
        login_time = save_broker_login_entry(
            broker=broker,
            api_key=api_key,
            secret_key=secret_key,
            ip_address=request.remote_addr
        )

        broker_entry = {
            'broker': broker,
            'api_key': api_key,
            'secret_key': secret_key,
            'login_time': login_time.isoformat(),
            'ip_address': request.remote_addr
        }

        print(f"✅ Broker login recorded in DB: {broker} at {broker_entry['login_time']}")
        
        return jsonify({
            'success': True,
            'message': 'Broker login successful',
            'broker': broker,
            'redirect_url': 'https://demo.delta.exchange/app/futures/trade/ETH/ETHUSD'
        })
        
    except Exception as e:
        print(f"❌ Broker login error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


def get_delta_client():
    """Delta India client — same static keys as CryptoAPIClient (no login / broker DB)."""
    return DeltaExchangeClient(API_KEY, SECRET_KEY)


# Delta error code -> (reason slug, user-facing message template).
# Ye sab "keys setup/usable nahi hain" wale cases hain — inka matlab "no open positions" NAHI hai.
DELTA_SETUP_ERRORS = {
    'ip_not_whitelisted_for_api_key': (
        'ip_not_whitelisted',
        'Delta API key is server ke IP ({client_ip}) se allowed nahi hai. '
        'Delta Exchange > API Keys mein ye IP whitelist karein, tabhi live positions aayengi.',
    ),
    'invalid_api_key': (
        'invalid_api_key',
        'Delta API key invalid hai. Backend mein valid Delta India API key aur secret set karein.',
    ),
    'unauthorized_api_access': (
        'unauthorized',
        'Is Delta API key ko positions read karne ki permission nahi hai. '
        'Delta par key ke liye read permission enable karein.',
    ),
    'expired_signature': (
        'expired_signature',
        'Delta signature expire ho gaya (server clock out of sync lag raha hai). '
        'System time sync karke dubara try karein.',
    ),
    'SignatureMismatch': (
        'signature_mismatch',
        'Delta signature match nahi hua — API secret galat lag raha hai. Key/secret dubara check karein.',
    ),
}


def classify_delta_error(raw_error):
    """
    Delta ke raw error body ko structured reason + readable message mein badalta hai.

    Returns dict: {reason, code, message, client_ip, needs_setup}
    `needs_setup=True` ka matlab: credentials/config problem hai, transient failure nahi.
    """
    raw = (raw_error or '').strip()
    code = ''
    client_ip = None

    if raw.startswith('{'):
        try:
            parsed = json.loads(raw)
            err = parsed.get('error')
            if isinstance(err, dict):
                code = str(err.get('code') or '')
                context = err.get('context')
                if isinstance(context, dict):
                    client_ip = context.get('client_ip')
            elif isinstance(err, str):
                code = err
        except Exception:
            pass

    if not code:
        # Non-JSON body (connection errors etc.) — substring par fall back karo.
        for known in DELTA_SETUP_ERRORS:
            if known in raw:
                code = known
                break

    entry = DELTA_SETUP_ERRORS.get(code)
    if entry:
        reason, template = entry
        return {
            'reason': reason,
            'code': code,
            'message': template.format(client_ip=client_ip or 'unknown'),
            'client_ip': client_ip,
            'needs_setup': True,
        }

    return {
        'reason': 'upstream_error',
        'code': code,
        'message': (raw[:300] if raw else 'Delta se positions fetch nahi hui'),
        'client_ip': client_ip,
        'needs_setup': False,
    }


def normalize_delta_positions(payload):
    """Delta ke {success, result} response ko flat positions list bana deta hai."""
    if payload is None:
        return []
    result = payload.get('result', payload) if isinstance(payload, dict) else payload
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        return []
    return [p for p in result if isinstance(p, dict)]


def delta_underlying_asset(symbol):
    """'BTCUSDT' / 'BTCUSD' -> 'BTC'. Quote suffix na mile to symbol as-is."""
    s = (symbol or '').strip().upper()
    if not s:
        return None
    for quote in ('USDT', 'USDC', 'USD', 'INR'):
        if s.endswith(quote) and len(s) > len(quote):
            return s[:-len(quote)]
    return s


@app.route('/api/delta/market-data', methods=['GET'])
def get_delta_market_data():
    """Delta Exchange market data fetch karta hai"""
    try:
        symbol = request.args.get('symbol', None)
        delta_client = get_delta_client()
        data = delta_client.get_market_data(symbol)
        
        if data:
            return jsonify({
                'success': True,
                'data': data
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Market data fetch nahi hua'
            }), 400
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/delta/positions', methods=['GET'])
def get_delta_positions():
    """
    Delta Exchange positions fetch karta hai.

    Response hamesha structured hota hai taki UI bata sake ki positions kyun nahi aayi:
        {success, configured, reason, message, data: [...]}

    `configured: false` => Delta API keys set/usable nahi hain. Data khali hai, lekin
    ye "koi open position nahi hai" nahi hai — UI ko ye farq dikhana chahiye.

    Query params (optional):
        symbol=BTCUSDT | underlying_asset_symbol=BTC | product_id=27
    Kuch na mile to Delta client BTC underlying par default karta hai.
    """
    product_id = (request.args.get('product_id') or '').strip() or None
    underlying = (request.args.get('underlying_asset_symbol') or '').strip().upper() or None
    if not underlying and not product_id:
        underlying = delta_underlying_asset(request.args.get('symbol'))

    if not API_KEY or not SECRET_KEY:
        return jsonify({
            'success': True,
            'configured': False,
            'reason': 'missing_credentials',
            'message': 'Delta API key/secret configure nahi hain, isliye live positions load nahi ho sakti.',
            'data': []
        })

    try:
        delta_client = get_delta_client()
        data = delta_client.get_positions(
            underlying_asset_symbol=underlying,
            product_id=product_id
        )
    except Exception as e:
        return jsonify({
            'success': False,
            'configured': True,
            'reason': 'request_failed',
            'message': f'Delta se connect nahi ho paya: {e}',
            'error': str(e),
            'data': []
        }), 502

    if data is not None:
        return jsonify({
            'success': True,
            'configured': True,
            'reason': None,
            'message': None,
            'data': normalize_delta_positions(data)
        })

    info = classify_delta_error(getattr(delta_client, 'last_error', None))
    payload = {
        'success': bool(info['needs_setup']),
        'configured': not info['needs_setup'],
        'reason': info['reason'],
        'message': info['message'],
        'data': []
    }
    if info.get('client_ip'):
        payload['client_ip'] = info['client_ip']

    if info['needs_setup']:
        # Setup problem hai, request problem nahi — 200 bhejo taki UI clear reason dikha sake
        # (400 ko frontend swallow kar leta tha aur panel chup-chaap khali dikhta tha).
        print(f"\u26a0\ufe0f Delta positions unavailable: {info['reason']} - {info['message']}")
        return jsonify(payload)

    payload['error'] = info['message']
    return jsonify(payload), 502


@app.route('/api/delta/orders', methods=['GET'])
def get_delta_orders():
    """Delta Exchange orders fetch karta hai"""
    try:
        symbol = request.args.get('symbol', None)
        delta_client = get_delta_client()
        data = delta_client.get_orders(symbol)
        
        if data:
            return jsonify({
                'success': True,
                'data': data
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Orders fetch nahi hui'
            }), 400
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/delta/place-order', methods=['POST'])
def place_delta_order():
    """Delta Exchange mein order place karta hai"""
    try:
        data = request.get_json()
        symbol = data.get('symbol', '')
        side = data.get('side', 'buy')  # 'buy' or 'sell'
        order_type = data.get('order_type', 'limit')  # 'limit' or 'market'
        quantity = float(data.get('quantity', 0))
        price = data.get('price', None)
        reduce_only = data.get('reduce_only', False)
        
        if not symbol or quantity <= 0:
            return jsonify({
                'success': False,
                'error': 'Symbol aur quantity required hain'
            }), 400
        
        delta_client = get_delta_client()
        result = delta_client.place_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=float(price) if price else None,
            reduce_only=reduce_only
        )
        
        if result:
            return jsonify({
                'success': True,
                'message': 'Order placed successfully',
                'data': result
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Order place nahi hua'
            }), 400
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/delta/cancel-order', methods=['POST'])
def cancel_delta_order():
    """Delta Exchange mein order cancel karta hai"""
    try:
        data = request.get_json()
        order_id = data.get('order_id', '')
        
        if not order_id:
            return jsonify({
                'success': False,
                'error': 'Order ID required hai'
            }), 400
        
        delta_client = get_delta_client()
        result = delta_client.cancel_order(order_id)
        
        if result:
            return jsonify({
                'success': True,
                'message': 'Order cancelled successfully',
                'data': result
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Order cancel nahi hua'
            }), 400
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/place-order', methods=['POST'])
def place_order():
    """Demo order placement endpoint"""
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

        save_demo_order_entry(order_entry)
        
        print(f"✅ Order placed: {side} {quantity} {symbol} @ {price or 'Market'}")
        
        return jsonify({
            'success': True,
            'message': 'Order placed successfully',
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
def get_orders():
    """Get all orders"""
    try:
        orders = fetch_recent_orders(limit=50)
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


@app.route('/api/health', methods=['GET'])
def health_check():
    """API health check"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat()
    })


@app.route('/')
def index():
    """Main page serve karta hai"""
    try:
        static_path = os.path.join(os.path.dirname(__file__), 'static', 'index.html')
        if os.path.exists(static_path):
            with open(static_path, 'r', encoding='utf-8') as f:
                html_content = f.read()
            return Response(html_content, mimetype='text/html')
        else:
            return f"File not found at: {static_path}", 404
    except Exception as e:
        return f"Error loading page: {str(e)}", 500


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
    print("   POST /api/login - App login")
    print("   POST /api/delta-demo-login - Delta Exchange Demo account login")
    print("   POST /api/broker-login - Delta Exchange broker login")
    print("   GET /api/delta/market-data - Delta Exchange market data")
    print("   GET /api/delta/positions - Delta Exchange positions")
    print("   GET /api/delta/orders - Delta Exchange orders")
    print("   POST /api/delta/place-order - Place order on Delta Exchange")
    print("   POST /api/delta/cancel-order - Cancel order on Delta Exchange")
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
