"""
backfill_regime_at_entry.py -- one-time (or ad-hoc) backfill of
`regime_at_entry` / `regime_fit` on the open `rsi:` and `rsi_trend:`
positions in the SIM + LIVE forex state files.

The runner stamps these at ENTRY going forward (forex/runner._run_entries,
2026-09-02). Positions opened before that show "?" in the dashboard's
"RSI SIGNAL QUALITY" division. This classifies each open RSI position's
CURRENT daily bars (a proxy -- the true entry-bar regime is gone) and
writes the fields so the division is fully populated now.

    python backfill_regime_at_entry.py            # dry run -- prints, writes nothing
    python backfill_regime_at_entry.py --apply    # write the state files

Read-only w.r.t. trading (only edits the two JSON state files, only adds
two annotation keys, never a price / stop / quantity). Never places an
order.
"""
import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

STATE_FILES = [
    os.path.join(_ROOT, "data", "forex_state.json"),
    os.path.join(_ROOT, "data", "forex_live_state.json"),
]
_WANT = {"Buy": "TRENDING_BULLISH", "Sell": "TRENDING_BEARISH"}


def _classify(uic):
    import forex.runner as runner
    from ai.regime.classifier import classify_regime
    df = runner._fetch_history(uic)
    if df is None or len(df) < 65:
        return None
    return classify_regime(df).get("label")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the state files (default: dry run)")
    args = ap.parse_args()

    import forex.runner as runner
    runner.set_account_env("sim")
    from forex.universe import get_pair

    for path in STATE_FILES:
        if not os.path.exists(path):
            continue
        state = json.load(open(path, encoding="utf-8"))
        positions = state.get("positions", {})
        changed = 0
        print(f"\n{os.path.basename(path)}")
        for key, pos in positions.items():
            strat = key.split(":", 1)[0]
            if strat not in ("rsi", "rsi_trend"):
                continue
            if pos.get("regime_at_entry"):
                print(f"  {key:<22} already stamped ({pos['regime_at_entry']})")
                continue
            sym = key.split(":", 1)[1]
            direction = pos.get("direction", "Buy")
            try:
                uic = pos.get("uic") or get_pair(sym)["uic"]
                label = _classify(uic)
            except Exception as e:
                print(f"  {key:<22} classify failed: {e}")
                continue
            if not label:
                print(f"  {key:<22} no bars")
                continue
            fit = label == _WANT.get(direction)
            print(f"  {key:<22} {direction:<4} -> {label:<18} fit={fit}")
            pos["regime_at_entry"] = label
            pos["regime_fit"] = bool(fit)
            pos["regime_backfilled"] = True   # this is a CURRENT-bar proxy, not the true entry-bar regime
            changed += 1
        if changed and args.apply:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, path)
            print(f"  -> wrote {changed} position(s)")
        elif changed:
            print(f"  -> {changed} position(s) would be stamped (--apply to write)")
        else:
            print("  -> nothing to do")


if __name__ == "__main__":
    main()
