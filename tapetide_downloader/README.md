# Tapetide financial-data downloader

Two ways to get data out of Tapetide:

| Path | What it hits | Auth | Best for |
|------|--------------|------|----------|
| **Markdown mirror (this script)** | `https://tapetide.com/stocks/{SYMBOL}.md` | None | Quick, single-stock fetches without setting up a token. |
| **MCP server (TODO)** | `https://mcp.tapetide.com/mcp` | Free token from `tapetide.com/settings/tokens` | Structured tool calls (`get_financials`, `get_stock_quote`, screens, etc.) and higher limits. |

## Markdown mirror — how Tapetide exposes data without any API key

Each stock page has a companion Markdown file:

```
https://tapetide.com/stocks/SBIN.md
https://tapetide.com/stocks/RELIANCE.md
```

That file contains:

1. **YAML frontmatter** — symbol, company, sector, industry, market cap, PE, EPS,
   book value, P/B, dividend yield, ROE, ROCE, D/E, revenue, net income, EBITDA,
   52-week range, RSI(14), and a `generated_at` timestamp.
2. **Latest price block** — price, change, day high/low, volume.
3. **Financial statement tables** — Quarterly Results, Profit & Loss, Balance Sheet,
   Cash Flow, Ratios, Shareholding Pattern, and more, in Markdown table form.

That is the data the website itself renders. The script in this folder parses those
two sources into structured Python objects.

## Quick start

```bash
python tapetide_downloader/tapetide_downloader.py SBIN
```

Example output:

```
Symbol:            SBIN
Company:           State Bank of India
Sector / Industry: Financial Services / Banks
Market cap (₹ Cr): 920108
P/E (reported):    10
P/E (TTM):         10.66
EPS (TTM):         93.39 ₹
...
```

## Common operations

```bash
# One symbol, JSON dump
python tapetide_downloader/tapetide_downloader.py SBIN --json sbin.json

# One symbol, every statement table as a separate CSV
python tapetide_downloader/tapetide_downloader.py SBIN --csv-dir ./csv

# One symbol, one table as CSV
python tapetide_downloader/tapetide_downloader.py SBIN \
  --table "Profit & Loss" --csv-file sbin_pnl.csv

# Multiple symbols (polite sequential requests)
python tapetide_downloader/tapetide_downloader.py --symbols "SBIN,RELIANCE,TATASTEEL" --json out.json

# Inspect available tables for a symbol without printing everything
python tapetide_downloader/tapetide_downloader.py SBIN --list-tables
```

Columns are preserved as-is from the Markdown table, so a "Profit & Loss" CSV will
have `Item` plus one column per fiscal year / quarter exactly as shown on the site.

## What to watch out for

- The Markdown mirror is regenerated on each request (`generated_at` changes), so it
  represents Tapetide's current computed snapshot, not a fixed archive.
- The site sits behind Cloudflare. If you run this against many symbols back-to-back,
  slow the cadence by editing `DELAY_BETWEEN_REQUESTS` at the top of the script.
- The data is compiled from company filings and exchange disclosures and is labeled
  by Tapetide as research/information only, not investment advice.

## TODO — structured MCP path

For heavier use (historical OHLCV, screens, shareholding, FII/DII flows, option
chains, portfolio), the real API lives behind the Tapetide MCP server:

- Endpoint: `https://mcp.tapetide.com/mcp`
- Free token: `https://tapetide.com/settings/tokens`
- Tool catalog: `https://tapetide.com/mcp/llms-full.txt`

A natural next step is a small MCP client wrapper (for example with `pymcp` or a
manual Streamable HTTP client) that calls `get_financials`, `get_stock_quote`,
`get_price_history`, `get_shareholding`, and so on, with the same symbol input
signature as this script. That gives structured JSON instead of parsing Markdown,
and it is what the site itself uses for the richer features.
