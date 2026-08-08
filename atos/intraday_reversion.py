"""
atos/intraday_reversion.py
---------------------------
Intraday scanner for the US Mean Reversion strategy.

WHY THIS EXISTS
  The daily cycle (run once at PKT morning) uses yesterday's close prices.
  If a stock dips sharply during the US session (6:30 PM - 1:00 AM PKT),
  we miss it until the next morning. This module runs every ~90 minutes
  during US market hours and places entries if a dip signal fires intraday.

HOW IT WORKS
  Daily indicators (EMA200, SMA20) are already computed from historical closes
  — they don't change intraday. For RSI, we substitute today's live price as
  the 14th data point (using the last 13 daily closes). Volume is scaled by
  the fraction of the session already elapsed so partial-day volume is
  comparable to the 20-day average.

BAD NEWS FILTER
  A stock that gaps down >8% at open is likely reacting to news (earnings miss,
  downgrade, scandal) — the dip may extend much further and is NOT a technical
  reversion setup. We skip these. A stock that drifts down 5-7% over 2-3 days
  and is now 5% below SMA20 with panic volume is exactly what we want.

ENTRY TIMING (PKT = UTC+5, US session = EDT = UTC-4)
  US open:          09:30 ET = 18:30 PKT
  Skip first 30min: 10:00 ET = 19:00 PKT  ← earliest entry
  Best window:      10:00-14:30 ET = 19:00-23:30 PKT
  Pre-close scan:   15:30 ET = 00:30 PKT  ← last scan of the session
  US close:         16:00 ET = 01:00 PKT

SCHEDULED SCANS (add to Task Scheduler):
  19:00 PKT  (10:00 AM ET)  — after opening dust settles
  20:30 PKT  (11:30 AM ET)  — midday
  22:00 PKT  (13:00 PM ET)  — early afternoon
  23:30 PKT  (14:30 PM ET)  — mid afternoon
  00:30 PKT  (15:30 PM ET)  — pre-close final scan
"""
import datetime
import pandas as pd
import numpy as np

import atos.capital_config as CAP
from atos.corporate_events import tickers_to_avoid

# ── Intraday-specific thresholds ─────────────────────────────────────────
# These match the IS-validated daily params but with two safety guards:
#   1. BAD_NEWS_GAP_PCT  — if stock opened down > this from yesterday's close
#      it's likely a fundamental event, not a technical dip. Skip it.
#   2. CATASTROPHIC_DROP — if current price is this far below yesterday's close
#      total (intraday + gap), the situation is too uncertain. Skip it.
#   3. VOLUME_SCALE      — True = scale partial-day volume to a full-day
#      equivalent before comparing to the 20d average. This avoids false
#      volume spikes early in the session.

BAD_NEWS_GAP_PCT   = 0.08   # >8% single-day gap-down at open → news event, skip
CATASTROPHIC_DROP  = 0.15   # >15% total drop today → too extreme, skip
VOLUME_SCALE       = True   # Scale partial volume to full-session equivalent
SESSION_MINUTES    = 390    # 9:30 AM – 4:00 PM ET = 390 trading minutes


# ── RSI helper (same formula as us_reversion.py) ─────────────────────────

def _rsi14(closes: pd.Series) -> float:
    """RSI(14) on the supplied series. Returns the last value."""
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def _minutes_elapsed() -> int:
    """Minutes elapsed since today's 9:30 AM ET open (0 before market opens)."""
    now_et = datetime.datetime.utcnow() - datetime.timedelta(hours=4)  # EDT offset
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed = int((now_et - market_open).total_seconds() / 60)
    return max(0, min(elapsed, SESSION_MINUTES))


def _volume_scale_factor() -> float:
    """If VOLUME_SCALE=True, project partial-day volume to a full session."""
    if not VOLUME_SCALE:
        return 1.0
    elapsed = _minutes_elapsed()
    if elapsed < 15:
        return 10.0   # very early — don't trust volume yet, set factor high so it won't spike-trigger
    return SESSION_MINUTES / elapsed


