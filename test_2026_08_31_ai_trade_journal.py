"""
AI Trading Journal test gate -- roadmap #18, ai/features/trade_journal.py.

HARD REQUIREMENT (user, 2026-08-31): the journal must be READ-ONLY with
respect to trading state during the evidence/shadow phase -- it may
observe, analyse, score and learn from trades, but must never modify an
order, position, stop, or strategy decision. These tests lock that in:

  * the module imports nothing that can trade (no forex.runner, saxo_*,
    pnl_tracker mutators, housekeeping, order placement);
  * its source contains no order/position-mutation calls;
  * the ONLY path it writes is data/ai_trade_journal.jsonl;
  * generate() never raises and degrades to empty narratives;
  * run() is gated by config/ai.json journal_enabled;
  * everything runs strictly after a trade has closed (paired exit card).
"""

import ast
import json
import os
import sys
from datetime import datetime, timezone

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


import ai.config as aic
import ai.features.trade_journal as tj

_CARDS = os.path.join(BASE_DIR, "data", "_test_journal_cards.jsonl")
_PROP = os.path.join(BASE_DIR, "data", "_test_journal_proposals.jsonl")
_SHADOW = os.path.join(BASE_DIR, "data", "_test_journal_shadow.jsonl")
_EA = os.path.join(BASE_DIR, "data", "_test_journal_ea.jsonl")
_JOURNAL = os.path.join(BASE_DIR, "data", "_test_journal_out.jsonl")

tj.CARDS_LOG, tj.PROPOSALS_LOG, tj.SHADOW_LOG, tj.EXIT_ADVISOR_LOG, tj.JOURNAL_LOG = (
    _CARDS, _PROP, _SHADOW, _EA, _JOURNAL)

_ALL = (_CARDS, _PROP, _SHADOW, _EA, _JOURNAL)


def _clean():
    for p in _ALL:
        if os.path.exists(p):
            os.remove(p)


