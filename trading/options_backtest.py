"""
Options strategies ka backtest — asli Delta option data par.

Perp candle engine options ko test nahi kar sakta: option ka P&L premium se
banta hai, aur premium IV, expiry tak ka time aur strike par chalta hai. Isliye
yahan har trade ke liye wahi contract dhoondha jaata hai jo us waqt Delta par
listed tha, aur uska premium path Delta ke MARK price candles se aata hai.

Trade (last-traded) candles nahi lete: zyaadatar ghanton mein option par koi
trade hota hi nahi, to wo flat purana price dohrate hain. Mark price har bar par
exchange ka fair value hai — SL/target usi par lagte hain, jaise Delta khud
positions ko mark karta hai.

Signal frontend ka wahi `analyze()` nikalta hai jo live card chalata hai, aur
entries yahan bhejta hai. Yahan sirf contract selection aur exits hote hain.
"""

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from flask import Blueprint, jsonify, request

from fetch_trading_data import BASE_URL, to_delta_symbol

OPTION_UNDERLYINGS = {"BTC", "ETH"}
MAX_DAYS = 60
MAX_ENTRIES = 3000

RESOLUTION_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400,
}

# Live endpoints (/api/options/chain|spread|condor) ke min_hours_to_expiry defaults.
LIVE_MIN_HOURS = {"single": 4.0, "spread": 4.0, "condor": 12.0}

_session = requests.Session()
_products_lock = threading.Lock()
_products_cache = {}
PRODUCTS_TTL = 600

_candles_lock = threading.Lock()
_candles_cache = {}
_CANDLES_CACHE_MAX = 4000


def _utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _get(path, params, attempts=4):
    """
    Delta GET with retry — ek backtest mein sau se zyada calls hoti hain, aur
    unmein se ek ka atakna poore run ko fail nahi karna chahiye.
    """
    last_error = None
    for attempt in range(attempts):
        try:
            res = _session.get(f"{BASE_URL}{path}", params=params, timeout=20)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            time.sleep(1.0 * (attempt + 1))
            continue
        if res.status_code == 429:
            reset_ms = res.headers.get("X-RATE-LIMIT-RESET")
            wait = min(10.0, (float(reset_ms) / 1000.0) if reset_ms else 1.5 * (attempt + 1))
            time.sleep(max(0.5, wait))
            last_error = RuntimeError("Delta rate limit — thodi der baad dobara chalao")
            continue
        if res.status_code >= 500:
            last_error = RuntimeError(f"Delta {res.status_code}")
            time.sleep(1.0 * (attempt + 1))
            continue
        res.raise_for_status()
        return res.json()
    raise RuntimeError(f"Delta se data nahi aaya ({last_error}) — thodi der baad dobara chalao")


# ── Contracts ─────────────────────────────────────────────


def _normalise_product(p):
    kind = p.get("contract_type")
    if kind not in ("call_options", "put_options"):
        return None
    expiry = _parse_iso(p.get("settlement_time"))
    strike = p.get("strike_price")
    if expiry is None or strike is None:
        return None
    return {
        "symbol": p.get("symbol"),
        "type": "call" if kind == "call_options" else "put",
        "strike": float(strike),
        "expiry": expiry,
        "launch": _parse_iso(p.get("launch_time")),
        "contract_value": float(p.get("contract_value") or 0.001),
    }


def _fetch_products(underlying, state, since):
    out = []
    after = None
    while True:
        params = {
            "contract_types": "call_options,put_options",
            "states": state,
            "underlying_asset_symbols": underlying,
            "page_size": 1000,
        }
        if after:
            params["after"] = after
        data = _get("/v2/products", params)
        rows = data.get("result") or []
        page = [p for p in (_normalise_product(r) for r in rows) if p]
        out.extend(page)
        after = (data.get("meta") or {}).get("after")
        # Expired list naye se purane ki taraf aati hai — window se peeche pahunch gaye to bas.
        if not after or not page or min(p["expiry"] for p in page) < since:
            break
    return out


def option_contracts(underlying, since):
    """`since` ke baad expire hone wale saare contracts (expired + live)."""
    with _products_lock:
        hit = _products_cache.get(underlying)
        fresh = hit and (time.time() - hit["at"]) < PRODUCTS_TTL and hit["since"] <= since
        if fresh:
            return hit["items"]
    items = _fetch_products(underlying, "expired", since) + _fetch_products(underlying, "live", since)
    seen = {}
    for item in items:
        seen[item["symbol"]] = item
    merged = [c for c in seen.values() if c["expiry"] >= since]
    with _products_lock:
        _products_cache[underlying] = {"at": time.time(), "since": since, "items": merged}
    return merged


