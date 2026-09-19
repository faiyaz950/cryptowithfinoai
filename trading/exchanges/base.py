"""
Ek exchange ko desk se jodne ke liye jo kuch chahiye — bas itna.

Pehle poora BYOK code seedha Delta ke client se baat karta tha: uske method
naam, uske error strings, uska response shape. Naya exchange jodne ka matlab
tha har endpoint mein `if exchange == ...` likhna. Ab har exchange ek adapter
hai jo neeche wala chhota sa interface poora karta hai, aur backend sirf isi
interface se baat karta hai.

Adapter ka kaam do hi cheezein hain:
  1. exchange ki bhasha bolna (auth, signing, endpoints)
  2. jawab ko desk ke common shape mein badal dena

Isliye upar ka koi bhi code ye nahi jaanta ki Delta ke paas `size` hai aur
CoinDCX ke paas `active_pos` — wo farak yahin, is layer par khatam ho jaata hai.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def fnum(value: Any, default: float = 0.0) -> float:
    """Exchange kabhi number bhejta hai, kabhi string, kabhi null — teeno chalein."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# Har exchange apni galti apne code mein bolta hai (Delta: "invalid_api_key",
# Bybit: 10003, CoinDCX: 401 + message). Adapter use in mein se ek "reason"
# mein badal deta hai, aur user ko dikhne wala jumla yahan ek jagah rehta hai.
REASON_MESSAGES = {
    "invalid_api_key": (
        "API key sahi nahi hai. Dhyan dein ki key {exchange} ke account se bani ho, "
        "aur poori copy hui ho."
    ),
    "signature_mismatch": "API secret galat hai. Secret dobara copy karein — aage-peeche koi space na ho.",
    "expired_signature": "{exchange} se time match nahi hua. Kuch second baad dobara try karein.",
    "unauthorized": (
        "Is key ko zaroori permission nahi hai. {exchange} par key edit karke read aur "
        "trading dono on karein — balance aur positions ke liye trading permission zaroori hai."
    ),
    "ip_not_whitelisted": (
        "Aapki API key par IP restriction laga hai. {exchange} par key edit karke ye IP "
        "add karein: {client_ip} (ek se zyada IP comma se daal sakte hain)."
    ),
    "rate_limited": "{exchange} ne abhi bahut requests dekh li hain. Thodi der baad try karein.",
    "key_expired": "Ye API key expire ho chuki hai. {exchange} par nayi key banayein.",
    "network": "{exchange} tak pahuncha nahi ja saka. Thodi der baad try karein.",
    "adapter_missing": "{exchange} abhi support mein nahi hai.",
    "order_not_supported": "{exchange} par is desk se order lagana abhi nahi bana hai — sirf balance aur positions dikhte hain.",
}


def message_for(reason: Optional[str], *, exchange: str, client_ip: Optional[str] = None,
                fallback: str = "") -> str:
    """
    Reason ko user ki bhasha mein badlo.

    Anjaan reason par exchange ka raw jawab nahi dikhate — wo aksar JSON hota
    hai jiska user kuch nahi kar sakta. Uski jagah code batate hain aur wahi
    kehte hain jo user kar sakta hai.
    """
    template = REASON_MESSAGES.get(reason or "")
    if template:
        return template.format(exchange=exchange, client_ip=client_ip or "server IP")
    text = (fallback or "").strip()
    if text.startswith("{") or text.startswith("["):
        return f"{exchange} ne ye key accept nahi ki. API key aur secret dobara copy karein."
    return text[:400] or f"{exchange} se jawab nahi mila."