def _w(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _entry(cid, strat="rsi", sym="EURUSD", day="2026-08-31", acct="sim"):
    return {"event": "entry", "card_id": cid, "timestamp": f"{day}T09:00:00+00:00",
            "account_env": acct, "strategy": strat, "symbol": sym, "direction": "Buy",
            "entry_price": 1.10, "atr_at_entry": 0.001, "current_stop": 1.095,
            "quantity": 10000, "risk_eur": 45.0}


def _exit(cid, day="2026-08-31", net=50.0, r=1.5):
    return {"event": "exit", "card_id": cid, "timestamp": f"{day}T15:00:00+00:00",
            "exit_price": 1.11, "exit_reason": "take_profit", "gross_pnl_eur": net,
            "net_pnl_eur": net, "r_multiple": r, "mae_eur": -20.0, "mfe_eur": 70.0,
            "holding_hours": 6.0}


# ─────────────────────────────────────────────────────────────────────────
# READ-ONLY ENFORCEMENT -- the user's hard requirement
# ─────────────────────────────────────────────────────────────────────────
def test_module_imports_nothing_that_can_trade():
    src = open(tj.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    # anthropic is imported lazily inside generate(); that's fine
    forbidden = {"forex", "futures", "saxo_client", "saxo_order", "saxo_auth",
                 "housekeeping", "safeguard", "atos_runner", "intraday_monitor",
                 "trade_logger", "strategy_learner"}
    hit = imported & forbidden
    assert not hit, f"trade_journal must not import trade-capable modules: {hit}"
    # pnl_tracker is borderline (read helpers exist) -- assert it's not imported
    # at all, to keep the read-only guarantee trivially auditable
    assert "pnl_tracker" not in imported, "trade_journal must not import pnl_tracker"


def test_source_has_no_order_mutation_calls():
    src = open(tj.__file__, encoding="utf-8").read()
    for bad in ("place_with_stop", "place_order", "_amend_stop_order", "cancel_order",
                "cancel_all", "_post(", "_delete(", "close_position", "modify_position",
                "saxo_order", "reconcile_", "update_stop"):
        assert bad not in src, f"read-only violation: source references {bad!r}"


def test_only_writes_its_own_journal_file():
    src = open(tj.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    # every open(...) in write/append mode must target JOURNAL_LOG
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
            mode = ""
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            if any(c in mode for c in ("w", "a", "x", "+")):
                target = node.args[0]
                assert isinstance(target, ast.Name) and target.id == "JOURNAL_LOG", \
                    f"write-mode open() targets something other than JOURNAL_LOG (mode={mode!r})"


def test_writing_only_touches_journal(monkeypatch=None):
    _clean()
    aic_real = aic._CONFIG_PATH
    tmp_cfg = os.path.join(BASE_DIR, "config", "_test_journal_cfg.json")
    with open(tmp_cfg, "w") as f:
        json.dump({"journal_enabled": True}, f)
    aic._CONFIG_PATH = tmp_cfg
    try:
        _w(_CARDS, [_entry("c1"), _exit("c1")])
        before = {p: (os.path.getmtime(p) if os.path.exists(p) else None)
                  for p in (_CARDS, _PROP, _SHADOW, _EA)}
        # stub the LLM so no network
        orig = tj.generate
        tj.generate = lambda ds, day_context=None: {"trades": {}, "day_summary": "s",
                                                    "_agent": {"ok": True, "error": None}}
        try:
            tj.run()
        finally:
            tj.generate = orig
        after = {p: (os.path.getmtime(p) if os.path.exists(p) else None)
                 for p in (_CARDS, _PROP, _SHADOW, _EA)}
        assert before == after, "run() modified an input file"
        assert os.path.exists(_JOURNAL), "run() did not write the journal"
    finally:
        aic._CONFIG_PATH = aic_real
        os.path.exists(tmp_cfg) and os.remove(tmp_cfg)


# ─────────────────────────────────────────────────────────────────────────
# behaviour
# ─────────────────────────────────────────────────────────────────────────
def _cfg(**kw):
    real = aic._CONFIG_PATH
    tmp = os.path.join(BASE_DIR, "config", "_test_journal_cfg2.json")
    with open(tmp, "w") as f:
        json.dump(kw, f)
    aic._CONFIG_PATH = tmp
    return real, tmp


def _restore_cfg(real, tmp):
    aic._CONFIG_PATH = real
    os.path.exists(tmp) and os.remove(tmp)


def test_run_gated_by_journal_enabled():
    _clean()
    _w(_CARDS, [_entry("c1"), _exit("c1")])
    real, tmp = _cfg(journal_enabled=False)
    try:
        assert tj.run()["status"] == "disabled"
        assert not os.path.exists(_JOURNAL)
    finally:
        _restore_cfg(real, tmp)


def test_build_dossiers_pairs_and_joins():
    _clean()
    _w(_CARDS, [_entry("c1"), _exit("c1"),
                _entry("c2", sym="GBPUSD"), _exit("c2", net=-30.0, r=-1.0),
                _entry("c3", sym="AUDUSD")])  # c3 still open -> excluded
    _w(_SHADOW, [{"trade_id": "sim|rsi|EURUSD|2026-08-31", "regime": "RANGING",
                  "agent_action": "MODIFY", "agent_size_multiplier": 0.5,
                  "agent_comment": "cautious"}])
    _w(_EA, [{"card_id": "c1", "recommendation": "EXIT"},
             {"card_id": "c1", "recommendation": "HOLD"}])
    ds = tj.build_dossiers()
    assert len(ds) == 2, [d["card_id"] for d in ds]
    d1 = next(d for d in ds if d["card_id"] == "c1")
    assert d1["regime_at_entry"] == "RANGING"
    assert d1["ai_action"] == "MODIFY" and d1["ai_size_multiplier"] == 0.5
    assert d1["exit_advisor_exit_flags"] == 1
    assert d1["net_pnl_eur"] == 50.0


def test_journal_covers_sim_and_both_live_accounts():
    # the journal has NO account filter -- SIM + live + live_eur forex trades
    # all get journaled (every forex trade writes an observation card).
    _clean()
    cards = []
    for acct in ("sim", "live", "live_eur"):
        cid = f"{acct}:rsi:EURUSD:2026-08-31T09:00:00+00:00"
        cards += [_entry(cid, acct=acct), _exit(cid)]
    _w(_CARDS, cards)
    ds = tj.build_dossiers()
    assert {d["account_env"] for d in ds} == {"sim", "live", "live_eur"}
    # a real LIVE trade carries through to the day context too
    ctx = tj._day_context_row(next(d for d in ds if d["account_env"] == "live"))
    assert ctx["account"] == "live"


def test_dossier_carries_mae_mfe_quality_flags():
    _clean()
    e = _entry("c1"); x = _exit("c1")
    x["mae_mfe_coarse"] = True
    x["mae_mfe_invalidated"] = "unbounded-daily-window-bug-2026-09-01"
    _w(_CARDS, [e, x])
    d = tj.build_dossiers()[0]
    assert d["mae_mfe_coarse"] is True
    assert "2026-09-01" in d["mae_mfe_note"]


def test_build_dossiers_skips_already_journaled():
    _clean()
    _w(_CARDS, [_entry("c1"), _exit("c1"), _entry("c2", sym="GBPUSD"), _exit("c2")])
    _w(_JOURNAL, [{"event": "trade", "card_id": "c1"}])
    ds = tj.build_dossiers()
    assert [d["card_id"] for d in ds] == ["c2"]


def test_build_dossiers_since_and_limit():
    _clean()
    _w(_CARDS, [_entry("c1", day="2026-08-29"), _exit("c1", day="2026-08-29"),
                _entry("c2", sym="G", day="2026-08-31"), _exit("c2", day="2026-08-31")])
    assert [d["card_id"] for d in tj.build_dossiers(since="2026-08-30")] == ["c2"]
    real, tmp = _cfg(journal_max_trades_per_run=1)
    try:
        assert len(tj.build_dossiers()) == 1
    finally:
        _restore_cfg(real, tmp)


def test_generate_never_raises_without_sdk_key():
    # no ANTHROPIC_API_KEY in the test env -> anthropic.Anthropic() raises ->
    # generate() must catch it and return ok=False, not blow up
    out = tj.generate([{"card_id": "c1", "symbol": "EURUSD"}])
    assert out["trades"] == {} and out["_agent"]["ok"] is False
    assert out["day_summary"] is None


def test_generate_empty_dossiers():
    out = tj.generate([])
    assert out["_agent"]["ok"] is False and "no dossiers" in out["_agent"]["error"]


def test_extract_json_tolerates_prose():
    assert tj._extract_json('{"a":1}') == {"a": 1}
    assert tj._extract_json('here you go:\n{"a": 1}\ndone') == {"a": 1}
    assert tj._extract_json("not json at all") is None


def test_extract_json_strips_code_fence():
    assert tj._extract_json('```json\n{"trades": [], "day_summary": "x"}\n```') == \
        {"trades": [], "day_summary": "x"}


def test_extract_json_salvages_truncated_response():
    # response cut off mid-array -- must still recover the complete objects
    truncated = ('```json\n{\n "trades": [\n'
                 '  {"card_id": "c1", "entry_quality": "good", "exit_quality": "fair",'
                 ' "why_result": "w1", "lesson": "l1", "tags": ["a"]},\n'
                 '  {"card_id": "c2", "entry_quality": "poor", "exit_quality": "poor",'
                 ' "why_result": "w2", "lesson": "none", "tags": []},\n'
                 '  {"card_id": "c3", "entry_qua')
    got = tj._extract_json(truncated)
    assert got is not None and got.get("_truncated") is True
    ids = {t["card_id"] for t in got["trades"]}
    assert ids == {"c1", "c2"}, ids   # c3 was incomplete -> dropped, not guessed


def test_log_day_writes_trades_and_summary():
    _clean()
    ds = [{"card_id": "c1", "day": "2026-08-31", "account_env": "sim", "strategy": "rsi",
           "symbol": "EURUSD", "direction": "Buy", "net_pnl_eur": 50.0, "r_multiple": 1.5,
           "exit_reason": "tp", "holding_hours": 6.0, "regime_at_entry": "RANGING",
           "ai_action": "MODIFY", "ai_size_multiplier": 0.5}]
    result = {"trades": {"c1": {"entry_quality": "good", "exit_quality": "fair",
                                "why_result": "trend held", "lesson": "none",
                                "tags": ["clean-trend"]}},
              "day_summary": "One winner, RANGING regime.",
              "_agent": {"ok": True, "error": None, "model": "m"}}
    n = tj._log_day("2026-08-31", ds, result)
    assert n == 1
    rows = [json.loads(l) for l in open(_JOURNAL, encoding="utf-8")]
    trade = next(r for r in rows if r["event"] == "trade")
    summ = next(r for r in rows if r["event"] == "day_summary")
    assert trade["entry_quality"] == "good" and trade["tags"] == ["clean-trend"]
    assert trade["narrated"] is True
    assert summ["n_trades"] == 1 and summ["net_eur"] == 50.0
    # journaled_card_ids picks it up (dedup)
    assert tj.journaled_card_ids() == {"c1"}


def test_log_day_logs_trade_the_model_omitted():
    # call SUCCEEDED but the model dropped this trade from its array -> still
    # logged (un-narrated) so it isn't re-sent to the model every run
    _clean()
    ds = [{"card_id": "cX", "day": "2026-08-31", "account_env": "sim", "strategy": "rsi",
           "symbol": "EURUSD", "direction": "Buy", "net_pnl_eur": -10.0, "r_multiple": -0.3,
           "exit_reason": "stop", "holding_hours": 2.0, "regime_at_entry": None,
           "ai_action": None, "ai_size_multiplier": None}]
    result = {"trades": {}, "day_summary": "quiet day", "_agent": {"ok": True, "error": None}}
    tj._log_day("2026-08-31", ds, result)
    row = json.loads(open(_JOURNAL, encoding="utf-8").readline())
    assert row["narrated"] is False and row["entry_quality"] is None
    assert row["card_id"] == "cX"


def test_literal_none_summary_not_written():
    _clean()
    ds = [{"card_id": "c1", "day": "2026-08-31", "account_env": "sim", "strategy": "rsi",
           "symbol": "EURUSD", "direction": "Buy", "net_pnl_eur": 1.0, "r_multiple": 0.1,
           "exit_reason": "tp", "holding_hours": 1.0, "regime_at_entry": None,
           "ai_action": None, "ai_size_multiplier": None}]
    for bad in ("None", "null", "", "  n/a "):
        _clean()
        tj._log_day("2026-08-31", ds, {"trades": {}, "day_summary": bad,
                                       "_agent": {"ok": True}})
        rows = [json.loads(l) for l in open(_JOURNAL, encoding="utf-8")]
        assert not any(r["event"] == "day_summary" for r in rows), bad


def test_no_duplicate_day_summary_across_chunks_or_reruns():
    _clean()
    ds = [{"card_id": "c1", "day": "2026-08-31", "account_env": "sim", "strategy": "rsi",
           "symbol": "EURUSD", "direction": "Buy", "net_pnl_eur": 1.0, "r_multiple": 0.1,
           "exit_reason": "tp", "holding_hours": 1.0, "regime_at_entry": None,
           "ai_action": None, "ai_size_multiplier": None}]
    good = {"trades": {}, "day_summary": "real summary text", "_agent": {"ok": True}}
    tj._log_day("2026-08-31", ds, good)
    tj._log_day("2026-08-31", ds, good)   # a second chunk / a re-run
    rows = [json.loads(l) for l in open(_JOURNAL, encoding="utf-8")]
    assert sum(1 for r in rows if r["event"] == "day_summary") == 1


def test_run_retries_day_when_llm_call_fails():
    # whole LLM call failed -> log NOTHING for that day, so the next run
    # retries it instead of losing the retrospective forever
    _clean()
    _w(_CARDS, [_entry("c1"), _exit("c1")])
    real, tmp = _cfg(journal_enabled=True)
    orig = tj.generate
    tj.generate = lambda ds, day_context=None: {"trades": {}, "day_summary": None,
                              "_agent": {"ok": False, "error": "network down"}}
    try:
        res = tj.run()
        assert res["status"] == "ok" and res["journaled"] == 0 and res["days"] == 0
        assert res["errors"] and "network down" in res["errors"][0]
        assert not os.path.exists(_JOURNAL), "failed day must not be written"
        assert tj.journaled_card_ids() == set()
        # now the call works -> the same trade gets journaled
        tj.generate = lambda ds, day_context=None: {"trades": {d["card_id"]: {"entry_quality": "good",
                                  "exit_quality": "good", "why_result": "x", "lesson": "none",
                                  "tags": []} for d in ds},
                                  "day_summary": "s", "_agent": {"ok": True, "error": None}}
        assert tj.run()["journaled"] == 1
    finally:
        tj.generate = orig
        _restore_cfg(real, tmp)


def test_run_end_to_end_with_stubbed_llm():
    _clean()
    _w(_CARDS, [_entry("c1"), _exit("c1"),
                _entry("c2", sym="GBPUSD", day="2026-08-30"), _exit("c2", day="2026-08-30")])
    real, tmp = _cfg(journal_enabled=True)
    orig = tj.generate
    tj.generate = lambda ds, day_context=None: {
        "trades": {d["card_id"]: {"entry_quality": "good", "exit_quality": "good",
                                  "why_result": "x", "lesson": "y", "tags": ["t"]} for d in ds},
        "day_summary": (f"{len(day_context)} trades" if day_context else None),
        "_agent": {"ok": True, "error": None}}
    try:
        res = tj.run()
        assert res["status"] == "ok" and res["journaled"] == 2 and res["days"] == 2
        # re-run: nothing new
        assert tj.run()["status"] == "nothing_new"
    finally:
        tj.generate = orig
        _restore_cfg(real, tmp)


def test_run_chunks_a_big_day():
    _clean()
    n_trades = tj.CHUNK_SIZE * 2 + 2
    cards = []
    for i in range(n_trades):
        cid = f"b{i}"
        cards += [_entry(cid, sym=f"P{i:02d}"), _exit(cid, net=float(i))]
    _w(_CARDS, cards)
    real, tmp = _cfg(journal_enabled=True, journal_max_trades_per_run=100)
    calls = []
    orig = tj.generate

    def _fake(ds, day_context=None):
        calls.append((len(ds), day_context is not None))
        return {"trades": {d["card_id"]: {"entry_quality": "fair", "exit_quality": "fair",
                                          "why_result": "x", "lesson": "none", "tags": []} for d in ds},
                "day_summary": ("summary" if day_context else None),
                "_agent": {"ok": True, "error": None}}
    tj.generate = _fake
    try:
        res = tj.run()
        assert res["journaled"] == n_trades, res
        assert len(calls) == 3 and [c[0] for c in calls] == [tj.CHUNK_SIZE, tj.CHUNK_SIZE, 2]
        assert calls[0][1] is True and calls[1][1] is False   # only chunk 1 gets day_context
        rows = [json.loads(l) for l in open(_JOURNAL, encoding="utf-8")]
        summ = [r for r in rows if r["event"] == "day_summary"]
        assert len(summ) == 1 and summ[0]["n_trades"] == n_trades   # whole-day total
    finally:
        tj.generate = orig
        _restore_cfg(real, tmp)


def test_run_still_logs_when_one_chunk_fails():
    _clean()
    n = tj.CHUNK_SIZE + 3   # 2 chunks
    cards = []
    for i in range(n):
        cid = f"f{i}"
        cards += [_entry(cid, sym=f"Q{i:02d}"), _exit(cid)]
    _w(_CARDS, cards)
    real, tmp = _cfg(journal_enabled=True, journal_max_trades_per_run=100)
    orig = tj.generate
    seen = {"n": 0}

    def _fake(ds, day_context=None):
        seen["n"] += 1
        if seen["n"] == 1:                       # first chunk fails
            return {"trades": {}, "day_summary": None,
                    "_agent": {"ok": False, "error": "hit max_tokens"}}
        return {"trades": {d["card_id"]: {"entry_quality": "good", "exit_quality": "good",
                          "why_result": "x", "lesson": "none", "tags": []} for d in ds},
                "day_summary": None, "_agent": {"ok": True, "error": None}}
    tj.generate = _fake
    try:
        res = tj.run()
        assert res["journaled"] == 3 and res["days"] == 1   # chunk-2 trades logged
        assert res["errors"] and "max_tokens" in res["errors"][0]
        # the 8 failed-chunk trades are NOT journaled -> retried next run
        done = tj.journaled_card_ids()
        assert len(done) == 3
    finally:
        tj.generate = orig
        _restore_cfg(real, tmp)


def test_report_cli_runs_clean():
    _clean()
    import subprocess
    p = subprocess.run([sys.executable, "ai_trade_journal.py", "--report"],
                       cwd=BASE_DIR, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0


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
