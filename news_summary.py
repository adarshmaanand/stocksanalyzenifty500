"""
Moneycontrol news digest: last 24 hours of headlines, rated Positive / Negative /
Neutral by Claude, with an overall market opinion. Writes an email-ready summary.

Needs the GEMINI_API_KEY environment variable (a GitHub secret in the workflow).
(ANTHROPIC_API_KEY also works as an optional backup.)
Only the 'requests' package is required.

Outputs:
  news_summary.html (email body, formatted)
  news_summary.txt (plain-text email body)
  subject.txt (email subject line)
"""

import email.utils
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

# ---------------- CONFIG ----------------
IST = timezone(timedelta(hours=5, minutes=30))
HOURS = int(os.environ.get("NEWS_HOURS", "24")) # only news from the last N hours
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "50")) # cap headlines sent for analysis
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "") # optional backup (Claude)
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
HEADERS = {"User-Agent": "Mozilla/5.0 (personal news digest)"}

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


def build_prompt(items):
    lines = [f"{i} | {it['time']:%d %b %H:%M} | {it['title']} | {it['summary']}"
             for i, it in enumerate(items, start=1)]
    prompt = (
        "Below are Moneycontrol headlines from the last 24 hours (IST). For each one, judge its likely "
        "effect on Indian stock prices, then give an overall view of the market.\n\n"
        "Return ONLY valid JSON (no markdown) in this shape:\n"
        '{"market_mood": "Bullish|Bearish|Neutral|Mixed",\n'
        ' "market_outlook": "1-2 sentences: likely bias for Nifty/Sensex today (up, down or flat) and your confidence",\n'
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


def analyse(items, notes):
    """Try Gemini first (then a lighter Gemini model), then Claude if its key is set."""
    prompt = build_prompt(items)
    attempts = []
    if GEMINI_KEY:
        for m in dict.fromkeys([GEMINI_MODEL, "gemini-2.5-flash-lite"]):
            attempts.append((f"Gemini {m}", lambda m=m: call_gemini(prompt, m)))
    if API_KEY:
        attempts.append((f"Claude {MODEL}", lambda: call_claude(prompt, MODEL)))
    last = ""
    for name, fn in attempts:
        try:
            return fn()
        except Exception as ex:
            last = f"{name}: {str(ex)[:200]}"
            print(f"AI analysis failed with {last}")
    notes.append(f"AI analysis failed ({last}), so headlines are shown without ratings.")
    return None


COLORS = {"Positive": "#1a7f37", "Negative": "#cf222e", "Neutral": "#6e7781", "Unrated": "#6e7781"}
MOOD_COLORS = {"Bullish": "#1a7f37", "Bearish": "#cf222e", "Neutral": "#6e7781", "Mixed": "#9a6700"}
IMPACT_ORDER = {"High": 0, "Medium": 1, "Low": 2}
e = html.escape


def build_outputs(items, analysis, now, session, notes):
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
    for n in notes:
        h.append(f"<p style='color:#9a6700;font-size:13px'>Note: {e(n)}</p>")
    h.append("<p style='color:#888;font-size:12px'>Automated AI analysis of headlines only. "
             "It can be wrong and is not investment advice.</p></div>")

    # ---------- Plain text ----------
    t = [f"MARKET NEWS {session.upper()} - {date_text}",
         f"Last {HOURS} hours, {len(items)} headlines"]
    if analysis:
        t += [f"Market mood: {mood} ({count_text})", "", str(analysis.get("summary", "")), "",
              "NIFTY/SENSEX OUTLOOK", str(analysis.get("market_outlook", "")), "",
              "MY OPINION", str(analysis.get("opinion", ""))]
        if watch:
            t += ["", "WATCH"] + [f"- {w}" for w in watch]
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
    t += [""] + [f"Note: {n}" for n in notes]
    t.append("Automated AI analysis of headlines only. Not investment advice.")

    subject = f"Market News {session} - {mood or 'Headlines'} - {now:%d %b}"
    return subject, "\n".join(h), "\n".join(t)


def main():
    now = datetime.now(IST)
    session = "Morning Brief" if now.hour < 12 else "Afternoon Update"
    notes = []

    items = collect_news(now, notes)
    analysis = None
    if not items:
        notes.append(f"No Moneycontrol news found in the last {HOURS} hours "
                     "(feeds may be down or blocked).")
    elif not (GEMINI_KEY or API_KEY):
        notes.append("GEMINI_API_KEY is not set, so headlines are shown without analysis.")
    else:
        analysis = analyse(items, notes)

    subject, html_body, text_body = build_outputs(items, analysis, now, session, notes)
    with open("news_summary.html", "w", encoding="utf-8") as f:
        f.write(html_body)
    with open("news_summary.txt", "w", encoding="utf-8") as f:
        f.write(text_body)
    with open("subject.txt", "w", encoding="utf-8") as f:
        f.write(subject)
    print(text_body)


if __name__ == "__main__":
    main()
