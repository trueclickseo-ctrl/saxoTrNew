"""
bots/dashboard_helper.py
-------------------------
Data gatherer for dashboard_binance.ps1.

Connects to Binance testnet, reads local log files, and prints a single
JSON object to stdout.  PowerShell parses that JSON and formats the display.

Never places orders, cancels orders, or writes to the kill-switch file.
Read-only.

Usage (called by dashboard_binance.ps1 — don't run manually unless debugging):
    .venv/Scripts/python bots/dashboard_helper.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Project root on sys.path ─────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOG_DIR       = ROOT / "logs"
PID_FILE      = LOG_DIR / "binance_bot.pid"
SIGNALS_JSONL = LOG_DIR / "binance_signals.jsonl"
TRADES_JSONL  = LOG_DIR / "binance_trades.jsonl"
CONFIG_PATH   = ROOT / "binance_bot" / "config" / "binance_testnet_config.yaml"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_env() -> None:
    try:
        from dotenv import load_dotenv   # type: ignore
        env_path = ROOT / ".env.binance"
        if env_path.exists():
            load_dotenv(env_path)
    except ImportError:
        pass


def _load_config() -> dict:
    try:
        import yaml   # type: ignore
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return {}


def _is_pid_running(pid: int) -> bool:
    """Check if a process with the given PID is alive (Windows-safe)."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def _bot_health(cfg: dict) -> dict:
    scan_interval = int((cfg.get("execution") or {}).get("scan_interval_minutes", 60))

    # PID file check
    pid_exists  = PID_FILE.exists()
    pid         = None
    proc_alive  = False
    if pid_exists:
        try:
            pid = int(PID_FILE.read_text().strip())
            proc_alive = _is_pid_running(pid)
        except Exception:
            pid = None

    # Log freshness check — find today's log file
    today_log = LOG_DIR / f"binance_{datetime.now().strftime('%Y-%m-%d')}.log"
    log_age_minutes = None
    log_file_str    = str(today_log)
    if today_log.exists():
        age_sec = time.time() - today_log.stat().st_mtime
        log_age_minutes = round(age_sec / 60, 1)

    # Status
    if proc_alive:
        status = "RUNNING"
    elif pid_exists and not proc_alive:
        status = "PID_STALE"  # PID file exists but process is gone (crashed without cleanup)
    else:
        status = "STOPPED"

    stale_flag = (
        log_age_minutes is not None
        and log_age_minutes > scan_interval * 2
        and status == "RUNNING"
    )
    if stale_flag:
        status = "STALE"

    return {
        "pid_file_exists":    pid_exists,
        "pid":                pid,
        "process_running":    proc_alive,
        "log_age_minutes":    log_age_minutes,
        "scan_interval_minutes": scan_interval,
        "status":             status,
        "log_file":           log_file_str,
    }


def _get_positions(adapter, symbols: list[str]) -> tuple[list[dict], list[str]]:
    """
    Reconstruct open positions from trade history (our API key only).
    Returns (positions_list, held_symbols).
    """
    positions    = []
    held_symbols = []

    for symbol in symbols:
        try:
            trades  = adapter._c.get_my_trades(symbol, limit=100)
            net_qty = sum(
                float(t["qty"]) * (1.0 if t["isBuyer"] else -1.0)
                for t in trades
            )
            if net_qty < 1e-8:
                continue

            # Weighted-average entry from buy fills
            buy_trades = [t for t in trades if t["isBuyer"]]
            total_cost = sum(float(t["qty"]) * float(t["price"]) for t in buy_trades)
            total_qty  = sum(float(t["qty"]) for t in buy_trades)
            avg_entry  = total_cost / total_qty if total_qty > 0 else 0.0

            ticker        = adapter._c.get_symbol_ticker(symbol)
            current_price = float(ticker["price"])
            unreal_pnl    = (current_price - avg_entry) * net_qty
            unreal_pct    = ((current_price / avg_entry) - 1) * 100 if avg_entry > 0 else 0.0

            held_symbols.append(symbol)
            positions.append({
                "symbol":           symbol,
                "net_qty":          round(net_qty, 8),
                "avg_entry":        round(avg_entry, 4),
                "current_price":    round(current_price, 4),
                "unrealized_pnl":   round(unreal_pnl, 4),
                "unrealized_pnl_pct": round(unreal_pct, 2),
            })
        except Exception:
            pass

    return positions, held_symbols


