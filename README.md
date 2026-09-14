# 📈 Tapetide Financial Data Downloader

Download structured Indian stock market data (Nifty 500 and beyond) from
[tapetide.com](https://tapetide.com) — **no API key, no signup, no external
dependencies**. Pure Python standard library.

Built and verified against all **500 Nifty 500 constituents** (see
[Verified Results](#-verified-results)).

---

## ✨ Features

- **Zero dependencies** — Python 3.9+ standard library only
- **Full financial snapshot** — price, market cap, P/E (reported & TTM), EPS,
  book value, P/B, dividend yield, ROE, ROCE, D/E, revenue, net income, EBITDA,
  52-week range, RSI(14), volume and more
- **12 statement tables per stock** — Quarterly Results, Profit & Loss, Balance
  Sheet, Cash Flow, Ratios, Shareholding Pattern, Technical Indicators, Analyst
  Ratings, Tapetide Score, Growth Rates, Filings & Documents
- **JSON + CSV export** — one JSON per stock, one CSV per statement table
- **Batch mode with resume** — download 500 stocks in ~19 min; interrupted runs
  pick up where they left off
- **Polite scraping** — configurable 1s delay, retries with backoff,
  Cloudflare-challenge detection

---

## 📁 Repository Hierarchy

```
tapetide-financial-downloader/
├── README.md                        # ← you are here
├── .gitignore                       # keeps data outputs & caches out of git
├── LICENSE                          # add your license (e.g. MIT)
│
├── tapetide_downloader/             # the app
│   ├── tapetide_downloader.py       # core: fetcher, parsers, models, CLI
│   ├── batch_download.py            # batch downloader (resume + error log)
│   ├── test_smoke.py                # offline smoke test (no network needed)
│   ├── nifty500_symbols.txt         # official NSE Nifty 500 symbol list
│   └── README.md                    # detailed module-level docs
│
├── output/                          # created at runtime — gitignored
│   ├── SBIN_data.json               # full dump: snapshot + all 12 tables
│   ├── SBIN.json                    # single-symbol CLI output
│   ├── errors.log                   # batch failures (auto-retried next run)
│   └── csv/                         # per-table CSVs
│       ├── SBIN_financial_statements.csv
│       ├── SBIN_shareholding_pattern.csv
│       └── ...
│
└── .github/
    └── workflows/
        └── smoke.yml                # optional CI: runs the smoke test
```

| File | Purpose |
|------|---------|
| `tapetide_downloader.py` | Everything for one stock: fetch `.md` mirror, parse YAML frontmatter + bullets + Markdown tables, export JSON/CSV. Also the single-stock CLI. |
| `batch_download.py` | Loops over a symbols file, calls the core module, writes JSON + CSVs, skips already-downloaded symbols, logs failures. |
| `test_smoke.py` | Validates all parsers offline against a fixed sample payload. |
| `nifty500_symbols.txt` | 501 rows fetched from NSE archives (one is a known placeholder row). |

---

## 🚀 Quick Start

### Requirements

- Python **3.9+** (standard library only — nothing to `pip install`)

### Install

```bash
git clone https://github.com/<your-username>/tapetide-financial-downloader.git
cd tapetide-financial-downloader
```

### 1. Single stock

```bash
python3 tapetide_downloader/tapetide_downloader.py SBIN
```

```
Symbol:            SBIN
Company:           State Bank of India
Sector / Industry: Financial Services / Banks
Market cap (₹ Cr): 920107.96
P/E (reported):    10.3
P/E (TTM):         10.66
EPS (TTM):         93.39 ₹
ROE / ROCE:        13.85% / 5.16%
52w high / low:    1234.7 / 810.4
Price / chg / %:   995.7 / -14 / -1.39%
Volume:            7,771,109
```

### 2. Single stock with exports

```bash
# JSON dump + all statement tables as CSVs
python3 tapetide_downloader/tapetide_downloader.py SBIN \
    --json output/SBIN.json --csv-dir output/csv

# One specific table only
python3 tapetide_downloader/tapetide_downloader.py SBIN \
    --table "Profit & Loss" --csv-file output/SBIN_pnl.csv

# List available tables
python3 tapetide_downloader/tapetide_downloader.py SBIN --list-tables
```

### 3. Multiple symbols

```bash
python3 tapetide_downloader/tapetide_downloader.py \
    --symbols "SBIN,RELIANCE,TATASTEEL" --json output/multi.json
```

### 4. Download the full Nifty 500

```bash
python3 tapetide_downloader/batch_download.py \
    --symbols tapetide_downloader/nifty500_symbols.txt \
    --out-dir output/nifty500_data
```

Useful flags:

| Flag | Meaning |
|------|---------|
| `--limit 10` | Only fetch the first N symbols (test run) |
| `--delay 2.0` | Slow down if you see 429s / Cloudflare challenges |
| `--no-csv` | JSON only, skip per-table CSVs |

**Resume support:** already-downloaded symbols are skipped automatically.
Delete `<SYMBOL>_data.json` to force a re-fetch, or just re-run the command —
failed symbols are retried on the next run.

---

## 📦 Output Format

Per stock you get:

```
output/nifty500_data/
├── SBIN_data.json                     # everything, structured
└── csv/
    ├── SBIN_tapetide_score.csv
    ├── SBIN_growth_rates.csv
    ├── SBIN_financial_statements.csv  # quarterly P&L, periods as columns
    ├── SBIN_shareholding_pattern.csv
    ├── SBIN_technical_indicators.csv
    ├── SBIN_analyst_ratings.csv
    └── SBIN_filings_documents.csv
```

`*_data.json` shape:

```json
{
  "symbol": "SBIN",
  "snapshot": {
    "company": "State Bank of India",
    "market_cap_cr": 920107.96,
    "pe_reported": 10.3,
    "pe_ttm": 10.66,
    "price": 995.7,
    "volume": 7771109
  },
  "tables": {
    "Financial Statements": { "period_header": ["Sep 2023", "..."], "rows": [...] }
  },
  "source": "https://tapetide.com/stocks/SBIN.md",
  "generated_at": "2026-09-13T12:08:30.763Z"
}
```

CSV columns are preserved exactly as shown on the site (`Item` + one column per
quarter/fiscal year).

---

## 🔍 How It Works

Tapetide publishes a Markdown mirror of every stock page with **no auth**:

```
https://tapetide.com/stocks/SBIN.md
```

Each file contains:

1. **YAML frontmatter** — symbol, company, sector, market cap, P/E, EPS, ROE …
2. **Key fundamentals bullets** — `- **Price:** ₹995.7` style metrics
3. **Markdown tables** — quarterly results, P&L, shareholding, etc.

The downloader fetches the `.md` file (3 retries with exponential backoff,
HTML-challenge detection), parses those three sources into dataclasses
(`FinancialSnapshot`, `TimeSeriesTable`, `StockData`), and exports JSON/CSV.

---

## ✅ Verified Results

Run against the official NSE Nifty 500 list on 2026-09-13:

| Metric | Result |
|--------|--------|
| Symbols fetched | **500 / 501** |
| JSON files | 500 (all validated structurally) |
| CSV files | 3,461 |
| Total size | 45 MB |
| Wall time | ~19 minutes at 1s delay |
| Failures | 1 — `DUMMYHEG`* |

\* `DUMMYHEG` ("Dummy HEG Ltd.", ISIN `DUM545A01024`) is a **placeholder row that
NSE itself includes** in its index constituent CSVs — it is not a real stock,
so the 404 is correct behavior. The real `HEG` downloads fine.

---

## 🧪 Testing

```bash
# Offline parser test (no network) — run this first
python3 tapetide_downloader/test_smoke.py

# One live symbol to sanity-check connectivity
python3 tapetide_downloader/tapetide_downloader.py SBIN
```

---

## 🐙 Deploying to GitHub

```bash
# 1. Init and commit
git init
git add README.md .gitignore tapetide_downloader/
git commit -m "Tapetide financial data downloader: Nifty 500 batch support"

# 2. Create an empty repo on github.com, then:
git remote add origin https://github.com/<your-username>/tapetide-financial-downloader.git
git branch -M main
git push -u origin main
```

> ⚠️ The `.gitignore` in this repo excludes `output/`, `__pycache__/` and logs.
> Do **not** commit the downloaded datasets — re-run the batch downloader to
> regenerate them fresh anytime.

**Suggested topics:** `python`, `nse`, `nifty500`, `stock-market`,
`financial-data`, `webscraper`, `india-stocks`, `screener`

---

## ⚠️ Notes & Limitations

- **Rate limits:** the site sits behind Cloudflare. Keep `--delay` at 1s or
  higher for batch runs; back off if you see 429s.
- **Fresh snapshots only:** the `.md` mirror is regenerated per request
  (`generated_at` changes), so this gives Tapetide's *current* computed view —
  not a fixed historical archive.
- **Not investment advice:** data is compiled from company filings and exchange
  disclosures; Tapetide labels it research/information only.
- **Richer API exists:** an MCP server (`https://mcp.tapetide.com/mcp`, free
  token) exposes structured tools like `get_financials` and
  `get_price_history` — see `tapetide_downloader/README.md` for the roadmap.

---

## 📄 License

Add your preferred license (MIT/Apache-2.0 recommended for scrapers).
