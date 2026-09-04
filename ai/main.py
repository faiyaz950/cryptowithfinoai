import json
import base64
import asyncio
import threading
from datetime import date
from queue import Empty, Queue
from fastapi import FastAPI, HTTPException, Request, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Iterator, AsyncIterator
import os
from dotenv import load_dotenv

from .ai_router import ArjunAI, detect_topic
from .cache import get_cached_response, set_cached_response
from .thinking_steps import generate_thinking_steps
from .market_data import build_market_context, build_chart_payload, fetch_yahoo_quotes, fetch_yahoo_history, needs_live_data

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ALLOWED_FILE_TYPES = {"application/pdf", "text/plain", "text/csv"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
THINKING_STEP_DELAY = 0.04
TOKEN_CHUNK_DELAY = 0
TOKEN_CHUNK_SIZE = 160


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload) + "\n\n"


def _chunk_text(text: str, size: int = TOKEN_CHUNK_SIZE) -> Iterator[str]:
    """Yield text in small pieces so the UI can reveal the answer smoothly."""
    i = 0
    n = len(text)
    while i < n:
        end = min(i + size, n)
        if end < n:
            space = text.find(" ", end)
            newline = text.find("\n", i, end + 24)
            if newline != -1 and newline < end + 24:
                end = newline + 1
            elif space != -1 and space < end + 18:
                end = space + 1
        piece = text[i:end]
        if piece:
            yield piece
        i = end


async def _stream_thinking_steps(thinking_steps: list) -> AsyncIterator[str]:
    for i, step in enumerate(thinking_steps):
        yield _sse({
            "type": "thinking",
            "step": step,
            "index": i,
            "total": len(thinking_steps),
        })
        await asyncio.sleep(THINKING_STEP_DELAY)


load_dotenv()

