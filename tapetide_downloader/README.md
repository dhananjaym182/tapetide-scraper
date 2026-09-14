# Tapetide financial-data downloader

Two ways to get data out of Tapetide:

| Path | What it hits | Auth | Best for |
|------|--------------|------|----------|
| **Markdown mirror (this script)** | `https://tapetide.com/stocks/{SYMBOL}.md` | None | Quick, single-stock fetches without setting up a token. Quarterly tables (~3 years). |
| **MCP server (`mcp_client.py`)** | `https://mcp.tapetide.com/mcp` | Free token from `tapetide.com/settings/tokens` | Structured tool calls (52 tools), annual data back to Mar 2015, point-in-time availability metadata. Free tier: 50 calls/day. |

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

## Structured MCP path — `mcp_client.py`

`mcp_client.py` is a minimal MCP (Streamable HTTP) client using only the
standard library. It authenticates with a Bearer token and talks to the
Tapetide MCP server (52 tools).

```bash
# 1. Get a free token: https://tapetide.com/settings/tokens
export TAPETIDE_TOKEN="tpt_rt_..."

# 2. Verify auth and list tools
python3 tapetide_downloader/mcp_client.py verify

# 3. Call any tool
python3 tapetide_downloader/mcp_client.py call get_stock_quote --args '{"symbol": "RELIANCE"}' --pretty
python3 tapetide_downloader/mcp_client.py call get_financials --args '{"symbol": "SBIN", "section": "profit_loss"}' --pretty
```

Notes learned the hard way (handled by the client):

- **Cloudflare WAF:** the server blocks the default `Python-urllib` User-Agent
  (Error 1010). The client sends a browser-style UA — do not remove it.
- **Truncation:** unfiltered `get_financials` responses are cut at 25,000 chars
  server-side. Pass `section` (`profit_loss`, `balance_sheet`, `cash_flow`,
  `ratios`) to get one complete 5-15 KB section per call.
- **`get_stock_quote` returns price/volume only** — for PE/PB/market cap/52w
  range use `get_company_profile`.
- MCP `get_financials` is **annual** (Mar 2015 → today + TTM); the `.md` mirror
  table is **quarterly** (~3 years). MCP also ships a per-period
  `availability` array (`available_from`, `basis: reported|estimated`) for
  point-in-time/backtest use.

MCP client-app config examples (Cursor, VS Code, Claude Desktop, ...): see
`mcp_config.example.json` at the repo root. Never commit a real token —
`mcp_config.json` is gitignored.

Tool catalog: `https://tapetide.com/mcp/llms-full.txt`
