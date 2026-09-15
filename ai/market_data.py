"""Live market data via Delta Exchange (crypto), Yahoo Finance (stocks) and CoinGecko (fallback)."""

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


# Hinglish/English ke aam shabd jo ALL CAPS mein ticker jaise lagte hain.
_STOPWORD_TOKENS = {
    "AAJ", "KA", "KE", "KI", "KO", "KYA", "KYU", "KYUN", "HAI", "HAIN", "THA",
    "THE", "THI", "ABHI", "ACHHA", "ACCHA", "BATAO", "BATA", "KITNA", "KITNE",
    "MERA", "MERE", "MUJHE", "YE", "WO", "VO", "AUR", "YA", "SE", "PAR", "MEIN",
    "NAHI", "HO", "HOGA", "KAR", "KARO", "DO", "DENA", "CHAHIYE", "SAHI",
    "VALUE", "PRICE", "RATE", "LIVE", "NOW", "TODAY", "WHAT", "WHEN", "WHY",
    "HOW", "THE", "AND", "FOR", "WITH", "FROM", "THIS", "THAT", "IS", "ARE",
    "WAS", "WERE", "CAN", "WILL", "SHOULD", "GOOD", "BEST", "HIGH", "LOW",
    "BUY", "SELL", "HOLD", "LONG", "SHORT", "CHART", "MARKET", "DATA", "INFO",
    "NEWS", "TIME", "DAY", "WEEK", "MONTH", "YEAR", "LEVEL", "TREND", "UPDATE",
}

# Crypto ke naam stock ticker nahi hain — "BITCOIN" ko NSE symbol maanna galat hai.
_CRYPTO_TOKENS = {a.upper() for a in CRYPTO_ALIASES} | {
    c.upper() for c in CRYPTO_ALIASES.values()
} | {"CRYPTO", "COIN", "USDT", "USD", "PERP", "PERPETUAL"}


def _is_shouty(text: str) -> bool:
    """
    Poora sawaal CAPS mein likha hai?

    Aise sawaal mein capital letters ka koi matlab nahi rehta, isliye
    "AAJ BITCOIN KA KYA VALUE HAI?" ke har shabd ko NSE ticker samajh lena
    galat hai — yahi "ITC Limited" ka chart Bitcoin ke sawaal par laga raha tha.
    """
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) > 0.8


def _detect_stock_symbols(question: str) -> list[str]:
    q = question.lower()
    found: list[str] = []
    seen: set[str] = set()

    for alias, symbol in _SORTED_STOCK_ALIASES:
        # Word boundary ke bina "itc" b-ITC-oin ke andar match ho jaata tha, aur
        # Bitcoin ke sawaal par ITC Limited ka chart aa jaata tha.
        if re.search(r"\b" + re.escape(alias) + r"\b", q) and symbol not in seen:
            found.append(symbol)
            seen.add(symbol)

    # Explicit NSE tickers (2–15 uppercase letters) — sirf tab jab caps ka
    # matlab ho, yaani user ne poora sawaal CAPS mein na likha ho.
    if not _is_shouty(question):
        for match in re.findall(r"\b([A-Z]{2,15})\b", question):
            if match in seen or match in _NON_TICKER_ACRONYMS:
                continue
            if match in _STOPWORD_TOKENS or match in _CRYPTO_TOKENS:
                continue
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


# Yahoo par NSE = .NS, BSE = .BO. Dono se live quote milta hai aur `exchangeName`
# batata hai ki price kis exchange ka hai.
NSE_SUFFIX = ".NS"
BSE_SUFFIX = ".BO"


def _yahoo_symbol(sym: str, suffix: str = NSE_SUFFIX) -> str:
    if sym.startswith("^") or sym.endswith((NSE_SUFFIX, BSE_SUFFIX)):
        return sym
    return f"{sym}{suffix}"


def _exchange_label(meta: dict) -> str:
    """Yahoo ka exchange code -> padhne layak naam."""
    code = (meta.get("exchangeName") or "").upper()
    if code in ("NSI", "NSE"):
        return "NSE"
    if code == "BSE":
        return "BSE"
    return code or "NSE"


_QUOTE_CACHE: dict[str, tuple[float, Optional[dict]]] = {}
_QUOTE_CACHE_TTL = 20  # seconds — short-lived so quotes stay fresh, but avoids re-fetching
# the same symbol multiple times per second (e.g. the 21-symbol market overview, or
# several users asking about the same stock within the same few seconds).


