"""
research_ml_probability.py   (RESEARCH ONLY — not wired into the engine)
------------------------------------------------------------------------
Honest test of the "probability model" idea (meta-labeling / triple-barrier).
For each US stock/day we build technical features and a LABEL: did the stock hit
+TP% before -SL% within H days? A gradient-boosting classifier estimates that
probability. We evaluate it WALK-FORWARD (train on the past, test on unseen
future) so the numbers aren't overfit, and compare to the naive base rate.

Question we answer: does the model's high-probability bucket actually win more
often out-of-sample than just always trading? If not, ML adds nothing here.

    <venv>/python.exe research_ml_probability.py
"""
import warnings; warnings.filterwarnings("ignore")
import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd, yfinance as yf
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score
from atos.features import add_all
from atos.universe import US_TICKERS

HISTORY = "10y"
TP, SL, H = 0.05, 0.03, 20     # +5% target before -3% stop, within 20 trading days

FEATURES = ["rsi", "adx", "roc_10", "roc_20", "mom_acceleration", "vol_ratio"]


def build_dataset():
    ts = sorted(set(US_TICKERS))
    raw = yf.download(ts, period=HISTORY, interval="1d", group_by="ticker",
                      auto_adjust=True, threads=True, progress=False)
    frames = []
    for t in ts:
        try:
            df = raw[t].dropna(how="all")
            if len(df) < 300:
                continue
            f = add_all(df)
        except Exception:
            continue
        c = f["Close"].values; hi = f["High"].values; lo = f["Low"].values
        n = len(f)
        # triple-barrier label
        label = np.full(n, np.nan)
        for i in range(n - H):
            up, dn = c[i] * (1 + TP), c[i] * (1 - SL)
            lab = 0
            for j in range(1, H + 1):
                if hi[i + j] >= up:
                    lab = 1; break
                if lo[i + j] <= dn:
                    lab = 0; break
            label[i] = lab
        # engineered features
        d = pd.DataFrame(index=f.index)
        for col in FEATURES:
            d[col] = f[col].values if col in f else np.nan
        d["atr_pct"]  = (f.get("atr", pd.Series(np.nan, index=f.index)) / f["Close"]).values
        d["ema20_d"]  = (f["Close"] / f.get("ema20") - 1).values
        d["ema50_d"]  = (f["Close"] / f.get("ema50") - 1).values
        d["ema200_d"] = (f["Close"] / f.get("ema200") - 1).values
        bb_l, bb_u = f.get("bb_lower"), f.get("bb_upper")
        d["bb_pos"]   = ((f["Close"] - bb_l) / (bb_u - bb_l)).values
        d["mom120"]   = (f["Close"] / f["Close"].shift(120) - 1).values
        d["label"] = label
        d["date"] = f.index
        frames.append(d.dropna())
    data = pd.concat(frames).sort_values("date").reset_index(drop=True)
    return data


def main():
    print(f"Building dataset (label = hit +{TP*100:.0f}% before -{SL*100:.0f}% within {H}d)...")
    data = build_dataset()
    feat_cols = [c for c in data.columns if c not in ("label", "date")]
    X = data[feat_cols].values
    y = data["label"].values.astype(int)
    base = y.mean()
    print(f"Rows: {len(data):,} | features: {len(feat_cols)} | base rate P(win): {base:.1%}")

    tscv = TimeSeriesSplit(n_splits=5)
    aucs, oof_p, oof_y = [], [], []
    for k, (tr, te) in enumerate(tscv.split(X), 1):
        m = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05,
                                           max_depth=4, l2_regularization=1.0,
                                           random_state=0)
        m.fit(X[tr], y[tr])
        p = m.predict_proba(X[te])[:, 1]
        auc = roc_auc_score(y[te], p)
        aucs.append(auc)
        oof_p.extend(p); oof_y.extend(y[te])
        print(f"  fold {k}: test AUC {auc:.3f}  (test rows {len(te):,})")

    oof_p = np.array(oof_p); oof_y = np.array(oof_y)
    print(f"\nWalk-forward OOS AUC: {np.mean(aucs):.3f}  (0.50 = no skill)")
    print(f"{'threshold':>10} {'trades':>8} {'win rate':>9} {'lift vs base':>13}")
    for thr in (0.50, 0.60, 0.70, 0.80):
        sel = oof_p >= thr
        if sel.sum() == 0:
            print(f"{thr:>10.2f} {'0':>8} {'--':>9} {'--':>13}"); continue
        wr = oof_y[sel].mean()
        print(f"{thr:>10.2f} {sel.sum():>8,} {wr:>8.1%} {wr-base:>+12.1%}")
    print("\nRead: a threshold's win rate must beat the base rate by a MEANINGFUL, stable")
    print("margin (and AUC clearly > 0.55) for the model to add real edge over just")
    print("trading every setup. Small/negative lift = ML is not helping here.")


if __name__ == "__main__":
    main()
