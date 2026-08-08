"""
send_test_email.py
-------------------
Test the ATOS email notification system.

Usage:
    python send_test_email.py

Requires config/email.json to be set up. See config/email.json.template for instructions.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atos.notifier import _send, _wrap, _load_cfg
from datetime import date

def main():
    cfg = _load_cfg()
    if not cfg:
        print()
        print("  config/email.json not found.")
        print("  Steps to set up:")
        print("    1. Go to https://myaccount.google.com/apppasswords")
        print("    2. Create App Password for 'ATOS Trading'")
        print("    3. Copy config/email.json.template -> config/email.json")
        print("    4. Paste the 16-char app password into sender_password")
        print("    5. Run this script again")
        print()
        sys.exit(1)

    print(f"  Sending test email to {cfg['recipient_email']} ...")

    body = f"""
    <span style="display:inline-block;padding:4px 12px;border-radius:20px;
      font-size:12px;font-weight:700;background:#16a34a22;color:#4ade80;
      border:1px solid #16a34a">SYSTEM OK</span>

    <p style="color:#e2e8f0;margin-top:16px">
      Your ATOS email notifications are working correctly.
    </p>

    <div style="display:flex;gap:16px;margin:16px 0;flex-wrap:wrap">
      <div style="background:#0f172a;border-radius:8px;padding:12px 16px;flex:1;min-width:120px">
        <div style="font-size:11px;color:#64748b;text-transform:uppercase">Config</div>
        <div style="font-size:16px;font-weight:700;color:#f1f5f9;margin-top:4px">email.json</div>
      </div>
      <div style="background:#0f172a;border-radius:8px;padding:12px 16px;flex:1;min-width:120px">
        <div style="font-size:11px;color:#64748b;text-transform:uppercase">Sender</div>
        <div style="font-size:16px;font-weight:700;color:#f1f5f9;margin-top:4px">{cfg['sender_email']}</div>
      </div>
      <div style="background:#0f172a;border-radius:8px;padding:12px 16px;flex:1;min-width:120px">
        <div style="font-size:11px;color:#64748b;text-transform:uppercase">Date</div>
        <div style="font-size:16px;font-weight:700;color:#f1f5f9;margin-top:4px">{date.today()}</div>
      </div>
    </div>

    <p style="color:#94a3b8;font-size:13px">
      You will receive emails when:<br>
      &bull; <strong>Blend strategy</strong> changes its weekly target tickers (every Friday)<br>
      &bull; <strong>Reversion strategy</strong> finds a dip-buy entry signal<br>
      &bull; <strong>Reversion position</strong> closes (exit reason + P&L shown)
    </p>
    """

    ok = _send(
        subject=f"ATOS Test Email — System Online [{date.today()}]",
        html=_wrap("Test Email — ATOS Notifications Active", body),
    )
    if ok:
        print(f"  Done. Check {cfg['recipient_email']}")
    else:
        print("  Send failed — check config/email.json credentials")

if __name__ == "__main__":
    main()
