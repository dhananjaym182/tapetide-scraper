"""Offline test for screener_scraper parsers (no network needed)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

from bs4 import BeautifulSoup

from screener_scraper import (
    export_csvs,
    export_json,
    num,
    parse_all_sections,
    parse_company,
    parse_pros_cons,
    parse_top_ratios,
)

HTML = """
<html><body>
<h1>State Bank of India share price</h1>
<ul id="top-ratios">
  <li><span class="name">Market Cap </span> <span class="nowrap"> ₹ 9,20,107 </span></li>
  <li><span class="name">Stock P/E </span> <span class="nowrap"> 10.9 </span></li>
  <li><span class="name">Dividend Yield </span> <span class="nowrap"> 1.74% </span></li>
</ul>
<section id="quarters">
  <table class="data-table">
    <thead><tr><th></th><th>Jun 2023 +</th><th>Sep 2023 +</th><th>Dec 2023 +</th></tr></thead>
    <tbody>
      <tr><td class="text">Revenue +</td><td class="number">1,01,460</td><td class="number">1,07,391</td><td class="number">1,12,868</td></tr>
      <tr><td class="text">Net Profit +</td><td class="number">17,925</td><td class="number">18,079</td><td class="number">18,312</td></tr>
    </tbody>
  </table>
</section>
<section id="profit-loss">
  <table class="data-table">
    <thead><tr><th></th><th>Mar 2015</th><th>Mar 2016</th></tr></thead>
    <tbody>
      <tr><td class="text">Sales +</td><td class="number">2,07,974</td><td class="number">2,20,633</td></tr>
    </tbody>
  </table>
</section>
<section id="balance-sheet"></section>
<div class="pros"><p class="title">Pros</p><ul><li>Healthy dividend payout</li></ul></div>
<div class="cons"><p class="title">Cons</p><ul><li>Low interest coverage ratio</li></ul></div>
<ul class="list-links">
  <li><a href="/uploads/annual_report.pdf">Annual Report 2025</a></li>
</ul>
</body></html>
"""


def main() -> int:
    soup = BeautifulSoup(HTML, "html.parser")

    # value normalisation
    assert num("1,01,460") == 101460, num("1,01,460")
    assert num("1.74%") == 1.74
    assert num("₹ 995.7") == 995.7
    assert num("-") is None and num("") is None and num(None) is None

    # top ratios
    ratios = parse_top_ratios(soup)
    assert ratios["Market Cap"] == 920107, ratios
    assert ratios["Stock P/E"] == 10.9
    assert ratios["Dividend Yield"] == 1.74

    # sections: headers keep years, footnote '+' stripped
    secs = parse_all_sections(soup)
    qr = secs["quarterly_results"]
    assert qr["headers"] == ["Jun 2023", "Sep 2023", "Dec 2023"], qr["headers"]
    rev = qr["rows"][0]
    assert rev["label"] == "Revenue"
    assert rev["values"] == [101460, 107391, 112868], rev["values"]
    assert secs["profit_loss"]["headers"] == ["Mar 2015", "Mar 2016"]
    assert secs["balance_sheet"]["rows"] == []

    # pros/cons + docs
    pc = parse_pros_cons(soup)
    assert pc["pros"] == ["Healthy dividend payout"], pc
    assert pc["cons"] == ["Low interest coverage ratio"], pc

    # full record + exports
    record = parse_company("SBIN", "consolidated", soup,
                           "https://www.screener.in/company/SBIN/consolidated/")
    assert record["name"] == "State Bank of India"
    assert record["documents"][0]["url"] == "/uploads/annual_report.pdf"

    with tempfile.TemporaryDirectory() as tmp:
        jp = os.path.join(tmp, "SBIN.json")
        export_json(record, jp)
        loaded = json.load(open(jp))
        assert loaded["symbol"] == "SBIN"
        paths = export_csvs(record, tmp)
        assert any(p.endswith("SBIN_quarterly_results.csv") for p in paths), paths
        csv_text = open(os.path.join(tmp, "SBIN_quarterly_results.csv")).read()
        assert "Jun 2023" in csv_text and "101460.0" in csv_text

    print("SCREENER SCRAPER OFFLINE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
