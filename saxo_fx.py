"""
saxo_fx.py  —  Live currency conversion to SEK, from Saxo quotes only
------------------------------------------------------------------------
Per explicit user direction (2026-08-22): any live conversion (account
balance, position value, P&L) must use Saxo's own quotes, never Yahoo.
This is the stocks/ATOS-side equivalent of forex/runner.py's
_eur_per_unit() -- same triangulation idea, reusing forex/universe.py's
already-verified UICs and price_service.py's batched/retried live fetch,
but converting to SEK (ATOS's risk-capital currency) instead of EUR.

Usage:
    from saxo_fx import rate_to_sek
    rates = rate_to_sek(["USD", "EUR", "DKK"])
    # rates = {"USD": 10.98, "EUR": 11.42, "DKK": 1.53}
    # Missing key = Saxo had no live quote for it this call; caller must
    # treat that as unknown, not assume/guess a value.
"""

import price_service
from forex.universe import PAIRS as _FX_PAIRS

_FX_BY_SYMBOL = {p["symbol"]: p for p in _FX_PAIRS}


def _instruments_needed(ccys: set[str]) -> list[dict]:
    needed, seen, need_eurusd = [], set(), False
    if "EUR" in ccys or any(c != "SEK" for c in ccys):
        p = _FX_BY_SYMBOL.get("EURSEK")
        if p and "EURSEK" not in seen:
            seen.add("EURSEK")
            needed.append({"symbol": "EURSEK", "uic": p["uic"], "asset_type": "FxSpot"})
    for ccy in ccys:
        if ccy in ("SEK", "EUR") or ccy in seen:
            continue
        p = _FX_BY_SYMBOL.get(f"EUR{ccy}")
        if p:
            seen.add(f"EUR{ccy}")
            needed.append({"symbol": f"EUR{ccy}", "uic": p["uic"], "asset_type": "FxSpot"})
            continue
        p = _FX_BY_SYMBOL.get(f"USD{ccy}")
        if p:
            need_eurusd = True
            seen.add(f"USD{ccy}")
            needed.append({"symbol": f"USD{ccy}", "uic": p["uic"], "asset_type": "FxSpot"})
    if need_eurusd and "EURUSD" not in seen:
        p = _FX_BY_SYMBOL.get("EURUSD")
        if p:
            needed.append({"symbol": "EURUSD", "uic": p["uic"], "asset_type": "FxSpot"})
    return needed


def rate_to_sek(currencies: list[str]) -> dict[str, float]:
    """SEK value of one unit of each currency in `currencies`, from live
    Saxo quotes. A currency missing from the result means Saxo had no
    live quote for a pair needed to resolve it right now -- treat as
    unknown, do not substitute a guess."""
    ccys = {c.upper() for c in currencies if c}
    result = {c: 1.0 for c in ccys if c == "SEK"}
    remaining = ccys - set(result)
    if not remaining:
        return result

    instruments = _instruments_needed(remaining)
    live, _src = price_service.fetch_prices(instruments)

    eur_sek = live.get("EURSEK")
    eur_usd = live.get("EURUSD")

    for ccy in remaining:
        if ccy == "EUR":
            if eur_sek:
                result["EUR"] = eur_sek
            continue
        direct = live.get(f"EUR{ccy}")
        if direct and eur_sek:
            result[ccy] = eur_sek / direct
            continue
        usd_leg = live.get(f"USD{ccy}")
        if usd_leg and eur_usd and eur_sek:
            result[ccy] = eur_sek / (usd_leg * eur_usd)

    return result
