"""Tapetide financial-data downloader (scraping fallback path).

How Tapetide exposes data
--------------------------
- Human-facing pages: https://tapetide.com/stocks/{SYMBOL}
- Markdown mirrors:   https://tapetide.com/stocks/{SYMBOL}.md
- Structured meta block at the top of each .md file (YAML frontmatter).
- No open JSON REST endpoint was reachable from outside the site during
  discovery; the real API lives behind the MCP server at
  https://mcp.tapetide.com/mcp (see README.md / TODO for the fuller path).

Limitations of the scraping path
---------------------------------
- The site sits behind Cloudflare and sets per-request edge/CF-Ray headers;
  aggressive concurrent polling will get rate-limited or challenged.
- The .md mirror is regenerated on each request (see `generated_at`), so it
  reflects Tapetide's latest computed snapshot rather than a fixed historical
  archive.
- This is a research/information tool, not investment advice (same disclaimer
  Tapetide itself carries).
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from typing import ClassVar, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_URL = "https://tapetide.com"
MARKDOWN_PATH = "/stocks/{symbol}.md"

# Polite single-request cadence (seconds). Bump this if you see 429s / challenges.
DELAY_BETWEEN_REQUESTS = 1.0

YAML_SEPARATOR = "---\n"

# ---------------------------------------------------------------------------
# Minimal YAML frontmatter parser
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split YAML frontmatter from the Markdown body.

    Returns (meta_dict, body). If there is no frontmatter, meta_dict is empty
    and body is the full text.
    """
    if not text.startswith(YAML_SEPARATOR):
        return {}, text

    end = text.find(YAML_SEPARATOR, len(YAML_SEPARATOR))
    if end == -1:
        return {}, text

    raw = text[len(YAML_SEPARATOR) : end]
    body = text[end + len(YAML_SEPARATOR) :]

    meta: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, body


def _unescape_md_value(raw: str) -> str:
    """Collapse common Markdown niceties back to plain values.

    Handles things like ``- **Name:** value`` -> ``value`` after the
    frontmatter has already been consumed.
    """
    return raw.strip()


# ---------------------------------------------------------------------------
# Section -> table parser
# ---------------------------------------------------------------------------

def _split_md_sections(body: str) -> dict[str, str]:
    """Return {heading_text: section_markdown} for top-level `## ` sections."""
    sections: dict[str, str] = {}
    current_heading: Optional[str] = None
    current_lines: list[str] = []

    for line in body.splitlines():
        if line.startswith("## "):
            if current_heading is not None:
                sections[current_heading] = "\n".join(current_lines)
            current_heading = line[3:].strip()
            current_lines = []
        elif line.startswith("#") and not line.startswith("## "):
            # top-level `# Title` — skip (we already have the info above the
            # first `##`).
            continue
        else:
            current_lines.append(line)

    if current_heading is not None:
        sections[current_heading] = "\n".join(current_lines)
    return sections


def _parse_table(markdown_table: str) -> tuple[list[str], list[list[str]]]:
    """Parse a GitHub-flavoured Markdown table into (header_row, data_rows).

    Returns ([], []) when the text does not look like a table.
    """
    lines = [ln.strip() for ln in markdown_table.splitlines() if ln.strip()]
    if len(lines) < 2:
        return [], []

    # Require the separator row (the one filled with `---` / `|---|---|`).
    sep_idx: Optional[int] = None
    for i, ln in enumerate(lines):
        if ln.startswith("|") and set(ln.replace("|", "").replace("-", "").replace(" ", "")) <= set(":-"):
            sep_idx = i
            break
    if sep_idx is None:
        return [], []

    header = _cells(lines[sep_idx - 1])
    rows = [_cells(ln) for ln in lines[sep_idx + 1 :] if ln.startswith("|")]
    # An all-empty leading header cell means the first column is a row-label
    # column with a blank title; keep only one such empty slot.
    while header and header[0] == "" and len(header) > 1 and header[1] == "":
        header = header[1:]

    return header, rows


