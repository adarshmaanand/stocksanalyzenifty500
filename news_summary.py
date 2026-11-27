import csv
import email.utils
import glob
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests

# ---------------- CONFIG ----------------
IST = timezone(timedelta(hours=5, minutes=30))
HOURS = int(os.environ.get("NEWS_HOURS", "24")) # only news from the last N hours
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "50")) # cap headlines sent for analysis
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "") # optional backup (Claude)
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
HEADERS = {"User-Agent": "Mozilla/5.0 (personal news digest)"}
PRED_FILE = "predictions.csv" # prediction history, saved back to the repo by the workflow
BAND = 0.25 # Nifty move (%) smaller than this counts as "Flat"

MC_FEEDS = {
    "Top News": "https://www.moneycontrol.com/rss/MCtopnews.xml",
    "Latest": "https://www.moneycontrol.com/rss/latestnews.xml",
    "Markets": "https://www.moneycontrol.com/rss/marketreports.xml",
    "Business": "https://www.moneycontrol.com/rss/business.xml",
    "Buzzing Stocks": "https://www.moneycontrol.com/rss/buzzingstocks.xml",
}
# Used automatically if the Moneycontrol feeds return too little fresh news
FALLBACK_FEEDS = {
    "Moneycontrol Markets (Google News)":
        "https://news.google.com/rss/search?q=site:moneycontrol.com+(stocks+OR+sensex+OR+nifty+OR+shares+OR+RBI+OR+earnings)+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
    "Moneycontrol (Google News)":
        "https://news.google.com/rss/search?q=site:moneycontrol.com+when:1d&hl=en-IN&gl=IN&ceid=IN:en",
    "Moneycontrol (Bing News)":
        "https://www.bing.com/news/search?q=site%3Amoneycontrol.com&format=RSS&qft=interval%3D%227%22",
}
MIN_ITEMS_BEFORE_FALLBACK = 8
# ----------------------------------------


def clean(text, limit=220):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse_date(text):
    try:
        dt = email.utils.parsedate_to_datetime(text)
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def fetch_feed(label, url):
    resp = None
    for attempt in range(3):
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code == 429: # rate limited: wait and retry
            time.sleep(20 * (attempt + 1))
            continue
        break
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    out = []
    for item in root.iter("item"):
        title = clean(item.findtext("title"), 300)
        link = (item.findtext("link") or "").strip()
        pub = parse_date(item.findtext("pubDate"))
        if title and link and pub:
            out.append({"title": re.sub(r"\s+-\s+Moneycontrol$", "", title),
                        "link": link, "time": pub, "source": label,
                        "summary": clean(item.findtext("description"))})
    return out


def collect_news(now, notes):
    cutoff = now - timedelta(hours=HOURS)
    items, seen = [], set()

    def add_from(feeds):
        for label, url in feeds.items():
            try:
                found = fetch_feed(label, url)
            except Exception as e:
                print(f"Feed failed ({label}): {e}")
                continue
            if not found:
                print(f"{label}: feed returned no items")
                continue
            # Some feeds label IST times as UTC, which puts the newest items hours in the future
            ahead = max(it["time"] for it in found) - now
            if timedelta(hours=3) <= ahead <= timedelta(hours=8):
                for it in found:
                    it["time"] -= timedelta(hours=5, minutes=30)
                print(f"{label}: dates looked shifted by 5.5h, corrected")
            newest = max(it["time"] for it in found)
            oldest = min(it["time"] for it in found)
            print(f"{label}: newest item {newest:%d %b %H:%M}, oldest {oldest:%d %b %H:%M} (now {now:%d %b %H:%M})")
            fresh = 0
            for it in found:
                key = re.sub(r"\W+", "", it["title"].lower())
                if key in seen or not (cutoff <= it["time"] <= now + timedelta(hours=1)):
                    continue
                seen.add(key)
                items.append(it)
                fresh += 1
            print(f"{label}: {len(found)} items, {fresh} new in last {HOURS}h")

    add_from(MC_FEEDS)
    if len(items) < MIN_ITEMS_BEFORE_FALLBACK:
        notes.append("Moneycontrol feeds gave little fresh news, so Google News "
                     "(Moneycontrol articles) was also used.")
        add_from(FALLBACK_FEEDS)

    items.sort(key=lambda x: x["time"], reverse=True)
    return items[:MAX_ITEMS]


