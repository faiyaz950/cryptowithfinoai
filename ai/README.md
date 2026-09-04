# Finowings AI — Backend

FastAPI service powering chat, model routing and market data.

## Deploying on Render (recommended)

Render runs a normal long-lived server, so streaming chat responses are not cut
short by a function timeout.

`render.yaml` at the repository root already describes the service:

| Setting | Value |
| --- | --- |
| Root Directory | `arjunai/backend` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| Health Check | `/health` |

**Steps:** Render Dashboard → **New → Blueprint** → connect this repository →
Render reads `render.yaml` and prompts for the secret values below.

Note: the free plan sleeps after 15 minutes of inactivity, so the first request
after idling takes roughly 30–50 seconds to wake the service.

## Environment variables

| Variable | Required | Notes |
| --- | --- | --- |
| `GEMINI_API_KEY` | yes | `GEMINI_API_KEY_2` / `_3` are also read, for rotation |
| `FRONTEND_URL` | **yes** | deployed frontend origin, e.g. `https://cryptowithai.vercel.app` — **no trailing slash** |
| `OPENAI_API_KEY` | optional | enables the GPT models |
| `CLAUDE_API_KEY` | optional | enables the Claude models |
| `GROQ_API_KEY` | optional | enables the Groq models |
| `GROK_API_KEY` | optional | enables Grok |
| `REDIS_URL` | optional | response caching; the app runs fine without it |

`FRONTEND_URL` is appended to the CORS allow-list. Without it the browser blocks
every request from the deployed frontend and chat fails with a network error.
Only that one origin is allowed, so preview URLs stay blocked.

These are backend-only variables — they do nothing if set on the frontend
project, and an API key must never be given a `NEXT_PUBLIC_` prefix.

## Deploying on Vercel (alternative)

`vercel.json` and `api/index.py` are also present: create a project with Root
Directory `arjunai/backend` and Framework Preset **Other**. All requests are
routed to the ASGI app with `maxDuration: 300`.

## Local development

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in your keys
uvicorn main:app --reload --port 8001
```