def _cells(line: str) -> list[str]:
    """Split a `| a | b | c |` line into stripped cell strings.

    Inner empty cells are preserved (positionally) so that rows stay aligned
    with their header even when a cell is blank; only the outer pipes are
    dropped.
    """
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [p.strip() for p in stripped.split("|")]


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------

@dataclass
class FinancialSnapshot:
    """The structured key-metrics block rendered on the stock page."""

    symbol: str = ""
    company: str = ""
    sector: str = ""
    industry: str = ""
    market_cap_cr: Optional[float] = None
    pe_reported: Optional[float] = None
    pe_ttm: Optional[float] = None
    eps_ttm: Optional[float] = None
    eps_reported: Optional[float] = None
    book_value: Optional[float] = None
    price_to_book: Optional[float] = None
    dividend_yield_pct: Optional[float] = None
    roe_pct: Optional[float] = None
    roce_pct: Optional[float] = None
    debt_to_equity: Optional[float] = None
    revenue_yearly_cr: Optional[float] = None
    net_income_cr: Optional[float] = None
    ebitda_cr: Optional[float] = None
    week_52_high: Optional[float] = None
    week_52_low: Optional[float] = None
    rsi_14: Optional[float] = None
    price: Optional[float] = None
    change: Optional[float] = None
    change_pct: Optional[float] = None
    volume: Optional[int] = None
    generated_at: str = ""

    # Normalized bullet label -> dataclass field. Labels not present here fall
    # through to themselves and are skipped unless they match a real field.
    _KEY_MAP: ClassVar[dict[str, str]] = {
        "market_cap": "market_cap_cr",
        "market_cap_cr": "market_cap_cr",
        "pe": "pe_reported",
        "pe_reported": "pe_reported",
        "p_e_reported": "pe_reported",
        "pe_ttm": "pe_ttm",
        "p_e_ttm": "pe_ttm",
        "eps_ttm": "eps_ttm",
        "eps": "eps_reported",
        "eps_reported": "eps_reported",
        "book_value": "book_value",
        "price_to_book": "price_to_book",
        "dividend_yield": "dividend_yield_pct",
        "dividend_yield_pct": "dividend_yield_pct",
        "roe": "roe_pct",
        "roe_pct": "roe_pct",
        "roce": "roce_pct",
        "roce_pct": "roce_pct",
        "debt_to_equity": "debt_to_equity",
        "revenue_yearly": "revenue_yearly_cr",
        "revenue_yearly_cr": "revenue_yearly_cr",
        "net_income": "net_income_cr",
        "net_income_cr": "net_income_cr",
        "ebitda": "ebitda_cr",
        "ebitda_cr": "ebitda_cr",
        "week_52_high": "week_52_high",
        "52w_high": "week_52_high",
        "52_week_high": "week_52_high",
        "week_52_low": "week_52_low",
        "52w_low": "week_52_low",
        "52_week_low": "week_52_low",
        "rsi_14": "rsi_14",
        "price": "price",
        "change": "change",
        "change_pct": "change_pct",
        "volume": "volume",
    }

    @classmethod
    def _route_key(cls, key: str) -> str:
        """Map a normalized bullet label to the dataclass field it should fill."""
        return cls._KEY_MAP.get(key, key)

    @classmethod
    def from_frontmatter_and_body(cls, symbol: str, meta: dict[str, str], body: str) -> "FinancialSnapshot":
        snap = cls(symbol=symbol)
        snap.company = meta.get("company", "")
        snap.sector = meta.get("sector", "")
        snap.industry = meta.get("industry", "")
        snap.generated_at = meta.get("generated_at", "")

        # Frontmatter carries most of these as plain numbers (sometimes with
        # commas). Coerce to float where possible.
        for key, attr in [
            ("market_cap_cr", "market_cap_cr"),
            ("pe", "pe_reported"),
            ("pe_ttm", "pe_ttm"),
            ("eps_ttm", "eps_ttm"),
            ("eps", "eps_reported"),
            ("book_value", "book_value"),
            ("price_to_book", "price_to_book"),
            ("dividend_yield", "dividend_yield_pct"),
            ("roe", "roe_pct"),
            ("roce", "roce_pct"),
            ("debt_to_equity", "debt_to_equity"),
            ("revenue_yearly", "revenue_yearly_cr"),
            ("net_income", "net_income_cr"),
            ("ebitda", "ebitda_cr"),
            ("week_52_high", "week_52_high"),
            ("week_52_low", "week_52_low"),
            ("rsi_14", "rsi_14"),
            ("price", "price"),
            ("change", "change"),
            ("change_pct", "change_pct"),
            ("volume", "volume"),
        ]:
            raw = meta.get(key, "")
            val = _coerce_float(raw)
            if val is not None:
                setattr(snap, attr, val)

        # Most mirrors keep the bulk of the key metrics out of YAML and inside
        # the Markdown body, so always parse the body list and merge it on top
        # of whatever came from the frontmatter.
        body_kv = _parse_key_fundamentals_list(body)
        for key, raw_value in body_kv.items():
            target = cls._route_key(key)
            if not hasattr(snap, target):
                continue
            if target == "volume":
                setattr(snap, target, int(raw_value))
            else:
                setattr(snap, target, raw_value)

        return snap


