"""
push_ai_dashboard.py — reads local ATOS databases and pushes a live
summary to the AI SIM dashboard artifact db via `claude -p`.

Scheduled: Task Scheduler every 30 min (run_push_ai_dashboard.bat).
Log: data/push_ai_dashboard.log
"""
from __future__ import annotations
import json, os, sqlite3, subprocess, sys
from datetime import datetime, timezone

BASE         = os.path.dirname(os.path.abspath(__file__))
ARTIFACT_URL = "https://claude.ai/code/artifact/fe13027d-7e60-41b2-91fa-67d01257efd1"
LOG          = os.path.join(BASE, "data", "push_ai_dashboard.log")
# Full path so Task Scheduler (limited PATH) can find the CLI
_CLAUDE_CMD  = r"C:\Users\Kwaseem\AppData\Roaming\npm\claude.cmd"
AI_SINCE     = "2026-09-03"   # forex A/B started


# ── helpers ────────────────────────────────────────────────────────────────

def _con(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c

def _pnl_stats(cur, module: str, strategy: str | None = None, since: str | None = None) -> dict:
    q = "SELECT realized_pnl FROM trades WHERE module=? AND status='closed'"
    p: list = [module]
    if strategy:
        q += " AND strategy=?"; p.append(strategy)
    if since:
        q += " AND timestamp_open>=?"; p.append(since)
    rows = [r["realized_pnl"] for r in cur.execute(q, p).fetchall() if r["realized_pnl"] is not None]
    if not rows:
        return {"closed": 0, "wins": 0, "wr": 0.0, "pf": 0.0, "net": 0.0}
    wins = sum(1 for v in rows if v > 0)
    gw   = sum(v for v in rows if v > 0)
    gl   = abs(sum(v for v in rows if v <= 0))
    return {
        "closed": len(rows), "wins": wins,
        "wr":  round(wins / len(rows) * 100, 1),
        "pf":  round(gw / gl, 2) if gl else 0.0,
        "net": round(sum(rows), 1),
    }

def _open_count(cur, module: str) -> int:
    return cur.execute(
        "SELECT COUNT(*) FROM trades WHERE module=? AND status='open'", [module]
    ).fetchone()[0]

def _open_by_strat(cur, module: str) -> dict:
    rows = cur.execute(
        "SELECT strategy, COUNT(*) c FROM trades WHERE module=? AND status='open' GROUP BY strategy",
        [module]
    ).fetchall()
    return {r["strategy"]: r["c"] for r in rows}

def _per_strat_closed(cur, module: str) -> list:
    rows = cur.execute("""
        SELECT strategy, COUNT(*) total,
               SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
               SUM(CASE WHEN realized_pnl>0 THEN realized_pnl ELSE 0 END) gw,
               ABS(SUM(CASE WHEN realized_pnl<=0 THEN realized_pnl ELSE 0 END)) gl,
               SUM(realized_pnl) net
        FROM trades WHERE module=? AND status='closed'
        GROUP BY strategy ORDER BY net DESC
    """, [module]).fetchall()
    out = []
    for r in rows:
        d = dict(r); t = d["total"] or 1
        d["wr"]  = round((d["wins"] or 0) / t * 100, 1)
        d["pf"]  = round((d["gw"] or 0) / (d["gl"] or 1), 2) if d["gl"] else 0.0
        d["net"] = round(d["net"] or 0, 1)
        out.append(d)
    return out


# ── data collection ─────────────────────────────────────────────────────────

def collect() -> dict:
    con = _con(os.path.join(BASE, "data", "pnl_ledger.db"))
    cur = con.cursor()

    # ── Forex ──
    fd = _pnl_stats(cur, "forex",    since=AI_SINCE)
    fa = _pnl_stats(cur, "forex_ai", since=AI_SINCE)
    fd["open"] = _open_count(cur, "forex")
    fa["open"] = _open_count(cur, "forex_ai")
    fa["open_by_strat"] = _open_by_strat(cur, "forex_ai")
    fa_strats  = _per_strat_closed(cur, "forex_ai")

    verdict_diff = round((fa["net"] or 0) - (fd["net"] or 0), 1)

    # ── Stocks blend ──
    sb_d = _pnl_stats(cur, "stock",    "US Blend")
    sb_d["open"] = cur.execute(
        "SELECT COUNT(*) FROM trades WHERE module='stock' AND strategy='US Blend' AND status='open'"
    ).fetchone()[0]
    sb_a = _pnl_stats(cur, "stock_ai", "US Blend")
    sb_a["open"] = _open_count(cur, "stock_ai")

    # ── Stocks reversion ──
    sr_d = _pnl_stats(cur, "stock",    "US Reversion")
    sr_d["open"] = cur.execute(
        "SELECT COUNT(*) FROM trades WHERE module='stock' AND strategy='US Reversion' AND status='open'"
    ).fetchone()[0]
    sr_a = _pnl_stats(cur, "stock_ai", "US Reversion")

    con.close()

    # ── IBKR ──
    ibkr: dict = {"open": 0, "closed": 0, "blend": 0, "scorer": 0}
    ibkr_path = os.path.join(BASE, "data", "ibkr_stocks.db")
    if os.path.exists(ibkr_path):
        try:
            ic  = _con(ibkr_path); icur = ic.cursor()
            icur.execute("SELECT COUNT(*) FROM trades WHERE filled_at IS NOT NULL AND status='open'")
            ibkr["open"]   = icur.fetchone()[0]
            icur.execute("SELECT COUNT(*) FROM trades WHERE status='closed'")
            ibkr["closed"] = icur.fetchone()[0]
            for r in icur.execute(
                "SELECT strategy, COUNT(*) c FROM trades WHERE status='open' GROUP BY strategy"
            ).fetchall():
                s = (r["strategy"] or "").lower()
                if "blend"   in s: ibkr["blend"]  = r["c"]
                if "scorer"  in s or "portfolio" in s: ibkr["scorer"] = r["c"]
            ic.close()
        except Exception as e:
            print(f"IBKR read error: {e}", file=sys.stderr)

    # ── Outcome predictor model ──
    model: dict = {}
    rpt = os.path.join(BASE, "data", "trade_outcome_model", "report.json")
    if os.path.exists(rpt):
        r = json.loads(open(rpt).read())
        model = {
            "enabled":     True,
            "trained_at":  r.get("trained_at", ""),
            "n_train":     r.get("n_train",  0),
            "n_test":      r.get("n_test",   0),
            "n_total":     r.get("n_total",  0),
            "accuracy":    r.get("test_accuracy",  0),
            "win_prec":    r.get("test_win_prec",  0),
            "loss_prec":   r.get("test_loss_prec", 0),
            "base_wr":     r.get("base_win_rate",  0),
            "lift":        r.get("lift",            0),
            "features":    r.get("top_features",   []),
            "next_retrain": "2026-09-14T22:00:00+05:00",
        }

    now = datetime.now(timezone.utc).isoformat()

    return {
        "stats/forex": {
            "det_closed": fd["closed"], "det_wins": fd["wins"],
            "det_wr": fd["wr"], "det_pf": fd["pf"],
            "det_net": fd["net"], "det_open": fd["open"],
            "ai_closed": fa["closed"], "ai_wins": fa["wins"],
            "ai_wr": fa["wr"], "ai_pf": fa["pf"],
            "ai_net": fa["net"], "ai_open": fa["open"],
            "ai_open_by_strat": fa["open_by_strat"],
            "copilot_applied": 85, "copilot_total": 98,
            "verdict_diff": verdict_diff,
        },
        "forex/strategies": {"items": fa_strats},
        "stats/stocks-blend": {
            "det_closed": sb_d["closed"], "det_wins": sb_d["wins"],
            "det_wr": sb_d["wr"], "det_pf": sb_d["pf"],
            "det_net": sb_d["net"], "det_open": sb_d["open"],
            "ai_closed": sb_a["closed"], "ai_open": sb_a["open"],
            "det_holdings": ["AES","DELL","FTNT","HUM","MPC","PEN","STT","U"],
            "ai_holdings":  ["AES","DELL","HUM","PEN","STT","U"],
            "dropped":      ["FTNT","MPC"],
        },
        "stats/stocks-reversion": {
            "det_closed": sr_d["closed"], "det_wins": sr_d["wins"],
            "det_wr": sr_d["wr"], "det_pf": sr_d["pf"],
            "det_net": sr_d["net"], "det_open": sr_d["open"],
            "ai_closed": sr_a["closed"], "ai_open": sr_a.get("open", 0),
            "evolver": {
                "RSI_ENTRY": 38, "RSI_EXIT": 60, "DIP_PCT": 0.05,
                "VOL_MULT": 1.5, "MAX_HOLD_DAYS": 10,
            },
        },
        "stats/ibkr":  ibkr,
        "stats/model": model,
        "meta/last-push": {"at": now, "source": "push_ai_dashboard.py", "v": "1"},
    }


# ── push via claude -p ───────────────────────────────────────────────────────

def push(docs: dict) -> bool:
    writes = []
    for path, data in docs.items():
        col, doc_id = path.rsplit("/", 1)
        writes.append({"op": "set", "collection": col, "doc_id": doc_id, "data": data})

    prompt = (
        f"Write these documents to the AI SIM dashboard artifact db "
        f"(url: {ARTIFACT_URL}). "
        f"Use write_db with db_op=batch and this writes array:\n"
        f"{json.dumps(writes, separators=(',', ':'))}"
    )

    claude_exe = _CLAUDE_CMD if os.path.exists(_CLAUDE_CMD) else "claude"
    result = subprocess.run(
        [claude_exe, "-p", prompt, "--model", "claude-haiku-4-5-20251001"],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=180, cwd=BASE,
    )
    if result.returncode != 0:
        print(f"[ERROR] claude -p failed (rc={result.returncode}): {result.stderr[:400]}", file=sys.stderr)
        return False
    print(f"[OK] pushed {len(writes)} docs")
    return True


# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] push_ai_dashboard.py starting")
    try:
        docs = collect()
        ok   = push(docs)
        print(f"[{ts}] {'OK' if ok else 'FAILED'}")
        sys.exit(0 if ok else 1)
    except Exception as exc:
        import traceback
        print(f"[{ts}] EXCEPTION: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
