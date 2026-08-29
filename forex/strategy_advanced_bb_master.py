"""
Advanced BB Mean-Reversion Master
High-selectivity Bollinger Band exhaustion/reversion strategy.

Goal: improve signal quality and robustness, not guarantee a higher win rate.
Public API remains compatible with strategy_bb.py.

INTEGRATION (2026-08-30, user request): registered as STRATEGIES key
"advanced_bb_master" in forex/runner.py, in parallel with the original
"bb" (forex/strategy_bb.py, UNTOUCHED) for an A/B comparison. SIM ONLY --
not in either LIVE allowlist. Uncapped slots (mirrors "bb"). MIN_BARS 282
fits the 500-bar fetch; standard trailing_stop_update hook, no runner
exit-loop change. EXEMPT from the momentum pre-filter (added to
_NO_MOMENTUM_FILTER), same as the original "bb" -- this is a
mean-reversion strategy and restricting it to the most-trending pairs
would suppress its setups.
"""
import numpy as np
import pandas as pd

BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
RSI_OB = 65
RSI_OS = 35
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0
RISK_PCT = 0.0025
TIME_STOP_DAYS = 8
LOT_ROUND = 1_000

ADX_PERIOD = 14
ADX_MAX = 30                 # avoid strongest directional trends / band walks
VOL_LOOKBACK = 252
VOL_PCT_MIN = 0.20
VOL_PCT_MAX = 0.90
MIN_EXCURSION_ATR = 0.15     # avoid barely-outside-band signals
MIN_BANDWIDTH_PCT = 0.002    # reject near-flat bands
REVERSAL_LOOKBACK = 3

MIN_BARS = max(BB_PERIOD, VOL_LOOKBACK) + 30


def _bb(closes):
    mid = closes.rolling(BB_PERIOD).mean()
    std = closes.rolling(BB_PERIOD).std(ddof=0)
    return mid + BB_STD * std, mid, mid - BB_STD * std


def _rsi(closes, period=RSI_PERIOD):
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False,
                                   min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False,
                                      min_periods=period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - 100/(1 + rs)


