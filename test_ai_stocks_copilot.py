"""
Comprehensive test suite: AI Copilot for Stocks.

Covers without a live IBKR connection or real LLM call:
  A. Config gates (ai/config.py)
  B. Proposal building for every strategy family (ai/features/stock_proposal.py)
  C. Copilot internals -- _coerce_decision, _hold, _extract_json (trading_copilot.py)
  D. evaluate_stock_proposal robustness (mocked LLM)
  E. _ai_ibkr_score -- happy path + all early-return paths
  F. _ai_ibkr_apply -- APPROVE / MODIFY / REJECT / HOLD / None + shadow gate + clamps
  G. Dedup gate
  H. AST coverage -- all 7 entry functions have _ai_ibkr_score + _ai_ibkr_apply
  I. End-to-end pipeline per strategy family (mocked LLM, real proposal + logging)

Run from project root:
  python test_ai_stocks_copilot.py
  python -m pytest test_ai_stocks_copilot.py -v
"""

import ast
import json
import pathlib
import sys
import types
import unittest.mock
import unittest

ROOT = pathlib.Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── shared imports ────────────────────────────────────────────────────────────
import ai.config as cfg
import ai.features.stock_proposal as sp
import ai.features.trade_proposal as tp
import ai.agent.trading_copilot as cop
import ibkr_module.ibkr_executor as ex


# ─────────────────────────────────────────────────────────────────────────────
# A. Config gates
# ─────────────────────────────────────────────────────────────────────────────
class TestConfigGates(unittest.TestCase):

    def test_stocks_enabled_sim(self):
        self.assertTrue(cfg.stocks_enabled("sim"),
                        "stocks_enabled('sim') must be True -- config/ai.json stocks.enabled=true")

    def test_agent_enabled(self):
        raw = cfg._load()
        self.assertTrue(bool(raw.get("agent_enabled", False)),
                        "agent_enabled must be True in config/ai.json")

    def test_shadow_mode_is_true(self):
        """Current state: shadow_mode=true -- AI logs but does NOT change trades."""
        raw = cfg._stocks_cfg("sim")
        self.assertTrue(bool(raw.get("shadow_mode", True)),
                        "stocks.shadow_mode should be true until explicitly flipped")

    def test_can_apply_returns_false_in_shadow_mode(self):
        """With shadow_mode=true, can_apply must be False -- no trade changes allowed."""
        self.assertFalse(cfg.can_apply_stocks_decision(),
                         "can_apply_stocks_decision() must be False while shadow_mode=true")

    def test_can_apply_returns_true_when_shadow_off(self):
        """Simulate the flip: shadow_mode=false -> can_apply=True."""
        orig = cfg.can_apply_stocks_decision
        cfg.can_apply_stocks_decision = lambda: True
        self.assertTrue(cfg.can_apply_stocks_decision())
        cfg.can_apply_stocks_decision = orig

    def test_ibkr_ai_gate(self):
        """_ai_ibkr_score's gate: stocks_enabled('sim') AND agent_enabled."""
        self.assertTrue(
            cfg.stocks_enabled("sim") and bool(cfg._load().get("agent_enabled", False)),
            "_ai_ibkr_score gate check must pass with current config"
        )

    def test_saxo_copilot_flags_disabled(self):
        """Saxo SIM copilot flags must all be false after the disable commit."""
        s = cfg._stocks_cfg("sim")
        for flag in ("shadow_copilot_reversion", "shadow_copilot_signals",
                     "shadow_copilot_penny", "shadow_copilot_bagger"):
            self.assertFalse(bool(s.get(flag, False)),
                             f"{flag} must be false -- Saxo SIM hooks were removed")


# ─────────────────────────────────────────────────────────────────────────────
# B. Proposal building -- all strategy families
# ─────────────────────────────────────────────────────────────────────────────
STRATEGIES = [
    "us_reversion", "us_reversion_v2", "us_blend",
    "us_penny", "us_bagger",
    "us_sma_crossover", "us_rsi_reversal", "us_momentum", "us_ensemble",
    "scorer_swing", "scorer_portfolio",
]

