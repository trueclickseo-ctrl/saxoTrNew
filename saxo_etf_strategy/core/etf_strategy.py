"""
ETF signal generation — four selectable strategies.

Set config.strategy.strategy_name to one of:
  "sector_rotation"  — rank 11 US sector ETFs by 3-month return, hold top 3
  "risk_off"         — SPY vs SMA200 regime filter, rotate equity/defensive
  "mean_reversion"   — RSI<30 + ≥5% below SMA20 dip-buy on 5 broad ETFs
  "dual_ma"          — fast/slow MA crossover across the full ETF universe

All four produce the same ETFSignal shape so etf_executor.py works unchanged.
Nothing here imports from the shares strategies.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from core.saxo_client import SaxoClient
from config.etf_config import ETFStrategyConfig

logger = logging.getLogger("etf_strategy.signals")


@dataclass
class ETFSignal:
    uic: int
    symbol: str
    description: str
    exchange_id: str
    currency: str
    action: str           # "BUY"
    score: float          # higher = stronger candidate; used for ranking
    last_price: float     # most recent close — used for position sizing and entry_price tracking
    fast_ma: float = 0.0  # strategy-specific (MA, SMA20, last price depending on strategy)
    slow_ma: float = 0.0  # strategy-specific (MA, SMA200, entry SMA)
    avg_daily_turnover: float = 0.0


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------

class _BaseStrategy:
    def __init__(self, client: SaxoClient, cfg: ETFStrategyConfig):
        self.client = client
        self.cfg = cfg

    def generate_signals(self, universe: List[dict]) -> List[ETFSignal]:
        raise NotImplementedError

    def _history(self, uic: int, count: int) -> Optional[List[float]]:
        """Return daily close prices (oldest→newest). None if insufficient history."""
        try:
            resp = self.client.get("/chart/v3/charts", params={
                "Uic": uic,
                "AssetType": "Etf",
                "Horizon": 1440,      # 1440 min = daily bars
                "Count": count + 5,   # fetch a few extra to absorb any gaps
            })
            closes = []
            for bar in resp.get("Data", []):
                if isinstance(bar, dict):
                    c = bar.get("Close") or bar.get("close")
                elif isinstance(bar, (list, tuple)) and len(bar) >= 5:
                    c = bar[4]        # [Time, O, H, L, C, V?]
                else:
                    continue
                if c is not None and float(c) > 0:
                    closes.append(float(c))
            return closes if len(closes) >= count else None
        except Exception as exc:
            logger.debug(f"Price history failed for UIC {uic}: {exc}")
            return None

    def _find(self, symbol: str, universe: List[dict]) -> Optional[dict]:
        """
        Look up an instrument by symbol.
        Saxo stores US ETFs as 'SPY:arcx' or 'QQQ:xnas' — we match on the
        base part before ':', so callers can just pass 'SPY', 'QQQ', etc.
        Priority: exact match → base-ticker match (prefers primary US exchange) → API fallback.
        """
        # 1. Exact match
        for inst in universe:
            if inst.get("Symbol") == symbol:
                return inst

        # 2. Base-ticker match: find all 'SYMBOL:*' entries and prefer arcx/xnas
        sym_lower  = symbol.lower()
        candidates = [i for i in universe
                      if str(i.get("Symbol", "")).split(":")[0].lower() == sym_lower]
        if candidates:
            # Prefer NYSE Arca (arcx) or NASDAQ (xnas) over other exchanges for US ETFs
            for pref in ("arcx", "xnas", "nyse"):
                hit = next((c for c in candidates
                            if str(c.get("Symbol", "")).lower().endswith(f":{pref}")), None)
                if hit:
                    return hit
            return candidates[0]  # fall back to first match

        # 3. API search (for symbols not in the cached universe)
        try:
            resp = self.client.get("/ref/v1/instruments", params={
                "Keywords": symbol, "AssetTypes": "Etf"
            })
            for inst in resp.get("Data", []):
                base = str(inst.get("Symbol", "")).split(":")[0]
                if base.lower() == sym_lower:
                    return inst
        except Exception:
            pass

        logger.warning(f"Instrument not found for symbol '{symbol}'")
        return None

    @staticmethod
    def _sma(closes: List[float], period: int) -> float:
        if len(closes) < period:
            return 0.0
        tail = closes[-period:]
        return sum(tail) / len(tail) if tail else 0.0

    @staticmethod
    def _rsi(closes: List[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        recent = deltas[-period:]
        avg_g = sum(max(d, 0.0) for d in recent) / period
        avg_l = sum(abs(min(d, 0.0)) for d in recent) / period
        if avg_l == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


# ---------------------------------------------------------------------------
# Strategy 1: Sector Rotation
# ---------------------------------------------------------------------------

class SectorRotationStrategy(_BaseStrategy):
    """
    Rank 11 US sector ETFs by 3-month total return.
    Top N (cfg.max_candidates_per_run, default 3) get BUY signals.
    Rebalance monthly (run with rebalance_frequency_hours=168 for weekly).
    """
    SECTORS = ["SPY", "XLK", "XLV", "XLE", "XLF", "XLI", "XLY", "XLP", "XLU", "XLRE", "XLB"]
    LOOKBACK = 63  # ~3 months of trading days

    def generate_signals(self, universe: List[dict]) -> List[ETFSignal]:
        ranked = []
        for symbol in self.SECTORS:
            inst = self._find(symbol, universe)
            if inst is None:
                continue
            uic = inst.get("Identifier")
            closes = self._history(uic, self.LOOKBACK)
            if not closes:
                logger.debug(f"SectorRotation: insufficient history for {symbol}")
                continue
            ret_3m = closes[-1] / closes[0] - 1.0
            ranked.append((ret_3m, symbol, inst, uic, closes[-1]))

        ranked.sort(reverse=True)
        top_n = self.cfg.max_candidates_per_run
        logger.info(f"SectorRotation ranking: " +
                    ", ".join(f"{s}={r*100:+.1f}%" for r, s, *_ in ranked))

        signals = []
        for rank, (ret_3m, symbol, inst, uic, last_price) in enumerate(ranked):
            if rank >= top_n:
                break
            signals.append(ETFSignal(
                uic=uic, symbol=symbol,
                description=inst.get("Description", ""),
                exchange_id=inst.get("ExchangeId", ""),
                currency=inst.get("CurrencyCode", "USD"),
                action="BUY", score=ret_3m,
                last_price=last_price,
                fast_ma=last_price, slow_ma=0.0,
            ))
        return signals


# ---------------------------------------------------------------------------
# Strategy 2: Risk-Off / Defensive Rotation
# ---------------------------------------------------------------------------

class RiskOffStrategy(_BaseStrategy):
    """
    Checks whether SPY is above its 200-day SMA.
      Uptrend  (SPY > SMA200) → buy equity ETFs: SPY + QQQ
      Downtrend (SPY < SMA200) → rotate to defensive: TLT (bonds) + GLD (gold)
    """
    EQUITY    = ["SPY", "QQQ"]
    DEFENSIVE = ["TLT", "GLD"]
    SMA_PERIOD = 200

    def generate_signals(self, universe: List[dict]) -> List[ETFSignal]:
        spy_inst = self._find("SPY", universe)
        if spy_inst is None:
            logger.warning("RiskOff: SPY not found — cannot determine market regime")
            return []

        spy_hist = self._history(spy_inst.get("Identifier"), self.SMA_PERIOD)
        if not spy_hist or len(spy_hist) < self.SMA_PERIOD:
            logger.warning("RiskOff: insufficient SPY history for SMA200")
            return []

        spy_price = spy_hist[-1]
        sma200    = self._sma(spy_hist, self.SMA_PERIOD)
        regime_up = spy_price > sma200
        pct_vs    = (spy_price / sma200 - 1.0) if sma200 else 0.0
        regime    = "UPTREND" if regime_up else "DOWNTREND"
        logger.info(f"RiskOff: SPY {spy_price:.2f} vs SMA200 {sma200:.2f} — {regime} ({pct_vs*100:+.1f}%)")

        buy_list = self.EQUITY if regime_up else self.DEFENSIVE
        signals  = []
        for i, symbol in enumerate(buy_list):
            inst = self._find(symbol, universe)
            if inst is None:
                continue
            uic   = inst.get("Identifier")
            hist  = self._history(uic, 20) or []
            price = hist[-1] if hist else 0.0
            signals.append(ETFSignal(
                uic=uic, symbol=symbol,
                description=inst.get("Description", ""),
                exchange_id=inst.get("ExchangeId", ""),
                currency=inst.get("CurrencyCode", "USD"),
                action="BUY",
                score=abs(pct_vs) - i * 0.0001,  # tiny offset to rank within pair
                last_price=price,
                fast_ma=price, slow_ma=sma200,
            ))
        return signals


# ---------------------------------------------------------------------------
# Strategy 3: Mean Reversion (dip-buy)
# ---------------------------------------------------------------------------

class MeanReversionStrategy(_BaseStrategy):
    """
    RSI(14) < 30 AND price ≥ 5% below 20-day SMA.
    Targets: SPY, QQQ, IWM, EFA, EEM.
    Standard stop-loss / take-profit exits handled by etf_executor.review_exits().
    """
    TARGETS   = ["SPY", "QQQ", "IWM", "EFA", "EEM"]
    RSI_ENTRY = 30
    DIP_PCT   = 0.05

    def generate_signals(self, universe: List[dict]) -> List[ETFSignal]:
        signals = []
        for symbol in self.TARGETS:
            inst = self._find(symbol, universe)
            if inst is None:
                continue
            uic    = inst.get("Identifier")
            closes = self._history(uic, 50)
            if not closes or len(closes) < 22:
                continue
            rsi   = self._rsi(closes)
            sma20 = self._sma(closes, 20)
            price = closes[-1]
            dip   = (sma20 - price) / sma20 if sma20 else 0.0  # positive = below SMA

            logger.debug(f"MeanRev {symbol}: RSI={rsi:.1f}  dip={dip*100:.1f}%  threshold=RSI<{self.RSI_ENTRY} & dip≥{self.DIP_PCT*100:.0f}%")

            if rsi < self.RSI_ENTRY and dip >= self.DIP_PCT:
                score = (self.RSI_ENTRY - rsi) + dip * 100
                logger.info(f"MeanRev BUY: {symbol}  RSI={rsi:.1f}  dip={dip*100:.1f}%  score={score:.1f}")
                signals.append(ETFSignal(
                    uic=uic, symbol=symbol,
                    description=inst.get("Description", ""),
                    exchange_id=inst.get("ExchangeId", ""),
                    currency=inst.get("CurrencyCode", "USD"),
                    action="BUY", score=score,
                    last_price=price,
                    fast_ma=price, slow_ma=sma20,
                ))
        return signals


# ---------------------------------------------------------------------------
# Strategy 4: Dual Moving-Average (full universe scan)
# ---------------------------------------------------------------------------

class DualMAStrategy(_BaseStrategy):
    """
    MA crossover on a curated list of the 50 most liquid US ETFs.
    Buy when 20-day MA > 100-day MA; rank by crossover strength.
    Using a fixed universe (not full-universe scan) keeps run time under 60s.
    """
    # Top 50 US ETFs by AUM — broad market, sectors, bonds, commodities, factors
    UNIVERSE = [
        "SPY", "QQQ", "IWM", "EFA", "EEM", "VTI", "AGG", "LQD", "TLT", "GLD",
        "SHY", "HYG", "XLK", "XLV", "XLF", "XLE", "XLI", "XLY", "XLP", "XLU",
        "XLRE", "XLB", "SMH", "XHB", "XBI", "IBB", "KWEB", "ARKG", "ARKK", "ARKF",
        "VWO", "VEA", "BND", "VNQ", "GDX", "GDXJ", "SLV", "IAU", "USO", "UNG",
        "DIA", "MDY", "IJR", "IEMG", "SPYV", "SPYG", "VTV", "VUG", "VO", "VB",
    ]

    def generate_signals(self, universe: List[dict]) -> List[ETFSignal]:
        logger.info(f"DualMA: scanning {len(self.UNIVERSE)} curated ETFs "
                    f"(fast={self.cfg.lookback_days_fast}d / slow={self.cfg.lookback_days_slow}d MA)")
        signals = []
        for symbol in self.UNIVERSE:
            inst = self._find(symbol, universe)
            if inst is None:
                continue
            uic  = inst.get("Identifier")
            closes = self._history(uic, self.cfg.lookback_days_slow)
            if not closes:
                continue
            fast_ma = self._sma(closes, self.cfg.lookback_days_fast)
            slow_ma = self._sma(closes, self.cfg.lookback_days_slow)
            if slow_ma <= 0 or fast_ma <= slow_ma:
                continue
            score = fast_ma / slow_ma - 1.0
            signals.append(ETFSignal(
                uic=uic, symbol=symbol,
                description=inst.get("Description", ""),
                exchange_id=inst.get("ExchangeId", ""),
                currency=inst.get("CurrencyCode", "USD"),
                action="BUY", score=score,
                last_price=closes[-1],
                fast_ma=fast_ma, slow_ma=slow_ma,
            ))

        signals.sort(key=lambda s: s.score, reverse=True)
        return signals[: self.cfg.max_candidates_per_run]


# ---------------------------------------------------------------------------
# Strategy 5: Full-universe Momentum Scan
# ---------------------------------------------------------------------------

class MomentumScanStrategy(_BaseStrategy):
    """
    Scans the full 8,924-ETF universe to find top performers by 3-month momentum.

    Stage 1 — Filter to liquid US candidates:
      - Exchange: NYSE_ARCA or NASDAQ only
      - Base ticker length ≤ 5 chars (proxy for size/liquidity)
      - Description excludes leveraged/inverse keywords (ULTRA, 2X, 3X, BEAR, SHORT, INVERSE)
      - Take top MAX_CANDIDATES (200) sorted by ticker length ascending

    Stage 2 — Fetch price history and score:
      - Requires at least LOOKBACK bars of history
      - Price must be above SMA(20) — uptrend confirmation
      - 3-month return must be positive
      - Score = (price / price_63d_ago) - 1

    Stage 3 — Return top cfg.max_candidates_per_run as BUY signals.

    Runtime: ~3-5 minutes for 200 API calls. Schedule weekly or after market close.
    """
    LOOKBACK      = 63    # 3 months of trading days
    SMA_CONFIRM   = 20    # price must be above SMA20
    MAX_CANDIDATES = 200  # pre-filter size before API calls
    CALL_DELAY    = 0.25  # seconds between history API calls (rate-limit safety)
    EXCHANGES     = {"NYSE_ARCA", "NASDAQ"}
    EXCLUDE_KW    = {"ultra", "2x", "3x", "-2", "-3", "bear", "short", "inverse",
                     "proshares ultra", "direxion"}

    def generate_signals(self, universe: List[dict]) -> List[ETFSignal]:
        # Stage 1: filter to liquid US ETFs
        candidates = []
        for inst in universe:
            if inst.get("ExchangeId") not in self.EXCHANGES:
                continue
            base = str(inst.get("Symbol", "")).split(":")[0]
            if not base or len(base) > 5:
                continue
            desc = str(inst.get("Description", "")).lower()
            if any(kw in desc for kw in self.EXCLUDE_KW):
                continue
            candidates.append((len(base), inst))

        candidates.sort(key=lambda x: x[0])   # shorter ticker = more established
        candidates = [inst for _, inst in candidates[:self.MAX_CANDIDATES]]
        logger.info(f"MomentumScan: {len(candidates)} candidates after exchange/leverage filter "
                    f"(from {len(universe)} total ETFs)")

        # Stage 2: fetch history and rank
        scored = []
        for i, inst in enumerate(candidates):
            uic    = inst.get("Identifier")
            closes = self._history(uic, self.LOOKBACK)
            if closes and len(closes) >= self.LOOKBACK // 2:
                last  = closes[-1]
                first = closes[0]
                sma20 = self._sma(closes, min(self.SMA_CONFIRM, len(closes)))
                ret   = last / first - 1.0
                if last > sma20 and ret > 0:
                    base = str(inst.get("Symbol", "")).split(":")[0]
                    scored.append((ret, base, inst, uic, last, closes))
            if (i + 1) % 25 == 0:
                logger.info(f"  MomentumScan progress: {i+1}/{len(candidates)} "
                            f"({len(scored)} qualifying so far)")
            time.sleep(self.CALL_DELAY)

        scored.sort(reverse=True)
        top_n = self.cfg.max_candidates_per_run
        logger.info(f"MomentumScan: {len(scored)} qualify → selecting top {top_n}")
        if scored:
            logger.info("  Ranking: " +
                        ", ".join(f"{s}={r*100:+.1f}%" for r, s, *_ in scored[:10]))

        signals = []
        for rank, (ret, symbol, inst, uic, last_price, closes) in enumerate(scored):
            if rank >= top_n:
                break
            sma20 = self._sma(closes, self.SMA_CONFIRM)
            signals.append(ETFSignal(
                uic=uic, symbol=symbol,
                description=inst.get("Description", ""),
                exchange_id=inst.get("ExchangeId", ""),
                currency=inst.get("CurrencyCode", "USD"),
                action="BUY", score=ret,
                last_price=last_price,
                fast_ma=last_price, slow_ma=sma20,
            ))
        return signals


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------

class ETFStrategyEngine:
    """
    Selects the right strategy from cfg.strategy_name and delegates to it.
    ETFBot calls engine.generate_signals(universe) — no other coupling.
    """
    _MAP = {
        "sector_rotation": SectorRotationStrategy,
        "risk_off":        RiskOffStrategy,
        "mean_reversion":  MeanReversionStrategy,
        "dual_ma":         DualMAStrategy,
        "momentum_scan":   MomentumScanStrategy,
    }

    def __init__(self, client: SaxoClient, cfg: ETFStrategyConfig):
        cls = self._MAP.get(cfg.strategy_name)
        if cls is None:
            raise ValueError(
                f"Unknown ETF strategy '{cfg.strategy_name}'. "
                f"Valid options: {list(self._MAP)}"
            )
        self._impl = cls(client, cfg)
        logger.info(f"ETF strategy selected: {cfg.strategy_name}")

    def generate_signals(self, universe: List[dict]) -> List[ETFSignal]:
        return self._impl.generate_signals(universe)
