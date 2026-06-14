"""
Finnhub adapter for Stock Lens.

Replaces the heaviest yfinance calls (.info + peer info fetches) with Finnhub
API calls that work reliably from cloud server IPs like Render. The renderer
in stock_lens.py doesn't change — each function here returns data in the same
shape yfinance returns, so the swap is surgical.

Free Finnhub tier covers everything in this file (60 calls/min, no card).
  • Get a key:  https://finnhub.io/dashboard
  • Set env var FINNHUB_API_KEY before calling any function in this module.
"""

import os
import time
import math
import concurrent.futures as cf

import requests
import pandas as pd

# Load .env file if present (for local development).
# In production (Render), env vars come from the host so dotenv isn't needed.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
BASE = "https://finnhub.io/api/v1"
TIMEOUT = 8  # seconds per HTTP request


# ============================================================
#  HTTP plumbing — every external call goes through here
# ============================================================
def _get(path, params=None, retries=1):
    """One GET to Finnhub. Returns parsed JSON, or None on any failure.
    Never raises — callers can treat None as 'no data'."""
    if not FINNHUB_KEY:
        return None
    p = dict(params or {})
    p["token"] = FINNHUB_KEY
    for attempt in range(retries + 1):
        try:
            r = requests.get(BASE + path, params=p, timeout=TIMEOUT)
            if r.status_code == 429:               # rate-limited; brief backoff and retry
                time.sleep(0.7 * (attempt + 1))
                continue
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            return None
    return None


# ============================================================
#  Small helpers — keep number handling consistent
# ============================================================
def _f(x):
    """Float or None, coercing strings, rejecting NaN/inf."""
    if x is None:
        return None
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def _pct_to_dec(x):
    """Finnhub returns margins/yields as percentages (25.0); yfinance returns
    them as decimals (0.25). Divide by 100 so the renderer sees yfinance's format."""
    v = _f(x)
    return None if v is None else v / 100.0


# ============================================================
#  Metric key mapping:  yfinance info key  ->  Finnhub metric key
# ============================================================
# Direct numeric pass-through (no unit conversion needed).
_METRIC_DIRECT = {
    # valuation
    "trailingPE":                    "peBasicExclExtraTTM",
    "priceToBook":                   "pbAnnual",
    "priceToSalesTrailing12Months":  "psTTM",
    "enterpriseToEbitda":            "currentEv/ebitdaAnnual",
    # risk / capital structure
    "beta":                          "beta",
    "debtToEquity":                  "totalDebt/totalEquityAnnual",
    "currentRatio":                  "currentRatioAnnual",
    "quickRatio":                    "quickRatioAnnual",
    # earnings / market
    "trailingEps":                   "epsTTM",
    "bookValue":                     "bookValuePerShareAnnual",
    "fiftyTwoWeekHigh":              "52WeekHigh",
    "fiftyTwoWeekLow":               "52WeekLow",
}

# These come from Finnhub as percentages — divide by 100 to match yfinance shape.
_METRIC_PCT = {
    "grossMargins":      "grossMarginTTM",
    "operatingMargins":  "operatingMarginTTM",
    "profitMargins":     "netProfitMarginTTM",
    "returnOnEquity":    "roeTTM",
    "returnOnAssets":    "roaTTM",
    "revenueGrowth":     "revenueGrowthTTMYoy",
    "earningsGrowth":    "epsGrowthTTMYoy",
    "dividendYield":     "dividendYieldIndicatedAnnual",
    "payoutRatio":       "payoutRatioTTM",
}


# ============================================================
#  get_info — replaces yfinance Ticker.info (the rate-limit culprit)
# ============================================================
def get_info(ticker):
    """Return a yfinance-shaped 'info' dict built from Finnhub.
    Returns {} on total failure (so renderer code that does info.get('x') still works)."""
    ticker = (ticker or "").upper().strip()
    profile = _get("/stock/profile2", {"symbol": ticker}) or {}
    quote   = _get("/quote",          {"symbol": ticker}) or {}
    raw     = _get("/stock/metric",   {"symbol": ticker, "metric": "all"}) or {}
    m       = raw.get("metric") or {}

    # If all three failed, give up cleanly.
    if not (profile or quote or m):
        return {}

    info = {}

    # ---- identity ----
    info["symbol"]              = ticker
    info["longName"]            = profile.get("name")
    info["shortName"]           = profile.get("name")
    info["exchange"]            = profile.get("exchange")
    info["fullExchangeName"]    = profile.get("exchange")
    info["currency"]            = profile.get("currency")
    info["country"]             = profile.get("country")
    info["website"]             = profile.get("weburl")
    info["industry"]            = profile.get("finnhubIndustry")
    info["industryDisp"]        = profile.get("finnhubIndustry")
    info["industryKey"]         = profile.get("finnhubIndustry")
    info["sector"]              = profile.get("finnhubIndustry")  # Finnhub doesn't split sector vs industry
    info["longBusinessSummary"] = None  # free tier doesn't include a description; renderer shows "—"

    # ---- market cap & shares (Finnhub returns these in millions) ----
    mcap_m = _f(profile.get("marketCapitalization"))
    info["marketCap"] = mcap_m * 1e6 if mcap_m is not None else None
    so_m = _f(profile.get("shareOutstanding"))
    info["sharesOutstanding"] = so_m * 1e6 if so_m is not None else None

    # ---- live price (from /quote) ----
    info["currentPrice"]                = _f(quote.get("c"))
    info["regularMarketPrice"]          = _f(quote.get("c"))
    info["previousClose"]               = _f(quote.get("pc"))
    info["regularMarketPreviousClose"]  = _f(quote.get("pc"))
    info["dayHigh"]                     = _f(quote.get("h"))
    info["dayLow"]                      = _f(quote.get("l"))
    info["regularMarketOpen"]           = _f(quote.get("o"))

    # ---- metrics: direct pass-through ----
    for yk, fk in _METRIC_DIRECT.items():
        info[yk] = _f(m.get(fk))

    # ---- metrics: percent → decimal ----
    for yk, fk in _METRIC_PCT.items():
        info[yk] = _pct_to_dec(m.get(fk))

    # ---- yfinance keys with no clean Finnhub equivalent: keep them as None
    # so renderer .get() calls all work and just show "—" in those spots.
    for k in ("forwardPE", "forwardEps", "trailingPegRatio", "pegRatio",
              "fiveYearAvgDividendYield", "trailingAnnualDividendRate",
              "trailingAnnualDividendYield", "ebitda", "totalRevenue",
              "totalDebt", "totalCash", "fullTimeEmployees",
              "operatingCashflow", "freeCashflow"):
        info.setdefault(k, None)

    return info