def build_prompt(items, snapshot=None, horizon="today's close"):
    lines = [f"{i} | {it['time']:%d %b %H:%M} | {it['title']} | {it['summary']}"
             for i, it in enumerate(items, start=1)]
    prompt = (
        "Below are Moneycontrol headlines from the last 24 hours (IST). For each one, judge its likely "
        "effect on Indian stock prices, then give an overall view of the market.\n\n"
        "Return ONLY valid JSON (no markdown) in this shape:\n"
        '{"market_mood": "Bullish|Bearish|Neutral|Mixed",\n'
        ' "market_outlook": "1-2 sentences: likely bias for Nifty/Sensex today (up, down or flat) and your confidence",\n'
        ' "nifty_bias": "Up|Down|Flat", "confidence": 0-100,\n'
        ' "summary": "2-3 sentences on what happened",\n'
        ' "opinion": "3-5 sentences: your view on likely near-term market direction, with key risks",\n'
        ' "watch": ["up to 5 short strings: sectors, stocks or events to watch"],\n'
        ' "items": [{"id": 1, "sentiment": "Positive|Negative|Neutral|Not relevant", '
        '"scope": "Stock|Sector|Market|None", "impact": "High|Medium|Low", '
        '"stocks": ["affected stocks or sectors"], '
        '"reason": "max 20 words, and which way prices may move"}]}\n\n'
        "Rules: scope Market = could move the broad indices (RBI, rates, crude, FII flows, global cues, "
        "budget, index heavyweights); Sector = a whole industry; Stock = specific companies. Use "
        "sentiment 'Not relevant' and scope 'None' for headlines with no plausible effect on listed "
        "Indian stocks (crime, lifestyle, foreign politics and similar). Use only the headlines given; "
        "do not invent facts, prices or numbers; say when you are unsure; this is analysis, not "
        "investment advice.\n\nHeadlines (id | time | title | summary):\n" + "\n".join(lines)
    )
    if snapshot:
        prompt = ("Market data snapshot (latest vs previous close). Use these real figures and never "
                  "invent numbers:\n" + snapshot_to_text(snapshot) + "\n\n" + prompt)
    prompt = (f"Prediction task: nifty_bias is your call for where the Nifty 50 will be at {horizon} "
              "versus its latest level (Up = rise of more than 0.25%, Down = fall of more than 0.25%, "
              "otherwise Flat). confidence is 0-100.\n\n" + prompt)
    return prompt


def extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("model did not return JSON")
    return json.loads(text[start:end + 1])


SYSTEM_TEXT = "You are a careful, balanced Indian equity market analyst."


def call_gemini(prompt, model):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    resp = None
    for attempt in range(3):
        resp = requests.post(
            url,
            headers={"x-goog-api-key": GEMINI_KEY, "content-type": "application/json"},
            json={"systemInstruction": {"parts": [{"text": SYSTEM_TEXT}]},
                  "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192,
                                       "responseMimeType": "application/json"}},
            timeout=180,
        )
        if resp.status_code in (429, 503): # busy or rate limited: wait and retry
            time.sleep(20 * (attempt + 1))
            continue
        break
    if not resp.ok:
        raise RuntimeError(f"API error {resp.status_code}: {resp.text[:200]}")
    parts = resp.json()["candidates"][0]["content"]["parts"]
    return extract_json("".join(p.get("text", "") for p in parts if not p.get("thought")))


def call_claude(prompt, model):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": 8000, "system": SYSTEM_TEXT,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=180,
    )
    if not resp.ok:
        raise RuntimeError(f"API error {resp.status_code}: {resp.text[:200]}")
    text = "".join(b.get("text", "") for b in resp.json().get("content", [])
                   if b.get("type") == "text")
    return extract_json(text)


def discover_gemini_models():
    """Ask Google which Flash models this key can use (names change over time)."""
    resp = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                        headers={"x-goog-api-key": GEMINI_KEY}, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"model list error {resp.status_code}")
    names = []
    for m in resp.json().get("models", []):
        name = m.get("name", "").replace("models/", "")
        if ("flash" in name and "generateContent" in m.get("supportedGenerationMethods", [])
                and not any(x in name for x in ("image", "tts", "live", "audio", "embed", "thinking"))):
            names.append(name)
    # newest version first, preferring the full Flash model over Flash-Lite
    names.sort(key=lambda n: ("lite" in n, [-int(x) for x in re.findall(r"\d+", n)]))
    print("Discovered Gemini models:", names[:6])
    return names


