"""
atos_ai_stocks.py
-----------------
AI-DECISION stocks twin (2026-09-03). A SIM **paper** US Blend book that
trades the AI basket-ranker's re-ranked pick (`ai_offense[:ai_count]`)
instead of the deterministic top-N -- a live forward A/B vs the deterministic
SIM stocks book (`atos_runner.run_cycle` -> `atos_live.db`).

SEPARATE from atos_live_stocks.py (real money) and the SIM `atos_runner`:
  * ledger      data/atos_ai.db                    (ATOS_DB_PATH)
  * blend clock data/us_momentum_state_ai.json     (ATOS_US_MOMENTUM_STATE)
  * risk state  data/atos_ai_risk_state.json       (ATOS_RISK_STATE_FILE)
  * lock        proc_lock.ATOS_AI_STOCKS_LOCK

Everything is paper (SIM `_STOCKS_ENV`, `STOCKS_SIM_PAPER_FILL_ON_REJECT`) --
no real orders. `account_env="ai_sim"` makes `run_us_momentum` swap in the
AI basket (ai/config.basket_ranker_applies("ai_sim") is the only True).

Usage:
    python atos_ai_stocks.py            # one rebalance/overlay cycle (paper)
    python atos_ai_stocks.py --dashboard   # ai_dashboard.py
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

os.environ.setdefault("ATOS_DB_PATH",            os.path.join(_ROOT, "data", "atos_ai.db"))
os.environ.setdefault("ATOS_RISK_STATE_FILE",    os.path.join(_ROOT, "data", "atos_ai_risk_state.json"))
os.environ.setdefault("ATOS_US_MOMENTUM_STATE",  os.path.join(_ROOT, "data", "us_momentum_state_ai.json"))

import proc_lock
import saxo_client
import atos.capital_config as CAP

STATUS_FILE = os.path.join(_ROOT, "data", "atos_ai_stocks_status.json")


def _blend_budget_sek() -> float:
    """Same SIM blend budget the deterministic book uses (BLEND_CASH_PCT of
    live SIM cash, capped at starting_capital), so the A/B is like-for-like."""
    import atos_runner as ar
    try:
        bal = saxo_client.get_balances()
        cash = float(bal.get("CashBalance", 0) or 0) * ar._rate_to_sek(bal.get("Currency", "EUR"))
    except Exception:
        cash = ar.get_risk_capital()
    cap = CAP.starting_capital_sek() * CAP.max_deploy_pct()
    return min(cash * ar.BLEND_CASH_PCT, cap * ar.BLEND_CASH_PCT)


def _write_status(result: dict, budget: float) -> None:
    import json
    sig = result.get("signal") or {}
    payload = {
        "status": "complete", "timestamp": datetime.now().isoformat(),
        "account_env": "ai_sim", "budget_sek": round(budget, 2),
        "signal": {"targets": sig.get("targets", []), "risk_off": sig.get("risk_off", False),
                   "reason": sig.get("reason", ""), "momentum": sig.get("momentum", []),
                   "lowvol": sig.get("lowvol", [])},
        "actions": result.get("actions", []),
        "buy": result.get("buy", 0), "sell": result.get("sell", 0),
        "book_state": result.get("book_state", {}),
    }
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, STATUS_FILE)
    except Exception as exc:
        print(f"  [ai stocks] status write failed: {exc}")


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dashboard", action="store_true", help="open ai_dashboard.py")
    ap.add_argument("--fast", action="store_true", help="with --dashboard: 5s refresh")
    ap.add_argument("--once", action="store_true", help="with --dashboard: print once")
    args = ap.parse_args(argv)

    if args.dashboard or args.fast or args.once:
        import subprocess
        extra = (["--fast"] if args.fast else []) + (["--once"] if args.once else [])
        return subprocess.call([sys.executable, "-X", "utf8",
                                os.path.join(_ROOT, "ai_dashboard.py"), *extra])

    print(f"\n{'='*60}\n[AI SIM STOCKS] US Blend twin — {datetime.now():%Y-%m-%d %H:%M:%S}\n{'='*60}")
    if not proc_lock.acquire(proc_lock.ATOS_AI_STOCKS_LOCK, "atos_ai_stocks"):
        print("  could not acquire the ai-stocks lock — proceeding unprotected")
    try:
        import atos_runner
        import ai.config as ai_config
        if not ai_config.basket_ranker_applies("ai_sim"):
            print("  [ai stocks] stocks_ai / agent not enabled -- nothing to do "
                  "(config/ai.json enabled_ai_sim + stocks_ai.enabled + agent_enabled)")
            return 0
        budget = _blend_budget_sek()
        print(f"  budget {budget:,.0f} SEK  (same % of SIM cash as the deterministic book)")
        result = atos_runner.run_us_blend_ai(budget_sek=budget)
        print(f"  [AI SIM STOCKS] done — {result['buy']} buy / {result['sell']} sell "
              f"(paper, data/atos_ai.db)")
        _write_status(result, budget)
        return 0
    finally:
        proc_lock.release(proc_lock.ATOS_AI_STOCKS_LOCK)


if __name__ == "__main__":
    raise SystemExit(run())
