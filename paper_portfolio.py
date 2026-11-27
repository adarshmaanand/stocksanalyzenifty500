import csv
import glob
import html
import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests

# ---------------- RULES (edit freely) ----------------
CAPITAL_PER_TRADE = 10000 # virtual rupees per position
MAX_NEW_PER_RUN = 5 # new positions opened per weekly run
MAX_OPEN = 30 # maximum open positions at once
STOP_LOSS = -15.0 # close at -15% from entry
TARGET = 30.0 # close at +30% from entry
MAX_DAYS = 180 # close after 180 days (judged against the Nifty)
COOLDOWN_DAYS = 30 # do not re-buy a stock closed in the last 30 days
MIN_SCORE_TO_HOLD = 60 # close if the screener score falls below this
MAX_FLAGS_TO_HOLD = 3 # close if red flags reach this number
WATCHLIST_MAX_AGE_DAYS = 14 # ignore watchlist data older than this
# -----------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))
HEADERS = {"User-Agent": "Mozilla/5.0 (personal paper portfolio)"}
TRADES_FILE = "paper_trades.csv"
FIELDS = ["id", "symbol", "company", "source", "entry_date", "entry_price", "nifty_entry", "score",
          "valuation", "fair_value", "status", "exit_date", "exit_price", "nifty_exit",
          "exit_reason", "return_pct", "nifty_return_pct", "alpha_pct"]
e = html.escape