# ── Mark price candles ────────────────────────────────────


def mark_candles(symbol, resolution, start_ts, end_ts):
    """MARK:<symbol> candles, purane se naye. Expired contracts ka data badalta nahi, isliye cache."""
    key = (symbol, resolution, start_ts, end_ts)
    with _candles_lock:
        if key in _candles_cache:
            return _candles_cache[key]
    step = RESOLUTION_SECONDS.get(resolution, 300)
    rows = []
    cursor = start_ts
    # Delta ek request mein ~2000 bars deta hai.
    chunk = step * 1900
    while cursor < end_ts:
        stop = min(end_ts, cursor + chunk)
        data = _get("/v2/history/candles", {
            "symbol": f"MARK:{symbol}",
            "resolution": resolution,
            "start": cursor,
            "end": stop,
        })
        rows.extend(data.get("result") or [])
        cursor = stop
    by_time = {}
    for r in rows:
        t = int(r["time"])
        if start_ts <= t <= end_ts:
            by_time[t] = {
                "time": t,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            }
    out = [by_time[t] for t in sorted(by_time)]
    with _candles_lock:
        if len(_candles_cache) > _CANDLES_CACHE_MAX:
            _candles_cache.clear()
        _candles_cache[key] = out
    return out


def _bar_at(candles, ts):
    for c in candles:
        if c["time"] >= ts:
            return c if c["time"] - ts < 3600 else None
    return None


# ── Black-Scholes (r = 0, jaisa crypto options mein chalta hai) ──


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot, strike, years, vol, is_call):
    if years <= 0 or vol <= 0:
        return max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
    sq = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * years) / sq
    d2 = d1 - sq
    if is_call:
        return spot * _ncdf(d1) - strike * _ncdf(d2)
    return strike * _ncdf(-d2) - spot * _ncdf(-d1)


def bs_delta(spot, strike, years, vol, is_call):
    if years <= 0 or vol <= 0:
        itm = spot > strike if is_call else spot < strike
        return (1.0 if is_call else -1.0) if itm else 0.0
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * years) / (vol * math.sqrt(years))
    return _ncdf(d1) if is_call else _ncdf(d1) - 1.0


def implied_vol(premium, spot, strike, years, is_call):
    intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
    if premium is None or years <= 0 or premium <= intrinsic + 1e-9:
        return None
    lo, hi = 0.01, 6.0
    if bs_price(spot, strike, years, hi, is_call) < premium:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, years, mid, is_call) > premium:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def _years(expiry, ts):
    return max(0.0, (expiry.timestamp() - ts) / (365.0 * 86400))


# ── Contract selection ────────────────────────────────────


class Skip(Exception):
    """Is entry par trade nahi ho sakta — reason result mein ginta hai."""


def _pick_expiry(contracts, entry_ts, min_hours):
    entry_dt = _utc(entry_ts)
    listed = [
        c for c in contracts
        if (c["launch"] is None or c["launch"] <= entry_dt)
        and (c["expiry"] - entry_dt).total_seconds() / 3600 >= min_hours
    ]
    if not listed:
        raise Skip("koi listed expiry nahi")
    nearest = min(c["expiry"] for c in listed)
    return nearest, [c for c in listed if c["expiry"] == nearest]


def _entry_mark(contract, entry_ts, resolution):
    step = RESOLUTION_SECONDS.get(resolution, 300)
    bars = mark_candles(contract["symbol"], resolution, entry_ts, entry_ts + step * 4)
    bar = _bar_at(bars, entry_ts)
    return bar["open"] if bar else None


def _atm_vol(chain, spot, entry_ts, resolution):
    """ATM contract ke mark se IV — baaki strikes ka delta isi se andaazan nikalta hai."""
    for c in sorted(chain, key=lambda c: abs(c["strike"] - spot))[:4]:
        premium = _entry_mark(c, entry_ts, resolution)
        vol = implied_vol(premium, spot, c["strike"], _years(c["expiry"], entry_ts), c["type"] == "call")
        if vol:
            return vol
    raise Skip("ATM mark price nahi mila")


