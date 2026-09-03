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
import subprocess
import numpy as np
from datetime import datetime, date


class _Tee:
    """Write to both an original stream (stdout or stderr) and a shared log file."""
    def __init__(self, term, log_file):
        self._term = term
        self._file = log_file

    def write(self, data):
        self._term.write(data)
        self._file.write(data)

    def flush(self):
        self._term.flush()
        self._file.flush()


def _setup_logging():
    """Redirect stdout AND stderr -> both terminal and data/engine_YYYY-MM-DD.log.

    2026-08-28: found live -- only stdout was ever redirected here, so any
    real error reported via Python's `logging` module (e.g. saxo_order.py's
    `logger.error(...)` on a rejected order, which falls through to
    logging's stderr-only "handler of last resort" since nothing in this
    module's import chain ever calls logging.basicConfig()) was silently
    discarded — never landed in data/engine_*.log, never visible anywhere,
    including when the scheduled task runs with no console window at all.
    Confirmed live: US Reversion's PYPL buy was REJECTED 4 separate times
    today with only "no position opened, no DB row recorded" in the log —
    the actual Saxo API error code/message existed only on the invisible
    stderr stream. Both streams now share one file handle so nothing is
    lost, and interleave in the order they were actually written.
    """
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"engine_{date.today():%Y-%m-%d}.log")
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)
    return log_path

import pandas as pd

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
from atos import notifier
import atos.capital_config as CAP
from atos.corporate_events import get_exit_flags, tickers_to_avoid as corp_avoid
from atos.intraday_reversion import intraday_scan, us_market_is_open, next_scan_description

# ── Existing infrastructure (unchanged) ───────────────────────────
import saxo_client
import saxo_order
import saxo_fx
import saxo_history
import proc_lock

# ── AI observation layer (2026-09-02) -- OBSERVE/LOG ONLY, ships OFF ──
# Guarded exactly like forex/runner.py: if the ai package fails to import
# (or config/ai.json is missing/off) every hook below sees ai_config is
# None / the stubs and no-ops. There is NO apply path -- these hooks only
# build proposals, log them, and (when the paid agent is on) log a shadow
# decision. A stocks trade is never resized or skipped by any of this.
try:
    import ai.config as ai_config
    import ai.agent.trading_copilot as ai_trading_copilot
    from ai.features import stock_cards as ai_stock_cards
    from ai.features import stock_proposal as ai_stock_proposal
    from ai.features import basket_ranker as ai_basket_ranker
except Exception:                                      # pragma: no cover
    ai_config = None
    ai_trading_copilot = None
    ai_stock_cards = None
    ai_stock_proposal = None
    ai_basket_ranker = None

DEPLOY_CONFIG = os.path.join(BASE_DIR, "config", "deploy.json")


def _sek_per_eur() -> float | None:
    """SEK value of one EUR, from Saxo's live quotes. None on failure -- the
    AI card writers then skip the EUR conversion rather than guess."""
    try:
        r = _rate_to_sek("EUR")
        return float(r) if r and r > 0 else None
    except Exception:
        return None


def _rate_to_sek(ccy: str) -> float:
    """SEK value of one unit of `ccy`, from Saxo's own live quotes only.

    Per explicit user direction (2026-08-22): live trading decisions must
    use Saxo, never Yahoo -- replaces the old Yahoo-based fx module's
    rate lookup throughout this file's live paths. Preserves that function's
    contract (returns a float or raises) so none of the many existing
    call sites below need their own error handling changed. A couple of
    retries before raising: this queries live majors (USD/EUR/DKK/GBP/
    CHF/CAD/AUD), which are reliable on Saxo, so a transient miss is the
    much more likely failure mode than a genuinely unavailable currency.
    """
    for attempt in range(2):
        rate = saxo_fx.rate_to_sek([ccy]).get(ccy)
        if rate:
            return rate
    raise RuntimeError(f"No live Saxo rate for {ccy}/SEK after retries")

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

# The comment above describes the INTENT, but STRATEGY_MODE=True alone never
# actually stopped strategy_scan() from running -- it still scores all 385
# instruments every cycle via US Breakout / OMX Momentum / CPH Mean Reversion
# (STRATEGY_NOTES.md: "Per-market signal strategies ... -- weak", explicitly
# rejected, never cleared the live bar) and its BUY/EXIT decisions still feed
# straight into real order placement in run_cycle()'s sections 6a/6b. Zero
# real trades from these 3 strategies have ever landed in the DB (pure luck
# of the signal not firing yet, not because the path was actually gated) --
# and now that US Reversion has open positions again, 6a's generic ATR/
# trailing-stop exit loop (which only skips "US Blend") would fight
# run_us_reversion()'s own RSI/time/stop exit logic for the same trade,
# earlier in the same cycle. Set True only after a per-market strategy
# actually clears the validation bar in STRATEGY_NOTES.md.
LEGACY_PER_MARKET_STRATEGY_ENABLED = False

# ── Option 3: US Mean Reversion ────────────────────────────────────────────
# DISABLED until backtest_us_reversion.py shows Sharpe >= 0.8 and WinRate >= 50%.
# Run:  python backtest_us_reversion.py
# Then flip this to True when the verdict is ENABLE.
US_REVERSION_ENABLED = True   # SIM enabled 2026-08-08 — honest OOS validated (Sharpe 2.39, WR 70%)

# ── USA Strategy signals (2026-09-03) ──────────────────────────────────────
# 4 independent SIM-only strategies from the usa_strategy package:
#   SMAStrategy (US SMA Crossover), RSIStrategy (US RSI Reversal),
#   MomentumStrategy (US Momentum), EnsembleStrategy (US Ensemble).
# Runs on the same US universe; tracked in the trades table with distinct
# strategy names; visible on the dashboard. CORE STRATEGIES UNTOUCHED.
US_SIGNALS_ENABLED = True

# ── SIM paper-fill fallback (2026-09-01) ──────────────────────────────────
# Mirrors forex/runner.py's SIM_PAPER_FILL_ON_REJECT. Saxo SIM's order
# engine has been rejecting essentially every order with
# "CouldNotCompleteRequest (90)" since ~2026-08-28 (hits forex, stocks and
# protective stops alike). Forex rides it out by booking the fill locally;
# stocks had no such fallback, so a valid signal (e.g. PYPL US Reversion:
# RSI 33, -10.5% dip, 3.3x vol, 2026-08-31 — ~18 rejected attempts, then the
# window closed) was simply missed.
#
# With this on, a rejected SIM stock BUY is booked in the trades table with
# paper=1 at the scan price and managed by ATOS's own should_exit() /
# rebalance logic. housekeeping.StocksAdapter skips paper rows (no Saxo
# counterpart to reconcile).
#
# 2026-09-02: `_STOCKS_ENV` is now the real env gate. It defaults to "sim"
# (atos_runner.run_cycle / daily_run.py / run_open_scan / run_us_reversion /
# run_intraday_cycle never touch it). The real-money engine atos_live_stocks.py
# calls set_stocks_env("live") and runs ONLY the US Blend path
# (run_us_blend_live -> run_us_momentum). _sx() is threaded into every
# US-Blend-path Saxo call so those hit the LIVE account; every US Reversion /
# legacy / intraday Saxo call is left SIM-only forever. A paper-fill can NEVER
# happen unless _STOCKS_ENV == "sim" (LIVE rejections are logged + skipped, no
# phantom row).
_STOCKS_ENV = "sim"
STOCKS_SIM_PAPER_FILL_ON_REJECT = True


def set_stocks_env(env: str) -> None:
    """Point the US-Blend Saxo path at `env` ("sim" | "live"). Called once by
    atos_live_stocks.py before any cycle. Raises on anything else."""
    global _STOCKS_ENV
    if env not in ("sim", "live"):
        raise ValueError(f"_STOCKS_ENV must be 'sim' or 'live', got {env!r}")
    _STOCKS_ENV = env


def _sx() -> str:
    return _STOCKS_ENV


def _stocks_paper_fill_enabled() -> bool:
    assert _STOCKS_ENV in ("sim", "live")
    return STOCKS_SIM_PAPER_FILL_ON_REJECT and _STOCKS_ENV == "sim"


def stocks_live_commission_sek(shares: int, price_usd: float, fx_usd_sek: float) -> float:
    """Real-money US-stock commission estimate for the LIVE ledger. Saxo Classic
    US-stock ~USD 0.02/share, min ~USD 3 (confirm the account's plan -- Phase 2
    reads /trade/v1/infoprices Commissions for the exact figure). Uses the LIVE
    USD/SEK rate, not risk.py's hardcoded 10.5."""
    per_share_usd = 0.02
    min_usd = 3.0
    comm_usd = max(shares * per_share_usd, min_usd)
    return comm_usd * (fx_usd_sek or _rate_to_sek("USD") or 10.5)


def _sim_cap_shares(shares: int, price_usd: float, fx_usd_sek: float) -> int:
    """Clamp a stock BUY to config/capital.json account.sim_max_trade_notional_eur
    (2026-09-01, user -- SIM is for testing, not size). The stocks module is
    SIM-only. Rarely binds (a reversion slot is ~SEK 13,500 / ~EUR 1,250) but
    a backstop if the sleeves grow. 0/absent = disabled."""
    try:
        cap_eur = CAP.sim_max_trade_notional_eur()
    except Exception:
        return shares
    if not cap_eur or cap_eur <= 0 or shares < 1 or not price_usd or not fx_usd_sek:
        return shares
    cap_sek = cap_eur * (_rate_to_sek("EUR") or 11.5)
    if shares * price_usd * fx_usd_sek <= cap_sek:
        return shares
    capped = max(int(cap_sek / (price_usd * fx_usd_sek)), 1)
    if capped != shares:
        print(f"  [stocks] SIM notional cap: {shares} → {capped} sh "
              f"(cap €{cap_eur:,.0f})")
    return capped


def _heal_missing_stock_stops(open_trades: list) -> None:
    """Re-place the broker-side protective stop for any open non-paper stock
    position whose original stop was rejected (stop_order_id is NULL).

    2026-09-01: Saxo SIM started accepting ENTRY orders again after the
    ~Aug-28 outage, but rejected the stop placed microseconds later with
    "NotOwned" -- a settlement race (the fill hasn't propagated yet).
    place_with_stop returned (entry_oid, None): the position is real, the
    broker stop is missing. This runs once per cycle, confirms the position
    is actually held at Saxo (settled) and not already stop-covered, then
    places a GTC stop and records its id. Software-side exits
    (US Reversion should_exit(), US Blend trailing) protect it meanwhile.
    Best-effort -- never raises into the cycle."""
    need = [t for t in open_trades
            if not t.get("paper") and not t.get("stop_order_id")
            and (t.get("stop_price") or 0) > 0 and (t.get("shares") or 0) > 0]
    if not need:
        return
    try:
        from instrument_map import load_instrument_map
        imap = load_instrument_map()
    except Exception as e:
        print(f"  [stop-heal] instrument_map load failed: {e}")
        return
    try:
        held: dict = {}
        for p in saxo_client.get_positions(env=_sx()).get("Data", []):
            b = p.get("PositionBase", {})
            u, amt = b.get("Uic"), b.get("Amount", 0)
            if u is not None and amt:
                held[u] = held.get(u, 0.0) + amt
    except Exception as e:
        print(f"  [stop-heal] could not fetch Saxo positions: {e}")
        return
    try:
        stopped_uics = {
            o.get("Uic") for o in saxo_client.get_orders("Stock", env=_sx()).get("Data", [])
            if o.get("BuySell") == "Sell"
            and "Stop" in str(o.get("OpenOrderType") or o.get("OrderType") or "")
        }
    except Exception:
        stopped_uics = set()

    try:
        ak = saxo_client.get_account_key(env=_sx())
    except Exception as e:
        print(f"  [stop-heal] no account key: {e}")
        return

    healed = 0
    for t in need:
        tk = t["ticker"]
        uic = (imap.get(tk) or {}).get("uic")
        if not uic:
            continue
        if uic not in held or abs(held[uic]) < (t["shares"] or 0) * 0.999:
            continue   # not settled / not fully held yet -- retry next cycle
        if uic in stopped_uics:
            db.set_stop_order_id(t["id"], "EXISTING")
            continue
        oid = saxo_order.place_stop_only(
            post_fn=lambda path, body: saxo_client.post(path, body, env=_sx()),
            account_key=ak, uic=uic,
            asset_type="Stock", amount=int(t["shares"]),
            entry_side=("Buy" if str(t.get("direction", "BUY")).upper() == "BUY" else "Sell"),
            stop_price=float(t["stop_price"]), symbol=tk)
        if oid:
            db.set_stop_order_id(t["id"], oid)
            healed += 1
            print(f"  [stop-heal] {tk}: broker stop restored @ {t['stop_price']} (id {oid})")
    if healed:
        print(f"  [stop-heal] restored {healed} missing broker stop(s)")


# ── Fill confirmation ───────────────────────────────────────────────────────
# Same bug class as forex/runner.py: saxo_order.place_with_stop() returns an
# OrderId but no fill and no price, so a buy was recorded at the scan price
# (`price`), not what actually filled -- and an accepted-but-unfilled order
# recorded a phantom DB row anyway (this is what feeds the WSM/MTB/GEV
# hourly re-buy loop -- reconcile closes the phantom, the scan re-signals).
_STOCK_FILL_ATTEMPTS = 3
_STOCK_FILL_DELAY_S  = 1.5


