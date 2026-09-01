"""
set_token.py
------------
Securely save a Saxo 24h SIM access token to saxo_token.json.
Paste the token at the prompt — input is HIDDEN and never goes into the chat, the
shell history, or anywhere else. Run:

    python set_token.py

Then run the engine (or tell the assistant it's ready).

2026-09-01: the paste at the hidden prompt is fragile -- Ctrl+V in a raw
terminal inserts a literal \x16 instead of pasting, and the old script
saved that 1-char string without checking. It is now validated three
ways before it overwrites the existing file:
  1. shape  -- long enough, JWT-looking (two dots), no control/space chars
  2. live   -- an actual Saxo /port/v1/users/me call must succeed
  3. safety -- a failing token never clobbers a currently-working one
Paste with right-click or Ctrl+Shift+V, not Ctrl+V.
"""
import json
import os
import time
import getpass
import shutil

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saxo_token.json")


def _looks_like_token(tok: str) -> str | None:
    """Return an error string if the token is obviously not a Saxo JWT."""
    if any(ord(c) < 32 or c == " " for c in tok):
        return ("contains a space or control character — you probably hit Ctrl+V "
                "(which types \\x16 in a raw terminal). Paste with right-click "
                "or Ctrl+Shift+V instead.")
    if len(tok) < 100:
        return f"only {len(tok)} chars — a real Saxo access token is ~500."
    if tok.count(".") != 2 or not tok.startswith("eyJ"):
        return "not shaped like a JWT (expected 'eyJ....<dot>....<dot>....')."
    return None


def _live_check() -> tuple[bool, str]:
    try:
        import saxo_client as sc
        me = sc.test_connection(env="sim")
        return True, f"{me.get('Name', '?')} (UserId {me.get('UserId', '?')})"
    except Exception as e:
        return False, str(e)[:200]


def main() -> int:
    tok = getpass.getpass("Paste Saxo 24h SIM access token (input hidden), then Enter: ").strip()
    if not tok:
        print("No token entered — aborted, existing file untouched.")
        return 1

    shape_err = _looks_like_token(tok)
    if shape_err:
        print(f"\n  ✗ Rejected: token {shape_err}")
        print("  Existing saxo_token.json left untouched.")
        return 1

    backup = None
    if os.path.exists(PATH):
        backup = PATH + ".prev"
        shutil.copy(PATH, backup)

    with open(PATH, "w") as f:
        json.dump({
            "access_token": tok,
            "token_type": "Bearer",
            "obtained_at": time.time(),
            "expires_in": 86400,   # Saxo Developer Portal 24h SIM token
        }, f, indent=2)
    try:
        os.chmod(PATH, 0o600)
    except (AttributeError, OSError):
        pass

    ok, detail = _live_check()
    if ok:
        print(f"\n  ✓ Saved and verified against Saxo — {detail}")
        print(f"  {PATH}  (valid ~24h). Ready.")
        if backup:
            os.remove(backup)
        return 0

    # live check failed -- roll back to the previous file if we had one
    print(f"\n  ✗ Saved, but Saxo rejected it: {detail}")
    if backup:
        shutil.move(backup, PATH)
        print("  Rolled back to the previous saxo_token.json (which may also be stale).")
    else:
        print("  Left the new file in place (there was no previous one to restore).")
    print("  Get a fresh 24h token from https://www.developer.saxo (SIM app → 24h token) and re-run.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