def _delta_with_own_vol(contract, spot, entry_ts, resolution, fallback_vol):
    premium = _entry_mark(contract, entry_ts, resolution)
    years = _years(contract["expiry"], entry_ts)
    vol = implied_vol(premium, spot, contract["strike"], years, contract["type"] == "call") or fallback_vol
    return abs(bs_delta(spot, contract["strike"], years, vol, contract["type"] == "call")), premium


def _rank_by_delta(chain, spot, entry_ts, vol, target):
    def est(c):
        years = _years(c["expiry"], entry_ts)
        return abs(bs_delta(spot, c["strike"], years, vol, c["type"] == "call"))
    return sorted(chain, key=lambda c: abs(est(c) - target))


def select_single(contracts, entry, params, resolution):
    opt_type = "call" if entry["direction"] == "bull" else "put"
    dmin, dmax = sorted((abs(params["delta_min"]), abs(params["delta_max"])))
    min_hours = max(LIVE_MIN_HOURS["single"], params["time_exit_hours"] + 1)
    expiry, chain = _pick_expiry([c for c in contracts if c["type"] == opt_type], entry["time"], min_hours)
    vol = _atm_vol(chain, entry["spot"], entry["time"], resolution)
    for c in _rank_by_delta(chain, entry["spot"], entry["time"], vol, (dmin + dmax) / 2)[:4]:
        delta, premium = _delta_with_own_vol(c, entry["spot"], entry["time"], resolution, vol)
        if premium and dmin <= delta <= dmax:
            return expiry, [{"contract": c, "side": 1, "delta": delta}], vol
    raise Skip("delta band mein strike nahi")


def select_spread(contracts, entry, params, resolution):
    opt_type = "call" if entry["direction"] == "bull" else "put"
    min_hours = max(LIVE_MIN_HOURS["spread"], params["time_exit_hours"] + 1)
    expiry, chain = _pick_expiry([c for c in contracts if c["type"] == opt_type], entry["time"], min_hours)
    vol = _atm_vol(chain, entry["spot"], entry["time"], resolution)
    long_leg = _rank_by_delta(chain, entry["spot"], entry["time"], vol, abs(params["long_delta"]))[0]
    width = params["spread_width"]
    target = long_leg["strike"] + width if opt_type == "call" else long_leg["strike"] - width
    side = [c for c in chain if (c["strike"] > long_leg["strike"] if opt_type == "call" else c["strike"] < long_leg["strike"])]
    if not side:
        raise Skip("short leg strike nahi")
    short_leg = min(side, key=lambda c: abs(c["strike"] - target))
    return expiry, [
        {"contract": long_leg, "side": 1},
        {"contract": short_leg, "side": -1},
    ], vol


def select_condor(contracts, entry, params, resolution):
    min_hours = max(LIVE_MIN_HOURS["condor"], params["time_exit_hours"] + 1)
    expiry, chain = _pick_expiry(contracts, entry["time"], min_hours)
    calls = [c for c in chain if c["type"] == "call"]
    puts = [c for c in chain if c["type"] == "put"]
    if len(calls) < 2 or len(puts) < 2:
        raise Skip("condor ke liye strikes kam")
    vol = _atm_vol(chain, entry["spot"], entry["time"], resolution)
    if vol * 100 > params["max_iv_pct"]:
        raise Skip("IV max se upar")
    spot, ts = entry["spot"], entry["time"]
    short_call = _rank_by_delta(calls, spot, ts, vol, abs(params["short_delta"]))[0]
    short_put = _rank_by_delta(puts, spot, ts, vol, abs(params["short_delta"]))[0]
    outer_calls = [c for c in calls if c["strike"] > short_call["strike"]]
    outer_puts = [p for p in puts if p["strike"] < short_put["strike"]]
    if not outer_calls or not outer_puts or short_call["strike"] <= short_put["strike"]:
        raise Skip("condor wings nahi ban rahe")
    long_call = _rank_by_delta(outer_calls, spot, ts, vol, abs(params["long_delta"]))[0]
    long_put = _rank_by_delta(outer_puts, spot, ts, vol, abs(params["long_delta"]))[0]
    return expiry, [
        {"contract": short_call, "side": -1},
        {"contract": long_call, "side": 1},
        {"contract": short_put, "side": -1},
        {"contract": long_put, "side": 1},
    ], vol


SELECTORS = {"single": select_single, "spread": select_spread, "condor": select_condor}


