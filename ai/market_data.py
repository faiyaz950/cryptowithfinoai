"""Live market data via Yahoo Finance (stocks/indices) and CoinGecko (crypto)."""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

# ── Stock / index aliases (longest match first at runtime) ───────────────────

STOCK_ALIASES: dict[str, str] = {
    "hdfc bank": "HDFCBANK",
    "icici bank": "ICICIBANK",
    "axis bank": "AXISBANK",
    "kotak mahindra": "KOTAKBANK",
    "bajaj finance": "BAJFINANCE",
    "bajaj auto": "BAJAJ-AUTO",
    "tata motors": "TATAMOTORS",
    "tata steel": "TATASTEEL",
    "tata power": "TATAPOWER",
    "tata consultancy": "TCS",
    "adani ports": "ADANIPORTS",
    "adani green": "ADANIGREEN",
    "adani enterprises": "ADANIENT",
    "asian paints": "ASIANPAINT",
    "hindustan unilever": "HINDUNILVR",
    "sun pharma": "SUNPHARMA",
    "ultratech cement": "ULTRACEMCO",
    "power grid": "POWERGRID",
    "bank nifty": "^NSEBANK",
    "nifty bank": "^NSEBANK",
    "nifty 50": "^NSEI",
    "nifty50": "^NSEI",
    "reliance": "RELIANCE",
    "infosys": "INFY",
    "wipro": "WIPRO",
    "maruti": "MARUTI",
    "hul": "HINDUNILVR",
    "itc": "ITC",
    "hdfc": "HDFCBANK",
    "icici": "ICICIBANK",
    "sbi": "SBIN",
    "tcs": "TCS",
    "adani": "ADANIENT",
    "bajaj": "BAJFINANCE",
    "kotak": "KOTAKBANK",
    "nifty": "^NSEI",
    "sensex": "^BSESN",
}

CRYPTO_ALIASES: dict[str, str] = {
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "solana": "solana",
    "sol": "solana",
    "ripple": "ripple",
    "xrp": "ripple",
    "cardano": "cardano",
    "ada": "cardano",
    "dogecoin": "dogecoin",
    "doge": "dogecoin",
    "polygon": "matic-network",
    "matic": "matic-network",
    "bnb": "binancecoin",
    "shiba": "shiba-inu",
    "shib": "shiba-inu",
}

_LIVE_DATA_HINTS = re.compile(
    r"\b(price|rate|level|kitne|kitna|aaj|today|live|current|cmp|nav|"
    r"technical|rsi|macd|support|resistance|analysis|chart|trading|"
    r"nifty|sensex|stock|share|crypto|bitcoin|btc|eth|"
    r"kharid|buy|recommend|suggest|pick|kaunsa|konsa|sahi|accha|best)\b",
    re.I,
)

_BUY_INTENT = re.compile(
    r"\b(kharid|buy|purchase|recommend|salah|suggest|pick|kaunsa|kaun sa|konsa|kon sa|"
    r"best stock|accha stock|sahi rahega|sahi hai|invest kar|len|le lu|kharidu)\b",
    re.I,
)

# Nifty 50 heavyweights — scanned for "which stock to buy" questions
NIFTY_HEAVYWEIGHTS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "ITC",
    "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK", "MARUTI", "HINDUNILVR",
    "BAJFINANCE", "WIPRO", "SUNPHARMA", "TATAMOTORS", "ADANIENT",
]

_SORTED_STOCK_ALIASES = sorted(STOCK_ALIASES.items(), key=lambda x: len(x[0]), reverse=True)
_SORTED_CRYPTO_ALIASES = sorted(CRYPTO_ALIASES.items(), key=lambda x: len(x[0]), reverse=True)


def needs_live_data(question: str) -> bool:
    return (
        bool(_LIVE_DATA_HINTS.search(question))
        or is_stock_pick_question(question)
        or is_market_news_question(question)
    )


def is_market_news_question(question: str) -> bool:
    q = question.lower()
    return bool(re.search(r"\b(news|khabar|headline|update|major|market mood|aaj market)\b", q, re.I))


def is_stock_pick_question(question: str) -> bool:
    q = question.lower()
    has_buy_intent = bool(_BUY_INTENT.search(q))
    has_stock_context = bool(re.search(r"\b(stock|share|equity|nifty|bse|nse)\b", q, re.I))
    has_today = bool(re.search(r"\b(aaj|today|abhi)\b", q, re.I))
    return has_buy_intent and (has_stock_context or has_today)


