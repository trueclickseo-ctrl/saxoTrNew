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

def _make_body(cfg: dict, route_label: str, recipient: str) -> str:
    return f"""
    <span style="display:inline-block;padding:4px 12px;border-radius:20px;
      font-size:12px;font-weight:700;background:#16a34a22;color:#4ade80;
      border:1px solid #16a34a">SYSTEM OK</span>

    <p style="color:#e2e8f0;margin-top:16px">
      Your ATOS email notifications are working correctly.<br>
      This is a <strong>{route_label}</strong> routing test.
    </p>

    <div style="display:flex;gap:16px;margin:16px 0;flex-wrap:wrap">
      <div style="background:#0f172a;border-radius:8px;padding:12px 16px;flex:1;min-width:120px">
        <div style="font-size:11px;color:#64748b;text-transform:uppercase">Route</div>
        <div style="font-size:16px;font-weight:700;color:#f1f5f9;margin-top:4px">{route_label}</div>
      </div>
      <div style="background:#0f172a;border-radius:8px;padding:12px 16px;flex:1;min-width:120px">
        <div style="font-size:11px;color:#64748b;text-transform:uppercase">Recipient</div>
        <div style="font-size:16px;font-weight:700;color:#f1f5f9;margin-top:4px">{recipient}</div>
      </div>
      <div style="background:#0f172a;border-radius:8px;padding:12px 16px;flex:1;min-width:120px">
        <div style="font-size:11px;color:#64748b;text-transform:uppercase">Date</div>
        <div style="font-size:16px;font-weight:700;color:#f1f5f9;margin-top:4px">{date.today()}</div>
      </div>
    </div>

    <p style="color:#94a3b8;font-size:13px">
      <strong>SIM/Paper</strong> emails go to: {cfg['recipient_email']}<br>
      <strong>LIVE</strong> emails go to: {cfg.get('recipient_email_live', '(not configured)')}
    </p>
    """


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

    sim_recipient = cfg['recipient_email']
    live_recipient = cfg.get('recipient_email_live', sim_recipient)

    # --- SIM/Paper route ---
    print(f"  [1/2] Sending SIM/Paper test email to {sim_recipient} ...")
    ok1 = _send(
        subject=f"ATOS Test Email — SIM/Paper Route [{date.today()}]",
        html=_wrap("Test Email — SIM/Paper Routing", _make_body(cfg, "SIM / Paper", sim_recipient)),
        live=False,
    )
    print(f"       {'OK' if ok1 else 'FAILED'}")

    # --- LIVE route ---
    print(f"  [2/2] Sending LIVE test email to {live_recipient} ...")
    ok2 = _send(
        subject=f"ATOS Test Email — LIVE Route [{date.today()}]",
        html=_wrap("Test Email — LIVE Routing", _make_body(cfg, "LIVE", live_recipient)),
        live=True,
    )
    print(f"       {'OK' if ok2 else 'FAILED'}")

    print()
    if ok1 and ok2:
        print(f"  Both routes OK.")
        print(f"    SIM/Paper  -> {sim_recipient}")
        print(f"    LIVE       -> {live_recipient}")
    else:
        print("  One or more sends failed — check config/email.json credentials")

if __name__ == "__main__":
    main()
