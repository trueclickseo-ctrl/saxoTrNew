"""
avanza_mini_futures_backtest.py
--------------------------------
Simulate mini futures mechanics on Avanza underlyings using Yahoo Finance
historical data. Mini futures are leveraged knock-out products — this
backtest models the key economics without needing real instrument tickers.

Mechanics modelled:
  - Financing level (barrier) set at entry based on chosen leverage
  - Daily financing cost: financing level drifts upward (LONG) or downward
    (SHORT) by financing_rate/365 each trading day
  - Knock-out: position closes at 0 if underlying touches the barrier
  - Entry/exit signal: price crosses N-day moving average
  - Position sizing: full budget_sek into one position at a time

Usage:
    python avanza_mini_futures_backtest.py
    python avanza_mini_futures_backtest.py --underlying ^GSPC --direction LONG
    python avanza_mini_futures_backtest.py --underlying ^OMX  --leverage 15
    python avanza_mini_futures_backtest.py --underlying GC=F  --direction LONG --years 5
    python avanza_mini_futures_backtest.py --list-underlyings

Supported underlyings  (Yahoo Finance ticker):
    ^OMX    OMXS30 (SEK)          -- most relevant for Avanza SE
    ^GSPC   S&P 500 (USD)
    ^NDX    Nasdaq-100 (USD)
    ^GDAXI  DAX (EUR)
    GC=F    Gold futures (USD)
    CL=F    Crude Oil WTI (USD)
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# ── Universe ──────────────────────────────────────────────────────────────────

UNDERLYINGS = {
    "^OMX":   {"name": "OMXS30",     "ccy": "SEK"},
    "^GSPC":  {"name": "S&P 500",    "ccy": "USD"},
    "^NDX":   {"name": "Nasdaq-100", "ccy": "USD"},
    "^GDAXI": {"name": "DAX",        "ccy": "EUR"},
    "GC=F":   {"name": "Gold",       "ccy": "USD"},
    "CL=F":   {"name": "Crude Oil",  "ccy": "USD"},
}

# Approximate long-run average SEK/foreign rates (for display only).
# We run the backtest in underlying-currency % terms and apply to SEK budget.
_FX_APPROX = {"SEK": 1.0, "USD": 10.5, "EUR": 11.2}


# ── Data download ─────────────────────────────────────────────────────────────

def _download(ticker: str, years: int) -> tuple[list[float], list[float], list[float], list[str]]:
    """Return (closes, highs, lows, dates) oldest-first."""
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("  ERROR: yfinance not installed. Run: pip install yfinance")

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=int(years * 365.25) + 60)
    df = yf.download(ticker,
                     start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"),
                     auto_adjust=True, progress=False)
    if df.empty:
        sys.exit(f"  ERROR: no data for '{ticker}' from Yahoo Finance.")

    def _col(c):
        s = df[c]
        if hasattr(s, "squeeze"):
            s = s.squeeze()
        return [float(x) for x in s.tolist()]

    dates = [str(d.date()) for d in df.index.tolist()]
    return _col("Close"), _col("High"), _col("Low"), dates


def _moving_average(closes: list[float], n: int) -> list[Optional[float]]:
    ma: list[Optional[float]] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        ma[i] = sum(closes[i - n + 1 : i + 1]) / n
    return ma


# ── Core simulation ───────────────────────────────────────────────────────────

@dataclass
class Trade:
    entry_date:  str
    exit_date:   str  = ""
    direction:   str  = "LONG"
    entry_price: float = 0.0
    exit_price:  float = 0.0
    financing_entry: float = 0.0
    financing_exit:  float = 0.0
    leverage:    float = 10.0
    budget_sek:  float = 2000.0
    pnl_sek:     float = 0.0
    ko:          bool  = False
    days_held:   int   = 0
    notes:       str   = ""


def _simulate(closes: list[float], highs: list[float], lows: list[float],
              dates: list[str], ma: list[Optional[float]],
              direction: str, leverage: float, financing_rate: float,
              budget_sek: float, ma_days: int,
              strategy: str = "trend") -> list[Trade]:
    """Run the signal-driven mini futures simulation.

    strategy='trend'    : buy when price crosses ABOVE MA, sell when crosses below.
    strategy='reversion': buy when price crosses BELOW MA (oversold bounce),
                          sell when price crosses back ABOVE MA.
    """
    trades: list[Trade] = []
    in_position = False
    financing_level = 0.0
    entry_price = 0.0
    entry_date  = ""
    days_held   = 0
    financing_entry = 0.0
    equity = budget_sek

    daily_rate = financing_rate / 252   # per trading day
    rev = (strategy == "reversion")

    for i in range(ma_days, len(closes)):
        S    = closes[i]
        high = highs[i]
        low  = lows[i]
        date = dates[i]
        ma_i = ma[i]

        if ma_i is None:
            continue

        if not in_position:
            # Entry signal
            ma_prev = ma[i - 1]
            if ma_prev is None:
                continue

            if not rev:
                # Trend: buy crossover above MA
                signal_long  = direction == "LONG"  and closes[i - 1] <= ma_prev and S > ma_i
                signal_short = direction == "SHORT" and closes[i - 1] >= ma_prev and S < ma_i
            else:
                # Reversion: buy crossover below MA (dip → expect bounce)
                signal_long  = direction == "LONG"  and closes[i - 1] >= ma_prev and S < ma_i
                signal_short = direction == "SHORT" and closes[i - 1] <= ma_prev and S > ma_i

            if signal_long or signal_short:
                if direction == "LONG":
                    financing_level = S * (1.0 - 1.0 / leverage)
                else:
                    financing_level = S * (1.0 + 1.0 / leverage)

                entry_price     = S
                financing_entry = financing_level
                entry_date      = date
                days_held       = 0
                in_position     = True

        else:
            days_held += 1

            # Drift financing level each day
            if direction == "LONG":
                financing_level *= (1.0 + daily_rate)
            else:
                financing_level *= (1.0 - daily_rate)

            # Knock-out check (intraday — use high/low)
            ko_event = False
            if direction == "LONG"  and low  <= financing_level:
                ko_event = True
            if direction == "SHORT" and high >= financing_level:
                ko_event = True

            # Exit signal
            ma_prev = ma[i - 1]
            exit_signal = False
            if ma_prev is not None:
                if not rev:
                    # Trend: exit when crosses below
                    if direction == "LONG"  and S < ma_i:
                        exit_signal = True
                    if direction == "SHORT" and S > ma_i:
                        exit_signal = True
                else:
                    # Reversion: exit when price returns above MA
                    if direction == "LONG"  and S > ma_i:
                        exit_signal = True
                    if direction == "SHORT" and S < ma_i:
                        exit_signal = True

            # Last bar — force exit
            last_bar = (i == len(closes) - 1)

            if ko_event or exit_signal or last_bar:
                if ko_event:
                    exit_price = financing_level
                    pnl_pct    = -1.0               # lose full position
                else:
                    exit_price = S

                    if direction == "LONG":
                        pos_return = (exit_price - financing_level) / \
                                     (entry_price  - financing_entry)
                    else:
                        pos_return = (financing_level - exit_price) / \
                                     (financing_entry - entry_price)

                    pnl_pct = pos_return - 1.0

                pnl_sek  = equity * pnl_pct
                equity  += pnl_sek
                equity   = max(equity, 0.0)

                trades.append(Trade(
                    entry_date      = entry_date,
                    exit_date       = date,
                    direction       = direction,
                    entry_price     = entry_price,
                    exit_price      = exit_price,
                    financing_entry = financing_entry,
                    financing_exit  = financing_level,
                    leverage        = leverage,
                    budget_sek      = budget_sek,
                    pnl_sek         = round(pnl_sek, 2),
                    ko              = ko_event,
                    days_held       = days_held,
                    notes           = "LAST" if last_bar and not ko_event and not exit_signal else "",
                ))

                in_position = False

    return trades


# ── Stats ─────────────────────────────────────────────────────────────────────

def _stats(trades: list[Trade], budget_sek: float) -> dict:
    if not trades:
        return {}
    pnls   = [t.pnl_sek for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    kos    = sum(1 for t in trades if t.ko)

    total_pnl = sum(pnls)

    # Max drawdown on equity curve
    equity = budget_sek
    peak   = budget_sek
    max_dd = 0.0
    for t in trades:
        equity += t.pnl_sek
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    avg_win  = sum(wins)  / len(wins)  if wins  else 0
    avg_loss = sum(losses)/ len(losses) if losses else 0
    pf       = abs(sum(wins) / sum(losses)) if sum(losses) != 0 else float("inf")

    return {
        "n_trades":   len(trades),
        "win_rate":   len(wins) / len(trades) * 100,
        "total_pnl":  total_pnl,
        "final_equity": budget_sek + total_pnl,
        "return_pct": total_pnl / budget_sek * 100,
        "avg_win":    avg_win,
        "avg_loss":   avg_loss,
        "profit_factor": pf,
        "max_dd_pct": max_dd,
        "ko_count":   kos,
        "avg_days":   sum(t.days_held for t in trades) / len(trades),
    }


# ── Output ────────────────────────────────────────────────────────────────────

def _print_trades(trades: list[Trade]) -> None:
    if not trades:
        print("  No trades generated.")
        return
    hdr = f"  {'#':>3}  {'Entry':10s}  {'Exit':10s}  {'Days':>4}  " \
          f"{'Entry P':>8}  {'Exit P':>8}  {'Lev':>4}  {'P&L SEK':>9}  {'Note'}"
    print(hdr)
    print("  " + "-" * 80)
    equity = 0.0
    for i, t in enumerate(trades, 1):
        ko_marker = " KO!" if t.ko else ""
        print(f"  {i:>3}  {t.entry_date}  {t.exit_date}  {t.days_held:>4}  "
              f"{t.entry_price:>8.2f}  {t.exit_price:>8.2f}  "
              f"{t.leverage:>4.0f}x  {t.pnl_sek:>+9.2f}{ko_marker}")
        equity += t.pnl_sek
    print(f"\n  Running P&L: {equity:+.2f} SEK")


def _print_stats(s: dict, budget_sek: float, ticker: str,
                 direction: str, leverage: float, ma_days: int,
                 financing_rate: float, years: int, **kwargs) -> None:
    w = 60
    print("\n" + "=" * w)
    print("  AVANZA MINI FUTURES BACKTEST RESULTS")
    print("=" * w)
    info = UNDERLYINGS.get(ticker, {})
    print(f"  Underlying : {ticker}  ({info.get('name','')}  {info.get('ccy','')})")
    print(f"  Direction  : {direction}")
    print(f"  Leverage   : {leverage:.0f}x  (KO at {100/leverage:.1f}% adverse move)")
    strategy = kwargs.get("strategy", "trend")
    strat_label = "MA crossover (trend)" if strategy == "trend" else "MA crossover (mean-reversion)"
    print(f"  Signal     : {ma_days}-day {strat_label}")
    print(f"  Financing  : {financing_rate*100:.1f}% annual  "
          f"= {financing_rate/252*100:.4f}% per day")
    print(f"  Budget     : {budget_sek:,.0f} SEK")
    print(f"  Period     : {years} years")
    print("-" * w)
    if not s:
        print("  No trades.")
        return
    print(f"  Trades     : {s['n_trades']}")
    print(f"  Win rate   : {s['win_rate']:.1f}%")
    print(f"  Profit factor: {s['profit_factor']:.2f}")
    print(f"  Avg win    : {s['avg_win']:+.2f} SEK")
    print(f"  Avg loss   : {s['avg_loss']:+.2f} SEK")
    print(f"  Avg hold   : {s['avg_days']:.1f} days")
    print(f"  KO events  : {s['ko_count']}")
    print(f"  Max drawdown: {s['max_dd_pct']:.1f}%")
    print("-" * w)
    sign = "+" if s['total_pnl'] >= 0 else ""
    print(f"  Total P&L  : {sign}{s['total_pnl']:,.2f} SEK")
    print(f"  Final equity: {s['final_equity']:,.2f} SEK  "
          f"({sign}{s['return_pct']:.1f}% on {budget_sek:,.0f} SEK)")
    print("=" * w)


def _print_underlyings() -> None:
    print("\n  Supported underlyings:")
    print(f"  {'Ticker':8s}  {'Name':15s}  Currency")
    print("  " + "-" * 35)
    for tk, info in UNDERLYINGS.items():
        print(f"  {tk:8s}  {info['name']:15s}  {info['ccy']}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Avanza mini futures backtest — simulates leverage, "
                    "financing cost and knock-out on Yahoo Finance data."
    )
    p.add_argument("--underlying",      default="^OMX",  metavar="TICKER",
                   help="Yahoo Finance ticker (default: ^OMX = OMXS30)")
    p.add_argument("--direction",       default="LONG",  choices=["LONG", "SHORT"])
    p.add_argument("--leverage",        type=float, default=10.0,
                   help="Leverage multiplier (default 10 => KO at 10%% adverse move)")
    p.add_argument("--ma-days",         type=int,   default=20,
                   help="MA period for entry/exit signal (default 20)")
    p.add_argument("--financing-rate",  type=float, default=0.055,
                   help="Annual financing rate, decimal (default 0.055 = 5.5%%)")
    p.add_argument("--budget-sek",      type=float, default=2000.0,
                   help="Starting capital in SEK (default 2000)")
    p.add_argument("--years",           type=float, default=3.0,
                   help="Backtest lookback in years (default 3)")
    p.add_argument("--strategy",        default="trend",
                   choices=["trend", "reversion"],
                   help="trend: buy MA crossover up (default); "
                        "reversion: buy MA crossover down (dip bounce)")
    p.add_argument("--no-trades",       action="store_true",
                   help="Show only summary stats, not the trade list")
    p.add_argument("--list-underlyings", action="store_true",
                   help="Print available underlyings and exit")
    args = p.parse_args()

    if args.list_underlyings:
        _print_underlyings()
        return

    ticker  = args.underlying
    if ticker not in UNDERLYINGS:
        print(f"  WARNING: '{ticker}' not in predefined list — attempting download anyway.")

    print(f"\n  Downloading {args.years:.0f}y of {ticker} data from Yahoo Finance...")
    closes, highs, lows, dates = _download(ticker, args.years)
    print(f"  {len(closes)} trading days loaded  "
          f"({dates[0]} -> {dates[-1]})")

    ma = _moving_average(closes, args.ma_days)

    print(f"  Simulating {args.direction} mini future  "
          f"{args.leverage:.0f}x leverage  "
          f"({100/args.leverage:.1f}% KO distance)  "
          f"strategy={args.strategy}...")

    trades = _simulate(
        closes, highs, lows, dates, ma,
        direction       = args.direction,
        leverage        = args.leverage,
        financing_rate  = args.financing_rate,
        budget_sek      = args.budget_sek,
        ma_days         = args.ma_days,
        strategy        = args.strategy,
    )

    if not args.no_trades:
        print(f"\n  TRADE LOG ({len(trades)} trades):\n")
        _print_trades(trades)

    s = _stats(trades, args.budget_sek)
    _print_stats(s, args.budget_sek, ticker, args.direction,
                 args.leverage, args.ma_days, args.financing_rate,
                 int(args.years), strategy=args.strategy)


if __name__ == "__main__":
    main()
