"""
2026-09-02 -- the AI layer extended to the SIM stocks module.

Journal (read-only) + shadow Trading Copilot on US Reversion + shadow
basket-ranker for US Blend. All OBSERVE/LOG only, ships OFF
(config/ai.json "stocks": {"enabled": false}).

Verifies: default-off gating, the sibling proposal builder's schema, a
stocks card round-tripping through the Journal, SEK->EUR conversion, the
atos_runner hooks being inert when disabled, and -- structurally -- that
no stocks-AI code path can place / size / skip a trade.
"""

import ast
import inspect
import json
import os
import sys
import tempfile

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


import ai.config as ai_config
import ai.features.stock_cards as stock_cards
import ai.features.stock_proposal as stock_proposal
import ai.features.basket_ranker as basket_ranker
import ai.features.trade_journal as trade_journal
from ai.features.trade_proposal import REQUIRED_FIELDS


# ── 1. config: default-off ────────────────────────────────────────────────

def test_stocks_flags_default_off():
    real = ai_config._load
    try:
        ai_config._load = lambda: {}                       # empty config
        assert ai_config.stocks_enabled() is False
        assert ai_config.stocks_journal_enabled() is False
        assert ai_config.stocks_reversion_copilot_enabled() is False
        assert ai_config.stocks_basket_ranker_enabled() is False
        ai_config._load = lambda: {"stocks": "not-a-dict"}  # malformed
        assert ai_config.stocks_enabled() is False
    finally:
        ai_config._load = real


