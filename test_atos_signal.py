# -*- coding: utf-8 -*-
"""Debug: show raw detector scores for each ticker."""
import sys, os, warnings
warnings.filterwarnings('ignore')
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')

import yfinance as yf
import pandas as pd
from atos.features import add_all
from atos.detectors import (Detector1_Trend, Detector2_Momentum,
                              Detector3_Breakout, Detector4_MeanReversion,
                              Detector5_Volume)
from atos.decision_engine import BUY_THRESHOLD, evaluate
from atos.universe import market_of

d1 = Detector1_Trend()
d2 = Detector2_Momentum()
d3 = Detector3_Breakout()
d4 = Detector4_MeanReversion()
d5 = Detector5_Volume()

# Mix of tickers: trending, sideways, declining
test_tickers = ['AAPL','NVDA','MSFT','GLD','TSLA','JPM','SAP.DE','ERIC-B.ST']

print(f'Downloading {len(test_tickers)} tickers (300 days)...')
raw = yf.download(test_tickers, period='300d', interval='1d',
                  group_by='ticker', auto_adjust=True, threads=True, progress=False)

print(f'\n{"Ticker":<12} {"Close":>8} {"EMA20":>8} {"EMA50":>8} {"RSI":>6} {"ADX":>6}'
      f' {"D1-Trend":>9} {"D2-Mom":>7} {"D3-Break":>9} {"D4-MR":>6} {"D5-Vol":>7}'
      f' {"COMBINED":>9} {"ACTION"}')
print('-' * 110)

buy_signals = []

for ticker in test_tickers:
    try:
        df = raw[ticker].dropna(how='all')
        if len(df) < 50:
            print(f'{ticker:<12} SKIP: only {len(df)} rows')
            continue

        feat = add_all(df)
        row  = feat.iloc[-1]
        mkt  = market_of(ticker)

        s1 = d1.score(row)
        s2 = d2.score(row)
        s3 = d3.score(row)
        s4 = d4.score(row)
        s5 = d5.score(row)
        combined = (s1 + s2 + s3 + s4 + s5) / 5

        action = 'BUY ***' if combined >= BUY_THRESHOLD else 'hold'
        if combined >= BUY_THRESHOLD:
            buy_signals.append(ticker)

        close  = row.get('Close', 0)
        ema20  = row.get('ema20', 0)
        ema50  = row.get('ema50', 0)
        rsi    = row.get('rsi', 0)
        adx    = row.get('adx', 0)

        print(f'{ticker:<12} {close:>8.2f} {ema20:>8.2f} {ema50:>8.2f} {rsi:>6.1f} {adx:>6.1f}'
              f' {s1:>9.1f} {s2:>7.1f} {s3:>9.1f} {s4:>6.1f} {s5:>7.1f}'
              f' {combined:>9.1f} {action}')

    except Exception as e:
        print(f'{ticker:<12} ERROR: {e}')

print()
print(f'BUY signals: {buy_signals if buy_signals else "none — all markets in HOLD range today"}')
print(f'BUY threshold: {BUY_THRESHOLD}/100')
print()
print('Explanation of current market:')
print('  - Scores represent CURRENT market conditions from Yahoo Finance')
print('  - Low scores = stocks in pullback/sideways (expected for some days)')
print('  - The bot does NOT trade on bad signal days — this is CORRECT behavior')
print('  - After the fix: trending breakouts no longer penalized by Mean Reversion detector')