def call_gemini_discovered(prompt):
    last = None
    for m in discover_gemini_models()[:3]:
        try:
            return call_gemini(prompt, m)
        except Exception as ex:
            last = ex
    raise RuntimeError(f"no usable Gemini model found ({last})")


def analyse(items, notes, snapshot=None, horizon="today's close"):
    """Try Gemini (named models, then auto-discovered ones), then Claude if its key is set."""
    prompt = build_prompt(items, snapshot, horizon)
    attempts = []
    if GEMINI_KEY:
        for m in dict.fromkeys([GEMINI_MODEL, "gemini-3.5-flash-lite"]):
            attempts.append((f"Gemini {m}", lambda m=m: call_gemini(prompt, m)))
        attempts.append(("Gemini (auto-discovered model)", lambda: call_gemini_discovered(prompt)))
    if API_KEY:
        attempts.append((f"Claude {MODEL}", lambda: call_claude(prompt, MODEL)))
    errors = []
    for name, fn in attempts:
        try:
            return fn()
        except Exception as ex:
            msg = re.sub(r"\s+", " ", str(ex))[:160]
            errors.append(f"{name}: {msg}")
            print(f"AI analysis failed with {errors[-1]}")
    notes.append("AI analysis failed (" + " | ".join(errors)[:450] + "), so headlines are shown without ratings.")
    return None


COLORS = {"Positive": "#1a7f37", "Negative": "#cf222e", "Neutral": "#6e7781", "Unrated": "#6e7781"}
MOOD_COLORS = {"Bullish": "#1a7f37", "Bearish": "#cf222e", "Neutral": "#6e7781", "Mixed": "#9a6700"}
IMPACT_ORDER = {"High": 0, "Medium": 1, "Low": 2}
e = html.escape


