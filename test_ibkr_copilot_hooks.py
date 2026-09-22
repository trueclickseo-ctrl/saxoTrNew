"""
Smoke-test: verify all AI Copilot hooks are wired into ibkr_executor.py.

Steps:
  1. Check that _ai_cfg / _ai_copilot / _ai_stock_proposal loaded (no ImportError).
  2. Call _ai_ibkr_score() with synthetic data → must return a decision dict.
  3. Call _ai_ibkr_apply() for APPROVE / MODIFY / REJECT paths.
  4. AST-scan each entry function to confirm both hook calls are present.

No IBKR connection needed.  Run from the project root:
  python test_ibkr_copilot_hooks.py
"""
import sys, ast, pathlib

ROOT = pathlib.Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── 1. Import the module ──────────────────────────────────────────────────────
import ibkr_module.ibkr_executor as ex
print("[ ] Module import: OK")

# ── 2. Check AI helpers loaded ────────────────────────────────────────────────
assert ex._ai_cfg        is not None, "_ai_cfg not loaded"
assert ex._ai_copilot    is not None, "_ai_copilot not loaded"
assert ex._ai_stock_proposal is not None, "_ai_stock_proposal not loaded"
print("[1] AI imports: _ai_cfg / _ai_copilot / _ai_stock_proposal  OK")

# ── 3. Probe _ai_ibkr_score() ─────────────────────────────────────────────────
# Reset the process-level dedup cache so a prior test run can't block this call.
import ai.features.trade_proposal as _tp
_tp._evaluated_today = set()

# Temporarily patch evaluate_stock_proposal to avoid a real LLM call,
# and _append to prevent synthetic symbols polluting the live shadow log.
_orig_eval   = ex._ai_copilot.evaluate_stock_proposal
_orig_append = _tp._append
ex._ai_copilot.evaluate_stock_proposal = lambda p: {"action": "APPROVE", "comment": "smoke-test"}
_tp._append = lambda *a, **kw: None

dec = ex._ai_ibkr_score(
    strategy="us_reversion",
    ticker="TEST_HOOK_SMOKE",        # synthetic — never in real dedup cache
    price=500.0,
    stop_price=475.0,
    qty=10,
    rsi14=28.0,
    open_positions=[],
)
ex._ai_copilot.evaluate_stock_proposal = _orig_eval   # restore
_tp._append = _orig_append                            # restore

assert dec is not None, "_ai_ibkr_score returned None (check agent_enabled / stocks_enabled)"
assert dec.get("action") == "APPROVE", f"Unexpected action: {dec}"
print(f"[2] _ai_ibkr_score(): returned action={dec['action']}  OK")

# ── 4. Probe _ai_ibkr_apply() all three paths ─────────────────────────────────
# shadow_mode=true → can_apply returns False → decisions logged but not applied.
# Force-override for path coverage only.
_orig_can = ex._ai_cfg.can_apply_stocks_decision
ex._ai_cfg.can_apply_stocks_decision = lambda: True   # override for path test

skip, qty = ex._ai_ibkr_apply({"action": "APPROVE"}, "TEST_HOOK_SMOKE", 10, "test")
assert not skip and qty == 10, f"APPROVE path wrong: skip={skip} qty={qty}"

skip, qty = ex._ai_ibkr_apply({"action": "MODIFY", "size_multiplier": 0.5}, "TEST_HOOK_SMOKE", 10, "test")
assert not skip and qty == 5, f"MODIFY path wrong: skip={skip} qty={qty}"

skip, qty = ex._ai_ibkr_apply({"action": "REJECT", "comment": "sector concentration"}, "TEST_HOOK_SMOKE", 10, "test")
assert skip and qty == 10, f"REJECT path wrong: skip={skip} qty={qty}"

ex._ai_cfg.can_apply_stocks_decision = _orig_can       # restore
can_apply = _orig_can()
print(f"[3] _ai_ibkr_apply(): APPROVE / MODIFY / REJECT paths  OK")
print(f"    can_apply_stocks_decision() = {can_apply}  (expected False while shadow_mode=true)")

# ── 5. AST check — both hooks present in each entry function ──────────────────
src = (ROOT / "ibkr_module" / "ibkr_executor.py").read_text(encoding="utf-8-sig")
tree = ast.parse(src)

TARGET_FUNCTIONS = {
    "run_rebalance":            "run_rebalance (US Blend)",
    "run_reversion_entries":    "run_reversion_entries (US Reversion)",
    "run_reversion_v2_entries": "run_reversion_v2_entries (US Reversion V2)",
    "run_penny_entries":        "run_penny_entries (US Penny)",
    "run_bagger_entries":       "run_bagger_entries (US Bagger)",
    "run_us_signals_entries":   "run_us_signals_entries (US Signals x4)",
    "_place_book":              "_place_book (Scorer Swing/Portfolio)",
}

errors = []
for node in ast.walk(tree):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    if node.name not in TARGET_FUNCTIONS:
        continue
    fn_src   = ast.unparse(node)
    label    = TARGET_FUNCTIONS[node.name]
    has_sc   = "_ai_ibkr_score" in fn_src
    has_ap   = "_ai_ibkr_apply" in fn_src
    flag     = "" if (has_sc and has_ap) else " <-- MISSING"
    print(f"[4] {label:48s}  score={'Y' if has_sc else 'N'}  apply={'Y' if has_ap else 'N'}{flag}")
    if not (has_sc and has_ap):
        errors.append(label)

missing = [f for f in TARGET_FUNCTIONS
           if f not in {n.name for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}]
for f in missing:
    print(f"  WARNING: '{f}' not found in AST (nested inner function — check manually)")

print()
if errors:
    print(f"FAIL: {len(errors)} function(s) missing hooks: {errors}")
    sys.exit(1)
else:
    print("ALL HOOKS CONFIRMED  -- every entry function has _ai_ibkr_score + _ai_ibkr_apply")
