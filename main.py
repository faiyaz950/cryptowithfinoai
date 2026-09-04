"""
Backend ka single entrypoint — AI chat aur crypto trading, dono ek hi service mein.

Do alag frameworks hain (AI side FastAPI, trading side Flask) aur dono ka apna
maturity level hai, isliye unhe rewrite karne ke bajaye ek hi ASGI app mein jod
diya gaya hai:

    FastAPI (ai/)  ──►  apne routes: /api/chat, /api/models, /api/chart, /health
         │
         └── Mount("/") ──►  Flask (trading/): /api/candles, /api/backtest,
                             /api/screener, /api/orders, /api/auth/*, /api/byok/* ...

Starlette routes ko order mein match karta hai, isliye FastAPI ke apne routes
pehle chalte hain aur baaki sab request Flask ko chali jaati hai. Iska matlab
dono services ke saare purane URLs bilkul waise ke waise kaam karte hain —
bas ab port ek hai, deploy ek hai, aur .env ek hai.

Chalao:  uvicorn main:app --port 8000
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Trading side flat modules import karta hai (backend_api, fetch_trading_data, django_orm).
# Uska Django app label "django_orm" migrations mein baked hai, isliye usko package
# banane ke bajaye uski directory sys.path par daal dete hain — zero import changes.
sys.path.insert(0, str(BASE_DIR / "trading"))

# Flask ko apni CORS na lagane ka signal — CORS FastAPI ki middleware handle karti hai.
# Ye backend_api import hone se pehle set hona zaroori hai.
os.environ["MERGED_BACKEND"] = "1"

# Ek hi .env, backend root par. Ye pehle load hota hai taaki dono sides ke apne
# load_dotenv() calls isi ko override na karein (dotenv already-set vars nahi badalta).
load_dotenv(BASE_DIR / ".env")

from ai.main import app  # noqa: E402  — FastAPI app, CORS aur routes ke saath
from backend_api import app as trading_app  # noqa: E402  — Flask app (trading/ se)

try:
    from a2wsgi import WSGIMiddleware  # actively maintained WSGI->ASGI bridge
except ImportError:  # pragma: no cover - fallback for older installs
    from starlette.middleware.wsgi import WSGIMiddleware  # type: ignore[no-redef]

# Sabse aakhir mein mount — taaki FastAPI ke apne routes pehle match hon.
app.mount("/", WSGIMiddleware(trading_app))

__all__ = ["app"]