# ── Exits ─────────────────────────────────────────────────


def _aligned_paths(legs, entry_ts, end_ts, resolution):
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(
            lambda leg: mark_candles(leg["contract"]["symbol"], resolution, entry_ts, end_ts),
            legs,
        ))
    if any(not p for p in paths):
        raise Skip("mark price data nahi mila")
    times = sorted({c["time"] for c in paths[0] if c["time"] >= entry_ts})
    maps = [{c["time"]: c for c in p} for p in paths]
    bars = []
    last = [None] * len(legs)
    for t in times:
        row = []
        for i, m in enumerate(maps):
            if t in m:
                last[i] = m[t]
            row.append(last[i])
        if all(row):
            bars.append((t, row))
    if not bars or bars[0][0] - entry_ts > RESOLUTION_SECONDS.get(resolution, 300) * 2:
        raise Skip("entry par mark price nahi")
    return bars


def _spot_path(symbol, resolution, start_ts, end_ts):
    try:
        return {c["time"]: c["close"] for c in mark_candles(to_delta_symbol(symbol), resolution, start_ts, end_ts)}
    except Exception:
        return {}


def simulate(kind, legs, expiry, entry, params, resolution, slip, symbol):
    entry_ts = entry["time"]
    deadline = int(expiry.timestamp() - params["time_exit_hours"] * 3600)
    last_ts = int(time.time())
    bars = _aligned_paths(legs, entry_ts, min(int(expiry.timestamp()), last_ts), resolution)

    def fill(leg_bars, field, opening):
        # Khareedne par mark se upar bharo, bechne par neeche milta hai — dono taraf aadha spread.
        total = 0.0
        for leg, bar in zip(legs, leg_bars):
            buying = (leg["side"] > 0) == opening
            price = bar[field] * (1 + slip if buying else 1 - slip)
            total += price * leg["side"]
        return total

    first_ts, first = bars[0]
    entry_value = fill(first, "open", True)

    def close_trade(ts, value, status, booked=0.0, booked_qty=0.0):
        qty = 1.0 - booked_qty
        pnl = booked + (value - entry_value) * qty
        return {"entry_value": entry_value, "exit_time": ts, "exit_value": value, "status": status, "pnl_per_unit": pnl}

    if kind == "single":
        sl_level = entry_value * (1 - params["sl_premium_pct"] / 100)
        tp1 = entry_value * (1 + params["tp1_pct"] / 100)
        tp2 = entry_value * (1 + params["tp2_pct"] / 100)
        booked, booked_qty = 0.0, 0.0
        for ts, row in bars:
            bar = row[0]
            if ts >= deadline:
                return close_trade(ts, bar["open"] * (1 - slip), "PARTIAL_THEN_TIME" if booked_qty else "TIME_EXIT", booked, booked_qty)
            # Ek hi bar mein SL aur target dono ho sakte hain — pehle SL maano (conservative).
            if bar["low"] <= sl_level:
                price = min(bar["open"], sl_level) * (1 - slip)
                return close_trade(ts, price, "PARTIAL_THEN_SL" if booked_qty else "SL_HIT", booked, booked_qty)
            if not booked_qty and bar["high"] >= tp1 and tp1 < tp2:
                booked = (tp1 * (1 - slip) - entry_value) * 0.5
                booked_qty = 0.5
            if bar["high"] >= tp2:
                return close_trade(ts, tp2 * (1 - slip), "TARGET_HIT", booked, booked_qty)
        ts, row = bars[-1]
        return close_trade(ts, row[0]["close"] * (1 - slip), "OPEN_AT_END", booked, booked_qty)

    if kind == "spread":
        width = abs(legs[0]["contract"]["strike"] - legs[1]["contract"]["strike"])
        max_profit = width - entry_value
        if entry_value <= 0 or max_profit <= 0:
            raise Skip("spread ka debit/max profit galat")
        sl_level = entry_value * (1 - params["sl_debit_pct"] / 100)
        tp_level = entry_value + max_profit * params["tp_max_profit_pct"] / 100
        for ts, row in bars:
            if ts >= deadline:
                return close_trade(ts, fill(row, "open", False), "TIME_EXIT")
            value = fill(row, "close", False)
            if value <= sl_level:
                return close_trade(ts, value, "SL_HIT")
            if value >= tp_level:
                return close_trade(ts, value, "TARGET_HIT")
        ts, row = bars[-1]
        return close_trade(ts, fill(row, "close", False), "OPEN_AT_END")

    # Condor: entry_value negative hai (credit mila). Close karne ki value bhi negative.
    credit = -entry_value
    if credit <= 0:
        raise Skip("condor par credit nahi")
    spots = _spot_path(symbol, resolution, entry_ts, bars[-1][0])
    for ts, row in bars:
        if ts >= deadline:
            return close_trade(ts, fill(row, "open", False), "TIME_EXIT")
        value = fill(row, "close", False)
        profit = value - entry_value
        if profit >= credit * params["tp_credit_pct"] / 100:
            return close_trade(ts, value, "TARGET_HIT")
        if -profit >= credit * params["sl_credit_mult"]:
            return close_trade(ts, value, "SL_HIT")
        spot = spots.get(ts)
        if spot:
            years = _years(expiry, ts)
            for leg, bar in zip(legs, row):
                if leg["side"] > 0:
                    continue
                c = leg["contract"]
                vol = implied_vol(bar["close"], spot, c["strike"], years, c["type"] == "call")
                if vol and abs(bs_delta(spot, c["strike"], years, vol, c["type"] == "call")) >= params["emergency_delta"]:
                    return close_trade(ts, value, "EMERGENCY_DELTA")
    ts, row = bars[-1]
    return close_trade(ts, fill(row, "close", False), "OPEN_AT_END")


