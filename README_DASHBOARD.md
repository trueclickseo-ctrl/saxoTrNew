# ATOS Local Dashboard

## Quick Start

To launch the local dashboard server, run:

```bash
py -3 atos_dashboard.py
```

Once running, access the dashboard in your web browser at:
`http://localhost:8070`

---

## Overview & Architecture

The **ATOS Local Dashboard** is a standalone, web-based monitoring interface designed to provide real-time visibility into the ATOS algorithmic trading system.

> [!NOTE]
> **READ-ONLY OPERATIONAL MODE**
> The dashboard is strictly a **monitoring tool** and does not execute trades or alter market state directly. It displays real-time and historical data directly from the local SQLite database (`data/atos.db`). The daily trading cycle (`atos_runner.py` or `run_atos.py`) is responsible for executing algorithms, making API requests to Saxo Bank SIM, and writing state changes to the database.

---

## Key Dashboard Sections

1. **KPI Cards**
   - Current portfolio equity, total P&L (currency and percentage), open position count.

2. **Strategy Sleeve Cards**
   - US Blend (blue) and US Reversion (orange) cards showing open positions per strategy.

3. **Strategy Head-to-Head Table**
   - Side-by-side comparison: N trades, win rate, total P&L, avg win, avg loss, winner per metric.
   - Updates automatically as trades accumulate in `data/trade_log.csv`.

4. **Cumulative P&L Chart (per strategy)**
   - Line chart: US Blend (blue) vs US Reversion (orange) cumulative realised P&L over time.
   - Built from SELL rows in `data/trade_log.csv`.

5. **Equity Curve**
   - 90-day account equity progression.

6. **Algorithm Brain**
   - Adaptive detector weights (Trend, Momentum, Breakout, Mean Reversion, Volume).

7. **Today's Actions**
   - BUY / SELL / BLOCKED signals with strategy column (which strategy placed each trade).

8. **Open Positions**
   - Entry price, current price, unrealised P&L, strategy label.

9. **Trade History (last 30)**
   - Closed trades from `data/trade_log.csv` — strategy, entry/exit, P&L, reason, days held.

10. **Market Allocation**
    - Donut chart of capital across market groups.

8. **Signal Log**
   - Searchable and filterable log of all historical signals evaluated by the system.

---

## Features & Options

- **Theme Toggle**: Switch between Dark and Light mode themes. Your theme preference is automatically persisted in the browser's `localStorage`.
- **Auto-Refresh**: Dashboard data automatically synchronizes with `data/atos.db` every 60 seconds.
- **Port Configuration**: To change the default port from `8070`, update the `LOCAL_PORT` constant at the top of `atos_dashboard.py`:
  ```python
  LOCAL_PORT = 8070  # Change to desired port number
  ```

---

## REST API Endpoints

The dashboard server exposes public REST API endpoints delivering JSON payloads from `data/atos.db`:

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/api/summary` | `GET` | Key performance indicators, portfolio equity summary, and algorithm statistics. |
| `/api/equity` | `GET` | Historical daily equity values for plotting equity curves. |
| `/api/trades/open` | `GET` | List of current active portfolio positions and entry signals. |
| `/api/trades/closed` | `GET` | Historical record of closed trades and realized outcomes. |
| `/api/signals` | `GET` | Signal history including D1-D5 detector sub-scores. |
| `/api/weights` | `GET` | Current and historical detector weights (Algorithm Brain state). |

---
