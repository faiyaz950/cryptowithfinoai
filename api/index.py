"""Vercel entrypoint for the merged backend.

Vercel's Python runtime module-level ASGI ``app`` serve karta hai jo yahan milti hai.
Asli app ek directory upar ``main.py`` mein hai (FastAPI + mounted Flask), isliye
us directory ko import se pehle sys.path par daala jaata hai. ``vercel.json`` ka
``includeFiles`` sibling modules ko bundle mein le aata hai.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app  # noqa: E402

__all__ = ["app"]