def _fetch_yahoo_chart(yahoo_sym: str) -> Optional[dict]:
    """
    NSE pehle, phir BSE.

    Har Indian scrip dono exchanges par listed nahi hai — kuch sirf BSE par
    hain, aur NSE ka quote kabhi-kabhi khaali aa jaata hai. Pehle sirf `.NS`
    try hota tha, to aise stocks par "data nahi mila" aata tha jabki BSE par
    price maujood thi. Result mein `exchange` batata hai ki number kahan se aaya.
    """
    cached = _QUOTE_CACHE.get(yahoo_sym)
    if cached and (time.monotonic() - cached[0]) < _QUOTE_CACHE_TTL:
        return cached[1]

    data = _fetch_yahoo_chart_uncached(yahoo_sym)
    if data is None and yahoo_sym.endswith(NSE_SUFFIX):
        data = _fetch_yahoo_chart_uncached(yahoo_sym[: -len(NSE_SUFFIX)] + BSE_SUFFIX)

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
        raw_sym = meta.get("symbol", yahoo_sym).replace(NSE_SUFFIX, "").replace(BSE_SUFFIX, "")
        return {
            "key": raw_sym,
            "exchange": _exchange_label(meta),
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
    # Exchange saath mein — NSE aur BSE ke price thode alag hote hain, to AI ko
    # pata hona chahiye ki wo kis exchange ka number bol raha hai.
    venue = q.get("exchange") or "NSE"
    return (
        f"• **{q['name']}** ({sym} · {venue}): {_fmt_inr(q['price'])} | "
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


# Delta India ke perpetuals — wahi jo desk ke chart aur screener par chalte hain.
DELTA_BASE_URL = "https://api.india.delta.exchange"
_DELTA_SYMBOLS: dict[str, str] = {
    "bitcoin": "BTCUSD",
    "ethereum": "ETHUSD",
    "solana": "SOLUSD",
    "ripple": "XRPUSD",
    "cardano": "ADAUSD",
    "dogecoin": "DOGEUSD",
    "binancecoin": "BNBUSD",
    "matic-network": "POLUSD",
}

# Ticker ke `symbol` se AI ke liye padhne layak naam.
_DELTA_NAMES: dict[str, tuple[str, str]] = {
    "bitcoin": ("Bitcoin", "BTC"),
    "ethereum": ("Ethereum", "ETH"),
    "solana": ("Solana", "SOL"),
    "ripple": ("XRP", "XRP"),
    "cardano": ("Cardano", "ADA"),
    "dogecoin": ("Dogecoin", "DOGE"),
    "binancecoin": ("BNB", "BNB"),
    "matic-network": ("Polygon", "POL"),
}


def _fetch_delta_quote(coin_id: str) -> Optional[dict]:
    """
    Ek coin ka live quote Delta se.

    AI pehle CoinGecko par tha — global spot average — jabki desk ka chart,
    screener aur Risk Desk Delta India ke perpetuals dikhate hain. Do alag
    markets, do alag prices: AI wo number bolta tha jo user ke saamne chart par
    tha hi nahi. CoinGecko ke 24h high/low apne hi price se mel nahi khate the
    (high current price se neeche aa jaata tha).
    """
    sym = _DELTA_SYMBOLS.get(coin_id)
    if not sym:
        return None
    try:
        res = requests.get(f"{DELTA_BASE_URL}/v2/tickers/{sym}", timeout=8)
        res.raise_for_status()
        t = (res.json() or {}).get("result") or {}
    except Exception:
        return None

    def num(key):
        try:
            v = t.get(key)
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    price = num("close") or num("mark_price") or num("spot_price")
    if price is None:
        return None

    name, ticker = _DELTA_NAMES.get(coin_id, (coin_id.title(), coin_id.upper()))
    high = num("high") or num("mark_high_24h")
    low = num("low") or num("mark_low_24h")

    # Exchange ka LTP high/low kabhi-kabhi close se ek tick peeche hota hai, to
    # "24h high" current price se neeche aa jaati thi — jo namumkin hai aur AI
    # use hu-ba-hu bol deta tha. Price ko hi limit maan lo.
    if high is not None:
        high = max(high, price)
    if low is not None:
        low = min(low, price)

    return {
        "name": name,
        "symbol": ticker,
        "price_usd": price,
        "change_24h_pct": num("ltp_change_24h") or num("mark_change_24h") or 0.0,
        "market_cap_usd": None,
        "high_24h": high,
        "low_24h": low,
        "turnover_24h_usd": num("turnover_usd"),
        "source": "Delta",
    }


def fetch_crypto_quotes(coin_ids: list[str]) -> dict[str, dict]:
    """
    Delta pehle (desk isi par chalta hai), jo coin Delta par nahi hai uske liye
    CoinGecko. Isse AI wahi number bolta hai jo chart par dikh raha hota hai.
    """
    if not coin_ids:
        return {}

    result: dict[str, dict] = {}
    remaining: list[str] = []
    for cid in coin_ids:
        quote = _fetch_delta_quote(cid)
        if quote:
            result[cid] = quote
        else:
            remaining.append(cid)

    if not remaining:
        return result
    result.update(_fetch_coingecko_quotes(remaining))
    return result


def _fetch_coingecko_quotes(coin_ids: list[str]) -> dict[str, dict]:
    """Fallback — un coins ke liye jo Delta India par list nahi hain."""
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
            "source": "CoinGecko",
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
    # Crypto pehle: ye crypto desk hai, aur crypto detection word-boundary par
    # hoti hai to wo stock detection se zyada bharosemand hai.
    crypto_first = _detect_crypto_ids(question)
    if crypto_first:
        quotes = fetch_crypto_quotes(crypto_first[:1])
        if quotes:
            coin_id = crypto_first[0]
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
            turnover = q.get("turnover_24h_usd")
            lines.append(
                f"• **{q['name']}** ({q['symbol']}): {_fmt_usd(q['price_usd'])} | "
                f"24h: {(q.get('change_24h_pct') or 0):+.2f}% | "
                f"24h H/L: {_fmt_usd(q.get('high_24h'))} / {_fmt_usd(q.get('low_24h'))}"
                + (f" | 24h turnover: {_fmt_usd(turnover)}" if turnover else "")
                + f" [{q.get('source', 'CoinGecko')}]"
            )

    if not lines:
        return ""

    return (
        "LIVE MARKET DATA — use these exact numbers, do not recall prices from memory.\n"
        "Crypto quotes are Delta Exchange India perpetuals, the same market this "
        "desk's charts and screener show, so they may differ slightly from global "
        "spot averages. Indian stocks carry their exchange (NSE or BSE) on each "
        "line — quote the exchange when you quote the price.\n"
        + "\n".join(lines)
    )
