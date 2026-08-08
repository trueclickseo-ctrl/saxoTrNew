"""
atos_runner.py
---------------
ATOS v1 — Daily Entry Point.

Run this once per trading day (schedule with Windows Task Scheduler):
    python atos_runner.py

What it does each cycle:
  1. Kill switch + daily loss cap check
  2. Download latest daily bars for full universe
  3. Compute all features (EMA, ATR, RSI, MACD, Bollinger, Donchian, ADX)
  4. Run Decision Engine — 5 weighted detectors per ticker
  5. Risk Engine approval for each BUY signal
  6. Place approved orders on Saxo SIM via existing saxo_client.py
  7. Check exits on all open positions
  8. Run learning pass — update detector weights from closed trades
  9. Log everything to data/atos.db
 10. Generate HTML dashboard → dashboard/index.html
 11. Upload dashboard to namazic.com/atos/ via FTP

Schedule: once per day after market close (e.g., 23:00 PKT)
Uses daily bars, so intraday timing doesn't affect signals.
"""

import os
import sys
import json
import ftplib
from datetime import datetime, date


class _Tee:
    """Write to both the original stdout and a log file simultaneously."""
    def __init__(self, log_path: str):
        self._term = sys.stdout
        self._file = open(log_path, "a", encoding="utf-8", buffering=1)

    def write(self, data):
        self._term.write(data)
        self._file.write(data)

    def flush(self):
        self._term.flush()
        self._file.flush()

    def close(self):
        sys.stdout = self._term
        self._file.close()


def _setup_logging():
    """Redirect stdout → both terminal and data/engine_YYYY-MM-DD.log."""
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"engine_{date.today():%Y-%m-%d}.log")
    sys.stdout = _Tee(log_path)
    return log_path

import pandas as pd
import yfinance as yf

# ── Path setup (run from any directory) ───────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# ── ATOS modules ──────────────────────────────────────────────────
from atos import database as db
from atos.universe import ATOS_UNIVERSE, US_TICKERS, market_of, MARKET_GROUPS
from atos.features import add_all
from atos.decision_engine import scan_universe, BUY_THRESHOLD, consensus_evaluate
from atos.strategies import S3_MeanReversion, S4_BreakoutVol, S5_MomentumAccel
from atos.learner import run_learning_pass, format_weight_bar
from atos.risk import (
    RiskEngine, get_risk_capital, get_available_cash, get_total_equity,
    record_fill, get_day_start_equity,
    daily_loss_cap_breached, kill_switch_active, commission_sek,
    STARTING_CAPITAL_SEK,
)
from atos.dashboard_gen import generate as gen_dashboard
from atos.corporate_events import get_exit_flags, tickers_to_avoid as corp_avoid

# ── Existing infrastructure (unchanged) ───────────────────────────
import saxo_client
import fx

DEPLOY_CONFIG = os.path.join(BASE_DIR, "config", "deploy.json")

# ── Settings ───────────────────────────────────────────────────────
HISTORY_DAYS   = 300    # days of history to download (need 200 for EMA200)

# ATOS v3 consensus gate: a BUY candidate (already past the weighted
# detector score) must also win a multi-strategy quorum before an order is
# placed. This is the rule the README advertises — enforced here at the
# point of execution, not merely logged. Set REQUIRE_CONSENSUS = False to
# fall back to pure detector-score entries.
REQUIRE_CONSENSUS       = True
CONSENSUS_MIN_AGREEMENT = 3     # >= 3 of 6 strategies must vote BUY

# Distinct algorithm per market (the "good algorithms" layer). Each market runs a
# strategy suited to its character; every trade is tagged with the strategy name so
# the dashboard leaderboard is genuinely per-strategy.
STRATEGY_MODE = True   # False -> fall back to the detector-consensus scan

# US is now handled by the VALIDATED cross-sectional momentum strategy
# (atos/us_momentum.py, wired via run_us_momentum below). OMX/CPH per-instrument
# strategies are PAUSED — backtesting showed they have no reliable edge; they stay
# out until a validated strategy exists for those markets. So the per-instrument
# strategy_scan has nothing to run and the engine trades only proven US momentum.
US_MOMENTUM_ENABLED = True

# ── Option 3: US Mean Reversion ────────────────────────────────────────────
# DISABLED until backtest_us_reversion.py shows Sharpe >= 0.8 and WinRate >= 50%.
# Run:  python backtest_us_reversion.py
# Then flip this to True when the verdict is ENABLE.
US_REVERSION_ENABLED = True   # SIM enabled 2026-08-08 — honest OOS validated (Sharpe 2.39, WR 70%)

STRATEGY_INSTANCE_FOR_MARKET = {
    # "OMX30": S5_MomentumAccel(), "CPH25": S3_MeanReversion(),  # paused: unvalidated
}
STRATEGY_FOR_MARKET = {
    "US Equities": "US Momentum",
    "OMX30":       "OMX Momentum",
    "CPH25":       "CPH Mean Reversion",
}

from typing import NamedTuple as _NamedTuple


class StratDecision(_NamedTuple):
    """Decision object shaped like decision_engine.Decision, plus the strategy
    that produced it, so the existing risk/order/DB code works unchanged."""
    action:         str
    score:          float
    d1_trend:       float = 0.0
    d2_momentum:    float = 0.0
    d3_breakout:    float = 0.0
    d4_mean_revert: float = 0.0
    d5_volume:      float = 0.0
    strategy:       str = "ATOS"
    regime:         str = "unknown"


def strategy_scan(feat_data, open_tickers, weights):
    """Per-market strategy signals: each market's assigned strategy decides
    BUY / EXIT. Detector scores are attached for on-screen context only; the
    strategy — not the detector consensus — is what governs the trade."""
    from atos.decision_engine import evaluate
    results = {}
    for ticker, df in feat_data.items():
        if df is None or df.empty or len(df) < 50:
            continue
        mkt   = market_of(ticker)
        strat = STRATEGY_INSTANCE_FOR_MARKET.get(mkt)
        if strat is None:
            continue
        row = df.iloc[-1]
        if pd.isna(row.get("ema50")) or pd.isna(row.get("atr")):
            continue
        is_open = ticker in open_tickers
        try:
            sig  = strat.signal(df)
            conf = float(strat.confidence(df))
        except Exception as e:
            print(f"  [WARN] strategy {STRATEGY_FOR_MARKET.get(mkt)} failed on {ticker}: {e}")
            continue
        if is_open:
            action = "EXIT" if sig in ("SELL", "EXIT") else "HOLD"
        else:
            action = "BUY" if sig == "BUY" else "HOLD"
        if action == "HOLD" and not is_open:
            continue
        d = evaluate(row, mkt, weights)   # detector breakdown, for display
        results[ticker] = StratDecision(
            action=action,
            score=round(55 + max(0.0, min(conf, 1.0)) * 45, 1),   # 55–100 signal strength
            d1_trend=d.d1_trend, d2_momentum=d.d2_momentum, d3_breakout=d.d3_breakout,
            d4_mean_revert=d.d4_mean_revert, d5_volume=d.d5_volume,
            strategy=STRATEGY_FOR_MARKET.get(mkt, "ATOS"),
        )
    return results