# Common finance/market acronyms that are NOT stock tickers — excluded so questions like
# "RBI ka repo rate kya hai" don't trigger a bogus Yahoo Finance lookup for "RBI.NS".
_NON_TICKER_ACRONYMS = {
    "RSI", "MACD", "EMA", "SMA", "IPO", "NSE", "BSE", "FII", "DII", "PE", "ROE",
    "RBI", "SEBI", "GDP", "EMI", "ROI", "CAGR", "NAV", "SIP", "STP", "SWP",
    "IDCW", "NFO", "AMC", "ELSS", "ETF", "USD", "INR", "USA", "GST", "TDS",
    "LTCG", "STCG", "HUF", "KYC", "NPS", "PPF", "FD", "RD", "ATM", "UPI",
    "NEFT", "RTGS", "IMPS", "CEO", "CFO", "COO", "MD", "GMP", "SGB", "MCX",
    "NCDEX", "YOY", "QOQ", "CAPEX", "OPEX", "EBITDA", "ROCE", "EPS", "P/E",
    "F&O", "OI", "AI", "US", "UK", "EU", "IT", "OK", "FY", "QTR",
}


def _detect_stock_symbols(question: str) -> list[str]:
    q = question.lower()
    found: list[str] = []
    seen: set[str] = set()

    for alias, symbol in _SORTED_STOCK_ALIASES:
        if alias in q and symbol not in seen:
            found.append(symbol)
            seen.add(symbol)

    # Explicit NSE tickers (2–15 uppercase letters)
    for match in re.findall(r"\b([A-Z]{2,15})\b", question):
        if match not in seen and match not in _NON_TICKER_ACRONYMS:
            found.append(match)
            seen.add(match)

    return found[:5]


def _detect_crypto_ids(question: str) -> list[str]:
    q = question.lower()
    found: list[str] = []
    seen: set[str] = set()

    for alias, coin_id in _SORTED_CRYPTO_ALIASES:
        if re.search(r"\b" + re.escape(alias) + r"\b", q) and coin_id not in seen:
            found.append(coin_id)
            seen.add(coin_id)

    return found[:3]


def _yahoo_symbol(sym: str) -> str:
    if sym.startswith("^"):
        return sym
    return f"{sym}.NS"


_QUOTE_CACHE: dict[str, tuple[float, Optional[dict]]] = {}
_QUOTE_CACHE_TTL = 20  # seconds — short-lived so quotes stay fresh, but avoids re-fetching
# the same symbol multiple times per second (e.g. the 21-symbol market overview, or
# several users asking about the same stock within the same few seconds).


def _fetch_yahoo_chart(yahoo_sym: str) -> Optional[dict]:
    cached = _QUOTE_CACHE.get(yahoo_sym)
    if cached and (time.monotonic() - cached[0]) < _QUOTE_CACHE_TTL:
        return cached[1]
    data = _fetch_yahoo_chart_uncached(yahoo_sym)
    _QUOTE_CACHE[yahoo_sym] = (time.monotonic(), data)
    return data


def _fetch_yahoo_chart_uncached(yahoo_sym: str) -> Optional[dict]:
    try:
        resp = requests.get(
            f"https://query2.finance.yahoo.com/v8/finance/chart/{yahoo_sym}",
            params={"interval": "1d", "range": "5d"},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=8,
        )
        resp.raise_for_status()
        result = resp.json().get("chart", {}).get("result", [])
        if not result:
            return None
        meta = result[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        if price is None:
            return None
        prev = meta.get("chartPreviousClose") or meta.get("previousClose") or price
        change_pct = ((price - prev) / prev * 100) if prev else 0
        raw_sym = meta.get("symbol", yahoo_sym).replace(".NS", "")
        return {
            "key": raw_sym,
            "name": meta.get("longName") or meta.get("shortName", raw_sym),
            "price": price,
            "change": price - prev if prev else 0,
            "change_pct": change_pct,
            "day_high": meta.get("regularMarketDayHigh"),
            "day_low": meta.get("regularMarketDayLow"),
            "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
            "fifty_two_week_low": meta.get("fiftyTwoWeekLow"),
            "volume": meta.get("regularMarketVolume"),
            "market_cap": meta.get("marketCap"),
            "pe_ratio": meta.get("trailingPE"),
            "currency": meta.get("currency", "INR"),
            "market_state": meta.get("marketState", meta.get("exchangeTimezoneName", "")),
        }
    except Exception:
        return None


def fetch_yahoo_quotes(symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}

    result: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_yahoo_chart, _yahoo_symbol(sym)): sym for sym in symbols}
        for future in as_completed(futures):
            sym = futures[future]
            quote = future.result()
            if quote:
                key = sym if sym.startswith("^") else quote["key"]
                result[key] = {k: v for k, v in quote.items() if k != "key"}
    return result