def _confirm_stock_fill(entry_oid: str, uic: int) -> tuple[bool, float]:
    """(filled, real_average_fill_price) for the stock position `entry_oid`
    opened. Matches PositionBase.SourceOrderId == entry_oid, else a position
    on the same Uic opened in the last ~3 min. Never raises. (False, 0.0)
    means accepted-but-unfilled -- caller books it paper on SIM."""
    import time as _t
    want = str(entry_oid)
    for attempt in range(_STOCK_FILL_ATTEMPTS):
        if attempt:
            _t.sleep(_STOCK_FILL_DELAY_S)
        try:
            data = saxo_client.get_positions(env=_sx()).get("Data", [])
        except Exception:
            continue
        for p in data:
            b = p.get("PositionBase", {})
            op = b.get("OpenPrice")
            if not op:
                continue
            if str(b.get("SourceOrderId", "")) == want:
                return True, float(op)
            if b.get("Uic") == uic and (b.get("Amount") or 0) != 0:
                opened = str(b.get("ExecutionTimeOpen", ""))
                try:
                    dt = datetime.fromisoformat(opened.replace("Z", "+00:00"))
                    if (datetime.now(dt.tzinfo) - dt).total_seconds() <= 180:
                        return True, float(op)
                except (ValueError, AttributeError):
                    pass
    return False, 0.0


# ── Signal caches — written by run_us_momentum/run_us_reversion, read by dashboard ──
_blend_signal: dict  = {}   # keys: targets, risk_off, reason, momentum, lowvol
_rev_signals:  list  = []   # list of candidate dicts from USR.scan()

# ── Dynamic capital allocation — loaded from config/capital.json ──────────
# Edit config/capital.json to change percentages; no code change needed.
BLEND_CASH_PCT = CAP.blend_allocation_pct()
REV_CASH_PCT   = CAP.reversion_allocation_pct()

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
    """Download HISTORY_DAYS of daily OHLCV from Saxo's own live chart API.

    Was Yahoo (yf.download) -- switched 2026-08-22 per explicit user
    direction (live trading decisions must use Saxo, not Yahoo) after
    confirming live that Saxo's SIM chart endpoint DOES serve real
    historical stock data (a stale comment elsewhere in this codebase,
    data_loader.py, claimed otherwise -- never re-verified until now, see
    [[saxo_api_verification]]). Same return shape as before
    ({ticker: DataFrame[Open,High,Low,Close,Volume]}, >=50 bars only) so
    every downstream caller (add_all(), US Blend, US Reversion) is
    unaffected. Yahoo remains correct for data_loader.py's backtesting job.
    """
    print(f"  Downloading data for {len(tickers)} tickers (Saxo)...", end=" ", flush=True)
    try:
        result = saxo_history.fetch_daily_bars(tickers, count=HISTORY_DAYS)
    except Exception as e:
        print(f"FAILED: {e}")
        return {}

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


def _strategy_scorecard() -> dict:
    """Return per-strategy stats for the terminal banner.

    Reads data/atos_live.db directly (db.get_all_closed_trades()) -- the
    same source of truth atos/dashboard_gen.py's _strat_stats() uses --
    instead of parsing trade_log.csv separately. The two logs had already
    drifted apart (the CSV showed 20 Blend trades/35% WR while the DB had
    30+ for the same period), so this banner and the HTML dashboard could
    show different numbers for the same strategy. A trade with an unknown
    P&L (pnl_sek is NULL -- e.g. an old reconciliation cleanup row) is
    excluded rather than treated as a 0 or a loss.
    """
    result = {}
    try:
        closed = db.get_all_closed_trades()
    except Exception:
        return result

    for strat_key, label in [("Blend", "US Blend"), ("Reversion", "US Reversion")]:
        trades = [t for t in closed
                  if strat_key in (t.get("strategy") or "")
                  and t.get("pnl_sek") is not None]
        n = len(trades)
        pnls = [t["pnl_sek"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total_pnl = sum(pnls)
        wr = len(wins) / n * 100 if n > 0 else 0
        avg_win  = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        result[label] = {
            "n": n, "wr": wr, "total_pnl": total_pnl,
            "avg_win": avg_win, "avg_loss": avg_loss,
        }
    return result


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
║  US Reversion (mean-rev)   — {rev_open} slots used  stop:-4%  hold:≤10d   ║
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

    # Strategy comparison scorecard
    sc = _strategy_scorecard()
    if sc:
        blend_s = sc.get("US Blend", {})
        rev_s   = sc.get("US Reversion", {})
        b_n     = blend_s.get("n", 0)
        r_n     = rev_s.get("n", 0)
        b_wr    = blend_s.get("wr", 0)
        r_wr    = rev_s.get("wr", 0)
        b_pnl   = blend_s.get("total_pnl", 0)
        r_pnl   = rev_s.get("total_pnl", 0)
        b_aw    = blend_s.get("avg_win", 0)
        r_aw    = rev_s.get("avg_win", 0)
        b_al    = blend_s.get("avg_loss", 0)
        r_al    = rev_s.get("avg_loss", 0)
        winner_pnl = "🔵 Blend" if b_pnl > r_pnl else ("🟠 Revers" if r_pnl > b_pnl else "—   Tie")
        winner_wr  = "🔵 Blend" if b_wr  > r_wr  else ("🟠 Revers" if r_wr  > b_wr  else "—   Tie")
        print("╠══════════════════════════════════════════════════════════════════╣")
        print("║  STRATEGY HEAD-TO-HEAD                                           ║")
        print(f"║  {'Metric':<16}  {'US Blend':>12}  {'US Reversion':>12}  {'Leader':<9}║")
        print(f"║  {'─'*16}  {'─'*12}  {'─'*12}  {'─'*9}║")
        print(f"║  {'Trades':<16}  {b_n:>12}  {r_n:>12}  {'':9}║")
        print(f"║  {'Win Rate':<16}  {b_wr:>11.1f}%  {r_wr:>11.1f}%  {winner_wr:<9}║")
        print(f"║  {'Total P&L (SEK)':<16}  {b_pnl:>+12,.0f}  {r_pnl:>+12,.0f}  {winner_pnl:<9}║")
        print(f"║  {'Avg Win (SEK)':<16}  {b_aw:>+12,.0f}  {r_aw:>+12,.0f}  {'':9}║")
        print(f"║  {'Avg Loss (SEK)':<16}  {b_al:>+12,.0f}  {r_al:>+12,.0f}  {'':9}║")

    new_t = learning_result.get("new_trades_processed", 0)
    print(f"""╠══════════════════════════════════════════════════════════════════╣
║  LEARNING: {new_t} new trades processed this cycle                   ║
╚══════════════════════════════════════════════════════════════════╝""")


# ══════════════════════════════════════════════════════════════════
# Open-market scan (called by intraday_monitor at 09:35 ET)
# ══════════════════════════════════════════════════════════════════

def run_open_scan(log_fn=None) -> dict:
    """Run US strategy signal scan at market open.

    Downloads the most recent daily bars for the full US universe, computes
    features, then runs both US strategies (Blend + Reversion) and places any
    approved orders. Designed to be called once per trading day by
    intraday_monitor.py at ~09:35 ET (5 min after market open).

    Args:
        log_fn: optional callable(str) used for progress messages; defaults
                to print so the monitor's terminal sees the output.

    Returns:
        dict with keys: buy_count, exit_count, blocked_count, actions
    """
    _log = log_fn or print

    if date.today().weekday() >= 5:  # 5=Saturday, 6=Sunday
        _log(f"ATOS Open Scan — {datetime.now():%Y-%m-%d %H:%M:%S} — "
             f"skipped, weekend (US market closed)")
        return {"buy_count": 0, "exit_count": 0, "blocked_count": 0, "actions": []}

    _log(f"\n{'='*60}")
    _log(f"ATOS Open Scan — {datetime.now():%Y-%m-%d %H:%M:%S}")
    _log("Strategies: US Blend (momentum) + US Reversion (mean-rev)")
    _log(f"{'='*60}")

    _write_status("running")
    db.init_db()

    if kill_switch_active():
        _log("  STOP_TRADING file present — skipping open scan.")
        _write_status("idle")
        return {"buy_count": 0, "exit_count": 0, "blocked_count": 0, "actions": []}

    # Fetch live account cash
    try:
        balances = saxo_client.get_balances()
        cash_available = balances.get("CashBalance", 0)
        account_currency = balances.get("Currency", "EUR")
        fx_rate = _rate_to_sek(account_currency)
        cash_sek = cash_available * fx_rate
        _log(f"  Cash: {cash_available:,.2f} {account_currency} = {cash_sek:,.0f} SEK")
    except Exception as e:
        _log(f"  [WARN] Balances unavailable ({e}) — using risk capital estimate")
        cash_sek = get_risk_capital()

    # Download US universe (daily bars, same data the nightly engine uses)
    _log(f"  Downloading daily data for {len(US_TICKERS)} US tickers...")
    raw_data = download_universe(list(US_TICKERS))
    feat_data: dict = {}
    for ticker, df in raw_data.items():
        try:
            feat_data[ticker] = add_all(df)
        except Exception as e:
            _log(f"  [WARN] features failed for {ticker}: {e}")
    _log(f"  {len(feat_data)} tickers ready")

    if not feat_data:
        _log("  No market data — aborting open scan.")
        _write_status("idle")
        return {"buy_count": 0, "exit_count": 0, "blocked_count": 0, "actions": []}

    open_trades = db.get_open_trades()
    todays_actions: list = []

    # ── US Blend (cross-sectional momentum) ───────────────────────
    # Cap at starting_capital_sek so SIM demo credit never inflates position sizes.
    _max_deploy = CAP.starting_capital_sek() * CAP.max_deploy_pct()
    blend_budget = min(cash_sek * BLEND_CASH_PCT, _max_deploy * BLEND_CASH_PCT)
    _log(f"  US Blend — budget: {blend_budget:,.0f} SEK ({BLEND_CASH_PCT*100:.0f}% of cash, capped at {_max_deploy * BLEND_CASH_PCT:,.0f} SEK)")
    try:
        run_us_momentum(feat_data, open_trades, todays_actions,
                        available_cash_sek=blend_budget)
    except Exception as e:
        _log(f"  [US Blend ERROR] {e}")

    # ── US Reversion (mean reversion) ─────────────────────────────
    rev_budget = min(cash_sek * REV_CASH_PCT, _max_deploy * REV_CASH_PCT)
    _log(f"  US Reversion — budget: {rev_budget:,.0f} SEK ({REV_CASH_PCT*100:.0f}% of cash, capped at {_max_deploy * REV_CASH_PCT:,.0f} SEK)")
    try:
        run_us_reversion(feat_data, db.get_open_trades(), todays_actions,
                         available_cash_sek=rev_budget)
    except Exception as e:
        _log(f"  [US Reversion ERROR] {e}")

    # ── US Signals (4 SIM-only strategies) ───────────────────────
    if US_SIGNALS_ENABLED:
        _log("  Running USA strategy signals (SMA/RSI/Momentum/Ensemble)...")
        try:
            run_us_signals(feat_data, db.get_open_trades(), todays_actions)
        except Exception as e:
            _log(f"  [US signals ERROR] {e}")

    buy_n     = sum(1 for a in todays_actions if a["action"] == "BUY")
    exit_n    = sum(1 for a in todays_actions if a["action"] == "EXIT")
    blocked_n = sum(1 for a in todays_actions if a["action"] == "BLOCKED")

    _write_status("complete", buy_n, exit_n, blocked_n, actions=todays_actions)
    _send_notification(
        "ATOS Open Scan",
        f"{buy_n} BUY · {exit_n} EXIT  |  "
        f"{len(db.get_open_trades())} positions open",
    )
    _log(f"\n  Open scan done — {buy_n} BUY, {exit_n} EXIT, {blocked_n} blocked\n")

    return {"buy_count": buy_n, "exit_count": exit_n,
            "blocked_count": blocked_n, "actions": todays_actions}


# ══════════════════════════════════════════════════════════════════
# Main Cycle
# ══════════════════════════════════════════════════════════════════

def run_cycle():
    # US equities don't trade on weekends -- skip the whole cycle (universe
    # download, per-market scan, US Blend/Reversion, dashboard regen) rather
    # than burn all that against a market that's definitely closed. Nothing
    # would change over the weekend anyway (no new price data, no fills
    # possible), so there's no exit/trailing-stop management being lost.
    if date.today().weekday() >= 5:  # 5=Saturday, 6=Sunday
        print(f"ATOS Daily Cycle — {datetime.now():%Y-%m-%d %H:%M:%S} — "
              f"skipped, weekend (US market closed)")
        _write_status("idle")
        return

    log_path = _setup_logging()
    _write_status("running")
    print(f"\n{'='*60}\nATOS Daily Cycle — {datetime.now():%Y-%m-%d %H:%M:%S}\n{'='*60}")
    print(CAP.summary())
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
        fx_rate          = _rate_to_sek(account_currency)
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
    # Disabled by default -- see LEGACY_PER_MARKET_STRATEGY_ENABLED above.
    # Neither branch has ever produced a validated live strategy; both stay
    # wired to real order placement in 6a/6b whenever they're on.
    if not LEGACY_PER_MARKET_STRATEGY_ENABLED:
        print("  Per-market/detector-consensus scan disabled (never validated -- "
              "see LEGACY_PER_MARKET_STRATEGY_ENABLED). Only US Blend + US "
              "Reversion trade.")
        decisions = {}
    elif STRATEGY_MODE:
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
    # Skipped entirely while LEGACY_PER_MARKET_STRATEGY_ENABLED is False:
    # every open position today belongs to US Blend or US Reversion, and
    # both already have their own dedicated exit logic (6c/6d below). This
    # generic ATR/trailing-stop loop only ever skipped "US Blend" by name,
    # so it would otherwise also fire on US Reversion positions -- using
    # rules that know nothing about US Reversion's RSI/time/stop exits --
    # racing run_us_reversion()'s own exit check later in the same cycle.
    for trade in list(open_trades) if LEGACY_PER_MARKET_STRATEGY_ENABLED else []:
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
        rate   = _rate_to_sek(_currency_for(mkt))
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

    # ── 6a2. Heal missing broker-side stops ───────────────────────
    # 2026-09-01: on 2026-09-01 the Saxo SIM order engine started accepting
    # ENTRY orders again (after the ~Aug-28 outage) but rejected the
    # protective stop placed microseconds later with "NotOwned" -- a
    # settlement race (the fill hasn't propagated to the account yet).
    # place_with_stop returns (entry_oid, None); the position is real but
    # has no broker stop. This re-attempts the stop each cycle once the
    # position has settled. Software-side exits still run regardless
    # (US Reversion's should_exit(), US Blend's trailing check).
    try:
        _heal_missing_stock_stops(db.get_open_trades())
    except Exception as e:
        print(f"  [stop-heal] skipped: {e}")

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
            rate     = _rate_to_sek(_currency_for(mkt))

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
                    # Attach the stop-loss atomically with the entry (native
                    # Saxo GTC order, enforced 24/7 even if this machine or
                    # the next scheduled run is down) instead of a bare
                    # market order relying on software-side checks later.
                    saxo_order.place_with_stop(
                        post_fn=saxo_client.post,
                        account_key=saxo_client.get_account_key(),
                        uic=uic, asset_type=asset_type, amount=shares,
                        buy_sell="Buy", stop_price=stop_p, label=f"{mkt}:{ticker}",
                    )
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
    _max_deploy2 = CAP.starting_capital_sek() * CAP.max_deploy_pct()
    blend_budget = min(cash_sek * BLEND_CASH_PCT, _max_deploy2 * BLEND_CASH_PCT)
    if US_MOMENTUM_ENABLED:
        print(f"  Running US momentum strategy... (budget: {blend_budget:,.0f} SEK = {BLEND_CASH_PCT*100:.0f}% of capital, capped at {_max_deploy2 * BLEND_CASH_PCT:,.0f})")
        try:
            run_us_momentum(feat_data, db.get_open_trades(), todays_actions,
                            available_cash_sek=blend_budget)
        except Exception as e:
            print(f"  [US momentum] ERROR: {e}")

    # ── 6d. US mean reversion (Option 3 — enable after backtest) ──
    rev_budget = min(cash_sek * REV_CASH_PCT, _max_deploy2 * REV_CASH_PCT)
    if US_REVERSION_ENABLED:
        print(f"  Running US reversion strategy... (budget: {rev_budget:,.0f} SEK = {REV_CASH_PCT*100:.0f}% of capital, capped at {_max_deploy2 * REV_CASH_PCT:,.0f})")
        try:
            run_us_reversion(feat_data, db.get_open_trades(), todays_actions,
                             available_cash_sek=rev_budget)
        except Exception as e:
            print(f"  [US reversion] ERROR: {e}")

    # ── 6e. USA Strategy signals (SIM-only, 4 strategies) ─────────
    if US_SIGNALS_ENABLED:
        print("  Running USA strategy signals (SMA/RSI/Momentum/Ensemble)...")
        try:
            run_us_signals(feat_data, db.get_open_trades(), todays_actions)
        except Exception as e:
            print(f"  [US signals] ERROR: {e}")

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
        rate   = _rate_to_sek(_currency_for(mkt))   # FX-convert to SEK
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
            "total_equity_sek":      total_equity,
            "day_start_equity":      day_start,
            "trades_today":          len([a for a in todays_actions if a["action"] in ("BUY","EXIT")]),
            "errors":                [],
            "blend_targets":         _blend_signal.get("targets", []),
            "blend_risk_off":        _blend_signal.get("risk_off", False),
            "reversion_candidates":  _rev_signals,
        },
    )
    print(f"  Dashboard saved: {html_file}")
    upload_dashboard(html_file)

    # ── 11. Weekly report email (Fridays only) ────────────────────
    if date.today().weekday() == 4:  # Friday
        try:
            _send_weekly_report(total_equity, day_start, open_trades_now)
        except Exception as e:
            print(f"  [notifier] weekly report failed: {e}")

    # ── 12. Write scan state snapshot (read by dashboard_saxo.ps1) ──
    try:
        _rev_cands_logged = len(_rev_signals)
        _rev_executed     = sum(1 for c in _rev_signals if c.get("ticker") in
                                {a["ticker"] for a in todays_actions if a.get("strategy") == "US Reversion"})
        _rev_max          = CAP.reversion_slots(len(US_TICKERS))
        _blend_tgts       = _blend_signal.get("targets", [])
        _blend_risk_off   = _blend_signal.get("risk_off", False)
        _last_reb_state   = _load_us_state()
        _last_reb         = _last_reb_state.get("last_rebalance")
        _days_since_reb   = (date.today() - date.fromisoformat(_last_reb)).days if _last_reb else None
        _write_scan_state({
            "scan_ts":         datetime.now().isoformat(),
            "strategies": {
                "US Blend": {
                    "status":            "risk_off" if _blend_risk_off else "rebalanced" if _blend_tgts else "hold",
                    "targets":           _blend_tgts,
                    "risk_off":          _blend_risk_off,
                    "reason":            _blend_signal.get("reason", ""),
                    "days_since_rebalance": _days_since_reb,
                },
                "US Reversion": {
                    "candidates_found":  _rev_cands_logged,
                    "executed":          _rev_executed,
                    "max_slots":         _rev_max,
                    "slots_used":        len([t for t in open_trades_now
                                             if t.get("strategy") == "US Reversion"]),
                },
            },
            "total_equity_sek": total_equity,
            "open_positions":   len(open_trades_now),
            "signals_logged":   len(todays_actions),
        })
    except Exception as e:
        print(f"  [scan_state] failed: {e}")

    buy_n     = sum(1 for a in todays_actions if a["action"] == "BUY")
    exit_n    = sum(1 for a in todays_actions if a["action"] == "EXIT")
    blocked_n = sum(1 for a in todays_actions if a["action"] == "BLOCKED")
    _write_status("complete", buy_n, exit_n, blocked_n, actions=todays_actions)
    _send_notification(
        "ATOS Scan Complete",
        f"{buy_n} BUY · {exit_n} EXIT · {blocked_n} blocked  |  "
        f"{len(open_trades_now)} positions open  |  "
        f"Equity {total_equity:,.0f} SEK"
    )

    # See safeguard.py's module docstring for why this runs after every
    # real cycle -- atos_live.db is a P&L LEDGER, not a simple position
    # tracker, so its adapter only ever corrects an overstated `shares`
    # count and never auto-closes a row (that needs a real exit price);
    # naked-position protection is still fixed+verified like every module.
    try:
        import safeguard
        safeguard.run_safeguard(["stocks"])
    except Exception as e:
        print(f"  [SAFEGUARD] post-run fix pass failed: {e}")

    print("\nCycle complete.\n")


