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
   - Displays core portfolio metrics: current portfolio equity, total P&L (currency and percentage), count of open positions, and key algorithm performance statistics.

2. **Equity Curve**
   - Interactive 90-day chart tracking cumulative account equity and P&L progression over time.

3. **Algorithm Brain**
   - Visual breakdown of adaptive detector weights (Trend, Momentum, Breakout, Mean Reversion, Volume).
   - Includes real-time detector weight visual bars and historical evolution charts tracking model adaptation.

4. **Today's Signals**
   - Complete record of generated signals for the current trading day (`BUY`, `EXIT`, `BLOCKED`).
   - Detailed breakdown of sub-scores across all 5 detectors (D1 to D5).

5. **Open Positions (Portfolio)**
   - Active holdings detailing entry price, current price, unrealized P&L, position size, and detector breakdown per active trade.

6. **Trade History**
   - Archive of closed trades with realized P&L, exit reason (e.g., stop loss, take profit, signal exit), holding duration, and algorithm entry/exit scores.

7. **Market Allocation**
   - Interactive donut chart visualizing capital allocation across the 5 markets (US Equities, OMX30, DAX40, Commodities, Forex).

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