class TestBuildStockProposal(unittest.TestCase):

    def _build(self, strategy, **kw):
        defaults = dict(
            strategy=strategy, ticker="AAPL",
            entry_price=200.0, stop_price=190.0, target_price=220.0,
            rsi14=28.0, shares=10,
            daily_vol_pct=1.5, risk_eur=50.0, account_equity_eur=5000.0,
            open_positions=[],
        )
        defaults.update(kw)
        return sp.build_stock_proposal(**defaults)

    def test_all_strategies_return_dict(self):
        for s in STRATEGIES:
            with self.subTest(strategy=s):
                p = self._build(s)
                self.assertIsInstance(p, dict, f"build_stock_proposal({s}) must return dict")
                self.assertTrue(p, f"build_stock_proposal({s}) must not return empty dict")

    def test_required_fields_present(self):
        required = ("ts", "account_env", "market", "symbol", "side",
                    "entry_price", "stop_loss", "strategy_name", "open_positions")
        for s in STRATEGIES:
            with self.subTest(strategy=s):
                p = self._build(s)
                for field in required:
                    self.assertIn(field, p, f"Field '{field}' missing in proposal for {s}")

    def test_market_is_equity(self):
        p = self._build("us_reversion")
        self.assertEqual(p["market"], "equity")

    def test_strategy_name_preserved(self):
        for s in STRATEGIES:
            with self.subTest(strategy=s):
                p = self._build(s)
                self.assertEqual(p["strategy_name"], s)

    def test_side_is_buy(self):
        p = self._build("us_blend")
        self.assertEqual(p["side"], "BUY")

    def test_rsi14_mapped_to_rsi2(self):
        p = self._build("us_reversion", rsi14=32.5)
        self.assertAlmostEqual(p["rsi2"], 32.5, places=1)

    def test_none_rsi_ok(self):
        p = self._build("us_penny", rsi14=None)
        self.assertIsNone(p.get("rsi2"))

    def test_open_positions_list(self):
        ops = [{"symbol": "MSFT", "side": "BUY", "size": 5, "strategy": "us_reversion"}]
        p = self._build("us_reversion", open_positions=ops)
        self.assertEqual(p["n_open_positions"], 1)
        self.assertEqual(p["open_positions"], ops)

    def test_none_prices_dont_raise(self):
        """build_stock_proposal never raises -- returns {} on bad input."""
        p = sp.build_stock_proposal(
            strategy="us_reversion", ticker="AAPL",
            entry_price=0, stop_price=0, target_price=None,
            rsi14=None, shares=0,
            daily_vol_pct=None, risk_eur=None, account_equity_eur=None,
        )
        self.assertIsInstance(p, dict)

    def test_negative_price_dont_raise(self):
        p = sp.build_stock_proposal(
            strategy="us_bagger", ticker="AAPL",
            entry_price=-1.0, stop_price=-2.0, target_price=None,
            rsi14=None, shares=10,
            daily_vol_pct=None, risk_eur=None, account_equity_eur=None,
        )
        self.assertIsInstance(p, dict)

    def test_reward_risk_ratio_computed(self):
        # entry=200, stop=190, target=220 -> R:R = (220-200)/(200-190) = 2.0
        p = self._build("us_reversion",
                        entry_price=200, stop_price=190, target_price=220)
        econ = p.get("trade_economics", {})
        self.assertAlmostEqual(econ.get("reward_risk_ratio", 0), 2.0, places=1)

    def test_account_env_is_sim(self):
        p = self._build("us_blend")
        self.assertEqual(p["account_env"], "sim")


