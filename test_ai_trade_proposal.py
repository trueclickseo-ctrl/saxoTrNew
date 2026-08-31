"""
Sprint 2 test gate -- ai/features/trade_proposal.py + the inert
log-only hook in forex/runner._run_entries.

Contract: for a signal that passed every deterministic filter, a
structured proposal (roadmap schema + regime) is appended to
data/ai_trade_proposals.jsonl -- ONLY when ai_enabled_for(env) is True,
never changes anything downstream, never raises out of the entry loop.
"""

import inspect
import json
import os
import sys

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, RESET, BOLD = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_results = []


def _run(name, fn):
    try:
        fn()
        _results.append((name, True, None))
    except Exception as e:
        import traceback
        _results.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


from ai.features.trade_proposal import build_proposal, log_proposal, REQUIRED_FIELDS, PROPOSALS_LOG
import ai.config as aic
import forex.runner as r


def _daily_uptrend(n=210):
    base = 1.0 + np.linspace(0, 0.05, n)
    return pd.DataFrame({"High": base + 0.0015, "Low": base - 0.0015, "Close": base})


_SIG = {"symbol": "EURUSD", "direction": "Buy", "close": 1.15,
        "stop_price": 1.14, "atr": 0.006, "score": 7.0, "rsi": 3.2}
_FEAT = {"agreement_count": 4, "ml_prob": 0.61}
_POS = {"rsi:GBPUSD": {"direction": "Buy", "quantity": 10000},
        "ema:USDJPY": {"direction": "Sell", "quantity": 5000}}


# ═══════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}1. build_proposal(): schema + robustness{RESET}")
# ═══════════════════════════════════════════════════════════════════════

def test_proposal_has_every_required_field():
    p = build_proposal(account_env="sim", strategy="rsi", symbol="EURUSD", direction="Buy",
                       sig=_SIG, features=_FEAT, positions=_POS, equity=700_000,
                       take_profit=1.17, n_strategies=18, regime_bars=_daily_uptrend())
    missing = [f for f in REQUIRED_FIELDS if f not in p]
    assert not missing, f"missing required fields: {missing}"
    assert p["side"] == "BUY" and p["entry_price"] == 1.15 and p["stop_loss"] == 1.14
    assert p["timeframe"] == "D1"
    assert 0.0 <= p["signal_strength"] <= 1.0
    assert p["regime"]["label"] in ("TRENDING_BULLISH", "TRENDING_BEARISH", "RANGING",
                                    "BREAKOUT", "HIGH_VOLATILITY", "LOW_VOLATILITY",
                                    "CHAOTIC", "UNKNOWN")
    assert p["n_open_positions"] == 2 and p["open_positions"][0]["side"] in ("BUY", "SELL")
_run("build_proposal output carries every required field + a regime block", test_proposal_has_every_required_field)


def test_lbo_signal_is_h1_timeframe():
    p = build_proposal(account_env="sim", strategy="london_breakout", symbol="EURJPY",
                       direction="Sell", sig={"close": 185.0, "stop_price": 185.8, "atr": 0.6,
                                              "range_pips": 40}, features={},
                       positions={}, equity=1400, take_profit=183.4, n_strategies=18)
    assert p["timeframe"] == "H1"
    assert p["regime"]["label"] == "UNKNOWN"   # no bars passed
_run("an LBO signal gets timeframe H1; missing regime bars -> label UNKNOWN, no crash",
     test_lbo_signal_is_h1_timeframe)


def test_missing_optional_fields_dont_crash():
    p = build_proposal(account_env="sim", strategy="donchian", symbol="AUDUSD", direction="Buy",
                       sig={"close": 0.71, "stop_price": 0.70, "atr": 0.004},  # no score/rsi
                       features={}, positions={}, equity=0, take_profit=None,
                       n_strategies=0, regime_bars=None)
    assert p["rsi2"] is None and p["raw_score"] is None
    assert p["signal_strength"] is None and p["take_profit"] is None
    assert p["account_equity"] is None
    json.dumps(p, default=str)   # must be serialisable
