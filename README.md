# CryptoWithFinoAI — Backend

AI chat aur crypto trading, dono ek hi service mein. Frontend alag repo mein hai
(Next.js, Vercel par deploy hota hai).

```
.
├── main.py          # entrypoint — FastAPI app jispar Flask app mounted hai
├── ai/              # chat, model routing, market context
├── trading/         # candles, screener, backtest, orders, auth, BYOK
├── api/index.py     # Vercel entrypoint (Render ke liye zarurat nahi)
├── render.yaml      # Render blueprint
└── requirements.txt
```

## Ek service, do frameworks

Chat FastAPI par likha hai aur trading API Flask par. Dono ko rewrite karne ke
bajaye `main.py` Flask app ko FastAPI app par **mount** kar deta hai. Starlette
routes ko order mein match karta hai, isliye FastAPI ke apne routes pehle chalte
hain aur baaki har request Flask ko chali jaati hai:

| Route | Kaun handle karta hai |
|---|---|
| `/health`, `/api/models`, `/api/chat`, `/api/chart/*` | FastAPI (`ai/`) |
| `/api/candles`, `/api/screener`, `/api/backtest` | Flask (`trading/`) |
| `/api/orders`, `/api/auth/*`, `/api/byok/*`, `/api/delta/*` | Flask (`trading/`) |

Do baatein jo code padhte waqt ajeeb lag sakti hain, par jaan-boojh kar hain:

- **`ai/` package hai (relative imports), `trading/` flat hai.** `trading` ka
  Django app label `django_orm` migrations mein baked hai; usko package banane se
  app registry toot jaati. Isliye `main.py` uski directory `sys.path` par daal
  deta hai.
- **Flask ki CORS conditional hai.** Merged mode mein FastAPI ki CORSMiddleware
  har route par lagti hai. Dono set karein to browser ko do
  `Access-Control-Allow-Origin` headers milte hain aur wo response reject kar
  deta hai. Standalone Flask chalane par purani CORS wapas lag jaati hai.

## Local par chalao

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env          # phir apni keys bharein
venv/bin/python -m uvicorn main:app --reload --port 8000
```

Check karein:

- <http://127.0.0.1:8000/health> — FastAPI side
- <http://127.0.0.1:8000/api/health> — Flask side
- <http://127.0.0.1:8000/api/screener?interval=1h> — 30 coins ka scan

## Env

Sab kuch ek `.env` mein — AI providers (`GEMINI_API_KEY`, `OPENAI_API_KEY`,
`CLAUDE_API_KEY`, `GROQ_API_KEY`) aur Delta Exchange (`DELTA_API_KEY`,
`DELTA_SECRET_KEY`). Poori list `.env.example` mein hai.

`FRONTEND_URL` deployed frontend ka origin hona chahiye (bina trailing slash) —
ye CORS allow-list mein jaata hai.

## Deploy — Render

Render → New → Blueprint → ye repo. `render.yaml` khud detect ho jayega.
Jo env vars `sync: false` hain unki value Render aapse maangega.

### Deploy se pehle jaan lein

1. **SQLite Render par persist nahi hota.** `trading/` users, sessions aur BYOK
   exchange accounts SQLite mein rakhta hai. Render ka disk ephemeral hai — har
   deploy aur restart par ye data chala jayega. Auth wale features production
   mein tabhi chalenge jab managed database (Postgres) par shift karein.
2. **Free tier 15 min baad so jaata hai.** Pehli request par ~50s cold start.
3. **`FRONTEND_URL` set karna zaroori hai**, warna browser CORS par sab block
   kar dega.

## Note

Ye software analysis aur backtesting ke liye hai, investment advice ke liye nahi.
