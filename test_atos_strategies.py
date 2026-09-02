"""
test_atos_strategies.py
------------------------
Direct signal-logic tests for atos/us_momentum.py (US Blend) and
atos/us_reversion.py (US Reversion) — both PURE functions (no I/O, no
Saxo/Yahoo calls), yet neither had a single direct unit test before this
file existed. test_atos_signal.py is a live-data debug script with no
assertions; test_2026_08_22_session_fixes.py only source-inspects
atos_runner.py. This drives the actual scan()/compute_targets()/
should_exit() functions with synthetic, controlled price panels.

Run:  python test_atos_strategies.py
"""

import os
import sys

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import atos.us_momentum as mom
import atos.us_reversion as rev

GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
)
_results = []


def _run(name, fn):
    try:
        result = fn()
        if result is None:
            result = True
        _results.append((name, bool(result), None))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


def section(title):
    print(f"\n{BOLD}{CYAN}{'-'*70}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'-'*70}{RESET}")


# ── Synthetic data helpers ──────────────────────────────────────────────

def _uptrend_close(n=260, start=100.0, daily_ret=0.006, noise=0.002, seed=1):
    """Steady uptrend -- ends well above its own EMA200/SMA20, with a real
    multi-month return, exactly what the momentum offense screen wants."""
    rng = np.random.default_rng(seed)
    rets = daily_ret + rng.normal(0, noise, n)
    px = start * np.cumprod(1 + rets)
    return pd.Series(px)


