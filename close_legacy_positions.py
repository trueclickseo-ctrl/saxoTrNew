"""
close_legacy_positions.py
--------------------------
Closes the 4 legacy Nordic positions (PRX, NIBE-B, HEXA-B, HM-B) that were
left on the Saxo SIM account from before ATOS was restructured.  These are
NOT tracked by the ATOS engine — closing them cleans up the account so the
dashboard equity reflects only the 15k US Blend sleeve.

Run:
    python close_legacy_positions.py

You will see a preview of what will be sold and must type 'yes' to confirm.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json, requests, time

TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'saxo_token.json')
SIM_BASE   = 'https://gateway.saxobank.com/sim/openapi/'

def _load_token():
    try:
        d = json.load(open(TOKEN_FILE))
        if time.time() > d.get('obtained_at', 0) + d.get('expires_in', 0) - 60:
            print("Token expired. Run: python set_token.py"); sys.exit(1)
        return d['access_token']
    except FileNotFoundError:
        print("No token file. Run: python set_token.py"); sys.exit(1)

def _headers(tok): return {'Authorization': f'Bearer {tok}'}

def _get_positions(tok):
    r = requests.get(SIM_BASE + 'port/v1/positions/me',
        headers=_headers(tok),
        params={'FieldGroups': 'PositionBase,DisplayAndFormat'},
        timeout=10)
    r.raise_for_status()
    return r.json().get('Data', [])

def _get_account_key(tok):
    r = requests.get(SIM_BASE + 'port/v1/accounts/me', headers=_headers(tok), timeout=10)
    r.raise_for_status()
    return r.json()['Data'][0]['AccountKey']

def _place_sell(tok, account_key, uic, asset_type, amount, symbol):
    body = {
        'AccountKey': account_key,
        'AssetType': asset_type,
        'Uic': uic,
        'Amount': amount,
        'OrderType': 'Market',
        'BuySell': 'Sell',
        'OrderDuration': {'DurationType': 'DayOrder'},
        'ManualOrder': False,
    }
    r = requests.post(SIM_BASE + 'trade/v2/orders', headers={**_headers(tok), 'Content-Type': 'application/json'},
                      json=body, timeout=30)
    return r.status_code, r.json()

def main():
    tok = _load_token()

    # Legacy symbols we want to close (Saxo format)
    LEGACY = {'PRX:xams', 'NIBE_B:xome', 'HEXAb:xome', 'HMb:xome'}

    positions = _get_positions(tok)
    targets = []
    for p in positions:
        disp  = p.get('DisplayAndFormat', {})
        pbase = p.get('PositionBase', {})
        sym   = disp.get('Symbol', '')
        if sym in LEGACY:
            targets.append({
                'symbol':     sym,
                'description': disp.get('Description', '?'),
                'uic':        pbase.get('Uic'),
                'asset_type': pbase.get('AssetType', 'Stock'),
                'amount':     pbase.get('Amount', 0),
            })

    if not targets:
        print("No legacy positions found on account — already clean.")
        return

    print("\nLegacy positions to close:")
    for t in targets:
        print(f"  {t['symbol']:20s}  {t['description']:35s}  {t['amount']} shares")

    print("\nThis will place MARKET SELL orders on Saxo SIM (paper money only).")
    confirm = input("Type 'yes' to confirm: ").strip().lower()
    if confirm != 'yes':
        print("Aborted."); return

    account_key = _get_account_key(tok)
    for t in targets:
        status, resp = _place_sell(tok, account_key, t['uic'], t['asset_type'], t['amount'], t['symbol'])
        if status in (200, 201):
            print(f"  OK  {t['symbol']} — order placed")
        else:
            print(f"  FAIL {t['symbol']} — {status}: {resp}")

    print("\nDone. Refresh the dashboard to see updated positions.")

if __name__ == '__main__':
    main()
