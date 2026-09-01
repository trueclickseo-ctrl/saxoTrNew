# `account_equity.py` — the one honest real-money account view

**User, 2026-09-01: "fix account-equity reporting first — don't use the
€35,000 sizing cap as peak equity."**

Before this, nothing tracked the *actual* account value. `_account()`
sizes off `min(real_pooled, RISK_EQUITY_CAP)` and the old
`data/*_peak_equity.json` files stored that **capped** number as "peak
equity" — so drawdown / give-back / return were computed against a
ceiling, not real money.

## The spike (2026-09-01, live)

Saxo's OpenAPI **does not expose per-sub-account balances** when the
sub-accounts share a margin group. This login has SEK / EUR / USD under
one AccountGroup (`AccountGroupId 22540456`, `IndividualMargining: false`).
`/port/v1/balances/me` returns the **pooled group total, in SEK**,
regardless of `AccountKey` / `ClientKey` / `AccountGroupKey`.

| Tried | Result |
|---|---|
| `/balances/me` (no params) | pooled `TotalValue` in SEK |
| `?AccountKey=<SEK>` / `<EUR>` / `<USD>` | **identical** pooled value |
| `?AccountKey=X&ClientKey=Y` | identical |
| `?AccountGroupKey=…` | identical |
| `/port/v1/accounts/{key}` | metadata only, no value |
| `/hist/v*/perf*` | 404 (not in scope) |

**What IS available and reliable:**
- pooled `TotalValue` / `NetEquityForMargin` / `CashBalance` /
  `MarginUtilizationPct` / `UnrealizedMarginProfitLoss` (SEK)
- `TransactionsNotBookedDetail.CashDeposit` — recent unbooked transfers
- `/port/v1/positions/me` — per-`AccountKey` `ProfitLossOnTradeInBaseCurrency`
  (unrealised P&L, splittable by sub-account)

That pooled `TotalValue` **is** the real total real-money equity and **is
what actually constrains trading** (shared margin). After the SEK
consolidation there's one funded account, so pooled == that account.

## The module

| | |
|---|---|
| `snapshot()` | fetch pooled balance + positions, append one row to `data/account_equity_curve.jsonl`. Called from `run_daily` on the **`live` run only** (once per pool). |
| `stats()` | from the curve: real equity, all-time peak, drawdown %, return, 7-day peak→now give-back, weekly hi/lo, per-account unrealised P&L. |
| `render()` / `render_html()` | the block for the LIVE dashboard + the daily-summary email. |
| CLI | `python account_equity.py` (print) · `--snapshot` (append only) |

**Deposits** — `data/account_deposits.json`, `entries: [{date, sek, note}]`.
- Empty → return is measured from the first curve row ("since tracking started").
- Populated → true return-since-inception `(equity − Σdeposits) / Σdeposits`.
- Seeded with best-estimate values (`needs_review: true`) — **correct them
  against your real Saxo transfer history.**
- A snapshot that sees `TotalValue` jump in a way unrealised P&L can't
  explain (> 500 SEK) sets `suspected_transfer_sek` on the row, logs it,
  and raises a low-severity `attention` item so the return number doesn't
  silently break.

## Report-only

Nothing here gates a trade. The drawdown % is a number on a screen — the
drawdown breaker stays permanently disabled (user, 2026-08-24). A −15–20%
drawdown on a ~€3k book running €45 fixed risk is **ordinary variance**
(a 4–5 trade losing streak = −€200 ≈ −6%), not a signal.

## Next (not built yet)

- **Size the SEK book off `REAL_EQUITY`** once EUR is closed: the €45
  per-trade risk is fixed, so real equity only drives the 8% RSI heat cap
  (concurrency) and the 50% margin gate — point those at the real number
  instead of the cap.
- **Per-trade give-back exit** for RSI — after `report_giveback.py` has
  ~1 week of post-`4e2edb8` data.