def _send_weekly_report(total_equity: float, day_start: float, open_trades: list) -> None:
    """Build and send the Friday weekly P&L email."""
    from datetime import timedelta
    week_ago    = (date.today() - timedelta(days=7)).isoformat()
    closed_all  = db.get_all_closed_trades()
    closed_week = [t for t in closed_all if (t.get("exit_date") or "") >= week_ago]
    week_pnl    = sum(t.get("pnl_sek", 0) or 0 for t in closed_week)

    def _stats(strategy):
        trades = [t for t in closed_all if t.get("strategy") == strategy]
        wins   = [t for t in trades if (t.get("pnl_sek") or 0) > 0]
        return {
            "n":         len(trades),
            "wr":        len(wins) / len(trades) * 100 if trades else 0,
            "total_pnl": sum(t.get("pnl_sek", 0) or 0 for t in trades),
        }

    notifier.notify_weekly_report(
        total_equity_sek = total_equity,
        week_pnl_sek     = week_pnl,
        open_trades      = open_trades,
        closed_this_week = closed_week,
        blend_stats      = _stats("US Blend"),
        reversion_stats  = _stats("US Reversion"),
        starting_capital = STARTING_CAPITAL_SEK,
    )


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


def _log_buy_signal(mkt: str, ticker: str, decision, executed: int, block_reason,
                    strategy: str = "ATOS", scan_ts: str = None):
    """Record a BUY signal attempt in the signals table."""
    db.insert_signal({
        "signal_date": date.today().isoformat(),
        "scan_ts":     scan_ts or datetime.now().isoformat(),
        "strategy":    strategy,
        "market_group": mkt,
        "ticker":      ticker,
        "final_score": decision.score,
        "d1_trend":    decision.d1_trend,
        "d2_momentum": decision.d2_momentum,
        "d3_breakout": decision.d3_breakout,
        "d4_mean_revert": decision.d4_mean_revert,
        "d5_volume":   decision.d5_volume,
        "d6_smart_money": getattr(decision, 'd6_smart_money', 0),
        "d7_mom_quality": getattr(decision, 'd7_mom_quality', 0),
        "d8_regime":   getattr(decision, 'd8_regime', 0),
        "regime":      getattr(decision, 'regime', 'unknown'),
        "action":      "BUY",
        "executed":    executed,
        "block_reason": block_reason,
    })


# The real-money US Blend sleeve (atos_live_stocks.py) sets
# ATOS_US_MOMENTUM_STATE=data/us_momentum_state_live.json before importing this
# module, so its rebalance clock / sleeve-cash never touches SIM's file. SIM
# leaves it unset.
US_MOMENTUM_STATE = os.environ.get("ATOS_US_MOMENTUM_STATE") or os.path.join(
    BASE_DIR, "data", "us_momentum_state.json")
SCAN_STATE_FILE   = os.path.join(BASE_DIR, "data", "atos_scan_state.json")
STATUS_FILE       = os.path.join(BASE_DIR, "data", "atos_status.json")


def _write_status(status: str, buy_count: int = 0, exit_count: int = 0,
                  blocked_count: int = 0, actions: list = None):
    """Write run status for the dashboard status banner to read."""
    payload = {
        "status":        status,
        "timestamp":     datetime.now().isoformat(),
        "buy_count":     buy_count,
        "exit_count":    exit_count,
        "blocked_count": blocked_count,
        "actions":       actions or [],
    }
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, STATUS_FILE)
    except Exception:
        pass


