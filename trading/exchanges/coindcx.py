"""
CoinDCX adapter.

Signing (docs se, "Authentication"):
    body      = {... , "timestamp": <ms>}  -> compact JSON
    signature = HMAC_SHA256(body_json, secret) -> hex
    headers   = X-AUTH-APIKEY, X-AUTH-SIGNATURE, Content-Type: application/json

Dhyan: **saari** authenticated calls POST hain, aur signature usi exact JSON
string par banta hai jo bheji jaati hai — isliye body ek hi baar serialize
karke wahi string dono jagah use hoti hai. Dobara dump karne par separators
badal jaate aur signature fail ho jaata.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, List, Optional

import requests

from .base import ExchangeAdapter, fnum

BASE_URL = "https://api.coindcx.com"
TIMEOUT = 15
# Positions/orders paginated hain; desk ko sirf khuli cheezein dikhani hain.
PAGE_SIZE = "50"
MARGIN_CURRENCIES = ["INR", "USDT"]


class CoinDCXAdapter(ExchangeAdapter):
    id = "coindcx"
    name = "CoinDCX"
    region = "India"
    tagline = "INR + USDT futures · spot"
    key_url = "https://coindcx.com/api-dashboard"

    def _post(self, path: str, body: Optional[Dict[str, Any]] = None):
        payload = dict(body or {})
        payload["timestamp"] = int(time.time() * 1000)
        json_body = json.dumps(payload, separators=(",", ":"))
        signature = hmac.new(
            self.secret_key.encode("utf-8"), json_body.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        try:
            res = requests.post(
                f"{BASE_URL}{path}",
                data=json_body,
                timeout=TIMEOUT,
                headers={
                    "Content-Type": "application/json",
                    "X-AUTH-APIKEY": self.api_key,
                    "X-AUTH-SIGNATURE": signature,
                },
            )
        except Exception as exc:
            return self.fail("network", str(exc))

        if res.status_code == 200:
            try:
                self.clear_error()
                return res.json()
            except Exception:
                return self.fail(None, res.text)

        # CoinDCX reason ko HTTP code + message se batata hai.
        text = (res.text or "")[:400]
        lowered = text.lower()
        if res.status_code == 401:
            if "signature" in lowered:
                reason = "signature_mismatch"
            elif "expire" in lowered or "timestamp" in lowered:
                reason = "expired_signature"
            else:
                reason = "invalid_api_key"
        elif res.status_code == 403:
            reason = "ip_not_whitelisted" if "ip" in lowered else "unauthorized"
        elif res.status_code == 429:
            reason = "rate_limited"
        else:
            reason = None
        return self.fail(reason, text)

    # ── interface ──────────────────────────────────────────

    def verify(self) -> Dict[str, Any]:
        """
        Balances hi sabse seedha private call hai. CoinDCX key ki permissions
        API se nahi milti, isliye trading ko "verified" nahi kehte — wahi
        rawaiya jo Delta par hai.
        """
        data = self._post("/exchange/v1/users/balances")
        if data is None:
            return {
                "success": False,
                "can_trade": False,
                "can_withdraw": False,
                "permissions_verified": False,
                "reason": self.last_reason,
                "client_ip": self.last_client_ip,
                "error": self.error_message(),
            }
        return {
            "success": True,
            "can_trade": True,
            "can_withdraw": False,
            "permissions_verified": True,
        }

    def balances(self) -> Optional[List[Dict[str, Any]]]:
        data = self._post("/exchange/v1/users/balances")
        if data is None:
            return None
        out = []
        for r in data if isinstance(data, list) else []:
            if not isinstance(r, dict):
                continue
            balance = fnum(r.get("balance"))
            locked = fnum(r.get("locked_balance"))
            # CoinDCX "balance" free amount hai aur locked alag; desk total
            # aur available dono dikhata hai.
            row = self.balance_row(r.get("currency"), balance + locked, balance)
            if row:
                out.append(row)
        return out

    def positions(self) -> Optional[List[Dict[str, Any]]]:
        data = self._post("/exchange/v1/derivatives/futures/positions", {
            "page": "1",
            "size": PAGE_SIZE,
            "margin_currency_short_name": MARGIN_CURRENCIES,
        })
        if data is None:
            return None
        out = []
        for p in data if isinstance(data, list) else []:
            if not isinstance(p, dict):
                continue
            size = fnum(p.get("active_pos"))
            if not size:
                continue
            out.append(self.position_row(
                # "B-ETH_USDT" -> "ETH_USDT"; prefix contract type hai, symbol nahi.
                symbol=str(p.get("pair") or "").split("-", 1)[-1],
                side="long" if size > 0 else "short",
                size=size,
                entry_price=p.get("avg_price"),
                mark_price=p.get("mark_price"),
                margin=p.get("locked_margin"),
                liquidation_price=p.get("liquidation_price"),
            ))
        return out

    def open_orders(self) -> Optional[List[Dict[str, Any]]]:
        data = self._post("/exchange/v1/derivatives/futures/orders", {
            "status": "open",
            "page": "1",
            "size": PAGE_SIZE,
            "margin_currency_short_name": MARGIN_CURRENCIES,
        })
        if data is None:
            return None
        out = []
        for o in data if isinstance(data, list) else []:
            if not isinstance(o, dict):
                continue
            out.append(self.order_row(
                id=o.get("id"),
                symbol=str(o.get("pair") or "").split("-", 1)[-1],
                side=o.get("side"),
                order_type=o.get("order_type"),
                size=o.get("total_quantity"),
                unfilled_size=o.get("remaining_quantity"),
                price=o.get("price"),
                state=o.get("status"),
                created_at=o.get("created_at"),
            ))
        return out

    def profile(self) -> Dict[str, Any]:
        data = self._post("/exchange/v1/users/info")
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        info = rows[0] if rows and isinstance(rows[0], dict) else {}
        if not info:
            return {}
        name = " ".join(x for x in [info.get("first_name"), info.get("last_name")] if x).strip()
        return {
            "account_name": name,
            "exchange_email": (info.get("email") or "").lower(),
            "exchange_phone": info.get("mobile_number") or "",
            "exchange_username": info.get("coindcx_id") or "",
            "has_profile_data": True,
        }