# ── Live price fetch via yfinance ─────────────────────────────────────────

def fetch_intraday(tickers: list) -> dict:
    """Fetch today's 5-minute bars for all tickers.

    Returns {ticker: DataFrame(columns=[Open, High, Low, Close, Volume])}.
    Tickers with no data or errors are omitted.
    """
    import yfinance as yf
    result = {}
    # Batch download is faster than per-ticker loop
    try:
        raw = yf.download(
            tickers,
            period="1d",
            interval="5m",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"  [intraday] yfinance download error: {e}")
        return result

    for ticker in tickers:
        try:
            if len(tickers) == 1:
                df = raw
            else:
                df = raw[ticker]
            if df is None or df.empty:
                continue
            result[ticker] = df.dropna(how="all")
        except Exception:
            pass
    return result


# ── Main intraday scan ─────────────────────────────────────────────────────

def intraday_scan(
    feat_data: dict,
    us_tickers: list,
    prev_close_override: dict | None = None,
) -> list:
    """Scan the universe for mean-reversion signals using live intraday prices.

    Uses daily feat_data for slow indicators (EMA200, SMA20) and substitutes
    today's live price + volume from yfinance 5-minute bars.

    prev_close_override: {ticker: float} — supply yesterday's close manually
      (e.g. from the cached close_df). Used for the bad-news gap filter.

    Returns the same format as atos.us_reversion.scan():
      [{ticker, price, rsi, sma20, dip_pct, vol_ratio, score,
        gap_pct, session_vol_est, entry_type}]
    sorted by score descending.
    """
    RSI_ENTRY = CAP.reversion_stop_pct()   # reuse entry threshold
    # NOTE: RSI_ENTRY is a signal threshold, not stop — import correctly:
    from atos.us_reversion import RSI_ENTRY, DIP_PCT, VOL_MULT

    # Corporate events — tickers with earnings / ex-div in next 2 days
    avoid = set()
    try:
        avoid = tickers_to_avoid(us_tickers)
    except Exception:
        pass

    # Fetch live intraday data
    print("  [intraday] fetching live 5-min bars...")
    intra = fetch_intraday(us_tickers)
    if not intra:
        print("  [intraday] no intraday data returned")
        return []

    scale = _volume_scale_factor()
    elapsed = _minutes_elapsed()
    print(f"  [intraday] {elapsed} min into session | vol scale ×{scale:.1f}")

    candidates = []
    for ticker in us_tickers:
        if ticker in avoid:
            continue

        # ── Daily slow indicators ──────────────────────────────────────
        df_daily = feat_data.get(ticker)
        if df_daily is None or "Close" not in df_daily:
            continue
        close_daily = df_daily["Close"].dropna()
        volume_daily = df_daily.get("Volume", pd.Series(dtype=float)).dropna()
        if len(close_daily) < 220:
            continue

        ema200      = float(close_daily.ewm(span=200, adjust=False).mean().iloc[-1])
        sma20       = float(close_daily.rolling(20).mean().iloc[-1])
        vol_20d_avg = float(volume_daily.rolling(20).mean().iloc[-1]) if len(volume_daily) >= 20 else 0
        prev_close  = (prev_close_override or {}).get(ticker) or float(close_daily.iloc[-1])

        if pd.isna(ema200) or pd.isna(sma20) or vol_20d_avg <= 0:
            continue

        # ── Live intraday price & volume ───────────────────────────────
        df_intra = intra.get(ticker)
        if df_intra is None or df_intra.empty:
            continue
        try:
            current_price  = float(df_intra["Close"].dropna().iloc[-1])
            today_open     = float(df_intra["Open"].dropna().iloc[0])
            today_vol_so_far = float(df_intra["Volume"].dropna().sum())
        except (IndexError, KeyError, TypeError):
            continue

        if current_price <= 0:
            continue

        # ── Bad news filter ────────────────────────────────────────────
        # 1. Gap-down at open: if the stock opened > 8% below yesterday's close
        #    it reacted to overnight news — not a technical dip we want.
        gap_pct = (prev_close - today_open) / prev_close if prev_close > 0 else 0
        if gap_pct > BAD_NEWS_GAP_PCT:
            print(f"  [intraday] {ticker} skipped — gap-down {gap_pct*100:.1f}% "
                  f"(>8% threshold, likely news event)")
            continue

        # 2. Catastrophic total drop: current price > 15% below yesterday close
        total_drop = (prev_close - current_price) / prev_close if prev_close > 0 else 0
        if total_drop > CATASTROPHIC_DROP:
            print(f"  [intraday] {ticker} skipped — total drop {total_drop*100:.1f}% "
                  f"(>{CATASTROPHIC_DROP*100:.0f}%, too extreme)")
            continue

        # ── RSI using today's live price as the 14th data point ───────
        # Append today's live price to the last 30 daily closes and compute RSI.
        # This gives RSI "if today closed at this price right now".
        close_with_today = pd.concat([
            close_daily.iloc[-30:],
            pd.Series([current_price], index=[close_daily.index[-1] + pd.Timedelta(days=1)])
        ])
        try:
            rsi = _rsi14(close_with_today)
        except Exception:
            continue
        if pd.isna(rsi):
            continue

        # ── Volume: scale partial session to full-day equivalent ───────
        session_vol_est = today_vol_so_far * scale
        vol_ratio       = session_vol_est / vol_20d_avg if vol_20d_avg > 0 else 0

        # ── Entry conditions (same as daily scanner) ───────────────────
        above_ema200 = current_price > ema200
        dip_pct      = (sma20 - current_price) / sma20
        oversold     = rsi < RSI_ENTRY
        deep_dip     = dip_pct >= DIP_PCT
        vol_spike    = vol_ratio >= VOL_MULT

        if not (above_ema200 and oversold and deep_dip and vol_spike):
            continue

        score = dip_pct * (RSI_ENTRY - rsi)
        candidates.append({
            "ticker":          ticker,
            "price":           round(current_price, 2),
            "rsi":             round(rsi, 1),
            "sma20":           round(sma20, 2),
            "ema200":          round(ema200, 2),
            "dip_pct":         round(dip_pct * 100, 1),
            "vol_ratio":       round(vol_ratio, 2),
            "gap_pct":         round(gap_pct * 100, 1),
            "session_vol_est": int(session_vol_est),
            "score":           round(score, 4),
            "entry_type":      "intraday",
        })
        print(f"  [intraday] SIGNAL: {ticker}  ${current_price:.2f}  "
              f"RSI={rsi:.1f}  dip={dip_pct*100:.1f}%  vol={vol_ratio:.1f}x  "
              f"gap={gap_pct*100:.1f}%  score={score:.4f}")

    return sorted(candidates, key=lambda x: x["score"], reverse=True)


# ── Market hours check ────────────────────────────────────────────────────

def us_market_is_open() -> bool:
    """True if current time is within the US equity session (ET 10:00–15:30)."""
    now_et = datetime.datetime.utcnow() - datetime.timedelta(hours=4)
    # Skip first 30 min of session (9:30–10:00) and last 30 min (15:30–16:00)
    open_time  = now_et.replace(hour=10, minute=0, second=0, microsecond=0)
    close_time = now_et.replace(hour=15, minute=30, second=0, microsecond=0)
    if now_et.weekday() >= 5:   # Saturday / Sunday
        return False
    return open_time <= now_et <= close_time


def next_scan_description() -> str:
    """Return a human-readable string of the PKT scan schedule."""
    return (
        "Intraday scan schedule (PKT = ET + 9h):\n"
        "  19:00 PKT  (10:00 AM ET)  after opening dust settles\n"
        "  20:30 PKT  (11:30 AM ET)  midday\n"
        "  22:00 PKT  (13:00 PM ET)  early afternoon\n"
        "  23:30 PKT  (14:30 PM ET)  mid afternoon\n"
        "  00:30 PKT  (15:30 PM ET)  pre-close final scan\n"
    )