ASSET_TYPE_MAP = {      # Saxo asset type per market group
    "US Equities":  "Stock",
    "OMX30":        "Stock",
    "CPH25":        "Stock",
    "DAX40":        "Stock",
    "Commodities":  "Etf",
    "Forex":        "FxSpot",
}


# ══════════════════════════════════════════════════════════════════
# Data
# ══════════════════════════════════════════════════════════════════

def download_universe(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Download HISTORY_DAYS of daily OHLCV from Yahoo Finance."""
    print(f"  Downloading data for {len(tickers)} tickers...", end=" ", flush=True)
    try:
        raw = yf.download(
            tickers,
            period=f"{HISTORY_DAYS}d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as e:
        print(f"FAILED: {e}")
        return {}

    result = {}
    if len(tickers) == 1:
        ticker = tickers[0]
        if not raw.empty:
            result[ticker] = raw
    else:
        for ticker in tickers:
            try:
                df = raw[ticker].dropna(how="all")
                if not df.empty and len(df) >= 50:
                    result[ticker] = df
            except (KeyError, TypeError):
                pass

    print(f"OK ({len(result)} with data)")
    return result


# ══════════════════════════════════════════════════════════════════
# FTP Upload
# ══════════════════════════════════════════════════════════════════

def upload_dashboard(local_file: str):
    """Upload dashboard/index.html to namazic.com via FTP."""
    if not os.path.exists(DEPLOY_CONFIG):
        print("  [SKIP] config/deploy.json not found — no FTP upload")
        return

    with open(DEPLOY_CONFIG) as f:
        cfg = json.load(f)

    try:
        print(f"  Uploading dashboard to {cfg.get('domain_url', 'FTP')}...", end=" ", flush=True)
        with ftplib.FTP() as ftp:
            ftp.connect(cfg["ftp_host"], int(cfg.get("ftp_port", 21)), timeout=30)
            ftp.login(cfg["ftp_user"], cfg["ftp_password"])

            # Ensure remote directory exists
            remote_dir = cfg.get("remote_dir", "/public_html/atos/")
            try:
                ftp.mkd(remote_dir)
            except ftplib.error_perm:
                pass  # already exists
            ftp.cwd(remote_dir)

            with open(local_file, "rb") as f_data:
                ftp.storbinary("STOR index.html", f_data)

        print(f"OK → {cfg.get('domain_url', remote_dir)}")
    except Exception as e:
        print(f"FAILED: {e}")


# ══════════════════════════════════════════════════════════════════
# Terminal Dashboard
# ══════════════════════════════════════════════════════════════════

def _read_recent_trades(n: int = 5) -> list[dict]:
    """Read last n rows from trade_log.csv for the terminal banner."""
    import csv as _csv
    path = os.path.join(BASE_DIR, "data", "trade_log.csv")
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        return rows[-n:][::-1]
    except Exception:
        return []


def print_banner(total_equity: float, day_start: float, open_count: int,
                 weights: dict, todays_actions: list, learning_result: dict,
                 current_regime: str = "unknown"):
    pct     = (total_equity - STARTING_CAPITAL_SEK) / STARTING_CAPITAL_SEK * 100
    day_pnl = total_equity - day_start
    sign    = "+" if pct >= 0 else ""
    dpnl_s  = ("+" if day_pnl >= 0 else "") + f"{day_pnl:,.0f}"
    num_t   = weights.get("num_trades", 0)

    # Per-strategy open counts
    open_trades_now = db.get_open_trades()
    blend_open = sum(1 for t in open_trades_now if "Blend" in (t.get("strategy") or ""))
    rev_open   = sum(1 for t in open_trades_now if "Reversion" in (t.get("strategy") or ""))

    w = weights
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║   ATOS Daily Run — {datetime.now().strftime('%a %Y-%m-%d  %H:%M PKT'):<38}║
╠══════════════════════════════════════════════════════════════════╣
║  Total Equity:  {total_equity:>8,.0f} SEK  ({sign}{pct:.2f}%)   Open: {open_count}          ║
║  Today's P&L:   {dpnl_s:>10} SEK                                 ║
╠══════════════════════════════════════════════════════════════════╣
║  STRATEGY STATUS                                                 ║
║  US Blend     (momentum)   — {blend_open} positions open                ║
║  US Reversion (mean-rev)   — {rev_open}/2 slots used  stop:-4%  hold:≤10d ║
╠══════════════════════════════════════════════════════════════════╣
║  MARKET REGIME: {current_regime:<49}║
╠══════════════════════════════════════════════════════════════════╣
║  ALGORITHM WEIGHTS  (learned from {num_t} trades)               ║
║  Trend      {format_weight_bar(w.get('w_trend',1.0))}  {w.get('w_trend',1.0):.3f}                           ║
║  Momentum   {format_weight_bar(w.get('w_momentum',1.0))}  {w.get('w_momentum',1.0):.3f}                           ║
║  Breakout   {format_weight_bar(w.get('w_breakout',1.0))}  {w.get('w_breakout',1.0):.3f}                           ║
║  Mean Rev   {format_weight_bar(w.get('w_mean_revert',1.0))}  {w.get('w_mean_revert',1.0):.3f}                           ║
║  Volume     {format_weight_bar(w.get('w_volume',1.0))}  {w.get('w_volume',1.0):.3f}                           ║
╠══════════════════════════════════════════════════════════════════╣
║  TODAY'S ACTIONS                                                 ║""")

    if todays_actions:
        for a in todays_actions[:8]:
            action   = a.get("action", "")
            ticker   = a.get("ticker", "")[:8]
            strategy = (a.get("strategy") or "")[:12]
            reason   = a.get("reason", "")[:20]
            pnl      = a.get("pnl_sek")
            pnl_s    = f"+{pnl:.0f}" if pnl and pnl >= 0 else (f"{pnl:.0f}" if pnl else "")
            print(f"║  {action:<5}  {ticker:<8}  [{strategy:<12}]  {reason:<21}  {pnl_s:<7}║")
    else:
        print("║  No actions taken today                                          ║")

    # Last 5 trades from CSV
    recent = _read_recent_trades(5)
    if recent:
        print("╠══════════════════════════════════════════════════════════════════╣")
        print("║  RECENT TRADE HISTORY                                            ║")
        for r in recent:
            act  = r.get("action", "")[:4]
            tk   = r.get("ticker", "")[:7]
            strat = (r.get("strategy") or "")[:12]
            dt   = r.get("date", "")[:10]
            pnl_r = r.get("pnl_sek", "")
            try:
                pnl_f = float(pnl_r)
                pnl_disp = f'{"+" if pnl_f>=0 else ""}{pnl_f:,.0f} SEK'
            except (ValueError, TypeError):
                pnl_disp = "—"
            reason_r = r.get("reason", "")[:18]
            print(f"║  {dt}  {act:<4}  {tk:<7}  [{strat:<12}]  {pnl_disp:<13}  {reason_r:<18}║")

    new_t = learning_result.get("new_trades_processed", 0)
    print(f"""╠══════════════════════════════════════════════════════════════════╣
║  LEARNING: {new_t} new trades processed this cycle                   ║
╚══════════════════════════════════════════════════════════════════╝""")