class ExchangeAdapter:
    """
    Har exchange isi ko poora karta hai.

    Data dene wale teeno method fail hone par `None` lautate hain (khaali list
    nahi) — "kuch nahi mila" aur "pata hi nahi chala" do alag baatein hain, aur
    UI dono par alag cheez dikhata hai. Fail hone par adapter `last_error`
    aur `last_reason` bhar deta hai.
    """

    id: str = ""
    name: str = ""
    region: str = "Global"
    tagline: str = ""
    # Key banate waqt user ko kahan jaana hai — UI isi link ko dikhata hai.
    key_url: str = ""

    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key or ""
        self.secret_key = secret_key or ""
        self.last_error: str = ""
        self.last_reason: Optional[str] = None
        self.last_client_ip: Optional[str] = None

    # ── har adapter ye bharta hai ──────────────────────────

    def verify(self) -> Dict[str, Any]:
        """Key sach mein chalti hai? {success, can_trade, can_withdraw, permissions_verified}"""
        raise NotImplementedError

    def balances(self) -> Optional[List[Dict[str, Any]]]:
        """[{asset, balance, available}] — khaali assets hata kar."""
        raise NotImplementedError

    def positions(self) -> Optional[List[Dict[str, Any]]]:
        """Desk ke common position shape mein. Band positions shaamil na karein."""
        raise NotImplementedError

    def open_orders(self) -> Optional[List[Dict[str, Any]]]:
        """Sirf pending/open orders, common shape mein."""
        raise NotImplementedError

    def profile(self) -> Dict[str, Any]:
        """Exchange par user kaun hai — best effort, na mile to {}."""
        return {}

    # Order lagana har adapter mein nahi hai. Jo nahi karta, wo saaf mana
    # karta hai — chup-chaap kuch na karke "ho gaya" kehne se behtar.
    def place_order(self, **kwargs):
        return self.fail("order_not_supported")

    def cancel_order(self, order_id):
        return self.fail("order_not_supported")

    # ── common madad ───────────────────────────────────────

    def fail(self, reason: Optional[str], raw: str = "", client_ip: Optional[str] = None):
        self.last_reason = reason
        self.last_error = (raw or "")[:400]
        self.last_client_ip = client_ip
        return None

    def error_message(self) -> str:
        return message_for(
            self.last_reason,
            exchange=self.name,
            client_ip=self.last_client_ip,
            fallback=self.last_error,
        )

    def clear_error(self):
        self.last_error = ""
        self.last_reason = None
        self.last_client_ip = None

    @staticmethod
    def position_row(**kw) -> Dict[str, Any]:
        """
        Position ka common shape.

        `unrealized_pnl` sirf tab bharte hain jab exchange khud de. Hum khud
        nahi ginte — uske liye contract size aur inverse/linear ka hisaab
        chahiye, aur galat number dikhane se behtar hai kuch na dikhana.
        `move_pct` (entry se mark tak, side ke hisaab se) exact hota hai.
        """
        entry = fnum(kw.get("entry_price"))
        mark = kw.get("mark_price")
        mark = fnum(mark) if mark not in (None, "") else None
        side = kw.get("side") or "long"
        move = None
        if entry and mark:
            move = ((mark - entry) / entry) * (1 if side == "long" else -1) * 100
        return {
            "symbol": str(kw.get("symbol") or "").upper(),
            "side": side,
            "size": abs(fnum(kw.get("size"))),
            "entry_price": entry,
            "mark_price": mark,
            "move_pct": move,
            "unrealized_pnl": kw["unrealized_pnl"] if kw.get("unrealized_pnl") is not None else None,
            "realized_pnl": fnum(kw.get("realized_pnl")),
            "realized_funding": fnum(kw.get("realized_funding")),
            "margin": fnum(kw.get("margin")),
            "liquidation_price": fnum(kw.get("liquidation_price")) or None,
        }

    @staticmethod
    def order_row(**kw) -> Dict[str, Any]:
        size = fnum(kw.get("size"))
        unfilled = kw.get("unfilled_size")
        return {
            "id": kw.get("id"),
            "symbol": str(kw.get("symbol") or "").upper(),
            "side": str(kw.get("side") or "").lower(),
            "order_type": str(kw.get("order_type") or "").lower(),
            "size": size,
            "unfilled_size": fnum(unfilled, size) if unfilled not in (None, "") else size,
            "price": fnum(kw.get("price")) or None,
            "state": str(kw.get("state") or "").lower(),
            "created_at": kw.get("created_at") or "",
        }

    @staticmethod
    def balance_row(asset: str, balance: Any, available: Any = None) -> Optional[Dict[str, Any]]:
        bal = fnum(balance)
        avail = fnum(available, bal) if available not in (None, "") else bal
        if not asset or (not bal and not avail):
            return None
        return {"asset": str(asset).upper(), "balance": bal, "available": avail}