def _flat_close(n=260, start=100.0, noise=0.002, seed=2):
    """No net drift -- fails the momentum return threshold but should
    still be eligible for the low-volatility defense sleeve."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, noise, n)
    px = start * np.cumprod(1 + rets)
    return pd.Series(px)


def _downtrend_close(n=260, start=100.0, daily_ret=-0.004, noise=0.002, seed=3):
    rng = np.random.default_rng(seed)
    rets = daily_ret + rng.normal(0, noise, n)
    px = start * np.cumprod(1 + rets)
    return pd.Series(px)


def _mom_feat_data(tickers_up, tickers_flat=None, tickers_down=None, n=260):
    tickers_flat = tickers_flat or []
    tickers_down = tickers_down or []
    feat = {}
    seed = 10
    for t in tickers_up:
        feat[t] = pd.DataFrame({"Close": _uptrend_close(n, seed=seed)}); seed += 1
    for t in tickers_flat:
        feat[t] = pd.DataFrame({"Close": _flat_close(n, seed=seed)}); seed += 1
    for t in tickers_down:
        feat[t] = pd.DataFrame({"Close": _downtrend_close(n, seed=seed)}); seed += 1
    return feat


def _reversion_dip_df(n=260, base=100.0, dip_pct=0.10, vol_mult=2.0, seed=42):
    """A stock in a long-term uptrend (above EMA200) that just crashed hard
    and STAYS at the bottom on the final bar -- the exact reversion setup.
    A crash followed by any recovery would drag the 20-day SMA back down
    toward price and shrink dip_pct below DIP_PCT, so the series ends flat
    at the low instead of bouncing."""
    rng = np.random.default_rng(seed)
    lead = n - 3
    rets = 0.0020 + rng.normal(0, 0.002, lead)
    px = list(base * np.cumprod(1 + rets))
    pre_drop = px[-1]
    px.append(pre_drop * (1 - (dip_pct + 0.03) / 2))
    px.append(pre_drop * (1 - (dip_pct + 0.03)))
    px.append(px[-1])
    closes = pd.Series(px[:n])
    volume = pd.Series(rng.integers(900_000, 1_100_000, n).astype(float))
    volume.iloc[-1] = volume.iloc[:-1].tail(20).mean() * vol_mult
    return pd.DataFrame({"Close": closes, "Volume": volume})


def _flat_healthy_df(n=260, base=100.0, seed=99):
    """A stock that never dips -- must never fire a reversion signal."""
    rng = np.random.default_rng(seed)
    rets = 0.0005 + rng.normal(0, 0.001, n)
    closes = pd.Series(base * np.cumprod(1 + rets))
    volume = pd.Series(rng.integers(900_000, 1_100_000, n).astype(float))
    return pd.DataFrame({"Close": closes, "Volume": volume})


# ── us_momentum: market_risk_off / compute_targets ──────────────────────
section("us_momentum: market_risk_off / compute_targets")


def test_risk_off_true_when_index_below_200d_sma():
    feat = _mom_feat_data(tickers_up=[], tickers_down=["A", "B", "C", "D"])
    out = mom.compute_targets(feat, ["A", "B", "C", "D"])
    assert out["risk_off"] is True
    assert out["targets"] == []


def test_risk_off_false_and_targets_populated_in_uptrend():
    feat = _mom_feat_data(tickers_up=["A", "B", "C"], tickers_flat=["D", "E"])
    out = mom.compute_targets(feat, ["A", "B", "C", "D", "E"])
    assert out["risk_off"] is False
    assert set(["A", "B", "C"]).issubset(set(out["momentum"])), out


def test_flat_stocks_excluded_from_offense_but_eligible_for_defense():
    feat = _mom_feat_data(tickers_up=["A", "B"], tickers_flat=["C", "D"])
    out = mom.compute_targets(feat, ["A", "B", "C", "D"])
    assert "C" not in out["momentum"] and "D" not in out["momentum"], (
        "a flat stock with < MOM_THRESHOLD return must not qualify for offense")
    assert set(out["lowvol"]).issubset({"A", "B", "C", "D"})


def test_insufficient_universe_returns_no_targets():
    feat = _mom_feat_data(tickers_up=["A", "B"])  # only 2 tickers, _panel requires >= 4
    out = mom.compute_targets(feat, ["A", "B"])
    assert out["targets"] == [] and out["risk_off"] is False


def test_targets_are_deduplicated_offense_first():
    feat = _mom_feat_data(tickers_up=["A", "B", "C"], tickers_flat=["D"])
    out = mom.compute_targets(feat, ["A", "B", "C", "D"])
    assert len(out["targets"]) == len(set(out["targets"])), "no duplicate tickers"
    if out["momentum"]:
        assert out["targets"][0] in out["momentum"], "offense picks listed first"


# ── us_momentum: plan_rebalance ──────────────────────────────────────────
section("us_momentum: plan_rebalance")


def test_rebalance_exits_positions_no_longer_in_targets():
    actions = mom.plan_rebalance(
        current_shares={"OLD": 10}, targets=["NEW"], scale=1.0,
        prices_usd={"NEW": 50.0}, sleeve_sek=100_000, fx_usd_sek=10.0)
    sells = [a for a in actions if a["ticker"] == "OLD" and a["side"] == "Sell"]
    assert len(sells) == 1 and sells[0]["shares"] == 10


def test_rebalance_buys_up_to_target_share_count():
    actions = mom.plan_rebalance(
        current_shares={}, targets=["A"], scale=1.0,
        prices_usd={"A": 100.0}, sleeve_sek=1_000_000, fx_usd_sek=10.0)
    buys = [a for a in actions if a["ticker"] == "A" and a["side"] == "Buy"]
    assert len(buys) == 1 and buys[0]["shares"] > 0


def test_rebalance_skips_trivial_drift_within_threshold():
    # per_usd/price = 1,000,000/10.0/100.0 = 1000, capped at MAX_SHARES_PER_NAME=50.
    # Hold exactly the (capped) target -> delta 0 -> no action.
    actions = mom.plan_rebalance(
        current_shares={"A": 50}, targets=["A"], scale=1.0,
        prices_usd={"A": 100.0}, sleeve_sek=1_000_000, fx_usd_sek=10.0)
    assert actions == [], f"position already at target must not be re-traded, got {actions}"


def test_rebalance_caps_shares_at_max_per_name():
    actions = mom.plan_rebalance(
        current_shares={}, targets=["CHEAP"], scale=1.0,
        prices_usd={"CHEAP": 1.0}, sleeve_sek=10_000_000, fx_usd_sek=10.0)
    buy = next(a for a in actions if a["ticker"] == "CHEAP")
    assert buy["shares"] <= 50, "must never exceed MAX_SHARES_PER_NAME regardless of budget"


def test_rebalance_no_targets_only_exits():
    actions = mom.plan_rebalance(
        current_shares={"OLD": 5}, targets=[], scale=1.0,
        prices_usd={}, sleeve_sek=100_000, fx_usd_sek=10.0)
    assert actions == [{"ticker": "OLD", "side": "Sell", "shares": 5}]


_SMALL_SLEEVE = dict(   # 30k sleeve / 6 names / fx 10 -> per_usd = $500/slot
    targets=["N1", "N2", "N3", "N4", "N5", "N6"], scale=1.0,
    sleeve_sek=30_000, fx_usd_sek=10.0)


def test_rebalance_small_sleeve_floors_slightly_pricey_target_to_1_share():
    # $500/slot; N1 at $560 (1.12x) rounds to 0 shares and would drop out of the
    # equal-weight basket -- the small-sleeve rule floors it to 1 share.
    actions = mom.plan_rebalance(
        current_shares={},
        prices_usd={"N1": 560.0, "N2": 40.0, "N3": 40.0, "N4": 40.0, "N5": 40.0, "N6": 40.0},
        **_SMALL_SLEEVE)
    n1 = [a for a in actions if a["ticker"] == "N1"]
    assert n1 == [{"ticker": "N1", "side": "Buy", "shares": 1}], actions


def test_rebalance_does_not_floor_a_target_far_above_the_slot_budget():
    # $500/slot; a $2,000 stock (4x) is a genuine affordability limit -- still
    # skipped, not force-bought.
    actions = mom.plan_rebalance(
        current_shares={},
        prices_usd={"N1": 2_000.0, "N2": 40.0, "N3": 40.0, "N4": 40.0, "N5": 40.0, "N6": 40.0},
        **_SMALL_SLEEVE)
    assert not [a for a in actions if a["ticker"] == "N1"], actions


def test_rebalance_floor_does_not_touch_an_existing_position():
    # We already hold 1 share of a name that now rounds to 0 target shares
    # ($560 vs a $500/slot budget across 6 names) -> the floor rule
    # (cur==0 only) doesn't fire; delta -1 -> Sell 1.
    actions = mom.plan_rebalance(
        current_shares={"HELD": 1},
        targets=["HELD", "T2", "T3", "T4", "T5", "T6"], scale=1.0,
        prices_usd={"HELD": 560.0}, sleeve_sek=30_000, fx_usd_sek=10.0)
    assert actions == [{"ticker": "HELD", "side": "Sell", "shares": 1}]


# ── us_reversion: scan() ─────────────────────────────────────────────────
section("us_reversion: scan()")


def test_reversion_fires_on_full_dip_setup():
    feat = {"DIP": _reversion_dip_df()}
    hits = rev.scan(feat, ["DIP"])
    assert len(hits) == 1, f"expected exactly 1 candidate, got {hits}"
    h = hits[0]
    assert h["ticker"] == "DIP"
    assert h["rsi"] < rev.RSI_ENTRY
    assert h["dip_pct"] >= rev.DIP_PCT * 100 - 0.5  # rounding tolerance
    assert h["vol_ratio"] >= rev.VOL_MULT


def test_reversion_does_not_fire_on_a_stock_that_never_dipped():
    feat = {"HEALTHY": _flat_healthy_df()}
    hits = rev.scan(feat, ["HEALTHY"])
    assert hits == [], f"a stock with no dip/no oversold RSI/no volume spike must not signal, got {hits}"


def test_reversion_rejects_without_volume_spike():
    df = _reversion_dip_df(vol_mult=1.0)  # dip present, but no capitulation volume
    hits = rev.scan({"NODIPVOL": df}, ["NODIPVOL"])
    assert hits == [], "a dip without a volume spike must not qualify"


def test_reversion_rejects_insufficient_history():
    df = _reversion_dip_df(n=100)  # below the 220-bar minimum
    hits = rev.scan({"SHORT": df}, ["SHORT"])
    assert hits == []


def test_reversion_candidates_ranked_by_score_descending():
    shallow = _reversion_dip_df(dip_pct=0.055, seed=11)
    deep = _reversion_dip_df(dip_pct=0.15, seed=12)
    hits = rev.scan({"SHALLOW": shallow, "DEEP": deep}, ["SHALLOW", "DEEP"])
    if len(hits) == 2:
        assert hits[0]["ticker"] == "DEEP", (
            "a deeper/more-oversold dip must rank first")


def test_reversion_missing_volume_column_is_skipped_not_crashed():
    df = pd.DataFrame({"Close": _uptrend_close(260)})  # no Volume column
    hits = rev.scan({"NOVOL": df}, ["NOVOL"])
    assert hits == []


# ── us_reversion: should_exit() ──────────────────────────────────────────
section("us_reversion: should_exit()")


def test_reversion_time_stop_fires_at_max_hold():
    trade = {"entry_price": 100.0}
    exit_flag, reason = rev.should_exit(trade, current_price=101.0, current_rsi=45.0,
                                        sma20=105.0, trading_days_held=rev.MAX_HOLD_DAYS)
    assert exit_flag and "time-stop" in reason


def test_reversion_hard_stop_fires_on_loss():
    trade = {"entry_price": 100.0}
    stop_px = 100.0 * (1 - rev.STOP_PCT) - 0.01
    exit_flag, reason = rev.should_exit(trade, current_price=stop_px, current_rsi=45.0,
                                        sma20=105.0, trading_days_held=1)
    assert exit_flag and "stop-loss" in reason


def test_reversion_rsi_recovery_exit():
    trade = {"entry_price": 100.0}
    exit_flag, reason = rev.should_exit(trade, current_price=102.0,
                                        current_rsi=rev.RSI_EXIT + 1, sma20=110.0,
                                        trading_days_held=2)
    assert exit_flag and "RSI recovery" in reason


def test_reversion_sma20_target_hit_exit():
    trade = {"entry_price": 100.0}
    exit_flag, reason = rev.should_exit(trade, current_price=105.0, current_rsi=45.0,
                                        sma20=104.0, trading_days_held=2)
    assert exit_flag and "SMA20 target" in reason


def test_reversion_no_exit_when_nothing_triggers():
    trade = {"entry_price": 100.0}
    exit_flag, reason = rev.should_exit(trade, current_price=101.0, current_rsi=45.0,
                                        sma20=110.0, trading_days_held=2)
    assert not exit_flag and reason == ""


def test_reversion_invalid_entry_price_never_exits():
    trade = {"entry_price": 0}
    exit_flag, reason = rev.should_exit(trade, current_price=101.0, current_rsi=90.0,
                                        sma20=50.0, trading_days_held=99)
    assert not exit_flag, "a malformed trade record (no entry_price) must fail safe, not force an exit"


_run("risk-off gate: True when index below its 200d SMA", test_risk_off_true_when_index_below_200d_sma)
_run("risk-off gate: False + targets populated in an uptrend", test_risk_off_false_and_targets_populated_in_uptrend)
_run("flat stocks excluded from offense, still eligible for defense", test_flat_stocks_excluded_from_offense_but_eligible_for_defense)
_run("fewer than 4 usable tickers -> no targets, no crash", test_insufficient_universe_returns_no_targets)
_run("targets deduplicated, offense listed first", test_targets_are_deduplicated_offense_first)
_run("rebalance: exits positions no longer in the target set", test_rebalance_exits_positions_no_longer_in_targets)
_run("rebalance: buys up to the target share count", test_rebalance_buys_up_to_target_share_count)
_run("rebalance: skips a position already at its target (idempotent)", test_rebalance_skips_trivial_drift_within_threshold)
_run("rebalance: caps shares at MAX_SHARES_PER_NAME for cheap tickers", test_rebalance_caps_shares_at_max_per_name)
_run("rebalance: empty targets only produces exits", test_rebalance_no_targets_only_exits)
_run("rebalance: small sleeve floors a slightly-pricey target to 1 share (DELL fix)",
     test_rebalance_small_sleeve_floors_slightly_pricey_target_to_1_share)
_run("rebalance: a target far above the slot budget is still skipped, not force-bought",
     test_rebalance_does_not_floor_a_target_far_above_the_slot_budget)
_run("rebalance: the 1-share floor never touches an existing position",
     test_rebalance_floor_does_not_touch_an_existing_position)
_run("reversion fires on the full dip+oversold+volume setup", test_reversion_fires_on_full_dip_setup)
_run("reversion does not fire on a stock that never dipped", test_reversion_does_not_fire_on_a_stock_that_never_dipped)
_run("reversion rejects a dip with no volume spike", test_reversion_rejects_without_volume_spike)
_run("reversion rejects fewer than 220 bars of history", test_reversion_rejects_insufficient_history)
_run("reversion candidates ranked deepest/most-oversold first", test_reversion_candidates_ranked_by_score_descending)
_run("reversion skips a ticker with no Volume column instead of crashing", test_reversion_missing_volume_column_is_skipped_not_crashed)
_run("reversion time-stop fires at MAX_HOLD_DAYS", test_reversion_time_stop_fires_at_max_hold)
_run("reversion hard stop-loss fires on a real loss", test_reversion_hard_stop_fires_on_loss)
_run("reversion RSI-recovery exit fires above RSI_EXIT", test_reversion_rsi_recovery_exit)
_run("reversion SMA20-target exit fires on mean-reversion completion", test_reversion_sma20_target_hit_exit)
_run("reversion: no exit when none of the 4 conditions trigger", test_reversion_no_exit_when_nothing_triggers)
_run("reversion: a malformed trade (entry_price<=0) never forces an exit", test_reversion_invalid_entry_price_never_exits)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results if ok)
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] {name}")
    if err:
        print(f"         {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*70}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} TESTS FAILED{RESET}")
    sys.exit(1)
else:
    print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
    sys.exit(0)