def test_committed_config_ships_stocks_off():
    with open(os.path.join(BASE, "config", "ai.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg.get("stocks", {}).get("enabled") is False, "stocks.enabled must ship false"


def test_sub_flags_need_master_and_own_gate():
    real = ai_config._load
    try:
        # master on, journal sub-flag on, but journal_enabled() off -> still False
        ai_config._load = lambda: {"stocks": {"enabled": True, "journal": True},
                                   "journal_enabled": False}
        assert ai_config.stocks_journal_enabled() is False
        ai_config._load = lambda: {"stocks": {"enabled": True, "journal": True},
                                   "journal_enabled": True}
        assert ai_config.stocks_journal_enabled() is True
        # copilot sub-flags also need agent_enabled + enabled_sim
        ai_config._load = lambda: {"stocks": {"enabled": True, "shadow_copilot_reversion": True},
                                   "agent_enabled": True, "enabled_sim": True}
        assert ai_config.stocks_reversion_copilot_enabled() is True
        ai_config._load = lambda: {"stocks": {"enabled": True, "shadow_copilot_reversion": True},
                                   "agent_enabled": False, "enabled_sim": True}
        assert ai_config.stocks_reversion_copilot_enabled() is False
    finally:
        ai_config._load = real


# ── 2. proposal builder ──────────────────────────────────────────────────

def test_build_stock_proposal_full_schema():
    p = stock_proposal.build_stock_proposal(
        strategy="us_reversion", ticker="AAPL", entry_price=200.0,
        stop_price=192.0, target_price=205.0, rsi14=33.0, shares=60,
        daily_vol_pct=1.4, risk_eur=42.0, account_equity_eur=30000.0,
        regime_bars=None)
    assert isinstance(p, dict) and p
    for k in REQUIRED_FIELDS:
        assert k in p, f"missing REQUIRED_FIELD {k}"
    assert p["side"] == "BUY" and p["timeframe"] == "D1"
    assert p["strategy_name"] == "us_reversion"
    assert p["rsi2"] == 33.0
    assert p["stop_loss"] == 192.0 and p["take_profit"] == 205.0
    assert p["trade_economics"]["risk_eur"] == 42.0
    assert p["trade_economics"]["reward_risk_ratio"] == round(5.0 / 8.0, 2)


def test_build_stock_proposal_never_raises_on_junk():
    for bad in (dict(strategy="x", ticker="Z", entry_price=None, stop_price=None,
                     target_price=None, rsi14=None, shares=None, daily_vol_pct=None,
                     risk_eur=None, account_equity_eur=None),
                dict(strategy="x", ticker="Z", entry_price="oops", stop_price=[],
                     target_price={}, rsi14="?", shares=-1, daily_vol_pct="a",
                     risk_eur="b", account_equity_eur=None)):
        r = stock_proposal.build_stock_proposal(**bad)
        assert isinstance(r, dict)   # {} or a partial dict, never an exception


# ── 3. cards + Journal round-trip ────────────────────────────────────────

def test_card_round_trips_through_journal(monkeypatch=None):
    d = tempfile.mkdtemp()
    stock_cards.STOCK_CARDS_LOG = os.path.join(d, "stock_cards.jsonl")
    trade_journal.STOCK_CARDS_LOG = stock_cards.STOCK_CARDS_LOG
    trade_journal.CARDS_LOG = os.path.join(d, "empty_fx.jsonl")

    cid = stock_cards.log_stock_entry_card(
        strategy="us_reversion", ticker="ROST", direction="Buy",
        entry_price=120.0, shares=40, stop_price=115.2, sek_per_eur=11.4,
        entry_date="2026-09-02", risk_sek=2188.8, rsi_at_entry=31.0,
        sma20_target=126.0)
    assert cid == "sim:us_reversion:ROST:2026-09-02"
    stock_cards.log_stock_exit_card(
        card_id=cid, exit_price=126.5, exit_reason="SMA20 target hit",
        gross_pnl_sek=2964.0, commission_sek=45.0, net_pnl_sek=2919.0,
        holding_hours=96.0, sek_per_eur=11.4, risk_sek=2188.8)

    real = ai_config.stocks_journal_enabled
    try:
        ai_config.stocks_journal_enabled = lambda: True
        trades = trade_journal._closed_trades()
    finally:
        ai_config.stocks_journal_enabled = real
    assert len(trades) == 1
    t = trades[0]
    assert t["market"] == "equity" and t["symbol"] == "ROST"
    assert t["net_pnl_eur"] == round(2919.0 / 11.4, 2)      # SEK -> EUR at write
    assert t["net_pnl_native"] == 2919.0
    assert t["r_multiple"] == round(2919.0 / 2188.8, 2)     # currency-agnostic


def test_journal_ignores_stock_cards_when_disabled():
    d = tempfile.mkdtemp()
    stock_cards.STOCK_CARDS_LOG = os.path.join(d, "s.jsonl")
    trade_journal.STOCK_CARDS_LOG = stock_cards.STOCK_CARDS_LOG
    trade_journal.CARDS_LOG = os.path.join(d, "fx.jsonl")
    cid = stock_cards.log_stock_entry_card(
        strategy="us_blend", ticker="MSFT", direction="Buy", entry_price=400.0,
        shares=10, stop_price=380.0, sek_per_eur=11.0, entry_date="2026-09-01")
    stock_cards.log_stock_exit_card(card_id=cid, exit_price=410.0,
        exit_reason="momentum_rebalance", gross_pnl_sek=1100.0, commission_sek=20.0,
        net_pnl_sek=1080.0, holding_hours=None, sek_per_eur=11.0)
    real = ai_config.stocks_journal_enabled
    try:
        ai_config.stocks_journal_enabled = lambda: False
        assert trade_journal._closed_trades() == []
    finally:
        ai_config.stocks_journal_enabled = real


# ── 4. structural: no apply path anywhere in the stocks-AI code ──────────

_FORBIDDEN = ("_ai_apply_decision_to_qty", "place_with_stop", "place_market_order",
              "insert_trade", "close_trade", "cancel_order", "saxo_client",
              "saxo_order", "record_fill")


def _calls_in(src: str) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def test_stock_ai_modules_have_no_trade_capable_calls():
    for mod in (stock_cards, stock_proposal, basket_ranker):
        names = _calls_in(inspect.getsource(mod))
        hit = names & set(_FORBIDDEN)
        assert not hit, f"{mod.__name__} references trade-capable name(s): {hit}"


def test_stock_ai_modules_write_only_their_own_logs():
    import re
    allowed = {
        "ai.features.stock_cards": "STOCK_CARDS_LOG",
        "ai.features.stock_proposal": None,           # writes via trade_proposal's loggers only
        "ai.features.basket_ranker": "BASKET_SHADOW_LOG",
    }
    for mod, const in allowed.items():
        src = inspect.getsource(sys.modules[mod])
        for m in re.finditer(r"open\(([^,)]+),\s*['\"][aw]", src):
            arg = m.group(1).strip()
            assert const and const in arg, f"{mod}: write open() targets {arg}, not {const}"


def test_atos_runner_hooks_are_gated_and_have_no_new_apply_path():
    src = open(os.path.join(BASE, "atos_runner.py"), encoding="utf-8").read()
    # every stocks-AI hook block is guarded by ai_config.stocks_enabled() (or a
    # narrower stocks_* gate) inside a try/except that only prints on failure
    assert "ai_config.stocks_enabled()" in src
    assert "ai_config.stocks_reversion_copilot_enabled()" in src
    assert "ai_config.stocks_basket_ranker_enabled()" in src
    # the hooks never CALL can_apply_decision (a comment saying so is fine)
    assert "can_apply_decision(" not in src
    # the basket ranker's return value is never assigned/used
    assert "= ai_basket_ranker.rank_basket_shadow" not in src
    assert "rank_basket_shadow(" in src
    # no stocks-AI hook is inside a `not dry_run` order-placing branch by
    # accident: the shadow-Copilot hook must sit before the entry `for` loop
    assert src.index("reversion shadow-Copilot hook") < src.index("for cand in candidates[:slots_free]:\n        ticker = cand")


def test_module_parses():
    for m in (ai_config, stock_cards, stock_proposal, basket_ranker, trade_journal):
        ast.parse(inspect.getsource(m))


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

print(f"\n{B}{'=' * 66}{X}")
bad = [(n, e) for n, ok, e in _res if not ok]
for n, ok, e in _res:
    print(f"  [{G}PASS{X}]" if ok else f"  [{R}FAIL{X}]", n)
    if e:
        print(f"      {Y}{e}{X}")
print(f"{B}{'=' * 66}{X}")
if bad:
    print(f"{R}{B}  {len(bad)} / {len(_res)} FAILED{X}")
    sys.exit(1)
print(f"{G}{B}  ALL {len(_res)} TESTS PASSED{X}")
sys.exit(0)
