import os
import re
import base64
import logging
import time
from typing import Generator, Optional, List
from google import genai
from google.genai import types as genai_types
from openai import OpenAI
import anthropic
from dotenv import load_dotenv
from .system_prompt import get_arjunai_prompt
from .market_data import build_market_context, is_market_news_question
from .grounding import extract_grounding_sources, extract_search_queries

load_dotenv()

CRYPTO_KEYWORDS = {
    "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency", "altcoin",
    "defi", "nft", "blockchain", "solana", "sol", "bnb", "xrp", "ripple",
    "cardano", "ada", "polygon", "matic", "doge", "shib", "web3", "token",
    "staking", "yield", "wallet", "coinmarketcap", "wazirx", "coindcx",
    "halving", "dominance", "memecoin", "usdt", "usdc", "stablecoin",
}

MF_KEYWORDS = {
    "mutual fund", "sip", "nav", "elss", "index fund", "nfo", "amc",
    "large cap", "mid cap", "small cap", "flexi cap", "debt fund",
    "liquid fund", "hybrid fund", "expense ratio", "sharpe", "sortino",
    "sbi mf", "hdfc mf", "mirae", "parag parikh", "quant mf", "nippon",
    "stp", "swp", "idcw", "growth plan", "direct plan", "regular plan",
    "lumpsum", "corpus", "cagr", "xirr",
}

STOCK_KEYWORDS = {
    "nse", "bse", "nifty", "sensex", "stock", "share", "equity",
    "ipo", "p/e", "pe ratio", "roe", "roce", "eps", "market cap",
    "fii", "dii", "bulk deal", "block deal", "f&o", "futures", "options",
    "put", "call", "oi", "open interest", "dividend", "buyback", "bonus",
    "technical analysis", "support", "resistance", "rsi", "macd", "ema", "sma",
    "reliance", "tcs", "infosys", "hdfc", "icici", "sbi", "wipro", "adani",
    "tata", "bajaj", "kotak", "maruti", "hul", "itc", "axis bank",
}

COMMODITY_KEYWORDS = {
    "gold", "silver", "crude", "oil", "mcx", "commodity", "ncdex",
    "sgb", "sovereign gold", "gold etf", "copper", "zinc", "natural gas",
}

GEMINI_DEFAULT_MODEL = "gemini-3.6-flash"
GEMINI_FALLBACK_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
)
GEMINI_UNAVAILABLE_ATTEMPTS = 2


def _is_real_key(raw: Optional[str]) -> bool:
    value = (raw or "").strip()
    if not value:
        return False
    lower = value.lower()
    return not (
        lower.startswith("your_")
        or lower.endswith("_here")
        or "placeholder" in lower
        or "example" in lower
    )


def _load_gemini_api_keys() -> list[str]:
    keys: list[str] = []
    seen = set()
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"):
        raw = (os.getenv(name) or "").strip()
        if not _is_real_key(raw) or raw in seen:
            continue
        seen.add(raw)
        keys.append(raw)
    extra = os.getenv("GEMINI_API_KEYS") or ""
    for part in extra.split(","):
        raw = part.strip()
        if not _is_real_key(raw) or raw in seen:
            continue
        seen.add(raw)
        keys.append(raw)
    return keys

MODEL_NAMES = {
    "gemini": "Gemini 3.6 Flash + Search",
    "grok": "Grok 3 Fast",
    "groq": "Groq GPT-OSS",
    "openai": "GPT-4o Mini",
    "claude": "Claude Haiku 4.5",
}

MODEL_DESCRIPTIONS = {
    "auto": "Gemini first — Groq/OpenAI if Gemini is overloaded",
    "gemini": "Google Search grounding + image/file support",
    "grok": "xAI — fast general answers",
    "groq": "Free tier friendly, fast responses",
    "openai": "OpenAI GPT — strong reasoning + image vision",
    "claude": "Pro users ke liye — detailed analysis",
}

VALID_MODEL_IDS = {"auto", "gemini", "grok", "groq", "openai", "claude"}

# Desk sirf Gemini par chalta hai — Google Search grounding ke saath, jo live
# market questions par baaki models se behtar hai, aur ek hi model hone se
# jawab ka lehja aur quality har baar ek jaisi rehti hai. False kar dene par
# purana multi-model routing wapas aa jayega.
GEMINI_ONLY = True


def _normalize_model(preferred_model: Optional[str]) -> str:
    """Gemini-only mode mein har request Gemini par jaati hai."""
    if GEMINI_ONLY:
        return "gemini"
    model = _normalize_model(preferred_model)
    return model if model in VALID_MODEL_IDS else "auto"
VISION_MODEL_IDS = {"auto", "gemini", "openai"}


# Greetings, shukriya, aur "tum kaun ho" type sawaal — inpar search bekaar hai.
_CHITCHAT = re.compile(
    r"^\s*(hi|hey|hello|hii+|namaste|salaam|yo|sup|thanks|thank you|shukriya|"
    r"dhanyawad|ok|okay|thik|theek|acha|achha|bye|good morning|good evening|"
    r"kaise ho|how are you|kaun ho|who are you|what can you do|kya kar sakte ho|"
    r"help|madad)\b[\s!?.]*$",
    re.I,
)