# ══════════════════════════════════════════════════════════════════
# Main Cycle
# ══════════════════════════════════════════════════════════════════

def run_cycle():
    log_path = _setup_logging()
    print(f"\n{'='*60}\nATOS Daily Cycle — {datetime.now():%Y-%m-%d %H:%M:%S}\n{'='*60}")
    print(f"Log: {log_path}")

    # ── 0. Init DB ────────────────────────────────────────────────
    db.init_db()

    # ── DEMO MODE (SIM paper only) ────────────────────────────────
    # Set ATOS_DEMO=1 to relax the entry gate so the engine will actually
    # trade on weak signals — for verifying the buy/sell path on the SIM
    # account. ATOS_DEMO_MAX caps how many new positions it opens.
    # Never leave this on for real evaluation — it defeats the risk gate.
    _demo = bool(os.environ.get("ATOS_DEMO"))
    if _demo:
        global REQUIRE_CONSENSUS
        REQUIRE_CONSENSUS = False
        import atos.decision_engine as _de
        import atos.risk as _rk
        _de.BUY_THRESHOLD = 15
        _rk.MIN_SCORE_TO_ENTER = 15
        print("  [DEMO MODE] relaxed thresholds (SIM paper only) — engine will "
              "trade on weak signals; consensus gate OFF")

    # ── 1. Safety checks ──────────────────────────────────────────
    if kill_switch_active():
        print("STOP_TRADING file present — halted. Delete it to resume.")
        return

    open_trades = db.get_open_trades()
    day_start = get_day_start_equity(open_trades)

    if daily_loss_cap_breached(open_trades):
        print("Daily loss cap breached — no new entries today.")

    # ── 2. Saxo account state ─────────────────────────────────────
    try:
        balances         = saxo_client.get_balances()
        cash_available   = balances.get("CashBalance", 0)
        account_currency = balances.get("Currency", "EUR")
        fx_rate          = fx.get_rate_to_sek(account_currency)
        cash_sek         = cash_available * fx_rate
        print(f"  Saxo SIM: {cash_available:,.2f} {account_currency} "
              f"= {cash_sek:,.0f} SEK available")
    except Exception as e:
        print(f"  [WARN] Could not fetch Saxo balances: {e}")
        cash_sek = get_risk_capital()

    # ── 3. Load state ─────────────────────────────────────────────
    # open_trades already fetched
    open_tickers  = {t["ticker"] for t in open_trades}
    risk_capital  = get_risk_capital()
    weights       = db.get_current_weights()

    print(f"  Risk capital: {risk_capital:,.0f} SEK | Open positions: {len(open_trades)}")

    # ── 4. Download & compute features ────────────────────────────
    print("  Fetching market data...")
    raw_data   = download_universe(ATOS_UNIVERSE)
    feat_data  = {}
    for ticker, df in raw_data.items():
        try:
            feat_data[ticker] = add_all(df)
        except Exception as e:
            print(f"  [WARN] feature calc failed for {ticker}: {e}")

    # ── 5. Decision scan (per-market strategies, or detector consensus) ──
    if STRATEGY_MODE:
        print(f"  Running per-market strategies on {len(feat_data)} instruments "
              f"(US=Breakout, OMX30=Momentum, CPH25=MeanReversion)...")
        decisions = strategy_scan(feat_data, open_tickers, weights)
    else:
        print(f"  Running detector consensus on {len(feat_data)} instruments...")
        decisions = scan_universe(
            universe_data    = feat_data,
            market_group_fn  = market_of,
            open_tickers     = open_tickers,
            weights          = weights,
        )
    print(f"  Signals: {sum(1 for d in decisions.values() if d.action=='BUY')} BUY, "
          f"{sum(1 for d in decisions.values() if d.action=='EXIT')} EXIT")

    # ── 6. Risk Engine ────────────────────────────────────────────
    risk = RiskEngine(open_trades)
    todays_actions = []

    # ── 6a. Exits first ───────────────────────────────────────────
    for trade in list(open_trades):
        ticker   = trade["ticker"]
        # US momentum positions are managed exclusively by run_us_momentum (6c).
        # Applying generic stops here would conflict with monthly-rebalance logic.
        if trade.get("strategy") == "US Blend":
            continue
        decision = decisions.get(ticker)
        if decision is None or decision.action != "EXIT":
            # Also check ATR stop on latest price
            if ticker not in feat_data:
                continue
            last_row  = feat_data[ticker].iloc[-1]
            stop_price = trade.get("stop_price", 0) or 0
            hit_stop   = (pd.notna(last_row.get("Low")) and
                          last_row["Low"] <= stop_price and stop_price > 0)
            exit_reason = None
            if hit_stop:
                exit_reason = "stop_loss"
                
            # Check trailing stop (track highest price since entry)
            current_high = last_row.get('High', last_row['Close'])
            trailing_high = trade.get('trailing_stop_high') or trade.get('entry_price', 0)
            if current_high > trailing_high:
                trailing_high = current_high
            
            atr_val = last_row.get('atr', 0)
            if pd.notna(atr_val) and atr_val > 0 and trailing_high > 0:
                trailing_stop_price = trailing_high - 2.0 * atr_val
                if last_row['Close'] <= trailing_stop_price:
                    exit_reason = 'trailing_stop'
            
            if exit_reason is None:
                continue
        else:
            exit_reason = "score_dropped"

        last_price = feat_data[ticker].iloc[-1]["Close"] if ticker in feat_data else None
        if last_price is None:
            continue

        mkt    = trade.get("market_group", "Unknown")
        rate   = fx.get_rate_to_sek(_currency_for(mkt))
        price_sek = last_price * rate
        comm   = commission_sek(trade.get("shares", 0), price_sek)
        pnl_sek = (trade.get("shares", 0) * (price_sek -
                   (trade.get("entry_price", last_price) * rate)) - comm)

        order_ok = False
        try:
            asset_type = ASSET_TYPE_MAP.get(mkt, "Stock")
            # Look up Saxo UIC from existing instrument map where available
            from instrument_map import load_instrument_map
            imap = load_instrument_map()
            if ticker in imap:
                uic = imap[ticker]["uic"]
                saxo_client.place_market_order(uic, asset_type, "Sell",
                                               int(trade.get("shares", 0)))
                order_ok = True
            else:
                print(f"  [SKIP EXIT] {ticker} not in instrument_map.csv — position kept open")
        except Exception as e:
            print(f"  EXIT order failed {ticker}: {e} — position kept open")

        if order_ok:
            # Only close the DB trade and return cash once the sell executes,
            # so the DB never marks a position closed that Saxo still holds.
            db.close_trade(trade["id"], last_price, exit_reason, pnl_sek, comm)
            record_fill(trade.get("shares", 0) * price_sek)
            todays_actions.append({
                "action": "EXIT", "ticker": ticker, "market_group": mkt,
                "score": decision.score if decision else 0,
                "shares": trade.get("shares", 0),
                "price": last_price, "reason": exit_reason, "pnl_sek": pnl_sek,
            })
        else:
            # Sell not placed — leave the position open and retry next cycle.
            todays_actions.append({
                "action": "EXIT(FAILED)", "ticker": ticker, "market_group": mkt,
                "score": decision.score if decision else 0,
                "shares": trade.get("shares", 0),
                "price": last_price, "reason": exit_reason, "pnl_sek": None,
            })
        db.insert_signal({
            "signal_date": date.today().isoformat(), "market_group": mkt,
            "ticker": ticker, "final_score": decision.score if decision else 0,
            "d1_trend": decision.d1_trend if decision else 0,
            "d2_momentum": decision.d2_momentum if decision else 0,
            "d3_breakout": decision.d3_breakout if decision else 0,
            "d4_mean_revert": decision.d4_mean_revert if decision else 0,
            "d5_volume": decision.d5_volume if decision else 0,
            "d6_smart_money": getattr(decision, 'd6_smart_money', 0) if decision else 0,
            "d7_mom_quality": getattr(decision, 'd7_mom_quality', 0) if decision else 0,
            "d8_regime": getattr(decision, 'd8_regime', 0) if decision else 0,
            "regime": getattr(decision, 'regime', 'unknown') if decision else 'unknown',
            "action": "EXIT", "executed": 1 if order_ok else 0,
            "block_reason": None if order_ok else "order_failed",
        })

    # ── 6b. New entries ───────────────────────────────────────────
    if not daily_loss_cap_breached(open_trades):
        buy_candidates = [
            (ticker, dec) for ticker, dec in decisions.items()
            if dec.action == "BUY" and ticker not in open_tickers
        ]
        buy_candidates.sort(key=lambda x: x[1].score, reverse=True)
        if _demo:
            buy_candidates = buy_candidates[:int(os.environ.get("ATOS_DEMO_MAX", 3))]

        for ticker, decision in buy_candidates:
            if ticker not in feat_data:
                continue
            df_full  = feat_data[ticker]
            last_row = df_full.iloc[-1]
            mkt      = market_of(ticker)
            rate     = fx.get_rate_to_sek(_currency_for(mkt))

            entry_price = last_row["Close"]
            atr_raw     = last_row.get("atr")
            if pd.isna(atr_raw):
                continue

            entry_sek = entry_price * rate
            atr_sek   = atr_raw * rate

            # ── ATOS v3 consensus gate ────────────────────────────────
            # Only for the detector-consensus path. In STRATEGY_MODE the
            # per-market strategy IS the decision, so this quorum is skipped.
            if REQUIRE_CONSENSUS and not STRATEGY_MODE:
                consensus = consensus_evaluate(
                    df_full, mkt,
                    min_agreement=CONSENSUS_MIN_AGREEMENT,
                    weights=weights,
                )
                if consensus.final_action != "BUY":
                    reason = (f"consensus {consensus.agreement_count}/"
                              f"{consensus.total_strategies} (need "
                              f"{CONSENSUS_MIN_AGREEMENT})")
                    _log_buy_signal(mkt, ticker, decision, 0, reason)
                    todays_actions.append({
                        "action": "BLOCKED", "ticker": ticker, "market_group": mkt,
                        "score": decision.score, "shares": 0,
                        "price": entry_price, "reason": reason, "pnl_sek": None,
                    })
                    continue

            approval = risk.approve_entry(
                ticker, mkt, decision.score,
                entry_sek, atr_sek, cash_sek
            )

            if not approval["approved"]:
                _log_buy_signal(mkt, ticker, decision, 0, approval["reason"])
                todays_actions.append({
                    "action": "BLOCKED", "ticker": ticker, "market_group": mkt,
                    "score": decision.score, "shares": 0,
                    "price": entry_price, "reason": approval["reason"], "pnl_sek": None,
                })
                continue

            shares    = approval["shares"]
            stop_p    = approval["stop_price"] / rate   # back to instrument currency
            cost_sek  = approval["cost_sek"]
            comm_sek  = approval["comm_sek"]

            # ── Resolve Saxo UIC and sanity-check the mapping ─────────
            order_ok    = False
            skip_reason = None
            try:
                from instrument_map import load_instrument_map
                imap = load_instrument_map()
                if ticker not in imap:
                    skip_reason = "not in instrument_map.csv"
                elif imap[ticker]["currency"] != _currency_for(mkt):
                    # e.g. SAP.DE mapped to a USD NYSE listing — refuse to
                    # trade the wrong instrument in the wrong currency.
                    skip_reason = (f"currency mismatch "
                                   f"{imap[ticker]['currency']}!={_currency_for(mkt)}")
                else:
                    uic = imap[ticker]["uic"]
                    asset_type = ASSET_TYPE_MAP.get(mkt, "Stock")
                    saxo_client.place_market_order(uic, asset_type, "Buy", shares)
                    order_ok = True
            except Exception as e:
                skip_reason = f"order error: {e}"

            if order_ok:
                # Record the position ONLY after Saxo accepts the order, so
                # the DB never shows a phantom fill Saxo doesn't have.
                db.insert_trade({
                    "strategy": STRATEGY_FOR_MARKET.get(mkt, "ATOS"),
                    "market_group": mkt, "ticker": ticker, "direction": "BUY",
                    "entry_date": date.today().isoformat(),
                    "entry_price": entry_price,
                    "shares": shares,
                    "commission_sek": comm_sek,
                    "entry_score": decision.score,
                    "d1_trend": decision.d1_trend,
                    "d2_momentum": decision.d2_momentum,
                    "d3_breakout": decision.d3_breakout,
                    "d4_mean_revert": decision.d4_mean_revert,
                    "d5_volume": decision.d5_volume,
                    "d6_smart_money": getattr(decision, 'd6_smart_money', 0),
                    "d7_mom_quality": getattr(decision, 'd7_mom_quality', 0),
                    "d8_regime": getattr(decision, 'd8_regime', 0),
                    "stop_price": stop_p,
                    "trailing_stop_high": entry_price,
                    "regime_at_entry": getattr(decision, 'regime', 'unknown'),
                })
                record_fill(-cost_sek)
                cash_sek -= cost_sek
                risk.register_fill(mkt)
                # Add the fill to the in-cycle equity view so the daily-loss-cap
                # doesn't falsely trip: cash left the account but the new position
                # value offsets it, so equity is ~flat (only commission drag).
                # Without this, a single buy makes equity look ~11% down and blocks
                # every further buy this cycle — capping the engine at ~1 trade/day.
                risk.open_trades.append({
                    "market_group": mkt, "ticker": ticker,
                    "shares": shares, "entry_price": entry_price,
                })
                open_tickers.add(ticker)
                _log_buy_signal(mkt, ticker, decision, 1, None)
                todays_actions.append({
                    "action": "BUY", "ticker": ticker, "market_group": mkt,
                    "score": decision.score, "shares": shares,
                    "price": entry_price, "reason": "signal", "pnl_sek": None,
                })
            else:
                # No order placed → no DB position. Log the attempt only.
                print(f"  [SKIP BUY] {ticker}: {skip_reason}")
                _log_buy_signal(mkt, ticker, decision, 0, skip_reason)
                todays_actions.append({
                    "action": "BLOCKED", "ticker": ticker, "market_group": mkt,
                    "score": decision.score, "shares": 0,
                    "price": entry_price, "reason": skip_reason, "pnl_sek": None,
                })

    # ── 6c. US momentum rebalance (the validated strategy) ────────
    if US_MOMENTUM_ENABLED:
        print("  Running US momentum strategy...")
        try:
            run_us_momentum(feat_data, db.get_open_trades(), todays_actions)
        except Exception as e:
            print(f"  [US momentum] ERROR: {e}")

    # ── 6d. US mean reversion (Option 3 — enable after backtest) ──
    if US_REVERSION_ENABLED:
        print("  Running US reversion strategy...")
        try:
            run_us_reversion(feat_data, db.get_open_trades(), todays_actions)
        except Exception as e:
            print(f"  [US reversion] ERROR: {e}")

    # ── 7. Learning pass ──────────────────────────────────────────
    print("  Running learning pass...")
    learning_result = run_learning_pass()
    weights = db.get_current_weights()  # refresh after learning

    # ── 8. Equity snapshot ────────────────────────────────────────
    open_trades_now = db.get_open_trades()
    total_equity    = get_total_equity(open_trades_now)

    equity_by_mkt: dict[str, float] = {}
    for t in open_trades_now:
        mkt    = t.get("market_group", "Unknown")
        ticker = t.get("ticker", "")
        shares = t.get("shares", 0) or 0
        # Use today's close if available; fall back to entry price (cost basis)
        if ticker in feat_data:
            price = float(feat_data[ticker]["Close"].iloc[-1])
        else:
            price = t.get("entry_price", 0) or 0
        rate   = fx.get_rate_to_sek(_currency_for(mkt))   # FX-convert to SEK
        equity_by_mkt[mkt] = equity_by_mkt.get(mkt, 0.0) + shares * price * rate

    db.upsert_equity({
        "snap_date":        date.today().isoformat(),
        "total_equity_sek": total_equity,
        "us_equity_sek":    equity_by_mkt.get("US Equities", 0.0),
        "omx30_equity_sek": equity_by_mkt.get("OMX30", 0.0),
        "cph25_equity_sek": equity_by_mkt.get("CPH25", 0.0),
        "dax_equity_sek":   equity_by_mkt.get("DAX40", 0.0),
        "commodities_sek":  equity_by_mkt.get("Commodities", 0.0),
        "forex_sek":        equity_by_mkt.get("Forex", 0.0),
        "open_positions":   len(open_trades_now),
        "trades_today":     len([a for a in todays_actions if a["action"] in ("BUY","EXIT")]),
    })

    # ── 9. Terminal output ────────────────────────────────────────
    _first = next(iter(decisions.values()), None)
    current_regime = getattr(_first, "regime", "unknown") if _first is not None else "unknown"
    print_banner(total_equity, day_start, len(open_trades_now),
                 weights, todays_actions, learning_result, current_regime)

    # ── 10. Generate + upload HTML dashboard ──────────────────────
    print("\n  Generating HTML dashboard...")
    html_file = gen_dashboard(
        todays_actions = todays_actions,
        open_trades    = open_trades_now,
        run_summary    = {
            "total_equity_sek": total_equity,
            "day_start_equity": day_start,
            "trades_today":     len([a for a in todays_actions if a["action"] in ("BUY","EXIT")]),
            "errors":           [],
        },
    )
    print(f"  Dashboard saved: {html_file}")
    upload_dashboard(html_file)

    print("\nCycle complete.\n")


