"""
Bybit v5 adapter.

Signing (docs se, "Integration Guidance"):
    sign_payload = timestamp + api_key + recv_window + queryString
    signature    = HMAC_SHA256(sign_payload, secret) -> lowercase hex
    headers      = X-BAPI-API-KEY, X-BAPI-TIMESTAMP (ms), X-BAPI-RECV-WINDOW, X-BAPI-SIGN

Har jawab `{retCode, retMsg, result}` hota hai; retCode 0 hi success hai —
HTTP 200 aane par bhi retCode se hi pata chalta hai ki kaam hua ya nahi.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from .base import ExchangeAdapter, fnum

BASE_URL = "https://api.bybit.com"
RECV_WINDOW = "5000"
TIMEOUT = 15

# Docs > Error Codes. Jo yahan nahi hain unhe generic maana jaata hai.
_RET_CODE_REASONS = {
    10002: "expired_signature",   # request time window ke bahar
    10003: "invalid_api_key",
    10004: "signature_mismatch",
    10005: "unauthorized",        # permission denied
    10006: "rate_limited",
    10010: "ip_not_whitelisted",
    10018: "rate_limited",
    33004: "key_expired",
}


class BybitAdapter(ExchangeAdapter):
    id = "bybit"
    name = "Bybit"
    region = "Global"
    tagline = "USDT perpetuals · deep liquidity"
    key_url = "https://www.bybit.com/app/user/api-management"

    def sign_payload(self, timestamp: str, query: str) -> str:
        """
        Docs ka exact rule: timestamp + api_key + recv_window + queryString.

        Alag method isliye hai ki ise bina network ke test kiya ja ske — galat
        key par exchange signature check hi nahi karta, to "request chali gayi"
        se ye sabit nahi hota ki payload sahi bana tha.
        """
        return timestamp + self.api_key + RECV_WINDOW + query

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None):
        """Authenticated GET. Fail par None, aur reason `last_reason` mein."""
        query = urlencode(params or {})
        timestamp = str(int(time.time() * 1000))
        payload = self.sign_payload(timestamp, query)
        signature = hmac.new(
            self.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")
        try:
            res = requests.get(url, timeout=TIMEOUT, headers={
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": RECV_WINDOW,
                "X-BAPI-SIGN": signature,
            })
        except Exception as exc:
            return self.fail("network", str(exc))

        try:
            data = res.json()
        except Exception:
            return self.fail(None, res.text)

        ret_code = data.get("retCode")
        if ret_code == 0:
            self.clear_error()
            return data.get("result") or {}
        # Bybit US/Mainland China ko 403 deta hai — wo bhi yahin dikh jaata hai.
        if res.status_code == 403:
            return self.fail("unauthorized", res.text)
        return self.fail(_RET_CODE_REASONS.get(ret_code), f"{ret_code}: {data.get('retMsg')}")

    # ── interface ──────────────────────────────────────────

    def verify(self) -> Dict[str, Any]:
        """
        `/v5/user/query-api` koi bhi permission wali key se chalta hai, aur
        wahi batata hai ki key par kya-kya allowed hai — isliye verify ke liye
        sabse seedha endpoint yahi hai. Delta ke ulat, Bybit withdrawal
        permission sach mein bata deta hai, to hum use guess nahi karte.
        """
        info = self._get("/v5/user/query-api")
        if info is None:
            return {
                "success": False,
                "can_trade": False,
                "can_withdraw": False,
                "permissions_verified": False,
                "reason": self.last_reason,
                "client_ip": self.last_client_ip,
                "error": self.error_message(),
            }
        perms = info.get("permissions") or {}
        contract = perms.get("ContractTrade") or []
        spot = perms.get("Spot") or []
        wallet = perms.get("Wallet") or []
        read_only = str(info.get("readOnly", "")) == "1"
        return {
            "success": True,
            "can_trade": bool(contract or spot) and not read_only,
            "can_withdraw": any("withdraw" in str(p).lower() for p in wallet),
            "permissions_verified": True,
        }

    def balances(self) -> Optional[List[Dict[str, Any]]]:
        result = self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        if result is None:
            return None
        out = []
        for account in result.get("list") or []:
            for coin in account.get("coin") or []:
                row = self.balance_row(
                    coin.get("coin"),
                    coin.get("walletBalance"),
                    # availableToWithdraw kabhi khaali string hota hai; tab
                    # poora balance hi "available" maan lete hain.
                    coin.get("availableToWithdraw") or coin.get("availableBalance"),
                )
                if row:
                    out.append(row)
        return out

    def positions(self) -> Optional[List[Dict[str, Any]]]:
        result = self._get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        if result is None:
            return None
        out = []
        for p in result.get("list") or []:
            size = fnum(p.get("size"))
            if not size:
                continue
            side = "long" if str(p.get("side", "")).lower() == "buy" else "short"
            unrealized = p.get("unrealisedPnl")
            out.append(self.position_row(
                symbol=p.get("symbol"),
                side=side,
                size=size,
                entry_price=p.get("avgPrice"),
                mark_price=p.get("markPrice"),
                unrealized_pnl=fnum(unrealized) if unrealized not in (None, "") else None,
                realized_pnl=p.get("curRealisedPnl"),
                margin=p.get("positionIM"),
                liquidation_price=p.get("liqPrice"),
            ))
        return out

    def open_orders(self) -> Optional[List[Dict[str, Any]]]:
        result = self._get("/v5/order/realtime", {"category": "linear", "settleCoin": "USDT"})
        if result is None:
            return None
        out = []
        for o in result.get("list") or []:
            qty = fnum(o.get("qty"))
            out.append(self.order_row(
                id=o.get("orderId"),
                symbol=o.get("symbol"),
                side=o.get("side"),
                order_type=o.get("orderType"),
                size=qty,
                unfilled_size=qty - fnum(o.get("cumExecQty")),
                price=o.get("price"),
                state=o.get("orderStatus"),
                created_at=o.get("createdTime"),
            ))
        return out

    def profile(self) -> Dict[str, Any]:
        info = self._get("/v5/user/query-api")
        if not info:
            return {}
        # Bybit naam/email nahi deta — jo pehchan wo deta hai wahi dikhate hain.
        return {
            "account_name": info.get("note") or "",
            "exchange_username": str(info.get("userID") or ""),
            "whitelisted_ips": info.get("ips") or [],
            "has_profile_data": True,
        }
