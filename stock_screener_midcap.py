import io
import os
import time

import pandas as pd
import requests

import stock_screener as ss

# ---------------- CONFIG ----------------
MIDCAP_URL = "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv"
LOCAL_LIST = "niftymidcap150.csv" # fallback if NSE blocks the download
OUTPUT_FILE = "midcap_ranking.xlsx"
SUMMARY_FILE = "top_picks_midcap.txt"
MAX_STOCKS = int(os.environ.get("MAX_STOCKS", "0") or 0) # 0 = all 150
# ----------------------------------------


def load_midcap150():
    try:
        resp = requests.get(MIDCAP_URL, headers=ss.HEADERS, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        print("Loaded Nifty Midcap 150 list from NSE.")
    except Exception as e:
        print(f"Could not download list from NSE ({e}). Trying {LOCAL_LIST}...")
        if not os.path.exists(LOCAL_LIST):
            raise SystemExit(
                f"No {LOCAL_LIST} found. Download the Nifty Midcap 150 list CSV from "
                "niftyindices.com, save it as niftymidcap150.csv and upload it to the repo."
            )
        df = pd.read_csv(LOCAL_LIST)
    df.columns = [c.strip() for c in df.columns]
    return df[["Company Name", "Industry", "Symbol"]]


def make_note(d):
    notes = []
    if ss.ok(d.get("Stock P/E"), lambda v: 0 < v < 6):
        notes.append("Very low P/E: check if earnings are at a cyclical peak")
    if ss.ok(d.get("Profit Growth 5Y"), lambda v: v > 50):
        notes.append("High growth may be recovery from a low base")
    if d.get("Profit Growth 5Y") is None or d.get("Sales Growth 5Y") is None:
        notes.append("Incomplete growth data")
    return "; ".join(notes)


def analyse(row):
    symbol = str(row["Symbol"]).strip()
    soup = ss.fetch_company(symbol)
    if soup is None:
        return None
    d = ss.parse_company(soup)
    ss.add_valuation(d)
    is_fin = "financial" in str(row["Industry"]).lower()
    d["Score %"], d["Passed"], d["Failed"] = ss.score(d, is_fin)
    d["Company"] = row["Company Name"]
    d["Symbol"] = symbol
    d["Industry"] = row["Industry"]
    d["Note"] = make_note(d)
    return d


def main():
    stocks = load_midcap150()
    if MAX_STOCKS:
        stocks = stocks.head(MAX_STOCKS)
    total = len(stocks)

    rows, failed = [], []
    for i, (_, s) in enumerate(stocks.iterrows(), start=1):
        print(f"[{i}/{total}] {s['Symbol']}")
        try:
            d = analyse(s)
            if d is None:
                failed.append(s)
                print(" skipped, will retry later")
            else:
                rows.append(d)
        except Exception as e:
            failed.append(s)
            print(f" error: {e}")
        time.sleep(ss.DELAY_SECONDS)

    # One retry pass for stocks that failed
    if failed:
        print(f"\nRetrying {len(failed)} failed stocks after a pause...")
        time.sleep(30)
        for s in failed:
            try:
                d = analyse(s)
                if d is not None:
                    rows.append(d)
                    print(f" recovered {s['Symbol']}")
            except Exception as e:
                print(f" still failing {s['Symbol']}: {e}")
            time.sleep(ss.DELAY_SECONDS)

    if not rows:
        raise SystemExit("No data fetched. Screener may be blocking this server.")

    df = pd.DataFrame(rows)
    cols = ["Company", "Symbol", "Industry", "Score %", "Valuation", "Current Price",
            "Fair Value (Graham)", "Margin of Safety %", "Stock P/E", "PEG", "P/B",
            "ROE", "ROCE", "Sales Growth 5Y", "Profit Growth 5Y", "Market Cap",
            "Dividend Yield", "Note", "Passed", "Failed"]
    cols = [c for c in cols if c in df.columns]
    df = df[cols].sort_values(["Score %", "Margin of Safety %"], ascending=False)

    shortlist = df[(df["Score %"] >= ss.SHORTLIST_MIN_SCORE)
                   & (df["Valuation"].isin(["Undervalued", "Fairly valued"]))]
    non_fin = shortlist[~shortlist["Industry"].astype(str).str.contains("financial", case=False)]

    with pd.ExcelWriter(OUTPUT_FILE) as writer:
        shortlist.to_excel(writer, sheet_name="Shortlist", index=False)
        non_fin.to_excel(writer, sheet_name="Non-Financial Picks", index=False)
        df.to_excel(writer, sheet_name="All", index=False)

    lines = [f"Nifty Midcap 150 scan: {len(df)} stocks analysed, "
             f"{len(shortlist)} on the shortlist ({len(non_fin)} non-financial).",
             "Good growth + quality at a fair or low price:", ""]
    for n, (_, r) in enumerate(shortlist.head(ss.TOP_N_IN_EMAIL).iterrows(), start=1):
        flag = " (!)" if r.get("Note") else ""
        lines.append(
            f"{n}. {r['Company']} ({r['Symbol']}){flag} | Score {r['Score %']}% | {r['Valuation']} | "
            f"Price {r['Current Price']} vs fair value {r['Fair Value (Graham)']} | "
            f"P/E {r['Stock P/E']} | ROE {r['ROE']}% | Profit growth 5Y {r['Profit Growth 5Y']}%"
        )
    lines += ["", "(!) = see the Note column in the Excel (cyclical peak, low base or missing data).",
              "Midcaps are more volatile than large caps. Fair value is a rough Graham-number",
              "estimate, not advice. Do your own research before investing."]
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("\n".join(lines))


if __name__ == "__main__":
    main()