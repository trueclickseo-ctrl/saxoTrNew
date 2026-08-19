"""
k4_export.py  —  Swedish K4 tax-form export
--------------------------------------------
Reads closed trades from pnl_ledger.db and produces a K4 report for
Skatteverket (Swedish Tax Authority).

K4 sections used by this system:
  A  — Aktier och andelar  (stocks, ETFs traded on regulated markets)
  C  — Delägarrätter       (futures, CFDs, forex)

Each trade row:
  Beteckning         instrument name
  Antal              quantity sold/closed
  Försäljningspris   proceeds in SEK
  Omkostnadsbelopp   cost in SEK
  Vinst / Förlust    gain or loss in SEK

Currency conversion: USD → SEK using daily USDSEK rate on the close date
(Skatteverket requires transaction-date rate; yearly average is also accepted).
Uses yfinance for historical rates; falls back to a fixed rate if offline.

Usage:
    python k4_export.py --year 2025              # report for tax year 2025
    python k4_export.py --year 2025 --csv        # also write k4_2025.csv
    python k4_export.py --year 2025 --rate 11.20 # override SEK/USD rate
    python k4_export.py --year 2025 --module forex  # one module only
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, date, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "data", "pnl_ledger.db")

# Fallback SEK/USD rate when yfinance is unavailable or offline
FALLBACK_USDSEK = 10.50   # approximate; update if stale

# K4 section assignment per module
# A = shares/ETFs  C = derivatives (futures, forex CFDs)
MODULE_SECTION = {
    "stock":   "A",
    "etf":     "A",
    "futures": "C",
    "forex":   "C",
}

MODULE_DESC = {
    "stock":   "Aktier (US-aktier via Saxo)",
    "etf":     "ETF-andelar (Saxo)",
    "futures": "Terminskontrakt / CFD (Saxo)",
    "forex":   "Valutakontrakt / CFD (Saxo FX)",
}


# ── USDSEK rate lookup ─────────────────────────────────────────────────────────

_rate_cache: dict[str, float] = {}

def _usdsek_on(close_date: str, fixed_rate: float = None) -> float:
    """Return USDSEK rate for a given date string (YYYY-MM-DD).

    Priority: fixed_rate override → yfinance daily close → cache → fallback.
    """
    if fixed_rate:
        return fixed_rate
    if close_date in _rate_cache:
        return _rate_cache[close_date]
    try:
        import yfinance as yf
        d   = date.fromisoformat(close_date[:10])
        end = d + timedelta(days=3)   # +3 to catch weekends/holidays
        df  = yf.download("USDSEK=X", start=str(d), end=str(end),
                          auto_adjust=True, progress=False)
        if not df.empty:
            rate = float(df["Close"].iloc[0])
            _rate_cache[close_date] = rate
            return rate
    except Exception:
        pass
    _rate_cache[close_date] = FALLBACK_USDSEK
    return FALLBACK_USDSEK


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_trades(year: int, module: str = None) -> list[dict]:
    """Load closed trades for the given calendar year from pnl_ledger.db."""
    if not os.path.exists(DB_PATH):
        print(f"[k4_export] DB not found: {DB_PATH}")
        return []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    q    = """
        SELECT *
          FROM trades
         WHERE status = 'closed'
           AND timestamp_close >= ?
           AND timestamp_close <  ?
    """
    args = [f"{year}-01-01", f"{year+1}-01-01"]
    if module:
        q += " AND module = ?"
        args.append(module)
    q += " ORDER BY timestamp_close"

    rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    conn.close()
    return rows


# ── K4 row building ────────────────────────────────────────────────────────────

def _build_k4_rows(trades: list[dict], fixed_rate: float = None) -> list[dict]:
    """Convert raw trade rows to K4 presentation rows."""
    rows = []
    for t in trades:
        mod       = t.get("module", "")
        section   = MODULE_SECTION.get(mod, "C")
        currency  = t.get("currency", "USD")
        qty       = t.get("quantity", 0) or 0
        ep        = t.get("entry_price", 0) or 0
        xp        = t.get("exit_price",  0) or 0
        net_pnl   = t.get("realized_pnl", 0) or 0   # already net of commission
        comm      = t.get("commission",   0) or 0
        close_dt  = (t.get("timestamp_close") or "")[:10]
        symbol    = t.get("symbol", "?")
        direction = t.get("direction", "Buy")

        # ── USD → SEK conversion ──────────────────────────────────────────────
        if currency == "SEK":
            rate = 1.0
        else:
            rate = _usdsek_on(close_dt, fixed_rate)

        if direction in ("Buy", "BUY"):
            proceeds_usd = qty * xp
            cost_usd     = qty * ep
        else:   # Sell (short position)
            proceeds_usd = qty * ep   # shorted at entry = received
            cost_usd     = qty * xp   # closed at exit   = paid back

        proceeds_sek = proceeds_usd * rate
        cost_sek     = cost_usd     * rate
        comm_sek     = comm         * rate
        # Realized gain/loss in SEK: proceeds − cost − commission
        gain_sek = proceeds_sek - cost_sek - comm_sek
        loss_sek = 0.0
        if gain_sek < 0:
            loss_sek = abs(gain_sek)
            gain_sek = 0.0

        rows.append({
            "section":      section,
            "module":       mod,
            "symbol":       symbol,
            "direction":    direction,
            "quantity":     qty,
            "close_date":   close_dt,
            "currency":     currency,
            "usdsek_rate":  rate,
            "proceeds_sek": round(proceeds_sek, 0),
            "cost_sek":     round(cost_sek,     0),
            "commission_sek": round(comm_sek,   0),
            "gain_sek":     round(gain_sek,     0),
            "loss_sek":     round(loss_sek,     0),
            "strategy":     t.get("strategy", ""),
            "exit_reason":  t.get("exit_reason", ""),
        })
    return rows


# ── Report printing ────────────────────────────────────────────────────────────

def print_k4(rows: list[dict], year: int):
    BD = "\033[1m"; W = "\033[0m"; CY = "\033[96m"
    GR = "\033[92m"; RD = "\033[91m"; DM = "\033[2m"

    print(f"\n{BD}{CY}{'═'*80}{W}")
    print(f"{BD}{CY}  K4 — KAPITALVINSTER OCH KAPITALFÖRLUSTER  {year}  (Bilaga K4){W}")
    print(f"{BD}{CY}  Skatteverket — Inkomstdeklaration 1{W}")
    print(f"{BD}{CY}{'═'*80}{W}\n")

    for section in ("A", "C"):
        section_rows = [r for r in rows if r["section"] == section]
        if not section_rows:
            continue

        label = ("Aktier och andelar (Section A)"
                 if section == "A"
                 else "Delägarrätter / Terminer / CFD (Section C)")
        print(f"  {BD}{label}{W}")
        print(f"  {'─'*78}")
        hdr = (f"  {'Beteckning':<18} {'Datum':<12} {'Antal':>8} "
               f"{'Försäljn.':>12} {'Anskaffn.':>12} "
               f"{'Vinst':>10} {'Förlust':>10}")
        print(f"  {DM}{hdr}{W}")
        print(f"  {'─'*78}")

        tot_proceeds = tot_cost = tot_gain = tot_loss = 0.0

        for r in section_rows:
            name = f"{r['symbol']} ({r['module'].upper()})"
            g    = r["gain_sek"]
            l    = r["loss_sek"]
            tot_proceeds += r["proceeds_sek"]
            tot_cost     += r["cost_sek"]
            tot_gain     += g
            tot_loss     += l
            gc = GR if g > 0 else ""
            lc = RD if l > 0 else ""
            print(f"  {name:<18} {r['close_date']:<12} {r['quantity']:>8,.0f} "
                  f"{r['proceeds_sek']:>12,.0f} {r['cost_sek']:>12,.0f} "
                  f"{gc}{g:>10,.0f}{W} {lc}{l:>10,.0f}{W}  SEK")

        print(f"  {'─'*78}")
        net = tot_gain - tot_loss
        nc  = GR if net >= 0 else RD
        ns  = "+" if net >= 0 else ""
        print(f"  {'SUMMA':<18} {'':<12} {'':<8} "
              f"{tot_proceeds:>12,.0f} {tot_cost:>12,.0f} "
              f"{GR}{tot_gain:>10,.0f}{W} {RD}{tot_loss:>10,.0f}{W}  SEK")
        print(f"\n  {BD}Netto section {section}: {nc}{BD}{ns}{net:,.0f} SEK{W}")
        if net >= 0:
            tax = net * 0.30
            print(f"  {DM}Skatt (30% på vinst): {tax:,.0f} SEK{W}")
        else:
            deduct = abs(net) * 0.70
            print(f"  {DM}Avdrag (70% av förlust): {deduct:,.0f} SEK{W}")
        print()

    # Grand totals
    all_gain = sum(r["gain_sek"] for r in rows)
    all_loss = sum(r["loss_sek"] for r in rows)
    net_all  = all_gain - all_loss
    nc = GR if net_all >= 0 else RD
    print(f"  {'═'*78}")
    print(f"  {BD}TOTALT KAPITALRESULTAT {year}{W}")
    print(f"  Kapitalvinster:   {GR}{BD}{all_gain:>12,.0f} SEK{W}")
    print(f"  Kapitalförluster: {RD}{BD}{all_loss:>12,.0f} SEK{W}")
    print(f"  Netto:            {nc}{BD}{net_all:>+12,.0f} SEK{W}")
    if net_all >= 0:
        print(f"\n  {BD}Skatt att betala (30%): {net_all*0.30:,.0f} SEK{W}")
    else:
        print(f"\n  {BD}Förlustavdrag (70%): {abs(net_all)*0.70:,.0f} SEK{W}")
        print(f"  {DM}(Kvittas mot kapitalinkomster; ev. underskottsavdrag 21% mot slutskatt){W}")
    print(f"  {'═'*78}\n")

    print(f"  {DM}Obs: Valutakurs USD→SEK hämtad per stängningsdagen (yfinance USDSEK=X).")
    print(f"  Kontrollera kurser mot Riksbankens referenskurser om så krävs.{W}\n")


# ── CSV export ─────────────────────────────────────────────────────────────────

def write_csv(rows: list[dict], year: int):
    import csv
    path = os.path.join(BASE_DIR, f"k4_{year}.csv")
    fields = ["section", "module", "symbol", "direction", "quantity",
              "close_date", "usdsek_rate", "proceeds_sek", "cost_sek",
              "commission_sek", "gain_sek", "loss_sek", "strategy", "exit_reason"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"  CSV sparad: {path}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="K4 Swedish tax export from pnl_ledger.db")
    ap.add_argument("--year",   type=int,   default=date.today().year - 1,
                    help="Tax year (default: previous calendar year)")
    ap.add_argument("--module", choices=["stock","etf","futures","forex"],
                    help="Filter to one module")
    ap.add_argument("--rate",   type=float, default=None,
                    help="Fixed USD/SEK rate to use instead of historical lookup")
    ap.add_argument("--csv",    action="store_true",
                    help="Also write k4_YEAR.csv")
    args = ap.parse_args()

    print(f"\n  Läser trades för skatteår {args.year}…", end="", flush=True)
    trades = _load_trades(args.year, args.module)
    print(f" {len(trades)} stängda affärer hittades.")

    if not trades:
        print("  Inga stängda affärer — K4 behövs inte.\n")
        return

    print(f"  Hämtar USDSEK-kurser…", end="", flush=True)
    rows = _build_k4_rows(trades, fixed_rate=args.rate)
    print(" klar.")

    print_k4(rows, args.year)

    if args.csv:
        write_csv(rows, args.year)


if __name__ == "__main__":
    main()
