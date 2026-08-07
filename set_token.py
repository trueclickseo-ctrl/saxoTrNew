"""
set_token.py
------------
Securely save a Saxo 24h access token to saxo_token.json.
Paste the token at the prompt — input is HIDDEN and never goes into the chat, the
shell history, or anywhere else. Run:

    python set_token.py

Then run the engine (or tell the assistant it's ready).
"""
import json, time, getpass, os

tok = getpass.getpass("Paste Saxo 24h access token (input hidden), then press Enter: ").strip()
if not tok:
    print("No token entered — aborted.")
    raise SystemExit(1)

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saxo_token.json")
with open(path, "w") as f:
    json.dump({
        "access_token": tok,
        "token_type": "Bearer",
        "obtained_at": time.time(),
        "expires_in": 86400,   # 24h
    }, f, indent=2)
print(f"Saved to {path} (valid ~24h). Ready — run the engine or tell the assistant.")
