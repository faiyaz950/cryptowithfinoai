#!/usr/bin/env python3
"""
Script to fetch Trading Data and Historical Data from Delta Exchange (India API).
Includes: REST API, WebSocket real-time data, and Backtrader-compatible data.
"""

import os
import requests
import hmac
import hashlib
import time
import json
from datetime import datetime, timedelta
from urllib.parse import urlencode
import pandas as pd

try:
    import websocket
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# API credentials (Delta Exchange India) — supplied via environment.
# Set DELTA_API_KEY / DELTA_SECRET_KEY in a local .env (see .env.example)
# or in your host's environment settings. Never hardcode them here.
API_KEY = os.environ.get("DELTA_API_KEY", "")
SECRET_KEY = os.environ.get("DELTA_SECRET_KEY", "")

# Delta Exchange India base URL
BASE_URL = "https://api.india.delta.exchange"
WS_URL = "wss://socket.india.delta.exchange:2096"

# Delta India uses BTCUSD, ETHUSD (no T). Map common symbols.
DELTA_SYMBOL_MAP = {
    'BTCUSDT': 'BTCUSD',
    'ETHUSDT': 'ETHUSD',
    'BNBUSDT': 'BNBUSD',
    'ADAUSDT': 'ADAUSD',
    'SOLUSDT': 'SOLUSD',
}


def to_delta_symbol(symbol):
    """
    Frontend <COIN>USDT bhejta hai, Delta India <COIN>USD use karta hai.
    Explicit map pehle, warna trailing 'T' hata do (1000PEPEUSDT -> 1000PEPEUSD).
    Isse har naye coin ke liye map mein entry daalne ki zarurat nahi padti.
    """
    s = str(symbol or '').upper()
    if s in DELTA_SYMBOL_MAP:
        return DELTA_SYMBOL_MAP[s]
    if s.endswith('USDT'):
        return s[:-1]
    return s


