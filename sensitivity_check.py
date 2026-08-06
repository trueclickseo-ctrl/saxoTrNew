import pandas as pd

df = pd.read_csv("results/trade_log.csv")
total = df["pnl"].sum()
print("Full universe net profit:", round(total, 2))

for n in [1, 3, 5, 10]:
    top_n = df.nlargest(n, "pnl")
    removed = top_n["pnl"].sum()
    without = total - removed
    print(f"Without top {n} trades: {round(without, 2)}  (removed {round(removed, 2)})")
