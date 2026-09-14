"""Offline smoke test for tapetide_downloader using a sample .md payload."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# The test writes JSON/CSV into out/ — create it if missing (fresh CI checkout).
os.makedirs("out", exist_ok=True)

from tapetide_downloader import (
    FinancialSnapshot,
    StockData,
    TimeSeriesTable,
    _parse_frontmatter,
    _parse_key_fundamentals_list,
    _parse_time_series_table,
    _split_md_sections,
    export_all_csv,
    export_json,
    table_to_dicts,
)

SAMPLE = """---
symbol: SBIN
company: State Bank of India
sector: Financial Services
industry: Banks
generated_at: 2026-09-13T09:30:00+05:30
---

# SBIN share price

## Latest Price

- **Price:** ₹995.7
- **Change:** -14 (-1.39%)
- **Volume:** 1,234,567

## Key Fundamentals

- **Market Cap:** ₹920107.96 Cr
- **P/E (reported):** 10.3
- **P/E (TTM):** 9.8
- **EPS (TTM):** 101.6
- **Dividend Yield:** 1.72%
- **ROE:** 18.2%
- **ROCE:** 12.5%
- **Debt to equity:** 1.58
- **52w high:** ₹998.5
- **52w low:** ₹555.1
- **RSI (14):** 55.4

## Profit & Loss

| Item | Mar 2024 | Mar 2025 | Mar 2026 |
|---|---|---|---|
| Revenue | 4,00,000 | 4,50,000 | 5,00,000 |
| Net Profit | 60,000 | 67,000 | 75,000 |

## Quarterly Results

| | Sep 2025 | Dec 2025 | Mar 2026 |
|---|---|---|---|
| Revenue | 1,10,000 | 1,20,000 | 1,30,000 |
| Net Profit | 17,000 | 18,500 | 20,000 |
"""


def main() -> int:
    meta, body = _parse_frontmatter(SAMPLE)
    assert meta["symbol"] == "SBIN", meta
    assert meta["company"] == "State Bank of India", meta

    sections = _split_md_sections(body)
    assert set(sections) == {"Latest Price", "Key Fundamentals", "Profit & Loss", "Quarterly Results"}, sections.keys()

    bullets = _parse_key_fundamentals_list(body)
    assert bullets["market_cap"] == 920107.96, bullets
    assert bullets["p_e_reported"] == 10.3, bullets  # raw key before _KEY_MAP routing
    assert bullets["p_e_ttm"] == 9.8, bullets
    assert bullets["change"] == -14.0, bullets
    assert bullets["change_pct"] == -1.39, bullets  # sign preserved from (-1.39%)
    assert bullets["dividend_yield"] == 1.72, bullets

    snap = FinancialSnapshot.from_frontmatter_and_body("SBIN", meta, body)
    assert snap.price == 995.7, snap.price
    assert snap.volume == 1234567, snap.volume
    assert snap.market_cap_cr == 920107.96, snap.market_cap_cr
    assert snap.pe_reported == 10.3, snap.pe_reported
    assert snap.pe_ttm == 9.8, snap.pe_ttm
    assert snap.eps_ttm == 101.6, snap.eps_ttm
    assert snap.dividend_yield_pct == 1.72, snap.dividend_yield_pct
    assert snap.roe_pct == 18.2, snap.roe_pct
    assert snap.roce_pct == 12.5, snap.roce_pct
    assert snap.debt_to_equity == 1.58, snap.debt_to_equity
    assert snap.week_52_high == 998.5, snap.week_52_high
    assert snap.week_52_low == 555.1, snap.week_52_low
    assert snap.rsi_14 == 55.4, snap.rsi_14
    assert snap.change_pct == -1.39, snap.change_pct

    tables = {label: _parse_time_series_table(label, text) for label, text in sections.items()}
    pl = tables["Profit & Loss"]
    assert pl.period_header == ["Mar 2024", "Mar 2025", "Mar 2026"], pl.period_header
    assert pl.rows[0]["_row_label"] == "Revenue", pl.rows[0]
    assert pl.rows[0]["Mar 2024"] == "4,00,000", pl.rows[0]

    qtr = tables["Quarterly Results"]
    assert qtr.period_header == ["Sep 2025", "Dec 2025", "Mar 2026"], qtr.period_header
    assert qtr.rows[1]["_row_label"] == "Net Profit", qtr.rows[1]
    assert qtr.rows[1]["Dec 2025"] == "18,500", qtr.rows[1]

    stock = StockData(
        symbol=snap.symbol,
        snapshot=snap,
        sections=sections,
        tables=tables,
        raw_markdown=SAMPLE,
    )

    payload = export_json(stock, path="out/sbin_smoke.json")
    assert payload["snapshot"]["pe_reported"] == 10.3
    assert "Profit & Loss" in payload["tables"]

    written = export_all_csv(stock, "out")
    assert any(p.endswith("SBIN_profit_loss.csv") for p in written), written
    assert any(p.endswith("SBIN_quarterly_results.csv") for p in written), written

    dicts = table_to_dicts(qtr)
    assert dicts[0]["_row_label"] == "Revenue", dicts[0]

    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
