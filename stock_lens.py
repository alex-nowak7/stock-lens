#!/usr/bin/env python3
"""
Stock Lens — a one-stop stock research report generator.

Type a ticker, and Stock Lens fetches everything a serious investor weighs
before buying (business quality, valuation, financial health, growth, the
people, and what the smart money is doing), then opens a clean, plain-English
report in your web browser.

Data comes from Yahoo Finance via the `yfinance` library, which runs here in
Python (no browser, so none of the CORS/proxy problems a web page hits).

USAGE
    python stock_lens.py            # interactive: it will ask for tickers
    python stock_lens.py AAPL       # analyze one ticker and exit
    python stock_lens.py AAPL MSFT  # analyze several in a row

REQUIREMENTS  (one-time setup — see README.txt for step-by-step help)
    pip install yfinance matplotlib pandas
"""

import sys
import os
import io
import time
import base64
import math
import webbrowser
import tempfile
from datetime import datetime

# ---- friendly dependency check so the user gets a clear message, not a crash ----
_missing = []
try:
    import yfinance as yf
except ImportError:
    _missing.append("yfinance")
try:
    import pandas as pd
except ImportError:
    _missing.append("pandas")
try:
    import matplotlib
    matplotlib.use("Agg")  # no display needed; we render to image files
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
except ImportError:
    _missing.append("matplotlib")

# curl_cffi lets us impersonate a real Chrome browser when calling Yahoo.
# This is what keeps the rich "info" data flowing from cloud servers (Render
# etc.), where Yahoo otherwise rate-limits (HTTP 429) and returns blanks.
# It's optional: if it's missing, we fall back to yfinance's default session.
try:
    from curl_cffi import requests as _curl_requests
except ImportError:
    _curl_requests = None

if _missing:
    print("\n  Stock Lens needs a few free Python packages that aren't installed yet.")
    print("  Please run this command in your terminal, then run Stock Lens again:\n")
    print("      pip install " + " ".join(_missing) + "\n")
    print("  (If 'pip' isn't found, try 'pip3' or 'python -m pip install ...')\n")
    sys.exit(1)


def _new_session():
    """A browser-impersonating session (Chrome) to avoid Yahoo rate limits.
    Returns None if curl_cffi isn't installed, in which case yfinance uses
    its own default session."""
    if _curl_requests is None:
        return None
    try:
        return _curl_requests.Session(impersonate="chrome")
    except Exception:
        return None


# ============================================================
#  SMALL HELPERS  — formatting & safe data access
# ============================================================
def numf(x):
    """Return x as a float, or None if it isn't a usable number."""
    try:
        if x is None:
            return None
        f = float(x)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def gi(info, key):
    """Safely get a key from the info dict as a number when possible."""
    if not isinstance(info, dict):
        return None
    return info.get(key)


def fmt_num(n):
    n = numf(n)
    if n is None:
        return "—"
    a = abs(n)
    if a >= 1e12:
        return f"{n/1e12:.2f}T"
    if a >= 1e9:
        return f"{n/1e9:.2f}B"
    if a >= 1e6:
        return f"{n/1e6:.2f}M"
    if a >= 1e3:
        return f"{n/1e3:.1f}K"
    return f"{n:.2f}"


def fmt_usd(n):
    return "—" if numf(n) is None else "$" + fmt_num(n)


def fmt_pct(n, dec=1):
    n = numf(n)
    if n is None:
        return "—"
    sign = "+" if n >= 0 else ""
    return f"{sign}{n:.{dec}f}%"


def fmt_pct0(n):
    n = numf(n)
    return "—" if n is None else f"{n:.1f}%"


def fmt_ratio(n):
    n = numf(n)
    return "—" if n is None else f"{n:.2f}"


def fmt_price(n):
    n = numf(n)
    return "—" if n is None else f"${n:.2f}"


def esc(s):
    """Escape text for safe HTML embedding."""
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ============================================================
#  DATA FETCH  — every piece wrapped so one failure can't kill the report
# ============================================================
def fetch_all(ticker):
    """Pull everything we can from yfinance. Returns a dict; missing pieces are None."""
    session = _new_session()                       # Chrome-impersonating session (or None)
    tk = yf.Ticker(ticker, session=session) if session else yf.Ticker(ticker)
    data = {"ticker": ticker.upper(), "errors": [], "_session": session}

    def tryget(label, fn, retries=0):
        for attempt in range(retries + 1):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001 — we want to survive any single failure
                if attempt < retries:
                    time.sleep(0.8 * (attempt + 1))   # brief backoff, then retry
                    continue
                data["errors"].append(f"{label}: {e}")
                return None

    # `info` is the big one Yahoo rate-limits most, so give it a couple of retries.
    info = tryget("info", lambda: tk.info, retries=2)
    if not info:
        # one more try with a fresh impersonating session
        s2 = _new_session()
        if s2:
            tk = yf.Ticker(ticker, session=s2)
            data["_session"] = s2
            info = tryget("info (retry)", lambda: tk.info, retries=1)
    data["info"] = info or {}
    data["hist"] = tryget("price history", lambda: tk.history(period="2y", interval="1d"))
    data["income"] = tryget("income statement", lambda: tk.income_stmt)
    data["balance"] = tryget("balance sheet", lambda: tk.balance_sheet)
    data["cashflow"] = tryget("cash flow", lambda: tk.cashflow)
    data["recs"] = tryget("recommendations", lambda: tk.recommendations)
    data["targets"] = tryget("price targets", lambda: tk.analyst_price_targets)
    data["inst"] = tryget("institutional holders", lambda: tk.institutional_holders)
    data["major"] = tryget("major holders", lambda: tk.major_holders)
    data["insider"] = tryget("insider transactions", lambda: tk.insider_transactions)
    data["upgrades"] = tryget("analyst actions", lambda: tk.upgrades_downgrades)
    data["calendar"] = tryget("calendar", lambda: tk.calendar)
    # --- industry peers, for relative ("vs industry") analysis ---
    data["peers"] = tryget("industry peers", lambda: fetch_peers(data["info"], data.get("_session")))
    data.pop("_session", None)  # don't keep the session object in the result
    return data


# metrics we compare against industry peers (yfinance info key -> sign meaning)
PEER_METRICS = ["grossMargins", "operatingMargins", "profitMargins", "returnOnEquity",
                "returnOnAssets", "trailingPE", "forwardPE", "priceToBook",
                "priceToSalesTrailing12Months", "enterpriseToEbitda", "revenueGrowth",
                "earningsGrowth", "debtToEquity", "currentRatio"]


def fetch_peers(info, session=None):
    """Fetch same-industry peers and the median of each comparable metric.
    Returns {'name': industry_name, 'medians': {key: median}, 'n': count, 'tickers': [...]}.
    Designed to fail soft: returns None on any problem, capped in time."""
    if not isinstance(info, dict):
        return None
    ind_key = info.get("industryKey") or info.get("industryDisp")
    self_sym = (info.get("symbol") or "").upper()
    if not ind_key:
        return None
    try:
        ind = yf.Industry(ind_key, session=session) if session else yf.Industry(ind_key)
        top = ind.top_companies
        ind_name = ind.name or info.get("industry") or ""
    except Exception:
        return None
    if top is None or not hasattr(top, "empty") or top.empty:
        return None
    # peer tickers = index of the DataFrame, minus self, capped to keep it fast
    syms = [str(s).upper() for s in list(top.index) if str(s).upper() != self_sym][:9]
    if not syms:
        return None

    import concurrent.futures as cf
    vals = {k: [] for k in PEER_METRICS}

    def one(sym):
        try:
            # each thread uses its own impersonating session to avoid 429s
            s = _new_session()
            pt = yf.Ticker(sym, session=s) if s else yf.Ticker(sym)
            pinfo = pt.info
            return {k: pinfo.get(k) for k in PEER_METRICS}
        except Exception:
            return None

    try:
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(one, s) for s in syms]
            for fu in cf.as_completed(futs, timeout=25):
                r = fu.result()
                if not r:
                    continue
                for k in PEER_METRICS:
                    x = numf(r.get(k))
                    if x is not None:
                        vals[k].append(x)
    except Exception:
        pass

    medians = {}
    for k, lst in vals.items():
        if len(lst) >= 3:  # need a few peers for a meaningful median
            s = sorted(lst)
            n = len(s)
            medians[k] = (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)
    if not medians:
        return None
    return {"name": ind_name, "medians": medians, "n": max(len(v) for v in vals.values()),
            "tickers": syms}


def valid(data):
    """A fetch is usable if we at least got a name or price history."""
    info = data.get("info") or {}
    has_name = bool(info.get("longName") or info.get("shortName"))
    hist = data.get("hist")
    has_hist = hist is not None and hasattr(hist, "empty") and not hist.empty
    return has_name or has_hist


# ============================================================
#  DERIVED STATS  — price math the report uses
# ============================================================
def price_stats(hist):
    """Compute returns, volatility, drawdown, trend from a price DataFrame."""
    if hist is None or not hasattr(hist, "empty") or hist.empty or "Close" not in hist.columns:
        return None
    closes = [c for c in hist["Close"].tolist() if numf(c) is not None]
    if len(closes) < 20:
        return None
    last = closes[-1]

    def ret(days):
        i = len(closes) - 1 - days
        return (last - closes[i]) / closes[i] * 100 if i >= 0 else None

    window = closes[-252:] if len(closes) >= 252 else closes
    hi52, lo52 = max(window), min(window)

    daily = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    mean = sum(daily) / len(daily)
    var = sum((d - mean) ** 2 for d in daily) / len(daily)
    vol = math.sqrt(var) * math.sqrt(252) * 100

    ma50 = sum(closes[-50:]) / min(50, len(closes))
    ma200 = sum(closes[-200:]) / min(200, len(closes))

    peak, max_dd = closes[0], 0.0
    for c in closes:
        if c > peak:
            peak = c
        dd = (c - peak) / peak
        if dd < max_dd:
            max_dd = dd

    win_hi = max(closes)
    from_hi = (last - win_hi) / win_hi * 100
    best = max(daily) * 100
    worst = min(daily) * 100
    up_days = sum(1 for d in daily if d > 0) / len(daily) * 100

    # downsample dates/closes for charting (keep it light)
    dates = [d.strftime("%Y-%m-%d") for d in hist.index]
    return {
        "last": last, "ret1m": ret(21), "ret6m": ret(126), "ret1y": ret(252),
        "hi52": hi52, "lo52": lo52, "vol": vol, "ma50": ma50, "ma200": ma200,
        "max_dd": max_dd * 100, "from_hi": from_hi, "best": best, "worst": worst,
        "up_days": up_days, "dates": dates, "closes": closes,
    }


