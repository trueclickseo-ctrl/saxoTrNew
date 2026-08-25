"""
atos/capital_config.py
-----------------------
Single source of truth for capital allocation and position sizing.
All strategy modules and the runner load from here instead of
hardcoding percentages.

Config file: config/capital.json (relative to the project root).
"""
import json
import os

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_BASE_DIR, "config", "capital.json")

_cfg: dict = {}


def _load() -> dict:
    global _cfg
    if _cfg:
        return _cfg
    if not os.path.exists(_CONFIG_PATH):
        raise FileNotFoundError(
            f"Capital config not found: {_CONFIG_PATH}\n"
            "Create config/capital.json — see the template in the repo."
        )
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        _cfg = json.load(f)
    return _cfg


def reload():
    """Force re-read from disk (useful after editing capital.json mid-run)."""
    global _cfg
    _cfg = {}
    _load()


# ── Account-level ──────────────────────────────────────────────────────────

def starting_capital_sek() -> float:
    return float(_load()["account"]["starting_capital_sek"])


def max_deploy_pct() -> float:
    """Fraction of live cash that can be deployed across all strategies."""
    return float(_load()["account"].get("max_deploy_pct", 0.90))


def cash_buffer_pct() -> float:
    return float(_load()["account"].get("cash_buffer_pct", 0.10))


# ── US Blend ───────────────────────────────────────────────────────────────

def blend_allocation_pct() -> float:
    """Fraction of live cash given to US Blend each cycle."""
    return float(_load()["strategies"]["us_blend"]["allocation_pct"])


def blend_offense_slots() -> int:
    """Max momentum picks (offense)."""
    return int(_load()["strategies"]["us_blend"].get("offense_slots", 6))


def blend_defense_slots() -> int:
    """Low-vol picks (defense)."""
    return int(_load()["strategies"]["us_blend"].get("defense_slots", 2))


def blend_total_slots() -> int:
    return blend_offense_slots() + blend_defense_slots()


def blend_position_size_pct() -> float:
    """Equal weight per Blend position as a fraction of the blend budget."""
    return 1.0 / blend_total_slots()


# ── US Reversion ──────────────────────────────────────────────────────────

def reversion_allocation_pct() -> float:
    """Fraction of live cash given to US Reversion each cycle."""
    return float(_load()["strategies"]["us_reversion"]["allocation_pct"])


def reversion_max_universe_pct() -> float:
    """Max concurrent positions as a fraction of the universe size."""
    return float(_load()["strategies"]["us_reversion"].get("max_universe_pct", 0.10))


def reversion_min_slots() -> int:
    return int(_load()["strategies"]["us_reversion"].get("min_slots", 2))


def reversion_max_slots() -> int:
    """Hard ceiling on concurrent reversion positions.

    max_universe_pct alone scales slots with universe size, which broke when the
    universe grew 61 -> 385 (10% => 38 slots, vs the 2-3 the strategy was actually
    validated at). At 38 slots each slot is ~3,550 SEK (~$370): 13% of the universe
    costs more than one share and is silently skipped, and Saxo's minimum commission
    becomes a large fraction of each position. This caps the derived value.
    """
    return int(_load()["strategies"]["us_reversion"].get("max_slots", 6))


def reversion_slots(universe_size: int) -> int:
    """The single source of truth for reversion slot count.

    min_slots <= round(universe_size * max_universe_pct) <= max_slots
    """
    derived = round(universe_size * reversion_max_universe_pct())
    return max(reversion_min_slots(), min(derived, reversion_max_slots()))


# ── Futures ───────────────────────────────────────────────────────────────

def futures_risk_equity_eur() -> float:
    """Equity base (EUR) that futures sizing risks RISK_PCT of per trade.

    The Saxo account is denominated in EUR. Without this, futures sized off the
    raw SIM TotalValue (~957,000 EUR of demo credit) instead of real capital.
    """
    try:
        return float(_load()["strategies"]["futures"]["risk_equity_eur"])
    except Exception:
        return 27_800.0


def forex_risk_equity_eur() -> float:
    """Equity base (EUR) that forex sizing risks RISK_PCT of per trade.

    Without this, forex sized off the raw SIM TotalValue (~945,000 EUR of demo
    credit) rather than real capital. See futures_risk_equity_eur().
    """
    try:
        return float(_load()["strategies"]["forex"]["risk_equity_eur"])
    except Exception:
        return 27_800.0


def forex_live_risk_equity_sek() -> float:
    """Equity base (SEK) that the real-money LIVE forex account sizes
    RISK_PCT of per trade — separate from SIM's forex_risk_equity_eur().
    The LIVE account is itself SEK-denominated (6,000 SEK opening balance,
    2026-08-25), so this is a direct cap, no currency conversion needed.
    """
    try:
        return float(_load()["strategies"]["forex_live"]["risk_equity_sek"])
    except Exception:
        return 6_000.0


def forex_lbo_capital_eur() -> float:
    """Dedicated day-trading capital (EUR) for the London Breakout book.

    LBO is a separate book from the swing strategies; without this it was sized
    off the whole account rather than its documented 15,000 SEK.
    """
    try:
        return float(_load()["strategies"]["forex"]["lbo_capital_eur"])
    except Exception:
        return 1_390.0


def reversion_stop_pct() -> float:
    return float(_load()["strategies"]["us_reversion"].get("stop_pct", 0.04))


def reversion_max_hold_days() -> int:
    return int(_load()["strategies"]["us_reversion"].get("max_hold_days", 10))


def reversion_sleeve_dd_cap() -> float:
    return float(_load()["strategies"]["us_reversion"].get("sleeve_dd_cap", 0.10))


def reversion_fallback_sleeve_sek() -> float:
    return float(_load()["strategies"]["us_reversion"].get("fallback_sleeve_sek", 300_000.0))


# ── Summary (for terminal/dashboard display) ───────────────────────────────

def summary() -> str:
    cfg = _load()
    acct = cfg["account"]
    b = cfg["strategies"]["us_blend"]
    r = cfg["strategies"]["us_reversion"]
    b_slots = b.get("offense_slots", 6) + b.get("defense_slots", 2)
    r_slots = f"{r.get('max_universe_pct', 0.10)*100:.0f}% of universe"
    return (
        f"Capital config ({_CONFIG_PATH})\n"
        f"  Account:      start={acct['starting_capital_sek']:,.0f} SEK  "
        f"max_deploy={acct.get('max_deploy_pct', 0.90)*100:.0f}%\n"
        f"  US Blend:     {b['allocation_pct']*100:.0f}% of cash  "
        f"{b_slots} slots ({b.get('offense_slots',6)} offense + {b.get('defense_slots',2)} defense)\n"
        f"  US Reversion: {r['allocation_pct']*100:.0f}% of cash  "
        f"{r_slots}  stop={r.get('stop_pct',0.04)*100:.0f}%  "
        f"hold<={r.get('max_hold_days',10)}d  DD-cap={r.get('sleeve_dd_cap',0.10)*100:.0f}%"
    )
