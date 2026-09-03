"""
test_2026_09_03_trade_outcome_predictor.py
Tests for the Trade Outcome Predictor (TOP).

Coverage:
  - Data loading safety (missing/empty/corrupt files)
  - Feature extraction (_extract_features consistency)
  - Training gate (< 100 samples → not trained)
  - Training with synthetic data → report structure + saved files
  - Prediction: None without model; float [0,1] with model
  - Prediction degrades safely on every bad input
  - Recovery_thin computed correctly from proposal economics
  - top_win_prob wired into build_proposal (disabled → None)
  - Governance: no forbidden imports in predictor module
  - status() always returns a dict with expected keys

27 tests.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import pickle
import random
import sys
import tempfile
import unittest

try:
    import sklearn  # noqa: F401
    _SKLEARN_AVAILABLE = True
except ImportError:
    _SKLEARN_AVAILABLE = False

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_entry_card(strategy="rsi", symbol="EURUSD", acct="sim",
                     direction="Buy", entry_price=1.10, atr=0.005,
                     r2c=4.2, thin=False, ts="2026-09-01T10:00:00+00:00") -> dict:
    card_id = f"{acct}:{strategy}:{symbol}:{ts}"
    return {
        "card_id":                card_id,
        "event":                  "entry",
        "timestamp":              ts,
        "account_env":            acct,
        "strategy":               strategy,
        "symbol":                 symbol,
        "direction":              direction,
        "entry_price":            entry_price,
        "atr_at_entry":           atr,
        "quantity":               1000,
        "current_stop":           entry_price - 0.01,
        "structural_stop":        None,
        "hybrid_stop":            None,
        "risk_eur":               45.0,
        "cost_eur":               5.18,
        "cost_to_edge_ratio":     0.32,
        "all_in_cost_eur":        6.5,
        "recovery_to_cost_ratio": r2c,
        "recovery_thin":          thin,
        "rate_source":            "live",
        "exposure_before_eur":    {},
        "exposure_after_eur":     {},
    }


def _make_exit_card(card_id: str, r_multiple: float) -> dict:
    return {
        "card_id":    card_id,
        "event":      "exit",
        "timestamp":  "2026-09-03T14:00:00+00:00",
        "exit_price": 1.11,
        "exit_reason": "profit_target",
        "gross_pnl_eur": r_multiple * 45.0,
        "commission_eur": 5.18,
        "net_pnl_eur": r_multiple * 45.0 - 5.18,
        "r_multiple": r_multiple,
        "mae_eur": -10.0,
        "mfe_eur": 55.0,
        "holding_hours": 24.0,
    }


def _make_proposal(strategy="rsi", symbol="EURUSD", acct="sim",
                   ts="2026-09-01", rsi=8.0, adx=32.0, regime="TRENDING_BULLISH",
                   signal_strength=0.5, n_open=3, win_rate=62.0, n_closed=45) -> dict:
    return {
        "ts":               ts + "T10:00:00+00:00",
        "account_env":      acct,
        "strategy_name":    strategy,
        "symbol":           symbol,
        "side":             "BUY",
        "rsi2":             rsi,
        "atr_pct":          0.45,
        "signal_strength":  signal_strength,
        "n_open_positions": n_open,
        "regime": {
            "label": regime,
            "adx":   adx,
        },
        "trade_economics": {
            "recovery_0p5R_to_cost_ratio": 3.46,
        },
        "pair_history": {
            "win_rate_pct": win_rate,
            "n_closed":     n_closed,
        },
    }


def _write_jsonl(path: str, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _synthetic_rows(n: int, seed: int = 42) -> list[dict]:
    """Generate n synthetic training rows for fast unit tests."""
    rng = random.Random(seed)
    strategies = ["rsi", "rsi_trend", "ema_trend", "bb_quality", "zscore_quality"]
    regimes    = ["TRENDING_BULLISH", "TRENDING_BEARISH", "RANGING", "HIGH_VOLATILITY"]
    rows = []
    for i in range(n):
        strat = rng.choice(strategies)
        won   = rng.random() > 0.45
        rows.append({
            "strategy":               strat,
            "regime_label":           rng.choice(regimes),
            "direction":              rng.choice(["Buy", "Sell"]),
            "rsi2":                   rng.uniform(3.0, 20.0),
            "adx":                    rng.uniform(15.0, 50.0),
            "atr_pct":                rng.uniform(0.1, 1.5),
            "signal_strength":        rng.uniform(0.2, 1.0),
            "n_open_positions":       rng.randint(0, 8),
            "recovery_to_cost_ratio": rng.uniform(2.5, 6.0),
            "recovery_thin":          float(rng.random() < 0.3),
            "day_of_week":            float(rng.randint(0, 4)),
            "pair_win_rate":          rng.uniform(40.0, 70.0),
            "pair_n_closed":          float(rng.randint(5, 80)),
            "entry_date":             f"2026-0{rng.randint(1,8)}-{rng.randint(1,28):02d}",
            "r_multiple":             rng.uniform(0.5, 2.0) if won else rng.uniform(-1.5, -0.1),
            "won":                    1 if won else 0,
        })
    return rows


# ── tests ─────────────────────────────────────────────────────────────────────

class TestDataLoading(unittest.TestCase):

    def test_missing_cards_file_returns_empty(self):
        import ai.models.trade_outcome_predictor as top
        orig = top.CARDS_LOG
        top.CARDS_LOG = "/nonexistent/path/cards.jsonl"
        try:
            result = top._load_raw_data()
            self.assertEqual(result, [])
        finally:
            top.CARDS_LOG = orig

    def test_empty_cards_file_returns_empty(self):
        import ai.models.trade_outcome_predictor as top
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
            fname = f.name
        orig = top.CARDS_LOG
        top.CARDS_LOG = fname
        try:
            self.assertEqual(top._load_raw_data(), [])
        finally:
            top.CARDS_LOG = orig
            os.unlink(fname)

    def test_corrupt_lines_skipped(self):
        import ai.models.trade_outcome_predictor as top
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False,
                                        mode="w", encoding="utf-8") as f:
            f.write("not json\n")
            f.write("{}\n")  # no event key
            fname = f.name
        orig = top.CARDS_LOG
        top.CARDS_LOG = fname
        try:
            result = top._load_raw_data()
            self.assertEqual(result, [])
        finally:
            top.CARDS_LOG = orig
            os.unlink(fname)

    def test_orphaned_cards_excluded(self):
        import ai.models.trade_outcome_predictor as top
        entry = _make_entry_card()
        entry["orphaned"] = True
        exit_ = _make_exit_card(entry["card_id"], r_multiple=0.8)
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False,
                                        mode="w", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
            f.write(json.dumps(exit_) + "\n")
            fname = f.name
        orig = top.CARDS_LOG
        top.CARDS_LOG = fname
        try:
            self.assertEqual(top._load_raw_data(), [])
        finally:
            top.CARDS_LOG = orig
            os.unlink(fname)

    def test_open_position_excluded(self):
        """Entry card without matching exit → not counted."""
        import ai.models.trade_outcome_predictor as top
        entry = _make_entry_card()
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False,
                                        mode="w", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
            fname = f.name
        orig = top.CARDS_LOG
        top.CARDS_LOG = fname
        try:
            self.assertEqual(top._load_raw_data(), [])
        finally:
            top.CARDS_LOG = orig
            os.unlink(fname)

    def test_entry_exit_pair_loaded(self):
        import ai.models.trade_outcome_predictor as top
        entry = _make_entry_card()
        exit_ = _make_exit_card(entry["card_id"], r_multiple=0.7)
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False,
                                        mode="w", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
            f.write(json.dumps(exit_) + "\n")
            fname = f.name
        orig = top.CARDS_LOG
        top.CARDS_LOG = fname
        try:
            rows = top._load_raw_data()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["won"], 1)
            self.assertEqual(rows[0]["strategy"], "rsi")
        finally:
            top.CARDS_LOG = orig
            os.unlink(fname)

    def test_loss_labelled_correctly(self):
        import ai.models.trade_outcome_predictor as top
        entry = _make_entry_card()
        exit_ = _make_exit_card(entry["card_id"], r_multiple=-0.9)
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False,
                                        mode="w", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
            f.write(json.dumps(exit_) + "\n")
            fname = f.name
        orig = top.CARDS_LOG
        top.CARDS_LOG = fname
        try:
            rows = top._load_raw_data()
            self.assertEqual(rows[0]["won"], 0)
        finally:
            top.CARDS_LOG = orig
            os.unlink(fname)


class TestFeatureExtraction(unittest.TestCase):

    def test_build_X_deterministic(self):
        from ai.models.trade_outcome_predictor import _extract_features
        row = _synthetic_rows(1)[0]
        f1 = _extract_features(row)
        f2 = _extract_features(row)
        self.assertEqual(f1, f2)

    def test_feature_vector_length_consistent(self):
        from ai.models.trade_outcome_predictor import (
            _extract_features, _NUM_FEATURES, _STRATEGIES, _REGIMES, _DIRECTIONS,
        )
        expected_len = len(_NUM_FEATURES) + len(_STRATEGIES) + len(_REGIMES) + len(_DIRECTIONS)
        for row in _synthetic_rows(10):
            self.assertEqual(len(_extract_features(row)), expected_len)

    def test_unknown_strategy_handled(self):
        from ai.models.trade_outcome_predictor import _extract_features
        row = _synthetic_rows(1)[0]
        row["strategy"] = "some_future_strategy"
        vec = _extract_features(row)
        self.assertEqual(len(vec), len(_extract_features(_synthetic_rows(1)[0])))

    def test_none_numerics_default_zero(self):
        from ai.models.trade_outcome_predictor import _extract_features
        row = _synthetic_rows(1)[0]
        row["rsi2"] = None
        row["adx"]  = None
        vec = _extract_features(row)
        # First two numerics should be 0.0
        self.assertEqual(vec[0], 0.0)
        self.assertEqual(vec[1], 0.0)


class TestTrainingGate(unittest.TestCase):

    @unittest.skipUnless(_SKLEARN_AVAILABLE, "scikit-learn not installed")
    def test_too_few_samples_returns_not_trained(self):
        import ai.models.trade_outcome_predictor as top
        orig_load = top._load_raw_data
        top._load_raw_data = lambda: _synthetic_rows(50)
        try:
            result = top.train(min_samples=100)
            self.assertFalse(result.get("trained"))
            self.assertIn("50", result.get("reason", ""))
        finally:
            top._load_raw_data = orig_load

    def test_zero_samples_returns_not_trained(self):
        import ai.models.trade_outcome_predictor as top
        orig_load = top._load_raw_data
        top._load_raw_data = lambda: []
        try:
            result = top.train(min_samples=100)
            self.assertFalse(result.get("trained"))
        finally:
            top._load_raw_data = orig_load


class TestTraining(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _patch_model_dir(self, top):
        top.MODEL_DIR  = self._tmpdir
        top.MODEL_PKL  = os.path.join(self._tmpdir, "model.pkl")
        top.REPORT_JSON = os.path.join(self._tmpdir, "report.json")

    @unittest.skipUnless(_SKLEARN_AVAILABLE, "scikit-learn not installed")
    def test_train_with_sufficient_data(self):
        import ai.models.trade_outcome_predictor as top
        orig_load = top._load_raw_data
        orig_dir  = top.MODEL_DIR
        orig_pkl  = top.MODEL_PKL
        orig_rep  = top.REPORT_JSON
        self._patch_model_dir(top)
        top._load_raw_data = lambda: _synthetic_rows(150)
        try:
            result = top.train(min_samples=100)
            self.assertTrue(result.get("trained"), result)
            self.assertIn("test_accuracy",  result)
            self.assertIn("test_win_prec",  result)
            self.assertIn("base_win_rate",  result)
            self.assertIn("lift",           result)
            self.assertIn("top_features",   result)
            self.assertIsInstance(result["top_features"], list)
        finally:
            top._load_raw_data = orig_load
            top.MODEL_DIR  = orig_dir
            top.MODEL_PKL  = orig_pkl
            top.REPORT_JSON = orig_rep

    @unittest.skipUnless(_SKLEARN_AVAILABLE, "scikit-learn not installed")
    def test_train_saves_model_and_report(self):
        import ai.models.trade_outcome_predictor as top
        orig_load = top._load_raw_data
        orig_dir  = top.MODEL_DIR
        orig_pkl  = top.MODEL_PKL
        orig_rep  = top.REPORT_JSON
        self._patch_model_dir(top)
        top._load_raw_data = lambda: _synthetic_rows(150)
        try:
            result = top.train(min_samples=100)
            self.assertTrue(result.get("trained"))
            self.assertTrue(os.path.exists(top.MODEL_PKL))
            self.assertTrue(os.path.exists(top.REPORT_JSON))
            with open(top.REPORT_JSON, encoding="utf-8") as f:
                rep = json.load(f)
            self.assertEqual(rep["trained"], True)
        finally:
            top._load_raw_data = orig_load
            top.MODEL_DIR  = orig_dir
            top.MODEL_PKL  = orig_pkl
            top.REPORT_JSON = orig_rep

    def test_train_never_raises(self):
        """train() must not raise even with a completely broken load."""
        import ai.models.trade_outcome_predictor as top
        orig_load = top._load_raw_data

        def _boom():
            raise RuntimeError("boom")

        top._load_raw_data = _boom
        try:
            result = top.train()
            self.assertIsInstance(result, dict)
        except Exception as exc:
            self.fail(f"train() raised: {exc}")
        finally:
            top._load_raw_data = orig_load


class TestPrediction(unittest.TestCase):

    def test_predict_no_model_returns_none(self):
        import ai.models.trade_outcome_predictor as top
        orig_load = top._load_model
        top._load_model = lambda: None
        try:
            result = top.predict(_make_proposal())
            self.assertIsNone(result)
        finally:
            top._load_model = orig_load

    @unittest.skipUnless(_SKLEARN_AVAILABLE, "scikit-learn not installed")
    def test_predict_returns_float_in_range(self):
        """End-to-end: train on synthetic data, predict on a proposal."""
        import ai.models.trade_outcome_predictor as top
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.preprocessing import StandardScaler

        # Build a minimal model object directly to avoid file I/O
        rows = _synthetic_rows(150)
        X = [top._extract_features(r) for r in rows]
        y = [r["won"] for r in rows]
        scaler = StandardScaler().fit(X)
        model  = GradientBoostingClassifier(
            n_estimators=10, max_depth=2, random_state=0
        ).fit(scaler.transform(X), y)

        fake_obj = {"model": model, "scaler": scaler}
        orig_load = top._load_model
        top._load_model = lambda: fake_obj
        try:
            result = top.predict(_make_proposal())
            self.assertIsNotNone(result)
            self.assertIsInstance(result, float)
            self.assertGreaterEqual(result, 0.0)
            self.assertLessEqual(result, 1.0)
        finally:
            top._load_model = orig_load

    def test_predict_never_raises_on_bad_input(self):
        import ai.models.trade_outcome_predictor as top
        for bad in [None, {}, {"regime": None}, {"side": "BUY"}, 42]:
            try:
                result = top.predict(bad)  # type: ignore
                # must return None or a float — never raise
                self.assertTrue(result is None or isinstance(result, float))
            except Exception as exc:
                self.fail(f"predict({bad!r}) raised: {exc}")

    @unittest.skipUnless(_SKLEARN_AVAILABLE, "scikit-learn not installed")
    def test_recovery_thin_computed_from_economics(self):
        """recovery_thin=1 when ratio is between 0 and 3."""
        import ai.models.trade_outcome_predictor as top
        calls = []
        orig_extract = top._extract_features

        def capture(row):
            calls.append(row.copy())
            return orig_extract(row)

        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.preprocessing import StandardScaler
        rows = _synthetic_rows(150)
        X = [top._extract_features(r) for r in rows]
        y = [r["won"] for r in rows]
        scaler = StandardScaler().fit(X)
        model  = GradientBoostingClassifier(n_estimators=10, max_depth=2).fit(
            scaler.transform(X), y
        )
        orig_load = top._load_model
        top._load_model   = lambda: {"model": model, "scaler": scaler}
        top._extract_features = capture
        try:
            prop = _make_proposal()
            prop["trade_economics"]["recovery_0p5R_to_cost_ratio"] = 2.5
            top.predict(prop)
            self.assertEqual(calls[-1]["recovery_thin"], 1.0)

            calls.clear()
            prop["trade_economics"]["recovery_0p5R_to_cost_ratio"] = 4.0
            top.predict(prop)
            self.assertEqual(calls[-1]["recovery_thin"], 0.0)
        finally:
            top._load_model = orig_load
            top._extract_features = orig_extract


class TestStatus(unittest.TestCase):

    def test_status_returns_expected_keys(self):
        from ai.models.trade_outcome_predictor import status
        s = status()
        for key in ("n_closed_cards", "needed_for_train", "gate_cleared",
                    "model_exists", "trained_at", "test_accuracy",
                    "base_win_rate", "lift"):
            self.assertIn(key, s, f"Missing key: {key}")

    def test_status_never_raises(self):
        import ai.models.trade_outcome_predictor as top
        orig_load = top._load_raw_data

        def _boom():
            raise OSError("boom")

        top._load_raw_data = _boom
        try:
            result = top.status()
            self.assertIsInstance(result, dict)
        except Exception as exc:
            self.fail(f"status() raised: {exc}")
        finally:
            top._load_raw_data = orig_load


class TestProposalIntegration(unittest.TestCase):

    def test_build_proposal_has_top_win_prob_field(self):
        """top_win_prob must always be present in a built proposal."""
        from ai.features.trade_proposal import build_proposal
        import pandas as pd
        bars = pd.DataFrame({
            "Open":   [1.10] * 70, "High":  [1.11] * 70,
            "Low":    [1.09] * 70, "Close": [1.10] * 70,
            "Volume": [1000] * 70,
        })
        prop = build_proposal(
            account_env="sim", strategy="rsi", symbol="EURUSD",
            direction="Buy",
            sig={"close": 1.10, "stop_price": 1.09, "atr": 0.005, "rsi": 8.0},
            features={"agreement_count": 1},
            positions={}, equity=10000.0,
            take_profit=1.12, n_strategies=5,
            regime_bars=bars,
        )
        self.assertIn("top_win_prob", prop)

    def test_build_proposal_top_win_prob_none_when_disabled(self):
        """When outcome_predictor.enabled is false, top_win_prob stays None."""
        import ai.config as cfg
        orig = cfg.outcome_predictor_enabled
        cfg.outcome_predictor_enabled = lambda: False
        from ai.features.trade_proposal import build_proposal
        import pandas as pd
        bars = pd.DataFrame({
            "Open": [1.10]*70, "High": [1.11]*70,
            "Low":  [1.09]*70, "Close": [1.10]*70,
            "Volume": [1000]*70,
        })
        try:
            prop = build_proposal(
                account_env="sim", strategy="rsi", symbol="EURUSD",
                direction="Buy",
                sig={"close": 1.10, "stop_price": 1.09, "atr": 0.005, "rsi": 8.0},
                features={"agreement_count": 1},
                positions={}, equity=10000.0,
                take_profit=1.12, n_strategies=5,
                regime_bars=bars,
            )
            self.assertIsNone(prop["top_win_prob"])
        finally:
            cfg.outcome_predictor_enabled = orig


class TestGovernance(unittest.TestCase):

    def test_no_forbidden_imports(self):
        """AST check: predictor must not import trading-capable modules."""
        import ai.models.trade_outcome_predictor as top
        source_path = top.__file__
        with open(source_path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)

        forbidden = {
            "forex.runner", "atos_runner", "saxo_client", "saxo_order",
            "saxo_account", "pnl_tracker", "housekeeping", "safeguard",
        }
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = ""
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module = alias.name
                        for f in forbidden:
                            self.assertNotIn(
                                f, module,
                                f"Forbidden import '{f}' found in predictor"
                            )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                    for f in forbidden:
                        self.assertNotIn(
                            f, module,
                            f"Forbidden import '{f}' found in predictor"
                        )

    def test_outcome_predictor_config_gate_exists(self):
        from ai.config import outcome_predictor_enabled, outcome_predictor_cfg
        # Must return bool / dict without raising
        self.assertIsInstance(outcome_predictor_enabled(), bool)
        cfg = outcome_predictor_cfg()
        self.assertIsInstance(cfg, dict)
        self.assertIn("enabled", cfg)

    def test_config_default_is_off(self):
        """outcome_predictor ships OFF in defaults."""
        from ai.config import _DEFAULTS
        op = _DEFAULTS.get("outcome_predictor", {})
        self.assertFalse(op.get("enabled"), "outcome_predictor must ship disabled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