def dividend_yield_pct(info):
    """Robustly compute dividend yield as a percentage."""
    rate = numf(gi(info, "dividendRate"))
    price = numf(gi(info, "currentPrice")) or numf(gi(info, "regularMarketPrice")) \
        or numf(gi(info, "previousClose"))
    if rate is not None and price:
        return rate / price * 100
    dy = numf(gi(info, "dividendYield"))
    if dy is None:
        return None
    # yfinance has historically returned this either as a fraction (0.025) or a
    # percent (2.5). Heuristic: small values are fractions.
    return dy * 100 if dy < 1 else dy


# ============================================================
#  PILLAR SCORING  (1–5)
# ============================================================
def score_pillars(info, stats):
    s = {"biz": None, "val": None, "health": None, "growth": None, "mom": None, "smart": None}
    pm = numf(gi(info, "profitMargins"))
    if pm is not None:
        p = pm * 100
        s["biz"] = 5 if p > 20 else 4 if p > 12 else 3 if p > 5 else 2 if p > 0 else 1
    pe = numf(gi(info, "trailingPE"))
    if pe is not None:
        s["val"] = 1 if pe < 0 else 5 if pe < 15 else 4 if pe < 22 else 3 if pe < 32 else 2 if pe < 45 else 1
    roe = numf(gi(info, "returnOnEquity"))
    de = numf(gi(info, "debtToEquity"))
    cr = numf(gi(info, "currentRatio"))
    if roe is not None or de is not None:
        h = 3
        if roe is not None and roe * 100 > 15:
            h += 1
        if roe is not None and roe * 100 > 25:
            h += 1
        if de is not None and de < 50:
            h += 1
        if de is not None and de > 200:
            h -= 2
        if cr is not None and cr < 1:
            h -= 1
        s["health"] = max(1, min(5, h))
    rg = numf(gi(info, "revenueGrowth"))
    if rg is not None:
        g = 3
        r = rg * 100
        if r > 10:
            g += 1
        if r > 25:
            g += 1
        if r < 0:
            g -= 2
        s["growth"] = max(1, min(5, g))
    if stats and stats.get("ret6m") is not None:
        r = stats["ret6m"]
        s["mom"] = 5 if r > 25 else 4 if r > 10 else 3 if r > -5 else 2 if r > -20 else 1
    rm = numf(gi(info, "recommendationMean"))
    if rm is not None:  # Yahoo: 1=Strong Buy ... 5=Sell; invert so 5=bullish
        s["smart"] = max(1, min(5, round(6 - rm)))
    return s


def pillar_grade(s):
    return "n/a" if s is None else ["", "Weak", "Fair", "Fair", "Good", "Strong"][s]


# ============================================================
#  CHARTS  — matplotlib → base64 PNG (self-contained, no CDN)
# ============================================================
_AXIS = "#8a8a82"  # mid gray, readable on light & dark
_ACCENT = "#2a6df4"
_GREEN = "#1f8a4c"


def _fig_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", transparent=True)
    plt.close(fig)
    buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")


def chart_price(stats):
    if not stats:
        return None
    fig, ax = plt.subplots(figsize=(8, 2.7))
    closes, dates = stats["closes"], stats["dates"]
    ax.plot(range(len(closes)), closes, color=_ACCENT, linewidth=1.6)
    ax.fill_between(range(len(closes)), closes, min(closes), color=_ACCENT, alpha=0.10)
    n = len(dates)
    ticks = [0, n // 4, n // 2, 3 * n // 4, n - 1]
    ax.set_xticks(ticks)
    ax.set_xticklabels([dates[i][:7] for i in ticks], fontsize=8, color=_AXIS)
    ax.tick_params(colors=_AXIS, labelsize=8)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    for sp in ["top", "right"]:
        ax.spines[sp].set_visible(False)
    for sp in ["left", "bottom"]:
        ax.spines[sp].set_color(_AXIS)
        ax.spines[sp].set_alpha(0.3)
    ax.grid(axis="y", color=_AXIS, alpha=0.15)
    fig.patch.set_alpha(0)
    return _fig_to_b64(fig)


def chart_growth(income):
    """Grouped bars: revenue & net income by year (oldest→newest)."""
    if income is None or not hasattr(income, "empty") or income.empty:
        return None
    try:
        cols = list(income.columns)[::-1]  # oldest first
        labels = [c.strftime("%Y") if hasattr(c, "strftime") else str(c)[:4] for c in cols]

        def row(*names):
            for nm in names:
                if nm in income.index:
                    return [numf(income.loc[nm, c]) for c in cols]
            return None

        rev = row("Total Revenue", "Operating Revenue", "TotalRevenue")
        ni = row("Net Income", "Net Income Common Stockholders", "NetIncome")
        if not rev and not ni:
            return None
    except Exception:
        return None

    fig, ax = plt.subplots(figsize=(8, 2.9))
    x = range(len(labels))
    w = 0.38
    if rev:
        rev_b = [(v / 1e9 if v is not None else 0) for v in rev]
        ax.bar([i - w / 2 for i in x], rev_b, width=w, color=_ACCENT, label="Revenue ($B)")
    if ni:
        ni_b = [(v / 1e9 if v is not None else 0) for v in ni]
        ax.bar([i + w / 2 for i in x], ni_b, width=w, color=_GREEN, label="Net income ($B)")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=9, color=_AXIS)
    ax.tick_params(colors=_AXIS, labelsize=8)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v:,.0f}B"))
    for sp in ["top", "right"]:
        ax.spines[sp].set_visible(False)
    for sp in ["left", "bottom"]:
        ax.spines[sp].set_color(_AXIS)
        ax.spines[sp].set_alpha(0.3)
    ax.grid(axis="y", color=_AXIS, alpha=0.15)
    ax.legend(fontsize=8, frameon=False, labelcolor=_AXIS)
    ax.axhline(0, color=_AXIS, alpha=0.3, linewidth=0.8)
    fig.patch.set_alpha(0)
    return _fig_to_b64(fig)


