"""
ai/features/stock_proposal.py -- build a Trading-Copilot proposal for a US
Reversion entry, in the EXACT shape ai.features.trade_proposal produces
for forex, so ai.agent.trading_copilot.evaluate_proposal() scores it
unchanged. 2026-09-02.

Kept SEPARATE from trade_proposal.build_proposal (which stays byte-
identical -- forex regression safety). Reuses trade_proposal's loggers /
dedup / trade_id so the stocks shadow rows land in the same
data/ai_trade_proposals.jsonl + data/ai_shadow_decisions.jsonl the forex
shadow study and the Journal already read.

US Reversion (atos/us_reversion.py) maps cleanly:
  entry      = candidate price
  stop       = entry * (1 - STOP_PCT)          (~ -4%)
  target     = sma20                            (mean-reversion target)
  direction  = always Buy
  rsi2  -> rsi14   (the strategy's RSI(14) < 38 oversold trigger)
  volatility_atr / atr_pct  <- 20-day realised daily-vol %  (no ATR concept)

OBSERVE/LOG ONLY. This module has NO apply path -- it never imports or
calls anything trade-capable. Never raises.
"""

from __future__ import annotations

from datetime import datetime, timezone

# reuse the forex proposal plumbing verbatim
from ai.features.trade_proposal import (              # noqa: F401  (re-exported for callers)
    log_proposal, trade_id, already_evaluated, log_shadow_decision,
)

_ACCOUNT_ENV = "sim"


def build_stock_proposal(*, strategy: str, ticker: str, entry_price: float,
                         stop_price: float, target_price: float | None,
                         rsi14: float | None, shares: float,
                         daily_vol_pct: float | None,
                         risk_eur: float | None,
                         account_equity_eur: float | None,
                         open_positions: list | None = None,
                         est_commission_eur: float | None = None,
                         regime_bars=None,
                         pair_stats: dict | None = None) -> dict:
    """Assemble one US-Reversion proposal. `risk_eur` / `account_equity_eur`
    are pre-converted by the caller (which holds the live FX rates -- the
    stock price is USD, not SEK). `regime_bars` = the ticker's daily OHLC
    DataFrame (or None). `open_positions` = list of {symbol, side, size,
    strategy} for the reversion sleeve. Never raises."""
    try:
        entry = float(entry_price or 0)
        stop = float(stop_price or 0)
        risk_eur = round(float(risk_eur), 2) if risk_eur else None
        vol_pct = float(daily_vol_pct) if daily_vol_pct is not None else None
        vol_abs = round(entry * vol_pct / 100, 6) if (vol_pct and entry) else 0.0
        equity_eur = round(float(account_equity_eur), 2) if account_equity_eur else None

        if regime_bars is not None:
            try:
                from ai.regime.classifier import classify_regime
                regime = classify_regime(regime_bars)
            except Exception:
                regime = {"label": "UNKNOWN"}
        else:
            regime = {"label": "UNKNOWN"}

        econ = _stock_economics(entry, stop, target_price, risk_eur, est_commission_eur)

        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "account_env": _ACCOUNT_ENV,
            "market": "equity",
            "symbol": ticker,
            "side": "BUY",
            "entry_price": entry,
            "stop_loss": stop,
            "take_profit": float(target_price) if target_price is not None else None,
            "timeframe": "D1",
            "strategy_name": strategy,          # "us_reversion"
            # reversion is a contrarian single-strategy signal -- no consensus
            # stack, so signal_strength / agreement_count are structurally 1.
            "signal_strength": None,
            "raw_score": None,
            "agreement_count": 1,
            "ml_prob": None,
            "account_equity": equity_eur,
            "open_positions": open_positions or [],
            "n_open_positions": len(open_positions or []),
            "volatility_atr": vol_abs,
            "atr_pct": round(vol_pct, 3) if vol_pct is not None else None,
            "proposed_shares": shares,
            "rsi2": round(rsi14, 1) if rsi14 is not None else None,
            "regime": {
                "label": regime.get("label"),
                "adx": regime.get("adx"),
                "atr_ratio": regime.get("atr_ratio"),
                "ma_slope": regime.get("ma_slope"),
                "confidence": regime.get("confidence"),
            },
            "trade_economics": econ,
            "pair_history": pair_stats,
        }
    except Exception:
        # a proposal we cannot build is simply not logged -- never break the run
        return {}


def _stock_economics(entry, stop, target, risk_eur, commission_eur):
    try:
        entry = float(entry or 0)
        stop = float(stop or 0)
        target = float(target) if target is not None else None
        out = {"commission_eur": commission_eur}
        if not (entry and stop and target) or entry == stop:
            return out
        rr = abs(target - entry) / abs(entry - stop)
        out["reward_risk_ratio"] = round(rr, 2)
        if risk_eur:
            out["risk_eur"] = risk_eur
            tp_gross = risk_eur * rr
            out["tp_gross_eur"] = round(tp_gross, 1)
            if commission_eur is not None:
                out["tp_net_after_cost_eur"] = round(tp_gross - commission_eur, 1)
                out["breakeven_move_R"] = round(commission_eur / risk_eur, 3)
        return out
    except Exception:
        return {"commission_eur": commission_eur}
