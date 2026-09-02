"""
test_2026_09_02_ai_rsi_signal_tracking.py
------------------------------------------
2026-09-02: an audit of "is the AI tracking every SIM RSI signal across
184 pairs?" found three gaps. This covers the fixes:

  B. _eur_rate_for_log() -- ANALYTICS-ONLY EUR conversion with a persisted
     last-good Saxo-rate fallback. ~80% of SIM RSI cost-gate rows had
     recovery_thin / all_in_cost_eur = None because the per-process
     _QUOTE_RATE_CACHE has nothing for an exotic quote ccy Saxo can't
     price this run. Strict _eur_per_unit() (sizing / the LIVE EUR45 cap)
     is untouched -- still returns None on a miss.

  A. forward_observation.log_signal_rejected() -- a structured row for a
     signal the pipeline drops BEFORE the cost gate (stale price, signal
     filter, currency exposure, opposing strategy, wide spread, no FX
     rate). Before this those existed only as scheduler-log text.

  C. the orphaned-entry-card sweep -- entry cards with no exit + no open
     position (crash-state re-entry artifacts) get orphaned=true;
     report_giveback / trade_journal skip them explicitly.

Run:  python test_2026_09_02_ai_rsi_signal_tracking.py
"""
import ast
import inspect
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

G, R, Y, X, B = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_res = []