# ============================================================
#  HTML BUILDING
# ============================================================
CSS = """
:root{
  --bg:#f6f6f3;--surface:#fff;--surface2:#f0efea;--ink:#222220;--muted:#6a695f;
  --hint:#9a988f;--border:rgba(0,0,0,.11);--border2:rgba(0,0,0,.2);
  --accent:#2a6df4;--accent-bg:#e8f0fe;--green:#1f8a4c;--green-bg:#e6f4ea;
  --red:#cf3a3a;--red-bg:#fcebeb;--amber:#b87514;--amber-bg:#fbf0db;
  --r:14px;--r-sm:9px;--r-lg:18px;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#191816;--surface:#232220;--surface2:#2c2b27;--ink:#ece9e2;--muted:#a5a299;
  --hint:#787669;--border:rgba(255,255,255,.13);--border2:rgba(255,255,255,.24);
  --accent:#6fa0ff;--accent-bg:#13294d;--green:#74c98a;--green-bg:#1d3b28;
  --red:#f1908f;--red-bg:#3f1c1c;--amber:#e3a64a;--amber-bg:#3d2c10;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  line-height:1.55;-webkit-font-smoothing:antialiased;}
.wrap{max-width:920px;margin:0 auto;padding:0 20px 90px;}
.hero{text-align:center;padding:46px 0 22px;}
.hero .badge{width:74px;height:74px;border-radius:50%;background:var(--accent);display:flex;
  align-items:center;justify-content:center;margin:0 auto 16px;box-shadow:0 6px 22px rgba(42,109,244,.28);}
.hero .badge svg{width:38px;height:38px;stroke:#fff;}
.hero h1{font-size:30px;font-weight:700;margin:0;letter-spacing:-.02em;}
.hero .tk{color:var(--muted);font-size:15px;margin:8px 0 0;}
.hero .stamp{color:var(--hint);font-size:12px;margin:4px 0 0;}
.scard{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:24px;margin-bottom:18px;}
.scard .top{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:14px;}
.scard .nm{font-size:22px;font-weight:700;margin:0;letter-spacing:-.01em;}
.scard .sub{color:var(--muted);font-size:13.5px;margin:3px 0 0;}
.scard .pr{text-align:right;}.scard .pr .v{font-size:28px;font-weight:700;}.scard .pr .c{font-size:14px;font-weight:600;margin-top:2px;}
.up{color:var(--green);}.down{color:var(--red);}
.pillars{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:9px;margin-top:18px;}
.pillar{background:var(--surface2);border-radius:var(--r-sm);padding:11px 12px;}
.pillar .l{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-weight:600;}
.pillar .r{display:flex;align-items:center;gap:7px;margin-top:6px;}
.dots{display:flex;gap:3px;}.dot{width:8px;height:8px;border-radius:50%;background:var(--border2);}
.dot.g{background:var(--green);}.dot.a{background:var(--amber);}.dot.rd{background:var(--red);}
.pillar .gr{font-size:12px;font-weight:600;margin-left:auto;color:var(--muted);}
.verdict{margin-top:18px;padding:15px 17px;border-radius:var(--r-sm);background:var(--surface2);font-size:14px;}
.verdict b{font-weight:600;}
.controls{display:flex;gap:9px;justify-content:center;flex-wrap:wrap;margin-bottom:16px;}
.controls button{font-size:12.5px;font-weight:500;padding:7px 14px;border-radius:20px;border:1px solid var(--border2);
  background:transparent;color:var(--muted);cursor:pointer;}
.controls button:hover{background:var(--surface2);color:var(--ink);}
.sec{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);margin-bottom:12px;overflow:visible;}
.sec-head{display:flex;align-items:center;gap:13px;padding:17px 20px;cursor:pointer;user-select:none;
  border-radius:var(--r);transition:background .12s;}
.sec-head:hover{background:var(--accent-bg);}
.sec-ic{width:34px;height:34px;border-radius:9px;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.sec-ic svg{width:19px;height:19px;}
.sec-tt{flex:1;}.sec-tt .h{font-size:16px;font-weight:600;margin:0;}.sec-tt .s{font-size:12.5px;color:var(--muted);margin:1px 0 0;}
.sec-grade{font-size:12px;font-weight:600;padding:4px 11px;border-radius:20px;margin-right:6px;white-space:nowrap;}
.sec-grade.g{background:var(--green-bg);color:var(--green);}.sec-grade.a{background:var(--amber-bg);color:var(--amber);}
.sec-grade.rd{background:var(--red-bg);color:var(--red);}.sec-grade.n{background:var(--surface2);color:var(--muted);}
.chev{width:18px;height:18px;color:var(--hint);transition:transform .2s;flex-shrink:0;}
.sec.open .chev{transform:rotate(180deg);}
.sec-body{display:none;padding:0 20px 20px;}.sec.open .sec-body{display:block;}
/* symmetric grid: equal columns, equal heights */
.mgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;align-items:stretch;}
@media(max-width:760px){.mgrid{grid-template-columns:repeat(2,1fr);}}
@media(max-width:460px){.mgrid{grid-template-columns:1fr;}}
.m{background:var(--surface2);border-radius:var(--r-sm);padding:13px 14px;display:flex;flex-direction:column;min-height:108px;}
.m .ml{font-size:12px;color:var(--muted);display:flex;align-items:flex-start;gap:5px;line-height:1.3;min-height:32px;}
.m .mv{font-size:19px;font-weight:600;margin-top:auto;padding-top:6px;}
.pill-row{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:7px;}
.pill{font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px;letter-spacing:.02em;}
.pill.good{background:var(--green-bg);color:var(--green);}
.pill.ok{background:var(--amber-bg);color:var(--amber);}
.pill.bad{background:var(--red-bg);color:var(--red);}
.vs{font-size:11px;color:var(--hint);font-weight:500;}
.info{cursor:help;color:var(--hint);font-size:11px;border:1px solid var(--border2);border-radius:50%;
  width:15px;height:15px;min-width:15px;display:inline-flex;align-items:center;justify-content:center;line-height:1;flex-shrink:0;margin-top:1px;}
/* tooltip: clamp to viewport so it never clips out of the card */
.tip{position:relative;display:inline-flex;}
.tip .tt{visibility:hidden;opacity:0;position:absolute;bottom:150%;left:0;
  background:var(--ink);color:var(--bg);font-size:12px;font-weight:400;padding:10px 12px;border-radius:9px;
  width:240px;max-width:78vw;line-height:1.5;z-index:50;transition:opacity .15s;box-shadow:0 6px 22px rgba(0,0,0,.28);
  pointer-events:none;}
.tip .tt b{font-weight:700;}
/* flip tooltip to the right edge for cards near the right side */
.m:nth-child(3n) .tip .tt,.m:last-child .tip .tt{left:auto;right:0;}
.tip:hover .tt{visibility:visible;opacity:1;}
.sub-h{font-size:14px;font-weight:600;margin:20px 0 10px;}.sub-h:first-child{margin-top:0;}
img.chart{width:100%;border-radius:var(--r-sm);margin-top:6px;}
table.tbl{width:100%;border-collapse:collapse;font-size:13.5px;}
table.tbl th{text-align:left;color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;
  letter-spacing:.03em;padding:7px 8px;border-bottom:1px solid var(--border);}
table.tbl td{padding:8px;border-bottom:1px solid var(--border);}table.tbl tr:last-child td{border-bottom:none;}
.badge{display:inline-block;font-size:11.5px;font-weight:600;padding:2px 9px;border-radius:20px;}
.badge.buy{background:var(--green-bg);color:var(--green);}.badge.sell{background:var(--red-bg);color:var(--red);}
.badge.hold{background:var(--amber-bg);color:var(--amber);}
.note{font-size:13px;color:var(--muted);margin:14px 0 0;padding:13px 15px;background:var(--surface2);border-radius:var(--r-sm);}
.note b{font-weight:600;color:var(--ink);}
.cons-wrap{display:flex;align-items:center;gap:18px;flex-wrap:wrap;padding:6px 0 4px;}
.cons-badge{font-size:22px;font-weight:800;padding:12px 22px;border-radius:14px;letter-spacing:-.01em;white-space:nowrap;}
.cons-badge.g{background:var(--green-bg);color:var(--green);}
.cons-badge.a{background:var(--amber-bg);color:var(--amber);}
.cons-badge.rd{background:var(--red-bg);color:var(--red);}
.cons-badge.n{background:var(--surface2);color:var(--muted);}
.cons-meta{flex:1;min-width:200px;}
.cons-lbl{font-size:13px;font-weight:700;color:var(--ink);}
.cons-sub{font-size:13px;color:var(--muted);margin-top:2px;}
.cons-ws{font-size:13px;color:var(--muted);margin-top:6px;}.cons-ws b{color:var(--ink);}
.why-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
@media(max-width:600px){.why-grid{grid-template-columns:1fr;}}
.why-col{background:var(--surface2);border-radius:var(--r-sm);padding:14px 16px;}
.why-h{font-weight:700;font-size:13.5px;margin-bottom:8px;}
.why-col.pos .why-h{color:var(--green);}.why-col.neg .why-h{color:var(--amber);}
.why-col ul{margin:0;padding-left:18px;}.why-col li{font-size:13.5px;margin-bottom:6px;line-height:1.45;}
.empty{display:flex;align-items:flex-start;gap:10px;color:var(--muted);font-size:13.5px;
  background:var(--surface2);border-radius:var(--r-sm);padding:13px 15px;}
.empty b{color:var(--ink);}
.primer-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;align-items:stretch;}
@media(max-width:760px){.primer-grid{grid-template-columns:repeat(2,1fr);}}
@media(max-width:460px){.primer-grid{grid-template-columns:1fr;}}
.pc{background:var(--surface2);border-radius:var(--r-sm);padding:13px 14px;}
.bizdesc{font-size:14px;margin:0 0 16px;}
.bizdesc.clamp{display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden;}
.bizmore{background:none;border:none;color:var(--accent);font-weight:600;font-size:13px;cursor:pointer;padding:0;margin:0 0 16px;}
.pc .pt{font-weight:600;font-size:13.5px;}.pc .pd{font-size:12.5px;color:var(--muted);margin-top:5px;line-height:1.45;}
.split{display:flex;border-radius:6px;overflow:hidden;border:1px solid var(--border);}
.split div{height:22px;}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:8px;}
.disc{font-size:12px;color:var(--hint);margin-top:28px;padding-top:16px;border-top:1px solid var(--border);line-height:1.6;}
"""

ICONS = {
    "primer": '<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>',
    "business": '<path d="M3 21h18"/><path d="M5 21V7l8-4v18"/><path d="M19 21V11l-6-4"/>',
    "price": '<path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/>',
    "risk": '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "value": '<path d="M12 2v20"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
    "health": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "growth": '<path d="M3 3v18h18"/><path d="m7 14 4-4 3 3 5-6"/>',
    "people": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "check": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/>',
    "summary": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6"/><path d="M9 17h6"/>',
}
ICO_COLOR = {"primer": "accent", "business": "accent", "price": "green", "risk": "amber",
             "value": "amber", "health": "green", "growth": "green", "people": "amber",
             "check": "accent", "summary": "accent"}
_PAL = {"accent": ("var(--accent-bg)", "var(--accent)"),
        "green": ("var(--green-bg)", "var(--green)"),
        "amber": ("var(--amber-bg)", "var(--amber)")}


def tip(text):
    return f'<span class="tip"><span class="info">i</span><span class="tt">{text}</span></span>'


def tip2(what, how):
    """Two-part tooltip: what the metric means, and how to read this value."""
    return (f'<span class="tip"><span class="info">i</span>'
            f'<span class="tt"><b>What it is.</b> {esc(what)}<br><br>'
            f'<b>How to read it.</b> {esc(how)}</span></span>')


# Standardized status vocabulary — used EVERYWHERE so it never gets confusing.
#   good    -> green pill "Good"
#   ok      -> amber pill "Average"
#   bad     -> red  pill "Watch"
STATUS_LABEL = {"good": "Good", "ok": "Average", "bad": "Watch"}


def _fmt_compare(val, med, pct=False, ratio_label="x"):
    """Human phrase comparing val to the industry median."""
    if pct:
        return f"{val:.0f}% vs {med:.0f}% industry"
    return f"{val:.1f} vs {med:.1f} industry"


def assess(val, good, bad, higher=True, peers=None, key=None,
           is_pct_frac=False, is_pct_already=False):
    """Return a standardized status dict for a metric.

    Judges against the INDUSTRY MEDIAN when peer data for this metric exists,
    otherwise falls back to sensible fixed thresholds. Returns:
       {cls, label, vs}  where cls in good/ok/bad, label is the standard word,
       and vs is a short 'vs industry' phrase (or "").
    val is in the SAME units as good/bad. For peer comparison we convert the
    peer median (always a raw yfinance value) to match.
    """
    val = numf(val)
    if val is None:
        return None

    # --- industry-relative path ---
    med = None
    if peers and key and key in peers.get("medians", {}):
        med = numf(peers["medians"][key])
    if med is not None:
        # convert median to the metric's display units to compare/show
        m = med
        v = val
        if is_pct_frac:        # yfinance fraction -> percent
            m = med * 100
        if key == "debtToEquity":
            # yfinance gives e.g. 151.8 meaning D/E 1.52; compare raw, show as ratio
            better = v <= m if not higher else v >= m
            close = abs(v - m) <= max(15.0, 0.15 * abs(m))
            vs = f"{v/100:.2f}x vs {m/100:.2f}x industry"
        else:
            show_pct = is_pct_frac or is_pct_already
            if higher:
                better = v >= m
            else:
                better = v <= m
            close = abs(v - m) <= (0.15 * abs(m) if m else 0)
            vs = _fmt_compare(v, m, pct=show_pct)
        if close:
            return {"cls": "ok", "label": STATUS_LABEL["ok"], "vs": "≈ industry median"}
        return {"cls": "good" if better else "bad",
                "label": STATUS_LABEL["good" if better else "bad"],
                "vs": vs}

    # --- fixed-threshold fallback (no peers) ---
    if higher:
        cls = "good" if val >= good else "bad" if val <= bad else "ok"
    else:
        cls = "good" if val <= good else "bad" if val >= bad else "ok"
    return {"cls": cls, "label": STATUS_LABEL[cls], "vs": ""}


def metric(label, value, tiptext=None, status=None):
    """Render a metric card with a standardized status pill.
    status = dict from assess(), or None.
    tiptext may be a plain string (gets wrapped) or already-built tip markup
    from tip2() (used as-is, so we don't nest a second info icon)."""
    pill = ""
    if status:
        vs = f'<span class="vs">{esc(status["vs"])}</span>' if status.get("vs") else ""
        pill = f'<div class="pill-row"><span class="pill {status["cls"]}">{status["label"]}</span>{vs}</div>'
    if not tiptext:
        tt = ""
    elif 'class="tip"' in tiptext:   # already a full tip (from tip2)
        tt = tiptext
    else:
        tt = tip(tiptext)
    return (f'<div class="m"><div class="ml">{label}{tt}</div>'
            f'<div class="mv">{value}</div>{pill}</div>')