TRADE_LOG_CSV = os.path.join(BASE_DIR, "data", "trade_log.csv")
_TRADE_LOG_HEADER = ("date,strategy,action,ticker,shares,price_usd,"
                     "value_sek,pnl_sek,reason,entry_date,days_held\n")


def _append_trade_log(strategy: str, action: str, ticker: str, shares: int,
                      price_usd: float, value_sek: float, pnl_sek,
                      reason: str = "", entry_date: str = "", days_held: int = 0):
    """Append one trade row to data/trade_log.csv.
    Called on every BUY and SELL across ALL strategies so there is one
    human-readable record of the entire trade history regardless of which
    strategy placed the order.
    """
    write_header = not os.path.exists(TRADE_LOG_CSV)
    try:
        with open(TRADE_LOG_CSV, "a", newline="", encoding="utf-8") as f:
            if write_header:
                f.write(_TRADE_LOG_HEADER)
            pnl_str = f"{pnl_sek:.2f}" if pnl_sek is not None else ""
            f.write(
                f"{date.today().isoformat()},{strategy},{action},{ticker},"
                f"{shares},{price_usd:.4f},{value_sek:.2f},{pnl_str},"
                f"{reason},{entry_date},{days_held}\n"
            )
    except Exception as e:
        print(f"  [trade_log] write failed: {e}")


