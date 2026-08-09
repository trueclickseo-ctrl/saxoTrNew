"""
strategies/binance/mean_reversion.py
--------------------------------------
Crypto mean-reversion strategy for Binance testnet.

Signal logic (all 4 must fire simultaneously):
  1. RSI-14 < rsi_oversold          -- oversold momentum
  2. Price >= dip_threshold_pct below SMA-20  -- meaningful pullback
  3. 24h volume > volume_multiplier x 20d avg -- real sell-off, not illiquid drift
  4. Price above EMA-200            -- long-term uptrend intact

This mirrors the Saxo US Reversion strategy conceptually, but uses different
thresholds and crypto-appropriate parameters.  It is Binance-specific because:
  - Crypto trades 24/7 (no market-hours filter needed)
  - Position sizing is in USDT, not SEK
  - Lot precision varies per symbol (handled by BinanceClient.format_qty)
  - EMA-200 on daily bars makes sense for crypto; for US equities Saxo uses it too

Pure module — no network calls.  Accepts a BrokerInterface adapter and config dict.
Returns a list of CandidateSignal objects that the bot entry point acts on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.broker_interface import BrokerInterface

import statistics


@dataclass
class CandidateSignal:
    symbol: str
    price: Decimal
    rsi: float
    dip_pct: float        # how far below SMA-20 in percent
    vol_ratio: float      # 24h vol / 20d avg vol
    action: str           # "BUY" | "QUEUED" (slots full) | "SKIP"
    reason: str = ""


def _rsi(closes: list[float], period: int = 14) -> float:
    """Wilder RSI — same algorithm used in atos/features.py."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = statistics.mean(gains[:period])
    avg_loss = statistics.mean(losses[:period])
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))


def _ema(closes: list[float], period: int) -> float:
    """Exponential moving average."""
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def scan(
    adapter: "BrokerInterface",
    symbols: list[str],
    cfg: dict,
    open_slots: int,
) -> list[CandidateSignal]:
    """
    Scan symbols and return CandidateSignal list (BUY / QUEUED / SKIP).

    adapter     -- BinanceAdapter (or any BrokerInterface)
    symbols     -- list of symbols to scan, e.g. ["BTCUSDT", "ETHUSDT"]
    cfg         -- strategy sub-dict from binance_testnet_config.yaml
    open_slots  -- how many new positions can be opened right now
    """
    rsi_oversold       = float(cfg.get("rsi_oversold", 35))
    dip_threshold      = float(cfg.get("dip_threshold_pct", 5.0))
    vol_multiplier     = float(cfg.get("volume_multiplier", 1.5))
    ema_period         = int(cfg.get("ema_trend_period", 200))
    sma_period         = 20
    rsi_period         = int(cfg.get("rsi_period", 14))
    kline_needed       = max(ema_period + 5, sma_period + rsi_period + 5)

    results: list[CandidateSignal] = []
    slots_remaining = open_slots

    for sym in symbols:
        try:
            bars    = adapter.get_ohlcv(sym, interval="1d", limit=kline_needed)
            closes  = [float(b["close"]) for b in bars]
            volumes = [float(b["volume"]) for b in bars]

            if len(closes) < sma_period + rsi_period + 1:
                results.append(CandidateSignal(sym, Decimal(0), 0, 0, 0, "SKIP", "insufficient_history"))
                continue

            price   = closes[-1]
            rsi_val = _rsi(closes, period=rsi_period)
            sma20   = sum(closes[-sma_period:]) / sma_period
            ema200  = _ema(closes, period=ema_period)
            dip_pct = ((sma20 - price) / sma20) * 100
            vol_avg = sum(volumes[-sma_period:]) / sma_period
            vol_24h = volumes[-1]
            vol_ratio = vol_24h / vol_avg if vol_avg > 0 else 0.0

            # All 4 conditions
            cond_rsi    = rsi_val < rsi_oversold
            cond_dip    = dip_pct >= dip_threshold
            cond_vol    = vol_ratio >= vol_multiplier
            cond_trend  = price > ema200

            if not (cond_rsi and cond_dip and cond_vol and cond_trend):
                skip_reasons = []
                if not cond_rsi:   skip_reasons.append(f"RSI={rsi_val:.1f}>={rsi_oversold}")
                if not cond_dip:   skip_reasons.append(f"dip={dip_pct:.1f}%<{dip_threshold}%")
                if not cond_vol:   skip_reasons.append(f"vol={vol_ratio:.2f}x<{vol_multiplier}x")
                if not cond_trend: skip_reasons.append(f"price<EMA200")
                results.append(CandidateSignal(
                    sym, Decimal(str(price)), rsi_val, dip_pct, vol_ratio,
                    "SKIP", "; ".join(skip_reasons)
                ))
                continue

            # Signal fired — slot check
            if slots_remaining > 0:
                action = "BUY"
                slots_remaining -= 1
            else:
                action = "QUEUED"

            results.append(CandidateSignal(
                symbol=sym,
                price=Decimal(str(price)),
                rsi=rsi_val,
                dip_pct=dip_pct,
                vol_ratio=vol_ratio,
                action=action,
            ))

        except Exception as exc:
            results.append(CandidateSignal(sym, Decimal(0), 0, 0, 0, "SKIP", f"error: {exc}"))

    return results