def section(sid, icon, title, subtitle, grade, body, open_default=False):
    bg, fg = _PAL[ICO_COLOR.get(icon, "accent")]
    grade_html = f'<span class="sec-grade {grade[0]}">{esc(grade[1])}</span>' if grade else ""
    open_cls = " open" if open_default else ""
    return f"""<div class="sec{open_cls}" data-sec="{sid}">
  <div class="sec-head" onclick="this.parentNode.classList.toggle('open')">
    <div class="sec-ic" style="background:{bg};color:{fg}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{ICONS[icon]}</svg>
    </div>
    <div class="sec-tt"><p class="h">{esc(title)}</p><p class="s">{esc(subtitle)}</p></div>
    {grade_html}
    <svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
  </div>
  <div class="sec-body">{body}</div>
</div>"""


# ---------- individual sections ----------
def sec_primer(peers=None):
    cards = [
        ("🏢", "A good business?", "Strong, steady profit margins and high return on equity usually mean the company has a durable edge — Buffett's 'moat'."),
        ("⚖️", "A fair price?", "The P/E ratio is what you pay per $1 of yearly profit. A great company at a crazy price can still lose you money."),
        ("🩺", "Financially safe?", "Low debt and enough cash to cover bills are what let a company survive recessions."),
        ("🌱", "Growing?", "Rising revenue and profit over several years means the business is winning, not just lucky."),
        ("👥", "Backed by smart money?", "Analysts, insiders buying their own shares, and big institutions holding it are reassuring signals."),
        ("🎢", "Can you stomach it?", "Every stock swings. Know the volatility and worst-case drops before buying so you don't panic-sell."),
    ]
    grid = "".join(f'<div class="pc"><div class="pt">{e} {esc(t)}</div><div class="pd">{esc(d)}</div></div>'
                   for e, t, d in cards)
    # legend explaining the standardized status pills
    legend = ('<div class="sub-h">Reading the colored labels</div>'
              '<div class="primer-grid">'
              '<div class="pc"><div class="pt"><span class="pill good">Good</span></div>'
              '<div class="pd">This metric looks favorable — at or better than what you\'d want, or better than industry peers.</div></div>'
              '<div class="pc"><div class="pt"><span class="pill ok">Average</span></div>'
              '<div class="pd">Middle-of-the-road — neither a strength nor a worry. Roughly typical for its industry.</div></div>'
              '<div class="pc"><div class="pt"><span class="pill bad">Watch</span></div>'
              '<div class="pd">Worth a closer look — weaker than ideal, or behind industry peers. Not automatically bad, but dig in.</div></div>'
              '</div>')
    if peers and peers.get("medians"):
        ind = esc(peers.get("name") or "its industry")
        n = peers.get("n", 0)
        ind_note = (f'<p class="note"><b>Compared to its industry.</b> Wherever possible, this report rates each '
                    f'number against the median of <b>{n}</b> companies in <b>{ind}</b> — because a gross margin '
                    f'that\'s great for a steelmaker would be poor for a software company. When a label says '
                    f'"vs industry," that\'s the comparison it\'s making.</p>')
    else:
        ind_note = ('<p class="note"><b>Compared to its industry.</b> Where peer data is available, metrics are '
                    'rated against the company\'s own industry (so we never compare apples to oranges). For this '
                    'company peer data wasn\'t available, so sensible general ranges are used instead.</p>')
    body = (f'<div class="primer-grid">{grid}</div>'
            '<p class="note"><b>The big idea:</b> a stock is a piece of a real business. You\'re trying to '
            'buy a good business at a fair price and hold it. Each section below scores one of these six '
            'questions so you can form your own view — none of it is a "buy" or "sell" command.</p>'
            + legend + ind_note)
    return section("primer", "primer", "How to Read This Page",
                   "The 60-second version of how professionals judge a stock", None, body, open_default=True)


def sec_business(info):
    if not info or not (info.get("longBusinessSummary") or info.get("sector")):
        return section("business", "business", "The Business", "Data Unavailable",
                       ("n", "no data"), '<div class="empty"><span><b>Company profile didn\'t load.</b></span></div>')
    desc = info.get("longBusinessSummary") or "No description available."
    ceo = ""
    for off in (info.get("companyOfficers") or []):
        if off.get("title") and ("CEO" in off["title"] or "Chief Executive" in off["title"]):
            ceo = off.get("name", "")
            break
    emp = info.get("fullTimeEmployees")
    # full description, clamped to 4 lines with a Show more / Show less toggle
    long_enough = len(desc) > 320
    desc_html = (
        f'<p class="bizdesc clamp" id="bizdesc">{esc(desc)}</p>'
        '<button class="bizmore" onclick="var d=document.getElementById(\'bizdesc\');'
        'var c=d.classList.toggle(\'clamp\');this.textContent=c?\'Show more\':\'Show less\';">Show more</button>'
        if long_enough else f'<p class="bizdesc">{esc(desc)}</p>'
    )
    body = (desc_html + '<div class="mgrid">'
            + metric("Market cap", fmt_usd(info.get("marketCap")),
                     tip2("The total market value of all the company's shares — what it would cost to buy the whole company at today's price.",
                          "Bigger companies (over $200B, 'large cap') are generally steadier; small ones can grow faster but swing harder. It's a size gauge, not good or bad by itself."))
            + metric("Sector", esc(info.get("sector") or "—"),
                     tip2("The broad slice of the economy the company belongs to, like Technology or Healthcare.",
                          "Useful context: it sets expectations for what 'normal' margins, growth, and valuation look like."))
            + metric("Industry", esc(info.get("industry") or "—"),
                     tip2("The specific business it competes in within its sector — e.g. 'Consumer Electronics' inside Technology.",
                          "This is the peer group used for the industry comparisons elsewhere in this report."))
            + metric("Employees", f"{emp:,}" if isinstance(emp, (int, float)) else "—",
                     tip2("Full-time headcount.",
                          "Mainly a scale indicator. Revenue-per-employee (not shown) can hint at efficiency, but headcount alone isn't good or bad."))
            + metric("CEO", esc(ceo or "—"),
                     tip2("The chief executive who runs the company day to day.",
                          "Long-tenured, founder, or large-shareholder CEOs are often seen as a positive for alignment with shareholders."))
            + metric("Country", esc(info.get("country") or "—"),
                     tip2("Where the company is headquartered.",
                          "Affects which accounting rules, taxes, and currency risks apply."))
            + "</div>")
    return section("business", "business", "The Business", "What it does, and whether it's a good business", None, body)


def sec_price(info, stats, price_chart):
    if not stats:
        return section("price", "price", "Price & Momentum", "Data Unavailable",
                       ("n", "no data"), '<div class="empty"><span><b>Price history didn\'t load.</b></span></div>')
    trend_up = stats["ma50"] >= stats["ma200"]
    beta = numf(gi(info, "beta"))
    beta_status = None
    if beta is not None:
        cls = "good" if beta < 1 else "ok" if beta < 1.5 else "bad"
        beta_status = {"cls": cls, "label": STATUS_LABEL[cls], "vs": ""}

    def ret_status(r):
        r = numf(r)
        if r is None:
            return None
        cls = "good" if r >= 0 else "bad"
        return {"cls": cls, "label": STATUS_LABEL[cls], "vs": ""}

    body = ('<div class="mgrid">'
            + metric("1-month return", fmt_pct(stats["ret1m"]),
                     tip2("How much the share price changed over roughly the last month.",
                          "Short-term moves are noisy — don't read too much into one month. Green just means it rose, red that it fell."),
                     ret_status(stats["ret1m"]))
            + metric("6-month return", fmt_pct(stats["ret6m"]),
                     tip2("Price change over roughly the last six months.",
                          "A better sense of medium-term direction than one month, but still just price, not value."),
                     ret_status(stats["ret6m"]))
            + metric("1-year return", fmt_pct(stats["ret1y"]),
                     tip2("Price change over the last year.",
                          "Useful for seeing the bigger trend. Compare it mentally to the broad market (~10%/yr average)."),
                     ret_status(stats["ret1y"]))
            + metric("52-week range", f"${stats['lo52']:.0f}–${stats['hi52']:.0f}",
                     tip2("The lowest and highest prices over the past year.",
                          "Trading near the high reflects optimism; near the low reflects pessimism — which could be a bargain or a warning, so check the other sections."))
            + metric("Trend", "Uptrend" if trend_up else "Downtrend",
                     tip2("Compares the average price over the last 50 days to the last 200 days — a widely watched momentum signal.",
                          "When the short-term average is above the long-term one (uptrend), momentum is positive. It describes direction, not whether the price is fair."),
                     {"cls": "good" if trend_up else "bad", "label": STATUS_LABEL["good" if trend_up else "bad"], "vs": ""})
            + metric("Beta", fmt_ratio(beta),
                     tip2("How much the stock tends to move relative to the overall market.",
                          "1.0 moves with the market; above 1 is more volatile (bigger swings up and down); below 1 is calmer. High beta isn't 'bad', but it's a bumpier ride."),
                     beta_status)
            + "</div>")
    if price_chart:
        body += f'<img class="chart" src="{price_chart}" alt="2-year price chart">'
    return section("price", "price", "Price & Momentum", "How the stock has moved over 2 years", None, body)


def sec_risk(stats):
    if not stats:
        return section("risk", "risk", "Risk & the Bumpy Ride", "Data Unavailable",
                       ("n", "no data"), '<div class="empty"><span><b>Price history didn\'t load.</b></span></div>')
    vol, dd = stats["vol"], stats["max_dd"]
    vcls = "good" if vol < 25 else "ok" if vol < 45 else "bad"
    dcls = "good" if dd > -20 else "ok" if dd > -40 else "bad"
    body = ('<div class="mgrid">'
            + metric("Volatility (annual)", f"{vol:.0f}%",
                     tip2("How much the price bounces around over a year, in statistical terms (annualized standard deviation of daily moves).",
                          "Under ~25% is calm; 25–45% is moderate; over ~45% means large swings in both directions. Higher volatility means a more stressful hold, not necessarily worse returns."),
                     {"cls": vcls, "label": STATUS_LABEL[vcls], "vs": ""})
            + metric("Worst drop", f"{dd:.0f}%",
                     tip2("The biggest peak-to-trough fall over the last two years (maximum drawdown).",
                          "This is the gut-check: if you'd bought at the worst moment, this is how far it sank before recovering. Ask yourself honestly whether you could have held through it."),
                     {"cls": dcls, "label": STATUS_LABEL[dcls], "vs": ""})
            + metric("Now vs its high", f"{stats['from_hi']:.0f}%",
                     tip2("How far below its two-year high the stock currently trades.",
                          "Near 0% means it's at its peak; a deep negative means it's well off its highs — which could be a discount or a sign of trouble. Cross-check with valuation and growth."))
            + metric("Best single day", fmt_pct(stats["best"]),
                     tip2("The largest one-day gain over the period.",
                          "Big single-day jumps are a hallmark of volatile stocks — exciting on the way up, painful on the way down."))
            + metric("Worst single day", fmt_pct(stats["worst"]),
                     tip2("The largest one-day drop over the period.",
                          "Pairs with 'best single day' to show how dramatic the daily swings can get."))
            + metric("Up days", f"{stats['up_days']:.0f}%",
                     tip2("The share of trading days that ended higher than they started.",
                          "Around 50% is normal even for great long-term winners — markets rise in uneven bursts, so this being near half is not a concern."))
            + "</div>")
    msg = ("This stock has seen <b>severe</b> drops — a wild ride suited only to investors who won't panic-sell at the bottom."
           if dd < -40 else
           "This stock has had <b>meaningful</b> dips — normal for stocks, but be sure you could hold through them without selling."
           if dd < -20 else
           "This stock has been <b>relatively steady</b> over this period, though past calm never guarantees future calm.")
    body += f'<p class="note">{msg}</p>'
    return section("risk", "risk", "Risk & the Bumpy Ride", "Know what you're getting into before you buy", None, body)


