"""
Django ORM layer for backend_api.

Default: SQLite file next to this package (`crypto_trading.sqlite3`).
Optional MySQL: set USE_MYSQL=true and MYSQL_* env vars.

Env:
  SQLITE_PATH          - full path to SQLite file (optional)
  USE_MYSQL            - "true"/"1" to use MySQL instead of SQLite
  MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE
"""

from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

_setup_done = False


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_sqlite_path() -> str:
    return os.getenv("SQLITE_PATH") or os.path.join(_project_root(), "crypto_trading.sqlite3")


def _use_mysql() -> bool:
    return os.getenv("USE_MYSQL", "").strip().lower() in ("1", "true", "yes")


def _configure_settings() -> None:
    from django.conf import settings

    if settings.configured:
        return

    if _use_mysql():
        try:
            import pymysql

            pymysql.install_as_MySQLdb()
        except Exception:
            pass
        databases = {
            "default": {
                "ENGINE": "django.db.backends.mysql",
                "NAME": os.getenv("MYSQL_DATABASE", "crypto_trading"),
                "USER": os.getenv("MYSQL_USER", "root"),
                "PASSWORD": os.getenv("MYSQL_PASSWORD", ""),
                "HOST": os.getenv("MYSQL_HOST", "127.0.0.1"),
                "PORT": os.getenv("MYSQL_PORT", "3306"),
                "OPTIONS": {"charset": "utf8mb4"},
            }
        }
        print("ℹ️ Database: MySQL (USE_MYSQL=true)")
    else:
        path = _default_sqlite_path()
        databases = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": path,
            }
        }
        print(f"ℹ️ Database: SQLite at {path}")

    settings.configure(
        DEBUG=False,
        SECRET_KEY=os.getenv("DJANGO_SECRET_KEY", "dev-local-django-secret-change-me"),
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django_orm.apps.DjangoOrmConfig",
        ],
        DATABASES=databases,
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        TIME_ZONE="UTC",
    )


def _ensure_django() -> None:
    global _setup_done
    import django
    from django.conf import settings

    if not settings.configured:
        _configure_settings()
    if not _setup_done:
        django.setup()
        _setup_done = True


def init_database() -> None:
    """Run migrations for django_orm app (creates tables)."""
    _ensure_django()
    from django.core.management import call_command

    call_command("migrate", "django_orm", interactive=False, verbosity=0)
    print("✅ Django ORM tables ready")


def _parse_dt(value: Any) -> datetime:
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = parse_datetime(value)
        if dt is None:
            dt = timezone.now()
    else:
        dt = timezone.now()
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


# --- App logins ---


def save_login_entry(
    *,
    username: str,
    password: str,
    login_type: str,
    ip_address: str = "",
) -> datetime:
    _ensure_django()
    from django_orm.models import AppLogin

    row = AppLogin.objects.create(
        username=username,
        password=password,
        login_type=login_type,
        ip_address=ip_address or "",
    )
    return row.created_at


