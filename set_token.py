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
saved that 1-char string and printed "valid ~24h" without checking.
Now: shape-check the paste, write it to a STAGING file, make one real
Saxo /port/v1/users/me call, and ONLY on success atomically promote the
staging file to saxo_token.json. Nothing is reported as saved and the
working token file is never touched until Saxo has accepted the token.
Paste with right-click or Ctrl+Shift+V, not Ctrl+V.
"""
import json
import os
import time
import getpass

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


def _verify_against_saxo(token_path: str) -> tuple[bool, str]:
    """Point saxo_auth at `token_path` and make one real /port/v1/users/me
    call. Restores the original path afterwards no matter what."""
    try:
        import saxo_auth
        import saxo_client as sc
        orig = saxo_auth._ENV_CONFIG["sim"]["token_file"]
        saxo_auth._ENV_CONFIG["sim"]["token_file"] = token_path
        try:
            me = sc.test_connection(env="sim")
            return True, f"{me.get('Name', '?')} (UserId {me.get('UserId', '?')})"
        finally:
            saxo_auth._ENV_CONFIG["sim"]["token_file"] = orig
    except Exception as e:
        return False, str(e)[:200]


def main() -> int:
    tok = getpass.getpass("Paste Saxo 24h SIM access token (input hidden), then Enter: ").strip()
    if not tok:
        print("No token entered — aborted, existing file untouched.")
        return 1

    shape_err = _looks_like_token(tok)
    if shape_err:
        print(f"\n  ✗ Rejected — NOT saved: token {shape_err}")
        print("  Your existing saxo_token.json is untouched.")
        return 1

    # Write to a STAGING file first. The real saxo_token.json is never
    # touched until Saxo has actually accepted this token, so a bad paste
    # can neither be reported as success nor clobber a working file.
    staging = PATH + ".staging"
    with open(staging, "w") as f:
        json.dump({
            "access_token": tok,
            "token_type": "Bearer",
            "obtained_at": time.time(),
            "expires_in": 86400,   # Saxo Developer Portal 24h SIM token
        }, f, indent=2)

    ok, detail = _verify_against_saxo(staging)
    if not ok:
        os.remove(staging)
        print(f"\n  ✗ Saxo rejected this token — NOT saved: {detail}")
        print("  Your existing saxo_token.json is untouched.")
        print("  Get a fresh 24h token from https://www.developer.saxo (SIM app → 24h token) and re-run.")
        return 1

    os.replace(staging, PATH)                       # atomic promote
    try:
        os.chmod(PATH, 0o600)
    except (AttributeError, OSError):
        pass
    print(f"\n  ✓ Verified against Saxo and saved — {detail}")
    print(f"  {PATH}  (valid ~24h). Ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