def build_outputs(items, analysis, now, session, notes, snapshot_html="", snapshot_text="",
                  track_html="", track_text="", picks_html="", picks_text=""):
    by_id = {}
    if analysis:
        for a in analysis.get("items", []):
            if isinstance(a, dict) and a.get("id") is not None:
                by_id[str(a["id"])] = a

    groups = {"Positive": [], "Negative": [], "Neutral": [], "Unrated": []}
    skipped = 0
    for i, it in enumerate(items, start=1):
        a = by_id.get(str(i), {})
        sent = str(a.get("sentiment", "")).capitalize()
        if not analysis:
            sent = "Unrated"
        elif sent.startswith("Not") or str(a.get("scope", "")).lower() == "none":
            skipped += 1
            continue
        elif sent not in ("Positive", "Negative", "Neutral"):
            sent = "Neutral"
        groups[sent].append((it, a))
    if skipped:
        notes.append(f"{skipped} headlines were skipped as not market-related.")
    for g in groups.values():
        g.sort(key=lambda x: IMPACT_ORDER.get(str(x[1].get("impact")), 3))

    mood = (analysis or {}).get("market_mood", "")
    counts = {k: len(v) for k, v in groups.items() if v}
    count_text = " | ".join(f"{k} {v}" for k, v in counts.items())
    date_text = now.strftime("%d %b %Y, %I:%M %p IST")
    watch = (analysis or {}).get("watch", [])

    # ---------- HTML ----------
    h = [f"<div style='font-family:Arial,sans-serif;max-width:700px;margin:auto;color:#222'>",
         f"<h2 style='margin-bottom:2px'>Market News {e(session)}</h2>",
         f"<div style='color:#666;font-size:13px'>{e(date_text)} &middot; last {HOURS} hours "
         f"&middot; {len(items)} headlines</div>"]
    if snapshot_html:
        h.append(snapshot_html)
    if analysis:
        mc = MOOD_COLORS.get(mood, "#6e7781")
        h.append(f"<p><span style='background:{mc};color:#fff;padding:4px 10px;border-radius:12px;"
                 f"font-weight:bold'>Market mood: {e(str(mood))}</span> "
                 f"<span style='color:#666;font-size:13px'>{e(count_text)}</span></p>")
        h.append(f"<p>{e(str(analysis.get('summary', '')))}</p>")
        if analysis.get("market_outlook"):
            h.append("<p style='background:#f6f8fa;border-left:4px solid " + mc + ";padding:8px 10px;color:#222'>"
                     f"<b>Nifty/Sensex outlook:</b> {e(str(analysis['market_outlook']))}</p>")
        h.append("<h3 style='margin-bottom:4px'>My opinion</h3>"
                 f"<p style='margin-top:0'>{e(str(analysis.get('opinion', '')))}</p>")
        if watch:
            h.append("<h3 style='margin-bottom:4px'>Watch</h3><ul style='margin-top:0'>"
                     + "".join(f"<li>{e(str(w))}</li>" for w in watch) + "</ul>")
    if picks_html:
        h.append(picks_html)
    for sent in ("Positive", "Negative", "Neutral", "Unrated"):
        if not groups[sent]:
            continue
        label = "Headlines" if sent == "Unrated" else f"{sent} news"
        h.append(f"<h3 style='color:{COLORS[sent]};border-bottom:2px solid {COLORS[sent]};"
                 f"padding-bottom:3px'>{label} ({len(groups[sent])})</h3>")
        for it, a in groups[sent]:
            meta = f"{it['time']:%d %b %H:%M} &middot; {e(it['source'])}"
            if a.get("scope"):
                meta += f" &middot; Affects: {e(str(a['scope']))}"
            if a.get("impact"):
                meta += f" &middot; Impact: {e(str(a['impact']))}"
            extra = ""
            if a.get("stocks"):
                extra += f"<br><b>Affects:</b> {e(', '.join(map(str, a['stocks'])))}"
            if a.get("reason"):
                extra += f"<br><i>{e(str(a['reason']))}</i>"
            h.append(f"<p style='margin:8px 0'><a href='{e(it['link'])}' style='color:#0969da;"
                     f"text-decoration:none;font-weight:bold'>{e(it['title'])}</a><br>"
                     f"<span style='color:#666;font-size:12px'>{meta}</span>"
                     f"<span style='font-size:13px'>{extra}</span></p>")
    if track_html:
        h.append(track_html)
    for n in notes:
        h.append(f"<p style='color:#9a6700;font-size:13px'>Note: {e(n)}</p>")
    h.append("<p style='color:#888;font-size:12px'>Automated AI analysis of headlines only. "
             "It can be wrong and is not investment advice.</p></div>")

    # ---------- Plain text ----------
    t = [f"MARKET NEWS {session.upper()} - {date_text}",
         f"Last {HOURS} hours, {len(items)} headlines"]
    if snapshot_text:
        t += ["", snapshot_text, ""]
    if analysis:
        t += [f"Market mood: {mood} ({count_text})", "", str(analysis.get("summary", "")), "",
              "NIFTY/SENSEX OUTLOOK", str(analysis.get("market_outlook", "")), "",
              "MY OPINION", str(analysis.get("opinion", ""))]
        if watch:
            t += ["", "WATCH"] + [f"- {w}" for w in watch]
    if picks_text:
        t += ["", picks_text]
    for sent in ("Positive", "Negative", "Neutral", "Unrated"):
        if not groups[sent]:
            continue
        t += ["", f"== {'HEADLINES' if sent == 'Unrated' else sent.upper() + ' NEWS'} "
                  f"({len(groups[sent])}) =="]
        for it, a in groups[sent]:
            t.append(f"* {it['title']} [{it['time']:%d %b %H:%M}]")
            if a.get("reason"):
                t.append(f" {a['reason']}")
            t.append(f" {it['link']}")
    if track_text:
        t += ["", track_text]
    t += [""] + [f"Note: {n}" for n in notes]
    t.append("Automated AI analysis of headlines only. Not investment advice.")

    subject = f"Market News {session} - {mood or 'Headlines'} - {now:%d %b}"
    return subject, "\n".join(h), "\n".join(t)