app = FastAPI(title="ArjunAI Backend", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3002",
        "http://localhost:3003",
        "http://localhost:3004",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:3002",
        "http://127.0.0.1:3003",
        "http://127.0.0.1:3004",
        *( [os.getenv("FRONTEND_URL", "")] if os.getenv("FRONTEND_URL") else [] ),
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

arjun = ArjunAI()

# In-memory rate limiting (use Redis in production)
# Keyed by IP -> (day, count) so the count resets every day instead of growing forever.
rate_limit_store: dict[str, tuple[date, int]] = {}
FREE_DAILY_LIMIT = 10


class ChatRequest(BaseModel):
    question: str
    user_type: Optional[str] = "free"
    history: Optional[list] = []
    portfolio_context: Optional[str] = None
    preferred_model: Optional[str] = "auto"


class ChatResponse(BaseModel):
    answer: str
    model: str
    cached: bool
    topic: Optional[str] = "general"
    sources: Optional[list] = []
    search_queries: Optional[list] = []
    grounded: Optional[bool] = False


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host


def validate_and_rate_limit(request: Request, body: ChatRequest):
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Sawaal khali nahi ho sakta")
    if len(question) > 1000:
        raise HTTPException(status_code=400, detail="Sawaal bahut lamba hai — 1000 characters se kam rakho")
    if body.portfolio_context and len(body.portfolio_context) > 8000:
        raise HTTPException(status_code=400, detail="Portfolio data bahut lamba hai — 8000 characters se kam rakho")
    if body.user_type != "pro":
        client_ip = get_client_ip(request)
        today = date.today()
        stored_day, current_count = rate_limit_store.get(client_ip, (today, 0))
        if stored_day != today:
            current_count = 0
        if current_count >= FREE_DAILY_LIMIT:
            raise HTTPException(
                status_code=429,
                detail="Aaj ki limit khatam ho gayi (10 sawaal/day). Pro plan upgrade karein unlimited questions ke liye.",
            )
        rate_limit_store[client_ip] = (today, current_count + 1)
    return question


def get_portfolio_context(body: ChatRequest) -> Optional[str]:
    ctx = (body.portfolio_context or "").strip()
    return ctx if ctx else None


@app.get("/health")
def health():
    return {"status": "ok", "message": "ArjunAI chal raha hai!", "version": "2.0.0"}


@app.get("/api/models")
def list_models(user_type: str = Query("free", description="free or pro")):
    return {"models": arjun.get_available_models(user_type)}


@app.get("/api/chart/{symbol}")
def get_chart_data(symbol: str, range_: str = Query("6mo", alias="range")):
    """Historical OHLCV for stock chart rendering."""
    sym = symbol.strip().upper()
    if not sym or len(sym) > 15:
        raise HTTPException(status_code=400, detail="Invalid symbol")
    data = fetch_yahoo_history(sym, range_)
    if not data:
        raise HTTPException(status_code=502, detail="Chart data fetch failed")
    return data


@app.get("/api/prices/stocks")
def get_stock_prices(symbols: str = Query(..., description="Comma-separated NSE symbols e.g. TCS,RELIANCE")):
    """Proxy Yahoo Finance for NSE-listed Indian stocks."""
    raw = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not raw:
        raise HTTPException(status_code=400, detail="No symbols provided")

    quotes = fetch_yahoo_quotes(raw)
    if not quotes:
        raise HTTPException(status_code=502, detail="Price fetch failed")

    result: dict = {}
    for sym, q in quotes.items():
        result[sym] = {
            "price": q.get("price", 0),
            "change24h": q.get("change_pct", 0),
            "name": q.get("name", sym),
            "currency": q.get("currency", "INR"),
        }
    return result


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest):
    question = validate_and_rate_limit(request, body)
    portfolio_context = get_portfolio_context(body)
    use_live = needs_live_data(question) or bool(portfolio_context)
    model_pref = (body.preferred_model or "auto").strip().lower()

    cached = None if (use_live or model_pref != "auto") else get_cached_response(question)
    if cached:
        return ChatResponse(answer=cached, model="Cache", cached=True, topic=detect_topic(question))

    result = arjun.ask(
        question=question,
        user_type=body.user_type or "free",
        history=body.history or [],
        portfolio_context=portfolio_context,
        preferred_model=body.preferred_model or "auto",
    )

    if result.get("model") != "Error":
        set_cached_response(question, result["answer"])

    return ChatResponse(
        answer=result["answer"],
        model=result["model"],
        topic=result.get("topic", "general"),
        cached=False,
        sources=result.get("sources", []),
        search_queries=result.get("search_queries", []),
        grounded=result.get("grounded", False),
    )


async def process_uploaded_files(files: List[UploadFile]) -> List[dict]:
    """Process uploaded files and return list of file data dicts."""
    processed = []
    for file in files:
        if not file.filename:
            continue
        content_type = file.content_type or ""
        
        # Check file type
        if content_type not in ALLOWED_IMAGE_TYPES and content_type not in ALLOWED_FILE_TYPES:
            continue
        
        # Read file content
        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            continue
        
        file_data = {
            "name": file.filename,
            "mime_type": content_type,
            "data": base64.b64encode(content).decode("utf-8"),
            "is_image": content_type in ALLOWED_IMAGE_TYPES,
        }
        processed.append(file_data)
    
    return processed