_run("a bare signal (no score / rsi / tp / equity) still builds a serialisable proposal",
     test_missing_optional_fields_dont_crash)


# ═══════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}2. the runner hook is inert unless AI is enabled{RESET}")
# ═══════════════════════════════════════════════════════════════════════

def test_hook_is_guarded_and_cannot_change_entries():
    src = inspect.getsource(r._run_entries)
    start = src.index("if ai_config.ai_enabled_for(ACCOUNT_ENV):")
    hook = src[start: src.index("if not _currency_ok")]
    assert "ai_config.ai_enabled_for(ACCOUNT_ENV)" in hook, "hook must be behind the kill switch"
    assert "try:" in hook and "except Exception" in hook, "hook must never raise into the loop"
    # it comes AFTER the signal_filter pass and BEFORE sizing/order placement
    sf = src.index("signal_filter.evaluate(")
    hk = src.index("_ai_log_proposal(")
    sz = src.index("strat_mod.size_position(")
    assert sf < hk < sz, "hook must sit after the filter gate and before sizing"
    # the hook block must not touch order flow / position mutation
    for forbidden in ("entries += 1", "positions[pos_key]", "place_with_stop", "_post(", "size_position"):
        assert forbidden not in hook, f"hook must not contain {forbidden!r}"
_run("the _run_entries hook is kill-switch-guarded, wrapped in try/except, and downstream-inert",
     test_hook_is_guarded_and_cannot_change_entries)


def test_default_off_writes_nothing():
    assert aic.ai_enabled_for("sim") is False, "AI ships OFF"
    before = os.path.getsize(PROPOSALS_LOG) if os.path.exists(PROPOSALS_LOG) else -1
    # simulate what the hook does when disabled: nothing
    if aic.ai_enabled_for("sim"):
        log_proposal({"x": 1})
    after = os.path.getsize(PROPOSALS_LOG) if os.path.exists(PROPOSALS_LOG) else -1
    assert before == after
_run("with AI off (default), the proposal log is never written", test_default_off_writes_nothing)


def test_log_proposal_appends_when_called():
    p = os.path.join(BASE_DIR, "data", "_test_ai_proposals.jsonl")
    import ai.features.trade_proposal as tp
    old = tp.PROPOSALS_LOG
    tp.PROPOSALS_LOG = p
    try:
        if os.path.exists(p):
            os.remove(p)
        prop = build_proposal(account_env="sim", strategy="rsi", symbol="EURUSD", direction="Buy",
                              sig=_SIG, features=_FEAT, positions={}, equity=1000,
                              take_profit=1.17, n_strategies=18, regime_bars=_daily_uptrend())
        tp.log_proposal(prop)
        rec = json.loads(open(p).read().strip())
        assert rec["symbol"] == "EURUSD" and rec["strategy_name"] == "rsi"
    finally:
        tp.PROPOSALS_LOG = old
        if os.path.exists(p):
            os.remove(p)
_run("log_proposal() appends a well-formed jsonl row", test_log_proposal_appends_when_called)


def test_hook_is_only_hook_in_runner():
    src = open(os.path.join(BASE_DIR, "forex", "runner.py"), encoding="utf-8").read()
    # exactly one build + one log call -- Sprint 2 adds one hook, not many
    assert src.count("_ai_build_proposal(") == 1 and src.count("_ai_log_proposal(") == 1
_run("Sprint 2 adds exactly ONE proposal hook to forex/runner.py", test_hook_is_only_hook_in_runner)


print(f"\n{BOLD}{'='*66}{RESET}")
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    print(f"  [{GREEN}PASS{RESET}]" if ok else f"  [{RED}FAIL{RESET}]", name)
    if err:
        print(f"      {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*66}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} FAILED{RESET}")
    sys.exit(1)
print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
sys.exit(0)