# ---------------- Live market data ----------------
SNAPSHOT_SYMBOLS = [
    ("Nifty 50", "^NSEI"), ("Sensex", "^BSESN"), ("Bank Nifty", "^NSEBANK"), ("Nifty IT", "^CNXIT"),
    ("India VIX", "^INDIAVIX"), ("USD/INR", "INR=X"), ("Brent crude", "BZ=F"), ("Gold", "GC=F"),
    ("S&P 500", "^GSPC"), ("Nasdaq", "^IXIC"), ("Nikkei 225", "^N225"), ("Hang Seng", "^HSI"),
]


def yahoo_daily(symbol, range_="5d"):
    """Daily closes as [(date, close), ...] from Yahoo Finance's public chart endpoint."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?range={range_}&interval=1d"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    closes = result["indicators"]["quote"][0]["close"]
    return [(datetime.fromtimestamp(t, IST).date(), c) for t, c in zip(result["timestamp"], closes)
            if c is not None]


def fetch_market_snapshot():
    snap = []
    for name, symbol in SNAPSHOT_SYMBOLS:
        try:
            series = yahoo_daily(symbol)
            if len(series) >= 2:
                last, prev = series[-1][1], series[-2][1]
                snap.append({"name": name, "last": last, "chg": (last / prev - 1) * 100})
        except Exception as ex:
            print(f"Market data failed for {name}: {str(ex)[:80]}")
        time.sleep(0.4)
    print(f"Market snapshot: {len(snap)} of {len(SNAPSHOT_SYMBOLS)} symbols")
    return snap


def snapshot_to_text(snap):
    return "\n".join(f"{r['name']}: {r['last']:,.2f} ({r['chg']:+.2f}%)" for r in snap)


def snapshot_html(snap):
    if not snap:
        return ""
    cells = []
    for r in snap:
        color = "#1a7f37" if r["chg"] >= 0 else "#cf222e"
        cells.append(f"<td style='padding:4px 8px;border:1px solid #ddd;font-size:12px'>{e(r['name'])}"
                     f"<br><b>{r['last']:,.1f}</b> <span style='color:{color}'>{r['chg']:+.2f}%</span></td>")
    rows = "".join("<tr>" + "".join(cells[i:i + 3]) + "</tr>" for i in range(0, len(cells), 3))
    return ("<div style='font-size:13px;color:#666;margin-top:10px'>Market snapshot "
            "(latest vs previous close)</div>"
            f"<table style='border-collapse:collapse;margin:4px 0'>{rows}</table>")


def snapshot_text(snap):
    return ("MARKET SNAPSHOT (latest vs previous close)\n" + snapshot_to_text(snap)) if snap else ""


# ---------------- Prediction tracking ----------------
PRED_FIELDS = ["run_date", "session", "mood", "nifty_bias", "confidence", "outlook",
               "outcome_date", "baseline", "outcome_close", "change_pct", "result"]


def load_history():
    if not os.path.exists(PRED_FILE):
        return []
    with open(PRED_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_history(rows):
    with open(PRED_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PRED_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in PRED_FIELDS})


def classify(change):
    return "Up" if change > BAND else "Down" if change < -BAND else "Flat"


def evaluate_pending(rows, now):
    """Compare earlier predictions with what the Nifty actually did once the closing bar is final."""
    pending = [r for r in rows if r.get("result") in ("", "Pending", None)]
    if not pending:
        return
    try:
        closes = dict(yahoo_daily("^NSEI", "3mo"))
    except Exception as ex:
        print(f"Could not load Nifty history for tracking: {str(ex)[:80]}")
        return
    dates = sorted(closes)
    today, market_closed = now.date(), now.hour >= 16
    for r in pending:
        run_date = date.fromisoformat(r["run_date"])
        if r["session"] == "Morning Brief": # call is for the same day's close vs previous close
            prior = [d for d in dates if d < run_date]
            out_date = run_date
            if out_date not in closes or not prior:
                base = None
            else:
                base = closes[prior[-1]]
        else: # call is for the next trading day's close
            later = [d for d in dates if d > run_date]
            if run_date not in closes or not later:
                base = None
            else:
                out_date, base = later[0], closes[run_date]
        if base is None:
            if (today - run_date).days > 7:
                r["result"] = "No data"
            continue
        if out_date > today or (out_date == today and not market_closed):
            continue # closing bar not final yet
        change = (closes[out_date] / base - 1) * 100
        r.update({"outcome_date": out_date.isoformat(), "baseline": round(base, 1),
                  "outcome_close": round(closes[out_date], 1), "change_pct": round(change, 2),
                  "result": "Hit" if classify(change) == r.get("nifty_bias") else "Miss"})


def log_prediction(rows, analysis, now, session):
    if now.weekday() >= 5: # markets are closed, nothing to score
        return
    bias = str(analysis.get("nifty_bias", "")).strip().capitalize()
    if bias not in ("Up", "Down", "Flat"):
        return
    run_date = now.date().isoformat()
    rows[:] = [r for r in rows if not (r.get("run_date") == run_date and r.get("session") == session
                                       and r.get("result") in ("", "Pending"))]
    rows.append({"run_date": run_date, "session": session, "mood": analysis.get("market_mood", ""),
                 "nifty_bias": bias, "confidence": analysis.get("confidence", ""),
                 "outlook": str(analysis.get("market_outlook", ""))[:200], "result": "Pending"})


def accuracy_summary(rows):
    done = [r for r in rows if r.get("result") in ("Hit", "Miss")]
    waiting = [r for r in rows if r.get("result") in ("", "Pending", None)]
    if not done:
        text = (f"PREDICTION TRACKER: started, {len(waiting)} prediction(s) waiting for market closes. "
                "Results will appear here after the next trading day.")
        return f"<p style='color:#666;font-size:13px'>{e(text)}</p>", text
    hits = sum(r["result"] == "Hit" for r in done)
    directional = [r for r in done if r.get("nifty_bias") in ("Up", "Down")]
    d_hits = sum(r["result"] == "Hit" for r in directional)
    last = done[-10:]
    l_hits = sum(r["result"] == "Hit" for r in last)
    head = f"PREDICTION TRACKER: {hits}/{len(done)} correct ({100 * hits // len(done)}%)"
    if directional:
        head += f"; Up/Down calls {d_hits}/{len(directional)} ({100 * d_hits // len(directional)}%)"
    head += f"; last {len(last)}: {l_hits}/{len(last)}."
    lines = [f"{r['outcome_date']} {str(r['session']).split()[0]}: said {r['nifty_bias']}, "
             f"Nifty {float(r['change_pct']):+.2f}% -> {r['result']}" for r in done[-5:][::-1]]
    note = "Small samples mean little; pure chance would be right roughly 35-50% of the time."
    text = head + "\n" + "\n".join(f" {ln}" for ln in lines) + "\n" + note
    html_out = (f"<h3 style='margin-bottom:4px'>Prediction tracker</h3><p style='margin:2px 0'>"
                f"<b>{e(head.split(': ', 1)[1])}</b></p><ul style='margin:2px 0;font-size:13px'>"
                + "".join(f"<li>{e(ln)}</li>" for ln in lines)
                + f"</ul><p style='color:#666;font-size:12px'>{e(note)}</p>")
    return html_out, text


# ---------------- News on your picks ----------------
SYMBOL_STOPWORDS = {"THE", "AND", "FOR", "ALL", "NEW", "IPO", "FII", "DII", "RBI", "SEBI", "GDP", "ETF",
                    "SIP", "NSE", "BSE", "USD", "INR", "CEO", "CFO", "NPA", "EPS", "FY", "Q1", "Q2", "Q3", "Q4"}


def clean_name(name):
    n = re.sub(r"\(.*?\)", " ", str(name).lower())
    n = re.sub(r"[^a-z0-9& ]+", " ", n)
    n = re.sub(r"\b(limited|ltd|corporation|corp|company|co|inc|the)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def load_watchlist():
    """Shortlisted stocks from the screeners (watchlist_*.csv) plus symbols in my_stocks.txt."""
    rows = []
    for path in glob.glob("watchlist_*.csv"):
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if r.get("Shortlisted") == "Yes":
                        r["List"] = "Shortlist"
                        rows.append(r)
        except Exception as ex:
            print(f"Could not read {path}: {ex}")
    if os.path.exists("my_stocks.txt"):
        with open("my_stocks.txt", encoding="utf-8") as f:
            for line in f:
                sym = line.split("#")[0].strip().upper()
                if sym:
                    rows.append({"Symbol": sym, "Company": sym, "List": "My stocks"})
    return rows


def link_news(items, watch):
    """{headline number: [watchlist rows mentioned in that headline]}"""
    compiled = []
    for w in watch:
        name, sym = clean_name(w.get("Company", "")), str(w.get("Symbol", "")).strip()
        pats = []
        if len(name) >= 3 and (" " in name or len(name) >= 6):
            pats.append((re.compile(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])"), True))
        if len(sym) >= 3 and sym not in SYMBOL_STOPWORDS:
            pats.append((re.compile(r"(?<![A-Za-z0-9])" + re.escape(sym) + r"(?![A-Za-z0-9])"), False))
        if pats:
            compiled.append((w, pats))
    links = {}
    for i, it in enumerate(items, start=1):
        title = it["title"]
        low = re.sub(r"[^a-z0-9& ]+", " ", title.lower())
        for w, pats in compiled:
            if any(p.search(low if lower else title) for p, lower in pats):
                links.setdefault(i, []).append(w)
    return links


def picks_section(items, analysis, links):
    """(html, text) block listing headlines that mention your shortlisted or own stocks."""
    if not links:
        return "", ""
    by_id = {str(a.get("id")): a for a in (analysis or {}).get("items", []) if isinstance(a, dict)}
    h = ["<h3 style='color:#8250df;border-bottom:2px solid #8250df;padding-bottom:3px'>"
         f"Your picks in the news ({len(links)})</h3>"]
    t = [f"== YOUR PICKS IN THE NEWS ({len(links)}) =="]
    for i, ws in sorted(links.items()):
        it, a = items[i - 1], by_id.get(str(i), {})
        sent = str(a.get("sentiment", "")).capitalize() if analysis else ""
        tags = []
        for w in ws:
            tag = w.get("Company") or w.get("Symbol")
            if w.get("List") == "Shortlist":
                tag += f" (score {w.get('Score %')}%, {w.get('Valuation')}"
                tag += f", {w.get('Red Flags')} red flags)" if str(w.get("Red Flags", "0")) != "0" else ")"
            else:
                tag += " (my stock)"
            tags.append(tag)
        color = COLORS.get(sent, "#6e7781")
        h.append(f"<p style='margin:8px 0'><b>{e('; '.join(tags))}</b><br>"
                 f"<a href='{e(it['link'])}' style='color:#0969da;text-decoration:none'>{e(it['title'])}</a><br>"
                 f"<span style='font-size:13px;color:{color}'>{e(sent)}"
                 f"{' - ' + e(str(a.get('reason'))) if a.get('reason') else ''}</span></p>")
        t.append(f"* {'; '.join(tags)}\n {it['title']}\n {sent}{' - ' + str(a.get('reason')) if a.get('reason') else ''}\n {it['link']}")
    return "".join(h), "\n".join(t)


def main():
    now = datetime.now(IST)
    session = "Morning Brief" if now.hour < 12 else "Afternoon Update"
    horizon = "today's close" if now.hour < 12 else "the next trading day's close"
    notes = []

    items = collect_news(now, notes)
    snapshot = fetch_market_snapshot()
    if not snapshot:
        notes.append("Live market data was unavailable this time (Yahoo Finance may be blocking).")
    analysis = None
    if not items:
        notes.append(f"No Moneycontrol news found in the last {HOURS} hours "
                     "(feeds may be down or blocked).")
    elif not (GEMINI_KEY or API_KEY):
        notes.append("GEMINI_API_KEY is not set, so headlines are shown without analysis.")
    else:
        analysis = analyse(items, notes, snapshot, horizon)

    history = load_history()
    try:
        evaluate_pending(history, now)
        if analysis:
            log_prediction(history, analysis, now, session)
        save_history(history)
    except Exception as ex:
        print(f"Tracking error: {ex}")
    track_html, track_text = accuracy_summary(history)
    picks_html, picks_text = picks_section(items, analysis, link_news(items, load_watchlist()))

    subject, html_body, text_body = build_outputs(
        items, analysis, now, session, notes,
        snapshot_html(snapshot), snapshot_text(snapshot), track_html, track_text,
        picks_html, picks_text)
    with open("news_summary.html", "w", encoding="utf-8") as f:
        f.write(html_body)
    with open("news_summary.txt", "w", encoding="utf-8") as f:
        f.write(text_body)
    with open("subject.txt", "w", encoding="utf-8") as f:
        f.write(subject)
    print(text_body)


if __name__ == "__main__":
    main()
