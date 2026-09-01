"""
Phase 2 of reports/live_pair_commission_analysis.py -- turns
data/live_pair_commission_analysis.json into a formatted .xlsx.

    py -3.12 reports/live_pair_commission_analysis_xlsx.py
"""
import json
import os

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "data", "live_pair_commission_analysis.json")
OUT = os.path.join(BASE, "data", "live_pair_commission_analysis.xlsx")

HDR_FILL = PatternFill("solid", fgColor="1F4E78")
HDR_FONT = Font(color="FFFFFF", bold=True, size=10)
OK_FILL = PatternFill("solid", fgColor="C6EFCE")
WARN_FILL = PatternFill("solid", fgColor="FFEB9C")
BAD_FILL = PatternFill("solid", fgColor="FFC7CE")
TITLE_FONT = Font(bold=True, size=13)
NOTE_FONT = Font(italic=True, size=9, color="555555")

COLS = [
    ("pair", "Pair", 10),
    ("tier", "Tier", 18),
    ("price", "Price", 10),
    ("stop_pct", "RSI stop %", 10),
    ("commission_eur_roundtrip", "Commission €\n(round trip)", 12),
    ("units_at_E45", "Units\n@ €45 risk", 11),
    ("notional_eur_at_E45", "Notional €\n@ €45 risk", 12),
    ("realised_risk_eur", "Realised\nrisk €", 10),
    ("commission_pct_of_notional", "Commission\n% of notional", 12),
    ("tp_2R_gross_eur", "TP (2R)\ngross €", 10),
    ("tp_2R_net_after_comm_eur", "TP (2R) net\nafter comm €", 12),
    ("bounce_0p5R_gross_eur", "0.5R bounce\ngross €", 11),
    ("bounce_0p5R_net_after_comm_eur", "0.5R bounce net\nafter comm €", 13),
    ("breakeven_bounce_R", "Break-even\nbounce (R)", 11),
    ("recommended_min_notional_eur", "Rec. MIN\nnotional €", 11),
    ("recommended_min_units", "Rec. MIN\nunits", 10),
    ("verdict", "Verdict", 34),
]


def main():
    data = json.load(open(SRC, encoding="utf-8"))
    rows = data["rows"]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "LIVE pair economics"

    ws["A1"] = "ATOS LIVE — per-pair commission economics at €45 fixed RSI risk"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = (f"Generated {data['generated'][:19]}  ·  RISK_EUR={data['risk_eur']}  ·  "
                f"TP={data['tp_rr']}R  ·  min-notional floor €{data['min_notional_floor']:,.0f}  ·  "
                f"target commission ≤ {data['target_comm_pct']*100:.2f}% of notional")
    ws["A2"].font = NOTE_FONT
    ws["A3"] = ("MXNUSD lesson: a +€2.13 gross price move netted −€3.05 because the flat "
                "~€5.19 round-trip commission was 0.48% of the tiny ~€1,090 position. "
                "'0.5R bounce net' is the key column — a typical RSI(2) exit is a small "
                "bounce, not the full 2R take-profit; it must stay comfortably positive.")
    ws["A3"].font = NOTE_FONT

    hr = 5
    for c, (_, label, width) in enumerate(COLS, 1):
        cell = ws.cell(row=hr, column=c, value=label)
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        ws.column_dimensions[get_column_letter(c)].width = width
    ws.row_dimensions[hr].height = 30
    ws.freeze_panes = f"A{hr+1}"

    for i, r in enumerate(rows):
        rr = hr + 1 + i
        for c, (key, _, _) in enumerate(COLS, 1):
            ws.cell(row=rr, column=c, value=r.get(key))
        v = (r.get("verdict") or "")
        fill = OK_FILL if v == "OK" else (BAD_FILL if ("BELOW" in v or "THIN" in v) else WARN_FILL)
        ws.cell(row=rr, column=len(COLS)).fill = fill

    # a small summary block
    sr = hr + 2 + len(rows)
    live = [r for r in rows if str(r.get("tier", "")).startswith("HIGH_VOLUME")]
    thin = [r for r in live if (r.get("verdict") or "") != "OK"]
    ws.cell(row=sr, column=1, value="LIVE (HIGH_VOLUME) pairs:").font = Font(bold=True)
    ws.cell(row=sr, column=2, value=len(live))
    ws.cell(row=sr+1, column=1, value="  of those, not 'OK':").font = Font(bold=True)
    ws.cell(row=sr+1, column=2, value=len(thin))
    ws.cell(row=sr+1, column=3, value=", ".join(r["pair"] for r in thin) or "—")

    wb.save(OUT)
    print(f"wrote {OUT}  ({len(rows)} pairs, {len(thin)} live pairs flagged)")


if __name__ == "__main__":
    main()