def _send_notification(title: str, msg: str):
    """Windows balloon notification — no external packages."""
    try:
        ps_cmd = (
            'Add-Type -AssemblyName System.Windows.Forms; '
            '$n = New-Object System.Windows.Forms.NotifyIcon; '
            '$n.Icon = [System.Drawing.SystemIcons]::Information; '
            '$n.Visible = $true; '
            f'$n.BalloonTipTitle = "{title}"; '
            f'$n.BalloonTipText = "{msg}"; '
            '$n.ShowBalloonTip(8000); '
            'Start-Sleep -Milliseconds 8500; $n.Dispose()'
        )
        subprocess.Popen(
            ['powershell', '-WindowStyle', 'Hidden', '-Command', ps_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _write_scan_state(state: dict):
    """Overwrite atos_scan_state.json with this cycle's summary.
    Read by dashboard_saxo.ps1 / saxo_dashboard_helper.py."""
    try:
        tmp = SCAN_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, SCAN_STATE_FILE)
    except Exception as e:
        print(f"  [scan_state] write failed: {e}")


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


US_BLEND_STOP_PCT = 0.08   # 8% stop-loss, matching the ETF module's convention
US_BLEND_TP_PCT   = 0.20   # 20% take-profit, matching the ETF module's convention


def _blend_book_state() -> dict:
    """Rebalance clock + current holdings for BOTH US Blend books (SIM and the
    real-money live_stocks sleeve), read from their own state files + ledgers.
    Handed to the AI basket-ranker so it can track how the two books' timing
    and holdings have drifted -- OBSERVE only. Never raises."""
    from datetime import date as _date
    out: dict = {}
    _books = {
        "sim":         (os.path.join(BASE_DIR, "data", "us_momentum_state.json"),
                        os.path.join(BASE_DIR, "data", "atos_live.db")),
        "live_stocks": (os.path.join(BASE_DIR, "data", "us_momentum_state_live.json"),
                        os.path.join(BASE_DIR, "data", "atos_live_stocks.db")),
        "ai_sim":      (os.path.join(BASE_DIR, "data", "us_momentum_state_ai.json"),
                        os.path.join(BASE_DIR, "data", "atos_ai.db")),
    }
    try:
        from atos import us_momentum as _USM
        rebal_days = int(_USM.REBAL_DAYS)
    except Exception:
        rebal_days = 14
    for name, (state_f, db_f) in _books.items():
        info: dict = {"last_rebalance": None, "days_since": None,
                      "next_due_in_days": None, "holdings": {}}
        try:
            if os.path.exists(state_f):
                last = json.load(open(state_f)).get("last_rebalance")
                info["last_rebalance"] = last
                if last:
                    ds = (_date.today() - _date.fromisoformat(last)).days
                    info["days_since"] = ds
                    info["next_due_in_days"] = max(0, rebal_days - ds)
        except Exception:
            pass
        try:
            if os.path.exists(db_f):
                import sqlite3 as _sq
                con = _sq.connect(db_f)
                try:
                    rows = con.execute(
                        "select ticker, shares from trades where exit_price is null "
                        "and strategy='US Blend'").fetchall()
                finally:
                    con.close()
                info["holdings"] = {t: int(s or 0) for t, s in rows}
        except Exception:
            pass
        out[name] = info
    return out


def _place_us(side: str, ticker: str, shares: int, imap: dict,
              todays_actions: list, price: float, cur_trade: dict = None,
              strategy: str = "US Blend", account_env: str = "sim") -> bool:
    """Place ONE US market order and update DB + local cash on success.
    Routes to _sx() -- "sim" for atos_runner.run_cycle, "live" only when
    atos_live_stocks.py has set it.

    account_env == "ai_sim" (the AI-decision paper twin, atos_ai_stocks.py):
    NEVER touches Saxo -- books the fill locally at the scan price and skips
    the per-trade notifier email. Its DB is data/atos_ai.db (ATOS_DB_PATH),
    fully isolated from the deterministic SIM / real-money books."""
    # 2026-09-02: real-money hard guard. _place_us is only ever called from the
    # US Blend rebalance path (run_us_momentum). Assert it so a future caller
    # can't route a non-Blend order to the live account.
    if _sx() == "live" and strategy != "US Blend":
        raise RuntimeError(f"_place_us on the LIVE account is US-Blend-only, got {strategy!r}")
    _paper_twin = account_env == "ai_sim"
    shares = int(shares)
    if side == "Buy":
        shares = _sim_cap_shares(shares, price, _rate_to_sek("USD"))
    if shares < 1 or ticker not in imap:
        return False
    paper = 0
    entry_oid = stop_oid = None
    try:
        if _paper_twin:
            # AI SIM twin -- no Saxo order at all (the shared SIM account has
            # no margin left for a parallel book, and its orders would collide
            # with the deterministic book's on the same tickers). Book paper.
            paper = 1
            if side == "Sell" and cur_trade is None:
                return False
        elif side == "Buy":
            # Fetch the live Saxo mid-price before computing stop/TP so we
            # anchor the bracket to the actual tradable price, not a bar close
            # that may be hours stale (e.g. a 19:20 PKT run uses yesterday's
            # daily close; the stock may have gapped 3-5% by open).
            _live = saxo_client.get_quote(imap[ticker]["uic"], "Stock", env=_sx())
            if _live:
                if abs(_live - price) / max(price, 1e-9) > 0.001:
                    print(f"  [US momentum] {ticker}: scan ${price:.2f} → live "
                          f"${_live:.2f} ({(_live / price - 1) * 100:+.2f}%) "
                          f"— using live price for stop/TP")
                price = _live
            elif account_env in ("live", "live_eur"):
                # LIVE trade with no live price — alert prominently; use scan close as fallback
                import sys as _sys
                print(f"\n  *** WARNING [{ticker}] LIVE price fetch FAILED — using stale scan "
                      f"close ${price:.2f}. Saxo token may be expired. ***\n", file=_sys.stderr, flush=True)
                try:
                    import atos.notifier as _ntf
                    from datetime import date as _date
                    _ntf._send(
                        f"ATOS ALERT: Live price fetch failed — {ticker} [{_date.today()}]",
                        f"<p><b style='color:#f87171'>LIVE price fetch FAILED for {ticker}</b></p>"
                        f"<p>Placing LIVE BUY with <b>stale scan close ${price:.2f}</b>.</p>"
                        f"<p>Stop/TP anchored to stale price — check Saxo LIVE token.</p>"
                        f"<p>Run: <code>python saxo_client.py --test-live</code></p>",
                    )
                except Exception:
                    pass
            # Attach stop-loss/take-profit atomically with the entry — this
            # strategy previously placed a bare market order with no broker-
            # side protection at all (stop_price was hardcoded to 0), relying
            # entirely on the next scheduled cycle to notice and exit. A
            # native Saxo GTC bracket is enforced 24/7 even if a run is missed.
            stop_p = round(price * (1 - US_BLEND_STOP_PCT), 2)
            tp_p   = round(price * (1 + US_BLEND_TP_PCT), 2)
            entry_oid, stop_oid, _ = saxo_order.place_with_stop(
                post_fn=lambda path, body: saxo_client.post(path, body, env=_sx()),
                account_key=saxo_client.get_account_key(env=_sx()),
                uic=imap[ticker]["uic"], asset_type="Stock", amount=shares,
                buy_sell="Buy", stop_price=stop_p, take_profit_price=tp_p,
                label=f"US Blend:{ticker}",
            )
            if entry_oid is None:
                # Saxo rejected it (no exception -- place_with_stop returns
                # None). Previously this fell through and recorded a PHANTOM
                # DB row Saxo didn't have. Now: paper-fill on SIM, else skip.
                if not _stocks_paper_fill_enabled():
                    print(f"  [US momentum] BUY {shares} {ticker} REJECTED — "
                          f"no position opened, no DB row recorded")
                    return False
                paper = 1
                print(f"  [US momentum] PAPER-FILL {ticker}: {shares} @ ${price:.2f} — "
                      f"Saxo SIM rejected the order; booked locally")
            else:
                _ok, _fp = _confirm_stock_fill(entry_oid, imap[ticker]["uic"])
                if _ok:
                    if _fp > 0 and abs(_fp - price) / max(price, 1e-9) > 0.001:
                        print(f"  [US momentum] {ticker} real fill ${_fp:.2f} "
                              f"(scan ${price:.2f})")
                    price = _fp or price
                else:
                    for _o in (entry_oid, stop_oid):
                        try:
                            _o and saxo_client.cancel_order(str(_o), env=_sx())
                        except Exception:
                            pass
                    if _stocks_paper_fill_enabled():
                        paper = 1
                        print(f"  [US momentum] {ticker}: entry {entry_oid} accepted but "
                              f"unfilled — cancelled, booking paper (no phantom row)")
                    else:
                        print(f"  [US momentum] BUY {ticker} entry {entry_oid} unfilled — "
                              f"cancelled, no DB row recorded")
                        return False
        else:
            cur_is_paper = bool(cur_trade and cur_trade.get("paper"))
            if not cur_is_paper:
                saxo_client.place_market_order(imap[ticker]["uic"], "Stock", side, shares, env=_sx())
            # paper position: no Saxo counterpart -- DB close happens below
    except Exception as e:
        print(f"  [US momentum] {side} {shares} {ticker} FAILED: {e}")
        return False
    rate = _rate_to_sek("USD")
    price_sek = (price or 0) * rate
    # Commission: the LIVE stocks sleeve uses a per-share US-stock schedule
    # (stocks_live_commission_sek); SIM keeps the legacy value-based model.
    comm = (stocks_live_commission_sek(shares, price, rate) if _sx() == "live"
            else commission_sek(shares, price_sek))
    if side == "Buy":
        db.insert_trade({
            "strategy": "US Blend", "market_group": "US Equities", "ticker": ticker,
            "direction": "BUY", "entry_date": date.today().isoformat(),
            "entry_price": price, "shares": shares, "commission_sek": comm,
            "entry_score": 0, "d1_trend": 0, "d2_momentum": 0, "d3_breakout": 0,
            "d4_mean_revert": 0, "d5_volume": 0, "d6_smart_money": 0,
            "d7_mom_quality": 0, "d8_regime": 0,
            "stop_price": round(price * (1 - US_BLEND_STOP_PCT), 2),
            "trailing_stop_high": price, "regime_at_entry": "momentum",
            "paper": paper,
            "stop_order_id": (stop_oid if not paper else None),
        })
        record_fill(-(shares * price_sek + comm))
        _append_trade_log("US Blend", "BUY", ticker, shares, price,
                          shares * price_sek, None, "US momentum rebalance")
        _ae = "live_stocks" if _sx() == "live" else ("ai_sim" if _paper_twin else "sim")
        if (ai_config is not None and ai_config.stocks_enabled(_ae)
                and ai_stock_cards is not None):
            try:
                _blend_stop = round(price * (1 - US_BLEND_STOP_PCT), 2)
                ai_stock_cards.log_stock_entry_card(
                    strategy="us_blend", ticker=ticker, direction="Buy",
                    entry_price=price, shares=shares, stop_price=_blend_stop,
                    sek_per_eur=_sek_per_eur(), entry_date=date.today().isoformat(),
                    risk_sek=abs(price - _blend_stop) * shares * rate,
                    account_env=_ae,
                )
            except Exception as _exc:
                print(f"  [ai] blend entry-card hook failed for {ticker}: {_exc}")
        todays_actions.append({"action": "BUY", "ticker": ticker, "market_group": "US Equities",
                               "strategy": "US Blend", "score": 0, "shares": shares,
                               "price": price, "reason": "US momentum", "pnl_sek": None})
        if not _paper_twin:   # AI SIM twin: no per-trade email (paper A/B, watched on ai_dashboard.py)
            notifier.notify_trade_executed(
                side="BUY", ticker=ticker, shares=shares, price_usd=price,
                value_sek=shares * price_sek, strategy="US Blend",
                account_balance_sek=get_total_equity(db.get_open_trades()),
                reason=("[PAPER-FILL — Saxo SIM down] " if paper else "") + "Weekly momentum rebalance",
            )
    else:  # Sell (full close of the tracked position)
        pnl = None
        if cur_trade:
            entry_sek = cur_trade.get("entry_price", price) * rate
            pnl = shares * (price_sek - entry_sek) - comm
            db.close_trade(cur_trade["id"], price, "momentum_rebalance", pnl, comm)
        record_fill(shares * price_sek - comm)
        _append_trade_log("US Blend", "SELL", ticker, shares, price,
                          shares * price_sek, pnl, "US momentum exit",
                          entry_date=cur_trade.get("entry_date", "") if cur_trade else "")
        _blend_entry_d = cur_trade.get("entry_date", "") if cur_trade else ""
        if (ai_config is not None and ai_config.stocks_enabled()
                and ai_stock_cards is not None and cur_trade and _blend_entry_d):
            try:
                _ep = cur_trade.get("entry_price", price)
                _sp = cur_trade.get("stop_price")
                ai_stock_cards.log_stock_exit_card(
                    card_id=ai_stock_cards.card_id_for("us_blend", ticker, _blend_entry_d),
                    exit_price=price, exit_reason="momentum_rebalance",
                    gross_pnl_sek=shares * (price_sek - _ep * rate),
                    commission_sek=comm, net_pnl_sek=pnl, holding_hours=None,
                    sek_per_eur=_sek_per_eur(),
                    risk_sek=(abs(_ep - _sp) * shares * rate) if _sp else None,
                )
            except Exception as _exc:
                print(f"  [ai] blend exit-card hook failed for {ticker}: {_exc}")
        todays_actions.append({"action": "EXIT", "ticker": ticker, "market_group": "US Equities",
                               "strategy": "US Blend", "score": 0, "shares": shares,
                               "price": price, "reason": "US momentum exit", "pnl_sek": pnl})
        if not _paper_twin:
            notifier.notify_trade_executed(
                side="SELL", ticker=ticker, shares=shares, price_usd=price,
                value_sek=shares * price_sek, strategy="US Blend",
                account_balance_sek=get_total_equity(db.get_open_trades()),
                pnl_sek=pnl, reason="Weekly momentum rebalance exit",
            )
    return True


US_BLEND_LIVE_WOULD_BE_ORDERS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "us_blend_live_would_be_orders.jsonl")


