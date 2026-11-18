import re
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup

# ---------------- CONFIG ----------------
TICKERS = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ITC", "LT", "TITAN", "ASIANPAINT"]

# Scoring rules: (column, test, label)
RULES = [
    ("ROE", lambda v: v > 15, "ROE > 15%"),
    ("ROCE", lambda v: v > 15, "ROCE > 15%"),
    ("Stock P/E", lambda v: 0 < v < 30, "P/E between 0 and 30"),
    ("Sales Growth 5Y", lambda v: v > 10, "Sales growth 5Y > 10%"),
    ("Profit Growth 5Y", lambda v: v > 10, "Profit growth 5Y > 10%"),
]

OUTPUT_FILE = "stock_ranking.xlsx"
DELAY_SECONDS = 2 # be polite to the website
HEADERS = {"User-Agent": "Mozilla/5.0 (personal research script)"}
# ----------------------------------------


def to_number(text):
    """Convert strings like '₹ 2,950', '18.5 %', '1,234 Cr.' to float."""
    if text is None:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", text.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch_company(symbol):
    """Fetch one company's data. Tries consolidated page first, then standalone."""
    for suffix in ("consolidated/", ""):
        url = f"https://www.screener.in/company/{symbol}/{suffix}"
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            if soup.select("#top-ratios li"):
                return soup
    return None


def parse_company(symbol, soup):
    data = {"Symbol": symbol}

    # Top ratios: Market Cap, Current Price, P/E, ROCE, ROE, etc.
    for li in soup.select("#top-ratios li"):
        name_el = li.select_one(".name")
        num_el = li.select_one(".number")
        if name_el and num_el:
            data[name_el.get_text(strip=True)] = to_number(num_el.get_text())

    # Growth tables: Compounded Sales Growth, Profit Growth, etc.
    for ul in soup.select("ul.ranges-table"):
        items = ul.find_all("li")
        if not items:
            continue
        title = items[0].get_text(strip=True)
        for li in items[1:]:
            text = li.get_text(" ", strip=True) # e.g. "5 Years: 12 %"
            if ":" in text:
                period, value = text.split(":", 1)
                data[f"{title} {period.strip()}"] = to_number(value)

    # Friendly column names
    data["Sales Growth 5Y"] = data.get("Compounded Sales Growth 5 Years")
    data["Profit Growth 5Y"] = data.get("Compounded Profit Growth 5 Years")
    return data


def score_row(row):
    passed, failed = [], []
    for col, test, label in RULES:
        value = row.get(col)
        if value is not None and not pd.isna(value) and test(value):
            passed.append(label)
        else:
            failed.append(label)
    return len(passed), "; ".join(passed), "; ".join(failed)


def main():
    rows = []
    for symbol in TICKERS:
        print(f"Fetching {symbol}...")
        try:
            soup = fetch_company(symbol)
            if soup is None:
                print(f" Could not load {symbol} (check the symbol or your connection).")
                continue
            rows.append(parse_company(symbol, soup))
        except Exception as e:
            print(f" Error for {symbol}: {e}")
        time.sleep(DELAY_SECONDS)

    if not rows:
        print("No data fetched.")
        return

    df = pd.DataFrame(rows)
    scores = df.apply(score_row, axis=1, result_type="expand")
    df["Score"] = scores[0]
    df["Passed"] = scores[1]
    df["Failed"] = scores[2]

    # Put the most useful columns first
    first = ["Symbol", "Score", "Current Price", "Market Cap", "Stock P/E",
             "ROE", "ROCE", "Sales Growth 5Y", "Profit Growth 5Y",
             "Dividend Yield", "Book Value"]
    first = [c for c in first if c in df.columns]
    rest = [c for c in df.columns if c not in first]
    df = df[first + rest].sort_values("Score", ascending=False)

    df.to_excel(OUTPUT_FILE, index=False)
    print(f"\nDone. Saved to {OUTPUT_FILE}")
    print(df[first].to_string(index=False))


if __name__ == "__main__":
    main()