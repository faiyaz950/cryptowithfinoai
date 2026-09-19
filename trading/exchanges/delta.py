"""
Delta Exchange India adapter.

Signing aur retry ka kaam pehle se `DeltaExchangeClient` mein hai aur wo asli
keys par test ho chuka hai, isliye use dobara nahi likha — ye adapter usi ke
upar baith kar jawab ko desk ke common shape mein badalta hai.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

import requests

from fetch_trading_data import BASE_URL, DeltaExchangeClient

from .base import ExchangeAdapter, fnum

# Delta kabhi `invalid_api_key` bhejta hai, kabhi `InvalidApiKey`, kabhi
# `Signature Mismatch` space ke saath. Match karne se pehle sab kuch chhote
# akshar aur sirf a-z0-9 mein badal dete hain, isliye teenon ek hi key par
# aa jaate hain.
_CODE_REASONS = {
    "ipnotwhitelistedforapikey": "ip_not_whitelisted",
    "invalidapikey": "invalid_api_key",
    "unauthorizedapiaccess": "unauthorized",
    "expiredsignature": "expired_signature",
    # Delta isi galti ko ulte shabd-kram se bhi bhejta hai.
    "signatureexpired": "expired_signature",
    "signaturemismatch": "signature_mismatch",
}

_MARK_CACHE: Dict[str, tuple] = {}
_MARK_TTL = 10.0


def _code_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _mark_price(symbol: str) -> Optional[float]:
    """Public ticker se mark — 10s cache, fail par None."""
    key = (symbol or "").upper()
    if not key:
        return None
    hit = _MARK_CACHE.get(key)
    now = time.time()
    if hit and (now - hit[0]) < _MARK_TTL:
        return hit[1]
    try:
        res = requests.get(f"{BASE_URL}/v2/tickers/{key}", timeout=8)
        res.raise_for_status()
        t = (res.json() or {}).get("result") or {}
        price = fnum(t.get("mark_price")) or fnum(t.get("close")) or fnum(t.get("spot_price")) or None
    except Exception:
        price = None
    _MARK_CACHE[key] = (now, price)
    return price


class DeltaAdapter(ExchangeAdapter):
    id = "delta"
    name = "Delta Exchange India"
    region = "India"
    tagline = "BTC/ETH options · USD perpetuals"
    key_url = "https://india.delta.exchange/app/account/manageapikeys"

    def __init__(self, api_key: str, secret_key: str):
        super().__init__(api_key, secret_key)
        self.client = DeltaExchangeClient(api_key, secret_key)

    # ── errors ─────────────────────────────────────────────

    def _absorb_error(self):
        """Client ka raw error body padho aur usse reason + client_ip nikaalo."""
        raw = (getattr(self.client, "last_error", "") or "").strip()
        code, client_ip = "", None
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                err = parsed.get("error")
                if isinstance(err, dict):
                    code = str(err.get("code") or "")
                    context = err.get("context")
                    if isinstance(context, dict):
                        client_ip = context.get("client_ip")
                elif isinstance(err, str):
                    code = err
            except Exception:
                pass

        reason = _CODE_REASONS.get(_code_key(code))
        if not reason:
            raw_key = _code_key(raw)
            for known, mapped in _CODE_REASONS.items():
                if known in raw_key:
                    reason = mapped
                    break
        return self.fail(reason, raw, client_ip)

    # ── interface ──────────────────────────────────────────

    def verify(self) -> Dict[str, Any]:
        probe = self.client.get_positions(underlying_asset_symbol="BTC")
        if probe is not None:
            self.clear_error()
            return {
                "success": True,
                "can_trade": True,
                # Delta ka API ye nahi batata ki key par withdrawal on hai ya
                # nahi, isliye ise "verified off" nahi kehte — UI user ko khud
                # band rakhne ko kehta hai.
                "can_withdraw": False,
                "permissions_verified": True,
            }
        self._absorb_error()
        return {
            "success": False,
            "can_trade": False,
            "can_withdraw": False,
            "permissions_verified": False,
            "reason": self.last_reason,
            "client_ip": self.last_client_ip,
            "error": self.error_message(),
        }

    def balances(self) -> Optional[List[Dict[str, Any]]]:
        raw = self.client.get_wallet_balances_strict()
        if raw is None:
            return self._absorb_error()
        rows = raw.get("result") if isinstance(raw, dict) else raw
        out = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            asset = r.get("asset_symbol")
            if not asset and isinstance(r.get("asset"), dict):
                asset = r["asset"].get("symbol")
            row = self.balance_row(asset, r.get("balance"), r.get("available_balance"))
            if row:
                out.append(row)
        self.clear_error()
        return out

    def positions(self) -> Optional[List[Dict[str, Any]]]:
        raw = self.client.get_margined_positions()
        if raw is None:
            return self._absorb_error()
        out = []
        for r in raw or []:
            if not isinstance(r, dict):
                continue
            size = fnum(r.get("size"))
            if not size:
                continue
            symbol = str(r.get("product_symbol") or (r.get("product") or {}).get("symbol") or "").upper()
            unrealized = r.get("unrealized_pnl")
            out.append(self.position_row(
                symbol=symbol,
                side="long" if size > 0 else "short",
                size=size,
                entry_price=r.get("entry_price"),
                mark_price=_mark_price(symbol),
                unrealized_pnl=fnum(unrealized) if unrealized not in (None, "") else None,
                realized_pnl=r.get("realized_pnl"),
                realized_funding=r.get("realized_funding"),
                margin=r.get("margin"),
                liquidation_price=r.get("liquidation_price"),
            ))
        self.clear_error()
        return out

    def open_orders(self) -> Optional[List[Dict[str, Any]]]:
        raw = self.client.get_open_orders()
        if raw is None:
            return self._absorb_error()
        out = []
        for r in raw or []:
            if not isinstance(r, dict):
                continue
            out.append(self.order_row(
                id=r.get("id"),
                symbol=r.get("product_symbol") or (r.get("product") or {}).get("symbol"),
                side=r.get("side"),
                order_type=r.get("order_type"),
                size=r.get("size"),
                unfilled_size=r.get("unfilled_size"),
                price=r.get("limit_price"),
                state=r.get("state"),
                created_at=r.get("created_at"),
            ))
        self.clear_error()
        return out

    def profile(self) -> Dict[str, Any]:
        try:
            data = self.client.get_account_profile()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def place_order(self, **kwargs):
        result = self.client.place_order(
            symbol=kwargs.get("symbol"),
            side=kwargs.get("side"),
            order_type=kwargs.get("order_type"),
            quantity=kwargs.get("quantity"),
            price=kwargs.get("price"),
            reduce_only=kwargs.get("reduce_only", False),
        )
        return result if result else self._absorb_error()

    def cancel_order(self, order_id):
        result = self.client.cancel_order(order_id)
        return result if result else self._absorb_error()