def run_us_momentum(feat_data: dict, open_trades: list, todays_actions: list,
                    dry_run: bool = False, available_cash_sek: float = 0.0,
                    account_env: str = "sim", observe: bool = False,
                    exits_only: bool = False):
    """Validated US cross-sectional momentum, executed as a monthly rebalance with a
    daily market risk-off overlay. See atos/us_momentum.py + STRATEGY_NOTES.md.
    available_cash_sek: if >0, overrides the fixed sleeve and uses this as the rebalance budget.
    dry_run=True previews the orders (prints them) without placing any or touching the DB.

    account_env: "sim" (default -- byte-identical to the pre-2026-09-02 behaviour)
      or "live_stocks" (the real-money US Blend sleeve, driven by atos_live_stocks.py,
      which has already called set_stocks_env("live") and set ATOS_DB_PATH).
    observe=True: Phase-1 dry-run for the LIVE stocks sleeve -- the AI basket-ranker
      and notify_blend_targets hooks still fire, and every would-be order is appended
      to data/us_blend_live_would_be_orders.jsonl, but NO real order is placed, NO DB
      row is written, and last_rebalance is NOT stamped. Distinct from dry_run (which
      is silent -- no hooks, no JSONL)."""
    from atos import us_momentum as USM
    from instrument_map import load_instrument_map, MAP_FILE_LIVE
    # In observe mode nothing may mutate the ledger / broker / rebalance clock.
    _mutate = not dry_run and not observe
    _live_stocks = account_env == "live_stocks"
    if kill_switch_active():
        print("  [US momentum] STOP_TRADING present — skip"); return
    try:
        if _live_stocks:
            # Real-money sleeve: LIVE Uics (differ from SIM), USD-only.
            imap = load_instrument_map(path=MAP_FILE_LIVE, require_usd=True)
        else:
            imap = load_instrument_map()
    except Exception as e:
        print(f"  [US momentum] instrument_map load failed: {e}"); return

    # Blend's working set is ITS OWN positions only. Previously this keyed off
    # market_group == "US Equities", which also swept in every US Reversion
    # position (same market_group, different strategy) -- so the delta
    # rebalance below saw Reversion's dip-buys as Blend holdings that weren't
    # in the momentum/low-vol target and emitted Sell orders for them.
    # _place_us("Sell", cur_trade=<reversion row>) then closed the Reversion
    # DB row tagged "momentum_rebalance"; the next Reversion scan re-bought the
    # freed slot -> a buy/sell churn loop at the same price, pure commission +
    # spread bleed (DELL/MTB, 2026-09-01/02). Reversion already scopes its own
    # set by strategy (run_us_reversion); Blend now matches.
    us_open = {t["ticker"]: t for t in open_trades if t.get("strategy") == "US Blend"}
    rev_held = {t["ticker"] for t in open_trades if t.get("strategy") == "US Reversion"}
    tag = "[US momentum DRY-RUN]" if dry_run else "[US momentum]"

    # ── Reconcile DB open positions against the real Saxo account ─────────────
    # A DB row can go stale (e.g. a sell attempted against a position that had
    # already been closed by an earlier same-day run) and linger with no exit_date
    # even though Saxo no longer holds it. Trading against a stale row makes the
    # next sell fail with "NotOwned" and then opens a fresh duplicate position for
    # the same ticker instead of just holding it. Reconcile first so us_open only
    # contains what's actually held.
    if _mutate and us_open:
        try:
            broker_positions = saxo_client.get_positions(env=_sx())
            held_uics = set()
            for p in broker_positions.get("Data", []):
                base = p.get("PositionBase", {})
                uic = base.get("Uic")
                amount = base.get("Amount", 0)
                if uic is not None and amount:
                    held_uics.add(uic)
            for tk in list(us_open.keys()):
                if us_open[tk].get("paper"):
                    continue   # locally-simulated fill -- no Saxo position to reconcile against
                info = imap.get(tk)
                uic = info.get("uic") if info else None
                if uic is None or uic not in held_uics:
                    tr = us_open.pop(tk)
                    print(f"  {tag} RECONCILE: {tk} (trade id {tr['id']}) is open in the DB "
                          f"but not held at Saxo — closing as stale, no P&L (manual review advised)")
                    db.close_trade(tr["id"], tr.get("entry_price", 0), "reconciled_not_owned", 0.0, 0.0)
        except Exception as e:
            print(f"  {tag} RECONCILE skipped — could not fetch Saxo positions: {e}")

    tgt = USM.compute_targets(feat_data, US_TICKERS)   # US names only — not the whole universe
    print(f"  {tag} risk_off={tgt['risk_off']} | {tgt.get('reason')} | targets={tgt['targets']}")
    fx_usd = _rate_to_sek("USD")

    # observe (LIVE Phase 1): fire the signal/notify/AI-shadow hooks, but not the
    # order/DB mutations below. dry_run: stay fully silent.
    if not dry_run or observe:
        global _blend_signal
        _blend_signal = {
            "targets":  tgt.get("targets", []),
            "risk_off": tgt.get("risk_off", False),
            "reason":   tgt.get("reason", ""),
            "momentum": tgt.get("momentum", []),
            "lowvol":   tgt.get("lowvol", []),
        }
        if account_env != "ai_sim":   # the AI paper twin stays silent -- watched on ai_dashboard.py
            notifier.notify_blend_targets(
                targets          = tgt.get("targets", []),
                risk_off         = tgt.get("risk_off", False),
                reason           = tgt.get("reason", ""),
                momentum_tickers = tgt.get("momentum", []),
                lowvol_tickers   = tgt.get("lowvol", []),
                sleeve_sek       = available_cash_sek or CAP.blend_allocation_pct() * CAP.starting_capital_sek(),
            )

        # ── AI shadow basket ranker (OBSERVE/LOG ONLY) ──────────────────────
        # Logs what the LLM WOULD do to the offense (momentum) basket next to
        # `tgt`. `tgt` is NOT modified -- plan_rebalance() below gets the
        # deterministic pick unchanged. Report: report_ai_basket.py. The
        # deterministic re-ranking rule (if any) comes out of the user's
        # review of this log, then a backtest -- never from this hook.
        if (ai_config is not None and ai_basket_ranker is not None
                and ai_config.stocks_basket_ranker_enabled(account_env)
                and tgt.get("momentum")):
            try:
                _mom = tgt.get("momentum", [])
                _regime = None
                try:
                    from ai.regime.classifier import classify_regime
                    _lead_bars = feat_data.get(_mom[0])
                    if _lead_bars is not None:
                        _regime = classify_regime(_lead_bars).get("label")
                except Exception:
                    pass
                try:
                    _bstate = _blend_book_state()
                except Exception:
                    _bstate = {}
                _rk = ai_basket_ranker.rank_basket_shadow(
                    account_env=account_env,
                    det_offense=_mom, det_defense=tgt.get("lowvol", []),
                    det_count=len(_mom), detail=tgt.get("detail", {}),
                    regime_label=_regime, mom_n_max=USM.MOM_N_MAX,
                    as_of_date=date.today().isoformat(),
                    book_state=_bstate,
                )
                # ai_sim twin: TRADE the AI's re-ranked pick instead of the
                # deterministic momentum names. Everywhere else this is
                # shadow-log only (the ranker's row is logged, tgt untouched).
                if (ai_config is not None and _rk
                        and ai_config.basket_ranker_applies(account_env)):
                    _ai_off = list(_rk.get("ai_offense") or _mom)
                    if _ai_off and _ai_off != _mom:
                        print(f"  {tag} AI basket: {_mom} -> {_ai_off} "
                              f"(conf={_rk.get('confidence')}, {_rk.get('reasoning','')[:80]})")
                    tgt["momentum"] = _ai_off
            except Exception as _exc:
                print(f"  [ai] blend basket-ranker hook failed: {_exc}")

    def _price(tk, fallback=0):
        return float(feat_data[tk]["Close"].iloc[-1]) if tk in feat_data else fallback

    def _observe_order(side, tk, shares, price, cur_trade=None):
        """LIVE Phase 1: record the would-be order + (for buys) an AI entry card.
        Places nothing, writes no DB row."""
        rec = {
            "ts": datetime.now().isoformat(),
            "side": side, "ticker": tk, "shares": int(shares),
            "price_usd": round(float(price), 4),
            "notional_sek": round(shares * price * fx_usd, 2),
            "budget_sek": round(float(available_cash_sek or 0.0), 2),
            "strategy": "US Blend", "account_env": account_env,
        }
        try:
            os.makedirs(os.path.dirname(US_BLEND_LIVE_WOULD_BE_ORDERS), exist_ok=True)
            with open(US_BLEND_LIVE_WOULD_BE_ORDERS, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as _exc:
            print(f"  {tag} could not append would-be order for {tk}: {_exc}")
        print(f"    {tag} OBSERVE would {side.upper()} {shares} {tk} @ ${price:.2f}  "
              f"(~{shares*price*fx_usd:,.0f} SEK)")
        if side == "Buy" and ai_config is not None and ai_stock_cards is not None:
            try:
                _stop = round(price * (1 - US_BLEND_STOP_PCT), 2)
                ai_stock_cards.log_stock_entry_card(
                    strategy="us_blend", ticker=tk, direction="Buy",
                    entry_price=price, shares=int(shares), stop_price=_stop,
                    sek_per_eur=_sek_per_eur(), entry_date=date.today().isoformat(),
                    risk_sek=abs(price - _stop) * shares * fx_usd,
                    account_env=account_env,
                )
            except Exception as _exc:
                print(f"  [ai] observe entry-card hook failed for {tk}: {_exc}")
        # Surface the would-be order the same way a real fill would, so the
        # count + the LIVE stocks dashboard's scan-signal panel see it.
        todays_actions.append({
            "action": "BUY" if side == "Buy" else "SELL",
            "ticker": tk, "market_group": "US Equities", "strategy": "US Blend",
            "score": 0, "shares": int(shares), "price": round(float(price), 4),
            "reason": ("fortnightly rebalance — enter" if side == "Buy"
                       else "dropped from target basket"),
            "pnl_sek": None, "would_be": True,
        })
        return True

    def _do(side, tk, shares, price, cur_trade=None):
        if dry_run:
            print(f"    {tag} would {side.upper()} {shares} {tk} @ ${price:.2f}  (~{shares*price*fx_usd:,.0f} SEK)")
            return True
        if observe:
            return _observe_order(side, tk, shares, price, cur_trade=cur_trade)
        return _place_us(side, tk, shares, imap, todays_actions, price=price,
                         cur_trade=cur_trade, strategy="US Blend", account_env=account_env)

    def _sell_all_us():
        for tk, tr in us_open.items():
            _do("Sell", tk, tr.get("shares", 0),
                _price(tk, tr.get("entry_price", 0)), cur_trade=tr)

    # US sleeve capital. If available_cash_sek is provided (dynamic mode), the
    # rebalance budget is that value directly — position sizes scale with the
    # full account. Otherwise falls back to the compounding fixed sleeve.
    state     = _load_us_state()
    us_value  = sum((tr.get("shares", 0) or 0) * _price(tk, tr.get("entry_price", 0)) * fx_usd
                    for tk, tr in us_open.items())
    if available_cash_sek > 0:
        sleeve_equity = available_cash_sek   # dynamic: 50% of live SIM cash
        print(f"  {tag} dynamic budget: {sleeve_equity:,.0f} SEK (open positions: ~{us_value:,.0f} SEK)")
    else:
        sleeve_cash   = float(state.get("sleeve_cash", USM.US_SLEEVE_SEK))
        sleeve_equity = sleeve_cash + us_value   # classic compounding sleeve

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
            if _mutate and event_sell_value > 0:
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
        if _mutate:
            state["sleeve_cash"] = sleeve_equity   # value parked in cash until re-entry
            _save_us_state(state)
        return

    # exits_only: the corp-event + risk-off overlays above still run (they can
    # only ever REDUCE exposure), but no new rebalance / buys. Used by the LIVE
    # stocks Exit Check backstop and when the daily-loss cap is breached.
    if exits_only:
        print(f"  {tag} exits-only — overlays ran, skipping the rebalance/buy path")
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
    # Blend priority: momentum names (offense) first, then low-vol (defense), deduped.
    # Exclude tickers with imminent ex-dividend or earnings from new buys.
    # We check only the rebalance candidates (not the full 61-stock universe).
    corp_skip = corp_avoid(mom_names + lv_names)
    if corp_skip:
        print(f"  {tag} skipping {sorted(corp_skip)} — imminent corporate event (buy next rebalance)")

    priority = []
    for tk in mom_names + lv_names:
        if tk in corp_skip:
            continue
        if tk in rev_held:
            # US Reversion already holds this name -- don't open a second,
            # independently-managed Blend position in the same ticker.
            print(f"  {tag} skipping {tk} — held by US Reversion (no duplicate position)")
            continue
        if tk not in priority and tk in feat_data and tk in imap and _price(tk) > 0:
            priority.append(tk)

    # Delta rebalance, NOT liquidate-and-rebuy: a ticker that stays in the target
    # list across two rebalances should just be left alone. plan_rebalance() only
    # sells names that dropped out of the target (or moved >10% off target) and
    # only buys/trims what's actually changed — avoids paying commission/spread
    # to sell and immediately rebuy the same position.
    current_shares = {tk: int(tr.get("shares", 0) or 0) for tk, tr in us_open.items()}
    prices_usd = {tk: _price(tk) for tk in set(priority) | set(current_shares)}
    actions = USM.plan_rebalance(current_shares, priority, 1.0, prices_usd, sleeve_equity, fx_usd)

    if not actions:
        print(f"  {tag} REBALANCE (blend) — {len(mom_names)} momentum {mom_names} + "
              f"{len(lv_names)} low-vol {lv_names} — holdings already match target, no trades needed "
              f"| sleeve ~{sleeve_equity:,.0f} SEK")
        no_action_tickers = set(priority)
        if _mutate:
            state["last_rebalance"] = date.today().isoformat()
            state["sleeve_cash"] = sleeve_equity - us_value
            _save_us_state(state)
    else:
        print(f"  {tag} REBALANCE (blend) — {len(mom_names)} momentum {mom_names} + "
              f"{len(lv_names)} low-vol {lv_names} | budget {sleeve_equity:,.0f} SEK "
              f"(started {USM.US_SLEEVE_SEK:,.0f}, compounds with P&L)")
        sells = [a for a in actions if a["side"] == "Sell"]
        buys  = [a for a in actions if a["side"] == "Buy"]
        deployed_sek = 0.0
        freed_sek    = 0.0
        filled_any   = False
        for a in sells:
            tk = a["ticker"]
            tr = us_open.get(tk)
            px = _price(tk, tr.get("entry_price", 0) if tr else 0)
            if _do("Sell", tk, a["shares"], px, cur_trade=tr):
                freed_sek  += a["shares"] * px * fx_usd
                filled_any  = True
        for a in buys:
            tk = a["ticker"]
            px = _price(tk)
            if px <= 0:
                continue
            if _do("Buy", tk, a["shares"], px):
                deployed_sek += a["shares"] * px * fx_usd
                filled_any    = True
        print(f"  {tag} rebalanced ({len(sells)} sell / {len(buys)} buy) — "
              f"freed ~{freed_sek:,.0f} SEK, deployed ~{deployed_sek:,.0f} SEK "
              f"of {sleeve_equity:,.0f} SEK sleeve; unchanged positions left in place")
        no_action_tickers = {tk for tk in priority if tk not in {a["ticker"] for a in actions}}
        if _mutate:
            # Only stamp last_rebalance once at least one order actually landed.
            # If the market is closed (holiday) Saxo rejects every order and
            # filled_any stays False — retry next trading day.
            if filled_any:
                state["last_rebalance"] = date.today().isoformat()
            else:
                print(f"  {tag} WARNING: 0 orders filled — market may be closed. "
                      f"Rebalance will retry tomorrow (last_rebalance unchanged).")
            state["sleeve_cash"] = (sleeve_equity - us_value) + freed_sek - deployed_sek
            _save_us_state(state)

    # ── Log rebalance signals (DB write -- SIM only) ────────────────────────
    if _mutate:
        _mom_scan_ts = datetime.now().isoformat()
        for tk in priority:
            _placed = tk in {a["ticker"] for a in todays_actions if a.get("action") == "BUY"}
            if _placed:
                _block_reason = None
            elif tk in no_action_tickers:
                _block_reason = "already_at_target"
            else:
                _block_reason = "order_failed"
            try:
                db.insert_signal({
                    "signal_date": date.today().isoformat(), "scan_ts": _mom_scan_ts,
                    "strategy": "US Blend", "market_group": "US Equities",
                    "ticker": tk, "final_score": 0,
                    "action": "BUY", "executed": 1 if _placed else 0,
                    "block_reason": _block_reason,
                    "d1_trend": 0, "d2_momentum": 0, "d3_breakout": 0,
                    "d4_mean_revert": 0, "d5_volume": 0,
                    "d6_smart_money": 0, "d7_mom_quality": 0, "d8_regime": 0,
                    "regime": "momentum",
                })
            except Exception:
                pass


def run_us_blend_live(*, budget_sek: float, dry_run: bool, exits_only: bool = False) -> dict:
    """Real-money US Blend sleeve entry point (atos_live_stocks.py only).

    Runs ONLY run_us_momentum with account_env="live_stocks" -- never
    run_us_reversion, never the legacy per-market engine, never the SIM
    dashboard / learning pass. The caller (atos_live_stocks.py) has already:
      * proc_lock.acquire(ATOS_LIVE_STOCKS_LOCK)
      * set ATOS_DB_PATH / ATOS_RISK_STATE_FILE / ATOS_US_MOMENTUM_STATE
      * atos_runner.set_stocks_env("live")
      * verified SAXO_LIVE_STOCKS_CONFIRMED etc.

    dry_run=True  -> observe=True inside run_us_momentum: AI hooks fire, every
                     would-be order is logged to us_blend_live_would_be_orders
                     .jsonl, but no real order / no DB row / no rebalance stamp.
    exits_only=True -> risk-off is forced (sell-all path only); no new buys.
    Returns {"actions": [...], "buy": n, "sell": n, "signal": {...}} -- `signal`
    is the blend target basket (targets / risk_off / reason / momentum / lowvol)
    from compute_targets(), for the LIVE stocks dashboard's scan-signal panel."""
    assert _sx() == "live", "run_us_blend_live requires set_stocks_env('live') first"
    global _blend_signal
    _blend_signal = {}
    db.init_db()
    todays_actions: list = []

    raw = download_universe(list(US_TICKERS))
    feat_data: dict = {}
    for tk, dfr in raw.items():
        try:
            feat_data[tk] = add_all(dfr)
        except Exception as e:
            print(f"  [live stocks] features failed for {tk}: {e}")
    if not feat_data:
        print("  [live stocks] no market data — aborting (no orders, no state change)")
        return {"actions": [], "buy": 0, "sell": 0, "signal": {}}

    open_trades = db.get_open_trades()
    try:
        run_us_momentum(feat_data, open_trades, todays_actions,
                        available_cash_sek=max(0.0, float(budget_sek)),
                        account_env="live_stocks", observe=dry_run,
                        exits_only=exits_only)
    except Exception as e:
        print(f"  [live stocks] run_us_momentum error: {e}")

    buy_n  = sum(1 for a in todays_actions if a.get("action") == "BUY")
    sell_n = sum(1 for a in todays_actions if a.get("action") in ("SELL", "EXIT"))
    try:
        _bstate = _blend_book_state()
    except Exception:
        _bstate = {}
    return {"actions": todays_actions, "buy": buy_n, "sell": sell_n,
            "signal": dict(_blend_signal), "book_state": _bstate}


def run_us_blend_ai(*, budget_sek: float) -> dict:
    """AI-decision stocks twin entry point (atos_ai_stocks.py only).

    Runs ONLY run_us_momentum(account_env="ai_sim") -- a real SIM PAPER
    rebalance (observe=False; _STOCKS_ENV stays "sim" so orders paper-fill
    and book to data/atos_ai.db). The basket-ranker's re-ranked pick is
    swapped in for the deterministic momentum names inside run_us_momentum
    (ai_config.basket_ranker_applies("ai_sim")). Never run_us_reversion /
    legacy / dashboard. Returns the same dict shape as run_us_blend_live."""
    global _blend_signal
    _blend_signal = {}
    db.init_db()
    todays_actions: list = []

    raw = download_universe(list(US_TICKERS))
    feat_data: dict = {}
    for tk, dfr in raw.items():
        try:
            feat_data[tk] = add_all(dfr)
        except Exception as e:
            print(f"  [ai stocks] features failed for {tk}: {e}")
    if not feat_data:
        print("  [ai stocks] no market data — aborting")
        return {"actions": [], "buy": 0, "sell": 0, "signal": {}, "book_state": {}}

    try:
        run_us_momentum(feat_data, db.get_open_trades(), todays_actions,
                        available_cash_sek=max(0.0, float(budget_sek)),
                        account_env="ai_sim")
    except Exception as e:
        print(f"  [ai stocks] run_us_momentum error: {e}")

    buy_n  = sum(1 for a in todays_actions if a.get("action") == "BUY")
    sell_n = sum(1 for a in todays_actions if a.get("action") in ("SELL", "EXIT"))
    try:
        _bstate = _blend_book_state()
    except Exception:
        _bstate = {}
    return {"actions": todays_actions, "buy": buy_n, "sell": sell_n,
            "signal": dict(_blend_signal), "book_state": _bstate}


def run_us_reversion(feat_data: dict, open_trades: list, todays_actions: list,
                     available_cash_sek: float = 0.0):
    """US Mean Reversion — short-term dip-buying strategy (3-10 day holds).

    Completely independent of US Blend: separate DB rows (strategy='US Reversion'),
    separate position limit (MAX_POSITIONS=2).
    available_cash_sek: if >0, slot size = this / MAX_POSITIONS (dynamic mode).
    Otherwise uses fixed REVERSION_SLEEVE_SEK.

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
    fx_usd = _rate_to_sek("USD")

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
            is_paper = bool(trade.get("paper"))
            print(f"  {tag} EXIT {ticker}: {reason}{' [PAPER]' if is_paper else ''}")
            uic = imap.get(ticker, {}).get("uic")
            if (uic and sh > 0) or (is_paper and sh > 0):
                try:
                    if not is_paper:
                        saxo_client.place_market_order(uic, "Stock", "Sell", sh)
                    # paper position: no Saxo counterpart to sell -- just close
                    # the DB row at the current price, same as forex paper exits
                    comm_exit = commission_sek(sh, sh * cur_price * fx_usd)
                    pnl_sek = (cur_price - trade.get("entry_price", 0)) * sh * fx_usd - comm_exit
                    db.close_trade(trade["id"], exit_price=cur_price,
                                   exit_reason=reason, pnl_sek=pnl_sek,
                                   commission_sek=comm_exit)
                    entry_d = trade.get("entry_date", "")
                    held_d  = (today - _date.fromisoformat(entry_d)).days if entry_d else 0
                    _append_trade_log(
                        "US Reversion", "SELL", ticker, sh, cur_price,
                        sh * cur_price * fx_usd, pnl_sek, reason,
                        entry_date=entry_d, days_held=held_d,
                    )
                    if (ai_config is not None and ai_config.stocks_enabled()
                            and ai_stock_cards is not None and entry_d):
                        try:
                            _ep = trade.get("entry_price", 0)
                            _sp = trade.get("stop_price")
                            ai_stock_cards.log_stock_exit_card(
                                card_id=ai_stock_cards.card_id_for("us_reversion", ticker, entry_d),
                                exit_price=cur_price, exit_reason=reason,
                                gross_pnl_sek=(cur_price - _ep) * sh * fx_usd,
                                commission_sek=comm_exit,
                                net_pnl_sek=pnl_sek,
                                holding_hours=held_d * 24.0 if held_d else None,
                                sek_per_eur=_sek_per_eur(),
                                risk_sek=(abs(_ep - _sp) * sh * fx_usd) if _sp else None,
                            )
                        except Exception as _exc:
                            print(f"  [ai] reversion exit-card hook failed for {ticker}: {_exc}")
                    todays_actions.append({
                        "action": "SELL", "ticker": ticker, "market_group": "US Equities",
                        "strategy": "US Reversion", "score": 0, "shares": sh,
                        "price": cur_price, "reason": f"reversion exit: {reason}",
                        "pnl_sek": pnl_sek,
                    })
                    notifier.notify_reversion_exit(
                        ticker=ticker, pnl_sek=pnl_sek,
                        reason=reason, hold_days=held_d,
                        account_balance_sek=get_total_equity(db.get_open_trades()),
                    )
                except Exception as e:
                    print(f"  {tag} sell {ticker} FAILED: {e}")

    # ── Max positions: percentage of universe, clamped both ends ─────
    # min_slots <= round(universe × max_universe_pct) <= max_slots.
    # The max_slots ceiling matters: the universe grew 61 -> 385, and an
    # unclamped 10% would give 38 slots against the 2-3 this strategy was
    # actually validated at, shrinking each slot below the share price of
    # 13% of the universe. See capital_config.reversion_slots().
    max_positions = CAP.reversion_slots(len(US_TICKERS))

    # ── Entry scan — only if slots are available ───────────────────
    # Re-read open trades after exits (some may have just been closed)
    rev_open_now = {t["ticker"]: t for t in db.get_open_trades()
                    if t.get("strategy") == "US Reversion"}
    slots_free = max_positions - len(rev_open_now)
    if slots_free <= 0:
        print(f"  {tag} full ({max_positions}/{max_positions} positions, "
              f"{USR.MAX_UNIVERSE_PCT*100:.0f}% of {len(US_TICKERS)}-stock universe)")
        return

    # ── Sleeve size: dynamic (% of SIM cash) or fixed fallback ───────
    sleeve_base = available_cash_sek if available_cash_sek > 0 else USR.REVERSION_SLEEVE_SEK
    slot_sek    = sleeve_base / max_positions

    # ── Sleeve DD cap check (mirrors backtest logic) ───────────────
    open_value_sek = sum(
        (t.get("shares", 0) or 0) * _price(tk) * fx_usd
        for tk, t in rev_open_now.items()
    )
    open_cost_sek = sum(
        (t.get("shares", 0) or 0) * (t.get("entry_price", 0) or 0) * fx_usd
        for t in rev_open_now.values()
    )
    sleeve_equity = (sleeve_base - open_cost_sek) + open_value_sek
    sleeve_dd = (sleeve_base - sleeve_equity) / sleeve_base if sleeve_base > 0 else 0
    if sleeve_dd >= USR.SLEEVE_DD_CAP:
        print(f"  {tag} sleeve DD {sleeve_dd*100:.1f}% >= cap {USR.SLEEVE_DD_CAP*100:.0f}% "
              f"— no new entries (sleeve ~{sleeve_equity:,.0f} SEK)")
        return

    blend_held = {t["ticker"] for t in db.get_open_trades()
                  if t.get("strategy") == "US Blend"}
    candidates = USR.scan(feat_data, US_TICKERS)
    candidates = [c for c in candidates
                  if c["ticker"] not in rev_open_now
                  and c["ticker"] not in blend_held]   # no duplicate cross-strategy position
    if not candidates:
        print(f"  {tag} no entry signals today")
        return

    print(f"  {tag} {len(candidates)} signal(s) | {slots_free} slot(s) free of {max_positions} "
          f"({USR.MAX_UNIVERSE_PCT*100:.0f}% of {len(US_TICKERS)}) | "
          f"slot: {slot_sek:,.0f} SEK each")

    global _rev_signals
    _rev_signals = list(candidates)
    notifier.notify_reversion_signal(
        candidates = candidates,
        slots_free = slots_free,
        sleeve_sek = slot_sek * max_positions,
    )

    scan_ts_str    = datetime.now().isoformat()
    ordered_tickers = set()

    # ── AI shadow Copilot on US Reversion entries (OBSERVE/LOG ONLY) ──────
    # Mirrors forex/runner.py's _run_entries hook: build a proposal in the
    # forex schema, log it, and (when the paid agent is on + not already
    # evaluated today) score it and log the shadow decision. NOTHING here
    # changes which candidates are entered, the size, or the order -- there
    # is no apply path. can_apply_decision is never consulted.
    if (ai_config is not None and ai_stock_proposal is not None
            and ai_config.stocks_enabled()):
        try:
            _spe = _sek_per_eur()
            _equity_sek = get_total_equity(db.get_open_trades())
            _equity_eur = (_equity_sek / _spe) if (_equity_sek and _spe) else None
            _open_rev = [{"symbol": t, "side": "BUY", "size": v.get("shares"),
                          "strategy": "us_reversion"}
                         for t, v in rev_open_now.items()]
            for _c in candidates[:slots_free]:
                _tk = _c["ticker"]
                _bars = feat_data.get(_tk)
                _vol_pct = None
                try:
                    _cl = _bars["Close"].dropna()
                    _vol_pct = round(float(_cl.pct_change().rolling(20).std().iloc[-1]) * 100, 3)
                except Exception:
                    pass
                _stop = round(_c["price"] * (1 - USR.STOP_PCT), 2)
                _sh = _sim_cap_shares(int(slot_sek / (_c["price"] * fx_usd)), _c["price"], fx_usd)
                _risk_eur = ((_c["price"] - _stop) * _sh * fx_usd / _spe) if _spe else None
                _prop = ai_stock_proposal.build_stock_proposal(
                    strategy="us_reversion", ticker=_tk, entry_price=_c["price"],
                    stop_price=_stop, target_price=_c.get("sma20"),
                    rsi14=_c.get("rsi"), shares=_sh,
                    daily_vol_pct=_vol_pct, risk_eur=_risk_eur,
                    account_equity_eur=_equity_eur, open_positions=_open_rev,
                    regime_bars=_bars,
                )
                if not _prop:
                    continue
                ai_stock_proposal.log_proposal(_prop)
                if (ai_config.stocks_reversion_copilot_enabled()
                        and ai_trading_copilot is not None):
                    if not (ai_config.agent_dedup_enabled()
                            and ai_stock_proposal.already_evaluated(_prop)):
                        _dec = ai_trading_copilot.evaluate_proposal(_prop)
                        ai_stock_proposal.log_shadow_decision(
                            _prop, _dec, entered=_tk in {c["ticker"] for c in candidates[:slots_free]})
        except Exception as _exc:
            print(f"  [ai] reversion shadow-Copilot hook failed: {_exc}")

    for cand in candidates[:slots_free]:
        ticker = cand["ticker"]
        price  = cand["price"]
        uic_data = imap.get(ticker, {})
        uic = uic_data.get("uic")
        if not uic:
            print(f"  {tag} {ticker}: no UIC in instrument_map — skip")
            continue

        shares = _sim_cap_shares(int(slot_sek / (price * fx_usd)), price, fx_usd)
        if shares < 1:
            print(f"  {tag} {ticker}: slot too small for 1 share — skip")
            continue

        cost_sek = shares * price * fx_usd
        print(f"  {tag} BUY {ticker}: RSI={cand['rsi']} dip={cand['dip_pct']}% "
              f"vol={cand['vol_ratio']}x | {shares} shares @ ${price:.2f} "
              f"(~{cost_sek:,.0f} SEK) [US Reversion sleeve]")
        try:
            # This used to call the nonexistent saxo_client.place_order() --
            # confirmed live 2026-08-21: the strategy's first-ever real
            # candidate (ROST) failed with "module 'saxo_client' has no
            # attribute 'place_order'". Fixed to the real function, and
            # attached an atomic broker-side stop-loss the same way US Blend
            # was fixed earlier this session -- this path previously
            # hardcoded stop_price=0 in the DB record, meaning even a
            # successful order would have sat with no protection at all.
            stop_p = round(price * (1 - USR.STOP_PCT), 2)
            entry_oid, stop_oid, _ = saxo_order.place_with_stop(
                post_fn=saxo_client.post,
                account_key=saxo_client.get_account_key(),
                uic=uic, asset_type="Stock", amount=shares,
                buy_sell="Buy", stop_price=stop_p,
                label=f"US Reversion:{ticker}",
            )
            paper = 0
            if entry_oid is None:
                if not _stocks_paper_fill_enabled():
                    print(f"  {tag} buy {ticker} REJECTED — no position opened, no DB row recorded")
                    continue
                paper = 1
                print(f"  {tag} PAPER-FILL {ticker}: {shares} @ ${price:.2f} — Saxo SIM "
                      f"rejected the order; booked locally, managed by ATOS should_exit() logic")
            else:
                _ok, _fp = _confirm_stock_fill(entry_oid, uic)
                if _ok:
                    if _fp > 0 and abs(_fp - price) / max(price, 1e-9) > 0.001:
                        print(f"  {tag} {ticker} real fill ${_fp:.2f} (scan ${price:.2f})")
                    price = _fp or price
                else:
                    for _o in (entry_oid, stop_oid):
                        try:
                            _o and saxo_client.cancel_order(str(_o))
                        except Exception:
                            pass
                    if not _stocks_paper_fill_enabled():
                        print(f"  {tag} buy {ticker} entry {entry_oid} unfilled — "
                              f"cancelled, no DB row recorded")
                        continue
                    paper = 1
                    print(f"  {tag} {ticker}: entry {entry_oid} accepted but unfilled — "
                          f"cancelled, booking paper (no phantom row)")
            comm = commission_sek(shares, cost_sek)
            db.insert_trade({
                "strategy": "US Reversion", "market_group": "US Equities",
                "ticker": ticker, "direction": "BUY",
                "entry_date": today.isoformat(), "entry_price": price,
                "shares": shares, "commission_sek": comm,
                "entry_score": cand["score"], "d1_trend": 0, "d2_momentum": 0,
                "d3_breakout": 0,
                "d4_mean_revert": cand["rsi"],
                "d5_volume": cand["vol_ratio"],
                "d6_smart_money": 0, "d7_mom_quality": 0, "d8_regime": 0,
                "stop_price": stop_p, "trailing_stop_high": price, "regime_at_entry": "reversion",
                "paper": paper,
                "stop_order_id": (stop_oid if not paper else None),
            })
            ordered_tickers.add(ticker)
            _append_trade_log(
                "US Reversion", "BUY", ticker, shares, price, cost_sek, None,
                f"RSI={cand['rsi']} dip={cand['dip_pct']}% vol={cand['vol_ratio']}x",
            )
            # AI observation card (OBSERVE/LOG only) -- picked up by the Journal
            if ai_config is not None and ai_config.stocks_enabled() and ai_stock_cards is not None:
                try:
                    _spe = _sek_per_eur()
                    ai_stock_cards.log_stock_entry_card(
                        strategy="us_reversion", ticker=ticker, direction="Buy",
                        entry_price=price, shares=shares, stop_price=stop_p,
                        sek_per_eur=_spe, entry_date=today.isoformat(),
                        risk_sek=abs(price - stop_p) * shares * fx_usd,
                        rsi_at_entry=cand.get("rsi"), sma20_target=cand.get("sma20"),
                    )
                except Exception as _exc:
                    print(f"  [ai] reversion entry-card hook failed for {ticker}: {_exc}")
            todays_actions.append({
                "action": "BUY", "ticker": ticker, "market_group": "US Equities",
                "strategy": "US Reversion", "score": cand["score"],
                "shares": shares, "price": price,
                "reason": (f"[US Reversion] RSI {cand['rsi']}, "
                           f"dip {cand['dip_pct']}%, vol {cand['vol_ratio']}x"),
                "pnl_sek": None,
            })
            notifier.notify_trade_executed(
                side="BUY", ticker=ticker, shares=shares, price_usd=price,
                value_sek=cost_sek, strategy="US Reversion",
                account_balance_sek=get_total_equity(db.get_open_trades()),
                reason=(("[PAPER-FILL — Saxo SIM down] " if paper else "")
                        + f"RSI {cand['rsi']:.1f} | Dip {cand['dip_pct']}% | Vol {cand['vol_ratio']}x"),
            )
        except Exception as e:
            print(f"  {tag} buy {ticker} FAILED: {e}")

    # ── Log ALL US tickers to signals table (candidates + skips) ─────────────
    # Candidates that fired: BUY, executed=1 if order placed, 0 if failed/no-UIC
    # Candidates that didn't fit (slots_full): BUY, executed=0, block_reason
    # All others: SKIP, with current RSI / dip / vol for dashboard near-miss view
    cand_tickers = {c["ticker"] for c in candidates}
    attempt_set  = {c["ticker"] for c in candidates[:slots_free]}
    for tk in US_TICKERS:
        if tk in rev_open_now:
            continue
        try:
            if tk in cand_tickers:
                cand = next(c for c in candidates if c["ticker"] == tk)
                if tk in attempt_set:
                    block = None if tk in ordered_tickers else "order_failed"
                    executed = 1 if tk in ordered_tickers else 0
                else:
                    block = "slots_full"
                    executed = 0
                db.insert_signal({
                    "signal_date": today.isoformat(), "scan_ts": scan_ts_str,
                    "strategy": "US Reversion", "market_group": "US Equities",
                    "ticker": tk, "final_score": cand.get("score", 0),
                    "action": "BUY", "executed": executed, "block_reason": block,
                    "rsi": cand.get("rsi"), "dip_pct": cand.get("dip_pct"),
                    "vol_ratio": cand.get("vol_ratio"),
                    "d1_trend": 0, "d2_momentum": 0, "d3_breakout": 0,
                    "d4_mean_revert": cand.get("rsi"), "d5_volume": cand.get("vol_ratio"),
                    "d6_smart_money": 0, "d7_mom_quality": 0, "d8_regime": 0,
                    "regime": "reversion",
                })
            else:
                rsi_s, sma20_s = _rsi_sma20(tk)
                px_s = _price(tk)
                dip_s = (sma20_s - px_s) / sma20_s * 100 if sma20_s and sma20_s > 0 else None
                vr_s = None
                if tk in feat_data and "Volume" in feat_data[tk].columns:
                    vvol = feat_data[tk]["Volume"].dropna()
                    v20m = float(vvol.tail(20).mean()) if len(vvol) >= 20 else 0
                    vr_s = float(vvol.iloc[-1]) / v20m if v20m > 0 else None
                db.insert_signal({
                    "signal_date": today.isoformat(), "scan_ts": scan_ts_str,
                    "strategy": "US Reversion", "market_group": "US Equities",
                    "ticker": tk, "final_score": 0,
                    "action": "SKIP", "executed": 0, "block_reason": "conditions_not_met",
                    "rsi": rsi_s, "dip_pct": dip_s, "vol_ratio": vr_s,
                    "d1_trend": 0, "d2_momentum": 0, "d3_breakout": 0,
                    "d4_mean_revert": rsi_s, "d5_volume": vr_s,
                    "d6_smart_money": 0, "d7_mom_quality": 0, "d8_regime": 0,
                    "regime": "reversion",
                })
        except Exception as _sig_err:
            print(f"  {tag} signal log failed for {tk}: {_sig_err}")


# ── USA Strategy signals — SIM only ───────────────────────────────────────────

def run_us_signals(feat_data: dict, open_trades: list, todays_actions: list) -> None:
    """
    Run the 4 usa_strategy strategies (SMA Crossover, RSI Reversal, Momentum,
    Ensemble) on the US universe. SIM-only — never called from
    atos_live_stocks.py. Core strategies (US Blend, US Reversion) untouched.

    Entry: BUY signal from a strategy; max MAX_POSITIONS_PER_STRATEGY open
           simultaneously per strategy; 1 position per (ticker, strategy) pair.
    Stop:  ATR * 2.0, floor at 6% below entry.
    Exit:  SELL signal from same strategy, hard stop hit, or 30-day time limit.
    Size:  SIGNALS_SLOT_SEK SEK per slot (fixed, SIM only).
    """
    from atos.us_signals import (
        get_entry_signals, should_exit, compute_stop,
        ALL_SIGNAL_STRATEGY_NAMES, MAX_POSITIONS_PER_STRATEGY,
    )
    from instrument_map import load_instrument_map

    SIGNALS_SLOT_SEK = 5_000.0     # SEK per position slot (SIM paper money)

    tag = "[US signals]"
    if kill_switch_active():
        print(f"  {tag} STOP_TRADING present — skip"); return

    try:
        imap = load_instrument_map()
    except Exception as e:
        print(f"  {tag} instrument_map load failed: {e}"); return

    fx_usd = _rate_to_sek("USD")
    today_str = date.today().isoformat()

    # Current open US Signals positions, keyed by (ticker, strategy)
    sig_open: dict[tuple, dict] = {
        (t["ticker"], t["strategy"]): t
        for t in open_trades
        if t.get("strategy") in ALL_SIGNAL_STRATEGY_NAMES
    }

    def _price(tk: str) -> float:
        try:
            return float(feat_data[tk]["Close"].iloc[-1])
        except Exception:
            return 0.0

    # ── 1. Exits ──────────────────────────────────────────────────────────────
    for (ticker, strategy), trade in list(sig_open.items()):
        cur_price = _price(ticker)
        if cur_price <= 0 or ticker not in feat_data:
            continue
        exit_flag, reason = should_exit(trade, feat_data[ticker], cur_price)
        if not exit_flag:
            continue
        sh = trade.get("shares", 0) or 0
        is_paper = bool(trade.get("paper"))
        print(f"  {tag} EXIT {ticker} [{strategy}]: {reason}{' [PAPER]' if is_paper else ''}")
        uic = imap.get(ticker, {}).get("uic")
        try:
            if uic and sh > 0 and not is_paper:
                saxo_client.place_market_order(uic, "Stock", "Sell", sh)
            comm_exit = commission_sek(sh, sh * cur_price * fx_usd)
            pnl_sek = (cur_price - trade.get("entry_price", 0)) * sh * fx_usd - comm_exit
            db.close_trade(trade["id"], exit_price=cur_price,
                           exit_reason=reason, pnl_sek=pnl_sek,
                           commission_sek=comm_exit)
            _append_trade_log(
                strategy, "SELL", ticker, sh, cur_price,
                sh * cur_price * fx_usd, pnl_sek, reason,
                entry_date=trade.get("entry_date", ""), days_held=0,
            )
            todays_actions.append({
                "action": "SELL", "ticker": ticker, "market_group": "US Equities",
                "strategy": strategy, "score": 0, "shares": sh,
                "price": cur_price, "reason": f"signals exit: {reason}",
                "pnl_sek": pnl_sek,
            })
        except Exception as e:
            print(f"  {tag} sell {ticker} FAILED: {e}")

    # ── 2. Entries ────────────────────────────────────────────────────────────
    # Re-read to reflect exits that just happened
    sig_open_now: dict[tuple, bool] = {
        (t["ticker"], t["strategy"]): True
        for t in db.get_open_trades()
        if t.get("strategy") in ALL_SIGNAL_STRATEGY_NAMES
    }
    # Count open positions per strategy
    per_strategy_open: dict[str, int] = {}
    for _, strat in sig_open_now:
        per_strategy_open[strat] = per_strategy_open.get(strat, 0) + 1

    for ticker in US_TICKERS:
        if ticker not in feat_data:
            continue
        cur_price = _price(ticker)
        if cur_price <= 0:
            continue
        uic = imap.get(ticker, {}).get("uic")
        if not uic:
            continue

        signals = get_entry_signals(ticker, feat_data[ticker])
        for sig in signals:
            strategy = sig["strategy_name"]
            pair_key = (ticker, strategy)
            if pair_key in sig_open_now:
                continue   # already holding this (ticker, strategy) pair
            if per_strategy_open.get(strategy, 0) >= MAX_POSITIONS_PER_STRATEGY:
                continue   # strategy's slot limit reached

            shares = max(1, int(SIGNALS_SLOT_SEK / (cur_price * fx_usd)))
            shares = _sim_cap_shares(shares, cur_price, fx_usd)
            stop = compute_stop(feat_data[ticker], cur_price)

            print(f"  {tag} BUY {ticker} [{strategy}] "
                  f"@ {cur_price:.2f} x{shares} "
                  f"(conf={sig['confidence']:.2f}) stop={stop:.2f}")

            entry_oid = None
            is_paper  = False
            try:
                entry_oid = saxo_client.place_market_order(
                    uic, "Stock", "Buy", shares, env="sim")
            except Exception as e:
                print(f"  {tag} order {ticker} rejected: {e}")

            filled_price = cur_price
            if entry_oid:
                ok, fp = _confirm_stock_fill(entry_oid, uic)
                if ok:
                    filled_price = fp
                else:
                    if _stocks_paper_fill_enabled():
                        is_paper = True
                        print(f"  {tag} {ticker} unfilled → paper fill @ {cur_price:.2f}")
                    else:
                        print(f"  {tag} {ticker} unfilled, skipping")
                        continue
            else:
                if _stocks_paper_fill_enabled():
                    is_paper = True
                    print(f"  {tag} {ticker} no order id → paper fill @ {cur_price:.2f}")
                else:
                    continue

            stop_oid = None
            if not is_paper and uic:
                try:
                    ak = saxo_client.get_account_key(env="sim")
                    stop_oid = saxo_order.place_stop_only(
                        post_fn=lambda path, body: saxo_client.post(path, body, env="sim"),
                        account_key=ak, uic=uic, asset_type="Stock",
                        amount=shares, entry_side="Buy",
                        stop_price=stop, symbol=ticker,
                    )
                except Exception as e:
                    print(f"  {tag} stop order failed for {ticker}: {e}")

            comm = commission_sek(shares, shares * filled_price * fx_usd)
            trade_id = db.insert_trade({
                "strategy":        strategy,
                "market_group":    "US Equities",
                "ticker":          ticker,
                "direction":       "BUY",
                "entry_date":      today_str,
                "entry_price":     filled_price,
                "shares":          shares,
                "commission_sek":  comm,
                "entry_score":     round(sig["confidence"] * 100, 1),
                "d1_trend":        0.0,
                "d2_momentum":     0.0,
                "d3_breakout":     0.0,
                "d4_mean_revert":  0.0,
                "d5_volume":       0.0,
                "d6_smart_money":  0.0,
                "d7_mom_quality":  0.0,
                "d8_regime":       0.0,
                "trailing_stop_high": filled_price,
                "regime_at_entry": "unknown",
                "stop_price":      stop,
                "paper":           1 if is_paper else 0,
                "stop_order_id":   stop_oid or None,
            })
            print(f"  {tag} recorded trade id={trade_id} "
                  f"{'[PAPER]' if is_paper else ''}")
            _append_trade_log(
                strategy, "BUY", ticker, shares, filled_price,
                shares * filled_price * fx_usd, None, sig["reason"][:80],
                entry_date=today_str, days_held=0,
            )
            todays_actions.append({
                "action": "BUY", "ticker": ticker, "market_group": "US Equities",
                "strategy": strategy, "score": sig["confidence"],
                "shares": shares, "price": filled_price, "reason": sig["reason"],
                "pnl_sek": None,
            })
            sig_open_now[pair_key] = True
            per_strategy_open[strategy] = per_strategy_open.get(strategy, 0) + 1


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


def run_intraday_cycle():
    """Intraday reversion scanner — runs every ~90 min during US market hours.

    Scans the 61-stock universe using live 5-minute bars. If a stock passes
    all four reversion conditions (EMA200, RSI<33, dip>5%, volume spike) AND
    passes the bad-news filter (gap-down < 8%), it places a buy order.

    Call this from Task Scheduler at:
      19:00, 20:30, 22:00, 23:30, 00:30 PKT  (10:00 AM – 3:30 PM ET)
    """
    print(f"\n{'='*60}")
    print(f"ATOS Intraday Reversion Scan — {datetime.now():%Y-%m-%d %H:%M:%S PKT}")
    print(f"{'='*60}")
    print(next_scan_description())

    if not us_market_is_open():
        print("  US market not in tradeable window (ET 10:00-15:30). Exiting.")
        return

    if kill_switch_active():
        print("  STOP_TRADING file present — halted.")
        return

    db.init_db()

    # ── 1. Check how many reversion slots are already filled ──────────
    open_trades   = db.get_open_trades()
    rev_open_now  = [t for t in open_trades if "Reversion" in (t.get("strategy") or "")]
    max_positions = CAP.reversion_slots(len(US_TICKERS))
    slots_free = max_positions - len(rev_open_now)
    if slots_free <= 0:
        print(f"  Reversion full ({max_positions}/{max_positions} slots). Nothing to do.")
        return

    # ── 2. Fetch historical data for daily indicators ─────────────────
    print("  Downloading daily history for indicators...")
    feat_data = download_universe(US_TICKERS)   # Saxo-native, see download_universe()
    if not feat_data:
        print("  Failed to download daily history.")
        return

    if not feat_data:
        print("  No feature data available. Aborting.")
        return

    # Build prev_close map (yesterday's close for the bad-news gap filter)
    prev_close = {}
    for ticker, df in feat_data.items():
        try:
            closes = df["Close"].dropna()
            if len(closes) >= 2:
                prev_close[ticker] = float(closes.iloc[-1])
        except Exception:
            pass

    # ── 3. Run intraday scanner ───────────────────────────────────────
    print("  Running intraday reversion scan...")
    candidates = intraday_scan(feat_data, US_TICKERS, prev_close_override=prev_close)

    if not candidates:
        print("  No intraday reversion signals. Nothing to do.")
        return

    print(f"  {len(candidates)} signal(s) found. {slots_free} slot(s) free.")

    # ── 4. Determine budget and slot size ─────────────────────────────
    try:
        balances   = saxo_client.get_balances()
        cash_sek   = float(balances.get("CashBalance", 0))
        fx_usd     = fx.get_usd_rate()
    except Exception as e:
        print(f"  Cannot fetch account balance: {e}. Aborting.")
        return

    # Cap at starting_capital_sek so SIM demo credit never inflates position sizes.
    # (The daily path already does this; this intraday path did not, so an inflated
    # SIM CashBalance could still oversize intraday reversion entries.)
    _max_deploy = CAP.starting_capital_sek() * CAP.max_deploy_pct()
    _rev_pct    = CAP.reversion_allocation_pct()
    rev_budget  = min(cash_sek * _rev_pct, _max_deploy * _rev_pct)
    slot_sek    = rev_budget / max_positions
    print(f"  Rev budget: {rev_budget:,.0f} SEK (capped at {_max_deploy * _rev_pct:,.0f}) "
          f"| slot: {slot_sek:,.0f} SEK each")

    # ── 5. Place orders for top signals (up to slots_free) ───────────
    todays_actions = []
    # Exclude names already held by EITHER sleeve -- a Blend momentum holding
    # must not also be opened as an independently-managed Reversion position
    # (and vice versa); that is the duplicate-trade / cross-strategy churn.
    already_held   = ({t.get("ticker") for t in rev_open_now}
                      | {t.get("ticker") for t in open_trades
                         if t.get("strategy") == "US Blend"})
    placed         = 0

    # This loop previously called three nonexistent things: a module-level
    # _get_uic() helper (never defined anywhere in this file), saxo_client's
    # nonexistent place_order(), and db.record_trade() (the only record_trade
    # in the codebase is atos/strategy_monitor.py's, a different class with a
    # totally different signature). It would have raised NameError on the
    # very first candidate, every single scan, since this function was
    # written -- fixed to reuse the same instrument-map lookup, order
    # placement (with atomic stop-loss), and db.insert_trade() schema
    # already fixed in run_us_reversion() (the daily path) this session.
    from atos import us_reversion as USR
    imap = load_instrument_map()
    today = date.today()

    for cand in candidates[:slots_free]:
        ticker = cand["ticker"]
        if ticker in already_held:
            continue
        price_usd = cand["price"]
        uic = imap.get(ticker, {}).get("uic")
        if not uic:
            print(f"  {ticker}: no UIC in instrument_map — skip")
            continue
        shares    = _sim_cap_shares(int(slot_sek / (price_usd * fx_usd)), price_usd, fx_usd)
        if shares < 1:
            print(f"  {ticker}: slot {slot_sek:,.0f} SEK too small for 1 share at ${price_usd:.2f}. Skip.")
            continue

        value_sek = shares * price_usd * fx_usd
        print(f"  BUY {ticker}: {shares} sh @ ${price_usd:.2f}  = {value_sek:,.0f} SEK  "
              f"[RSI={cand['rsi']} dip={cand['dip_pct']}% vol={cand['vol_ratio']}x  "
              f"gap={cand['gap_pct']}%  intraday]")

        try:
            stop_p = round(price_usd * (1 - USR.STOP_PCT), 2)
            entry_oid, stop_oid, _ = saxo_order.place_with_stop(
                post_fn=saxo_client.post,
                account_key=saxo_client.get_account_key(),
                uic=uic, asset_type="Stock", amount=shares,
                buy_sell="Buy", stop_price=stop_p,
                label=f"US Reversion:{ticker}",
            )
            paper = 0
            if entry_oid is None:
                if not _stocks_paper_fill_enabled():
                    print(f"  {ticker}: order REJECTED — no position opened, no DB row recorded")
                    continue
                paper = 1
                print(f"  {ticker}: PAPER-FILL {shares} @ ${price_usd:.2f} — Saxo SIM "
                      f"rejected the order; booked locally, managed by ATOS exit logic")
            else:
                _ok, _fp = _confirm_stock_fill(entry_oid, uic)
                if _ok:
                    if _fp > 0 and abs(_fp - price_usd) / max(price_usd, 1e-9) > 0.001:
                        print(f"  {ticker} real fill ${_fp:.2f} (scan ${price_usd:.2f})")
                    price_usd = _fp or price_usd
                else:
                    for _o in (entry_oid, stop_oid):
                        try:
                            _o and saxo_client.cancel_order(str(_o))
                        except Exception:
                            pass
                    if not _stocks_paper_fill_enabled():
                        print(f"  {ticker}: entry {entry_oid} unfilled — cancelled, no DB row")
                        continue
                    paper = 1
                    print(f"  {ticker}: entry {entry_oid} accepted but unfilled — "
                          f"cancelled, booking paper (no phantom row)")
            comm = commission_sek(shares, value_sek)
            db.insert_trade({
                "strategy": "US Reversion", "market_group": "US Equities",
                "ticker": ticker, "direction": "BUY",
                "entry_date": today.isoformat(), "entry_price": price_usd,
                "shares": shares, "commission_sek": comm,
                "entry_score": cand["score"], "d1_trend": 0, "d2_momentum": 0,
                "d3_breakout": 0,
                "d4_mean_revert": cand["rsi"],
                "d5_volume": cand["vol_ratio"],
                "d6_smart_money": 0, "d7_mom_quality": 0, "d8_regime": 0,
                "stop_price": stop_p, "trailing_stop_high": price_usd, "regime_at_entry": "reversion",
                "paper": paper,
                "stop_order_id": (stop_oid if not paper else None),
            })
            _append_trade_log(
                strategy="US Reversion",
                action="BUY",
                ticker=ticker,
                shares=shares,
                price_usd=price_usd,
                value_sek=value_sek,
                pnl_sek=None,
                reason=f"intraday dip {cand['dip_pct']}%",
            )
            todays_actions.append({
                "action": "BUY", "ticker": ticker,
                "strategy": "US Reversion",
                "reason": f"intraday dip {cand['dip_pct']}%",
            })
            notifier.notify_trade_executed(
                side="BUY", ticker=ticker, shares=shares, price_usd=price_usd,
                value_sek=value_sek, strategy="US Reversion",
                account_balance_sek=get_total_equity(db.get_open_trades()),
                reason=f"RSI {cand['rsi']:.1f} | Dip {cand['dip_pct']}% | Vol {cand['vol_ratio']}x | intraday",
            )
            placed += 1
            already_held.add(ticker)
        except Exception as e:
            print(f"  {ticker}: order failed: {e}")

    print(f"\n  Intraday scan complete. {placed} order(s) placed.")
    if todays_actions:
        print("  Orders:")
        for a in todays_actions:
            print(f"    {a['action']} {a['ticker']} [{a['strategy']}] — {a['reason']}")


if __name__ == "__main__":
    import sys
    # 2026-09-02: serialize the SIM stocks engine against the every-30-min
    # ATOS Housekeeping / ATOS Safeguard processes (all touch data/atos_live.db,
    # WAL, with no prior lock). Never skips work -- only sequences it. The
    # real-money atos_live_stocks.py uses its OWN lock (ATOS_LIVE_STOCKS_LOCK).
    _lbl = "atos-intraday" if (len(sys.argv) > 1 and sys.argv[1] == "intraday") else "atos-cycle"
    proc_lock.acquire(proc_lock.ATOS_LOCK, _lbl)
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "intraday":
            run_intraday_cycle()
        else:
            run_cycle()
    finally:
        proc_lock.release(proc_lock.ATOS_LOCK)
