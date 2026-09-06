"""
forex_smoke_test.py — daily pre-flight import check for forex/runner.py.

Runs at 05:30 PKT (before the 06:00 AI-twin scan, the earliest daily task).
Tries to import forex.runner; if it fails, sends an email alert immediately
so a module-level bug is caught once rather than repeated across all 7 tasks.

Exit code 0 = OK, 1 = import failed (watchdog will catch this too).
"""
import sys
import os
import json
import smtplib
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))


def _load_email_cfg() -> dict | None:
    p = os.path.join(BASE, "config", "email.json")
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def _send_alert(subject: str, body: str) -> None:
    cfg = _load_email_cfg()
    if not cfg:
        print(f"[smoke-test] no config/email.json — would have sent: {subject}", file=sys.stderr)
        return
    try:
        html = f"<pre style='font-family:monospace'>{body}</pre>"
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"ATOS Smoke Test <{cfg['sender_email']}>"
        msg["To"]      = cfg["recipient_email"]
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.starttls()
            s.login(cfg["sender_email"], cfg["sender_password"])
            s.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        print(f"[smoke-test] alert sent: {subject}")
    except Exception as exc:
        print(f"[smoke-test] email FAILED: {exc}", file=sys.stderr)


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[smoke-test] {ts} — importing forex.runner …")

    os.chdir(BASE)
    sys.path.insert(0, BASE)

    try:
        import forex.runner  # noqa: F401
        print("[smoke-test] OK — forex.runner imported cleanly")
        return 0
    except Exception:
        tb = traceback.format_exc()
        print(f"[smoke-test] FAILED:\n{tb}", file=sys.stderr)
        _send_alert(
            subject=f"[ATOS] forex.runner import FAILED — ALL forex tasks will crash ({ts})",
            body=(
                f"forex/runner.py failed to import at {ts}.\n"
                f"ALL 7+ scheduled forex tasks will fail until this is fixed.\n\n"
                f"Traceback:\n{tb}"
            ),
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