# "tum kaun ho", "aap kya kar sakte ho" — kahin bhi aaye to chit-chat hai.
_IDENTITY = re.compile(
    r"\b(tum|aap|you)\s+(kaun|kya\s+kar|who|what can)\b|\bwho are you\b|\bkaun ho\b",
    re.I,
)


def _is_chitchat(question: str) -> bool:
    q = (question or "").strip()
    if not q:
        return True
    if _CHITCHAT.match(q) or _IDENTITY.search(q):
        return True
    # Bina kisi sawaal ke ek-do shabd — search bhejne layak kuch hai hi nahi.
    return len(q.split()) <= 2 and "?" not in q


# Price / quote poochne wale shabd. Inka jawab exchange ka ticker deta hai.
_PRICE_INTENT = re.compile(
    r"\b(price|rate|value|cmp|ltp|quote|level|bhav|kitna|kitne|kitni|"
    r"chal raha|trading at|kya hai ab|abhi kya)\b",
    re.I,
)


def _is_price_question(question: str) -> bool:
    return bool(_PRICE_INTENT.search(question or ""))


def _wants_search(question: str, market_context: Optional[str]) -> bool:
    """
    Google Search kab chalani hai.

    Pehle ye ek keyword list par chalti thi (`news|ipo|rbi|budget…`), jise
    hamesha maintain karte rehna padta. Ab faisla behaviour se hota hai:

      1. News / khabar / headline  -> hamesha search. Live price hona "kya
         hua aur kyun" ka jawab nahi hota.
      2. Price poocha gaya aur live data maujood -> search nahi. Exchange ka
         apna ticker haath mein hote hue web se price dhoondhna downgrade hai —
         har site alag number deti hai aur koi bhi wo venue nahi jahan user
         trade karta hai. Sirf `market_context` hona kaafi nahi: "bitcoin
         halving kab hai?" par bhi BTC ka price context mein aa jaata hai,
         par jawab price se nahi milta.
      3. Baaki sab kuch           -> search, kyunki AI ki training purani hai
         aur live web usse behtar jawab dega.

    Sirf greetings/chit-chat chhod diye jaate hain.
    """
    if _is_chitchat(question):
        return False
    if is_market_news_question(question):
        return True
    if market_context and _is_price_question(question):
        return False
    return True


def _pretty_gemini_name(model_id: str, grounded: bool = False) -> str:
    raw = (model_id or GEMINI_DEFAULT_MODEL).replace("models/", "").strip()
    parts = raw.split("-")
    if parts and parts[0].lower() == "gemini":
        rest = []
        for part in parts[1:]:
            if part.isalpha():
                rest.append(part.capitalize())
            else:
                rest.append(part)
        name = "Gemini " + " ".join(rest)
    else:
        name = raw
    if grounded and "+ Search" not in name:
        name += " + Search"
    return name


def _extract_gemini_text(response) -> str:
    """Read visible text parts and skip thought / thought_signature chunks."""
    if not response:
        return ""
    texts = []
    for cand in getattr(response, "candidates", None) or []:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            if getattr(part, "thought", False):
                continue
            text = getattr(part, "text", None)
            if text:
                texts.append(text)
    if texts:
        return "".join(texts)
    try:
        return getattr(response, "text", None) or ""
    except Exception:
        return ""


def _kw_matches(kw: str, q: str) -> bool:
    """Word-boundary match for single words; substring for multi-word phrases."""
    if " " in kw:
        return kw in q
    return bool(re.search(r"\b" + re.escape(kw) + r"\b", q))


def detect_topic(question: str) -> str:
    q = question.lower()
    scores = {"crypto": 0, "mutual_fund": 0, "stock": 0, "commodity": 0}
    for kw in CRYPTO_KEYWORDS:
        if _kw_matches(kw, q):
            scores["crypto"] += 1
    for kw in MF_KEYWORDS:
        if _kw_matches(kw, q):
            scores["mutual_fund"] += 1
    for kw in STOCK_KEYWORDS:
        if _kw_matches(kw, q):
            scores["stock"] += 1
    for kw in COMMODITY_KEYWORDS:
        if _kw_matches(kw, q):
            scores["commodity"] += 1
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "general"


