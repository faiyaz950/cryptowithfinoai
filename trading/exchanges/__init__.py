"""
Kaun se exchange desk se jud sakte hain — ek hi jagah.

Naya exchange jodne ka poora kaam: ek adapter file likho aur usse yahan
REGISTRY mein daal do. Na koi endpoint badalta hai, na frontend ka catalogue —
UI apni list isi registry se leta hai (`GET /api/byok/exchanges`), isliye
dono taraf list kabhi alag nahi ho sakti.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import ExchangeAdapter, message_for  # noqa: F401  (backend_api isse import karta hai)
from .bybit import BybitAdapter
from .coindcx import CoinDCXAdapter
from .delta import DeltaAdapter

# Jo adapters sach mein bane hue hain.
REGISTRY: Dict[str, type] = {
    DeltaAdapter.id: DeltaAdapter,
    CoinDCXAdapter.id: CoinDCXAdapter,
    BybitAdapter.id: BybitAdapter,
}

# Jinka adapter abhi nahi bana. Ye sirf UI ko dikhane ke liye hain taaki user
# ko pata rahe kya aa raha hai — inhe connect nahi kiya ja sakta, aur backend
# inhe kabhi accept nahi karta (SUPPORTED_EXCHANGES mein ye hain hi nahi).
COMING_SOON: List[Dict[str, str]] = [
    {"id": "pi42", "name": "Pi42", "region": "India", "tagline": "INR-margined perpetuals"},
    {"id": "mudrex", "name": "Mudrex", "region": "India", "tagline": "Spot + futures"},
    {"id": "binance", "name": "Binance", "region": "Global", "tagline": "USDT perps · deepest liquidity"},
    {"id": "okx", "name": "OKX", "region": "Global", "tagline": "Perps + options"},
    {"id": "deribit", "name": "Deribit", "region": "Global", "tagline": "BTC/ETH options"},
]

SUPPORTED_EXCHANGES = set(REGISTRY)


def get_adapter(exchange: str, api_key: str, secret_key: str) -> Optional[ExchangeAdapter]:
    cls = REGISTRY.get((exchange or "").strip().lower())
    return cls(api_key, secret_key) if cls else None


def exchange_name(exchange: str) -> str:
    cls = REGISTRY.get((exchange or "").strip().lower())
    if cls:
        return cls.name
    for entry in COMING_SOON:
        if entry["id"] == exchange:
            return entry["name"]
    return (exchange or "Exchange").title()


def catalogue() -> List[Dict[str, Any]]:
    """UI ke liye poori list — live pehle, coming soon baad mein."""
    live = [
        {
            "id": cls.id,
            "name": cls.name,
            "region": cls.region,
            "tagline": cls.tagline,
            "key_url": cls.key_url,
            "available": True,
        }
        for cls in REGISTRY.values()
    ]
    soon = [{**entry, "key_url": "", "available": False} for entry in COMING_SOON]
    return live + soon