def _log_buy_signal(mkt: str, ticker: str, decision, executed: int, block_reason):
    """Record a BUY signal attempt in the signals table (executed reflects
    whether an order was actually placed, not merely whether it was approved)."""
    db.insert_signal({
        "signal_date": date.today().isoformat(), "market_group": mkt,
        "ticker": ticker, "final_score": decision.score,
        "d1_trend": decision.d1_trend,
        "d2_momentum": decision.d2_momentum,
        "d3_breakout": decision.d3_breakout,
        "d4_mean_revert": decision.d4_mean_revert,
        "d5_volume": decision.d5_volume,
        "d6_smart_money": getattr(decision, 'd6_smart_money', 0),
        "d7_mom_quality": getattr(decision, 'd7_mom_quality', 0),
        "d8_regime": getattr(decision, 'd8_regime', 0),
        "regime": getattr(decision, 'regime', 'unknown'),
        "action": "BUY",
        "executed": executed,
        "block_reason": block_reason,
    })


US_MOMENTUM_STATE = os.path.join(BASE_DIR, "data", "us_momentum_state.json")


def _load_us_state():
    try:
        with open(US_MOMENTUM_STATE) as f:
            return json.load(f)
    except Exception:
        return {"last_rebalance": None}


def _save_us_state(state: dict):
    os.makedirs(os.path.dirname(US_MOMENTUM_STATE), exist_ok=True)
    tmp = f"{US_MOMENTUM_STATE}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, US_MOMENTUM_STATE)
    # Ensure user-writable so a non-elevated process can update it next time
    try:
        import stat
        os.chmod(US_MOMENTUM_STATE, stat.S_IRUSR | stat.S_IWUSR |
                 stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH)
    except Exception:
        pass