# ============================================================
#  get_recommendations — replaces yfinance Ticker.recommendations
# ============================================================
def get_recommendations(ticker):
    """Return a DataFrame matching yfinance .recommendations shape.
    Columns: period, strongBuy, buy, hold, sell, strongSell."""
    rows = _get("/stock/recommendation", {"symbol": (ticker or "").upper().strip()})
    if not rows or not isinstance(rows, list):
        return None
    # Finnhub: newest first. yfinance: oldest first. Reverse so downstream code is consistent.
    rows = list(reversed(rows))
    df = pd.DataFrame([{
        "period":     r.get("period", ""),
        "strongBuy":  int(r.get("strongBuy")  or 0),
        "buy":        int(r.get("buy")        or 0),
        "hold":       int(r.get("hold")       or 0),
        "sell":       int(r.get("sell")       or 0),
        "strongSell": int(r.get("strongSell") or 0),
    } for r in rows])
    return df if not df.empty else None


# ============================================================
#  get_insider_transactions — replaces yfinance Ticker.insider_transactions
# ============================================================
def get_insider_transactions(ticker):
    """Return a DataFrame matching yfinance .insider_transactions shape."""
    resp = _get("/stock/insider-transactions", {"symbol": (ticker or "").upper().strip()})
    if not resp or not isinstance(resp, dict):
        return None
    data = resp.get("data") or []
    if not data:
        return None
    rows = []
    for r in data:
        shares = _f(r.get("share"))
        price  = _f(r.get("transactionPrice"))
        value  = shares * price if (shares is not None and price is not None) else None
        rows.append({
            "Insider":      r.get("name"),
            "Shares":       shares,
            "Value":        value,
            "Start Date":   r.get("transactionDate"),
            "Transaction":  r.get("transactionCode"),
            "Position":     None,  # Finnhub doesn't provide a clean title field
        })
    df = pd.DataFrame(rows)
    return df if not df.empty else None


# ============================================================
#  get_peer_data — replaces fetch_peers() in stock_lens.py
# ============================================================
_PEER_METRICS = ["grossMargins", "operatingMargins", "profitMargins",
                 "returnOnEquity", "returnOnAssets", "trailingPE",
                 "priceToBook", "priceToSalesTrailing12Months",
                 "enterpriseToEbitda", "revenueGrowth", "earningsGrowth",
                 "debtToEquity", "currentRatio"]


def _peer_metric_snapshot(ticker):
    """Pull just the comparable metrics for one peer ticker."""
    raw = _get("/stock/metric", {"symbol": ticker, "metric": "all"})
    if not raw:
        return None
    m = raw.get("metric") or {}
    out = {}
    for yk, fk in _METRIC_DIRECT.items():
        if yk in _PEER_METRICS:
            out[yk] = _f(m.get(fk))
    for yk, fk in _METRIC_PCT.items():
        if yk in _PEER_METRICS:
            out[yk] = _pct_to_dec(m.get(fk))
    return out


def get_peer_data(info):
    """Return {'name','medians','n','tickers'} — same shape as stock_lens.fetch_peers()."""
    if not isinstance(info, dict):
        return None
    self_sym = (info.get("symbol") or "").upper()
    industry = info.get("industry") or ""
    if not self_sym:
        return None

    peer_resp = _get("/stock/peers", {"symbol": self_sym})
    if not peer_resp or not isinstance(peer_resp, list):
        return None
    peers = [str(p).upper() for p in peer_resp if str(p).upper() != self_sym][:9]
    if not peers:
        return None

    vals = {k: [] for k in _PEER_METRICS}
    try:
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(_peer_metric_snapshot, p) for p in peers]
            for fu in cf.as_completed(futs, timeout=25):
                r = fu.result()
                if not r:
                    continue
                for k in _PEER_METRICS:
                    x = _f(r.get(k))
                    if x is not None:
                        vals[k].append(x)
    except Exception:
        pass

    medians = {}
    for k, lst in vals.items():
        if len(lst) >= 3:                       # need >=3 peers for a meaningful median
            s = sorted(lst)
            n = len(s)
            medians[k] = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    if not medians:
        return None

    return {"name": industry, "medians": medians,
            "n": max(len(v) for v in vals.values()), "tickers": peers}