@dataclass
class TimeSeriesTable:
    """A single financial statement / ratio table from the Markdown."""

    label: str                # e.g. "Quarterly Results", "Profit & Loss"
    period_header: list[str]  # column headers (period labels)
    rows: list[dict[str, Optional[str]]]  # "_row_label" plus one key per period


@dataclass
class StockData:
    symbol: str
    snapshot: FinancialSnapshot
    sections: dict[str, str]               # raw section text, for debugging
    tables: dict[str, TimeSeriesTable]     # label -> parsed table
    raw_markdown: str


# ---------------------------------------------------------------------------
# Low-level fetcher
# ---------------------------------------------------------------------------

def _fetch_markdown(symbol: str, timeout_seconds: int = 60) -> str:
    """Fetch the Markdown mirror for *symbol*.

    Raises on non-200 responses (including 404 / Cloudflare challenges).
    """
    url = f"{BASE_URL}{MARKDOWN_PATH.format(symbol=quote(symbol, safe=''))}"
    req = Request(url, headers={
        "User-Agent": "TapetideDataDownloader/1.0 (research tool; contact: user@example.com)",
        "Accept": "text/markdown, text/plain, text/html, */*",
        "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    })

    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            with urlopen(req, timeout=timeout_seconds) as resp:
                status = resp.status
                raw_bytes = resp.read()
                content_type = (resp.headers.get("Content-Type") or "").lower()

                decoded = raw_bytes.decode("utf-8", "replace")

                if status != 200:
                    raise HTTPError(url, status, f"HTTP {status}", {}, None)

                # If Cloudflare returned an HTML challenge instead of the markdown,
                # surface it as an error so callers can decide what to do.
                if "text/markdown" not in content_type and "text/plain" not in content_type:
                    if decoded.lstrip().startswith(("<!doctype html", "<html", "<svg")):
                        raise RuntimeError(
                            f"Expected markdown from {url} but got HTML (status {status}, "
                            f"content-type {content_type}). The site may be challenging automated requests."
                        )
                return decoded
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            last_error = exc
            time.sleep(min(2 ** attempt, 8))
            continue

    raise last_error or RuntimeError(f"Failed to fetch {url} after retries.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_stock(symbol: str, timeout_seconds: int = 60) -> StockData:
    """Download and structure financial data for one stock from Tapetide.

    Parameters
    ----------
    symbol:
        NSE/BSE symbol, e.g. ``"SBIN"``, ``"RELIANCE"``, ``"TATASTEEL"``.
    timeout_seconds:
        Per-request timeout passed to the HTTP client.
    """
    raw = _fetch_markdown(symbol, timeout_seconds=timeout_seconds)
    meta, body = _parse_frontmatter(raw)
    sections = _split_md_sections(body)

    symbol_from_meta = meta.get("symbol", symbol)
    snapshot = FinancialSnapshot.from_frontmatter_and_body(symbol_from_meta, meta, body)
    tables = {label: _parse_time_series_table(label, text) for label, text in sections.items()}

    return StockData(
        symbol=symbol_from_meta,
        snapshot=snapshot,
        sections=sections,
        tables=tables,
        raw_markdown=raw,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _coerce_float(raw: str) -> Optional[float]:
    """Turn "₹9,20,108 Cr" / "10.3" / "0" into float when possible."""
    if raw in ("", "-", "—", "nil", "null"):
        return None

    cleaned = raw.strip()
    # Lose currency symbols and whitespace.
    cleaned = re.sub(r"[₹$€\s]", "", cleaned)
    # Indian-crore style grouping: "9,20,108" -> "920108".
    cleaned = cleaned.replace(",", "")
    # Drop unit suffixes like "Cr" / "crore" / "%".
    cleaned = re.sub(r"(?i)(cro|cr|core|cores?)", "", cleaned)
    # Keep a leading sign and digits, one decimal point.
    m = re.match(r"[-+]?\d+(?:\.\d+)?", cleaned)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _parse_key_fundamentals_list(body: str) -> dict[str, float]:
    """Parse the bullet list under `## Latest Price` / `## Key Fundamentals`.

    The Tapetide Markdown mirrors place most key metrics as bullets, e.g.::

        - **Price:** ₹995.7
        - **Change:** -14 (-1.39%)
        - **Market Cap:** ₹920107.96 Cr
        - **P/E (reported):** 10.3
        - **Dividend Yield:** 1.72%

    Labels are normalized into snake_case keys; parenthetical context like
    ``P/E (reported)`` becomes ``pe_reported``. For compound bullets such as
    ``- **Change:** -14 (-1.39%)`` the first numeric token is the primary
    value and the second becomes a ``_pct`` (or ``_2``) companion key.
    """
    out: dict[str, float] = {}
    for line in body.splitlines():
        # Bullets look like ``- **Price:** ₹995.7`` — note the colon sits
        # INSIDE the bold markers, so match `:**` not `**:`.
        m = re.match(r"-\s+\*\*([^*:]+):\*\*\s*(.+)", line)
        if not m:
            continue
        raw_name = m.group(1).strip()
        value_text = m.group(2).strip()

        # Normalize the label into a snake_case key. Keep parenthetical
        # context as a suffix so ``P/E (reported)`` -> ``p_e_reported`` and
        # ``P/E (TTM)`` -> ``p_e_ttm`` stay distinct (``RSI (14)`` ->
        # ``rsi_14`` also benefits).
        name = raw_name.lower().replace("(", "_").replace(")", "")
        name = re.sub(r"[^A-Za-z0-9]", "_", name)
        name = re.sub(r"_+", "_", name).strip("_")

        nums = re.findall(r"[-+]?\d+(?:\.\d+)?", value_text.replace(",", ""))
        if not nums:
            continue

        primary = float(nums[0])
        out[name] = primary

        # If the bullet is something like ``- **Change:** -14 (-1.39%)``, store
        # the second numeric token as a companion so the snapshot can keep
        # ``change`` and ``change_pct`` separate.
        if len(nums) >= 2:
            remainder = value_text[value_text.find(nums[0]) + len(nums[0]):]
            if "%" in remainder or "pct" in remainder.lower():
                pct_key = f"{name}_pct" if not name.endswith("_pct") else name
                out[pct_key] = float(nums[1])
            else:
                # second token without a percent sign -> a sibling key.
                out[f"{name}_2"] = float(nums[1])
    return out


def _parse_time_series_table(label: str, text: str) -> TimeSeriesTable:
    header, rows = _parse_table(text)
    if not header:
        return TimeSeriesTable(label=label, period_header=[], rows=[])

    # The first header cell is usually the row-label column ("Item"); many
    # mirrors leave it blank instead.
    first = header[0].strip().lower()
    has_label_col = first in ("", "item", "category", "particulars")
    period_header = header[1:] if has_label_col else header

    parsed_rows: list[dict[str, Optional[str]]] = []
    for row in rows:
        if not row:
            continue
        if has_label_col:
            row_label: Optional[str] = row[0].strip()
            values = row[1:]
        else:
            row_label = None
            values = row
        # pad to header length
        if len(values) < len(period_header):
            values = list(values) + [None] * (len(period_header) - len(values))
        rec: dict[str, Optional[str]] = {"_row_label": row_label}
        for i, period in enumerate(period_header):
            val = values[i] if i < len(values) else None
            rec[period] = val.strip() if isinstance(val, str) else None
        parsed_rows.append(rec)

    return TimeSeriesTable(label=label, period_header=period_header, rows=parsed_rows)


def _quote_symbol(symbol: str) -> str:
    return quote(symbol, safe="")


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def snapshot_to_dict(snap: FinancialSnapshot) -> dict:
    return {
        "symbol": snap.symbol,
        "company": snap.company,
        "sector": snap.sector,
        "industry": snap.industry,
        "market_cap_cr": snap.market_cap_cr,
        "pe_reported": snap.pe_reported,
        "pe_ttm": snap.pe_ttm,
        "eps_ttm": snap.eps_ttm,
        "eps_reported": snap.eps_reported,
        "book_value": snap.book_value,
        "price_to_book": snap.price_to_book,
        "dividend_yield_pct": snap.dividend_yield_pct,
        "roe_pct": snap.roe_pct,
        "roce_pct": snap.roce_pct,
        "debt_to_equity": snap.debt_to_equity,
        "revenue_yearly_cr": snap.revenue_yearly_cr,
        "net_income_cr": snap.net_income_cr,
        "ebitda_cr": snap.ebitda_cr,
        "week_52_high": snap.week_52_high,
        "week_52_low": snap.week_52_low,
        "rsi_14": snap.rsi_14,
        "price": snap.price,
        "change": snap.change,
        "change_pct": snap.change_pct,
        "volume": snap.volume,
        "generated_at": snap.generated_at,
    }


def table_to_dicts(table: TimeSeriesTable) -> list[dict]:
    out = []
    for row in table.rows:
        rec: dict = {"_row_label": row.get("_row_label")}
        for col in table.period_header:
            rec[col] = row.get(col)
        out.append(rec)
    return out


def export_json(stock: StockData, path: Optional[str] = None) -> dict:
    """Serialize ``StockData`` to a JSON-friendly dict (and optionally write it)."""
    payload = {
        "symbol": stock.symbol,
        "snapshot": snapshot_to_dict(stock.snapshot),
        "tables": {label: {
            "label": t.label,
            "period_header": t.period_header,
            "rows": t.rows,
        } for label, t in stock.tables.items()},
        "source": f"{BASE_URL}/stocks/{stock.symbol}.md",
        "generated_at": stock.snapshot.generated_at,
    }
    if path:
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2, ensure_ascii=False)
    return payload


def export_csv(stock: StockData, table_label: str, path: str) -> None:
    """Write one statement table to a CSV file (periods as columns)."""
    table = stock.tables.get(table_label)
    if not table or not table.period_header:
        raise KeyError(f"No table named {table_label!r} in {stock.symbol}")

    with open(path, "w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(["Item"] + table.period_header)
        for row in table.rows:
            writer.writerow(
                [row.get("_row_label")] + [row.get(col) for col in table.period_header]
            )


def export_all_csv(stock: StockData, out_dir: str) -> list[str]:
    """Write every statement table to its own CSV file under *out_dir*."""
    import os

    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    for label, table in stock.tables.items():
        if not table.period_header:
            # Bullet-list sections (e.g. "Latest Price") have no table to export.
            continue
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_").lower() or "table"
        path = os.path.join(out_dir, f"{stock.symbol}_{safe}.csv")
        export_csv(stock, label, path)
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_snapshot(snap: FinancialSnapshot) -> None:
    def fmt_num(v) -> str:
        if v is None:
            return "—"
        if isinstance(v, float) and v == int(v):
            return f"{int(v)}"
        return f"{v}"

    def fmt_int(v) -> str:
        if v is None:
            return "—"
        return f"{int(v):,}"

    print(f"Symbol:            {snap.symbol}")
    print(f"Company:           {snap.company}")
    print(f"Sector / Industry: {snap.sector} / {snap.industry}")
    print(f"Market cap (₹ Cr): {fmt_num(snap.market_cap_cr)}")
    print(f"P/E (reported):    {fmt_num(snap.pe_reported)}")
    print(f"P/E (TTM):         {fmt_num(snap.pe_ttm)}")
    print(f"EPS (TTM):         {fmt_num(snap.eps_ttm)} ₹")
    print(f"EPS (reported):    {fmt_num(snap.eps_reported)} ₹")
    print(f"Book value:        {fmt_num(snap.book_value)} ₹")
    print(f"P/B:               {fmt_num(snap.price_to_book)}")
    print(f"Dividend yield:    {fmt_num(snap.dividend_yield_pct)}%")
    print(f"ROE / ROCE:        {fmt_num(snap.roe_pct)}% / {fmt_num(snap.roce_pct)}%")
    print(f"D/E:               {fmt_num(snap.debt_to_equity)}")
    print(f"Revenue (₹ Cr):    {fmt_num(snap.revenue_yearly_cr)}")
    print(f"Net income (₹ Cr): {fmt_num(snap.net_income_cr)}")
    print(f"EBITDA (₹ Cr):     {fmt_num(snap.ebitda_cr)}")
    print(f"52w high / low:    {fmt_num(snap.week_52_high)} / {fmt_num(snap.week_52_low)}")
    print(f"RSI(14):           {fmt_num(snap.rsi_14)}")
    print(f"Price / chg / %:   {fmt_num(snap.price)} / {fmt_num(snap.change)} / {fmt_num(snap.change_pct)}%")
    print(f"Volume:            {fmt_int(snap.volume)}")
    print(f"Fetched at:        {snap.generated_at}")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Download financial data from Tapetide's Markdown stock mirrors.",
    )
    parser.add_argument("symbol", nargs="?", help="Stock symbol, e.g. SBIN")
    parser.add_argument("--symbols", "-s", help="Comma-separated symbols (alternative to positional)")
    parser.add_argument("--json", metavar="PATH", help="Write structured JSON dump")
    parser.add_argument("--csv-dir", metavar="DIR", help="Write each statement table to a CSV file")
    parser.add_argument("--table", metavar="NAME", help="Write a single table to CSV (requires --csv-file)")
    parser.add_argument("--csv-file", metavar="PATH", help="CSV output path (used with --table)")
    parser.add_argument("--list-tables", action="store_true", help="Print available tables and their rows")
    parser.add_argument("--timeout", type=int, default=60, help="Per-request timeout in seconds")
    args = parser.parse_args(argv)

    symbols: list[str] = []
    if args.symbol:
        symbols.append(args.symbol)
    if args.symbols:
        symbols.extend([s.strip() for s in args.symbols.split(",") if s.strip()])
    if not symbols:
        parser.error("Specify a symbol (positional or --symbols).")

    exit_code = 0
    for i, sym in enumerate(symbols):
        if len(symbols) > 1 and i > 0:
            time.sleep(DELAY_BETWEEN_REQUESTS)
        try:
            stock = fetch_stock(sym, timeout_seconds=args.timeout)
        except Exception as exc:
            print(f"ERROR fetching {sym}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        if args.json:
            export_json(stock, args.json)
            print(f"Wrote JSON -> {args.json}", file=sys.stderr)

        if args.csv_dir:
            paths = export_all_csv(stock, args.csv_dir)
            for p in paths:
                print(f"Wrote CSV -> {p}", file=sys.stderr)

        if args.table and args.csv_file:
            export_csv(stock, args.table, args.csv_file)
            print(f"Wrote CSV -> {args.csv_file}", file=sys.stderr)

        _print_snapshot(stock.snapshot)

        if args.list_tables:
            print(f"\nTables for {stock.symbol}:")
            for label in stock.tables:
                print(f"  - {label}")

            for label, table in stock.tables.items():
                if not table.period_header:
                    continue
                print(f"\n## {label}")
                print(" | ".join(["Item"] + table.period_header))
                print(" | ".join(["---"] * (1 + len(table.period_header))))
                for row in table.rows:
                    cells = [str(row.get("_row_label") or "")]
                    cells += [str(row.get(col) or "") for col in table.period_header]
                    print(" | ".join(cells))
            print()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