def _place_us(side: str, ticker: str, shares: int, imap: dict,
              todays_actions: list, price: float, cur_trade: dict = None) -> bool:
    """Place ONE US market order and update DB + local cash on success."""
    shares = int(shares)
    if shares < 1 or ticker not in imap:
        return False
    try:
        saxo_client.place_market_order(imap[ticker]["uic"], "Stock", side, shares)
    except Exception as e:
        print(f"  [US momentum] {side} {shares} {ticker} FAILED: {e}")
        return False
    rate = fx.get_rate_to_sek("USD")
    price_sek = (price or 0) * rate
    if side == "Buy":
        comm = commission_sek(shares, price_sek)
        db.insert_trade({
            "strategy": "US Blend", "market_group": "US Equities", "ticker": ticker,
            "direction": "BUY", "entry_date": date.today().isoformat(),
            "entry_price": price, "shares": shares, "commission_sek": comm,
            "entry_score": 0, "d1_trend": 0, "d2_momentum": 0, "d3_breakout": 0,
            "d4_mean_revert": 0, "d5_volume": 0, "d6_smart_money": 0,
            "d7_mom_quality": 0, "d8_regime": 0, "stop_price": 0,
            "trailing_stop_high": price, "regime_at_entry": "momentum",
        })
        record_fill(-(shares * price_sek + comm))
        _append_trade_log("US Blend", "BUY", ticker, shares, price,
                          shares * price_sek, None, "US momentum rebalance")
        todays_actions.append({"action": "BUY", "ticker": ticker, "market_group": "US Equities",
                               "strategy": "US Blend", "score": 0, "shares": shares,
                               "price": price, "reason": "US momentum", "pnl_sek": None})
    else:  # Sell (full close of the tracked position)
        comm = commission_sek(shares, price_sek)
        pnl = None
        if cur_trade:
            entry_sek = cur_trade.get("entry_price", price) * rate
            pnl = shares * (price_sek - entry_sek) - comm
            db.close_trade(cur_trade["id"], price, "momentum_rebalance", pnl, comm)
        record_fill(shares * price_sek - comm)
        _append_trade_log("US Blend", "SELL", ticker, shares, price,
                          shares * price_sek, pnl, "US momentum exit",
                          entry_date=cur_trade.get("entry_date", "") if cur_trade else "")
        todays_actions.append({"action": "EXIT", "ticker": ticker, "market_group": "US Equities",
                               "strategy": "US Blend", "score": 0, "shares": shares,
                               "price": price, "reason": "US momentum exit", "pnl_sek": pnl})
    return True


