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

import pandas as pd
import yfinance as yf

# ── Path setup (run from any directory) ───────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# ── ATOS modules ──────────────────────────────────────────────────
from atos import database as db
from atos.universe import ATOS_UNIVERSE, market_of, MARKET_GROUPS
from atos.features import add_all
from atos.decision_engine import scan_universe, BUY_THRESHOLD, consensus_evaluate
from atos.learner import run_learning_pass, format_weight_bar
from atos.risk import (
    RiskEngine, get_risk_capital, get_available_cash, get_total_equity,
    record_fill, get_day_start_equity,
    daily_loss_cap_breached, kill_switch_active, commission_sek,
    STARTING_CAPITAL_SEK,
)
from atos.dashboard_gen import generate as gen_dashboard

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

# Strategy label per market — attributes each trade to a named strategy sleeve so
# the dashboard leaderboard is per-strategy. Today ATOS runs one consensus method
# per market, so these are three instances of it (one per market).
STRATEGY_FOR_MARKET = {
    "US Equities": "ATOS US",
    "OMX30":       "ATOS OMX30",
    "CPH25":       "ATOS CPH25",
}
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

def print_banner(total_equity: float, day_start: float, open_count: int,
                 weights: dict, todays_actions: list, learning_result: dict,
                 current_regime: str = "unknown"):
    pct = (total_equity - STARTING_CAPITAL_SEK) / STARTING_CAPITAL_SEK * 100
    day_pnl = total_equity - day_start
    sign    = "+" if pct >= 0 else ""
    dpnl_s  = ("+" if day_pnl >= 0 else "") + f"{day_pnl:,.0f}"
    num_t   = weights.get("num_trades", 0)

    w = weights
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║   ATOS Daily Run — {datetime.now().strftime('%a %Y-%m-%d  %H:%M PKT'):<38}║
╠══════════════════════════════════════════════════════════════════╣
║  Total Equity:  {total_equity:>8,.0f} SEK  ({sign}{pct:.2f}%)   Open: {open_count}/10     ║
║  Today's P&L:   {dpnl_s:>10} SEK                                 ║
╠══════════════════════════════════════════════════════════════════╣
║  MARKET REGIME: {current_regime:<49}║
╠══════════════════════════════════════════════════════════════════╣
║  ALGORITHM WEIGHTS  (learned from {num_t} trades)               ║
║  Trend      {format_weight_bar(w.get('w_trend',1.0))}  {w.get('w_trend',1.0):.3f}                           ║
║  Momentum   {format_weight_bar(w.get('w_momentum',1.0))}  {w.get('w_momentum',1.0):.3f}                           ║
║  Breakout   {format_weight_bar(w.get('w_breakout',1.0))}  {w.get('w_breakout',1.0):.3f}                           ║
║  Mean Rev   {format_weight_bar(w.get('w_mean_revert',1.0))}  {w.get('w_mean_revert',1.0):.3f}                           ║
║  Volume     {format_weight_bar(w.get('w_volume',1.0))}  {w.get('w_volume',1.0):.3f}                           ║
║  SmartMoney {format_weight_bar(w.get('w_smart_money',1.0))}  {w.get('w_smart_money',1.0):.3f}                           ║
║  MomQuality {format_weight_bar(w.get('w_mom_quality',1.0))}  {w.get('w_mom_quality',1.0):.3f}                           ║
║  Regime     {format_weight_bar(w.get('w_regime',1.0))}  {w.get('w_regime',1.0):.3f}                           ║
╠══════════════════════════════════════════════════════════════════╣
║  TODAY'S ACTIONS                                                 ║""")

    if todays_actions:
        for a in todays_actions[:8]:
            action = a.get("action","")
            ticker = a.get("ticker","")[:8]
            score  = a.get("score", 0)
            reason = a.get("reason","")[:25]
            pnl    = a.get("pnl_sek")
            pnl_s  = f"+{pnl:.0f}" if pnl and pnl >= 0 else (f"{pnl:.0f}" if pnl else "")
            print(f"║  {action:<5}  {ticker:<10}  Score:{score:<5}  {reason:<26}  {pnl_s:<8}║")
    else:
        print("║  No actions taken today                                          ║")

    new_t = learning_result.get("new_trades_processed", 0)
    print(f"""╠══════════════════════════════════════════════════════════════════╣
║  LEARNING: {new_t} new trades processed this cycle                   ║
╚══════════════════════════════════════════════════════════════════╝""")


# ══════════════════════════════════════════════════════════════════
# Main Cycle
# ══════════════════════════════════════════════════════════════════

def run_cycle():
    print(f"\n{'='*60}\nATOS Daily Cycle — {datetime.now():%Y-%m-%d %H:%M:%S}\n{'='*60}")

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

    # ── 5. Decision Engine scan ───────────────────────────────────
    print(f"  Running Decision Engine on {len(feat_data)} instruments...")
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
            # The weighted detector score qualified this candidate; now
            # require a multi-strategy quorum before risking capital.
            if REQUIRE_CONSENSUS:
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
        shares = t.get("shares", 0) or 0
        price  = t.get("entry_price", 0) or 0
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
