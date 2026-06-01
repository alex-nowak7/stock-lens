"""
Stock Lens — web backend.

A thin FastAPI wrapper around stock_lens.py. It:
  • serves the landing page at  /
  • generates a report fragment at  /analyze?ticker=AAPL
  • caches each ticker's report for ~15 minutes (so we don't hammer Yahoo)
  • rate-limits each visitor to a sane number of requests per minute

Run locally:   uvicorn server:app --reload
Then open:     http://127.0.0.1:8000
"""

import time
import threading
from collections import defaultdict, OrderedDict

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

import stock_lens as sl
from landing import LANDING_PAGE

app = FastAPI(title="Stock Lens")

# ------------------------------------------------------------
#  Simple in-memory cache  (ticker -> (timestamp, result_dict))
#  15-minute TTL. In-memory is fine for a single small instance;
#  if you scale to multiple instances later, swap in Redis.
# ------------------------------------------------------------
CACHE_TTL = 15 * 60          # seconds
CACHE_MAX = 200              # cap entries so memory can't grow forever
_cache = OrderedDict()
_cache_lock = threading.Lock()


def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        ts, value = item
        if time.time() - ts > CACHE_TTL:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)      # mark as recently used
        return value


def cache_set(key, value):
    with _cache_lock:
        _cache[key] = (time.time(), value)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)  # evict oldest


# ------------------------------------------------------------
#  Basic per-IP rate limit  (sliding 60-second window)
# ------------------------------------------------------------
RATE_LIMIT = 20              # requests
RATE_WINDOW = 60             # seconds
_hits = defaultdict(list)
_hits_lock = threading.Lock()


def rate_limited(ip):
    now = time.time()
    with _hits_lock:
        q = _hits[ip]
        # drop timestamps outside the window
        while q and now - q[0] > RATE_WINDOW:
            q.pop(0)
        if len(q) >= RATE_LIMIT:
            return True
        q.append(now)
        return False


def client_ip(request: Request):
    # Render/most hosts put the real IP in X-Forwarded-For
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------
#  Routes
# ------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return LANDING_PAGE


@app.get("/healthz", response_class=PlainTextResponse)
def health():
    return "ok"


@app.get("/analyze")
def analyze(request: Request, ticker: str = ""):
    ip = client_ip(request)
    if rate_limited(ip):
        return JSONResponse(
            {"ok": False, "error": "You're going a little fast — please wait a few seconds and try again."},
            status_code=429,
        )

    t = (ticker or "").strip().upper()
    if not t:
        return JSONResponse({"ok": False, "error": "Please enter a ticker symbol."}, status_code=400)

    cached = cache_get(t)
    if cached is not None:
        return JSONResponse({**cached, "cached": True})

    result = sl.generate_report(t)   # never raises; returns a dict
    if result.get("ok"):
        payload = {"ok": True, "ticker": result["ticker"],
                   "html": result["html"], "partial": result.get("partial", False)}
        cache_set(t, payload)
        return JSONResponse({**payload, "cached": False})

    # error path — don't cache failures (a retry might succeed)
    return JSONResponse({"ok": False, "ticker": t, "error": result.get("error", "Unknown error.")},
                        status_code=200)