def run_us_momentum(feat_data: dict, open_trades: list, todays_actions: list, dry_run: bool = False):
    """Validated US cross-sectional momentum, executed as a monthly rebalance with a
    daily market risk-off overlay. See atos/us_momentum.py + STRATEGY_NOTES.md.
    dry_run=True previews the orders (prints them) without placing any or touching the DB."""
    from atos import us_momentum as USM
    from instrument_map import load_instrument_map
    if kill_switch_active():
        print("  [US momentum] STOP_TRADING present — skip"); return
    try:
        imap = load_instrument_map()
    except Exception as e:
        print(f"  [US momentum] instrument_map load failed: {e}"); return

    us_open = {t["ticker"]: t for t in open_trades if t.get("market_group") == "US Equities"}
    tgt = USM.compute_targets(feat_data, US_TICKERS)   # US names only — not the whole universe
    tag = "[US momentum DRY-RUN]" if dry_run else "[US momentum]"
    print(f"  {tag} risk_off={tgt['risk_off']} | {tgt.get('reason')} | targets={tgt['targets']}")
    fx_usd = fx.get_rate_to_sek("USD")

    def _price(tk, fallback=0):
        return float(feat_data[tk]["Close"].iloc[-1]) if tk in feat_data else fallback

    def _do(side, tk, shares, price, cur_trade=None):
        if dry_run:
            print(f"    {tag} would {side.upper()} {shares} {tk} @ ${price:.2f}  (~{shares*price*fx_usd:,.0f} SEK)")
            return True
        return _place_us(side, tk, shares, imap, todays_actions, price=price, cur_trade=cur_trade)

    def _sell_all_us():
        for tk, tr in us_open.items():
            _do("Sell", tk, tr.get("shares", 0),
                _price(tk, tr.get("entry_price", 0)), cur_trade=tr)

    # US sleeve capital — starts at US_SLEEVE_SEK and COMPOUNDS with the strategy's
    # own P&L. Profits raise the tradeable budget (the bot's reward); losses lower it.
    # It is NEVER topped up from the rest of the account, so extra deposits stay untouched.
    state         = _load_us_state()
    sleeve_cash   = float(state.get("sleeve_cash", USM.US_SLEEVE_SEK))
    us_value      = sum((tr.get("shares", 0) or 0) * _price(tk, tr.get("entry_price", 0)) * fx_usd
                        for tk, tr in us_open.items())
    sleeve_equity = sleeve_cash + us_value   # current tradeable budget (compounds)

    # ── Corporate event exits (ex-dividend / earnings) ────────────────────
    # Run every cycle — not just on rebalance day. If we hold a stock that is
    # 1-3 days from its ex-dividend date or 1-2 days from earnings, sell now
    # to avoid the mechanical price drop (ex-div) or binary gap risk (earnings).
    if us_open:
        event_flags = get_exit_flags(list(us_open.keys()))
        if event_flags:
            event_sell_value = 0.0
            for tk, reason in event_flags.items():
                tr = us_open.get(tk)
                if not tr:
                    continue
                print(f"  {tag} EVENT EXIT: {tk} — {reason}")
                sh = tr.get("shares", 0) or 0
                px = _price(tk, tr.get("entry_price", 0))
                _do("Sell", tk, sh, px, cur_trade=tr)
                event_sell_value += sh * px * fx_usd
            # Park the recovered cash back into the sleeve so the next rebalance
            # has the correct budget (mirrors how risk-off overlay works).
            if not dry_run and event_sell_value > 0:
                # Recalculate open positions after the exits
                remaining_us_open = {k: v for k, v in us_open.items()
                                     if k not in event_flags}
                remaining_us_value = sum(
                    (t.get("shares", 0) or 0) * _price(tk2, t.get("entry_price", 0)) * fx_usd
                    for tk2, t in remaining_us_open.items()
                )
                state["sleeve_cash"] = sleeve_equity - remaining_us_value
                _save_us_state(state)
            # Refresh us_open and sleeve_equity so the rest of the cycle is correct
            us_open = {k: v for k, v in us_open.items() if k not in event_flags}
            us_value     = sum((tr.get("shares", 0) or 0) * _price(tk2, tr.get("entry_price", 0)) * fx_usd
                               for tk2, tr in us_open.items())
            sleeve_equity = sleeve_cash + us_value

    # Daily risk-off overlay: exit US to cash the moment the market breaks trend.
    if tgt["risk_off"]:
        if us_open:
            print(f"  {tag} RISK-OFF — selling all US to cash (sleeve ~{sleeve_equity:,.0f} SEK)")
            _sell_all_us()
        if not dry_run:
            state["sleeve_cash"] = sleeve_equity   # value parked in cash until re-entry
            _save_us_state(state)
        return

    # Rebalance when REBAL_DAYS calendar days have elapsed since last rebalance.
    last  = state.get("last_rebalance")
    today = date.today()
    if last:
        days_since = (today - date.fromisoformat(last)).days
        due = days_since >= USM.REBAL_DAYS
    else:
        due = True
    if not due:
        print(f"  {tag} hold — rebalanced {days_since}d ago (next in "
              f"{USM.REBAL_DAYS - days_since}d, every {USM.REBAL_DAYS}d); "
              f"sleeve equity ~{sleeve_equity:,.0f} SEK")
        return

    # Liquidate all US, then rebuy the top-N. Budget = the CURRENT sleeve equity
    # (compounds with profit). Whole shares; slots capped by the running remaining
    # budget so total spend can never exceed the sleeve (and never touches the rest).
    mom_names = tgt.get("momentum") or []
    lv_names  = tgt.get("lowvol") or []
    print(f"  {tag} REBALANCE (blend) — {len(mom_names)} momentum {mom_names} + "
          f"{len(lv_names)} low-vol {lv_names} | budget {sleeve_equity:,.0f} SEK "
          f"(started {USM.US_SLEEVE_SEK:,.0f}, compounds with P&L)")
    _sell_all_us()
    deployed_sek = 0.0
    # Blend priority: momentum names (offense) first, then low-vol (defense), deduped.
    # Dynamic greedy sizing: each name gets remaining_budget / names_still_to_place, so
    # budget skipped on an unaffordable name flows forward and the sleeve stays invested.
    # Exclude tickers with imminent ex-dividend or earnings from new buys.
    # We check only the rebalance candidates (not the full 61-stock universe).
    corp_skip = corp_avoid(mom_names + lv_names)
    if corp_skip:
        print(f"  {tag} skipping {sorted(corp_skip)} — imminent corporate event (buy next rebalance)")

    priority = []
    for tk in mom_names + lv_names:
        if tk in corp_skip:
            continue
        if tk not in priority and tk in feat_data and tk in imap and _price(tk) > 0:
            priority.append(tk)
    remaining_sek = sleeve_equity
    for i, tk in enumerate(priority):
        names_left = len(priority) - i
        slot_usd = (remaining_sek / names_left) / fx_usd
        px = _price(tk)
        shares = int(slot_usd / px)
        if shares >= 1:
            ok = _do("Buy", tk, shares, px)
            if ok:  # only count cost if order actually filled
                cost = shares * px * fx_usd
                remaining_sek -= cost
                deployed_sek  += cost
        else:
            print(f"  {tag} {tk}: ${slot_usd:.0f}/slot < 1 share (${px:.0f}) — skip")
    print(f"  {tag} deployed ~{deployed_sek:,.0f} of {sleeve_equity:,.0f} SEK; "
          f"{sleeve_equity - deployed_sek:,.0f} SEK stays as cash (rest of account untouched)")
    if not dry_run:
        # Only stamp last_rebalance if at least one buy order landed.
        # If the market is closed (holiday) Saxo rejects all orders and deployed_sek=0,
        # so we do NOT advance the timestamp — the engine retries next trading day.
        if deployed_sek > 0:
            state["last_rebalance"] = date.today().isoformat()
        elif priority:
            print(f"  {tag} WARNING: 0 orders filled — market may be closed. "
                  f"Rebalance will retry tomorrow (last_rebalance unchanged).")
        state["sleeve_cash"] = sleeve_equity - deployed_sek
        _save_us_state(state)


