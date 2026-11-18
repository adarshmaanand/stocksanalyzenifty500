import io
import math
import os
import re
import time
from urllib.parse import quote

import pandas as pd
import requests
from bs4 import BeautifulSoup

# ---------------- CONFIG ----------------
NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
LOCAL_LIST = "nifty500.csv"
OUTPUT_FILE = "stock_ranking.xlsx"
SUMMARY_FILE = "top_picks.txt"
DELAY_SECONDS = 1.5
MAX_STOCKS = int(os.environ.get("MAX_STOCKS", "0") or 0) # 0 = all 500
SHORTLIST_MIN_SCORE = 80 # percent of rules a stock must pass
TOP_N_IN_EMAIL = 15
HEADERS = {"User-Agent": "Mozilla/5.0 (personal research script)"}
# ----------------------------------------


def to_number(text):
    if text is None:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", text.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def ok(value, test):
    """True if value is a real number and passes the test."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    return bool(test(value))


def load_nifty500():
    try:
        resp = requests.get(NIFTY500_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        print("Loaded Nifty 500 list from NSE.")
    except Exception as e:
        print(f"Could not download list from NSE ({e}). Trying {LOCAL_LIST}...")
        if not os.path.exists(LOCAL_LIST):
            raise SystemExit(
                f"No {LOCAL_LIST} found. Download the Nifty 500 list CSV from "
                "niftyindices.com, save it as nifty500.csv and upload it to the repo."
            )
        df = pd.read_csv(LOCAL_LIST)
    df.columns = [c.strip() for c in df.columns]
    return df[["Company Name", "Industry", "Symbol"]]


def fetch_company(symbol):
    for suffix in ("consolidated/", ""):
        url = f"https://www.screener.in/company/{quote(symbol)}/{suffix}"
        resp = None
        for attempt in range(3):
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 429: # rate limited: wait and retry
                time.sleep(10 * (attempt + 1))
                continue
            break
        if resp is not None and resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            if soup.select("#top-ratios li"):
                return soup
        time.sleep(DELAY_SECONDS)
    return None


def parse_company(soup):
    data = {}
    for li in soup.select("#top-ratios li"):
        name_el = li.select_one(".name")
        num_el = li.select_one(".number")
        if name_el and num_el:
            data[name_el.get_text(strip=True)] = to_number(num_el.get_text())

    # Growth boxes (flexible matching on the title and the "5 Years" label)
    for ul in soup.select("ul.ranges-table"):
        items = ul.find_all("li")
        if not items:
            continue
        title = items[0].get_text(" ", strip=True).lower()
        if "sales" in title or "revenue" in title:
            kind = "Sales"
        elif "profit" in title:
            kind = "Profit"
        else:
            continue
        for li in items[1:]:
            text = li.get_text(" ", strip=True)
            m = re.match(r"\s*(\d+)\s*Years?", text, re.I)
            if m and ":" in text:
                data[f"{kind} Growth {m.group(1)}Y"] = to_number(text.split(":", 1)[1])

    # Fallback: compute 5-year growth from the annual profit & loss table
    if data.get("Sales Growth 5Y") is None:
        data["Sales Growth 5Y"] = cagr_from_table(soup, ("sales", "revenue"))
    if data.get("Profit Growth 5Y") is None:
        data["Profit Growth 5Y"] = cagr_from_table(soup, ("net profit",))
    return data


def cagr_from_table(soup, row_prefixes, years=5):
    """5-year growth (CAGR %) from the annual columns of the profit & loss table."""
    section = soup.select_one("section#profit-loss")
    table = section.select_one("table.data-table") if section else None
    if table is None:
        return None
    headers = [th.get_text(strip=True) for th in table.select("thead th")][1:]
    annual = [i for i, h in enumerate(headers) if h and h.upper() != "TTM"]
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        label = cells[0].get_text(strip=True).lower()
        if any(label.startswith(p) for p in row_prefixes):
            values = [to_number(c.get_text()) for c in cells[1:]]
            values = [values[i] for i in annual if i < len(values)]
            if len(values) > years:
                end, start = values[-1], values[-1 - years]
                if end and start and end > 0 and start > 0:
                    return round(((end / start) ** (1 / years) - 1) * 100, 1)
            return None
    return None


def add_valuation(d):
    """Adds P/B, PEG, Graham fair value, margin of safety and a valuation label."""
    price, pe, bv = d.get("Current Price"), d.get("Stock P/E"), d.get("Book Value")
    profit5 = d.get("Profit Growth 5Y")

    d["P/B"] = price / bv if ok(price, lambda x: x > 0) and ok(bv, lambda x: x > 0) else None
    d["PEG"] = pe / profit5 if ok(pe, lambda x: x > 0) and ok(profit5, lambda x: x > 0) else None

    fair = None
    if ok(price, lambda x: x > 0) and ok(pe, lambda x: x > 0) and ok(bv, lambda x: x > 0):
        eps = price / pe
        fair = math.sqrt(22.5 * eps * bv) # Graham number
    d["Fair Value (Graham)"] = round(fair, 1) if fair else None

    mos = (fair - price) / fair * 100 if fair and price else None
    d["Margin of Safety %"] = round(mos, 1) if mos is not None else None

    if mos is None:
        d["Valuation"] = "Unknown"
    elif mos >= 20:
        d["Valuation"] = "Undervalued"
    elif mos >= -20:
        d["Valuation"] = "Fairly valued"
    else:
        d["Valuation"] = "Expensive"


def score(d, is_financial):
    """Returns (score percent, passed list, failed list)."""
    if is_financial: # banks / NBFCs: ROCE and debt are not meaningful
        rules = [
            ("ROE > 12%", ok(d.get("ROE"), lambda v: v > 12)),
            ("Profit growth 5Y > 12%", ok(d.get("Profit Growth 5Y"), lambda v: v > 12)),
            ("Sales growth 5Y > 10%", ok(d.get("Sales Growth 5Y"), lambda v: v > 10)),
            ("P/E between 0 and 25", ok(d.get("Stock P/E"), lambda v: 0 < v < 25)),
            ("P/B < 3", ok(d.get("P/B"), lambda v: 0 < v < 3)),
        ]
    else:
        rules = [
            ("ROE > 15%", ok(d.get("ROE"), lambda v: v > 15)),
            ("ROCE > 15%", ok(d.get("ROCE"), lambda v: v > 15)),
            ("Sales growth 5Y > 10%", ok(d.get("Sales Growth 5Y"), lambda v: v > 10)),
            ("Profit growth 5Y > 10%", ok(d.get("Profit Growth 5Y"), lambda v: v > 10)),
            ("P/E between 0 and 30", ok(d.get("Stock P/E"), lambda v: 0 < v < 30)),
            ("PEG < 1.5", ok(d.get("PEG"), lambda v: 0 < v < 1.5)),
            ("Price <= 1.2x fair value", ok(d.get("Margin of Safety %"), lambda v: v >= -20)),
        ]
    passed = [name for name, p in rules if p]
    failed = [name for name, p in rules if not p]
    return round(100 * len(passed) / len(rules)), "; ".join(passed), "; ".join(failed)


def main():
    stocks = load_nifty500()
    if MAX_STOCKS:
        stocks = stocks.head(MAX_STOCKS)
    total = len(stocks)

    rows = []
    for i, (_, s) in enumerate(stocks.iterrows(), start=1):
        symbol = str(s["Symbol"]).strip()
        print(f"[{i}/{total}] {symbol}")
        try:
            soup = fetch_company(symbol)
            if soup is None:
                print(" skipped (page not found or blocked)")
                continue
            d = parse_company(soup)
            if i == 1:
                print(" check:", {k: v for k, v in d.items() if "Growth" in k})
            add_valuation(d)
            is_fin = "financial" in str(s["Industry"]).lower()
            d["Score %"], d["Passed"], d["Failed"] = score(d, is_fin)
            d["Company"] = s["Company Name"]
            d["Symbol"] = symbol
            d["Industry"] = s["Industry"]
            rows.append(d)
        except Exception as e:
            print(f" error: {e}")
        time.sleep(DELAY_SECONDS)

    if not rows:
        raise SystemExit("No data fetched. Screener may be blocking this server.")

    df = pd.DataFrame(rows)
    cols = ["Company", "Symbol", "Industry", "Score %", "Valuation", "Current Price",
            "Fair Value (Graham)", "Margin of Safety %", "Stock P/E", "PEG", "P/B",
            "ROE", "ROCE", "Sales Growth 5Y", "Profit Growth 5Y", "Market Cap",
            "Dividend Yield", "Passed", "Failed"]
    cols = [c for c in cols if c in df.columns]
    df = df[cols].sort_values(["Score %", "Margin of Safety %"], ascending=False)

    shortlist = df[(df["Score %"] >= SHORTLIST_MIN_SCORE)
                   & (df["Valuation"].isin(["Undervalued", "Fairly valued"]))]

    with pd.ExcelWriter(OUTPUT_FILE) as writer:
        shortlist.to_excel(writer, sheet_name="Shortlist", index=False)
        df.to_excel(writer, sheet_name="All", index=False)

    lines = [f"Nifty 500 scan: {len(df)} stocks analysed, {len(shortlist)} on the shortlist.",
             "Good growth + quality at a fair or low price:", ""]
    for n, (_, r) in enumerate(shortlist.head(TOP_N_IN_EMAIL).iterrows(), start=1):
        lines.append(
            f"{n}. {r['Company']} ({r['Symbol']}) | Score {r['Score %']}% | {r['Valuation']} | "
            f"Price {r['Current Price']} vs fair value {r['Fair Value (Graham)']} | "
            f"P/E {r['Stock P/E']} | ROE {r['ROE']}% | Profit growth 5Y {r['Profit Growth 5Y']}%"
        )
    lines += ["", "Full list in the attached Excel. Fair value is a rough Graham-number",
              "estimate, not advice. Do your own research before investing."]
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("\n".join(lines))


if __name__ == "__main__":
    main()
