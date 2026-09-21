"""
Unit tests for DualMAStrategy entry-quality filters:
  1. Slope filter  — rejects when SMA20 is flat/falling
  2. Freshness filter — rejects when crossover is older than max_age days
  3. Both filters pass — signal is generated correctly
"""
import sys, os, types, unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# ETF root on sys.path first
# ---------------------------------------------------------------------------
_ETF_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ETF_ROOT not in sys.path:
    sys.path.insert(0, _ETF_ROOT)

# Stub external modules
for _mod in ("pnl_tracker", "trade_logger", "saxo_order", "saxo_auth"):
    _stub = types.ModuleType(_mod)
    _stub.log_trade = MagicMock()
    _stub.log_close  = MagicMock()
    _stub.get_valid_access_token = MagicMock(return_value="tok")
    sys.modules.setdefault(_mod, _stub)

# Stub config package
_cfg_pkg = types.ModuleType("config")
_cfg_mod = types.ModuleType("config.etf_config")

class _FakeStratCfg:
    strategy_name              = "dual_ma"
    lookback_days_fast         = 20
    lookback_days_slow         = 100
    max_candidates_per_run     = 10
    rebalance_frequency_hours  = 24
    crossover_max_age_days     = 10
    slope_lookback_days        = 5

_cfg_mod.ETFStrategyConfig = _FakeStratCfg
_cfg_pkg.etf_config = _cfg_mod
sys.modules.setdefault("config", _cfg_pkg)
sys.modules.setdefault("config.etf_config", _cfg_mod)

from core.etf_strategy import DualMAStrategy
from core.saxo_client import SaxoClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_strategy(max_age: int = 10, slope_n: int = 5) -> DualMAStrategy:
    cfg = _FakeStratCfg()
    cfg.crossover_max_age_days = max_age
    cfg.slope_lookback_days    = slope_n
    client = MagicMock(spec=SaxoClient)
    strat = DualMAStrategy(client=client, cfg=cfg)
    strat.UNIVERSE = ["SPY"]   # single symbol so len(signals) is 0 or 1
    return strat


def _trending_up_closes(n: int, start: float = 90.0, step: float = 0.15):
    """Steadily rising: SMA20 > SMA100, slope always positive."""
    return [start + i * step for i in range(n)]


def _slope_falling(slow_period: int = 100, fast_period: int = 20,
                   extra: int = 12, slope_n: int = 5):
    """
    Price rises for most of the series, then drops sharply for slope_n bars.
    Result: SMA20_today < SMA20_slope_n_days_ago — slope filter should reject.
    The fast MA still exceeds the slow MA (no crossover reversal yet).
    """
    n = slow_period + extra          # 112
    rises = n - slope_n              # 107 rising bars
    rising = [90.0 + i * 0.2 for i in range(rises)]
    peak = rising[-1]
    # Sharp fall (2.5/bar) ensures SMA20 clearly declines over slope_n bars
    falling = [peak - (i + 1) * 2.5 for i in range(slope_n)]
    return rising + falling


def _crossover_just_now(slow_period: int = 100, fast_period: int = 20,
                        extra: int = 12, age_days: int = 3):
    """
    Flat for most bars (SMA20 ≈ SMA100), then a sharp 3-bar spike so
    the crossover occurred within age_days — freshness filter should pass.
    """
    n = slow_period + extra          # 112
    pre  = [100.0] * (n - age_days)
    post = [100.0 + (i + 1) * 2.0 for i in range(age_days)]
    return pre + post


def _crossover_long_ago(slow_period: int = 100, fast_period: int = 20,
                        extra: int = 12):
    """Steadily rising for full series — crossover happened >max_age days ago."""
    return _trending_up_closes(n=slow_period + extra)


def _fake_universe(symbol: str = "SPY", uic: int = 1234) -> list:
    return [{"Symbol": symbol, "Identifier": uic,
             "Description": f"{symbol} ETF", "ExchangeId": "NYSE_ARCA",
             "CurrencyCode": "USD"}]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSlopeFilter(unittest.TestCase):
    """Slope filter: reject when SMA20 is declining vs N days ago."""

    def _run(self, closes, max_age=0, slope_n=5):
        strat    = _make_strategy(max_age=max_age, slope_n=slope_n)
        universe = _fake_universe()
        with patch.object(strat, "_find", return_value=universe[0]), \
             patch.object(strat, "_history", return_value=closes):
            return strat.generate_signals(universe)

    def test_rejects_falling_slope(self):
        closes = _slope_falling(slope_n=5)
        signals = self._run(closes, max_age=0, slope_n=5)
        self.assertEqual(signals, [], "Falling SMA20 should be rejected")

    def test_accepts_rising_slope(self):
        closes = _trending_up_closes(n=112)
        signals = self._run(closes, max_age=0, slope_n=5)
        self.assertEqual(len(signals), 1, "Rising SMA20 should produce a signal")

    def test_disabled_when_slope_n_is_zero(self):
        closes = _slope_falling(slope_n=5)
        signals = self._run(closes, max_age=0, slope_n=0)
        self.assertEqual(len(signals), 1, "slope_lookback_days=0 should disable slope filter")


class TestFreshnessFilter(unittest.TestCase):
    """Freshness filter: reject when crossover is older than max_age_days."""

    def _run(self, closes, max_age=10, slope_n=0):
        strat    = _make_strategy(max_age=max_age, slope_n=slope_n)
        universe = _fake_universe()
        with patch.object(strat, "_find", return_value=universe[0]), \
             patch.object(strat, "_history", return_value=closes):
            return strat.generate_signals(universe)

    def test_rejects_old_crossover(self):
        closes = _crossover_long_ago()
        signals = self._run(closes, max_age=10, slope_n=0)
        self.assertEqual(signals, [], "Crossover >max_age days old should be rejected")

    def test_accepts_fresh_crossover(self):
        closes = _crossover_just_now(age_days=3)
        signals = self._run(closes, max_age=10, slope_n=0)
        self.assertEqual(len(signals), 1, "Fresh crossover (3d ago) should produce a signal")
        self.assertEqual(signals[0].symbol, "SPY")

    def test_disabled_when_max_age_is_zero(self):
        closes = _crossover_long_ago()
        signals = self._run(closes, max_age=0, slope_n=0)
        self.assertEqual(len(signals), 1, "crossover_max_age_days=0 should disable freshness filter")


class TestBothFiltersActive(unittest.TestCase):
    """With both filters active, only a genuinely fresh + rising crossover passes."""

    def _run(self, closes, max_age=10, slope_n=5):
        strat    = _make_strategy(max_age=max_age, slope_n=slope_n)
        universe = _fake_universe()
        with patch.object(strat, "_find", return_value=universe[0]), \
             patch.object(strat, "_history", return_value=closes):
            return strat.generate_signals(universe)

    def test_fresh_and_rising_produces_signal(self):
        closes  = _crossover_just_now(age_days=3)
        signals = self._run(closes, max_age=10, slope_n=5)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].symbol, "SPY")

    def test_old_crossover_rejected_even_if_still_rising(self):
        closes  = _crossover_long_ago()
        signals = self._run(closes, max_age=10, slope_n=5)
        self.assertEqual(signals, [])

    def test_falling_slope_rejected_even_if_crossover_fresh(self):
        closes  = _slope_falling(slope_n=5)
        # Make it "fresh" by setting max_age large enough not to trigger
        signals = self._run(closes, max_age=0, slope_n=5)
        self.assertEqual(signals, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