def _recent_trades_from_api(adapter, symbols: list[str], limit: int = 20) -> list[dict]:
    """
    Pull fills from the exchange API for all universe symbols, merge with
    our local JSONL (for strategy attribution), return most recent `limit` fills.
    """
    # Load local JSONL for strategy + signal-condition attribution
    local_by_order: dict[str, dict] = {}
    if TRADES_JSONL.exists():
        try:
            with open(TRADES_JSONL, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        oid = str(rec.get("order_id", ""))
                        if oid:
                            local_by_order[oid] = rec
                    except Exception:
                        pass
        except Exception:
            pass

    all_fills = []
    for symbol in symbols:
        try:
            fills = adapter._c.get_my_trades(symbol, limit=50)
            for f in fills:
                ts_ms  = int(f["time"])
                ts_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
                oid    = str(f.get("orderId", ""))
                local  = local_by_order.get(oid, {})
                all_fills.append({
                    "ts":        ts_iso,
                    "ts_ms":     ts_ms,
                    "symbol":    symbol,
                    "side":      "BUY" if f["isBuyer"] else "SELL",
                    "price":     float(f["price"]),
                    "qty":       float(f["qty"]),
                    "order_id":  oid,
                    "strategy":  local.get("strategy", "unknown"),
                    "rsi":       local.get("rsi"),
                    "dip_pct":   local.get("dip_pct"),
                    "vol_ratio": local.get("vol_ratio"),
                })
        except Exception:
            pass

    all_fills.sort(key=lambda x: x["ts_ms"], reverse=True)
    for f in all_fills:
        del f["ts_ms"]
    return all_fills[:limit]


def _last_scan(scan_interval: int) -> dict:
    """Read the most recent complete scan from binance_signals.jsonl."""
    if not SIGNALS_JSONL.exists():
        return {"available": False}

    lines = []
    try:
        with open(SIGNALS_JSONL, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return {"available": False}

    if not lines:
        return {"available": False}

    # Find last scan_id (the most recent group of entries)
    last_scan_id = None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
            sid = rec.get("scan_id")
            if sid:
                last_scan_id = sid
                break
        except Exception:
            pass

    if not last_scan_id:
        return {"available": False}

    # Collect all signals from that scan_id
    signals = []
    scan_ts  = None
    for line in lines:
        try:
            rec = json.loads(line)
            if rec.get("scan_id") == last_scan_id:
                signals.append({
                    "symbol":    rec.get("symbol"),
                    "action":    rec.get("action"),
                    "reason":    rec.get("reason", ""),
                    "rsi":       rec.get("rsi"),
                    "dip_pct":   rec.get("dip_pct"),
                    "vol_ratio": rec.get("vol_ratio"),
                    "price":     rec.get("price"),
                })
                if not scan_ts:
                    scan_ts = rec.get("ts")
        except Exception:
            pass

    minutes_ago = None
    if scan_ts:
        try:
            dt = datetime.fromisoformat(scan_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            minutes_ago = round((datetime.now(timezone.utc) - dt).total_seconds() / 60, 1)
        except Exception:
            pass

    return {
        "available":   True,
        "ts":          scan_ts,
        "scan_id":     last_scan_id,
        "minutes_ago": minutes_ago,
        "signals":     signals,
    }


def gather() -> dict:
    _load_env()
    cfg     = _load_config()
    symbols = cfg.get("symbols", [])
    max_slots = int((cfg.get("capital") or {}).get("max_slots", 3))
    scan_interval = int((cfg.get("execution") or {}).get("scan_interval_minutes", 60))

    result: dict = {
        "ts":            _now_iso(),
        "account":       None,
        "positions":     [],
        "slots":         None,
        "recent_trades": [],
        "last_scan":     {},
        "health":        {},
        "error":         None,
    }

    try:
        from binance_bot.binance_client  import BinanceClient   # type: ignore
        from binance_bot.binance_adapter import BinanceAdapter   # type: ignore

        client  = BinanceClient.from_env()
        adapter = BinanceAdapter(client)

        # Account balance
        balance = adapter.get_account_balance()
        result["account"] = {
            "total_usdt":  round(float(balance.total_equity),   2),
            "free_usdt":   round(float(balance.available_cash), 2),
            "locked_usdt": round(float(balance.total_equity - balance.available_cash), 2),
        }

        # Open positions
        positions, held = _get_positions(adapter, symbols)
        result["positions"] = positions
        free_symbols = [s for s in symbols if s not in held]
        result["slots"] = {
            "used":         len(held),
            "max":          max_slots,
            "held_symbols": held,
            "free_symbols": free_symbols,
        }

        # Recent trades (API fills merged with local JSONL)
        result["recent_trades"] = _recent_trades_from_api(adapter, symbols, limit=20)

    except Exception as exc:
        result["error"] = str(exc)

    result["last_scan"] = _last_scan(scan_interval)
    result["health"]    = _bot_health(cfg)

    return result


if __name__ == "__main__":
    data = gather()
    print(json.dumps(data, indent=None, default=str))
