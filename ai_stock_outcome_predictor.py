"""
ai_stock_outcome_predictor.py
------------------------------
CLI wrapper for the Stock Trade Outcome Predictor.

Usage:
    python ai_stock_outcome_predictor.py             # status (default)
    python ai_stock_outcome_predictor.py --status    # show card count + gate
    python ai_stock_outcome_predictor.py --train     # train the model
    python ai_stock_outcome_predictor.py --report    # show last training report

Gate: 50 closed non-orphaned stock trades from active strategies.
Sibling of ai_outcome_predictor.py (forex TOP).
"""

from __future__ import annotations

import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


def _status() -> None:
    from ai.models.stock_outcome_predictor import status, MIN_SAMPLES
    s = status()
    n   = s["n_closed_cards"]
    need = s["needed_for_train"]
    gate = s["gate_cleared"]
    exists = s["model_exists"]
    print(f"Stock Outcome Predictor — status")
    print(f"  Active-strategy stock cards : {n}")
    if gate:
        print(f"  Gate                       : CLEARED (>= {MIN_SAMPLES})")
    else:
        print(f"  Gate                       : NOT cleared ({need} more needed to train)")
    if exists:
        ta = s.get("trained_at") or "—"
        wr = s.get("test_win_prec")
        bwr = s.get("base_win_rate")
        lift = s.get("lift")
        acc = s.get("test_accuracy")
        print(f"  Model                      : EXISTS (trained {ta})")
        if wr is not None:
            print(f"  Test win-precision          : {wr*100:.1f}%  (base WR {bwr*100:.1f}%)")
        if lift is not None:
            print(f"  Lift                        : {lift*100:+.1f}%")
        if acc is not None:
            print(f"  Test accuracy               : {acc*100:.1f}%")
    else:
        print(f"  Model                      : not trained yet")

    cfg_path = os.path.join(BASE_DIR, "config", "ai.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        enabled = cfg.get("stock_outcome_predictor", {}).get("enabled", False)
    except Exception:
        enabled = False
    print(f"  config/ai.json enabled      : {enabled}")
    print()
    if not gate:
        print(f"  Accumulating data — check again in a few days after more stock trades close.")
    elif exists and not enabled:
        print("  Next step: review --report, then set config/ai.json stock_outcome_predictor.enabled=true")
    elif not exists and gate:
        print("  Gate cleared — run: python ai_stock_outcome_predictor.py --train")


def _train() -> None:
    try:
        from sklearn.ensemble import GradientBoostingClassifier  # noqa: F401
    except ImportError:
        print("ERROR: scikit-learn not installed. Install it and retry.")
        sys.exit(1)

    from ai.models.stock_outcome_predictor import train, MIN_SAMPLES
    print("Training Stock Outcome Predictor...")
    result = train(min_samples=MIN_SAMPLES)
    if not result.get("trained"):
        reason = result.get("reason") or result.get("error") or "unknown"
        print(f"Not trained: {reason}")
        return
    print(f"Trained on {result['n_train']} trades (test set: {result['n_test']})")
    print(f"  Test accuracy   : {result['test_accuracy']*100:.1f}%")
    print(f"  Win precision   : {result['test_win_prec']*100:.1f}%")
    print(f"  Base win-rate   : {result['base_win_rate']*100:.1f}%")
    lift = result.get("lift", 0)
    if lift >= 0.05:
        verdict = "meaningful edge"
    elif lift >= 0.02:
        verdict = "marginal edge — accumulate more data"
    else:
        verdict = "no edge yet — do not enable"
    print(f"  Lift            : {lift*100:+.1f}%  ({verdict})")
    print()
    feats = result.get("top_features", [])
    if feats:
        print("  Top features:")
        for fname, imp in feats[:5]:
            print(f"    {fname:<30} {imp:.4f}")
    print()
    print("Model saved → data/stock_outcome_model/model.pkl + report.json")
    if lift >= 0.05:
        print("Lift >= +5%: consider enabling via config/ai.json stock_outcome_predictor.enabled=true")
    else:
        print("Lift < +5%: keep disabled — accumulate more trades first.")


def _report() -> None:
    from ai.models.stock_outcome_predictor import REPORT_JSON
    if not os.path.exists(REPORT_JSON):
        print("No report found. Run: python ai_stock_outcome_predictor.py --train")
        return
    try:
        with open(REPORT_JSON, encoding="utf-8") as f:
            r = json.load(f)
    except Exception as e:
        print(f"Could not read report: {e}")
        return

    print(f"Stock Outcome Predictor — last training report ({r.get('trained_at','')[:10]})")
    print(f"  Trades used     : {r.get('n_total')}  (train {r.get('n_train')}, test {r.get('n_test')})")
    print(f"  Test accuracy   : {r.get('test_accuracy',0)*100:.1f}%")
    print(f"  Win precision   : {r.get('test_win_prec',0)*100:.1f}%")
    print(f"  Base win-rate   : {r.get('base_win_rate',0)*100:.1f}%")
    lift = r.get("lift", 0)
    if lift >= 0.05:
        interp = "meaningful edge — model worth enabling"
    elif lift >= 0.02:
        interp = "marginal — accumulate more data"
    else:
        interp = "no edge yet — do not enable"
    print(f"  Lift            : {lift*100:+.1f}%  ({interp})")
    print()
    feats = r.get("top_features", [])
    if feats:
        print("  Top predictive features:")
        for fname, imp in feats:
            print(f"    {fname:<30} {imp:.4f}")


def main() -> None:
    args = sys.argv[1:]
    if not args or "--status" in args:
        _status()
    elif "--train" in args:
        _train()
    elif "--report" in args:
        _report()
    else:
        print(f"Unknown args: {args}")
        print("Usage: python ai_stock_outcome_predictor.py [--status|--train|--report]")
        sys.exit(1)


if __name__ == "__main__":
    main()
