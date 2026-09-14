# Screener.in scraper — JSON + CSV, batch with resume

One tool combining the best anti-blocking techniques from
[`sahiljani/screener-india`](https://github.com/sahiljani/screener-india) and
[`mayur1064/screenercli`](https://github.com/mayur1064/screenercli):

| Protection | Detail |
|---|---|
| Browser-like headers | Full Chrome header set (UA, Accept, Sec-Fetch-*) |
| Enforced throttle | Min 1.5s between requests **+ random jitter** |
| TTL cache | Repeated lookups within 5 min are served from memory |
| 429 handling | Honours `Retry-After` header, then exponential backoff (3 tries) |
| Proxy support | `--proxy http://...` or `socks5://...` |
| View fallback | Auto consolidated → standalone when one is missing |
| Resume | Batch mode skips symbols whose JSON already exists |

## Install

```bash
pip install -r screener_scraper/requirements.txt
```

## Single stock → JSON + CSVs

```bash
python3 screener_scraper/screener_scraper.py SBIN \
    --json out/screener/SBIN.json --csv-dir out/screener/csv
```

Output for each stock:

```
out/screener/
├── SBIN.json                    # everything, structured
└── csv/
    ├── SBIN_quarterly_results.csv
    ├── SBIN_profit_loss.csv
    ├── SBIN_balance_sheet.csv
    ├── SBIN_cash_flow.csv
    ├── SBIN_ratios.csv
    ├── SBIN_shareholding.csv
    ├── SBIN_top_ratios.csv
    ├── SBIN_pros_cons.csv
    └── SBIN_documents.csv
```

## Batch — all Indian stocks

```bash
# One symbol per line; # comments allowed
python3 screener_scraper/screener_scraper.py \
    --symbols-file tapetide_downloader/nifty500_symbols.txt \
    --out-dir out/screener --delay 1.5
```

Useful flags: `--delay`, `--jitter`, `--proxy`, `--no-csv`, `--view standalone`.
Re-running resumes (skips existing JSONs); failures land in `errors.log`.

## Timing guide (measured ~1.0s latency + pacing ≈ 2s/stock)

| Universe | Stocks | Time @1.5s+0.5s jitter |
|---|---|---|
| NSE active | ~2,700 | ~1.5–2 h |
| NSE + BSE | ~5,000–6,000 | ~3–4 h |

Screener.in is lenient (no aggressive Cloudflare). Keep ≥1s spacing, use the
cache, and split multi-day refreshes; fundamentals only change quarterly.

## Data quality note

Screener.in tables are the ground truth used (verified) by Tapetide's MCP
annual data — cross-check details in the root README.