def sec_value(info, peers=None):
    pe = numf(gi(info, "trailingPE")); pe_fwd = numf(gi(info, "forwardPE"))
    peg = numf(gi(info, "pegRatio") or gi(info, "trailingPegRatio"))
    ps = numf(gi(info, "priceToSalesTrailing12Months")); pb = numf(gi(info, "priceToBook"))
    ev_eb = numf(gi(info, "enterpriseToEbitda"))
    dy = dividend_yield_pct(info); payout = numf(gi(info, "payoutRatio"))
    if not any(v is not None for v in [pe, peg, ps, pb, ev_eb]):
        return section("value", "value", "Valuation", "Data Unavailable",
                       ("n", "no data"), '<div class="empty"><span><b>Valuation ratios didn\'t load.</b></span></div>')
    pe_status = assess(pe, 22, 45, higher=False, peers=peers, key="trailingPE")
    grade = None
    if pe_status:
        grade = {"good": ("g", "reasonable"), "ok": ("a", "fair"), "bad": ("rd", "expensive")}[pe_status["cls"]]
    body = ('<div class="mgrid">'
            + metric("P/E (trailing)", fmt_ratio(pe),
                     tip2("Price ÷ last year's profit per share. It says how many dollars you pay for each $1 the company earns annually.",
                          "Lower is generally cheaper. Roughly: under ~15 is cheap, 15–25 is normal, over ~40 is pricey and needs fast growth to justify — but always judge it against the company's own industry, shown here when available."),
                     pe_status)
            + metric("P/E (forward)", fmt_ratio(pe_fwd),
                     tip2("The same price-to-profit idea, but using next year's expected earnings instead of last year's.",
                          "If it's lower than the trailing P/E, the market expects earnings to grow. If higher, earnings are expected to fall."),
                     assess(pe_fwd, 22, 45, higher=False, peers=peers, key="forwardPE"))
            + metric("PEG ratio", fmt_ratio(peg),
                     tip2("The P/E divided by the company's growth rate — it adjusts 'how expensive' for 'how fast it's growing'.",
                          "Around 1.0 is often considered fair value. Under 1 can be a bargain for the growth you get; over 2 is steep."),
                     assess(peg, 1.2, 2.5, higher=False))
            + metric("Price / Sales", fmt_ratio(ps),
                     tip2("Company value compared to its yearly revenue. Useful for judging companies that aren't very profitable yet.",
                          "Lower is cheaper. What counts as 'normal' varies hugely by industry — software trades far higher than retail — so the industry comparison matters most here."),
                     assess(ps, 3, 10, higher=False, peers=peers, key="priceToSalesTrailing12Months"))
            + metric("Price / Book", fmt_ratio(pb),
                     tip2("Price compared to the company's net assets on paper (book value). A classic Buffett yardstick for asset-heavy businesses.",
                          "Under ~3 is modest; very high means you're paying mostly for brand and future profits, not physical assets. Banks and industrials run low; tech runs high."),
                     assess(pb, 3, 10, higher=False, peers=peers, key="priceToBook"))
            + metric("EV / EBITDA", fmt_ratio(ev_eb),
                     tip2("Total company value (including debt) versus core operating earnings. A cleaner cross-company comparison than P/E because it ignores debt and tax differences.",
                          "Under ~12 is often reasonable; over ~20 is rich. Best read against industry peers."),
                     assess(ev_eb, 12, 22, higher=False, peers=peers, key="enterpriseToEbitda"))
            + metric("Dividend yield", f"{dy:.2f}%" if dy is not None else "—",
                     tip2("The annual cash dividend paid to shareholders, as a percentage of the share price.",
                          "0% isn't bad — many great companies reinvest profits instead. A very high yield (8%+) can signal the market doubts it's sustainable."))
            + metric("Payout ratio", f"{payout*100:.0f}%" if payout is not None else "—",
                     tip2("The share of profit paid out as dividends.",
                          "Comfortable below ~60%. Above ~80% leaves little cushion, so the dividend is more at risk if profits dip."))
            + "</div>"
            + _peer_note(peers, "These valuation ratios are judged against this company's own industry where shown — a P/E that's cheap for software may be expensive for a utility."))
    return section("value", "value", "Valuation — Cheap or Expensive?",
                   "What you're paying per dollar of earnings, sales, and assets", grade, body)


def _peer_note(peers, fallback):
    """A note line that names the peer set when available."""
    if peers and peers.get("medians"):
        ind = esc(peers.get("name") or "its industry")
        n = peers.get("n", 0)
        return (f'<p class="note"><b>Industry comparison.</b> The green/amber/red ratings below compare this '
                f'company against the median of <b>{n}</b> peers in <b>{ind}</b>. That\'s how we avoid comparing '
                f'apples to oranges — a healthy number for one industry can be poor for another. '
                f'Where a peer median isn\'t available, a general rule-of-thumb is used instead.</p>')
    return f'<p class="note">{fallback} (Industry peer data wasn\'t available for this company, so general rule-of-thumb ranges are used.)</p>'


def sec_health(info, peers=None):
    pm = numf(gi(info, "profitMargins")); gm = numf(gi(info, "grossMargins"))
    om = numf(gi(info, "operatingMargins")); roe = numf(gi(info, "returnOnEquity"))
    roa = numf(gi(info, "returnOnAssets")); de = numf(gi(info, "debtToEquity"))
    cr = numf(gi(info, "currentRatio")); fcf = numf(gi(info, "freeCashflow"))
    cash = numf(gi(info, "totalCash")); debt = numf(gi(info, "totalDebt"))
    if not any(v is not None for v in [pm, roe, de, cr]):
        return section("health", "health", "Financial Health", "Data Unavailable",
                       ("n", "no data"), '<div class="empty"><span><b>Financial health data didn\'t load.</b></span></div>')
    pct = lambda x: x * 100 if x is not None else None
    statuses = {
        "pm": assess(pct(pm), 15, 0, peers=peers, key="profitMargins", is_pct_frac=True),
        "gm": assess(pct(gm), 40, 15, peers=peers, key="grossMargins", is_pct_frac=True),
        "om": assess(pct(om), 15, 0, peers=peers, key="operatingMargins", is_pct_frac=True),
        "roe": assess(pct(roe), 15, 0, peers=peers, key="returnOnEquity", is_pct_frac=True),
        "roa": assess(pct(roa), 8, 0, peers=peers, key="returnOnAssets", is_pct_frac=True),
        "de": assess(de, 100, 200, higher=False, peers=peers, key="debtToEquity"),
        "cr": assess(cr, 1.5, 1, peers=peers, key="currentRatio"),
    }
    goods = sum(1 for s in statuses.values() if s and s["cls"] == "good")
    n = sum(1 for s in statuses.values() if s)
    grade = None
    if n:
        r = goods / n
        grade = ("g", "healthy") if r >= .6 else ("a", "mixed") if r >= .35 else ("rd", "fragile")
    fcf_status = ({"cls": "good", "label": "Good", "vs": ""} if (fcf is not None and fcf > 0)
                  else {"cls": "bad", "label": "Watch", "vs": ""} if fcf is not None else None)
    body = ('<div class="mgrid">'
            + metric("Net profit margin", fmt_pct0(pct(pm)),
                     tip2("Of every $1 of sales, how many cents end up as actual profit after all costs.",
                          "Higher means a more efficient, often higher-quality business. Over 20% is excellent in most industries — but grocery chains live on 2% while software can top 30%, so the industry comparison is what matters."),
                     statuses["pm"])
            + metric("Gross margin", fmt_pct0(pct(gm)),
                     tip2("Profit left after only the direct cost of making the product or service, before overhead, marketing, and R&D.",
                          "High, stable gross margins suggest pricing power — a sign of a durable 'moat'. A great gross margin for a steelmaker (~15%) would be weak for a software firm (~70%), which is exactly why this is compared to industry peers."),
                     statuses["gm"])
            + metric("Operating margin", fmt_pct0(pct(om)),
                     tip2("Profit from the core business after all operating costs, but before interest and taxes.",
                          "Shows how profitable the actual operations are. Higher and steadier is better; compare to the industry."),
                     statuses["om"])
            + metric("Return on equity", fmt_pct0(pct(roe)),
                     tip2("How much profit the company generates for every dollar shareholders have invested.",
                          "Buffett prizes consistent ROE above 15%. Very high ROE can sometimes just mean lots of debt, so read it alongside Debt/Equity."),
                     statuses["roe"])
            + metric("Return on assets", fmt_pct0(pct(roa)),
                     tip2("Profit relative to everything the company owns — how efficiently it turns its assets into earnings.",
                          "Higher is better. Asset-heavy industries (airlines, utilities) naturally run lower, so judge against peers."),
                     statuses["roa"])
            + metric("Debt / Equity", fmt_ratio(de / 100 if de is not None else None),
                     tip2("How much debt the company uses compared to shareholders' money. Shown as a ratio (1.0 = equal debt and equity).",
                          "Lower is safer. Under ~1.0 is comfortable for most firms; high debt magnifies losses in a downturn. Capital-heavy industries carry more, so compare to peers."),
                     statuses["de"])
            + metric("Current ratio", fmt_ratio(cr),
                     tip2("Whether the company has enough short-term assets (cash, receivables, inventory) to cover bills due within a year.",
                          "Above 1.0 means it can cover near-term obligations; 1.5–3 is comfortable. Below 1 can be a liquidity warning."),
                     statuses["cr"])
            + metric("Free cash flow", fmt_usd(fcf),
                     tip2("The actual cash left over after running the business and paying for investments — harder to fudge than reported profit.",
                          "Positive and growing is what you want; it funds dividends, buybacks, and debt repayment. Persistent negative free cash flow means the company is burning money."),
                     fcf_status)
            + "</div>"
            + f'<p class="note">Cash position: <b>{fmt_usd(cash)}</b> cash vs <b>{fmt_usd(debt)}</b> total debt. '
            'More cash than debt is a fortress balance sheet; far more debt than cash is a risk to watch.</p>'
            + _peer_note(peers, "Margins and returns vary enormously by industry, so these are best read against peers."))
    return section("health", "health", "Financial Health", "Can it pay its bills and survive a downturn?", grade, body)


