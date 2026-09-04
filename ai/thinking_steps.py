"""Generate Thinking Paths steps shown before AI response (STOXO-style)."""

import re
from typing import Optional

from .market_data import (
    _detect_stock_symbols,
    _detect_crypto_ids,
    is_market_news_question,
    is_stock_pick_question,
    needs_live_data,
)
from .ai_router import detect_topic


def _extract_entities(question: str) -> list[str]:
    entities: list[str] = []
    stocks = _detect_stock_symbols(question)
    cryptos = _detect_crypto_ids(question)

    for sym in stocks:
        label = sym.replace("^", "")
        if label in ("NSEI", "NSEBANK", "BSESN"):
            names = {"NSEI": "Nifty 50", "NSEBANK": "Bank Nifty", "BSESN": "Sensex"}
            entities.append(names.get(label, label))
        else:
            entities.append(f"Stock: {label}")

    for coin_id in cryptos:
        entities.append(f"Crypto: {coin_id.replace('-', ' ').title()}")

    if re.search(r"\b(sip|mutual fund|nav|elss)\b", question, re.I):
        entities.append("Mutual Funds")

    if re.search(r"\b(gold|silver|crude|mcx)\b", question, re.I):
        entities.append("Commodities")

    return entities[:6]


def generate_thinking_steps(question: str, market_context: Optional[str] = None) -> list[str]:
    """Deterministic thinking path steps based on query analysis."""
    topic = detect_topic(question)
    entities = _extract_entities(question)
    steps: list[str] = []

    steps.append("Interpreting the user query to focus on financial analysis and actionable insights.")

    if entities:
        steps.append(f"Identifying entities: {', '.join(entities)}.")
    elif topic == "stock":
        steps.append("Scanning query for NSE/BSE stock symbols and market context.")
    elif topic == "crypto":
        steps.append("Identifying cryptocurrency assets and market cycle context.")
    elif topic == "mutual_fund":
        steps.append("Identifying mutual fund category, SIP/NAV, and AMC context.")
    else:
        steps.append("Classifying query type and selecting relevant data sources.")

    if needs_live_data(question) or market_context:
        if topic == "stock" or _detect_stock_symbols(question):
            steps.append(
                "Fetching live price, day range, 52-week high/low, volume, and P/E from Yahoo Finance."
            )
            steps.append(
                "Pulling technical context — RSI zones, moving averages (SMA/EMA), support & resistance levels."
            )
            if is_market_news_question(question):
                steps.append("Gathering latest market news, FII/DII flows, and sector-wise advances/declines.")
            elif is_stock_pick_question(question):
                steps.append("Scanning Nifty heavyweights for today's top gainers, losers, and momentum.")
            else:
                steps.append("Checking corporate actions — dividends, earnings, and recent news sentiment.")
        elif topic == "crypto":
            steps.append("Fetching live crypto prices, 24h change, and market cap from CoinGecko.")
            steps.append("Analyzing dominance, volatility, and cycle context for the asset.")
        else:
            steps.append("Fetching latest market data and cross-referencing with live sources.")

    if is_market_news_question(question):
        steps.append("Compiling global cues — US markets, crude oil, dollar index, and macro events.")

    steps.append("Preparing a clear report with levels, risks, and what to do next.")
    return steps[:4]