def _format_stock_line(sym: str, q: dict) -> str:
    return (
        f"• **{q['name']}** ({sym}): {_fmt_inr(q['price'])} | "
        f"Change: {q.get('change_pct', 0):+.2f}% | "
        f"Day H/L: {_fmt_inr(q.get('day_high'))} / {_fmt_inr(q.get('day_low'))} | "
        f"52W H/L: {_fmt_inr(q.get('fifty_two_week_high'))} / {_fmt_inr(q.get('fifty_two_week_low'))}"
    )


def build_market_overview_context() -> str:
    """Fetch Nifty + top heavyweight movers for stock-pick questions."""
    symbols = ["^NSEI", "^NSEBANK", "^BSESN"] + NIFTY_HEAVYWEIGHTS
    quotes = fetch_yahoo_quotes(symbols)
    if not quotes:
        return ""

    lines: list[str] = ["MARKET OVERVIEW (aaj ka mood):"]

    for idx_sym, label in [("^NSEI", "Nifty 50"), ("^NSEBANK", "Bank Nifty"), ("^BSESN", "Sensex")]:
        q = quotes.get(idx_sym)
        if q and q.get("price"):
            lines.append(
                f"• **{label}**: {_fmt_inr(q['price'])} | Change: {q.get('change_pct', 0):+.2f}%"
            )

    stocks = [(sym, q) for sym, q in quotes.items() if not sym.startswith("^") and q.get("price")]
    stocks.sort(key=lambda x: x[1].get("change_pct", 0), reverse=True)

    if stocks:
        lines.append("\nTOP GAINERS TODAY (Nifty heavyweights):")
        for sym, q in stocks[:4]:
            if q.get("change_pct", 0) > 0:
                lines.append(_format_stock_line(sym, q))

        lines.append("\nTOP LOSERS TODAY (Nifty heavyweights):")
        for sym, q in reversed(stocks[-3:]):
            if q.get("change_pct", 0) < 0:
                lines.append(_format_stock_line(sym, q))

    return "\n".join(lines)


def fetch_crypto_quotes(coin_ids: list[str]) -> dict[str, dict]:
    if not coin_ids:
        return {}

    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ",".join(coin_ids),
                "price_change_percentage": "24h",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6,
        )
        resp.raise_for_status()
        coins = resp.json()
    except Exception:
        return {}

    result: dict[str, dict] = {}
    for coin in coins:
        result[coin["id"]] = {
            "name": coin.get("name", coin["id"]),
            "symbol": coin.get("symbol", "").upper(),
            "price_usd": coin.get("current_price"),
            "change_24h_pct": coin.get("price_change_percentage_24h"),
            "market_cap_usd": coin.get("market_cap"),
            "high_24h": coin.get("high_24h"),
            "low_24h": coin.get("low_24h"),
        }
    return result


def _fmt_inr(val: Optional[float]) -> str:
    if val is None:
        return "N/A"
    if val >= 1_00_00_000:
        return f"₹{val:,.2f}"
    return f"₹{val:,.2f}"


def _fmt_usd(val: Optional[float]) -> str:
    if val is None:
        return "N/A"
    return f"${val:,.2f}"