def sec_growth(info, growth_chart, peers=None):
    rg = numf(gi(info, "revenueGrowth")); eg = numf(gi(info, "earningsGrowth"))
    rev = numf(gi(info, "totalRevenue")); ebitda = numf(gi(info, "ebitda"))
    if rg is None and not growth_chart:
        return section("growth", "growth", "Growth", "Data Unavailable",
                       ("n", "no data"), '<div class="empty"><span><b>Growth data didn\'t load.</b></span></div>')
    pct = lambda x: x * 100 if x is not None else None
    rg_status = assess(pct(rg), 10, 0, peers=peers, key="revenueGrowth", is_pct_frac=True)
    grade = None
    if rg_status:
        grade = {"good": ("g", "growing"), "ok": ("a", "steady"), "bad": ("rd", "shrinking")}[rg_status["cls"]]
    body = ('<div class="mgrid">'
            + metric("Revenue growth (YoY)", fmt_pct(pct(rg)),
                     tip2("How much total sales grew compared with the same period a year ago.",
                          "Growing sales is the engine of a healthy business. Over ~10% is strong; flat or negative is a worry. Mature industries grow slowly, fast ones quickly — hence the peer comparison."),
                     rg_status)
            + metric("Earnings growth (YoY)", fmt_pct(pct(eg)),
                     tip2("How much profit grew versus a year ago — ultimately what owners care about most.",
                          "Rising profit, especially faster than revenue, signals improving efficiency. Falling profit while sales rise can mean margin pressure."),
                     assess(pct(eg), 10, 0, peers=peers, key="earningsGrowth", is_pct_frac=True))
            + metric("Revenue (TTM)", fmt_usd(rev),
                     tip2("Total sales over the trailing twelve months — the company's top line.",
                          "Bigger isn't automatically better; pair it with growth and margins. It mainly tells you the scale of the business."))
            + metric("EBITDA", fmt_usd(ebitda),
                     tip2("Earnings before interest, taxes, depreciation and amortization — a rough proxy for cash the core operations throw off.",
                          "Useful for comparing operating performance across companies with different debt and tax situations."))
            + "</div>")
    if growth_chart:
        body += ('<div class="sub-h">Multi-year trend</div>'
                 f'<img class="chart" src="{growth_chart}" alt="revenue and net income by year">'
                 '<p class="note">Bars show annual revenue and net income (in $B), oldest to newest. '
                 'Rising, consistent bars are what you want — a single good year can be luck.</p>')
    return section("growth", "growth", "Growth", "Is the business getting bigger and more profitable?", grade, body)


def _df_ok(df):
    return df is not None and hasattr(df, "empty") and not df.empty


def _badge_cls(grade_text):
    g = (grade_text or "").lower()
    if any(w in g for w in ["buy", "outperform", "overweight", "accumulate"]):
        return "buy"
    if any(w in g for w in ["sell", "underperform", "underweight", "reduce"]):
        return "sell"
    return "hold"


def sec_people(info, data):
    body = ""
    # ---- analyst consensus ----
    rec_key = gi(info, "recommendationKey")
    rec_mean = numf(gi(info, "recommendationMean"))
    n_an = gi(info, "numberOfAnalystOpinions")
    tgt_mean = numf(gi(info, "targetMeanPrice"))
    tgt_low = numf(gi(info, "targetLowPrice")); tgt_high = numf(gi(info, "targetHighPrice"))
    price = numf(gi(info, "currentPrice")) or numf(gi(info, "regularMarketPrice")) or numf(gi(info, "previousClose"))
    # also try analyst_price_targets dict
    tg = data.get("targets")
    if isinstance(tg, dict):
        tgt_mean = tgt_mean or numf(tg.get("mean"))
        tgt_low = tgt_low or numf(tg.get("low")); tgt_high = tgt_high or numf(tg.get("high"))
        price = price or numf(tg.get("current"))
    rec_label = None
    if rec_key:
        rec_label = {"strong_buy": "Strong Buy", "buy": "Buy", "hold": "Hold",
                     "underperform": "Underperform", "sell": "Sell"}.get(rec_key, rec_key.replace("_", " ").title())
    if rec_label or tgt_mean:
        upside = ((tgt_mean - price) / price * 100) if (tgt_mean and price) else None
        head = "Wall Street's view" + (f" ({int(n_an)} analysts)" if isinstance(n_an, (int, float)) else "")
        body += f'<div class="sub-h">{esc(head)}</div><div class="mgrid">'
        if rec_label:
            body += metric("Consensus rating", f'<span class="badge {_badge_cls(rec_label)}">{esc(rec_label)}</span>',
                           "The blended buy/sell/hold call from analysts covering the stock.")
        if tgt_mean:
            body += metric("Avg price target", fmt_price(tgt_mean), "Where analysts on average expect the price to go over the next year.")
        if upside is not None:
            body += metric("Implied upside", fmt_pct(upside),
                           tip2("How far the average analyst price target sits above (or below) today's price.",
                                "Positive means analysts see room to rise; negative means the stock already trades above their targets. Targets are opinions, not guarantees."),
                           {"cls": "good", "label": STATUS_LABEL["good"], "vs": ""} if upside > 0
                           else {"cls": "bad", "label": STATUS_LABEL["bad"], "vs": ""})
        if tgt_low and tgt_high:
            body += metric("Target range", f"${tgt_low:.0f}–${tgt_high:.0f}", "Lowest and highest analyst targets — shows how much they disagree.")
        body += "</div>"
    # ---- recommendation split bar ----
    recs = data.get("recs")
    if _df_ok(recs):
        try:
            row = recs.iloc[0]
            sb = int(row.get("strongBuy", 0) or 0); b = int(row.get("buy", 0) or 0)
            h = int(row.get("hold", 0) or 0); s = int(row.get("sell", 0) or 0); ss = int(row.get("strongSell", 0) or 0)
            tot = sb + b + h + s + ss
            if tot > 0:
                seg = lambda n, c, t: f'<div style="flex:{n};background:{c}" title="{t}: {n}"></div>' if n > 0 else ""
                body += ('<div class="sub-h">How analysts split today</div><div class="split">'
                         + seg(sb, "var(--green)", "Strong Buy") + seg(b, "#5db87a", "Buy")
                         + seg(h, "var(--amber)", "Hold") + seg(s, "#e08a5a", "Sell")
                         + seg(ss, "var(--red)", "Strong Sell") + "</div>"
                         f'<div class="legend"><span>● {sb} Strong Buy</span><span>● {b} Buy</span>'
                         f'<span>● {h} Hold</span><span>● {s} Sell</span><span>● {ss} Strong Sell</span></div>')
        except Exception:
            pass
    # ---- recent analyst actions ----
    up = data.get("upgrades")
    if _df_ok(up):
        try:
            d2 = up.copy()
            d2 = d2.sort_index(ascending=False).head(6)
            rows = ""
            for idx, r in d2.iterrows():
                date = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                firm = esc(r.get("Firm", "—")); action = esc(str(r.get("Action", "") or "")[:14])
                to = r.get("ToGrade", "—")
                rows += (f'<tr><td>{date}</td><td>{firm}</td>'
                         f'<td style="color:var(--muted);font-size:12px">{action}</td>'
                         f'<td><span class="badge {_badge_cls(to)}">{esc(to or "—")}</span></td></tr>')
            if rows:
                body += ('<div class="sub-h">Recent analyst actions</div>'
                         '<table class="tbl"><thead><tr><th>Date</th><th>Firm</th><th>Action</th><th>To</th></tr></thead><tbody>'
                         + rows + "</tbody></table>")
        except Exception:
            pass
    # ---- ownership ----
    inst_pct = ins_pct = None
    mh = data.get("major")
    if _df_ok(mh):
        try:
            if "Value" in mh.columns:
                if "insidersPercentHeld" in mh.index:
                    ins_pct = numf(mh.loc["insidersPercentHeld", "Value"])
                if "institutionsPercentHeld" in mh.index:
                    inst_pct = numf(mh.loc["institutionsPercentHeld", "Value"])
        except Exception:
            pass
    inst_pct = inst_pct if inst_pct is not None else numf(gi(info, "heldPercentInstitutions"))
    ins_pct = ins_pct if ins_pct is not None else numf(gi(info, "heldPercentInsiders"))
    if inst_pct is not None or ins_pct is not None:
        body += '<div class="sub-h">Ownership</div><div class="mgrid">'
        if inst_pct is not None:
            body += metric("Institutional ownership", f"{inst_pct*100:.1f}%",
                           "Share held by big funds (pensions, mutual funds, hedge funds). Heavy smart-money ownership can reassure.")
        if ins_pct is not None:
            body += metric("Insider ownership", f"{ins_pct*100:.1f}%",
                           tip2("The share of the company owned by its own executives and directors.",
                                "Higher insider ownership ties leadership's wealth to yours, which is reassuring. Above ~5% is meaningful 'skin in the game'."),
                           {"cls": "good", "label": STATUS_LABEL["good"], "vs": ""} if ins_pct * 100 > 5 else None)
        body += "</div>"
    # ---- insider transactions ----
    ins = data.get("insider")
    if _df_ok(ins):
        try:
            rows = ""
            for _, r in ins.head(6).iterrows():
                txt = str(r.get("Text", "") or "")
                is_buy = any(w in txt.lower() for w in ["purchase", "buy", "acqui", "exercise"])
                name = esc(r.get("Insider", "—"))
                sd = r.get("Start Date")
                date = sd.strftime("%Y-%m-%d") if hasattr(sd, "strftime") else str(sd)[:10] if sd is not None else "—"
                shares = r.get("Shares")
                shares_s = f"{int(shares):,}" if isinstance(shares, (int, float)) and not (isinstance(shares, float) and math.isnan(shares)) else "—"
                color = "var(--green)" if is_buy else "var(--muted)"
                rows += (f'<tr><td>{date}</td><td>{name}</td>'
                         f'<td style="font-size:12px;color:{color}">{esc(txt[:30])}</td><td>{shares_s}</td></tr>')
            if rows:
                body += ('<div class="sub-h">Recent insider trades '
                         + tip("Executives and directors trading their own stock. Buying often signals confidence; routine selling is normal.")
                         + '</div><table class="tbl"><thead><tr><th>Date</th><th>Insider</th><th>Transaction</th><th>Shares</th></tr></thead><tbody>'
                         + rows + "</tbody></table>")
        except Exception:
            pass
    # ---- leadership ----
    officers = info.get("companyOfficers") or []
    if officers:
        rows = ""
        for o in officers[:5]:
            pay = o.get("totalPay")
            pay_s = fmt_usd(pay) if isinstance(pay, (int, float)) else "—"
            rows += f'<tr><td>{esc(o.get("name","—"))}</td><td style="color:var(--muted)">{esc(o.get("title","—"))}</td><td>{pay_s}</td></tr>'
        body += ('<div class="sub-h">Leadership</div>'
                 '<table class="tbl"><thead><tr><th>Name</th><th>Title</th><th>Pay</th></tr></thead><tbody>'
                 + rows + "</tbody></table>")
    if not body:
        body = '<div class="empty"><span><b>People &amp; ownership data didn\'t load.</b></span></div>'
    grade = None
    if rec_mean is not None:
        grade = ("g", "bullish") if rec_mean <= 2.2 else ("rd", "bearish") if rec_mean >= 3.5 else ("a", "neutral")
    return section("people", "people", "The People & the Smart Money",
                   "Analysts, insiders, institutions, and leadership", grade, body)