@app.post("/api/chat/stream")
async def chat_stream(
    request: Request,
    question: Optional[str] = Form(None),
    user_type: Optional[str] = Form("free"),
    history: Optional[str] = Form("[]"),
    portfolio_context: Optional[str] = Form(None),
    preferred_model: Optional[str] = Form("auto"),
    files: List[UploadFile] = File(default=[]),
):
    # Handle both JSON and FormData requests
    content_type = request.headers.get("content-type", "")
    
    if "application/json" in content_type:
        body_bytes = await request.body()
        body_data = json.loads(body_bytes)
        question = body_data.get("question", "")
        user_type = body_data.get("user_type", "free")
        history = body_data.get("history", [])
        portfolio_context = body_data.get("portfolio_context")
        preferred_model = body_data.get("preferred_model", "auto")
        file_data_list = []
    else:
        # FormData request
        if not question:
            raise HTTPException(status_code=400, detail="Sawaal khali nahi ho sakta")
        try:
            history = json.loads(history) if history else []
        except json.JSONDecodeError:
            history = []
        file_data_list = await process_uploaded_files(files)
    
    # Create a mock ChatRequest for validation
    class MockBody:
        def __init__(self):
            self.question = question
            self.user_type = user_type
            self.portfolio_context = portfolio_context
    
    mock_body = MockBody()
    question = validate_and_rate_limit(request, mock_body)
    portfolio_ctx = (portfolio_context or "").strip() or None
    
    use_live = needs_live_data(question) or bool(portfolio_ctx)
    has_files = len(file_data_list) > 0
    model_pref = (preferred_model or "auto").strip().lower()

    # Check cache first — skip for live market / portfolio / file / specific model
    cached = None if (use_live or has_files or model_pref != "auto") else get_cached_response(question)
    if cached:
        topic = detect_topic(question)

        async def cached_stream():
            thinking_steps = generate_thinking_steps(question)
            yield _sse({"type": "start", "topic": topic})
            async for event in _stream_thinking_steps(thinking_steps):
                yield event
            yield _sse({"type": "token", "content": cached})
            yield _sse({
                "type": "done", "model": "Cache", "topic": topic, "cached": True,
                "thinking_steps": thinking_steps,
            })

        return StreamingResponse(
            cached_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    topic = detect_topic(question)
    user_type_str = user_type or "free"
    history_list = history if isinstance(history, list) else []
    thinking_steps = generate_thinking_steps(question)

    async def generate():
        sentinel = object()
        events: Queue = Queue()
        chart_payload = None
        full_text = ""
        completed_model = None
        completion_meta: dict = {}

        def produce():
            try:
                market_ctx = build_market_context(question)
                events.put(("chart", build_chart_payload(question)))
                for item in arjun.stream(
                    question,
                    user_type=user_type_str,
                    history=history_list,
                    portfolio_context=portfolio_ctx,
                    file_data=file_data_list if has_files else None,
                    preferred_model=preferred_model or "auto",
                    market_context=market_ctx,
                ):
                    events.put(("item", item))
            except Exception as exc:
                events.put(("error", exc))
            finally:
                events.put(("done", sentinel))

        def apply_event(kind, payload):
            nonlocal chart_payload, full_text, completed_model, completion_meta
            if kind == "chart":
                chart_payload = payload
                return None
            if kind == "error":
                raise payload
            if kind == "done":
                return "stop"
            token, model_done, meta = payload
            sse = None
            if token:
                full_text += token
                sse = _sse({"type": "token", "content": token})
            if model_done is not None:
                completed_model = model_done
                completion_meta = meta or {}
            return sse

        threading.Thread(target=produce, daemon=True).start()

        yield _sse({
            "type": "start",
            "topic": topic,
            "chart_data": None,
        })

        llm_done = False
        async for event in _stream_thinking_steps(thinking_steps):
            yield event
            while not llm_done:
                try:
                    kind, payload = events.get_nowait()
                except Empty:
                    break
                result = apply_event(kind, payload)
                if result == "stop":
                    llm_done = True
                    break
                if result:
                    yield result

        loop = asyncio.get_running_loop()
        while not llm_done:
            kind, payload = await loop.run_in_executor(None, events.get)
            result = apply_event(kind, payload)
            if result == "stop":
                break
            if result:
                yield result

        if completed_model and completed_model != "error" and full_text and not has_files and model_pref == "auto":
            set_cached_response(question, full_text)

        yield _sse({
            "type": "done",
            "model": completed_model or "Error",
            "topic": topic,
            "cached": False,
            "sources": completion_meta.get("sources", []),
            "search_queries": completion_meta.get("search_queries", []),
            "grounded": completion_meta.get("grounded", False),
            "chart_data": chart_payload,
            "thinking_steps": thinking_steps,
        })

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
