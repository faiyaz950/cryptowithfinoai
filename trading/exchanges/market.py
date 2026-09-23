"""
Chart ka data kis exchange se aaye.

Trading adapter (`base.py`) ko user ki key chahiye; ye layer usse alag hai —
candles aur bhaav sabke liye khule hain, koi login nahi lagta. Alag isliye
rakha hai ki chart bina login bhi chalta hai, aur uske liye kisi ki key
maangna galat hota.

Zarurat kyun padi: har exchange ka apna bhaav hota hai. Ek hi waqt par BTC
Delta par $84,570 tha aur CoinDCX par $84,356 — $214 ka farak. Jo user
CoinDCX par trade karta hai, usse Delta ka chart dikhana matlab galat level
dikhana.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from .base import fnum

TIMEOUT = 15


class MarketSource:
    """
    Ek exchange ka public market data.

    `candles()` aur `ticker()` fail hone par `None` dete hain, khaali list
    nahi — "data nahi mila" aur "koi candle hi nahi" do alag baatein hain.
    Candles hamesha **purani se nayi** taraf, aur time **milliseconds** mein.
    """

    id: str = ""
    name: str = ""
    # UI ka interval -> exchange ka apna naam. Jo yahan nahi hai, wo us
    # exchange par mumkin hi nahi — UI use button hi nahi dikhata.
    INTERVALS: Dict[str, str] = {}

    @classmethod
    def intervals(cls) -> List[str]:
        return list(cls.INTERVALS)

    @classmethod
    def to_symbol(cls, ui_symbol: str) -> Optional[str]:
        raise NotImplementedError

    @classmethod
    def candles(cls, ui_symbol: str, interval: str, limit: int) -> Optional[List[Dict[str, Any]]]:
        raise NotImplementedError

    @classmethod
    def ticker(cls, ui_symbol: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    @staticmethod
    def candle(t, o, h, l, c, v) -> Dict[str, Any]:
        return {
            "time": int(t),
            "open": fnum(o),
            "high": fnum(h),
            "low": fnum(l),
            "close": fnum(c),
            "volume": fnum(v),
        }


# ── CoinDCX ──────────────────────────────────────────────

_CDX_PAIRS: Dict[str, Any] = {"at": 0.0, "map": {}}
_CDX_PAIRS_TTL = 300.0


def _coindcx_pairs() -> Dict[str, str]:
    """
    Plain symbol ("BTCUSDT") se CoinDCX ka pair ("B-BTC_USDT").

    Mapping khud CoinDCX se aati hai — uske har price entry mein `mkt` field
    plain naam hota hai. Naam khud banane ki koshish ("B-" + base + "_USDT")
    541 pairs mein kahin na kahin galat nikalti, isliye jo wo bhejta hai wahi
    maante hain.
    """
    now = time.time()
    if _CDX_PAIRS["map"] and (now - _CDX_PAIRS["at"]) < _CDX_PAIRS_TTL:
        return _CDX_PAIRS["map"]
    try:
        res = requests.get(
            "https://public.coindcx.com/market_data/v3/current_prices/futures/rt",
            timeout=TIMEOUT,
        )
        res.raise_for_status()
        prices = (res.json() or {}).get("prices") or {}
        mapping = {}
        for pair, row in prices.items():
            mkt = str((row or {}).get("mkt") or "").upper()
            if mkt:
                mapping[mkt] = pair
        if mapping:
            _CDX_PAIRS.update(at=now, map=mapping)
    except Exception:
        pass
    return _CDX_PAIRS["map"]


class CoinDCXMarket(MarketSource):
    id = "coindcx"
    name = "CoinDCX"
    # 3m yahan jaanbujh kar nahi hai — CoinDCX us resolution par 400 deta
    # hai. 1m se khud 3m banana mumkin hai par usme galti ke mauke hain;
    # button na dikhana zyada saaf hai.
    INTERVALS = {
        "1m": "1", "5m": "5", "15m": "15", "30m": "30",
        "1h": "60", "4h": "240", "1d": "1D",
    }
    _MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}

    @classmethod
    def to_symbol(cls, ui_symbol: str) -> Optional[str]:
        return _coindcx_pairs().get((ui_symbol or "").upper())

    @classmethod
    def candles(cls, ui_symbol, interval, limit):
        pair = cls.to_symbol(ui_symbol)
        resolution = cls.INTERVALS.get(interval)
        if not pair or not resolution:
            return None
        minutes = cls._MINUTES.get(interval, 60)
        now = int(time.time())
        # Thoda extra maangte hain — exchange kabhi-kabhi kam bhejta hai.
        span = int(minutes * 60 * (limit + 5))
        try:
            res = requests.get(
                "https://public.coindcx.com/market_data/candlesticks",
                params={"pair": pair, "from": now - span, "to": now,
                        "resolution": resolution, "pcode": "f"},
                timeout=TIMEOUT,
            )
            res.raise_for_status()
            body = res.json() or {}
            if body.get("s") not in (None, "ok"):
                return None
            rows = body.get("data") or []
        except Exception:
            return None

        out = [cls.candle(r.get("time"), r.get("open"), r.get("high"),
                          r.get("low"), r.get("close"), r.get("volume"))
               for r in rows if isinstance(r, dict) and r.get("time")]
        out.sort(key=lambda c: c["time"])
        return out[-limit:]

    @classmethod
    def ticker(cls, ui_symbol):
        pair = cls.to_symbol(ui_symbol)
        if not pair:
            return None
        try:
            res = requests.get(
                "https://public.coindcx.com/market_data/v3/current_prices/futures/rt",
                timeout=TIMEOUT,
            )
            res.raise_for_status()
            row = ((res.json() or {}).get("prices") or {}).get(pair)
        except Exception:
            return None
        if not row:
            return None
        return {
            "price": fnum(row.get("ls")),
            "high_24h": fnum(row.get("h")),
            "low_24h": fnum(row.get("l")),
            # `pc` pehle se percent mein hai.
            "change_24h": fnum(row.get("pc")),
            # `v` quote currency mein turnover hai, base coins mein nahi.
            "turnover_24h": fnum(row.get("v")),
            "mark_price": fnum(row.get("mp")),
        }


# ── Bybit ────────────────────────────────────────────────

class BybitMarket(MarketSource):
    id = "bybit"
    name = "Bybit"
    INTERVALS = {
        "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
        "1h": "60", "4h": "240", "1d": "D",
    }

    @classmethod
    def to_symbol(cls, ui_symbol: str) -> Optional[str]:
        # Bybit ke linear perps ka naam wahi hai jo desk use karta hai.
        return (ui_symbol or "").upper() or None

    @classmethod
    def candles(cls, ui_symbol, interval, limit):
        symbol = cls.to_symbol(ui_symbol)
        resolution = cls.INTERVALS.get(interval)
        if not symbol or not resolution:
            return None
        try:
            res = requests.get(
                "https://api.bybit.com/v5/market/kline",
                params={"category": "linear", "symbol": symbol,
                        "interval": resolution, "limit": min(limit, 1000)},
                timeout=TIMEOUT,
            )
            res.raise_for_status()
            body = res.json() or {}
            if body.get("retCode") != 0:
                return None
            rows = (body.get("result") or {}).get("list") or []
        except Exception:
            return None

        # Bybit nayi candle pehle bhejta hai aur har value string hoti hai.
        out = [cls.candle(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows if len(r) >= 6]
        out.sort(key=lambda c: c["time"])
        return out[-limit:]

    @classmethod
    def ticker(cls, ui_symbol):
        symbol = cls.to_symbol(ui_symbol)
        if not symbol:
            return None
        try:
            res = requests.get(
                "https://api.bybit.com/v5/market/tickers",
                params={"category": "linear", "symbol": symbol},
                timeout=TIMEOUT,
            )
            res.raise_for_status()
            body = res.json() or {}
            if body.get("retCode") != 0:
                return None
            row = ((body.get("result") or {}).get("list") or [None])[0]
        except Exception:
            return None
        if not row:
            return None
        return {
            "price": fnum(row.get("lastPrice")),
            "high_24h": fnum(row.get("highPrice24h")),
            "low_24h": fnum(row.get("lowPrice24h")),
            # Bybit ratio deta hai (-0.0215), percent nahi.
            "change_24h": fnum(row.get("price24hPcnt")) * 100,
            "volume_24h": fnum(row.get("volume24h")),
            "turnover_24h": fnum(row.get("turnover24h")),
            "mark_price": fnum(row.get("markPrice")),
        }


# ── Delta ────────────────────────────────────────────────
#
# Candles ka mushkil hissa (product_id, batching, retries) pehle se
# `fetch_trading_data.DeltaExchangeClient` mein hai aur asal use mein test ho
# chuka hai — dobara likhne ka koi fayda nahi. Yahan usi ko wrap kiya hai.

_DELTA_BASE = "https://api.india.delta.exchange"


class DeltaMarket(MarketSource):
    id = "delta"
    name = "Delta Exchange India"
    INTERVALS = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1h", "4h": "4h", "1d": "1d",
    }

    @classmethod
    def to_symbol(cls, ui_symbol: str) -> Optional[str]:
        sym = (ui_symbol or "").upper()
        # Delta India par USDT nahi, USD-settled perps hain: BTCUSDT -> BTCUSD
        return sym[:-1] if sym.endswith("USDT") else sym or None

    @classmethod
    def candles(cls, ui_symbol, interval, limit):
        if interval not in cls.INTERVALS:
            return None
        try:
            # Lazy import — market layer ko trading client ke boot par depend
            # nahi karna chahiye, aur circular import bhi nahi aana chahiye.
            from fetch_trading_data import DeltaExchangeClient

            out = DeltaExchangeClient("", "").get_historical_data(
                ui_symbol, interval, limit,
            )
        except Exception:
            return None
        if not out or out.get("dataframe") is None or len(out["dataframe"]) == 0:
            return None

        df = out["dataframe"]
        rows: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            ts = row.get("Open Time")
            if ts is None:
                continue
            # DataFrame mein Timestamp hota hai; frontend ms chahta hai.
            try:
                ms = int(pd_timestamp_ms(ts))
            except Exception:
                continue
            rows.append(cls.candle(
                ms, row.get("Open"), row.get("High"),
                row.get("Low"), row.get("Close"), row.get("Volume"),
            ))
        rows.sort(key=lambda c: c["time"])
        return rows[-limit:] if limit else rows

    @classmethod
    def ticker(cls, ui_symbol):
        symbol = cls.to_symbol(ui_symbol)
        if not symbol:
            return None
        try:
            res = requests.get(
                f"{_DELTA_BASE}/v2/tickers/{symbol}",
                timeout=TIMEOUT,
            )
            res.raise_for_status()
            t = (res.json() or {}).get("result") or {}
        except Exception:
            return None
        price = fnum(t.get("close")) or fnum(t.get("mark_price")) or fnum(t.get("spot_price"))
        high = fnum(t.get("high")) or fnum(t.get("mark_high_24h"))
        low = fnum(t.get("low")) or fnum(t.get("mark_low_24h"))
        if not price or not high or not low:
            return None
        return {
            "price": price,
            "high_24h": high,
            "low_24h": low,
            "change_24h": fnum(t.get("ltp_change_24h"), fnum(t.get("mark_change_24h"))),
            "volume_24h": fnum(t.get("volume")),
            "turnover_24h": fnum(t.get("turnover_usd"), fnum(t.get("turnover"))),
            "mark_price": fnum(t.get("mark_price")),
        }


def pd_timestamp_ms(ts) -> int:
    """pandas Timestamp / datetime / int → milliseconds."""
    if isinstance(ts, (int, float)):
        v = float(ts)
        if v < 1e11:  # seconds
            return int(v * 1000)
        if v < 1e14:  # ms
            return int(v)
        return int(v / 1000)  # µs
    # pandas Timestamp / datetime
    try:
        return int(ts.timestamp() * 1000)
    except Exception:
        import pandas as pd
        return int(pd.Timestamp(ts).timestamp() * 1000)


MARKET_SOURCES: Dict[str, type] = {
    DeltaMarket.id: DeltaMarket,
    CoinDCXMarket.id: CoinDCXMarket,
    BybitMarket.id: BybitMarket,
}


def get_market_source(exchange: str) -> Optional[type]:
    return MARKET_SOURCES.get((exchange or "").strip().lower())


def market_catalogue() -> List[Dict[str, Any]]:
    """UI ko: chart kis-kis exchange ka dikh sakta hai, aur kaun se timeframe."""
    return [
        {"id": cls.id, "name": cls.name, "intervals": cls.intervals()}
        for cls in MARKET_SOURCES.values()
    ]
