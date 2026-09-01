"""
attention.py -- ONE "ATOS needs a human" alert channel.

The goal: ATOS runs itself. When it genuinely CANNOT resolve something on
its own, exactly one clear email goes out, and it keeps nagging (once a
day) until the thing is dealt with -- instead of a single line buried in a
routine digest that says "FIXED" when it wasn't.

Contract for callers (housekeeping, safeguard, forex/runner, ...):
  * raise_attention(key, title=..., detail=...) on EVERY run the condition
    is still true. Cheap; it just refreshes a timestamp.
  * clear_attention(key) the moment it resolves.
  * if a caller just stops raising a key (condition went away, no explicit
    clear), the item auto-expires after `recheck_minutes` and is reported
    resolved.

flush() reconciles the open set and sends the consolidated digest. Call it
once at the end of a cycle that had a chance to raise/clear things -- the
safeguard agents do (every 30 min). Multiple flush() calls per cycle are
harmless (idempotent: re-email is gated on last_emailed).

Never raises. No-op (logs only) without config/email.json.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_BASE, "data", "attention_state.json")
_EMAIL_CFG = os.path.join(_BASE, "config", "email.json")

RE_EMAIL_HOURS = 24            # nag cadence once an item has escalated
GRACE_MINUTES_DEFAULT = 60     # how long a condition must persist before it emails
RECHECK_MINUTES_DEFAULT = 120  # no raise in this long -> assume the condition cleared


# ── state ────────────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as exc:
        logger.warning(f"[attention] could not save state: {exc}")


# ── public API ───────────────────────────────────────────────────────────
def raise_attention(key: str, *, title: str, detail: str = "", source: str = "",
                    severity: str = "warn", grace_minutes: int = GRACE_MINUTES_DEFAULT,
                    recheck_minutes: int = RECHECK_MINUTES_DEFAULT) -> None:
    """Declare / refresh an open condition that needs a human. Idempotent."""
    try:
        now = datetime.now()
        state = _load()
        item = state.get(key) or {"first_seen": now.isoformat(), "last_emailed": None,
                                  "escalated": False}
        item.update({
            "last_seen": now.isoformat(), "title": title, "detail": detail,
            "source": source, "severity": severity,
            "grace_minutes": int(grace_minutes), "recheck_minutes": int(recheck_minutes),
            "resolved": False,
        })
        state[key] = item
        _save(state)
    except Exception as exc:
        logger.warning(f"[attention] raise_attention({key}) failed: {exc}")


def clear_attention(key: str, *, note: str = "") -> None:
    """The condition resolved. Drops the item; if it had escalated to email,
    flush() will send a one-line 'resolved' note on its next run."""
    try:
        state = _load()
        item = state.get(key)
        if not item or item.get("resolved"):
            return
        item["resolved"] = True
        item["resolved_at"] = datetime.now().isoformat()
        item["resolved_note"] = note
        state[key] = item
        _save(state)
    except Exception as exc:
        logger.warning(f"[attention] clear_attention({key}) failed: {exc}")


def open_items() -> list[dict]:
    """Currently-open (not resolved, not expired) items -- for tests/dashboards."""
    state = _load()
    now = datetime.now()
    out = []
    for key, it in state.items():
        if it.get("resolved"):
            continue
        if _expired(it, now):
            continue
        out.append({"key": key, **it})
    return out


# ── flush: reconcile + send the digest ───────────────────────────────────
def _expired(item: dict, now: datetime) -> bool:
    try:
        last = datetime.fromisoformat(item["last_seen"])
        return (now - last) > timedelta(minutes=item.get("recheck_minutes", RECHECK_MINUTES_DEFAULT))
    except Exception:
        return False


def flush(dry_run: bool = False) -> dict:
    """Expire stale items, escalate items past their grace period, and send
    ONE consolidated email if anything is due. Returns a summary dict."""
    now = datetime.now()
    state = _load()
    result = {"open": 0, "escalated": 0, "emailed": 0, "resolved": []}

    escalated_open: list[dict] = []      # every still-open item past grace (shown in the digest)
    fired_this_run = False               # a new escalation or a 24h re-nag came due
    resolved_since_alert: list[dict] = []

    for key in list(state.keys()):
        it = state[key]

        # explicit clear, or the caller simply stopped raising it
        if it.get("resolved") or _expired(it, now):
            if it.get("escalated"):
                resolved_since_alert.append({"key": key, **it})
            del state[key]
            result["resolved"].append(key)
            continue

        result["open"] += 1
        first = datetime.fromisoformat(it["first_seen"])
        past_grace = (now - first) >= timedelta(minutes=it.get("grace_minutes", GRACE_MINUTES_DEFAULT))
        if not past_grace:
            continue

        result["escalated"] += 1
        last_emailed = datetime.fromisoformat(it["last_emailed"]) if it.get("last_emailed") else None
        due = last_emailed is None or (now - last_emailed) >= timedelta(hours=RE_EMAIL_HOURS)
        if due:
            it["last_emailed"] = now.isoformat()
            it["escalated"] = True
            fired_this_run = True
        escalated_open.append({"key": key, **it})

    if not dry_run and (fired_this_run or resolved_since_alert):
        if _send_digest(escalated_open, resolved_since_alert):
            result["emailed"] = len(escalated_open)

    if not dry_run:
        _save(state)
    return result


# ── email ────────────────────────────────────────────────────────────────
def _email_cfg() -> dict | None:
    try:
        with open(_EMAIL_CFG) as f:
            return json.load(f)
    except Exception:
        return None


def _send_digest(open_items_: list[dict], resolved: list[dict]) -> bool:
    n = len(open_items_)
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    red = "\U0001F534"
    subject = (f"{red} ATOS needs a human — {n} open item(s)" if n
               else "✅ ATOS — attention item(s) resolved")

    def _rows(items):
        r = ""
        for it in items:
            age = ""
            try:
                age = _humanize(datetime.now() - datetime.fromisoformat(it["first_seen"]))
            except Exception:
                pass
            r += (f'<tr><td>{it.get("source","")}</td>'
                  f'<td><b>{it.get("title","")}</b></td>'
                  f'<td>{it.get("detail","")}</td><td>{age}</td></tr>')
        return r

    hdr = f"{red} {n} item(s) need a human decision" if n else "Attention items resolved"
    parts = [f'<h2>{hdr}</h2><p style="color:#666">{now}</p>']
    if open_items_:
        parts.append('<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">'
                     '<tr><th>Source</th><th>What</th><th>Detail</th><th>Open for</th></tr>'
                     f'{_rows(open_items_)}</table>')
    if resolved:
        parts.append('<h3 style="color:#2ea043">Resolved since the last alert</h3>'
                     '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">'
                     '<tr><th>Source</th><th>What</th><th>Note</th></tr>'
                     + "".join(f'<tr><td>{it.get("source","")}</td><td>{it.get("title","")}</td>'
                               f'<td>{it.get("resolved_note","") or "cleared"}</td></tr>' for it in resolved)
                     + '</table>')
    parts.append('<p style="color:#666;font-size:12px">ATOS resolves what it safely can on its own. '
                 'These are the things it will not touch without you. This email re-sends once a day '
                 'per item until the item clears.</p>')
    html = f'<!DOCTYPE html><html><body style="font-family:sans-serif">{"".join(parts)}</body></html>'

    cfg = _email_cfg()
    if not cfg:
        logger.info(f"[attention] no config/email.json -- would have sent: {subject}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"ATOS Attention <{cfg['sender_email']}>"
        msg["To"] = cfg["recipient_email"]
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.starttls()
            s.login(cfg["sender_email"], cfg["sender_password"])
            s.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        logger.info(f"[attention] sent: {subject}")
        return True
    except Exception as exc:
        logger.warning(f"[attention] email FAILED: {exc}")
        return False


def _humanize(td: timedelta) -> str:
    h = td.total_seconds() / 3600
    if h < 1:
        return f"{int(td.total_seconds() // 60)} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} d"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    import sys
    if "--flush" in sys.argv:
        print(flush())
    else:
        for it in open_items():
            print(f"  {it['key']:40} {it.get('title','')}")
        print(f"\n{len(open_items())} open item(s). `python attention.py --flush` to send the digest.")
