"""
scripts/ibkr_scan_signals.py
-----------------------------
Dry-run scanner: score all 492 universe stocks and print signal counts.
No orders are placed. No IBKR connection required.

Usage:
    python scripts/ibkr_scan_signals.py
    python scripts/ibkr_scan_signals.py --min-score 65 --top 30
    python scripts/ibkr_scan_signals.py --save            # also writes CSV outputs
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from ibkr_module.ibkr_scorer import run_scan


def _grade_bar(score: float) -> str:
    filled = int(score / 5)
    return "█" * filled + "░" * (20 - filled)


def _print_table(df, title: str, cols: list[str]) -> None:
    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print("=" * 72)
    if df.empty:
        print("  (no candidates)")
        return

    widths = {c: max(len(c), 6) for c in cols}
    header = "  " + "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("  " + "-" * (sum(widths.values()) + 2 * len(cols)))
    for _, row in df.iterrows():
        parts = []
        for c in cols:
            val = row.get(c, "")
            if isinstance(val, float):
                parts.append(f"{val:6.1f}".ljust(widths[c]))
            else:
                parts.append(str(val).ljust(widths[c]))
        print("  " + "  ".join(parts))


def main() -> None:
    parser = argparse.ArgumentParser(description="ATOS US 500 Signal Scanner (dry-run)")
    parser.add_argument("--min-score", type=float, default=60.0,
                        help="Minimum trade_score to include (default 60)")
    parser.add_argument("--top", type=int, default=20,
                        help="Max candidates per universe type (default 20)")
    parser.add_argument("--save", action="store_true",
                        help="Save scored CSVs to data/scorer_*.csv")
    args = parser.parse_args()

    print("=" * 72)
    print("  ATOS US 500 — Signal Scanner (DRY RUN, no orders placed)")
    print("=" * 72)

    results = run_scan(
        n_swing=args.top,
        n_portfolio=args.top,
        min_score=args.min_score,
        verbose=True,
    )

    scored  = results["all_scored"]
    swing   = results["swing"]
    port    = results["portfolio"]

    # ── Universe summary ─────────────────────────────────────────────────────
    print("\n── Universe snapshot ──────────────────────────────────────────────")
    if not scored.empty:
        total    = len(scored)
        gated    = int(scored.hard_gate.sum())
        a_plus   = int((scored.setup == "A+").sum())
        a        = int((scored.setup == "A").sum())
        b        = int((scored.setup == "B").sum())
        c        = int((scored.setup == "C").sum())
        above_60 = int((scored.trade_score >= 60).sum())
        above_65 = int((scored.trade_score >= 65).sum())
        above_75 = int((scored.trade_score >= 75).sum())

        print(f"  Tickers with data  : {total}")
        print(f"  Pass hard gates    : {gated}")
        print(f"  Grade A+           : {a_plus}  (trade_score ≥ 85)")
        print(f"  Grade A            : {a}   (≥ 75)")
        print(f"  Grade B            : {b}  (≥ 65)")
        print(f"  Grade C            : {c}  (≥ 55)")
        print(f"  Above 75 (buy zone): {above_75}")
        print(f"  Above 65           : {above_65}")
        print(f"  Above 60 (min)     : {above_60}")

        # Gate failure breakdown
        failed = scored[~scored.hard_gate]
        if not failed.empty:
            print(f"\n  Hard-gate failures : {len(failed)}")
            for reason, cnt in failed.gate_reason.value_counts().head(5).items():
                print(f"    {reason}: {cnt}")

    # ── Top Swing/Momentum candidates ────────────────────────────────────────
    _print_table(
        swing, f"Top {args.top} SWING / MOMENTUM  (rank by swing_score)",
        ["ticker", "price", "setup", "swing_score", "trade_score",
         "roc_20d", "atr_pct", "adx_14", "rs_vs_spy_20d"],
    )

    # ── Top Portfolio candidates ──────────────────────────────────────────────
    _print_table(
        port, f"Top {args.top} HYBRID / PORTFOLIO  (rank by trade_score)",
        ["ticker", "price", "setup", "trade_score", "swing_score",
         "roc_20d", "sma200_distance_pct", "adx_14", "rs_vs_spy_20d"],
    )

    # ── Signal summary ────────────────────────────────────────────────────────
    print(f"\n── Signal summary (min_score={args.min_score}) ────────────────────────────")
    print(f"  Swing candidates   : {len(swing)}")
    print(f"  Portfolio candidates: {len(port)}")
    print(f"  Total actionable   : {len(swing) + len(port)}")

    if not swing.empty:
        top_s = swing.iloc[0]
        print(f"\n  Best swing pick    : {top_s['ticker']}  "
              f"swing={top_s['swing_score']:.1f}  trade={top_s['trade_score']:.1f}  "
              f"grade={top_s['setup']}")
    if not port.empty:
        top_p = port.iloc[0]
        print(f"  Best portfolio pick: {top_p['ticker']}  "
              f"trade={top_p['trade_score']:.1f}  swing={top_p['swing_score']:.1f}  "
              f"grade={top_p['setup']}")

    # ── Save CSVs ─────────────────────────────────────────────────────────────
    if args.save:
        data_dir = _ROOT / "data"
        data_dir.mkdir(exist_ok=True)
        scored.to_csv(data_dir / "scorer_all.csv",   index=False)
        swing.to_csv( data_dir / "scorer_swing.csv", index=False)
        port.to_csv(  data_dir / "scorer_portfolio.csv", index=False)
        print(f"\n  Saved: data/scorer_all.csv, scorer_swing.csv, scorer_portfolio.csv")

    # ── Email notification ────────────────────────────────────────────────────
    if not swing.empty or not port.empty:
        try:
            from atos.notifier import notify_scorer_signals

            # Rename 'setup' → 'grade' for the email template
            def _prep(df):
                if df.empty:
                    return df
                out = df.copy()
                if "setup" in out.columns and "grade" not in out.columns:
                    out = out.rename(columns={"setup": "grade"})
                return out

            sent = notify_scorer_signals(
                swing_df=_prep(swing),
                portfolio_df=_prep(port),
                min_score=args.min_score,
            )
            if not sent:
                print("  [notifier] email skipped (no config or duplicate for today)")
        except Exception as exc:
            print(f"  [notifier] email error: {exc}")

    print("\n" + "=" * 72)
    print("  DRY RUN COMPLETE — no orders placed.")
    print("=" * 72)


if __name__ == "__main__":
    main()
