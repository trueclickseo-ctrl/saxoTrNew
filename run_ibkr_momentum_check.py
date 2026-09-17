"""
run_ibkr_momentum_check.py
--------------------------
Twice-weekly momentum check for IBKR live blend positions.

Scores all currently held blend positions against the full universe.
Flags any position where score < EXIT_SCORE_THRESH or rank > EXIT_RANK_THRESH
so the user can decide whether to exit early rather than waiting for the next
scheduled rebalance.

Usage:
    python run_ibkr_momentum_check.py --live       # check live ISK account
    python run_ibkr_momentum_check.py              # check paper (default)

Scheduled: Monday + Thursday at 17:30 PKT (09:30 ET -- right after open).
Output:    printed report + logged to data/ibkr_momentum_check.log
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

# ── Thresholds ────────────────────────────────────────────────────────────────
EXIT_SCORE_THRESH = 55    # score below this → flag for exit
EXIT_RANK_THRESH  = 120   # rank worse than this → flag for exit

LOG_FILE = os.path.join(_ROOT, "data", "ibkr_momentum_check.log")


# ── Logging ───────────────────────────────────────────────────────────────────
class _Tee:
    def __init__(self, *streams):
        self._s = streams

    def write(self, d):
        for s in self._s:
            try:
                s.write(d)
            except Exception:
                pass

    def flush(self):
        for s in self._s:
            try:
                s.flush()
            except Exception:
                pass


def _setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    log_f = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
    tee = _Tee(sys.__stdout__, log_f)
    sys.stdout = tee
    sys.stderr = tee
    return log_f


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="IBKR blend momentum check")
    parser.add_argument("--live", action="store_true",
                        help="Use live ISK account DB (ibkr_live_stocks.db)")
    args = parser.parse_args()

    if args.live:
        live_db = os.path.join(_ROOT, "data", "ibkr_live_stocks.db")
        os.environ["IBKR_DB_PATH"] = live_db
        account_label = "LIVE ISK  U28013794"
    else:
        account_label = "PAPER"

    log_f = _setup_logging()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print()
    print("=" * 72)
    print(f"  IBKR Momentum Check  ·  {account_label}  ·  {now}")
    print(f"  Thresholds: score < {EXIT_SCORE_THRESH}  OR  rank > {EXIT_RANK_THRESH}")
    print("=" * 72)

    # ── 1. Read held positions ─────────────────────────────────────────────
    import ibkr_module.ibkr_state as st
    held = st.get_open_positions(strategy="blend")

    if not held:
        print("\n  No open blend positions — nothing to check.\n")
        log_f.close()
        return

    held_symbols = [p["symbol"] for p in held]
    print(f"\n  Held positions ({len(held)}): {', '.join(held_symbols)}\n")

    # ── 2. Run scorer ──────────────────────────────────────────────────────
    print("  Running scorer (Yahoo historical, ~30s)...")
    from ibkr_module.ibkr_scorer import run_scan
    result = run_scan(verbose=False)
    df = result["all_scored"]

    if df.empty:
        print("  [ERROR] Scorer returned no data — check Yahoo/internet connection.")
        log_f.close()
        sys.exit(1)

    df_sorted = df.sort_values("trade_score", ascending=False).reset_index(drop=True)

    # Build lookup: symbol -> (rank, score)
    rank_map: dict[str, tuple[int, float]] = {}
    for i, row in df_sorted.iterrows():
        # ticker column name varies; try common names
        sym = (row.get("ticker") or row.get("symbol") or row.get("Ticker") or "")
        if sym:
            rank_map[str(sym)] = (i + 1, float(row.get("trade_score", 0)))

    # ── 3. Build report ────────────────────────────────────────────────────
    flagged = []
    ok      = []

    print(f"  {'Symbol':<6}  {'Rank':>6}  {'Score':>6}  {'Entry':>8}  {'Stop':>8}  {'Days':>5}  Status")
    print("  " + "-" * 68)

    for pos in held:
        sym   = pos["symbol"]
        entry = pos.get("fill_price") or pos.get("limit_price") or 0
        stop  = pos.get("stop_price") or 0
        days  = 0
        if pos.get("filled_at"):
            try:
                filled_dt = datetime.fromisoformat(
                    pos["filled_at"].replace("Z", "+00:00")
                )
                days = (datetime.now(timezone.utc) - filled_dt).days
            except Exception:
                pass

        rank, score = rank_map.get(sym, (999, 0.0))
        is_flagged  = score < EXIT_SCORE_THRESH or rank > EXIT_RANK_THRESH
        status      = "** EXIT **" if is_flagged else "OK"

        print(f"  {sym:<6}  {rank:>6}  {score:>6.1f}  ${entry:>7.2f}  ${stop:>7.2f}  {days:>5}d  {status}")

        if is_flagged:
            flagged.append((sym, rank, score, entry, stop, days))
        else:
            ok.append(sym)

    print("  " + "-" * 68)

    # ── 4. Summary ─────────────────────────────────────────────────────────
    print()
    if flagged:
        print(f"  !! {len(flagged)} position(s) flagged for review:")
        for sym, rank, score, entry, stop, days in flagged:
            print(f"     {sym}: rank #{rank}, score {score:.1f}  "
                  f"(entry ${entry:.2f}, stop ${stop:.2f}, held {days}d)")
        print()
        print("  ACTION: review these positions and sell manually in IBKR if you agree.")
        print("  Stops are in place as a safety net if you choose to hold.")
    else:
        print(f"  All {len(ok)} position(s) are in momentum — no action needed.")

    print()
    print("=" * 72)
    print()

    log_f.close()


if __name__ == "__main__":
    main()