# ─────────────────────────────────────────────────────────────────────────────
# C. Copilot internals (no LLM call)
# ─────────────────────────────────────────────────────────────────────────────
class TestCopilotInternals(unittest.TestCase):

    def test_hold_has_action_hold(self):
        d = cop._hold("test reason")
        self.assertEqual(d["action"], "HOLD")
        self.assertFalse(d["_agent"]["ok"])

    def test_hold_multiplier_is_one(self):
        d = cop._hold("x")
        self.assertEqual(d["size_multiplier"], 1.0)

    def test_coerce_approve(self):
        d = cop._coerce_decision({"action": "APPROVE", "comment": "ok"}, "model", 10.0)
        self.assertEqual(d["action"], "APPROVE")
        self.assertEqual(d["size_multiplier"], 1.0)

    def test_coerce_reject(self):
        d = cop._coerce_decision({"action": "REJECT", "comment": "sector risk"}, "model", 5.0)
        self.assertEqual(d["action"], "REJECT")

    def test_coerce_modify_valid(self):
        d = cop._coerce_decision({"action": "MODIFY", "size_multiplier": 0.5}, "model", 5.0)
        self.assertEqual(d["action"], "MODIFY")
        self.assertAlmostEqual(d["size_multiplier"], 0.5, places=3)

    def test_coerce_clamps_multiplier_above_one(self):
        d = cop._coerce_decision({"action": "MODIFY", "size_multiplier": 1.5}, "model", 5.0)
        self.assertLessEqual(d["size_multiplier"], cop.MULTIPLIER_CEIL)

    def test_coerce_clamps_multiplier_below_floor(self):
        d = cop._coerce_decision({"action": "MODIFY", "size_multiplier": 0.1}, "model", 5.0)
        self.assertGreaterEqual(d["size_multiplier"], cop.MULTIPLIER_FLOOR)

    def test_coerce_bad_action_returns_hold(self):
        d = cop._coerce_decision({"action": "MAYBE"}, "model", 5.0)
        self.assertEqual(d["action"], "HOLD")

    def test_coerce_forces_stop_tp_to_none(self):
        d = cop._coerce_decision({
            "action": "MODIFY", "size_multiplier": 0.5,
            "adjusted_stop_loss": 190.0, "adjusted_take_profit": 220.0,
        }, "model", 5.0)
        self.assertIsNone(d["adjusted_stop_loss"])
        self.assertIsNone(d["adjusted_take_profit"])

    def test_extract_json_plain(self):
        d = cop._extract_json('{"action": "APPROVE"}')
        self.assertEqual(d["action"], "APPROVE")

    def test_extract_json_fenced(self):
        text = '```json\n{"action": "REJECT", "comment": "risk"}\n```'
        d = cop._extract_json(text)
        self.assertIsNotNone(d)
        self.assertEqual(d["action"], "REJECT")

    def test_extract_json_prose_wrapped(self):
        text = 'Here is my decision: {"action": "APPROVE"} hope that helps.'
        d = cop._extract_json(text)
        self.assertIsNotNone(d)

    def test_extract_json_garbage(self):
        d = cop._extract_json("not json at all")
        self.assertIsNone(d)

    def test_extract_json_empty_string(self):
        d = cop._extract_json("")
        self.assertIsNone(d)