def _atr(h, l, c, period=ATR_PERIOD):
    prev = c.shift(1)
    tr = pd.concat([h-l, (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()


def _adx(h, l, c, period=ADX_PERIOD):
    up = h.diff()
    down = -l.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    prev = c.shift(1)
    tr = pd.concat([h-l, (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)

    def wild(x):
        return x.ewm(alpha=1/period, adjust=False, min_periods=period).mean()

    atr = wild(tr)
    pdi = 100 * wild(plus_dm) / (atr + 1e-10)
    mdi = 100 * wild(minus_dm) / (atr + 1e-10)
    dx = 100 * (pdi-mdi).abs() / (pdi+mdi+1e-10)
    return wild(dx), pdi, mdi


def _recent_extreme(c, rsi, side):
    """Require an extreme in the last few bars, but allow today's reversal."""
    if side == "Buy":
        return bool(((c < c.rolling(BB_PERIOD).mean()).tail(REVERSAL_LOOKBACK)).any())
    return bool(((c > c.rolling(BB_PERIOD).mean()).tail(REVERSAL_LOOKBACK)).any())


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    open_symbols = open_symbols or set()
    signals = []

    for sym, df in market_data.items():
        if sym in open_symbols or df is None or len(df) < MIN_BARS:
            continue
        try:
            h, l, c = df["High"], df["Low"], df["Close"]
            bbu, bbm, bbl = _bb(c)
            rsi = _rsi(c)
            atr = _atr(h, l, c)
            adx, pdi, mdi = _adx(h, l, c)

            i = -1
            close = float(c.iloc[i])
            upper, mid, lower = map(float, (bbu.iloc[i], bbm.iloc[i], bbl.iloc[i]))
            cur_rsi, cur_atr, cur_adx = map(float, (rsi.iloc[i], atr.iloc[i], adx.iloc[i]))
            band_width = (upper-lower) / max(abs(mid), 1e-10)
            atr_pct = float(atr.rolling(VOL_LOOKBACK, min_periods=100).rank(pct=True).iloc[i])

            vals = [close, upper, mid, lower, cur_rsi, cur_atr, cur_adx, atr_pct, band_width]
            if not all(np.isfinite(v) for v in vals) or cur_atr <= 0:
                continue
            if not (VOL_PCT_MIN <= atr_pct <= VOL_PCT_MAX):
                continue
            if cur_adx > ADX_MAX or band_width < MIN_BANDWIDTH_PCT:
                continue

            # Exhaustion/reversal confirmation:
            # Long: prior excursion below lower band, current candle closes upward.
            # Short: prior excursion above upper band, current candle closes downward.
            prior_low_excursion = bool((c.iloc[-REVERSAL_LOOKBACK:-1] < bbl.iloc[-REVERSAL_LOOKBACK:-1]).any())
            prior_high_excursion = bool((c.iloc[-REVERSAL_LOOKBACK:-1] > bbu.iloc[-REVERSAL_LOOKBACK:-1]).any())
            bullish_reversal = close > float(c.iloc[-2])
            bearish_reversal = close < float(c.iloc[-2])

            # Today's excursion can also qualify when candle itself shows rejection.
            long_exc = close < lower or prior_low_excursion
            short_exc = close > upper or prior_high_excursion

            long_ok = (
                long_exc and cur_rsi < RSI_OS and bullish_reversal and
                close < mid and float(pdi.iloc[i]) >= float(mdi.iloc[i])
            )
            short_ok = (
                short_exc and cur_rsi > RSI_OB and bearish_reversal and
                close > mid and float(mdi.iloc[i]) >= float(pdi.iloc[i])
            )

            if long_ok:
                excursion = max(0.0, (lower-close)/cur_atr)
                rsi_edge = max(0.0, (RSI_OS-cur_rsi)/10.0)
                score = excursion + rsi_edge + (ADX_MAX-cur_adx)/100.0
                signals.append({
                    "symbol": sym, "direction": "Buy", "score": float(score),
                    "rsi": cur_rsi, "atr": cur_atr, "adx": cur_adx,
                    "close": close, "stop_price": close-ATR_STOP_MULT*cur_atr,
                    "bb_target": mid, "bb_upper": upper, "bb_lower": lower,
                    "strategy": "advanced_bb_master"
                })
            elif short_ok:
                excursion = max(0.0, (close-upper)/cur_atr)
                rsi_edge = max(0.0, (cur_rsi-RSI_OB)/10.0)
                score = excursion + rsi_edge + (ADX_MAX-cur_adx)/100.0
                signals.append({
                    "symbol": sym, "direction": "Sell", "score": float(score),
                    "rsi": cur_rsi, "atr": cur_atr, "adx": cur_adx,
                    "close": close, "stop_price": close+ATR_STOP_MULT*cur_atr,
                    "bb_target": mid, "bb_upper": upper, "bb_lower": lower,
                    "strategy": "advanced_bb_master"
                })
        except Exception:
            continue

    return sorted(signals, key=lambda x: x["score"], reverse=True)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    if df is None or len(df) < BB_PERIOD + 2:
        return False, ""

    h, l, c = df["High"], df["Low"], df["Close"]
    _, mid, _ = _bb(c)
    close, high, low = float(c.iloc[-1]), float(h.iloc[-1]), float(l.iloc[-1])
    target, stop = float(mid.iloc[-1]), float(position.get("stop_price", 0))
    direction = position["direction"]

    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    if direction == "Buy":
        if close >= target:
            return True, f"bb_mid_reversion ({close:.5f}>={target:.5f})"
        if stop > 0 and low <= stop:
            return True, f"hard_stop ({stop:.5f})"
    else:
        if close <= target:
            return True, f"bb_mid_reversion ({close:.5f}<={target:.5f})"
        if stop > 0 and high >= stop:
            return True, f"hard_stop ({stop:.5f})"

    return False, ""


def size_position(account_equity: float, atr: float, min_units: float = LOT_ROUND,
                  risk_pct: float | None = None, block_below_min: bool = False) -> int:
    risk = account_equity * (RISK_PCT if risk_pct is None else risk_pct)
    distance = ATR_STOP_MULT * atr
    if distance <= 0:
        return 0 if block_below_min else int(min_units)
    units = int((risk / distance) // min_units) * int(min_units)
    return units if units >= min_units else (0 if block_below_min else int(min_units))


def trailing_stop_update(current_stop: float, current_price: float,
                         current_atr: float, direction: str = "Buy") -> float:
    band = ATR_STOP_MULT * current_atr
    if direction == "Buy":
        return max(current_stop, current_price-band)
    return min(current_stop, current_price+band)


def scan_summary(market_data: dict) -> list:
    rows = []
    for sym, df in market_data.items():
        if df is None or len(df) < MIN_BARS:
            rows.append({"symbol": sym, "status": "no_data"})
            continue
        try:
            h, l, c = df["High"], df["Low"], df["Close"]
            u, m, lo = _bb(c)
            r = _rsi(c)
            a = _atr(h, l, c)
            adx, _, _ = _adx(h, l, c)
            width = float((u.iloc[-1]-lo.iloc[-1])/max(abs(m.iloc[-1]), 1e-10))
            rows.append({
                "symbol": sym, "close": float(c.iloc[-1]), "rsi14": float(r.iloc[-1]),
                "bb_upper": float(u.iloc[-1]), "bb_mid": float(m.iloc[-1]),
                "bb_lower": float(lo.iloc[-1]), "adx": float(adx.iloc[-1]),
                "band_width_pct": width*100, "atr": float(a.iloc[-1]), "status": "ok"
            })
        except Exception:
            rows.append({"symbol": sym, "status": "error"})
    return rows