def generate_signature_india(
    api_secret,
    method,
    path,
    query_string="",
    body="",
    timestamp=None,
    *,
    style="timestamp_first",
):
    """India API signature helper."""
    if timestamp is None:
        timestamp = str(int(time.time() * 1000))
    method_upper = method.upper()
    if style == "method_first":
        # Some Delta environments expect: METHOD + TIMESTAMP + PATH + QUERY + BODY
        message = method_upper + timestamp + path + query_string + body
    else:
        # Common format: TIMESTAMP + METHOD + PATH + QUERY + BODY
        message = timestamp + method_upper + path + query_string + body
    signature = hmac.new(api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return timestamp, signature


# Default request timeout and retries for public API
CANDLES_TIMEOUT = 45
CANDLES_RETRIES = 3
CANDLES_BATCH_LIMIT = 500  # Max candles per request (Delta may cap this)
AUTH_TIMEOUT = 12


class DeltaExchangeClient:
    """Delta Exchange India API Client"""
    
    def __init__(self, api_key, secret_key):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = BASE_URL
        self._product_id_cache = {}  # symbol -> product_id
        self.last_error = None  # last API error (e.g. 401 expired_signature)
    
    def _get_product_id(self, symbol):
        """
        Resolve symbol to Delta product_id. Uses GET /v2/products/{symbol} or /v2/products list.
        Caches result. Returns None if not found.
        """
        delta_symbol = self._delta_symbol(symbol).upper()
        if delta_symbol in self._product_id_cache:
            return self._product_id_cache[delta_symbol]
        headers = {'User-Agent': 'python-crypto-client/1.0', 'Accept': 'application/json'}
        # Try GET /v2/products/{symbol} first (official doc: Get product by symbol)
        try:
            path = f"/v2/products/{delta_symbol}"
            r = requests.get(self.base_url + path, headers=headers, timeout=CANDLES_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                if data.get('success') and data.get('result'):
                    pid = data['result'].get('id')
                    if pid is not None:
                        self._product_id_cache[delta_symbol] = int(pid)
                        return self._product_id_cache[delta_symbol]
        except Exception as e:
            print(f"   ⚠️ Product by symbol failed: {e}")
        # Fallback: fetch products list and find by symbol
        try:
            r = requests.get(
                self.base_url + "/v2/products",
                params={"contract_types": "perpetual_futures", "states": "live"},
                headers=headers,
                timeout=CANDLES_TIMEOUT
            )
            if r.status_code == 200:
                data = r.json()
                if data.get('success') and isinstance(data.get('result'), list):
                    for p in data['result']:
                        if (p.get('symbol') or '').upper() == delta_symbol:
                            pid = p.get('id')
                            if pid is not None:
                                self._product_id_cache[delta_symbol] = int(pid)
                                return self._product_id_cache[delta_symbol]
        except Exception as e:
            print(f"   ⚠️ Products list failed: {e}")
        return None
    
    def _generate_signature(
        self,
        method,
        path,
        query_string,
        body=None,
        timestamp=None,
        *,
        style="timestamp_first",
        include_query=True,
        include_body=True,
    ):
        """HMAC SHA256 signature for authenticated requests."""
        if timestamp is None:
            timestamp = str(int(time.time() * 1000))
        method_upper = method.upper()
        if style == "method_first":
            message = method_upper + timestamp + path
        else:
            message = timestamp + method_upper + path
        if include_query and query_string:
            # Delta signature_data includes query with leading '?'.
            message += f"?{query_string}"
        if include_body and body:
            body_str = json.dumps(body, separators=(',', ':')) if isinstance(body, dict) else str(body)
            message += body_str
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature, timestamp
    
    def _make_request(self, method, endpoint, params=None, body=None):
        """Authenticated request (for private endpoints)"""
        path = endpoint
        query_string = urlencode(params) if params else ""
        url = f"{self.base_url}{endpoint}"
        if query_string:
            url += f"?{query_string}"

        def do_request(timestamp_value, signature_style, include_query=True, include_body=True):
            signature, _ = self._generate_signature(
                method,
                path,
                query_string,
                body,
                timestamp=timestamp_value,
                style=signature_style,
                include_query=include_query,
                include_body=include_body,
            )
            headers = {
                'api-key': self.api_key,
                'timestamp': timestamp_value,
                'signature': signature,
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            if method == 'GET':
                return requests.get(url, headers=headers, timeout=AUTH_TIMEOUT)
            if method == 'POST':
                return requests.post(url, headers=headers, json=body, timeout=AUTH_TIMEOUT)
            if method == 'DELETE':
                return requests.delete(url, headers=headers, timeout=AUTH_TIMEOUT)
            return requests.request(method, url, headers=headers, json=body, timeout=AUTH_TIMEOUT)

        def parse_server_time_candidates(response_text):
            """
            Parse server_time from error JSON and return candidate timestamps.
            """
            candidates = []
            try:
                data = json.loads(response_text or "{}")
                context = (data.get("error") or {}).get("context") or {}
                server_time = context.get("server_time")
                if server_time is not None:
                    server_time = int(server_time)
                    # Delta private auth works reliably with seconds timestamps.
                    candidates.extend([
                        str(server_time - 1),
                        str(server_time),
                        str(server_time + 1),
                    ])
            except Exception:
                pass
            return candidates

        # Retry across known timestamp/signature formats used by Delta variants.
        # Primary mode is method-first + seconds because error context shows:
        # signature_data => GET1772987178/v2/positions
        timestamp_ms = str(int(time.time() * 1000))
        timestamp_sec = str(int(time.time()))
        attempts = [
            (timestamp_sec, "method_first", False, False),  # strongest match with Delta signature_data context
            (timestamp_sec, "method_first", True, True),
            (timestamp_sec, "timestamp_first", True, True),
        ]
        added_server_time_attempts = False
        max_attempts = 10

        idx = 0
        while idx < len(attempts) and idx < max_attempts:
            ts, style, use_query, use_body = attempts[idx]
            try:
                response = do_request(ts, style, include_query=use_query, include_body=use_body)
                if response.status_code == 200:
                    self.last_error = None
                    return response.json()
                body_text = response.text or ""
                self.last_error = body_text
                sig_error = ("expired_signature" in body_text) or ("Signature Mismatch" in body_text)
                if sig_error:
                    if not added_server_time_attempts:
                        server_ts_candidates = parse_server_time_candidates(body_text)
                        if server_ts_candidates:
                            # Prefer server_time seconds first, then milliseconds.
                            for ts in server_ts_candidates:
                                attempts.extend([
                                    (ts, "method_first", False, False),
                                    (ts, "method_first", True, True),
                                    (ts, "timestamp_first", True, True),
                                ])
                        added_server_time_attempts = True
                    print(f"⚠️ Retrying authenticated request (style={style}, ts_len={len(ts)}, q={use_query}, b={use_body})")
                    idx += 1
                    continue
                print(f"❌ Error: {response.status_code} - {body_text[:200]}")
                return None
            except Exception as e:
                self.last_error = str(e)
                if idx < len(attempts) - 1 and idx < max_attempts - 1:
                    idx += 1
                    continue
                print(f"❌ Error: {e}")
                return None
            idx += 1
        if self.last_error:
            print(f"❌ Auth request failed after retries: {self.last_error[:200]}")
        return None
    
    def get_market_data(self, symbol=None):
        """Market data fetch karta hai"""
        endpoint = "/v2/tickers"
        params = {}
        if symbol:
            params['symbol'] = symbol
        return self._make_request('GET', endpoint, params)

    def get_account_profile(self):
        """
        Fetch user/profile/api-key details from private endpoints if available.
        Returns merged dict (best effort), or {} when all endpoints fail.
        """
        candidates = [
            "/v2/users/me",
            "/v2/user",
            "/v2/users/self",
            "/v2/profile",
            "/v2/user/profile",
            "/v2/account",
            "/v2/accounts",
            # Some Delta variants expose API-key metadata here.
            "/v2/api_keys",
            "/v2/api-keys",
            "/v2/api_keys/me",
        ]
        merged = {}
        for endpoint in candidates:
            data = self._make_request('GET', endpoint)
            if data:
                result = data.get('result') if isinstance(data, dict) else None
                if isinstance(result, dict):
                    merged.update(result)
                elif isinstance(result, list) and result:
                    # Keep full list for deep field extraction upstream.
                    merged.setdefault("api_keys", result)
                    first_item = result[0]
                    if isinstance(first_item, dict):
                        merged.update(first_item)
                elif isinstance(data, dict):
                    merged.update(data)
        return merged

    def get_wallet_balances(self):
        """
        Fetch wallet/balance details from private endpoints (best effort).
        Returns dict/list payload if available, else {}.
        """
        candidates = [
            "/v2/wallet/balances",
            "/v2/wallet/balance",
            "/v2/balances",
            "/v2/wallet",
        ]
        for endpoint in candidates:
            data = self._make_request('GET', endpoint)
            if not data:
                continue
            if isinstance(data, dict):
                result = data.get('result')
                if isinstance(result, (dict, list)) and result:
                    return result
                return data
            if isinstance(data, list) and data:
                return data
        return {}
    
    def _delta_symbol(self, symbol):
        """Delta India uses BTCUSD not BTCUSDT."""
        return to_delta_symbol(symbol)
    
    def _request_candles_once(self, path, params, headers):
        """Single candles request with timeout. Returns (candles_list, None) or (None, error_msg)."""
        try:
            response = requests.get(
                self.base_url + path,
                params=params,
                headers=headers,
                timeout=CANDLES_TIMEOUT
            )
            if response.status_code != 200:
                return None, f"{response.status_code} - {response.text[:300]}"
            data = response.json()
            if not data.get('success', True) or 'result' not in data:
                err = data.get('error') or data.get('message') or list(data.keys())
                return None, f"No result: {err}"
            result = data['result']
            if isinstance(result, dict):
                candles = result.get('candles') or result.get('candle') or []
            elif isinstance(result, list):
                candles = result
            else:
                candles = []
            if candles is None:
                candles = []
            return candles, None
        except requests.exceptions.Timeout:
            return None, "Timeout"
        except Exception as e:
            return None, str(e)
    
    def _candles_to_dataframe(self, candles, symbol, resolution):
        """Convert raw candle list to DataFrame with Open Time, Open, High, Low, Close, Volume."""
        df_data = []
        for c in candles:
            t = c.get('time', c.get('t'))
            if isinstance(t, (int, float)):
                if t < 1e10:
                    t = pd.to_datetime(t, unit='s')
                elif t < 1e15:
                    t = pd.to_datetime(t, unit='ms')
                else:
                    t = pd.to_datetime(t, unit='us')
            else:
                t = pd.to_datetime(t)
            df_data.append({
                'Open Time': t,
                'Open': float(c.get('open', c.get('o', 0))),
                'High': float(c.get('high', c.get('h', 0))),
                'Low': float(c.get('low', c.get('l', 0))),
                'Close': float(c.get('close', c.get('c', 0))),
                'Volume': float(c.get('volume', c.get('v', 0)))
            })
        return pd.DataFrame(df_data)
    
    def fetch_historical_data_public(self, symbol, resolution, from_timestamp_ms, to_timestamp_ms=None):
        """
        Historical OHLC - Public endpoint (no auth). India API.
        Tries product_id and symbol; from/to in seconds, ms, then microseconds; retries and batch fetch.
        from_timestamp_ms, to_timestamp_ms: Unix time in milliseconds.
        resolution: '1m','5m','15m','30m','1h','2h','4h','6h','12h','1d','1w','1M'
        Returns: dict with 'dataframe', 'raw_data', 'symbol', 'interval', 'count'
        """
        if to_timestamp_ms is None:
            to_timestamp_ms = int(time.time() * 1000)
        delta_symbol = self._delta_symbol(symbol)
        path = "/v2/history/candles"
        headers = {'User-Agent': 'python-crypto-client/1.0', 'Accept': 'application/json'}
        
        from_sec = int(from_timestamp_ms / 1000)
        to_sec = int(to_timestamp_ms / 1000)
        from_us = from_timestamp_ms * 1000
        to_us = to_timestamp_ms * 1000
        
        # Build param variants: prefer product_id (Delta doc), fallback symbol
        product_id = self._get_product_id(symbol)
        param_sets = []
        # Delta India candles API expects "start" and "end" (not from/to)
        if product_id is not None:
            param_sets.append({"product_id": product_id, "resolution": resolution, "start": from_sec, "end": to_sec})
            param_sets.append({"product_id": product_id, "resolution": resolution, "start": from_timestamp_ms, "end": to_timestamp_ms})
            param_sets.append({"product_id": product_id, "resolution": resolution, "start": from_us, "end": to_us})
        param_sets.append({"symbol": delta_symbol, "resolution": resolution, "start": from_sec, "end": to_sec})
        param_sets.append({"symbol": delta_symbol, "resolution": resolution, "start": from_timestamp_ms, "end": to_timestamp_ms})
        param_sets.append({"symbol": delta_symbol, "resolution": resolution, "start": from_us, "end": to_us})
        
        all_candles = []
        last_error = None
        for attempt in range(CANDLES_RETRIES):
            for params in param_sets:
                candles, err = self._request_candles_once(path, params, headers)
                if err:
                    last_error = err
                    continue
                if candles:
                    all_candles = candles
                    break
            if all_candles:
                break
            if attempt < CANDLES_RETRIES - 1:
                time.sleep(1.0 * (attempt + 1))
        
        if not all_candles:
            if last_error:
                print(f"❌ Candles API failed: {last_error}")
            return {
                'dataframe': pd.DataFrame(),
                'raw_data': [],
                'symbol': symbol,
                'interval': resolution,
                'count': 0
            }
        
        # Batch fetch: if API returned a full batch, fetch older chunks until we have enough or no more
        def _ts_to_sec(t):
            if t is None or not isinstance(t, (int, float)):
                return None
            if t < 1e10:
                return int(t)
            if t < 1e15:
                return int(t / 1000)
            return int(t / 1_000_000)
        
        while len(all_candles) >= CANDLES_BATCH_LIMIT:
            # Assume ascending order (oldest first); oldest is first
            oldest = all_candles[0]
            oldest_sec = _ts_to_sec(oldest.get('time', oldest.get('t')))
            if oldest_sec is None or oldest_sec <= from_sec:
                break
            to_older = oldest_sec - 1
            params_next = ({"product_id": product_id} if product_id else {"symbol": delta_symbol})
            params_next["resolution"] = resolution
            params_next["start"] = from_sec
            params_next["end"] = to_older
            more, err = self._request_candles_once(path, params_next, headers)
            if err or not more:
                break
            # Prepend older candles (avoid duplicates by time)
            existing_ts = {_ts_to_sec(c.get('time', c.get('t'))) for c in all_candles}
            for c in more:
                s = _ts_to_sec(c.get('time', c.get('t')))
                if s is not None and s not in existing_ts:
                    existing_ts.add(s)
                    all_candles.insert(0, c)
            if len(more) < CANDLES_BATCH_LIMIT:
                break
            time.sleep(0.3)
        
        df = self._candles_to_dataframe(all_candles, symbol, resolution)
        if len(df) > 0:
            df = df.sort_values('Open Time').drop_duplicates(subset=['Open Time']).reset_index(drop=True)
        return {
            'dataframe': df,
            'raw_data': all_candles,
            'symbol': symbol,
            'interval': resolution,
            'count': len(df)
        }
    
    def get_historical_data(self, symbol, interval="1h", limit=100, start_time=None, end_time=None):
        """Historical OHLCV - uses from/to (India API public). Backward compatible."""
        end_time = end_time or datetime.now()
        if start_time and end_time:
            from_ts = int(start_time.timestamp() * 1000)
            to_ts = int(end_time.timestamp() * 1000)
        else:
            to_ts = int(end_time.timestamp() * 1000)
            interval_minutes = {'1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30,
                '1h': 60, '2h': 120, '4h': 240, '6h': 360, '12h': 720, '1d': 1440, '1w': 10080, '1M': 43200}
            mins = interval_minutes.get(interval, 60)
            from_ts = to_ts - (limit * mins * 60 * 1000)
        if start_time:
            print(f"      📅 Requesting from: {start_time} to {end_time}")
        out = self.fetch_historical_data_public(symbol, interval, from_ts, to_ts)
        if out and out['dataframe'] is not None and len(out['dataframe']) > 0:
            print(f"      ✅ Received {out['count']} candles")
        return out
    
    def get_positions(self, underlying_asset_symbol=None, product_id=None):
        """
        Current positions fetch karta hai.
        Delta may require one of: product_id or underlying_asset_symbol.
        """
        endpoint = "/v2/positions"
        params = {}
        if product_id is not None:
            params["product_id"] = product_id
        elif underlying_asset_symbol:
            params["underlying_asset_symbol"] = str(underlying_asset_symbol).upper()
        else:
            # Safe default for auth probe / broad positions
            params["underlying_asset_symbol"] = "BTC"
        return self._make_request('GET', endpoint, params=params)
    
    def place_order(self, symbol, side, order_type, quantity, price=None, reduce_only=False):
        """Order place karta hai"""
        endpoint = "/v2/orders"
        body = {
            'product_id': symbol,
            'side': side,  # 'buy' or 'sell'
            'order_type': order_type,  # 'limit', 'market', etc.
            'size': quantity,
            'reduce_only': reduce_only
        }
        
        if price and order_type == 'limit':
            body['limit_price'] = price
        
        return self._make_request('POST', endpoint, body=body)
    
    def get_orders(self, symbol=None):
        """Open orders fetch karta hai"""
        endpoint = "/v2/orders"
        params = {}
        if symbol:
            params['product_id'] = symbol
        
        return self._make_request('GET', endpoint, params)
    
    def cancel_order(self, order_id):
        """Order cancel karta hai"""
        endpoint = f"/v2/orders/{order_id}"
        return self._make_request('DELETE', endpoint)


class CryptoAPIClient:
    """Generic Crypto API Client for Trading and Historical Data"""
    
    def __init__(self, api_key, secret_key, exchange="auto"):
        self.api_key = api_key
        self.secret_key = secret_key
        self.exchange = exchange
        self.base_url = None
        
    def get_historical_data(self, symbol="BTCUSDT", interval="1h", limit=100, exchange_name=None):
        """
        Historical Data (OHLCV/Candlestick) fetch karta hai - Delta Exchange only
        """
        print(f"📈 Fetching historical data: {symbol}, {interval}, {limit} candles (Delta Exchange)")
        delta_client = DeltaExchangeClient(self.api_key, self.secret_key)
        return delta_client.get_historical_data(symbol, interval, limit)
    
    def get_historical_data_batch(self, symbol="BTCUSDT", interval="1h", days=30, exchange_name=None):
        """
        Fetch historical data for specified days in batches - Delta Exchange only
        Returns all candles for the specified period
        """
        # Calculate total candles needed
        interval_minutes = {
            '1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30,
            '1h': 60, '2h': 120, '4h': 240, '6h': 360, '8h': 480, '12h': 720,
            '1d': 1440, '3d': 4320, '1w': 10080, '1M': 43200
        }
        
        minutes_per_candle = interval_minutes.get(interval, 60)
        total_candles_needed = int((days * 24 * 60) / minutes_per_candle)
        
        print(f"📈 Fetching {total_candles_needed} candles ({days} days) for {symbol}, {interval} (Delta Exchange)")
        
        all_dataframes = []
        
        if True:  # Delta Exchange only
            # Delta Exchange - fetch data for requested time range
            # IMPORTANT: If API ignores time params, we'll filter client-side
            delta_client = DeltaExchangeClient(self.api_key, self.secret_key)
            
            # Calculate target time range
            end_time_target = datetime.now()
            start_time_target = end_time_target - pd.Timedelta(days=days)
            
            print(f"   🎯 Target: {days} days of data from {start_time_target} to {end_time_target}")
            
            # Fetch in multiple time-windows to work around exchange per-request limits.
            # 5m and lower timeframes usually return ~4000 candles per call on Delta.
            batch_limit = min(total_candles_needed, 5000)
            current_end = end_time_target
            current_start = start_time_target
            estimated_batch_capacity = 4000 if interval in {'1m', '3m', '5m'} else 5000
            estimated_batches_needed = (total_candles_needed + estimated_batch_capacity - 1) // estimated_batch_capacity
            max_batches = max(20, min(estimated_batches_needed + 5, 400))  # adaptive with safety cap
            batch_num = 0
            
            while batch_num < max_batches:
                batch_num += 1
                print(f"   Fetching batch {batch_num}: {current_start} to {current_end}")
                batch_data = delta_client.get_historical_data(
                    symbol,
                    interval,
                    batch_limit,
                    start_time=current_start,
                    end_time=current_end
                )
                if not batch_data or 'dataframe' not in batch_data:
                    if batch_num == 1:
                        print(f"   ⚠️ Failed to fetch data from Delta Exchange")
                    break
                df = batch_data['dataframe']
                if len(df) == 0:
                    if batch_num == 1:
                        print(f"   ⚠️ Empty dataframe received")
                    break
                received_start = df['Open Time'].min()
                received_end = df['Open Time'].max()
                print(f"   ✅ Got {len(df)} candles (range: {received_start} to {received_end})")
                all_dataframes.append(df)
                total_so_far = sum(len(d) for d in all_dataframes)
                if total_so_far >= total_candles_needed:
                    print(f"   ✅ Have enough candles ({total_so_far} >= {total_candles_needed})")
                    break
                if received_start <= start_time_target:
                    print(f"   Reached requested start date; no more older data.")
                    break
                # Next window: fetch older data (before received_start)
                current_end = received_start - pd.Timedelta(minutes=1)
                current_start = start_time_target
                if current_end <= current_start:
                    break
                time.sleep(0.4)
        
        if not all_dataframes:
            return None
        
        # Combine all dataframes
        if not all_dataframes:
            print(f"⚠️ No dataframes to combine")
            return None
        
        combined_df = pd.concat(all_dataframes, ignore_index=True)
        
        # Remove duplicates and sort by time (oldest to newest)
        combined_df = combined_df.drop_duplicates(subset=['Open Time'])
        combined_df = combined_df.sort_values('Open Time').reset_index(drop=True)
        
        # CRITICAL: Filter by time range, not just count
        # Calculate the exact time range we need (last N days from now)
        end_time_target = datetime.now()
        start_time_target = end_time_target - pd.Timedelta(days=days)
        
        print(f"\n   🔍 TIME-BASED FILTERING:")
        print(f"   Target time range: {start_time_target} to {end_time_target}")
        print(f"   Requested: {days} days")
        print(f"   Data available: {combined_df['Open Time'].min()} to {combined_df['Open Time'].max()}")
        
        # Calculate available data span
        available_span = (combined_df['Open Time'].max() - combined_df['Open Time'].min()).total_seconds() / (60 * 60 * 24)
        print(f"   Available data span: {available_span:.2f} days")
        
        # Filter to only include candles within the requested time range
        mask = (combined_df['Open Time'] >= start_time_target) & (combined_df['Open Time'] <= end_time_target)
        filtered_df = combined_df[mask].copy()
        
        print(f"   ✅ After time filtering: {len(filtered_df)} candles (from {len(combined_df)} total)")
        
        if len(filtered_df) == 0:
            print(f"   ⚠️ WARNING: No data in requested time range!")
            print(f"   Using all available data instead")
            filtered_df = combined_df.copy()
        elif len(filtered_df) < len(combined_df) * 0.1:
            print(f"   ⚠️ WARNING: Very little data in requested range ({len(filtered_df)}/{len(combined_df)})")
        
        # If we still have more than needed, take the most recent ones
        if len(filtered_df) > total_candles_needed:
            print(f"   Trimming from {len(filtered_df)} to {total_candles_needed} candles (most recent)")
            filtered_df = filtered_df.tail(total_candles_needed).reset_index(drop=True)
        elif len(filtered_df) < total_candles_needed:
            print(f"   ⚠️ Got {len(filtered_df)} candles after time filter, but requested {total_candles_needed} candles")
            print(f"   Using all available data in time range ({len(filtered_df)} candles)")
        else:
            print(f"   ✅ Got exactly {len(filtered_df)} candles as requested")
        
        combined_df = filtered_df
        
        # Calculate actual days covered
        actual_days = 0
        if len(combined_df) > 0:
            start_time = combined_df['Open Time'].min()
            end_time = combined_df['Open Time'].max()
            actual_days = (end_time - start_time).total_seconds() / (60 * 60 * 24)
            print(f"✅ Final result: {len(combined_df)} candles covering {actual_days:.2f} days")
            print(f"   Requested: {days} days, Got: {actual_days:.2f} days")
            print(f"   Data range: {start_time} to {end_time}")
            
            # Warn if we got significantly less data than requested
            if actual_days < days * 0.5:
                print(f"   ⚠️ WARNING: Got only {actual_days:.2f} days but requested {days} days!")
                print(f"   This might indicate API limitations or insufficient historical data")
        else:
            print(f"⚠️ No candles in combined dataframe")
            return None
        
        return {
            'dataframe': combined_df,
            'symbol': symbol,
            'interval': interval,
            'count': len(combined_df),
            'days': days,
            'actual_days': actual_days
        }


# ---------------------------------------------------------------------------
# Delta Exchange India - WebSocket (real-time data)
# ---------------------------------------------------------------------------

class DeltaWebSocket:
    """
    Real-time data via WebSocket - Delta Exchange India.
    wss://socket.india.delta.exchange:2096
    """
    
    def __init__(self, symbols=None, on_message=None, on_error=None, on_close=None):
        self.ws_url = WS_URL
        self.symbols = symbols or ["BTCUSDT"]
        self.on_message_cb = on_message or self._default_on_message
        self.on_error_cb = on_error or self._default_on_error
        self.on_close_cb = on_close or self._default_on_close
        self.ws = None
    
    def _default_on_message(self, ws, message):
        data = json.loads(message)
        print("Real-time data:", data)
    
    def _default_on_error(self, ws, error):
        print("WebSocket Error:", error)
    
    def _default_on_close(self, ws, close_status_code, close_reason):
        print("WebSocket closed")
    
    def _on_open(self, ws):
        subscribe_msg = {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {"name": "ticker", "symbols": self.symbols},
                    {"name": "l2_orderbook", "symbols": self.symbols},
                    {"name": "trades", "symbols": self.symbols}
                ]
            }
        }
        ws.send(json.dumps(subscribe_msg))
    
    def run_forever(self):
        if not HAS_WEBSOCKET:
            print("❌ Install websocket-client: pip install websocket-client")
            return
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self.on_message_cb,
            on_error=self.on_error_cb,
            on_close=self.on_close_cb
        )
        self.ws.run_forever()
    
    def run_in_thread(self):
        """Start WebSocket in a background thread."""
        if not HAS_WEBSOCKET:
            print("❌ Install websocket-client: pip install websocket-client")
            return
        import threading
        t = threading.Thread(target=self.run_forever, daemon=True)
        t.start()
        return t