def fetch_login_history(*, limit: int = 50) -> Tuple[int, List[Dict[str, Any]]]:
    _ensure_django()
    from django_orm.models import AppLogin

    total = AppLogin.objects.count()
    rows = AppLogin.objects.all()[:limit]
    logins = [
        {
            "id": r.id,
            "username": r.username,
            "login_type": r.login_type,
            "ip_address": r.ip_address,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
    return total, logins


# --- Broker logins ---


def save_broker_login_entry(
    *,
    broker: str,
    api_key: str,
    secret_key: str,
    ip_address: str = "",
) -> datetime:
    _ensure_django()
    from django_orm.models import BrokerLogin

    row = BrokerLogin.objects.create(
        broker=broker,
        api_key=api_key,
        secret_key=secret_key,
        ip_address=ip_address or "",
    )
    return row.created_at


def get_latest_broker_login() -> Optional[Dict[str, Any]]:
    _ensure_django()
    from django_orm.models import BrokerLogin

    r = BrokerLogin.objects.order_by("-created_at").first()
    if not r:
        return None
    return {
        "broker": r.broker,
        "api_key": r.api_key,
        "secret_key": r.secret_key,
        "ip_address": r.ip_address,
        "login_time": r.created_at.isoformat(),
    }


# --- Demo orders ---


def save_demo_order_entry(order_entry: Dict[str, Any]) -> None:
    _ensure_django()
    from django_orm.models import DemoOrder

    ts = _parse_dt(order_entry.get("timestamp"))
    price = order_entry.get("price")
    DemoOrder.objects.create(
        order_id=str(order_entry.get("order_id", "")),
        symbol=str(order_entry.get("symbol", "")),
        side=str(order_entry.get("side", "")),
        order_type=str(order_entry.get("order_type", "")),
        quantity=Decimal(str(order_entry.get("quantity", 0))),
        price=Decimal(str(price)) if price not in (None, "") else None,
        status=str(order_entry.get("status", "")),
        created_at=ts,
    )


def fetch_recent_orders(*, limit: int = 50) -> List[Dict[str, Any]]:
    _ensure_django()
    from django_orm.models import DemoOrder

    out = []
    for r in DemoOrder.objects.all()[:limit]:
        out.append(
            {
                "order_id": r.order_id,
                "symbol": r.symbol,
                "side": r.side,
                "order_type": r.order_type,
                "quantity": float(r.quantity),
                "price": float(r.price) if r.price is not None else None,
                "status": r.status,
                "timestamp": r.created_at.isoformat(),
            }
        )
    return out


# --- Users & sessions ---


def _user_dict(u) -> Dict[str, Any]:
    return {
        "id": u.id,
        "username": u.username,
        "password_hash": u.password_hash,
        "full_name": u.full_name or "",
        "email": u.email or "",
        "email_verified": bool(u.email_verified),
        "is_active": bool(u.is_active),
    }


def create_user_account(
    username: str,
    password_hash: str,
    *,
    email: str = "",
) -> Dict[str, Any]:
    _ensure_django()
    from django_orm.models import UserAccount

    u = UserAccount.objects.create(
        username=username,
        password_hash=password_hash,
        email=(email or "").strip().lower(),
    )
    return {"id": u.id, "username": u.username}


def get_user_account_by_username(username: str) -> Optional[Dict[str, Any]]:
    _ensure_django()
    from django_orm.models import UserAccount

    u = UserAccount.objects.filter(username=username).first()
    return _user_dict(u) if u else None


def get_user_account_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    _ensure_django()
    from django_orm.models import UserAccount

    u = UserAccount.objects.filter(pk=user_id).first()
    return _user_dict(u) if u else None


def update_user_account_fields(user_id: int, **kwargs: Any) -> None:
    _ensure_django()
    from django_orm.models import UserAccount

    u = UserAccount.objects.filter(pk=user_id).first()
    if not u:
        return
    for key, val in kwargs.items():
        if hasattr(u, key):
            setattr(u, key, val)
    u.save()


def create_user_session(
    *,
    user_id: int,
    token: str,
    expires_at: datetime,
    ip_address: str = "",
    user_agent: str = "",
) -> None:
    _ensure_django()
    from django.utils import timezone
    from django_orm.models import UserAccount, UserSession

    exp = expires_at
    if timezone.is_naive(exp):
        exp = timezone.make_aware(exp, timezone.get_current_timezone())
    UserSession.objects.create(
        user_id=user_id,
        token=token,
        expires_at=exp,
        ip_address=ip_address or "",
        user_agent=user_agent or "",
        is_active=True,
    )


def get_active_session_by_token(token: str) -> Optional[Dict[str, Any]]:
    _ensure_django()
    from django.utils import timezone
    from django_orm.models import UserSession

    now = timezone.now()
    s = (
        UserSession.objects.filter(token=token, is_active=True, expires_at__gt=now)
        .order_by("-created_at")
        .first()
    )
    if not s:
        return None
    return {
        "id": s.id,
        "user_id": s.user_id,
        "expires_at": s.expires_at,
    }


def deactivate_session(token: str) -> None:
    _ensure_django()
    from django_orm.models import UserSession

    UserSession.objects.filter(token=token).update(is_active=False)


# --- Exchange accounts ---


def _exchange_summary(a) -> Dict[str, Any]:
    return {
        "id": a.id,
        "user_id": a.user_id,
        "exchange": a.exchange,
        "label": a.label,
        "key_hint": a.key_hint,
        "is_active": bool(a.is_active),
        "can_trade": bool(a.can_trade),
        "can_withdraw": bool(a.can_withdraw),
        "permissions_verified": bool(a.permissions_verified),
        "last_error": a.last_error or "",
        "last_verified_at": a.last_verified_at.isoformat() if a.last_verified_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def _exchange_full(a) -> Dict[str, Any]:
    d = _exchange_summary(a)
    d["api_key_encrypted"] = a.api_key_encrypted
    d["secret_key_encrypted"] = a.secret_key_encrypted
    d["api_key_fingerprint"] = a.api_key_fingerprint
    return d


def get_exchange_account_by_fingerprint(
    exchange: str, fingerprint: str
) -> Optional[Dict[str, Any]]:
    _ensure_django()
    from django_orm.models import ExchangeAccount

    a = ExchangeAccount.objects.filter(
        exchange=exchange.lower().strip(),
        api_key_fingerprint=fingerprint,
    ).first()
    if not a:
        return None
    return _exchange_full(a)


def create_exchange_account(
    *,
    user_id: int,
    exchange: str,
    api_key_encrypted: str,
    secret_key_encrypted: str,
    api_key_fingerprint: str,
    label: str = "Primary",
    key_hint: str = "",
    can_trade: bool = False,
    can_withdraw: bool = False,
    permissions_verified: bool = False,
    last_error: str = "",
) -> int:
    _ensure_django()
    from django.db import IntegrityError
    from django_orm.models import ExchangeAccount

    ex = exchange.lower().strip()
    try:
        a = ExchangeAccount.objects.create(
            user_id=user_id,
            exchange=ex,
            api_key_encrypted=api_key_encrypted,
            secret_key_encrypted=secret_key_encrypted,
            api_key_fingerprint=api_key_fingerprint,
            label=label or "Primary",
            key_hint=key_hint or "",
            is_active=True,
            can_trade=can_trade,
            can_withdraw=can_withdraw,
            permissions_verified=permissions_verified,
            last_error=last_error or "",
        )
        return int(a.id)
    except IntegrityError:
        existing = ExchangeAccount.objects.filter(
            exchange=ex, api_key_fingerprint=api_key_fingerprint
        ).first()
        if existing and existing.user_id == user_id:
            return int(existing.id)
        raise


def update_exchange_account_credentials(
    account_id: int,
    user_id: int,
    *,
    api_key_encrypted: str,
    secret_key_encrypted: str,
    api_key_fingerprint: str,
    key_hint: str = "",
) -> None:
    _ensure_django()
    from django_orm.models import ExchangeAccount

    a = ExchangeAccount.objects.filter(pk=account_id, user_id=user_id).first()
    if not a:
        return
    a.api_key_encrypted = api_key_encrypted
    a.secret_key_encrypted = secret_key_encrypted
    a.api_key_fingerprint = api_key_fingerprint
    a.key_hint = key_hint or ""
    a.save()


def update_exchange_account_status(account_id: int, user_id: int, **kwargs: Any) -> None:
    _ensure_django()
    from django_orm.models import ExchangeAccount

    a = ExchangeAccount.objects.filter(pk=account_id, user_id=user_id).first()
    if not a:
        return
    for key, val in kwargs.items():
        if not hasattr(a, key):
            continue
        setattr(a, key, val)
    a.save()


def get_exchange_account_for_user(
    account_id: int, user_id: int
) -> Optional[Dict[str, Any]]:
    _ensure_django()
    from django_orm.models import ExchangeAccount

    a = ExchangeAccount.objects.filter(pk=account_id, user_id=user_id).first()
    return _exchange_full(a) if a else None


def list_exchange_accounts_for_user(user_id: int) -> List[Dict[str, Any]]:
    _ensure_django()
    from django_orm.models import ExchangeAccount

    return [_exchange_summary(a) for a in ExchangeAccount.objects.filter(user_id=user_id)]


def delete_exchange_account_for_user(account_id: int, user_id: int) -> bool:
    _ensure_django()
    from django_orm.models import ExchangeAccount

    deleted, _ = ExchangeAccount.objects.filter(pk=account_id, user_id=user_id).delete()
    return deleted > 0


def get_latest_exchange_account_for_user(
    user_id: int, exchange: str
) -> Optional[Dict[str, Any]]:
    _ensure_django()
    from django_orm.models import ExchangeAccount

    ex = exchange.lower().strip()
    a = (
        ExchangeAccount.objects.filter(user_id=user_id, exchange=ex)
        .order_by("-updated_at")
        .first()
    )
    return _exchange_summary(a) if a else None


# --- BYOK orders ---


def save_byok_order_entry(order_record: Dict[str, Any]) -> None:
    _ensure_django()
    from django_orm.models import ByokOrder

    ts = _parse_dt(order_record.get("timestamp"))
    price = order_record.get("price")
    ByokOrder.objects.create(
        user_id=int(order_record["user_id"]),
        exchange_account_id=int(order_record["exchange_account_id"]),
        order_id=str(order_record.get("order_id", "")),
        symbol=str(order_record.get("symbol", "")),
        side=str(order_record.get("side", "")),
        order_type=str(order_record.get("order_type", "")),
        quantity=Decimal(str(order_record.get("quantity", 0))),
        price=Decimal(str(price)) if price not in (None, "") else None,
        status=str(order_record.get("status", "")),
        exchange_response=str(order_record.get("exchange_response", ""))[:5000],
        created_at=ts,
    )


def fetch_byok_orders(
    *,
    user_id: int,
    limit: int = 50,
    exchange_account_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    _ensure_django()
    from django_orm.models import ByokOrder

    qs = ByokOrder.objects.filter(user_id=user_id).order_by("-created_at")
    if exchange_account_id is not None:
        qs = qs.filter(exchange_account_id=exchange_account_id)
    out = []
    for r in qs[:limit]:
        out.append(
            {
                "order_id": r.order_id,
                "symbol": r.symbol,
                "side": r.side,
                "order_type": r.order_type,
                "quantity": float(r.quantity),
                "price": float(r.price) if r.price is not None else None,
                "status": r.status,
                "timestamp": r.created_at.isoformat(),
                "exchange_account_id": r.exchange_account_id,
            }
        )
    return out


# --- Email OTP ---


def create_email_change_otp(
    *,
    user_id: int,
    new_email: str,
    otp_code: str,
    expires_at: datetime,
) -> None:
    _ensure_django()
    from django.utils import timezone
    from django_orm.models import EmailChangeOtp

    exp = expires_at
    if timezone.is_naive(exp):
        exp = timezone.make_aware(exp, timezone.get_current_timezone())
    EmailChangeOtp.objects.create(
        user_id=user_id,
        new_email=new_email.strip().lower(),
        otp_code=otp_code,
        expires_at=exp,
        consumed=False,
    )


def verify_email_change_otp(user_id: int, new_email: str, otp: str) -> bool:
    _ensure_django()
    from django.utils import timezone
    from django_orm.models import EmailChangeOtp

    now = timezone.now()
    row = (
        EmailChangeOtp.objects.filter(
            user_id=user_id,
            new_email=new_email.strip().lower(),
            otp_code=otp.strip(),
            consumed=False,
            expires_at__gt=now,
        )
        .order_by("-created_at")
        .first()
    )
    if not row:
        return False
    row.consumed = True
    row.save(update_fields=["consumed"])
    return True
