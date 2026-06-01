"""
Landing page for the Stock Lens website.

It reuses the EXACT same CSS as the report (imported from stock_lens) so the
inline report looks identical to the standalone version, then adds the search
box and the small bit of JavaScript that calls /analyze and injects the result.
"""

import stock_lens as sl

# Extra styles just for the landing/search experience (report styles come from sl.CSS)
EXTRA_CSS = """
.search{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;max-width:560px;margin:24px auto 10px;}
.search input{flex:1;min-width:200px;font-size:18px;padding:14px 18px;border:1.5px solid var(--border2);
  border-radius:var(--r);background:var(--surface);color:var(--ink);text-transform:uppercase;
  letter-spacing:.06em;font-weight:600;text-align:center;transition:.15s;}
.search input::placeholder{font-weight:400;letter-spacing:.02em;color:var(--hint);text-transform:none;}
.search input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 4px var(--accent-bg);}
.search button{font-size:16px;font-weight:600;padding:14px 30px;border-radius:var(--r);border:none;
  background:var(--accent);color:#fff;cursor:pointer;transition:.12s;}
.search button:hover{filter:brightness(1.08);}.search button:active{transform:scale(.97);}
.search button:disabled{opacity:.55;cursor:default;}
.examples{text-align:center;font-size:13px;color:var(--hint);margin:0 0 10px;}
.examples span{color:var(--accent);cursor:pointer;font-weight:600;margin:0 5px;}
.examples span:hover{text-decoration:underline;}
.status{max-width:680px;margin:6px auto 0;font-size:14px;color:var(--muted);text-align:center;min-height:22px;padding:0 8px;}
.status.err{color:var(--red);}
.spin{display:inline-block;width:15px;height:15px;border:2px solid var(--border2);border-top-color:var(--accent);
  border-radius:50%;animation:sp .7s linear infinite;vertical-align:-3px;margin-right:8px;}
@keyframes sp{to{transform:rotate(360deg)}}
.report-meta{text-align:center;font-size:12px;color:var(--hint);margin:10px 0 18px;}
.intro{max-width:620px;margin:18px auto 0;text-align:center;color:var(--muted);font-size:14px;line-height:1.6;}
#report{margin-top:24px;}
.footer{text-align:center;font-size:12px;color:var(--hint);margin-top:40px;padding-top:18px;border-top:1px solid var(--border);}
"""

BADGE_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" '
             'stroke-linejoin="round"><path d="M3 3v18h18"/><path d="m7 14 4-4 3 3 5-6"/></svg>')

# Note: braces in the JS are doubled {{ }} because this is an f-string.
LANDING_PAGE = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stock Lens — one-stop stock research</title>
<style>{sl.CSS}{EXTRA_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div class="badge">{BADGE_SVG}</div>
    <h1>Stock Lens</h1>
    <p class="tk">Type a ticker. See everything a serious investor weighs before buying — in plain English.</p>
  </div>

  <div class="search">
    <input type="text" id="ticker" placeholder="Enter a ticker, e.g. AAPL" autocomplete="off" spellcheck="false">
    <button id="goBtn">Analyze</button>
  </div>
  <p class="examples">Try:
    <span data-t="AAPL">AAPL</span><span data-t="MSFT">MSFT</span><span data-t="NVDA">NVDA</span>
    <span data-t="KO">KO</span><span data-t="JPM">JPM</span><span data-t="TSLA">TSLA</span>
  </p>
  <div class="status" id="status"></div>

  <p class="intro" id="intro">Stock Lens pulls a company's business, valuation, financial health, growth, risk,
  and what analysts &amp; insiders are doing — then sums it up with a plain-English verdict. It's an educational
  research tool, not investment advice. Enter a ticker above to begin.</p>

  <div id="report"></div>

  <div class="footer">Stock Lens · educational use only · data from Yahoo Finance · not investment advice</div>
</div>

<script>
const $ = id => document.getElementById(id);
let busy = false;

async function analyze(t){{
  t = (t || $("ticker").value).trim().toUpperCase();
  if(!t){{ setStatus("Please enter a ticker symbol.", true); return; }}
  if(busy) return;
  busy = true;
  $("goBtn").disabled = true;
  $("intro").style.display = "none";
  $("report").innerHTML = "";
  setStatus('<span class="spin"></span>Loading ' + t + ' … (this can take a few seconds)');
  try {{
    const res = await fetch("/analyze?ticker=" + encodeURIComponent(t));
    const data = await res.json();
    if(data.ok){{
      $("report").innerHTML = data.html;
      setStatus(data.partial ? "Loaded — a few optional fields were unavailable, the rest is shown below."
                             : (data.cached ? "" : ""));
      // scroll the report into view on small screens
      $("report").scrollIntoView({{behavior:"smooth", block:"start"}});
    }} else {{
      setStatus(data.error || "Something went wrong. Please try again.", true);
      $("intro").style.display = "block";
    }}
  }} catch(e) {{
    setStatus("Couldn't reach the server. Check your connection and try again.", true);
    $("intro").style.display = "block";
  }} finally {{
    busy = false;
    $("goBtn").disabled = false;
  }}
}}

function setStatus(html, isErr){{
  const s = $("status");
  s.innerHTML = html || "";
  s.className = "status" + (isErr ? " err" : "");
}}

$("goBtn").onclick = () => analyze();
$("ticker").addEventListener("keydown", e => {{ if(e.key === "Enter") analyze(); }});
document.querySelectorAll(".examples span").forEach(b =>
  b.onclick = () => {{ $("ticker").value = b.dataset.t; analyze(b.dataset.t); }});

// allow deep links like /?t=AAPL
const qs = new URLSearchParams(location.search).get("t");
if(qs){{ $("ticker").value = qs.toUpperCase(); analyze(qs); }}
</script>
</body>
</html>"""