def sec_checklist(info, stats):
    items = []
    def add(state, text):
        items.append((state, text))
    pct = lambda x: x * 100 if numf(x) is not None else None
    if stats:
        add("y" if stats["last"] >= stats["ma200"] else "n",
            f"Trading {'above' if stats['last'] >= stats['ma200'] else 'below'} its 200-day average — long-term trend is {'up' if stats['last'] >= stats['ma200'] else 'down'}.")
        v = stats["vol"]
        add("y" if v < 40 else "q" if v < 60 else "n",
            f"Volatility is {v:.0f}% a year — {'manageable' if v < 40 else 'on the higher side' if v < 60 else 'very high; expect big swings'}.")
        add("y" if stats["max_dd"] > -35 else "q",
            f"Worst drop in 2 years was {stats['max_dd']:.0f}% — {'within a normal range' if stats['max_dd'] > -35 else 'steep; could you have held?'}")
    pm = pct(gi(info, "profitMargins"))
    if pm is not None:
        add("y" if pm > 5 else "q" if pm > 0 else "n", "Solidly profitable (healthy net margin).")
    roe = pct(gi(info, "returnOnEquity"))
    if roe is not None:
        add("y" if roe > 15 else "q" if roe > 5 else "n", "Strong return on shareholders' money (ROE above 15%).")
    de = numf(gi(info, "debtToEquity"))
    if de is not None:
        add("y" if de < 100 else "n" if de > 200 else "q", "Conservative debt relative to equity.")
    cr = numf(gi(info, "currentRatio"))
    if cr is not None:
        add("y" if cr > 1 else "n", "Can cover its short-term bills (current ratio above 1).")
    fcf = numf(gi(info, "freeCashflow"))
    if fcf is not None:
        add("y" if fcf > 0 else "n", "Generates positive free cash flow.")
    pe = numf(gi(info, "trailingPE"))
    if pe is not None:
        add("y" if 0 < pe < 25 else "n" if pe > 40 else "q", "Valuation (P/E) isn't stretched.")
    rg = pct(gi(info, "revenueGrowth"))
    if rg is not None:
        add("y" if rg > 5 else "q" if rg > 0 else "n", "Sales are growing year over year.")
    eg = pct(gi(info, "earningsGrowth"))
    if eg is not None:
        add("y" if eg > 0 else "n", "Profit is growing year over year.")
    rm = numf(gi(info, "recommendationMean"))
    if rm is not None:
        add("y" if rm <= 2.5 else "n" if rm >= 3.5 else "q", "Wall Street analysts lean positive overall.")
    ins = pct(gi(info, "heldPercentInsiders"))
    if ins is not None:
        add("y" if ins > 3 else "q", f"Insiders own {ins:.1f}% — leadership has skin in the game.")

    yes = sum(1 for s, _ in items if s == "y")
    tot = len(items)
    grade = None
    if tot:
        ratio = yes / tot
        cls = "g" if ratio >= .65 else "a" if ratio >= .4 else "rd"
        grade = (cls, f"{yes}/{tot} green")
    rows = ""
    for s, t in items:
        sym = "✓" if s == "y" else "✕" if s == "n" else "?"
        col = "var(--green)" if s == "y" else "var(--red)" if s == "n" else "var(--amber)"
        rows += (f'<li style="display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--border);font-size:14px">'
                 f'<span style="flex-shrink:0;width:18px;font-weight:700;color:{col}">{sym}</span><span>{esc(t)}</span></li>')
    body = (f'<ul style="list-style:none;padding:0;margin:0">{rows}</ul>'
            '<p class="note">Green = good sign, red = caution, amber = needs your judgment. No score here is a verdict — '
            'it\'s a structured way to see the whole picture at a glance.</p>')
    return section("check", "check", "The Investor's Checklist",
                   "A quick scan of the questions value investors ask", grade, body)


# ============================================================
#  BOTTOM LINE  — synthesized summary, consensus, forward outlook
# ============================================================
def _consensus_from_score(avg):
    """Map the 1-5 composite to one of the four verdicts."""
    if avg is None:
        return ("Hold", "n")
    if avg >= 4.0:
        return ("Strong Buy", "g")
    if avg >= 3.3:
        return ("Moderate Buy", "g")
    if avg >= 2.5:
        return ("Hold", "a")
    return ("Sell", "rd")


def sec_summary(info, stats, data):
    peers = data.get("peers")
    s = score_pillars(info, stats)
    known = [x for x in s.values() if x is not None]
    avg = sum(known) / len(known) if known else None
    verdict, vcls = _consensus_from_score(avg)

    # Wall Street consensus (from Yahoo), shown alongside ours
    rec_key = gi(info, "recommendationKey")
    ws = None
    if rec_key:
        ws = {"strong_buy": "Strong Buy", "buy": "Buy", "hold": "Hold",
              "underperform": "Underperform", "sell": "Sell"}.get(rec_key, rec_key.replace("_", " ").title())
    n_an = gi(info, "numberOfAnalystOpinions")

    # ---- build reasoning bullets dynamically from the data ----
    pos, neg = [], []
    pct = lambda x: x * 100 if numf(x) is not None else None
    gm, pm, roe = pct(gi(info, "grossMargins")), pct(gi(info, "profitMargins")), pct(gi(info, "returnOnEquity"))
    rg = pct(gi(info, "revenueGrowth")); de = numf(gi(info, "debtToEquity")); pe = numf(gi(info, "trailingPE"))
    fcf = numf(gi(info, "freeCashflow"))
    if pm is not None and pm > 15: pos.append(f"high net profit margin ({pm:.0f}%), a sign of a quality business")
    if roe is not None and roe > 18: pos.append(f"strong return on equity ({roe:.0f}%)")
    if rg is not None and rg > 10: pos.append(f"solid revenue growth ({rg:.0f}% year-over-year)")
    if fcf is not None and fcf > 0: pos.append("generates positive free cash flow")
    if de is not None and de < 80: pos.append("conservative debt load")
    if stats and stats.get("ret1y") is not None and stats["ret1y"] > 15: pos.append(f"strong 1-year price momentum ({stats['ret1y']:+.0f}%)")
    if s.get("smart") and s["smart"] >= 4: pos.append("analysts lean bullish")

    if pe is not None and pe > 35: neg.append(f"a rich valuation (P/E of {pe:.0f})")
    if pe is not None and pe < 0: neg.append("negative earnings (no P/E)")
    if de is not None and de > 150: neg.append(f"a heavy debt load (debt/equity {de/100:.1f}x)")
    if rg is not None and rg < 0: neg.append(f"shrinking revenue ({rg:.0f}% YoY)")
    if pm is not None and pm < 5: neg.append("thin profit margins")
    if stats and stats.get("vol") is not None and stats["vol"] > 45: neg.append(f"high volatility ({stats['vol']:.0f}%/yr) — a bumpy hold")
    if stats and stats.get("max_dd") is not None and stats["max_dd"] < -45: neg.append(f"a severe past drawdown ({stats['max_dd']:.0f}%)")
    if s.get("val") and s["val"] <= 2: neg.append("looks expensive on the numbers")

    if peers and peers.get("name"):
        pos_ctx = f" (judged against {peers.get('n','its')} peers in {esc(peers['name'])})"
    else:
        pos_ctx = ""

    def bullets(items, fallback):
        if not items:
            return f'<li>{fallback}</li>'
        return "".join(f'<li>{esc(x) if "<" not in x else x}</li>' for x in items)

    # ---- forward price outlook ----
    price = numf(gi(info, "currentPrice")) or numf(gi(info, "regularMarketPrice")) \
        or (stats["last"] if stats else None) or numf(gi(info, "previousClose"))
    tgt_mean = numf(gi(info, "targetMeanPrice"))
    tgt_low = numf(gi(info, "targetLowPrice")); tgt_high = numf(gi(info, "targetHighPrice"))
    tg = data.get("targets")
    if isinstance(tg, dict):
        tgt_mean = tgt_mean or numf(tg.get("mean"))
        tgt_low = tgt_low or numf(tg.get("low")); tgt_high = tgt_high or numf(tg.get("high"))

    outlook = ""
    if price:
        # 1-year: prefer real analyst targets
        if tgt_mean:
            up = (tgt_mean - price) / price * 100
            rng = f" (range ${tgt_low:.0f}–${tgt_high:.0f})" if (tgt_low and tgt_high) else ""
            one_yr = (f'<b>1 year — analyst consensus target: {fmt_price(tgt_mean)}</b>{rng}, '
                      f'about {up:+.0f}% from today\'s {fmt_price(price)}. '
                      f'{"This is real Wall Street data" if n_an else "Based on available analyst data"}'
                      f'{f" from {int(n_an)} analysts" if isinstance(n_an,(int,float)) else ""}.')
            base_annual = max(-0.10, min(0.30, up / 100))
        else:
            one_yr = (f'<b>1 year:</b> no analyst price target was available, so a long-run market-average assumption is used below.')
            base_annual = 0.08
        # multi-year illustrative scenarios (transparent compounding)
        bear, base, bull = -0.08, base_annual, max(base_annual + 0.10, 0.12)
        def proj(rate, yrs):
            return price * ((1 + rate) ** yrs)
        rows = ""
        for yrs in (3, 5):
            rows += (f'<tr><td>{yrs} years</td>'
                     f'<td>{fmt_price(proj(bear,yrs))}</td>'
                     f'<td>{fmt_price(proj(base,yrs))}</td>'
                     f'<td>{fmt_price(proj(bull,yrs))}</td></tr>')
        outlook = (
            f'<div class="sub-h">Where might it go?</div>'
            f'<p style="font-size:14px;margin:0 0 12px">{one_yr}</p>'
            f'<p style="font-size:13px;color:var(--muted);margin:0 0 10px">Longer-term <b>illustrative scenarios</b> '
            f'— these simply compound today\'s price at the labeled annual rates. They are <b>not predictions</b>, '
            f'just a way to picture outcomes if things go poorly (Bear, −8%/yr), as expected (Base, '
            f'{base*100:+.0f}%/yr), or well (Bull, {bull*100:+.0f}%/yr):</p>'
            f'<table class="tbl"><thead><tr><th>Horizon</th><th>Bear</th><th>Base</th><th>Bull</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')

    # ---- assemble ----
    ws_line = ""
    if ws:
        ws_line = (f'<div class="cons-ws">Wall Street analysts'
                   f'{f" ({int(n_an)})" if isinstance(n_an,(int,float)) else ""}: '
                   f'<b>{esc(ws)}</b></div>')

    body = (
        f'<div class="cons-wrap">'
        f'<div class="cons-badge {vcls}">{verdict}</div>'
        f'<div class="cons-meta"><div class="cons-lbl">Stock Lens composite</div>'
        f'{f"<div class=\"cons-sub\">based on a {avg:.1f} / 5 average across the six pillars{pos_ctx}</div>" if avg else ""}'
        f'{ws_line}</div></div>'
        f'<div class="sub-h">Why</div>'
        f'<div class="why-grid">'
        f'<div class="why-col pos"><div class="why-h">✓ Strengths</div><ul>{bullets(pos, "No standout strengths in the available data.")}</ul></div>'
        f'<div class="why-col neg"><div class="why-h">⚠ Watch-outs</div><ul>{bullets(neg, "No major red flags in the available data.")}</ul></div>'
        f'</div>'
        + outlook +
        '<p class="note"><b>How to use this.</b> The verdict is a data-derived composite of the six pillars above '
        'blended with analyst sentiment — a structured summary, <b>not financial advice</b> and not a guarantee. '
        'The price scenarios are illustrative math, not forecasts. Real outcomes depend on the business, the economy, '
        'and events no model can foresee. Always do your own research and consider a licensed professional before investing.</p>'
    )
    return section("summary", "summary", "The Bottom Line",
                   "A plain-English summary, consensus verdict, and forward outlook", (vcls, verdict), body,
                   open_default=True)


