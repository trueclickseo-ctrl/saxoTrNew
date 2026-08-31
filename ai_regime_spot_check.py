"""
ai_regime_spot_check.py -- Sprint 1 historical sanity check for
ai/regime/classifier.py. Read-only, no state, no orders.

Pulls the daily history forex/runner already fetches (Saxo, SIM) for a
spread of pairs and prints the current regime label + the numbers behind
it, plus the label a week ago and a month ago. Eyeball it: a stretch you
KNOW was choppy shouldn't read TRENDING; a quiet range shouldn't read
HIGH_VOLATILITY. If a sign looks inverted, that's the bug to catch here,
before Sprint 2 depends on this.

    python ai_regime_spot_check.py
    python ai_regime_spot_check.py EURUSD GBPJPY XAUUSD
"""

import sys

import forex.runner as r
from ai.regime.classifier import classify_regime, MIN_BARS

DEFAULT_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "EURGBP",
                 "AUDUSD", "USDCAD", "XAUUSD", "EURCHF", "NZDUSD"]

G, Y, C, DIM, X, B = "\033[92m", "\033[93m", "\033[96m", "\033[2m", "\033[0m", "\033[1m"


def _row(sym, df):
    if df is None or len(df) < MIN_BARS + 22:
        print(f"  {sym:8} {DIM}not enough history ({0 if df is None else len(df)} bars){X}")
        return
    now  = classify_regime(df)
    wk   = classify_regime(df.iloc[:-5])
    mo   = classify_regime(df.iloc[:-22])
    print(f"  {B}{sym:8}{X} {now['label']:<17} "
          f"adx={now['adx']:>5} (+DI {now['plus_di']:>4} / -DI {now['minus_di']:>4})  "
          f"atr%={now['atr_pct']:>5}  atr_ratio={now['atr_ratio']:>4}  "
          f"slope={now['ma_slope']:>6}  conf={now['confidence']:.2f}")
    print(f"  {DIM}{'':8} ~1wk ago: {wk['label']:<17}  ~1mo ago: {mo['label']}{X}")


def main():
    r.set_account_env("sim")
    pairs = [p.upper() for p in sys.argv[1:]] or DEFAULT_PAIRS
    by_sym = {p["symbol"]: p for p in r.PAIRS}
    print(f"{B}Regime spot-check{X}  {DIM}(daily bars, Saxo SIM){X}\n")
    for sym in pairs:
        pi = by_sym.get(sym)
        if pi is None:
            print(f"  {sym:8} {Y}not in the pair universe{X}")
            continue
        _row(sym, r._fetch_history(pi["uic"]))
    print(f"\n{DIM}Thresholds live in ai/regime/classifier.py "
          f"(ADX_TREND, ATR_RATIO_HIGH/LOW, ...). Adjust with evidence from this check.{X}")


if __name__ == "__main__":
    main()