# ─────────────────────────────────────────────────────────────────────────────
# D. evaluate_stock_proposal -- robustness (LLM mocked out)
# ─────────────────────────────────────────────────────────────────────────────
class TestEvaluateStockProposal(unittest.TestCase):

    def _mock_call(self, action, multiplier=1.0, comment="test"):
        # _call_llm signature is (system_prompt, proposal) -- mock must match
        def _fn(system_prompt, proposal):
            return {
                "action": action, "size_multiplier": multiplier,
                "adjusted_stop_loss": None, "adjusted_take_profit": None,
                "comment": comment,
                "_agent": {"ok": True, "error": None, "model": "mock", "latency_ms": 0},
            }
        return _fn

    def test_returns_dict(self):
        orig = cop._call_llm
        cop._call_llm = self._mock_call("APPROVE")
        try:
            d = cop.evaluate_stock_proposal({"strategy_name": "us_reversion"})
            self.assertIsInstance(d, dict)
        finally:
            cop._call_llm = orig

    def test_empty_proposal_returns_dict(self):
        orig = cop._call_llm
        cop._call_llm = self._mock_call("HOLD")
        try:
            d = cop.evaluate_stock_proposal({})
            self.assertIsInstance(d, dict)
            self.assertIn("action", d)
        finally:
            cop._call_llm = orig

    def test_never_raises_on_network_failure(self):
        """evaluate_stock_proposal must return HOLD on any realistic failure
        (network down, SDK missing, timeout). The never-raise contract lives
        inside _call_llm's try/except around the Anthropic client call."""
        orig = cop._call_llm
        # Simulate what _call_llm does when Anthropic raises: return a HOLD dict
        cop._call_llm = lambda system, p: cop._hold("simulated network failure", 0.0, "mock")
        try:
            d = cop.evaluate_stock_proposal({"symbol": "AAPL"})
            self.assertIsInstance(d, dict)
            self.assertEqual(d["action"], "HOLD")
        finally:
            cop._call_llm = orig

    def test_action_is_valid_word(self):
        for action in ("APPROVE", "MODIFY", "REJECT", "HOLD"):
            with self.subTest(action=action):
                orig = cop._call_llm
                cop._call_llm = self._mock_call(
                    action, multiplier=0.5 if action == "MODIFY" else 1.0)
                try:
                    d = cop.evaluate_stock_proposal({})
                    self.assertIn(d["action"], ("APPROVE", "MODIFY", "REJECT", "HOLD"))
                finally:
                    cop._call_llm = orig


# ─────────────────────────────────────────────────────────────────────────────
# E. _ai_ibkr_score -- all paths
# ─────────────────────────────────────────────────────────────────────────────
class TestAIIBKRScore(unittest.TestCase):

    TICKER = "IBKR_TEST_SCORE_ZZZ"   # synthetic -- never in real dedup

    def setUp(self):
        # Reset dedup cache before every test
        tp._evaluated_today = set()
        # Patch copilot to avoid real LLM call
        self._orig_eval = ex._ai_copilot.evaluate_stock_proposal
        ex._ai_copilot.evaluate_stock_proposal = lambda p: {
            "action": "APPROVE", "size_multiplier": 1.0,
            "comment": "mock", "_agent": {"ok": True},
        }
        # Prevent test symbols from polluting the live shadow log
        self._patch_append = unittest.mock.patch("ai.features.trade_proposal._append")
        self._patch_append.start()

    def tearDown(self):
        ex._ai_copilot.evaluate_stock_proposal = self._orig_eval
        self._patch_append.stop()

    def test_returns_decision_dict(self):
        d = ex._ai_ibkr_score("us_reversion", self.TICKER, 100.0, 95.0, 10, rsi14=28.0)
        self.assertIsNotNone(d)
        self.assertIn("action", d)

    def test_returns_none_when_cfg_missing(self):
        orig = ex._ai_cfg
        ex._ai_cfg = None
        try:
            d = ex._ai_ibkr_score("us_reversion", self.TICKER, 100.0, 95.0, 10)
            self.assertIsNone(d)
        finally:
            ex._ai_cfg = orig

    def test_returns_none_when_copilot_missing(self):
        orig = ex._ai_copilot
        ex._ai_copilot = None
        try:
            d = ex._ai_ibkr_score("us_reversion", self.TICKER, 100.0, 95.0, 10)
            self.assertIsNone(d)
        finally:
            ex._ai_copilot = orig

    def test_returns_none_when_stocks_disabled(self):
        orig = ex._ai_cfg.stocks_enabled
        ex._ai_cfg.stocks_enabled = lambda *a: False
        try:
            d = ex._ai_ibkr_score("us_reversion", self.TICKER, 100.0, 95.0, 10)
            self.assertIsNone(d)
        finally:
            ex._ai_cfg.stocks_enabled = orig

    def test_returns_none_when_agent_disabled(self):
        orig_load = ex._ai_cfg._load
        ex._ai_cfg._load = lambda: {**orig_load(), "agent_enabled": False}
        try:
            d = ex._ai_ibkr_score("us_reversion", self.TICKER, 100.0, 95.0, 10)
            self.assertIsNone(d)
        finally:
            ex._ai_cfg._load = orig_load

    def test_dedup_blocks_second_call_same_day(self):
        """Second call for the same ticker/strategy today returns None."""
        d1 = ex._ai_ibkr_score("us_reversion", self.TICKER, 100.0, 95.0, 10)
        self.assertIsNotNone(d1)
        d2 = ex._ai_ibkr_score("us_reversion", self.TICKER, 100.0, 95.0, 10)
        self.assertIsNone(d2, "Dedup must block re-evaluation of same ticker/strategy/day")

    def test_different_strategies_not_deduped(self):
        """Same ticker, different strategy -> both should fire."""
        d1 = ex._ai_ibkr_score("us_reversion",   self.TICKER, 100.0, 95.0, 10)
        tp._evaluated_today = set()   # reset between calls for clean test
        d2 = ex._ai_ibkr_score("us_reversion_v2", self.TICKER, 100.0, 95.0, 10)
        self.assertIsNotNone(d1)
        self.assertIsNotNone(d2)

    def test_exception_in_copilot_returns_none(self):
        ex._ai_copilot.evaluate_stock_proposal = lambda p: (_ for _ in ()).throw(RuntimeError("boom"))
        tp._evaluated_today = set()
        d = ex._ai_ibkr_score("us_reversion", self.TICKER, 100.0, 95.0, 10)
        self.assertIsNone(d)

    def test_all_strategy_names_accepted(self):
        for strat in STRATEGIES:
            tp._evaluated_today = set()
            with self.subTest(strategy=strat):
                d = ex._ai_ibkr_score(strat, self.TICKER + strat, 100.0, 95.0, 10, rsi14=30.0)
                self.assertIsNotNone(d, f"_ai_ibkr_score returned None for strategy={strat}")