class ArjunAI:
    def __init__(self):
        gemini_keys = _load_gemini_api_keys()
        self.gemini_clients = [genai.Client(api_key=k) for k in gemini_keys]
        self.gemini_client = self.gemini_clients[0] if self.gemini_clients else None
        self._gemini_active_index = 0
        self.gemini_model = (os.getenv("GEMINI_MODEL") or GEMINI_DEFAULT_MODEL).strip() or GEMINI_DEFAULT_MODEL
        self.gemini_model_ids = []
        for mid in (self.gemini_model, GEMINI_DEFAULT_MODEL, *GEMINI_FALLBACK_MODELS):
            if mid and mid not in self.gemini_model_ids:
                self.gemini_model_ids.append(mid)

        grok_key = os.getenv("GROK_API_KEY")
        self.grok = OpenAI(api_key=grok_key, base_url="https://api.x.ai/v1") if _is_real_key(grok_key) else None

        groq_key = os.getenv("GROQ_API_KEY")
        self.groq = OpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1") if _is_real_key(groq_key) else None
        groq_model = (os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b").strip()
        self.groq_model_ids = [groq_model] if groq_model else []
        for fallback in ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b"):
            if fallback not in self.groq_model_ids:
                self.groq_model_ids.append(fallback)

        openai_key = os.getenv("OPENAI_API_KEY")
        self.openai = OpenAI(api_key=openai_key) if _is_real_key(openai_key) else None
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        claude_key = os.getenv("CLAUDE_API_KEY")
        self.claude = anthropic.Anthropic(api_key=claude_key) if _is_real_key(claude_key) else None

    def _openai_label(self) -> str:
        labels = {
            "gpt-4o-mini": "GPT-4o Mini",
            "gpt-4o": "GPT-4o",
            "gpt-4.1-mini": "GPT-4.1 Mini",
            "gpt-4.1": "GPT-4.1",
        }
        return labels.get(self.openai_model, f"OpenAI ({self.openai_model})")

    def _friendly_agent_error(self, agent_name: str, err: str) -> Optional[str]:
        err_lower = err.lower()
        if agent_name == "openai" and ("insufficient_quota" in err_lower or "exceeded your current quota" in err_lower):
            return (
                "⚠️ **OpenAI account mein balance/quota nahi hai.**\n\n"
                "[platform.openai.com](https://platform.openai.com/settings/organization/billing) par jao → "
                "**Billing** → payment method add karo (minimum $5 credit).\n\n"
                "Gemini quota/rate limit hit ho gayi. Thodi der baad dobara try karein."
            )
        if agent_name == "openai" and ("invalid_api_key" in err_lower or "incorrect api key" in err_lower):
            return "⚠️ **OpenAI API key galat hai.** `.env` file mein `OPENAI_API_KEY` check karein."
        if agent_name == "gemini" and ("429" in err or "resource_exhausted" in err_lower):
            # Saaf-saaf batao ki free quota khatam hai aur aage kya karna hai —
            # warna user ko lagta hai app toot gaya.
            return (
                "⚠️ **Gemini ka free quota khatam ho gaya hai.**\n\n"
                "Free tier ki daily limit lag chuki hai. Ye apne aap reset hoti hai "
                "(aam taur par agle din), uske baad sab normal chalega.\n\n"
                "**Abhi chalana ho to:**\n"
                "- Backend ke `.env` mein ek aur key daalein — `GEMINI_API_KEY_2` "
                "(teen keys tak rotate ho jaati hain)\n"
                "- Ya [Google AI Studio](https://aistudio.google.com/app/apikey) se "
                "paid plan le lein"
            )
        if agent_name == "gemini" and ("503" in err or "unavailable" in err_lower):
            return (
                "Gemini abhi overloaded hai (high demand). "
                "2-3 second baad dobara try karein."
            )
        if agent_name == "gemini" and ("404" in err or "not_found" in err_lower):
            return (
                "⚠️ **Gemini model available nahi hai.** "
                f"`.env` mein `GEMINI_MODEL={GEMINI_DEFAULT_MODEL}` set karke backend restart karein."
            )
        if agent_name == "openai" and "429" in err:
            return (
                "⚠️ **OpenAI rate limit hit ho gayi.** Thodi der baad try karein."
            )
        return None

    def _is_configured(self, agent: str) -> bool:
        return {
            "gemini": bool(self.gemini_clients),
            "grok": bool(self.grok),
            "groq": bool(self.groq),
            "openai": bool(self.openai),
            "claude": bool(self.claude),
        }.get(agent, False)

    def get_available_models(self, user_type: str = "free") -> list[dict]:
        if GEMINI_ONLY:
            return [{
                "id": "gemini",
                "label": MODEL_NAMES["gemini"],
                "description": MODEL_DESCRIPTIONS["gemini"],
                "available": self._is_configured("gemini"),
            }]

        models = [{
            "id": "auto",
            "label": "Auto (Smart)",
            "description": MODEL_DESCRIPTIONS["auto"],
            "available": True,
        }]
        for agent_id in ("gemini", "groq", "openai", "grok"):
            if self._is_configured(agent_id):
                if agent_id == "openai":
                    label = self._openai_label()
                elif agent_id == "gemini":
                    label = _pretty_gemini_name(self.gemini_model, grounded=True)
                else:
                    label = MODEL_NAMES[agent_id]
                models.append({
                    "id": agent_id,
                    "label": label,
                    "description": MODEL_DESCRIPTIONS[agent_id],
                    "available": True,
                })
        if user_type == "pro" and self._is_configured("claude"):
            models.append({
                "id": "claude",
                "label": MODEL_NAMES["claude"],
                "description": MODEL_DESCRIPTIONS["claude"],
                "available": True,
                "pro_only": True,
            })
        return models

    def _build_agent_list(self, preferred_model: Optional[str], user_type: str, streaming: bool) -> list:
        fallback_order = ["gemini", "groq", "openai", "grok"]
        if user_type == "pro":
            fallback_order.append("claude")

        prefix = "_stream_" if streaming else "_try_"
        method_map = {name: getattr(self, prefix + name) for name in fallback_order}

        model = _normalize_model(preferred_model)

        if model == "claude" and user_type != "pro":
            return []

        configured = [(n, method_map[n]) for n in fallback_order if self._is_configured(n)]
        if not configured:
            return []
        if model == "auto":
            return configured

        preferred = [(n, fn) for n, fn in configured if n == model]
        if GEMINI_ONLY:
            # Chup-chaap doosre provider par mat jao — Gemini ki apni multi-key
            # rotation quota sambhalti hai, aur silent switch se jawab ka lehja
            # aur grounding dono badal jaate hain.
            return preferred
        rest = [(n, fn) for n, fn in configured if n != model]
        return preferred + rest

    def _build_messages(self, question: str, history: list) -> list:
        messages = []
        for msg in history[-12:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": question})
        return messages

    def _build_openai_messages(
        self,
        question: str,
        history: list,
        portfolio_context: Optional[str] = None,
        market_context: Optional[str] = None,
        file_data: Optional[List[dict]] = None,
    ) -> list:
        messages = [{"role": "system", "content": get_arjunai_prompt(portfolio_context, market_context)}]
        for msg in history[-12:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

        if file_data:
            parts: list = []
            for fd in file_data:
                if fd.get("is_image"):
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{fd['mime_type']};base64,{fd['data']}"},
                    })
                elif fd["mime_type"] in ("text/plain", "text/csv"):
                    text_content = base64.b64decode(fd["data"]).decode("utf-8", errors="ignore")
                    parts.append({
                        "type": "text",
                        "text": f"[File: {fd['name']}]\n```\n{text_content[:10000]}\n```",
                    })
                else:
                    parts.append({
                        "type": "text",
                        "text": f"[Attached: {fd['name']} — PDF ke liye Gemini model best hai]",
                    })
            parts.append({"type": "text", "text": question})
            messages.append({"role": "user", "content": parts})
        else:
            messages.append({"role": "user", "content": question})
        return messages

    def _build_gemini_contents(self, question: str, history: list, file_data: Optional[List[dict]] = None):
        history_blob = []
        for msg in history[-4:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            text = content.strip()
            if not text:
                continue
            if len(text) > 1200:
                text = text[:1200] + "\n…"
            label = "User" if role == "user" else "Assistant"
            history_blob.append(f"{label}: {text}")

        prompt = question
        if history_blob:
            prompt = "Previous conversation:\n" + "\n\n".join(history_blob) + "\n\nUser: " + question

        if not file_data:
            return prompt

        parts = []
        for fd in file_data:
            if fd.get("is_image"):
                parts.append(genai_types.Part(
                    inline_data=genai_types.Blob(
                        mime_type=fd["mime_type"],
                        data=base64.b64decode(fd["data"]),
                    )
                ))
                continue
            try:
                if fd["mime_type"] in ("text/plain", "text/csv"):
                    text_content = base64.b64decode(fd["data"]).decode("utf-8", errors="ignore")
                    parts.append(genai_types.Part(
                        text=f"[File: {fd['name']}]\n```\n{text_content[:10000]}\n```\n"
                    ))
                elif fd["mime_type"] == "application/pdf":
                    parts.append(genai_types.Part(
                        inline_data=genai_types.Blob(
                            mime_type=fd["mime_type"],
                            data=base64.b64decode(fd["data"]),
                        )
                    ))
            except Exception:
                pass
        parts.append(genai_types.Part(text=prompt))
        return [genai_types.Content(role="user", parts=parts)]

    def _gemini_config(self, portfolio_context: Optional[str], market_context: Optional[str], question: str, enable_search: bool = False, model_id: Optional[str] = None):
        search_note = ""
        tools = None
        if enable_search:
            search_note = (
                "\n\n🔍 GOOGLE SEARCH REQUIRED: Is sawaal ke liye pehle Google Search chalao — "
                "latest news, IPO, NAV, RBI policy, earnings ya aaj ki market updates fetch karo. "
                "Kam se kam 6-8 detailed points ke saath poora jawab do — kabhi beech mein mat ruko."
            )
            tools = [genai_types.Tool(google_search=genai_types.GoogleSearch())]
        kwargs = {
            "system_instruction": get_arjunai_prompt(portfolio_context, market_context) + search_note,
            "max_output_tokens": 8192,
        }
        # New Gemini projects reject thinking_budget=0 on 3.6 and
        # 3.5 often returns thought_signature-only MALFORMED_FUNCTION_CALL.
        if tools:
            kwargs["tools"] = tools
        elif model_id and "3.6" not in model_id:
            kwargs["tool_config"] = genai_types.ToolConfig(
                function_calling_config=genai_types.FunctionCallingConfig(
                    mode=genai_types.FunctionCallingConfigMode.NONE,
                )
            )
        return genai_types.GenerateContentConfig(**kwargs)

    def _gemini_meta(self, response) -> dict:
        sources = extract_grounding_sources(response)
        queries = extract_search_queries(response)
        return {"sources": sources, "search_queries": queries, "grounded": bool(sources or queries)}

    def _gemini_is_quota_error(self, err: str) -> bool:
        lower = err.lower()
        return "429" in err or "resource_exhausted" in lower

    def _gemini_retry_action(self, err: str, enable_search: bool) -> str:
        """retry_same | retry_no_search | next_model | raise"""
        lower = err.lower()
        if "unavailable" in lower or "503" in err:
            return "next_model"
        retry_without_search = enable_search and (
            self._gemini_is_quota_error(err)
            or "empty gemini response" in lower
            or "invalid_argument" in lower
        )
        if retry_without_search:
            return "retry_no_search"
        if (
            self._gemini_is_quota_error(err)
            or self._gemini_is_missing_model(err)
            or "empty gemini response" in lower
            or "invalid_argument" in lower
        ):
            return "next_model"
        return "raise"

    def _gemini_is_missing_model(self, err: str) -> bool:
        lower = err.lower()
        return "404" in err or "not_found" in lower or "no longer available" in lower

    def _iter_gemini_clients(self):
        n = len(self.gemini_clients)
        if n == 0:
            return
        start = self._gemini_active_index % n
        for offset in range(n):
            yield (start + offset) % n, self.gemini_clients[(start + offset) % n]

    # ── Non-streaming (fallback / cache path) ────────────────────────────────

    def _try_gemini(self, question: str, history: list, portfolio_context: Optional[str] = None, market_context: Optional[str] = None, file_data: Optional[List[dict]] = None):
        if not self.gemini_clients:
            raise Exception("Gemini not configured")

        want_search = _wants_search(question, market_context)
        last_err: Optional[Exception] = None
        contents = self._build_gemini_contents(question, history, file_data)
        for key_i, client in self._iter_gemini_clients():
            for model_id in self.gemini_model_ids:
                quota_hit = False
                skip_model = False
                # Search WITH pehle, bina-search baad mein. Pehle ulta tha —
                # non-search call pehle chalti thi, succeed hoti thi aur seedha
                # return kar jaati thi, to `True` tak control kabhi pahunchta hi
                # nahi tha aur Google Search grounding kabhi chalti hi nahi thi.
                # `_gemini_retry_action` ka "retry_no_search" isi fallback ke
                # liye likha tha — ab wo asli mein kaam karta hai.
                for enable_search in ([True, False] if want_search else [False]):
                    if skip_model:
                        break
                    for attempt in range(GEMINI_UNAVAILABLE_ATTEMPTS):
                        try:
                            response = client.models.generate_content(
                                model=model_id,
                                contents=contents,
                                config=self._gemini_config(portfolio_context, market_context, question, enable_search, model_id),
                            )
                            text = _extract_gemini_text(response)
                            if not text.strip():
                                raise Exception("Empty Gemini response")
                            meta = self._gemini_meta(response)
                            meta["model_id"] = model_id
                            self._gemini_active_index = key_i
                            return text, _pretty_gemini_name(model_id, bool(meta.get("grounded"))), meta
                        except Exception as e:
                            last_err = e
                            err = str(e)
                            logging.warning(
                                "Gemini key %s %s search=%s attempt %s failed: %s",
                                key_i + 1, model_id, enable_search, attempt + 1, err[:300],
                            )
                            if self._gemini_is_quota_error(err):
                                quota_hit = True
                                break
                            action = self._gemini_retry_action(err, enable_search)
                            if action == "retry_same" and attempt < GEMINI_UNAVAILABLE_ATTEMPTS - 1:
                                time.sleep(0.7 * (attempt + 1))
                                continue
                            if action == "retry_no_search":
                                break
                            if action in ("next_model", "retry_same"):
                                skip_model = True
                                break
                            raise
                    if quota_hit:
                        break
                if quota_hit:
                    logging.warning("Gemini key %s quota hit, trying next key", key_i + 1)
                    break
        raise last_err or Exception("Gemini request failed")

    def _try_grok(self, question: str, history: list, portfolio_context: Optional[str] = None, market_context: Optional[str] = None):
        if not self.grok:
            raise Exception("Grok not configured")
        messages = [{"role": "system", "content": get_arjunai_prompt(portfolio_context, market_context)}]
        messages.extend(self._build_messages(question, history))
        response = self.grok.chat.completions.create(
            model="grok-3-fast", messages=messages, max_tokens=4096,
        )
        return response.choices[0].message.content, MODEL_NAMES["grok"]

    def _try_groq(self, question: str, history: list, portfolio_context: Optional[str] = None, market_context: Optional[str] = None):
        if not self.groq:
            raise Exception("Groq not configured")
        messages = [{"role": "system", "content": get_arjunai_prompt(portfolio_context, market_context)}]
        messages.extend(self._build_messages(question, history))
        last_err: Optional[Exception] = None
        for model_id in self.groq_model_ids:
            try:
                response = self.groq.chat.completions.create(
                    model=model_id, messages=messages, max_tokens=4096,
                )
                text = response.choices[0].message.content or ""
                if not text.strip():
                    raise Exception("Empty Groq response")
                return text, MODEL_NAMES["groq"]
            except Exception as e:
                last_err = e
                logging.warning("Groq %s failed: %s", model_id, str(e)[:300])
        raise last_err or Exception("Groq request failed")

    def _try_openai(
        self,
        question: str,
        history: list,
        portfolio_context: Optional[str] = None,
        market_context: Optional[str] = None,
        file_data: Optional[List[dict]] = None,
    ):
        if not self.openai:
            raise Exception("OpenAI not configured")
        messages = self._build_openai_messages(
            question, history, portfolio_context, market_context, file_data
        )
        response = self.openai.chat.completions.create(
            model=self.openai_model,
            messages=messages,
            max_tokens=4096,
        )
        text = response.choices[0].message.content or ""
        if not text.strip():
            raise Exception("Empty OpenAI response")
        return text, self._openai_label()

    def _try_claude(self, question: str, history: list, portfolio_context: Optional[str] = None, market_context: Optional[str] = None):
        if not self.claude:
            raise Exception("Claude not configured")
        response = self.claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            system=get_arjunai_prompt(portfolio_context, market_context),
            messages=self._build_messages(question, history),
        )
        return response.content[0].text, MODEL_NAMES["claude"]

    # ── Streaming ─────────────────────────────────────────────────────────────

    def _stream_gemini(self, question: str, history: list, portfolio_context: Optional[str] = None, market_context: Optional[str] = None, file_data: Optional[List[dict]] = None):
        if not self.gemini_clients:
            raise Exception("Gemini not configured")

        contents = self._build_gemini_contents(question, history, file_data)
        has_images = file_data and any(fd.get("is_image") for fd in file_data)

        # UI yahi streaming path use karta hai, aur yahan search hardcoded False
        # thi — isliye app mein Google Search grounding kabhi chalti hi nahi
        # thi, chahe sawaal news/IPO/macro ka ho. Live market context hone par
        # ab bhi band rehti hai: exchange ka apna ticker maujood ho to web se
        # price dhoondhna ulta kharaab hai.
        want_search = _wants_search(question, market_context) and not has_images

        last_err: Optional[Exception] = None
        for key_i, client in self._iter_gemini_clients():
            for model_id in self.gemini_model_ids:
                quota_hit = False
                for attempt in range(GEMINI_UNAVAILABLE_ATTEMPTS):
                    # Search sirf pehle attempt par; wo fail ho to baaki
                    # attempts bina search, taaki jawab phir bhi mile.
                    enable_search = want_search and attempt == 0
                    config = self._gemini_config(portfolio_context, market_context, question, enable_search, model_id)
                    try:
                        if has_images:
                            response = client.models.generate_content(
                                model=model_id, contents=contents, config=config,
                            )
                            text = _extract_gemini_text(response)
                            if not text.strip():
                                raise Exception("Empty Gemini response")
                            meta = self._gemini_meta(response)
                            meta["model_id"] = model_id
                            self._gemini_active_index = key_i
                            for i in range(0, len(text), 80):
                                yield text[i:i + 80], None
                            yield "", {"agent": "gemini", **meta}
                            return

                        response = client.models.generate_content_stream(
                            model=model_id, contents=contents, config=config,
                        )
                        last_chunk = None
                        collected = []
                        emitted = ""
                        for chunk in response:
                            last_chunk = chunk
                            snapshot = _extract_gemini_text(chunk)
                            if not snapshot:
                                continue
                            if snapshot == emitted:
                                continue
                            if snapshot.startswith(emitted):
                                token = snapshot[len(emitted):]
                                emitted = snapshot
                            else:
                                token = snapshot
                                emitted += snapshot
                            if token:
                                collected.append(token)
                                yield token, None
                        if not collected:
                            response = client.models.generate_content(
                                model=model_id, contents=contents, config=config,
                            )
                            text = _extract_gemini_text(response)
                            if not text.strip():
                                raise Exception("Empty Gemini response")
                            meta = self._gemini_meta(response)
                            meta["model_id"] = model_id
                            self._gemini_active_index = key_i
                            for i in range(0, len(text), 80):
                                yield text[i:i + 80], None
                            yield "", {"agent": "gemini", **meta}
                            return
                        meta = self._gemini_meta(last_chunk)
                        meta["model_id"] = model_id
                        self._gemini_active_index = key_i
                        yield "", {"agent": "gemini", **meta}
                        return
                    except Exception as e:
                        last_err = e
                        err = str(e)
                        logging.warning(
                            "Gemini stream key %s %s attempt %s failed: %s",
                            key_i + 1, model_id, attempt + 1, err[:300],
                        )
                        if self._gemini_is_quota_error(err):
                            quota_hit = True
                            break
                        action = self._gemini_retry_action(err, False)
                        if action == "retry_same" and attempt < GEMINI_UNAVAILABLE_ATTEMPTS - 1:
                            time.sleep(0.7 * (attempt + 1))
                            continue
                        if action in ("retry_no_search", "next_model", "retry_same"):
                            break
                        raise
                if quota_hit:
                    logging.warning("Gemini key %s quota hit, trying next key", key_i + 1)
                    break
        raise last_err or Exception("Gemini request failed")

    def _stream_grok(self, question: str, history: list, portfolio_context: Optional[str] = None, market_context: Optional[str] = None) -> Generator[str, None, None]:
        if not self.grok:
            raise Exception("Grok not configured")
        messages = [{"role": "system", "content": get_arjunai_prompt(portfolio_context, market_context)}]
        messages.extend(self._build_messages(question, history))
        stream = self.grok.chat.completions.create(
            model="grok-3-fast", messages=messages, max_tokens=4096, stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta, None
        yield "", {"agent": "grok"}

    def _stream_groq(self, question: str, history: list, portfolio_context: Optional[str] = None, market_context: Optional[str] = None):
        if not self.groq:
            raise Exception("Groq not configured")
        messages = [{"role": "system", "content": get_arjunai_prompt(portfolio_context, market_context)}]
        messages.extend(self._build_messages(question, history))
        last_err: Optional[Exception] = None
        for model_id in self.groq_model_ids:
            try:
                stream = self.groq.chat.completions.create(
                    model=model_id, messages=messages, max_tokens=4096, stream=True,
                )
                any_text = False
                for chunk in stream:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        any_text = True
                        yield delta, None
                if not any_text:
                    raise Exception("Empty Groq response")
                yield "", {"agent": "groq"}
                return
            except Exception as e:
                last_err = e
                logging.warning("Groq stream %s failed: %s", model_id, str(e)[:300])
        raise last_err or Exception("Groq request failed")

    def _stream_openai(
        self,
        question: str,
        history: list,
        portfolio_context: Optional[str] = None,
        market_context: Optional[str] = None,
        file_data: Optional[List[dict]] = None,
    ):
        if not self.openai:
            raise Exception("OpenAI not configured")
        messages = self._build_openai_messages(
            question, history, portfolio_context, market_context, file_data
        )
        use_buffered = bool(file_data)

        if use_buffered:
            response = self.openai.chat.completions.create(
                model=self.openai_model,
                messages=messages,
                max_tokens=4096,
            )
            text = response.choices[0].message.content or ""
            if not text.strip():
                raise Exception("Empty OpenAI response")
            chunk_size = 48
            for i in range(0, len(text), chunk_size):
                yield text[i:i + chunk_size], None
            yield "", {"agent": "openai"}
            return

        stream = self.openai.chat.completions.create(
            model=self.openai_model,
            messages=messages,
            max_tokens=4096,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta, None
        yield "", {"agent": "openai"}

    def _stream_claude(self, question: str, history: list, portfolio_context: Optional[str] = None, market_context: Optional[str] = None):
        if not self.claude:
            raise Exception("Claude not configured")
        with self.claude.messages.stream(
            model="claude-haiku-4-5",
            max_tokens=4096,
            system=get_arjunai_prompt(portfolio_context, market_context),
            messages=self._build_messages(question, history),
        ) as stream:
            for text in stream.text_stream:
                yield text, None
        yield "", {"agent": "claude"}

    # ── Public API ────────────────────────────────────────────────────────────

    def ask(self, question: str, user_type: str = "free", history: list = None, portfolio_context: Optional[str] = None, file_data: Optional[List[dict]] = None, preferred_model: Optional[str] = None):
        if history is None:
            history = []

        topic = detect_topic(question)
        market_context = build_market_context(question)
        model_pref = _normalize_model(preferred_model)

        if file_data:
            if model_pref not in VISION_MODEL_IDS:
                return {
                    "answer": "Image/file analysis abhi available nahi — Gemini configure nahi hai.",
                    "model": "Error",
                    "topic": topic,
                    "cached": False,
                }

            file_agents = []
            if model_pref in ("gemini", "auto") and self._is_configured("gemini"):
                file_agents.append(("gemini", self._try_gemini))
            if model_pref in ("openai", "auto") and self._is_configured("openai"):
                file_agents.append(("openai", self._try_openai))
            if model_pref == "gemini" and not file_agents:
                file_agents = []
            elif model_pref == "openai" and not file_agents:
                file_agents = []

            errors = []
            for agent_name, agent_fn in file_agents:
                try:
                    kwargs = {
                        "portfolio_context": portfolio_context,
                        "market_context": market_context,
                        "file_data": file_data,
                    }
                    result = agent_fn(question, history, **kwargs)
                    if len(result) == 3:
                        answer, model_name, meta = result
                    else:
                        answer, model_name = result
                        meta = {}
                    return {
                        "answer": answer,
                        "model": model_name,
                        "topic": topic,
                        "cached": False,
                        "sources": meta.get("sources", []),
                        "search_queries": meta.get("search_queries", []),
                        "grounded": meta.get("grounded", False),
                    }
                except Exception as e:
                    errors.append("%s: %s" % (agent_name, str(e)))
                    if model_pref != "auto":
                        break

            return {
                "answer": "Image/file process karne mein error aaya. Thodi der baad dobara try karein.",
                "model": "Error",
                "topic": topic,
                "cached": False,
                "errors": errors,
            }

        agents = self._build_agent_list(preferred_model, user_type, streaming=False)
        if not agents:
            label = MODEL_NAMES.get(model_pref, model_pref)
            return {
                "answer": f"**{label}** abhi configure nahi hai — backend mein GEMINI_API_KEY set karein.",
                "model": "Error",
                "topic": topic,
                "cached": False,
            }

        errors = []
        for agent_name, agent_fn in agents:
            try:
                kwargs = {"portfolio_context": portfolio_context, "market_context": market_context}
                result = agent_fn(question, history, **kwargs)
                if len(result) == 3:
                    answer, model_name, meta = result
                else:
                    answer, model_name = result
                    meta = {}
                return {
                    "answer": answer,
                    "model": model_name,
                    "topic": topic,
                    "cached": False,
                    "sources": meta.get("sources", []),
                    "search_queries": meta.get("search_queries", []),
                    "grounded": meta.get("grounded", False),
                }
            except Exception as e:
                err = str(e)
                errors.append("%s: %s" % (agent_name, err))
                logging.warning("Agent %s failed: %s", agent_name, e)

        last_err = errors[-1] if errors else ""
        agent_from_err = last_err.split(": ", 1)[0] if last_err else ""
        friendly = self._friendly_agent_error(agent_from_err, last_err) if last_err else None
        if friendly:
            return {
                "answer": friendly,
                "model": "Quota Limit" if "quota" in last_err.lower() or "429" in last_err else "Error",
                "topic": topic,
                "cached": False,
                "errors": errors,
            }

        return {
            "answer": "Abhi mujhe jawab dene mein problem ho rahi hai. Thodi der mein dobara koshish karein.",
            "model": "Error",
            "topic": topic,
            "cached": False,
            "errors": errors,
        }

    def stream(self, question: str, user_type: str = "free", history: list = None, portfolio_context: Optional[str] = None, file_data: Optional[List[dict]] = None, preferred_model: Optional[str] = None, market_context: Optional[str] = None):
        """
        Yields (token: str, agent_name: str | None, meta: dict | None) tuples.
        agent_name is set on completion; meta has sources/search_queries for Gemini.
        """
        if history is None:
            history = []

        if market_context is None:
            market_context = build_market_context(question)
        model_pref = _normalize_model(preferred_model)

        if file_data:
            if model_pref not in VISION_MODEL_IDS:
                yield "Image/file analysis abhi available nahi — Gemini configure nahi hai.", "Error", None
                return

            file_agents = []
            if model_pref in ("gemini", "auto") and self._is_configured("gemini"):
                file_agents.append(("gemini", self._stream_gemini))
            if model_pref in ("openai", "auto") and self._is_configured("openai"):
                file_agents.append(("openai", self._stream_openai))

            for agent_name, agent_fn in file_agents:
                try:
                    completion_meta: dict = {}
                    for token, done_meta in agent_fn(
                        question, history,
                        portfolio_context=portfolio_context,
                        market_context=market_context,
                        file_data=file_data,
                    ):
                        if done_meta:
                            completion_meta = done_meta
                        elif token:
                            yield token, None, None
                    agent = completion_meta.get("agent", agent_name)
                    if agent == "gemini":
                        model_name = _pretty_gemini_name(
                            completion_meta.get("model_id") or self.gemini_model,
                            bool(completion_meta.get("grounded")),
                        )
                    elif agent == "openai":
                        model_name = self._openai_label()
                    else:
                        model_name = MODEL_NAMES.get(agent, "Error")
                    yield "", model_name, completion_meta
                    return
                except Exception as e:
                    # `import logging` yahan pehle local tha, jisse poore stream()
                    # function ke liye `logging` local naam ban jaata tha — aur
                    # usi function mein upar wale logging.warning() calls
                    # UnboundLocalError phenkte the. Isi wajah se quota error par
                    # friendly message yield hone se pehle hi stream mar jaata tha.
                    logging.warning("File agent %s failed: %s", agent_name, e)
                    if model_pref != "auto":
                        break

            yield "Image/file process karne mein error aaya. Thodi der baad dobara try karein.", "Error", None
            return

        agents = self._build_agent_list(preferred_model, user_type, streaming=True)
        if not agents:
            label = MODEL_NAMES.get(model_pref, model_pref)
            yield f"**{label}** abhi configure nahi hai — backend mein GEMINI_API_KEY set karein.", "Error", None
            return

        errors = []
        for agent_name, agent_fn in agents:
            emitted = False
            try:
                kwargs = {"portfolio_context": portfolio_context, "market_context": market_context}
                completion_meta: dict = {}
                for token, done_meta in agent_fn(question, history, **kwargs):
                    if done_meta:
                        completion_meta = done_meta
                    elif token:
                        emitted = True
                        yield token, None, None
                agent = completion_meta.get("agent", agent_name)
                if agent == "openai":
                    model_name = self._openai_label()
                elif agent == "gemini":
                    model_name = _pretty_gemini_name(
                        completion_meta.get("model_id") or self.gemini_model,
                        bool(completion_meta.get("grounded")),
                    )
                else:
                    model_name = MODEL_NAMES.get(agent, "Error")
                yield "", model_name, completion_meta
                return
            except Exception as e:
                err = str(e)
                errors.append("%s: %s" % (agent_name, err))
                logging.warning("Agent %s failed: %s", agent_name, e)
                if emitted:
                    yield "", MODEL_NAMES.get(agent_name, "Error"), None
                    return
                continue

        last_err = errors[-1] if errors else ""
        for agent_name, err in ((e.split(": ", 1)[0], e) for e in errors):
            friendly = self._friendly_agent_error(agent_name, err)
            if friendly:
                yield friendly, "Quota Limit" if "quota" in err.lower() or "429" in err else "Error", None
                return

        yield "Abhi jawab nahi aa raha. Thodi der mein dobara koshish karein.", "error", None