# ── Result ────────────────────────────────────────────────


def _short_leg_name(leg):
    c = leg["contract"]
    return f"{'+' if leg['side'] > 0 else '-'}{c['type'][0].upper()}{int(c['strike'])}"


def _trade_side(kind, legs):
    if kind == "condor":
        return "iron_condor"
    t = legs[0]["contract"]["type"]
    if kind == "spread":
        return "bull_call_spread" if t == "call" else "bear_put_spread"
    return t


PARAM_DEFAULTS = {
    "single": {"delta_min": 0.25, "delta_max": 0.35, "sl_premium_pct": 45, "tp1_pct": 100, "tp2_pct": 200, "time_exit_hours": 2, "max_spread_pct": 5},
    "spread": {"spread_width": 2000, "long_delta": 0.5, "sl_debit_pct": 45, "tp_max_profit_pct": 75, "time_exit_hours": 1.5, "max_spread_pct": 8},
    "condor": {"short_delta": 0.175, "long_delta": 0.075, "max_iv_pct": 60, "tp_credit_pct": 50, "sl_credit_mult": 1.5, "emergency_delta": 0.35, "time_exit_hours": 12, "max_spread_pct": 10},
}


def run_options_backtest(body):
    kind = body.get("kind")
    if kind not in SELECTORS:
        raise ValueError("kind must be single, spread or condor")
    symbol = str(body.get("symbol") or "BTCUSDT").upper()
    underlying = symbol.replace("USDT", "").replace("USD", "")
    if underlying not in OPTION_UNDERLYINGS:
        raise ValueError(f"Delta par {underlying} ke options nahi hain — BTC ya ETH chuno")
    resolution = body.get("timeframe") or "5m"
    if resolution not in RESOLUTION_SECONDS:
        raise ValueError(f"Timeframe {resolution} supported nahi")
    lots = max(0.01, float(body.get("lots") or 1))
    raw = body.get("params") or {}
    params = {}
    for key, default in PARAM_DEFAULTS[kind].items():
        try:
            params[key] = float(raw.get(key, default))
        except (TypeError, ValueError):
            params[key] = float(default)
    slip = max(0.0, params["max_spread_pct"]) / 200.0

    entries = sorted(
        (
            {"time": int(e["time"]) // 1000, "direction": e.get("direction"), "spot": float(e["spot"])}
            for e in (body.get("entries") or [])[:MAX_ENTRIES]
            if e.get("time") and e.get("spot")
        ),
        key=lambda e: e["time"],
    )
    first_ts = int(body.get("start") or 0) // 1000 or (entries[0]["time"] if entries else int(time.time()))
    contracts = option_contracts(underlying, _utc(first_ts) - timedelta(days=1)) if entries else []
    contract_value = contracts[0]["contract_value"] if contracts else 0.001

    trades, skipped = [], {}
    busy_until = 0
    for entry in entries:
        if entry["time"] < busy_until:
            continue
        try:
            expiry, legs, vol = SELECTORS[kind](contracts, entry, params, resolution)
            outcome = simulate(kind, legs, expiry, entry, params, resolution, slip, symbol)
        except Skip as reason:
            skipped[str(reason)] = skipped.get(str(reason), 0) + 1
            continue
        except (RuntimeError, requests.RequestException) as e:
            print(f"⚠️ Options backtest entry {entry['time']} skipped: {e}")
            skipped["Delta se data nahi aaya"] = skipped.get("Delta se data nahi aaya", 0) + 1
            continue
        busy_until = outcome["exit_time"]
        pnl_usd = outcome["pnl_per_unit"] * contract_value * lots
        trades.append({
            "entry_time": entry["time"] * 1000,
            "exit_time": outcome["exit_time"] * 1000,
            "side": _trade_side(kind, legs),
            "instrument": " ".join(_short_leg_name(l) for l in legs) + f" · {expiry.strftime('%d %b')}",
            "symbols": [l["contract"]["symbol"] for l in legs],
            "spot": round(entry["spot"], 2),
            "iv": round(vol * 100, 1),
            # Premium / debit / credit — 1 BTC (ya ETH) notional par, Delta quotes jaisa.
            "entry_price": round(abs(outcome["entry_value"]), 2),
            "exit_price": round(abs(outcome["exit_value"]), 2),
            "pnl_points": round(outcome["pnl_per_unit"], 2),
            "pnl": round(pnl_usd, 4),
            "status": outcome["status"],
        })

    settled = [t for t in trades if t["status"] != "OPEN_AT_END"]
    wins = sum(1 for t in settled if t["pnl"] > 0)
    losses = sum(1 for t in settled if t["pnl"] <= 0)
    return {
        "success": True,
        "strategy": body.get("strategy_name") or kind,
        "symbol": symbol,
        "timeframe": resolution,
        "days_requested": body.get("days"),
        "days_covered": body.get("days_covered"),
        "total_candles": body.get("total_candles"),
        "total_signals": len(entries),
        "total_trades": len(trades),
        "winning_trades": wins,
        "losing_trades": losses,
        "target_hits": sum(1 for t in trades if t["status"] == "TARGET_HIT"),
        "sl_hits": sum(1 for t in trades if "SL" in t["status"] or t["status"] == "EMERGENCY_DELTA"),
        "win_rate": round(wins / len(settled) * 100, 2) if settled else 0,
        "total_profit": round(sum(t["pnl"] for t in trades), 4),
        "lots": lots,
        "trades": trades,
        "options": {
            "kind": kind,
            "underlying": underlying,
            "contract_value": contract_value,
            "slippage_pct_per_side": round(slip * 100, 3),
            "skipped": sum(skipped.values()),
            "skip_reasons": skipped,
        },
    }


def create_options_backtest_blueprint(client):
    bp = Blueprint("options_backtest", __name__)

    @bp.route("/api/options/backtest/candles", methods=["GET"])
    def options_backtest_candles():
        """Signal replay ke liye perp candles — /api/candles ki 4000-bar limit ke bina."""
        try:
            symbol = request.args.get("symbol", "BTCUSDT")
            timeframe = request.args.get("timeframe", "5m")
            days = int(request.args.get("days", 14))
            if not 1 <= days <= MAX_DAYS:
                return jsonify({"success": False, "error": f"Options backtest 1-{MAX_DAYS} din ka hi hota hai"}), 400
            data = client.get_historical_data_batch(symbol=symbol, interval=timeframe, days=days, exchange_name="delta")
            df = (data or {}).get("dataframe")
            if df is None or len(df) == 0:
                return jsonify({"success": False, "error": "Is period ki candles nahi mili"}), 400
            candles = [
                {
                    "time": int(pd.Timestamp(row["Open Time"]).timestamp() * 1000),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row["Volume"]),
                }
                for _, row in df.iterrows()
            ]
            candles.sort(key=lambda c: c["time"])
            return jsonify({"success": True, "symbol": symbol, "timeframe": timeframe, "candles": candles})
        except Exception as e:
            print(f"❌ Options backtest candles error: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    @bp.route("/api/options/backtest", methods=["POST"])
    def options_backtest():
        try:
            return jsonify(run_options_backtest(request.get_json(silent=True) or {}))
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        except Exception as e:
            print(f"❌ Options backtest error: {e}")
            return jsonify({"success": False, "error": f"Options backtest fail: {e}"}), 500

    return bp