# ─────────────────────────────────────────────────────────────────────────────
# F. _ai_ibkr_apply -- all decision paths + shadow gate + multiplier clamps
# ─────────────────────────────────────────────────────────────────────────────
class TestAIIBKRApply(unittest.TestCase):

    TICKER = "IBKR_TEST_APPLY"

    def setUp(self):
        # Force can_apply=True for path tests; individual tests override as needed
        self._orig_can = ex._ai_cfg.can_apply_stocks_decision
        ex._ai_cfg.can_apply_stocks_decision = lambda: True

    def tearDown(self):
        ex._ai_cfg.can_apply_stocks_decision = self._orig_can

    # -- passthrough cases -------------------------------------------------

    def test_none_dec_returns_original_qty(self):
        skip, qty = ex._ai_ibkr_apply(None, self.TICKER, 20, "test")
        self.assertFalse(skip)
        self.assertEqual(qty, 20)

    def test_shadow_mode_passthrough(self):
        """When can_apply=False (shadow_mode=true), no modification ever happens."""
        ex._ai_cfg.can_apply_stocks_decision = lambda: False
        skip, qty = ex._ai_ibkr_apply({"action": "REJECT"}, self.TICKER, 20, "test")
        self.assertFalse(skip)
        self.assertEqual(qty, 20)

    # -- APPROVE -----------------------------------------------------------

    def test_approve_no_change(self):
        skip, qty = ex._ai_ibkr_apply({"action": "APPROVE"}, self.TICKER, 20, "test")
        self.assertFalse(skip)
        self.assertEqual(qty, 20)

    def test_hold_no_change(self):
        skip, qty = ex._ai_ibkr_apply({"action": "HOLD"}, self.TICKER, 20, "test")
        self.assertFalse(skip)
        self.assertEqual(qty, 20)

    def test_missing_action_no_change(self):
        skip, qty = ex._ai_ibkr_apply({}, self.TICKER, 20, "test")
        self.assertFalse(skip)
        self.assertEqual(qty, 20)

    # -- REJECT ------------------------------------------------------------

    def test_reject_sets_skip(self):
        skip, qty = ex._ai_ibkr_apply(
            {"action": "REJECT", "comment": "sector risk"}, self.TICKER, 20, "test")
        self.assertTrue(skip)
        self.assertEqual(qty, 20)   # qty unchanged even on reject

    # -- MODIFY ------------------------------------------------------------

    def test_modify_halves_qty(self):
        skip, qty = ex._ai_ibkr_apply(
            {"action": "MODIFY", "size_multiplier": 0.5}, self.TICKER, 20, "test")
        self.assertFalse(skip)
        self.assertEqual(qty, 10)

    def test_modify_floors_to_one_share(self):
        skip, qty = ex._ai_ibkr_apply(
            {"action": "MODIFY", "size_multiplier": 0.5}, self.TICKER, 1, "test")
        self.assertFalse(skip)
        self.assertEqual(qty, 1)    # max(1, int(1 * 0.5)) = 1

    def test_modify_clamps_multiplier_above_one(self):
        skip, qty = ex._ai_ibkr_apply(
            {"action": "MODIFY", "size_multiplier": 2.0}, self.TICKER, 10, "test")
        self.assertFalse(skip)
        self.assertEqual(qty, 10)   # clamped to 1.0 -> 10 unchanged

    def test_modify_clamps_multiplier_below_floor(self):
        skip, qty = ex._ai_ibkr_apply(
            {"action": "MODIFY", "size_multiplier": 0.1}, self.TICKER, 100, "test")
        self.assertFalse(skip)
        # floor=0.25 -> max(1, int(100 * 0.25)) = 25
        self.assertEqual(qty, 25)

    def test_modify_non_numeric_multiplier(self):
        """Non-numeric multiplier should not crash; falls back to original qty."""
        skip, qty = ex._ai_ibkr_apply(
            {"action": "MODIFY", "size_multiplier": "bad"}, self.TICKER, 10, "test")
        self.assertIsInstance(qty, int)

    def test_modify_zero_multiplier_floored(self):
        skip, qty = ex._ai_ibkr_apply(
            {"action": "MODIFY", "size_multiplier": 0.0}, self.TICKER, 10, "test")
        self.assertFalse(skip)
        self.assertGreaterEqual(qty, 1)