# ============================================================
#  SCORECARD + PAGE ASSEMBLY
# ============================================================
def build_scorecard(info, stats):
    name = info.get("longName") or info.get("shortName") or info.get("symbol") or ""
    ticker = info.get("symbol") or ""
    sub = " · ".join([x for x in [info.get("fullExchangeName") or info.get("exchange"),
                                  info.get("sector"), info.get("industry")] if x])
    price = numf(gi(info, "currentPrice")) or numf(gi(info, "regularMarketPrice")) \
        or (stats["last"] if stats else None) or numf(gi(info, "previousClose"))
    prev = numf(gi(info, "regularMarketPreviousClose")) or numf(gi(info, "previousClose"))
    chg = ((price - prev) / prev * 100) if (price and prev) else None
    chg_html = ""
    if chg is not None:
        chg_html = f'<div class="c {"up" if chg >= 0 else "down"}">{fmt_pct(chg)} today</div>'

    s = score_pillars(info, stats)
    order = [("Business", "biz"), ("Value", "val"), ("Health", "health"),
             ("Growth", "growth"), ("Momentum", "mom"), ("Analysts", "smart")]
    pillars = ""
    for lbl, k in order:
        sc = s[k]
        cls = "g" if (sc and sc >= 4) else "a" if (sc and sc >= 3) else "rd" if sc else ""
        dots = "".join(f'<span class="dot {cls if (sc and i <= sc) else ""}"></span>' for i in range(1, 6))
        pillars += (f'<div class="pillar"><div class="l">{lbl}</div>'
                    f'<div class="r"><div class="dots">{dots}</div><span class="gr">{pillar_grade(sc)}</span></div></div>')

    known = [x for x in s.values() if x is not None]
    avg = sum(known) / len(known) if known else None
    if avg is None:
        verdict = "Live price loaded, but the detailed feed was thin for this ticker — the sections below show whatever did come through."
    else:
        tone = "broadly strong" if avg >= 4 else "mixed but reasonable" if avg >= 3 else "showing real caution flags"
        verdict = (f'<b>Quick read:</b> across the six pillars, {esc(ticker)} looks <b>{tone}</b> on the data available '
                   f'(average {avg:.1f} of 5). Open each section below for the plain-English "why." '
                   "This is a research starting point, never a buy or sell command.")

    return f"""<div class="scard">
  <div class="top">
    <div><p class="nm">{esc(name)} ({esc(ticker)})</p><p class="sub">{esc(sub)}</p></div>
    <div class="pr"><div class="v">{fmt_price(price)}</div>{chg_html}</div>
  </div>
  <div class="pillars">{pillars}</div>
  <div class="verdict">{verdict}</div>
</div>"""


def build_report_fragment(data):
    """The report body only (scorecard + sections + disclaimer), no page wrapper.
    Used by the website, which already provides <html>, the CSS, and its own header."""
    info = data.get("info") or {}
    stats = price_stats(data.get("hist"))
    price_chart = chart_chart = chart_price(stats)
    growth_chart = chart_growth(data.get("income"))
    peers = data.get("peers")
    sections = "".join([
        sec_primer(peers),
        sec_business(info),
        sec_price(info, stats, price_chart),
        sec_risk(stats),
        sec_value(info, peers),
        sec_health(info, peers),
        sec_growth(info, growth_chart, peers),
        sec_people(info, data),
        sec_checklist(info, stats),
        sec_summary(info, stats, data),
    ])
    stamp = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    return f"""<div class="report-meta">Report for <b>{esc(data['ticker'])}</b> · generated {stamp} · data from Yahoo Finance</div>
{build_scorecard(info, stats)}
<div class="controls">
  <button onclick="document.querySelectorAll('.sec').forEach(s=>s.classList.add('open'))">Expand all</button>
  <button onclick="document.querySelectorAll('.sec').forEach(s=>s.classList.remove('open'))">Collapse all</button>
</div>
{sections}
<p class="disc"><b>Educational tool — not investment advice.</b> Stock Lens compiles publicly available data and
explains common analysis frameworks. Data may be delayed, incomplete, or wrong, and the "good / caution" thresholds
are general rules of thumb, not sector-calibrated truth. Nothing here is a recommendation to buy, sell, or hold any
security. Markets carry real risk of loss — do your own research and consider a licensed financial professional.</p>"""


def build_html(data):
    """Full standalone HTML page (used by the desktop/terminal version)."""
    badge_svg = ('<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" '
                 'stroke-linejoin="round"><path d="M3 3v18h18"/><path d="m7 14 4-4 3 3 5-6"/></svg>')
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stock Lens — {esc(data['ticker'])}</title><style>{CSS}</style></head><body>
<div class="wrap">
  <div class="hero">
    <div class="badge">{badge_svg}</div>
    <h1>Stock Lens</h1>
    <p class="tk">Research report for <b>{esc(data['ticker'])}</b></p>
  </div>
  {build_report_fragment(data)}
</div></body></html>"""


# ============================================================
#  MAIN
# ============================================================
def generate_report(ticker):
    """Reusable entry point for the web backend (and anything else).

    Returns a dict:
        {"ok": True,  "ticker": "AAPL", "html": "<...>", "partial": bool}
        {"ok": False, "ticker": "AAPL", "error": "human-readable reason"}
    Never raises — always returns a dict.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {"ok": False, "ticker": "", "error": "Please enter a ticker symbol."}
    # basic sanity: tickers are short and alphanumeric (allow . and - for e.g. BRK-B)
    if len(ticker) > 12 or not all(c.isalnum() or c in ".-" for c in ticker):
        return {"ok": False, "ticker": ticker,
                "error": "That doesn't look like a valid ticker symbol (try something like AAPL or BRK-B)."}
    try:
        data = fetch_all(ticker)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ticker": ticker, "error": f"Couldn't fetch data: {e}"}
    if not valid(data):
        detail = ""
        if data.get("errors"):
            detail = " (" + data["errors"][0] + ")"
        return {"ok": False, "ticker": ticker,
                "error": f"Couldn't find usable data for '{ticker}'. Double-check the symbol and try again.{detail}"}
    try:
        html = build_html(data)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ticker": ticker, "error": f"Couldn't build the report: {e}"}
    return {"ok": True, "ticker": ticker, "html": html,
            "partial": bool(data.get("errors"))}


def analyze_ticker(ticker):
    ticker = ticker.strip().upper()
    if not ticker:
        return
    print(f"  Fetching data for {ticker} … (this takes a few seconds)")
    data = fetch_all(ticker)
    if not valid(data):
        print(f"  ✗ Couldn't find usable data for '{ticker}'.")
        print("    • Double-check the ticker symbol (e.g. AAPL, MSFT, BRK-B).")
        print("    • Make sure you're connected to the internet.")
        if data["errors"]:
            print("    • Details: " + data["errors"][0])
        print()
        return
    html = build_html(data)
    # write to a temp file and open in the default browser
    out_dir = os.path.join(tempfile.gettempdir(), "stock_lens_reports")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"stock_lens_{ticker}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ Report ready — opening {ticker} in your browser.")
    if data["errors"]:
        print(f"    (A few optional fields were unavailable; the rest loaded fine.)")
    print()
    try:
        webbrowser.open("file://" + os.path.abspath(path))
    except Exception:
        print(f"    If your browser didn't open, open this file manually:\n    {path}\n")


def main():
    print()
    print("  ┌───────────────────────────────────────────┐")
    print("  │   STOCK LENS — one-stop stock research      │")
    print("  └───────────────────────────────────────────┘")
    print("  Type a ticker symbol to generate a full report in your browser.")
    print("  Examples: AAPL, MSFT, NVDA, KO, JPM, TSLA, BRK-B")
    print("  Type 'quit' (or press Ctrl-C) to exit.\n")

    # tickers passed on the command line? do those and exit.
    args = [a for a in sys.argv[1:] if a.strip()]
    if args:
        for t in args:
            analyze_ticker(t)
        return

    while True:
        try:
            entry = input("  Ticker › ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Goodbye!\n")
            break
        if entry.lower() in ("quit", "exit", "q", ""):
            print("  Goodbye!\n")
            break
        analyze_ticker(entry)


if __name__ == "__main__":
    main()