def num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def yahoo_daily(symbol, range_="1mo"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?range={range_}&interval=1d"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    closes = result["indicators"]["quote"][0]["close"]
    return [(datetime.fromtimestamp(t, IST).date(), c) for t, c in zip(result["timestamp"], closes)
            if c is not None]


def latest_close(symbol):
    return yahoo_daily(symbol)[-1][1]


def load_trades():
    if not os.path.exists(TRADES_FILE):
        return []
    with open(TRADES_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_trades(trades):
    with open(TRADES_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for t in trades:
            w.writerow({k: t.get(k, "") for k in FIELDS})


def load_watch(today):
    """{symbol: row} from every watchlist_*.csv that is recent enough."""
    watch = {}
    for path in glob.glob("watchlist_*.csv"):
        source = os.path.basename(path)[len("watchlist_"):-len(".csv")]
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    age = (today - date.fromisoformat(r.get("Updated", ""))).days
                except ValueError:
                    continue
                if age <= WATCHLIST_MAX_AGE_DAYS:
                    r["source"] = source
                    watch[r["Symbol"]] = r
    return watch


def exit_reason(t, ret, days, w):
    if ret <= STOP_LOSS:
        return f"Stop-loss ({STOP_LOSS:.0f}%)"
    if ret >= TARGET:
        return f"Target reached (+{TARGET:.0f}%)"
    if days >= MAX_DAYS:
        return f"Time limit ({MAX_DAYS} days)"
    if w and w.get("Current Price") not in ("", None): # only trust rows that had real data
        if w.get("Valuation") == "Expensive":
            return "Now expensive versus fair value"
        if num(w.get("Red Flags"), 0) >= MAX_FLAGS_TO_HOLD:
            return "New red flags"
        if num(w.get("Score %"), 100) < MIN_SCORE_TO_HOLD:
            return f"Score fell below {MIN_SCORE_TO_HOLD}%"
    return None


def update_positions(trades, watch, nifty_now, today, notes):
    closed_now = []
    for t in trades:
        if t["status"] != "Open":
            continue
        try:
            price = latest_close(t["symbol"] + ".NS")
        except Exception as ex:
            notes.append(f"No price for {t['symbol']} this week ({str(ex)[:60]}).")
            continue
        entry = float(t["entry_price"])
        ret = (price / entry - 1) * 100
        nret = (nifty_now / float(t["nifty_entry"]) - 1) * 100
        days = (today - date.fromisoformat(t["entry_date"])).days
        t.update({"_price": price, "_ret": ret, "_nret": nret, "_days": days})
        reason = exit_reason(t, ret, days, watch.get(t["symbol"]))
        if reason:
            t.update({"status": "Closed", "exit_date": today.isoformat(), "exit_price": round(price, 2),
                      "nifty_exit": round(nifty_now, 1), "exit_reason": reason,
                      "return_pct": round(ret, 2), "nifty_return_pct": round(nret, 2),
                      "alpha_pct": round(ret - nret, 2)})
            closed_now.append(t)
        time.sleep(0.3)
    return closed_now


def open_new_positions(trades, watch, nifty_now, today, notes):
    held = {t["symbol"] for t in trades if t["status"] == "Open"}
    cooling = {t["symbol"] for t in trades if t["status"] == "Closed" and t.get("exit_date")
               and (today - date.fromisoformat(t["exit_date"])).days < COOLDOWN_DAYS}
    candidates = [r for r in watch.values() if r.get("Clean") == "Yes"
                  and r["Symbol"] not in held and r["Symbol"] not in cooling]
    candidates.sort(key=lambda r: (-num(r.get("Score %"), 0), -num(r.get("Margin of Safety %"), -999)))
    slots = max(0, min(MAX_NEW_PER_RUN, MAX_OPEN - len(held)))
    next_id = max([int(t["id"]) for t in trades if str(t.get("id", "")).isdigit()] + [0]) + 1
    opened = []
    for r in candidates:
        if len(opened) >= slots:
            break
        try:
            price = latest_close(r["Symbol"] + ".NS")
        except Exception as ex:
            notes.append(f"Skipped {r['Symbol']}: no price ({str(ex)[:60]}).")
            continue
        trade = {"id": next_id, "symbol": r["Symbol"], "company": r.get("Company", r["Symbol"]),
                 "source": r["source"], "entry_date": today.isoformat(), "entry_price": round(price, 2),
                 "nifty_entry": round(nifty_now, 1), "score": r.get("Score %"),
                 "valuation": r.get("Valuation"), "fair_value": r.get("Fair Value (Graham)"),
                 "status": "Open"}
        next_id += 1
        trades.append(trade)
        opened.append(trade)
        time.sleep(0.3)
    return opened


def avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def fmt(v, suffix="%"):
    return "n/a" if v is None else f"{v:+.1f}{suffix}"


def build_report(trades, opened, closed_now, notes, today):
    open_pos = [t for t in trades if t["status"] == "Open" and "_ret" in t]
    closed = [t for t in trades if t["status"] == "Closed"]
    c_ret = [num(t["return_pct"]) for t in closed]
    c_alpha = [num(t["alpha_pct"]) for t in closed]
    o_ret = [t["_ret"] for t in open_pos]
    o_alpha = [t["_ret"] - t["_nret"] for t in open_pos]
    all_ret = [r for r in c_ret + o_ret if r is not None]
    all_nifty = [num(t["nifty_return_pct"]) for t in closed] + [t["_nret"] for t in open_pos]
    all_nifty = [r for r in all_nifty if r is not None]
    invested = CAPITAL_PER_TRADE * len(all_ret)
    pnl = sum(CAPITAL_PER_TRADE * r / 100 for r in all_ret)
    wins = sum(1 for r in c_ret if r is not None and r > 0)

    subject = (f"Paper Portfolio - {len(open_pos)} open, avg {fmt(avg(all_ret))} vs Nifty "
               f"{fmt(avg(all_nifty))}" if all_ret else "Paper Portfolio - started")

    rules = (f"Rules: Rs {CAPITAL_PER_TRADE:,} per stock, up to {MAX_NEW_PER_RUN} new buys a week from the "
             f"clean shortlist. Sell at {STOP_LOSS:.0f}% / +{TARGET:.0f}% / {MAX_DAYS} days, or when the "
             "stock turns expensive or picks up red flags. Prices are the last close.")
    caveat = ("A forward test with virtual money. It ignores brokerage, taxes and slippage, and needs "
              "months of data before it means much. Not investment advice.")

    # ---- text ----
    t_lines = [f"PAPER PORTFOLIO REPORT - {today:%d %b %Y}", rules, ""]
    if all_ret:
        t_lines += [f"All positions ({len(all_ret)}): average {fmt(avg(all_ret))} vs Nifty {fmt(avg(all_nifty))} "
                    f"over the same periods (alpha {fmt(avg(all_ret) - avg(all_nifty))})",
                    f"Virtual P&L: Rs {pnl:+,.0f} on Rs {invested:,.0f} invested",
                    f"Open: {len(open_pos)} (avg {fmt(avg(o_ret))}, vs Nifty {fmt(avg(o_alpha))}) | "
                    f"Closed: {len(closed)}" + (f" (win rate {100 * wins // len(closed)}%, avg {fmt(avg(c_ret))}, "
                                                f"alpha {fmt(avg(c_alpha))})" if closed else "")]
    else:
        t_lines.append("No positions yet. Positions open when the screeners produce a clean shortlist.")
    if opened:
        t_lines += ["", f"NEW THIS WEEK ({len(opened)})"] + [
            f"* {t['company']} ({t['symbol']}) at {float(t['entry_price']):,.1f} | score {t['score']}% | "
            f"{t['valuation']} | fair value {t['fair_value']}" for t in opened]
    if closed_now:
        t_lines += ["", f"CLOSED THIS WEEK ({len(closed_now)})"] + [
            f"* {t['company']} ({t['symbol']}): {num(t['return_pct']):+.1f}% (Nifty {num(t['nifty_return_pct']):+.1f}%) "
            f"- {t['exit_reason']}" for t in closed_now]
    if open_pos:
        t_lines += ["", "OPEN POSITIONS (best first)"]
        for t in sorted(open_pos, key=lambda x: -x["_ret"]):
            t_lines.append(f"* {t['symbol']}: {t['_ret']:+.1f}% vs Nifty {t['_nret']:+.1f}% "
                           f"({t['_days']} days, entry {float(t['entry_price']):,.1f}, now {t['_price']:,.1f})")
    t_lines += [""] + [f"Note: {n}" for n in notes[:8]] + [caveat]

    # ---- html ----
    h = ["<div style='font-family:Arial,sans-serif;max-width:700px;margin:auto;color:#222'>",
         f"<h2 style='margin-bottom:2px'>Paper Portfolio</h2><div style='color:#666;font-size:13px'>"
         f"{today:%d %b %Y}</div><p style='font-size:13px;color:#555'>{e(rules)}</p>"]
    if all_ret:
        color = "#1a7f37" if avg(all_ret) >= avg(all_nifty) else "#cf222e"
        h.append(f"<p style='font-size:16px'><b>{len(all_ret)} positions: average {fmt(avg(all_ret))}</b> "
                 f"vs Nifty {fmt(avg(all_nifty))} <span style='color:{color}'>(alpha "
                 f"{fmt(avg(all_ret) - avg(all_nifty))})</span><br><span style='font-size:13px;color:#555'>"
                 f"Virtual P&amp;L Rs {pnl:+,.0f} on Rs {invested:,.0f}. Open {len(open_pos)}"
                 + (f" &middot; Closed {len(closed)}, win rate {100 * wins // len(closed)}%, "
                    f"alpha {fmt(avg(c_alpha))}" if closed else "") + "</span></p>")
    else:
        h.append("<p>No positions yet. Positions open when the screeners produce a clean shortlist.</p>")

    def section(title, color, lines):
        if lines:
            h.append(f"<h3 style='color:{color};border-bottom:2px solid {color};padding-bottom:3px'>{title}</h3>"
                     + "".join(f"<p style='margin:6px 0;font-size:14px'>{ln}</p>" for ln in lines))

    section(f"New this week ({len(opened)})", "#0969da", [
        f"<b>{e(t['company'])}</b> ({e(t['symbol'])}) at {float(t['entry_price']):,.1f}<br>"
        f"<span style='font-size:12px;color:#666'>score {e(str(t['score']))}% &middot; {e(str(t['valuation']))}"
        f" &middot; fair value {e(str(t['fair_value']))}</span>" for t in opened])
    section(f"Closed this week ({len(closed_now)})", "#9a6700", [
        f"<b>{e(t['company'])}</b> ({e(t['symbol'])}): {num(t['return_pct']):+.1f}% vs Nifty "
        f"{num(t['nifty_return_pct']):+.1f}%<br><span style='font-size:12px;color:#666'>{e(t['exit_reason'])}</span>"
        for t in closed_now])
    if open_pos:
        rows = "".join(
            f"<tr><td style='padding:3px 6px'>{e(t['symbol'])}</td><td style='padding:3px 6px'>{t['_days']}d</td>"
            f"<td style='padding:3px 6px;color:{'#1a7f37' if t['_ret'] >= 0 else '#cf222e'}'>{t['_ret']:+.1f}%</td>"
            f"<td style='padding:3px 6px'>{t['_nret']:+.1f}%</td></tr>"
            for t in sorted(open_pos, key=lambda x: -x["_ret"]))
        h.append("<h3 style='border-bottom:2px solid #6e7781;padding-bottom:3px'>Open positions</h3>"
                 "<table style='border-collapse:collapse;font-size:13px'><tr style='color:#666'>"
                 "<td style='padding:3px 6px'>Stock</td><td style='padding:3px 6px'>Held</td>"
                 "<td style='padding:3px 6px'>Return</td><td style='padding:3px 6px'>Nifty</td></tr>"
                 f"{rows}</table>")
    h += [f"<p style='color:#9a6700;font-size:13px'>Note: {e(n)}</p>" for n in notes[:8]]
    h.append(f"<p style='color:#888;font-size:12px'>{e(caveat)}</p></div>")
    return subject, "".join(h), "\n".join(t_lines)


def main():
    now = datetime.now(IST)
    today = now.date()
    notes = []
    trades = load_trades()
    watch = load_watch(today)
    if not watch:
        notes.append("No recent watchlist_*.csv found. Run the Nifty 500 / Midcap screeners first.")

    try:
        nifty_now = latest_close("^NSEI")
    except Exception as ex:
        raise SystemExit(f"Could not load Nifty data ({ex}); nothing was changed.")

    closed_now = update_positions(trades, watch, nifty_now, today, notes)
    opened = open_new_positions(trades, watch, nifty_now, today, notes) if watch else []
    # newly opened positions have no change yet, so show them with a zero return
    for t in opened:
        t.update({"_price": float(t["entry_price"]), "_ret": 0.0, "_nret": 0.0, "_days": 0})

    save_trades(trades)
    subject, html_body, text_body = build_report(trades, opened, closed_now, notes, today)
    for name, content in (("paper_report.html", html_body), ("paper_report.txt", text_body),
                          ("paper_subject.txt", subject)):
        with open(name, "w", encoding="utf-8") as f:
            f.write(content)
    print(text_body)


if __name__ == "__main__":
    main()