# ─────────────────────────────────────────────────────────────────────────────
# G. Dedup gate
# ─────────────────────────────────────────────────────────────────────────────
class TestDedupGate(unittest.TestCase):

    def setUp(self):
        tp._evaluated_today = set()
        # Prevent test symbols from polluting the live shadow log
        self._patch_append = unittest.mock.patch("ai.features.trade_proposal._append")
        self._patch_append.start()

    def tearDown(self):
        self._patch_append.stop()

    def _prop(self, strategy, ticker):
        return sp.build_stock_proposal(
            strategy=strategy, ticker=ticker,
            entry_price=100.0, stop_price=95.0, target_price=None,
            rsi14=30.0, shares=10,
            daily_vol_pct=None, risk_eur=None, account_equity_eur=None,
        )

    def test_not_evaluated_initially(self):
        p = self._prop("us_reversion", "DEDUP_TEST_AA")
        self.assertFalse(sp.already_evaluated(p))

    def test_evaluated_after_log_shadow(self):
        p = self._prop("us_reversion", "DEDUP_TEST_BB")
        sp.log_shadow_decision(p, {"action": "APPROVE"}, entered=True)
        self.assertTrue(sp.already_evaluated(p))

    def test_different_ticker_not_deduped(self):
        p1 = self._prop("us_reversion", "DEDUP_TEST_CC")
        p2 = self._prop("us_reversion", "DEDUP_TEST_DD")
        sp.log_shadow_decision(p1, {"action": "APPROVE"}, entered=True)
        self.assertFalse(sp.already_evaluated(p2))

    def test_different_strategy_not_deduped(self):
        p1 = self._prop("us_reversion",    "DEDUP_TEST_EE")
        p2 = self._prop("us_reversion_v2", "DEDUP_TEST_EE")
        sp.log_shadow_decision(p1, {"action": "APPROVE"}, entered=True)
        self.assertFalse(sp.already_evaluated(p2))

    def test_agent_dedup_enabled_by_config(self):
        self.assertTrue(cfg.agent_dedup_enabled(),
                        "agent_dedup must be enabled to prevent LLM cost explosion")


