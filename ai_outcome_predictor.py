"""
ai_outcome_predictor.py
-----------------------
CLI for the Trade Outcome Predictor (TOP).

  python ai_outcome_predictor.py            # default: status
  python ai_outcome_predictor.py --status   # data readiness + model info
  python ai_outcome_predictor.py --train    # fit model on current closed cards
  python ai_outcome_predictor.py --report   # walk-forward stats + feature importances

The TOP replaces the CNN-LSTM's approach (predict raw price direction from Yahoo
bars) with a GradientBoosting classifier trained on our actual closed trade
observation cards -- predicting whether a new entry will produce a positive
R-multiple, using features already logged at entry time (regime, RSI, ADX,
strategy, spread-cost ratio, pair's own historical win rate).

See ai/models/trade_outcome_predictor.py for the full architecture.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai.models.trade_outcome_predictor import (
    train, status, REPORT_JSON, MODEL_PKL, MIN_SAMPLES,
)


def _cli_status() -> None:
    s = status()
    print("\n=== Trade Outcome Predictor STATUS ===")
    n = s["n_closed_cards"]
    if s["gate_cleared"]:
        print(f"  Active-strategy cards : {n}  (gate CLEARED >= {MIN_SAMPLES})")
    else:
        print(f"  Active-strategy cards : {n}  ({s['needed_for_train']} more needed to train)")
        print(f"  (counts rsi/rsi_trend/rsi_atr/ema_trend/bb_quality/zscore_quality only)")
    print(f"  Model on disk      : {'YES' if s['model_exists'] else 'NO'}")
    if s["model_exists"]:
        print(f"  Trained at         : {s['trained_at'] or 'unknown'}")
        if s["test_accuracy"] is not None:
            print(f"  Test accuracy      : {s['test_accuracy']:.1%}")
        if s["test_win_prec"] is not None:
            bwr = s["base_win_rate"] or 0.0
            lft = s["lift"] or 0.0
            print(f"  Win precision      : {s['test_win_prec']:.1%}  "
                  f"(base WR {bwr:.1%}  lift {lft:+.1%})")
        if s["n_train"]:
            print(f"  Train/test split   : {s['n_train']} / {s['n_test']}")
        if s["top_features"]:
            print("  Top features       :", ", ".join(f[0] for f in s["top_features"][:4]))
    print()


def _cli_train() -> None:
    print("\nTraining Trade Outcome Predictor...")
    result = train()
    if not result.get("trained"):
        reason = result.get("reason") or result.get("error") or "unknown"
        print(f"  Not trained: {reason}")
        avail = result.get("n_available")
        if avail is not None:
            need = result.get("n_needed", MIN_SAMPLES)
            print(f"  Closed cards available: {avail}/{need}")
        return

    print(f"  n_train  = {result['n_train']}   n_test = {result['n_test']}")
    bwr = result.get("base_win_rate", 0.0)
    wp  = result.get("test_win_prec", 0.0)
    lft = result.get("lift", 0.0)
    print(f"  Accuracy        : {result.get('test_accuracy', 0):.1%}")
    print(f"  Win precision   : {wp:.1%}  (base WR {bwr:.1%}  lift {lft:+.1%})")
    print(f"  Loss precision  : {result.get('test_loss_prec', 0):.1%}")
    print(f"  Top features:")
    for feat, imp in result.get("top_features", []):
        bar = "#" * int(imp * 80)
        print(f"    {feat:<38} {imp:.4f}  {bar}")
    print(f"\n  Model saved: {MODEL_PKL}")
    print(f"  Report saved: {REPORT_JSON}")


def _cli_report() -> None:
    if not os.path.exists(REPORT_JSON):
        print("No report found.  Run  python ai_outcome_predictor.py --train  first.")
        return
    try:
        with open(REPORT_JSON, encoding="utf-8") as f:
            report = json.load(f)
    except Exception as exc:
        print(f"Could not read report: {exc}")
        return

    print("\n=== Trade Outcome Predictor REPORT ===")
    print(f"  Trained at      : {str(report.get('trained_at', ''))[:16]}")
    print(f"  Training set    : {report.get('n_train')} trades")
    print(f"  Test set        : {report.get('n_test')} trades")
    bwr = report.get("base_win_rate", 0.0)
    wp  = report.get("test_win_prec", 0.0)
    lp  = report.get("test_loss_prec", 0.0)
    lft = report.get("lift", 0.0)
    print(f"  Accuracy        : {report.get('test_accuracy', 0):.1%}")
    print(f"  Win precision   : {wp:.1%}  (base {bwr:.1%}  lift {lft:+.1%})")
    print(f"  Loss precision  : {lp:.1%}")

    top = report.get("top_features", [])
    if top:
        print(f"\n  Feature importances (top {len(top)}):")
        max_imp = top[0][1] if top else 1.0
        for feat, imp in top:
            bar = "#" * max(1, int(imp / max_imp * 40))
            print(f"    {feat:<38} {imp:.4f}  {bar}")

    # Interpretation guide
    print()
    print("  Gate for SIM influence:")
    print(f"    win precision ({wp:.1%}) vs base WR ({bwr:.1%})")
    if lft >= 0.05:
        print(f"    lift = {lft:+.1%} -> meaningful edge; consider wiring into proposals")
    elif lft >= 0.02:
        print(f"    lift = {lft:+.1%} -> marginal; accumulate more data before relying on it")
    else:
        print(f"    lift = {lft:+.1%} -> no significant edge yet; keep accumulating")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Trade Outcome Predictor CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--train",  action="store_true", help="Train on current closed cards")
    ap.add_argument("--report", action="store_true", help="Show walk-forward report + importances")
    ap.add_argument("--status", action="store_true", help="Show data readiness + model state")
    args = ap.parse_args()

    if args.train:
        _cli_train()
    elif args.report:
        _cli_report()
    else:
        _cli_status()


if __name__ == "__main__":
    main()
