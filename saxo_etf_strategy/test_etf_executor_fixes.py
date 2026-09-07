"""
Unit tests for two ETF executor fixes:
  1. Dry-run must NOT mutate local state (remove_position / log_order must not be called)
  2. CouldNotCompleteRequest (code 90) must NOT crash the trim — position kept in state
"""
import sys, os, types, unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# ETF root must be first so its 'core' beats the parent project's 'core'.
# ---------------------------------------------------------------------------
_ETF_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ETF_ROOT not in sys.path:
    sys.path.insert(0, _ETF_ROOT)

# ---------------------------------------------------------------------------
# Stub every external/parent module BEFORE any ETF imports so we don't pull
# in live credentials, DB files, or the parent project's 'config' package.
# ---------------------------------------------------------------------------
for _mod in ("pnl_tracker", "trade_logger", "saxo_order", "saxo_auth"):
    _stub = types.ModuleType(_mod)
    _stub.log_trade = MagicMock()
    _stub.log_close  = MagicMock()
    _stub.get_valid_access_token = MagicMock(return_value="tok")
    sys.modules.setdefault(_mod, _stub)

# Stub config.etf_config so etf_strategy.py can import it without hitting
# the parent project's config/ directory (which has no __init__.py).
_cfg_pkg = types.ModuleType("config")
_cfg_mod = types.ModuleType("config.etf_config")

class _FakeStratCfg:
    strategy_name = "dual_ma"
    max_candidates_per_run = 10
    rebalance_frequency_hours = 1
    stop_loss_pct = 0.08
    take_profit_pct = 0.20

class _FakeRiskCfg:
    etf_account_key = "TEST_KEY"
    max_position_size_eur = 5000
    trailing_stop_high_pct = 0.08

class _FakeUniverseCfg:
    max_retries = 1
    request_delay_sec = 0

class _FakeEnvCfg:
    base_url = "https://gateway.saxobank.com/sim/openapi"

class _FakeCfg:
    dry_run = False
    strategy = _FakeStratCfg()
    risk = _FakeRiskCfg()
    universe = _FakeUniverseCfg()
    env = _FakeEnvCfg()
    state_path = ""
    log_path   = "etf.log"

_cfg_mod.ETFStrategyConfig = _FakeStratCfg
_cfg_mod.ETFRiskConfig = _FakeRiskCfg
_cfg_mod.ETFUniverseConfig = _FakeUniverseCfg
_cfg_mod.ETFEnvironmentConfig = _FakeEnvCfg
_cfg_mod.ETFConfig = _FakeCfg
_cfg_mod.DEFAULT_CONFIG = _FakeCfg()
_cfg_pkg.etf_config = _cfg_mod
sys.modules.setdefault("config", _cfg_pkg)
sys.modules.setdefault("config.etf_config", _cfg_mod)

# Now it's safe to import ETF internals.
from core.saxo_client import SaxoAPIError
from core.etf_executor import ETFExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor(dry_run: bool) -> ETFExecutor:
    cfg = _FakeCfg()
    cfg.dry_run = dry_run
    client = MagicMock()
    state  = MagicMock()
    ex = ETFExecutor.__new__(ETFExecutor)
    ex.client = client
    ex.state  = state
    ex.cfg    = cfg
    ex._account_key = "TEST_ACCT_KEY"
    return ex


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDryRunDoesNotMutateState(unittest.TestCase):
    """_exit_position with dry_run=True must return without touching state."""

    def setUp(self):
        self.ex = _make_executor(dry_run=True)

    def _call(self):
        self.ex._exit_position(uic=12345,
                               pos={"symbol": "VTI", "quantity": 10},
                               live_price=220.0,
                               reason="RANK_TRIM")

    def test_remove_position_not_called(self):
        self._call()
        self.ex.state.remove_position.assert_not_called()

    def test_log_order_not_called(self):
        self._call()
        self.ex.state.log_order.assert_not_called()

    def test_saxo_post_not_called(self):
        self._call()
        self.ex.client.post.assert_not_called()


class TestCode90DoesNotCrash(unittest.TestCase):
    """CouldNotCompleteRequest (code 90) must be swallowed; position kept."""

    def setUp(self):
        self.ex = _make_executor(dry_run=False)
        self.ex.client.post.side_effect = SaxoAPIError(400, "CouldNotCompleteRequest (90)")

    def _call(self):
        self.ex._exit_position(uic=99999,
                               pos={"symbol": "GLD", "quantity": 5},
                               live_price=400.0,
                               reason="RANK_TRIM")

    def test_does_not_raise(self):
        self._call()  # must not raise

    def test_remove_position_not_called(self):
        self._call()
        self.ex.state.remove_position.assert_not_called()

    def test_log_order_not_called(self):
        self._call()
        self.ex.state.log_order.assert_not_called()


class TestSellOrdersAlreadyExist(unittest.TestCase):
    """SellOrdersAlreadyExistForOwnedContracts must still remove from local state."""

    def setUp(self):
        self.ex = _make_executor(dry_run=False)
        self.ex.client.post.side_effect = SaxoAPIError(409, "SellOrdersAlreadyExistForOwnedContracts")

    def test_remove_position_called(self):
        self.ex._exit_position(uic=11111,
                               pos={"symbol": "XLB", "quantity": 50},
                               live_price=53.0,
                               reason="RANK_TRIM")
        self.ex.state.remove_position.assert_called_once_with(11111)


class TestSuccessfulExit(unittest.TestCase):
    """Normal (no exception) path: state is updated."""

    def setUp(self):
        self.ex = _make_executor(dry_run=False)
        self.ex.client.post.return_value = {"OrderId": "ORD123"}

    def test_remove_position_called(self):
        self.ex._exit_position(uic=22222,
                               pos={"symbol": "VTI", "quantity": 20},
                               live_price=230.0,
                               reason="STOP_LOSS")
        self.ex.state.remove_position.assert_called_once_with(22222)

    def test_log_order_called(self):
        self.ex._exit_position(uic=22222,
                               pos={"symbol": "VTI", "quantity": 20},
                               live_price=230.0,
                               reason="STOP_LOSS")
        self.ex.state.log_order.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