def fetch_yahoo_history(symbol: str, range_: str = "1mo") -> Optional[dict]:
    """Fetch OHLCV history for chart rendering."""
    yahoo_sym = _yahoo_symbol(symbol)
    try:
        resp = requests.get(
            f"https://query2.finance.yahoo.com/v8/finance/chart/{yahoo_sym}",
            params={"interval": "1d", "range": range_},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json().get("chart", {}).get("result", [])
        if not result:
            return None

        meta = result[0].get("meta", {})
        timestamps = result[0].get("timestamp") or []
        indicators = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes = indicators.get("close") or []
        opens = indicators.get("open") or []
        highs = indicators.get("high") or []
        lows = indicators.get("low") or []
        volumes = indicators.get("volume") or []

        points = []
        for i, ts in enumerate(timestamps):
            close = closes[i] if i < len(closes) else None
            if close is None:
                continue
            points.append({
                "date": ts,
                "open": opens[i] if i < len(opens) else close,
                "high": highs[i] if i < len(highs) else close,
                "low": lows[i] if i < len(lows) else close,
                "close": close,
                "volume": volumes[i] if i < len(volumes) else 0,
            })

        raw_sym = meta.get("symbol", yahoo_sym).replace(".NS", "")
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose") or price
        change_pct = ((price - prev) / prev * 100) if prev and price else 0

        return {
            "symbol": raw_sym,
            "name": meta.get("longName") or meta.get("shortName", raw_sym),
            "currency": meta.get("currency", "INR"),
            "price": price,
            "change_pct": change_pct,
            "change": (price - prev) if prev and price else 0,
            "day_high": meta.get("regularMarketDayHigh"),
            "day_low": meta.get("regularMarketDayLow"),
            "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
            "fifty_two_week_low": meta.get("fiftyTwoWeekLow"),
            "volume": meta.get("regularMarketVolume"),
            "pe_ratio": meta.get("trailingPE"),
            "market_cap": meta.get("marketCap"),
            "points": points[-120:],
        }
    except Exception:
        return None


def build_chart_payload(question: str) -> Optional[dict]:
    """Build chart metadata for the primary stock/crypto in the question."""
    stock_syms = _detect_stock_symbols(question)
    if stock_syms:
        sym = stock_syms[0]
        if sym.startswith("^"):
            return None
        data = fetch_yahoo_history(sym, "6mo")
        if data:
            return {"type": "stock", **data}
        return None

    crypto_ids = _detect_crypto_ids(question)
    if crypto_ids:
        quotes = fetch_crypto_quotes(crypto_ids[:1])
        if quotes:
            coin_id = crypto_ids[0]
            q = quotes[coin_id]
            return {
                "type": "crypto",
                "symbol": q.get("symbol", coin_id.upper()),
                "name": q.get("name", coin_id),
                "currency": "USD",
                "price": q.get("price_usd"),
                "change_pct": q.get("change_24h_pct", 0),
                "day_high": q.get("high_24h"),
                "day_low": q.get("low_24h"),
                "market_cap": q.get("market_cap_usd"),
            }
    return None


def build_market_context(question: str) -> str:
    """Fetch live quotes for symbols mentioned in the question."""
    if not needs_live_data(question):
        return ""

    lines: list[str] = []
    stock_syms = _detect_stock_symbols(question)
    crypto_ids = _detect_crypto_ids(question)

    # Market news / overview — Nifty, Sensex, Bank Nifty
    if is_market_news_question(question) and not stock_syms:
        overview = build_market_overview_context()
        if overview:
            return (
                "LIVE MARKET DATA (Yahoo Finance — use for today's market news analysis):\n"
                + overview
            )

    # "Aaj konsa stock kharidu?" — no specific symbol → fetch market overview
    if is_stock_pick_question(question) and not stock_syms and not crypto_ids:
        overview = build_market_overview_context()
        if overview:
            return (
                "LIVE MARKET DATA (Yahoo Finance — use for today's stock pick analysis):\n"
                + overview
            )

    if stock_syms:
        quotes = fetch_yahoo_quotes(stock_syms)
        for sym, q in quotes.items():
            if q.get("price") is None:
                continue
            lines.append(_format_stock_line(sym, q) + f" | Vol: {q.get('volume') or 'N/A'}")

    if crypto_ids:
        quotes = fetch_crypto_quotes(crypto_ids)
        for coin_id, q in quotes.items():
            if q.get("price_usd") is None:
                continue
            lines.append(
                f"• **{q['name']}** ({q['symbol']}): {_fmt_usd(q['price_usd'])} | "
                f"24h: {q.get('change_24h_pct', 0):+.2f}% | "
                f"24h H/L: {_fmt_usd(q.get('high_24h'))} / {_fmt_usd(q.get('low_24h'))}"
            )

    if not lines:
        return ""

    return (
        "LIVE MARKET DATA (Yahoo Finance / CoinGecko — use these numbers in your analysis):\n"
        + "\n".join(lines)
    )
