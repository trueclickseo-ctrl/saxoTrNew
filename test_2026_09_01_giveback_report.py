"""
Regression test -- 2026-09-01 report_giveback.py (P2 give-back analysis).

Read-only report over data/trade_observation_cards.jsonl. Normalises MFE /
final P&L / give-back by initial risk, breaks it down by strategy and
account, and answers the "went our way then went bad" questions
(MFE>=1R -> final <0.25R, etc.). Skips the pre-2026-09-01 corrupted
(mae_mfe_invalidated) trades; buckets intraday (mae_mfe_coarse) separately.
"""

import json
import os
import sys

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


import report_giveback as rg

_CARDS = os.path.join(BASE_DIR, "data", "_test_giveback_cards.jsonl")
rg.CARDS = _CARDS


def _clean():
    if os.path.exists(_CARDS):
        os.remove(_CARDS)


def _pair(cid, strat, acct, risk, mfe, net, *, coarse=False, invalid=False):
    e = {"event": "entry", "card_id": cid, "strategy": strat, "account_env": acct,
         "symbol": "EURUSD", "risk_eur": risk}
    x = {"event": "exit", "card_id": cid, "mfe_eur": mfe, "mae_eur": -risk * 0.5,
         "net_pnl_eur": net, "r_multiple": round(net / risk, 2), "exit_reason": "tp"}
    if coarse:
        x["mae_mfe_coarse"] = True
    if invalid:
        x["mae_mfe_invalidated"] = "unbounded-daily-window-bug-2026-09-01"
    return [e, x]


def _write(pairs):
    with open(_CARDS, "w", encoding="utf-8") as f:
        for grp in pairs:
            for row in grp:
                f.write(json.dumps(row) + "\n")


def test_skips_invalidated_and_buckets_coarse():
    _clean()
    _write([
        _pair("a", "rsi", "sim", 100, 300, 50),                 # clean precise
        _pair("b", "gap", "sim", 100, 400, -20, coarse=True),   # coarse
        _pair("c", "rsi", "sim", 100, 9999, 10, invalid=True),  # corrupted -> skip
    ])
    s = rg.summarize()
    assert s["n_precise"] == 1 and s["n_coarse"] == 1
    assert s["overall"]["n"] == 1


def test_normalises_by_risk():
    _clean()
    _write([_pair("a", "rsi", "sim", 200, 600, 100)])           # MFE 3R, final 0.5R
    s = rg.summarize()
    o = s["overall"]
    assert o["avg_mfe_r"] == 3.0
    assert o["avg_final_r"] == 0.5
    assert o["avg_giveback_r"] == 2.5


def test_went_our_way_then_bad_rules():
    _clean()
    # 4 trades all reach >=2R MFE; 3 of them finish < 0R
    _write([
        _pair("a", "rsi", "sim", 100, 250, -30),
        _pair("b", "rsi", "sim", 100, 220, -10),
        _pair("c", "rsi", "sim", 100, 300, -50),
        _pair("d", "rsi", "sim", 100, 260, 120),
    ])
    rule = rg.summarize()["overall"]["rules"]["MFE>=2R -> final <0R"]
    assert rule["n_reached"] == 4 and rule["n_bad"] == 3 and rule["pct_bad"] == 75.0


def test_by_strategy_and_by_account_split():
    _clean()
    _write([
        _pair("a", "rsi", "sim", 100, 300, 20),
        _pair("b", "rsi", "live", 100, 300, 10),
        _pair("c", "supertrend", "sim", 100, 200, 180),
    ])
    s = rg.summarize()
    assert set(s["by_strategy"]) == {"rsi", "supertrend"}
    assert set(s["by_account"]) == {"sim", "live"}
    assert s["by_strategy"]["rsi"]["n"] == 2
    assert s["by_account"]["live"]["n"] == 1


def test_lifecycle_distribution():
    _clean()
    _write([
        _pair("a", "rsi", "sim", 100, 500, -50),   # loss, big MFE -> big give-back
        _pair("b", "rsi", "sim", 100, 150, 40),     # small win
        _pair("c", "rsi", "sim", 100, 300, 250),    # large win
    ])
    life = rg.summarize()["overall"]["lifecycle"]
    assert life["loss (<0R)"]["n"] == 1
    assert life["loss (<0R)"]["avg_giveback_r"] == 5.5     # MFE 5R - final -0.5R
    assert life["small win (0-1R)"]["n"] == 1
    assert life["large win (>=1R)"]["n"] == 1


def test_account_and_strategy_filters():
    _clean()
    _write([
        _pair("a", "rsi", "sim", 100, 300, 20),
        _pair("b", "ml", "live", 100, 300, 10),
    ])
    assert rg.summarize(account="live")["n_precise"] == 1
    assert rg.summarize(strategy="rsi")["n_precise"] == 1
    assert rg.summarize(account="live", strategy="rsi")["n_precise"] == 0


def test_empty_and_missing_file_safe():
    _clean()
    s = rg.summarize()
    assert s["n_total"] == 0 and s["overall"] == {"n": 0}


def test_cli_runs_clean():
    _clean()
    _write([_pair("a", "rsi", "sim", 100, 300, 20)])
    import subprocess
    p = subprocess.run([sys.executable, "report_giveback.py"], cwd=BASE_DIR,
                       capture_output=True, text=True, timeout=30,
                       env={**os.environ, "PYTHONUTF8": "1"})
    # note: the CLI reads the REAL cards file, not the test one -- just assert it doesn't crash
    assert p.returncode == 0


def test_report_is_read_only():
    import ast
    src = open(rg.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    assert not (imported & {"forex", "saxo_client", "saxo_order", "pnl_tracker",
                            "housekeeping", "ai"}), imported
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "open":
            mode = n.args[1].value if len(n.args) > 1 and isinstance(n.args[1], ast.Constant) else "r"
            assert "w" not in mode and "a" not in mode, "report_giveback must not write files"


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

_clean()
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