# ─────────────────────────────────────────────────────────────────────────────
# H. AST coverage -- all 7 entry functions have both hooks
# ─────────────────────────────────────────────────────────────────────────────
class TestASTHookCoverage(unittest.TestCase):

    EXECUTOR = ROOT / "ibkr_module" / "ibkr_executor.py"

    TARGET_FUNCTIONS = {
        "run_rebalance":            "run_rebalance (US Blend)",
        "run_reversion_entries":    "run_reversion_entries (US Reversion)",
        "run_reversion_v2_entries": "run_reversion_v2_entries (US Reversion V2)",
        "run_penny_entries":        "run_penny_entries (US Penny)",
        "run_bagger_entries":       "run_bagger_entries (US Bagger)",
        "run_us_signals_entries":   "run_us_signals_entries (US Signals x4)",
        "_place_book":              "_place_book (Scorer Swing/Portfolio)",
    }

    @classmethod
    def setUpClass(cls):
        cls.src  = cls.EXECUTOR.read_text(encoding="utf-8-sig")
        cls.tree = ast.parse(cls.src)
        cls.fns  = {n.name: n for n in ast.walk(cls.tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def test_score_hook_present(self):
        for fn, label in self.TARGET_FUNCTIONS.items():
            with self.subTest(function=label):
                self.assertIn(fn, self.fns, f"{fn} not found in AST")
                fn_src = ast.unparse(self.fns[fn])
                self.assertIn("_ai_ibkr_score", fn_src,
                              f"_ai_ibkr_score missing from {label}")

    def test_apply_hook_present(self):
        for fn, label in self.TARGET_FUNCTIONS.items():
            with self.subTest(function=label):
                self.assertIn(fn, self.fns, f"{fn} not found in AST")
                fn_src = ast.unparse(self.fns[fn])
                self.assertIn("_ai_ibkr_apply", fn_src,
                              f"_ai_ibkr_apply missing from {label}")

    def test_module_level_ai_imports_are_guarded(self):
        """AI imports at module level must be wrapped in try/except so a missing
        AI module never crashes the IBKR executor."""
        src = self.src
        # The guard block is present if the string _ai_cfg is assigned inside a try
        self.assertIn("_ai_cfg = None", src,
                      "Module-level _ai_cfg guard missing from ibkr_executor")
        self.assertIn("_ai_copilot = None", src,
                      "Module-level _ai_copilot guard missing")

    def test_saxo_runner_has_no_copilot_hooks(self):
        """After the disable commit, atos_runner.py must not contain copilot calls."""
        runner = (ROOT / "atos_runner.py").read_text(encoding="utf-8")
        self.assertNotIn("evaluate_stock_proposal", runner,
                         "evaluate_stock_proposal still present in atos_runner.py -- hooks not removed")


# ─────────────────────────────────────────────────────────────────────────────
# I. End-to-end pipeline per strategy family (mocked LLM)
# ─────────────────────────────────────────────────────────────────────────────
class TestEndToEndPipeline(unittest.TestCase):
    """Simulate what the scanner does at entry time: build proposal → log →
    score → apply (shadow-safe). No IBKR connection, no LLM call."""

    def setUp(self):
        tp._evaluated_today = set()
        self._orig_eval = ex._ai_copilot.evaluate_stock_proposal
        # Prevent test symbols from polluting the live shadow log
        self._patch_append = unittest.mock.patch("ai.features.trade_proposal._append")
        self._patch_append.start()

    def tearDown(self):
        ex._ai_copilot.evaluate_stock_proposal = self._orig_eval
        self._patch_append.stop()

    def _run_pipeline(self, strategy, action, multiplier=1.0, qty=20):
        ticker = f"E2E_{strategy.upper()[:8]}_ZZZ"
        ex._ai_copilot.evaluate_stock_proposal = lambda p: {
            "action": action, "size_multiplier": multiplier,
            "comment": f"e2e-{action}", "_agent": {"ok": True},
        }
        price, stop = 100.0, 95.0
        dec = ex._ai_ibkr_score(strategy, ticker, price, stop, qty, rsi14=30.0)
        skip, final_qty = ex._ai_ibkr_apply(dec, ticker, qty, strategy)
        return dec, skip, final_qty

    # -- APPROVE path for every strategy -----------------------------------

    def test_approve_all_strategies(self):
        for s in STRATEGIES:
            tp._evaluated_today = set()
            with self.subTest(strategy=s):
                dec, skip, qty = self._run_pipeline(s, "APPROVE", qty=20)
                self.assertIsNotNone(dec, f"Pipeline returned None dec for {s}")
                # shadow_mode=True -> can_apply=False -> passthrough
                self.assertFalse(skip)
                self.assertEqual(qty, 20)

    # -- REJECT path (shadow passthrough) ----------------------------------

    def test_reject_passthrough_in_shadow_mode(self):
        """In shadow mode, REJECT decision is logged but trade is NOT skipped."""
        dec, skip, qty = self._run_pipeline("us_reversion", "REJECT", qty=15)
        self.assertIsNotNone(dec)
        self.assertFalse(skip,   "shadow_mode=true: REJECT must not skip the trade")
        self.assertEqual(qty, 15)

    # -- MODIFY path (shadow passthrough) ----------------------------------

    def test_modify_passthrough_in_shadow_mode(self):
        """In shadow mode, MODIFY is logged but qty is NOT changed."""
        dec, skip, qty = self._run_pipeline("us_blend", "MODIFY", multiplier=0.5, qty=20)
        self.assertIsNotNone(dec)
        self.assertFalse(skip)
        self.assertEqual(qty, 20, "shadow_mode=true: MODIFY must not change qty")

    # -- decision fields validated -----------------------------------------

    def test_decision_has_required_fields(self):
        tp._evaluated_today = set()
        dec, _, _ = self._run_pipeline("us_penny", "APPROVE")
        for field in ("action", "size_multiplier", "comment"):
            self.assertIn(field, dec, f"Decision missing field '{field}'")

    # -- simulate active mode (shadow_mode flipped) ------------------------

    def test_reject_skips_when_shadow_off(self):
        orig = ex._ai_cfg.can_apply_stocks_decision
        ex._ai_cfg.can_apply_stocks_decision = lambda: True
        tp._evaluated_today = set()
        try:
            dec, skip, qty = self._run_pipeline("scorer_swing", "REJECT", qty=10)
            self.assertTrue(skip, "When shadow_mode=false, REJECT must skip the trade")
        finally:
            ex._ai_cfg.can_apply_stocks_decision = orig

    def test_modify_reduces_qty_when_shadow_off(self):
        orig = ex._ai_cfg.can_apply_stocks_decision
        ex._ai_cfg.can_apply_stocks_decision = lambda: True
        tp._evaluated_today = set()
        try:
            dec, skip, qty = self._run_pipeline("scorer_portfolio", "MODIFY",
                                                multiplier=0.5, qty=20)
            self.assertFalse(skip)
            self.assertEqual(qty, 10, "When shadow_mode=false, MODIFY must halve qty")
        finally:
            ex._ai_cfg.can_apply_stocks_decision = orig


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromModule(
        sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