def _run(n, f):
    try:
        f()
        _res.append((n, True, None))
    except Exception as e:
        import traceback
        _res.append((n, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


import forex.runner as fr
import forex.forward_observation as fo


# ═══════════════════════════════════════════════════════════════════════
#  B. _eur_rate_for_log
# ═══════════════════════════════════════════════════════════════════════

def _with_store(entries: dict):
    """context: point the rate store at a temp file seeded with `entries`."""
    fd, path = tempfile.mkstemp(suffix=".json", dir=os.path.join(BASE, "data"))
    os.close(fd)
    json.dump(entries, open(path, "w"))
    return path


def test_eur_rate_for_log_live_path_passes_through():
    real = fr._eur_per_unit
    fr._eur_per_unit = lambda ccy, akey=None: 0.85 if ccy == "USD" else None
    try:
        rate, src = fr._eur_rate_for_log("USD")
        assert rate == 0.85 and src == "live"
    finally:
        fr._eur_per_unit = real
_run("_eur_rate_for_log: a fresh Saxo quote passes straight through, source='live'",
     test_eur_rate_for_log_live_path_passes_through)


def test_eur_rate_for_log_falls_back_to_last_good():
    real = fr._eur_per_unit
    real_path = fr._EUR_RATE_STORE_PATH
    fr._eur_per_unit = lambda ccy, akey=None: None            # Saxo has nothing this run
    fr._EUR_RATE_STORE_PATH = _with_store(
        {"HUF": {"rate": 0.0026, "ts": datetime.now(timezone.utc).isoformat()}})
    try:
        rate, src = fr._eur_rate_for_log("HUF")
        assert abs(rate - 0.0026) < 1e-9 and src == "last_good"
    finally:
        fr._eur_per_unit = real
        os.remove(fr._EUR_RATE_STORE_PATH)
        fr._EUR_RATE_STORE_PATH = real_path
_run("_eur_rate_for_log: Saxo miss -> last-good persisted rate, source='last_good'",
     test_eur_rate_for_log_falls_back_to_last_good)


def test_eur_rate_for_log_rejects_stale_last_good():
    real = fr._eur_per_unit
    real_path = fr._EUR_RATE_STORE_PATH
    fr._eur_per_unit = lambda ccy, akey=None: None
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    fr._EUR_RATE_STORE_PATH = _with_store({"HUF": {"rate": 0.0026, "ts": old}})
    try:
        rate, src = fr._eur_rate_for_log("HUF")
        assert rate is None and src is None, "a >24h old rate must not be used"
    finally:
        fr._eur_per_unit = real
        os.remove(fr._EUR_RATE_STORE_PATH)
        fr._EUR_RATE_STORE_PATH = real_path
_run("_eur_rate_for_log: a last-good rate older than 24h is rejected (returns None)",
     test_eur_rate_for_log_rejects_stale_last_good)


def test_eur_rate_for_log_none_when_nothing_anywhere():
    real = fr._eur_per_unit
    real_path = fr._EUR_RATE_STORE_PATH
    fr._eur_per_unit = lambda ccy, akey=None: None
    fr._EUR_RATE_STORE_PATH = _with_store({})
    try:
        assert fr._eur_rate_for_log("HUF") == (None, None)
    finally:
        fr._eur_per_unit = real
        os.remove(fr._EUR_RATE_STORE_PATH)
        fr._EUR_RATE_STORE_PATH = real_path
_run("_eur_rate_for_log: no live quote and nothing persisted -> (None, None)",
     test_eur_rate_for_log_none_when_nothing_anywhere)


def test_eur_per_unit_persists_every_fresh_success():
    real_path = fr._EUR_RATE_STORE_PATH
    real_retry = fr._live_price_retry
    fr._EUR_RATE_STORE_PATH = _with_store({})
    fr._QUOTE_RATE_CACHE.pop("USD", None)
    fr._live_price_retry = lambda uic, akey, attempts=2: 1.1765   # EURUSD px
    try:
        r = fr._eur_per_unit("USD")
        assert r and abs(r - 1 / 1.1765) < 1e-9
        store = json.load(open(fr._EUR_RATE_STORE_PATH))
        assert "USD" in store and abs(store["USD"]["rate"] - r) < 1e-9, "fresh rate must be persisted"
    finally:
        fr._live_price_retry = real_retry
        os.remove(fr._EUR_RATE_STORE_PATH)
        fr._EUR_RATE_STORE_PATH = real_path
        fr._QUOTE_RATE_CACHE.pop("USD", None)
_run("_eur_per_unit: every fresh Saxo success is written to the persistent store",
     test_eur_per_unit_persists_every_fresh_success)


def test_sizing_paths_still_use_strict_eur_per_unit():
    """The LIVE EUR45 risk cap and _equity_in_quote must NOT get the
    last-good fallback -- a real-money size off a stale rate is exactly
    what the 2026-08-22 rule forbids."""
    src = inspect.getsource(fr._run_entries)
    # the LIVE 45-EUR cap block
    cap_i = src.index("RSI_LIVE_FIXED_RISK_EUR):")
    cap_block = src[cap_i:cap_i + 500]
    assert "_eur_per_unit(_q_ccy" in cap_block, "LIVE EUR45 cap must call strict _eur_per_unit"
    assert "_eur_rate_for_log(_q_ccy" not in cap_block
    # analytics call sites DO use the fallback
    assert "_eur_rate_for_log(quote_ccy_for_log" in src
    assert "eur_rate_for_log = _eur_per_unit(" not in src
_run("sizing (LIVE EUR45 cap) stays on strict _eur_per_unit; only analytics uses _eur_rate_for_log",
     test_sizing_paths_still_use_strict_eur_per_unit)


def test_cost_gate_and_entry_card_carry_rate_source():
    cg = inspect.signature(fo.log_cost_gate_decision).parameters
    ec = inspect.signature(fo.log_trade_entry_card).parameters
    assert "rate_source" in cg and "rate_source" in ec
    # and the runner passes it
    src = inspect.getsource(fr._run_entries)
    assert "rate_source=_rate_src" in src
    assert "rate_source=_rate_src_entry" in src
_run("log_cost_gate_decision / log_trade_entry_card record rate_source; runner passes it",
     test_cost_gate_and_entry_card_carry_rate_source)


# ═══════════════════════════════════════════════════════════════════════
#  A. log_signal_rejected
# ═══════════════════════════════════════════════════════════════════════

def test_log_signal_rejected_writes_a_structured_row():
    real = fo.SIGNAL_REJECT_LOG
    fd, path = tempfile.mkstemp(suffix=".jsonl", dir=os.path.join(BASE, "data"))
    os.close(fd)
    fo.SIGNAL_REJECT_LOG = path
    try:
        fo.log_signal_rejected(account_env="sim", strategy="rsi", symbol="GBPHUF",
                               direction="Buy", stage="wide_spread", detail="0.42% > 0.30%",
                               entry_price=1.234, stop_price=1.22, rsi=8.1)
        row = json.loads(open(path).read().strip())
        assert row["stage"] == "wide_spread" and row["strategy"] == "rsi"
        assert row["symbol"] == "GBPHUF" and row["rsi"] == 8.1
        assert row["entry_price"] == 1.234 and "timestamp" in row
    finally:
        os.remove(path)
        fo.SIGNAL_REJECT_LOG = real
_run("log_signal_rejected: one JSONL row with stage / prices / rsi / timestamp",
     test_log_signal_rejected_writes_a_structured_row)


def test_runner_calls_rej_at_every_pre_cost_gate_skip():
    src = inspect.getsource(fr._run_entries)
    # the local helper exists
    assert "def _rej(stage: str" in src
    # every pre-cost-gate SKIP branch calls it
    for stage in ('"stale_price"', '"signal_filter"', '"currency_exposure"',
                  '"opposing_strategy"', '"wide_spread"', '"no_fx_rate"'):
        assert f"_rej({stage}" in src, f"missing _rej({stage}) call"
    # and _rej is defined BEFORE the cost-gate block
    assert src.index("def _rej(stage") < src.index("log_cost_gate_decision(")
_run("runner: _rej() is called at stale_price / signal_filter / currency / opposing / spread / no_fx_rate skips",
     test_runner_calls_rej_at_every_pre_cost_gate_skip)


def test_log_signal_rejected_is_observation_only():
    src = inspect.getsource(fo.log_signal_rejected)
    for bad in ("place", "post(", "_run_", "raise SystemExit", "cancel"):
        assert bad not in src, f"log_signal_rejected must be pure logging, found {bad!r}"
_run("log_signal_rejected touches nothing but its own JSONL file",
     test_log_signal_rejected_is_observation_only)


# ═══════════════════════════════════════════════════════════════════════
#  C. orphan sweep
# ═══════════════════════════════════════════════════════════════════════

def test_sweep_script_is_idempotent_and_read_mostly():
    import importlib.util
    p = os.path.join(BASE, "sweep_orphan_observation_cards_2026-09-02.py")
    tree = ast.parse(open(p, encoding="utf-8").read())
    # no order placement anywhere in the sweep
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "place_with_stop" not in names and "post" not in names
    # SWEEP_TAG constant present (idempotency key)
    assert 'SWEEP_TAG = "2026-09-02"' in open(p, encoding="utf-8").read()
_run("orphan sweep: no order calls, has a SWEEP_TAG idempotency key",
     test_sweep_script_is_idempotent_and_read_mostly)


def test_giveback_and_journal_skip_orphaned_entries():
    gb = inspect.getsource(__import__("report_giveback"))
    assert 'e.get("orphaned")' in gb and "continue" in gb
    tj = inspect.getsource(__import__("ai.features.trade_journal", fromlist=["build_dossiers"]))
    assert 'e.get("orphaned")' in tj
_run("report_giveback + trade_journal explicitly skip orphaned=true entry cards",
     test_giveback_and_journal_skip_orphaned_entries)


def test_real_cards_file_has_orphans_tagged_not_deleted():
    """the sweep must TAG, never drop -- the entry cards are still there."""
    path = os.path.join(BASE, "data", "trade_observation_cards.jsonl")
    if not os.path.exists(path):
        return
    orphan = paired = 0
    exit_ids = set()
    ents = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        c = json.loads(ln)
        if c.get("event") == "exit":
            exit_ids.add(c.get("card_id"))
        elif c.get("event") == "entry":
            ents.append(c)
    for c in ents:
        if c.get("orphaned"):
            orphan += 1
            assert c.get("orphan_swept") == "2026-09-02"
            assert "orphan_reason" in c
    assert orphan > 0, "expected the 2026-09-02 sweep to have tagged some orphans"
_run("real cards file: orphans are TAGGED (orphaned/orphan_reason/orphan_swept), entry rows intact",
     test_real_cards_file_has_orphans_tagged_not_deleted)


# ── summary ───────────────────────────────────────────────────────────
print(f"\n{B}{'=' * 70}{X}")
bad = [(n, e) for n, ok, e in _res if not ok]
for n, ok, e in _res:
    print(f"  [{G}PASS{X}]" if ok else f"  [{R}FAIL{X}]", n)
    if e:
        print(f"      {Y}{e}{X}")
print(f"{B}{'=' * 70}{X}")
if bad:
    print(f"{R}{B}  {len(bad)} / {len(_res)} FAILED{X}")
    sys.exit(1)
print(f"{G}{B}  ALL {len(_res)} TESTS PASSED{X}")
sys.exit(0)