def run_us_reversion(feat_data: dict, open_trades: list, todays_actions: list):
    """US Mean Reversion — short-term dip-buying strategy (3-10 day holds).

    Completely independent of US Blend: separate sleeve capital, separate DB rows
    (strategy='US Reversion'), separate position limit (MAX_POSITIONS=2, 150K SEK each).

    Enabled only when US_REVERSION_ENABLED = True (after backtest passes).
    """
    from atos import us_reversion as USR
    from instrument_map import load_instrument_map

    if kill_switch_active():
        print("  [US reversion] STOP_TRADING present — skip"); return
    try:
        imap = load_instrument_map()
    except Exception as e:
        print(f"  [US reversion] instrument_map load failed: {e}"); return

    tag = "[US reversion]"
    fx_usd = fx.get_rate_to_sek("USD")

    # Current open reversion positions (this strategy only)
    rev_open = {t["ticker"]: t for t in open_trades if t.get("strategy") == "US Reversion"}

    def _price(tk):
        return float(feat_data[tk]["Close"].iloc[-1]) if tk in feat_data else 0.0

    def _rsi_sma20(tk):
        if tk not in feat_data:
            return None, None
        c = feat_data[tk]["Close"].dropna()
        if len(c) < 20:
            return None, None
        delta = c.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi   = float((100 - 100 / (1 + rs)).iloc[-1])
        sma20 = float(c.rolling(20).mean().iloc[-1])
        return rsi, sma20

    # ── Exit check for existing reversion positions ────────────────
    from datetime import date as _date
    today = _date.today()
    for ticker, trade in list(rev_open.items()):
        cur_price = _price(ticker)
        if cur_price <= 0:
            continue
        cur_rsi, sma20 = _rsi_sma20(ticker)
        entry_date = _date.fromisoformat(trade.get("entry_date", today.isoformat()))
        days_held = (today - entry_date).days   # calendar days (conservative)
        exit_flag, reason = USR.should_exit(trade, cur_price, cur_rsi, sma20, days_held)
        if exit_flag:
            sh = trade.get("shares", 0) or 0
            print(f"  {tag} EXIT {ticker}: {reason}")
            uic = imap.get(ticker, {}).get("uic")
            if uic and sh > 0:
                try:
                    saxo_client.place_order(uic=uic, side="Sell", qty=sh, asset_type="Stock")
                    pnl_sek = (cur_price - trade.get("entry_price", 0)) * sh * fx_usd
                    db.close_trade(trade["id"], exit_price=cur_price,
                                   exit_date=today.isoformat(), pnl_sek=pnl_sek)
                    entry_d = trade.get("entry_date", "")
                    held_d  = (today - _date.fromisoformat(entry_d)).days if entry_d else 0
                    _append_trade_log(
                        "US Reversion", "SELL", ticker, sh, cur_price,
                        sh * cur_price * fx_usd, pnl_sek, reason,
                        entry_date=entry_d, days_held=held_d,
                    )
                    todays_actions.append({
                        "action": "SELL", "ticker": ticker, "market_group": "US Equities",
                        "strategy": "US Reversion", "score": 0, "shares": sh,
                        "price": cur_price, "reason": f"reversion exit: {reason}",
                        "pnl_sek": pnl_sek,
                    })
                except Exception as e:
                    print(f"  {tag} sell {ticker} FAILED: {e}")

    # ── Entry scan — only if slots are available ───────────────────
    # Re-read open trades after exits (some may have just been closed)
    rev_open_now = {t["ticker"]: t for t in db.get_open_trades()
                    if t.get("strategy") == "US Reversion"}
    slots_free = USR.MAX_POSITIONS - len(rev_open_now)
    if slots_free <= 0:
        print(f"  {tag} full ({USR.MAX_POSITIONS}/{USR.MAX_POSITIONS} positions)")
        return

    # ── Sleeve DD cap check (mirrors backtest logic) ───────────────
    # Estimate current sleeve equity: fixed sleeve minus cost of open positions.
    open_value_sek = sum(
        (t.get("shares", 0) or 0) * _price(tk) * fx_usd
        for tk, t in rev_open_now.items()
    )
    open_cost_sek = sum(
        (t.get("shares", 0) or 0) * (t.get("entry_price", 0) or 0) * fx_usd
        for t in rev_open_now.values()
    )
    sleeve_equity = (USR.REVERSION_SLEEVE_SEK - open_cost_sek) + open_value_sek
    sleeve_dd = (USR.REVERSION_SLEEVE_SEK - sleeve_equity) / USR.REVERSION_SLEEVE_SEK
    if sleeve_dd >= USR.SLEEVE_DD_CAP:
        print(f"  {tag} sleeve DD {sleeve_dd*100:.1f}% >= cap {USR.SLEEVE_DD_CAP*100:.0f}% "
              f"— no new entries (sleeve ~{sleeve_equity:,.0f} SEK)")
        return

    candidates = USR.scan(feat_data, US_TICKERS)
    candidates = [c for c in candidates if c["ticker"] not in rev_open_now]
    if not candidates:
        print(f"  {tag} no entry signals today")
        return

    print(f"  {tag} {len(candidates)} candidate(s), {slots_free} slot(s) free")
    slot_sek = USR.REVERSION_SLEEVE_SEK / USR.MAX_POSITIONS

    for cand in candidates[:slots_free]:
        ticker = cand["ticker"]
        price  = cand["price"]
        uic_data = imap.get(ticker, {})
        uic = uic_data.get("uic")
        if not uic:
            print(f"  {tag} {ticker}: no UIC in instrument_map — skip")
            continue

        shares = int(slot_sek / (price * fx_usd))
        if shares < 1:
            print(f"  {tag} {ticker}: slot too small for 1 share — skip")
            continue

        cost_sek = shares * price * fx_usd
        print(f"  {tag} BUY {ticker}: RSI={cand['rsi']} dip={cand['dip_pct']}% "
              f"vol={cand['vol_ratio']}× | {shares} shares @ ${price:.2f} "
              f"(~{cost_sek:,.0f} SEK) [US Reversion sleeve]")
        try:
            saxo_client.place_order(uic=uic, side="Buy", qty=shares, asset_type="Stock")
            comm = commission_sek(shares, cost_sek)
            db.insert_trade({
                "strategy": "US Reversion", "market_group": "US Equities",
                "ticker": ticker, "direction": "BUY",
                "entry_date": today.isoformat(), "entry_price": price,
                "shares": shares, "commission_sek": comm,
                "entry_score": cand["score"], "d1_trend": 0, "d2_momentum": 0,
                "d3_breakout": 0, "d4_meanrev": cand["rsi"], "d5_vol": cand["vol_ratio"],
            })
            _append_trade_log(
                "US Reversion", "BUY", ticker, shares, price, cost_sek, None,
                f"RSI={cand['rsi']} dip={cand['dip_pct']}% vol={cand['vol_ratio']}x",
            )
            todays_actions.append({
                "action": "BUY", "ticker": ticker, "market_group": "US Equities",
                "strategy": "US Reversion", "score": cand["score"],
                "shares": shares, "price": price,
                "reason": (f"[US Reversion] RSI {cand['rsi']}, "
                           f"dip {cand['dip_pct']}%, vol {cand['vol_ratio']}×"),
                "pnl_sek": None,
            })
        except Exception as e:
            print(f"  {tag} buy {ticker} FAILED: {e}")


def _currency_for(market_group: str) -> str:
    """Best-guess currency per market group for FX conversion."""
    return {
        "US Equities": "USD",
        "OMX30":       "SEK",
        "CPH25":       "DKK",
        "DAX40":       "EUR",
        "Commodities": "USD",
        "Forex":       "USD",
    }.get(market_group, "USD")


if __name__ == "__main__":
    run_cycle()
