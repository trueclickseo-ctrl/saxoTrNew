"""
Regression test -- 2026-09-01 token liveness check.

Incident: `set_token.py`'s hidden prompt got a Ctrl+V (which types \x16 in
a raw terminal, not a paste), saved {"access_token": "\x16",
"expires_in": 86400}, and nothing noticed. `get_valid_access_token` only
does time math on the file's own `expires_in`, so it returned the 1-char
string for hours; the keepalive called that and logged "SIM token OK" 24
times while every real scan failed with TOKEN EXPIRED.

Fixes:
  * set_token.py -- shape check (len, JWT-ish, no control/space chars) +
    a real Saxo /users/me call before it declares success; a failing
    token never clobbers a working saxo_token.json.
  * saxo_auth.get_valid_access_token -- rejects a structurally malformed
    stored access_token instead of returning it.
  * saxo_{sim,live}_token_keepalive -- run_once() now does a live
    /port/v1/users/me call, so "keepalive OK" means the token actually
    works.
"""

import ast
import inspect
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

G, R, Y, X, B = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_res = []


def _run(n, f):
    try:
        f()
        _res.append((n, True, None))
    except Exception as e:
        import traceback
        _res.append((n, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


import set_token
import saxo_auth
import saxo_sim_token_keepalive as sk
import saxo_live_token_keepalive as lk

_GOOD = "eyJ" + "a" * 400 + ".b" * 40 + ".c" * 40   # JWT-ish, long, two dots... fix dots below
_GOOD = "eyJhbGciOiJ" + "x" * 400 + "." + "y" * 40 + "." + "z" * 40


# ── set_token._looks_like_token ────────────────────────────────────────
def test_looks_like_token_rejects_bad_paste():
    assert set_token._looks_like_token("\x16") is not None
    assert set_token._looks_like_token("ab cd ef") is not None          # space
    assert set_token._looks_like_token("short") is not None             # too short
    assert set_token._looks_like_token("x" * 300) is not None           # not a JWT
    assert set_token._looks_like_token("eyJ" + "x" * 300) is not None   # no dots


def test_looks_like_token_accepts_a_real_shape():
    assert set_token._looks_like_token(_GOOD) is None


def test_set_token_stages_then_verifies_then_promotes():
    src = inspect.getsource(set_token)
    assert "_verify_against_saxo(" in src and "test_connection" in src
    assert '".staging"' in src and "os.replace(staging, PATH)" in src
    assert "_looks_like_token(tok)" in src
    # the real file must not be written before the verify passes
    i_write = src.index('open(staging, "w")')
    i_verify = src.index("_verify_against_saxo(staging)")
    i_promote = src.index("os.replace(staging, PATH)")
    assert i_write < i_verify < i_promote
    # failure path removes the staging file and does NOT touch PATH
    fail_block = src[src.index("if not ok:"):src.index("os.replace(staging, PATH)")]
    assert "os.remove(staging)" in fail_block and "NOT saved" in fail_block


# ── saxo_auth.get_valid_access_token guard ─────────────────────────────
def _with_token_file(payload, fn):
    p = saxo_auth._cfg("sim")["token_file"] + ".liveness_test"
    with open(p, "w") as fh:
        json.dump(payload, fh)
    orig = saxo_auth._ENV_CONFIG["sim"]["token_file"]
    saxo_auth._ENV_CONFIG["sim"]["token_file"] = p
    try:
        return fn()
    finally:
        saxo_auth._ENV_CONFIG["sim"]["token_file"] = orig
        os.path.exists(p) and os.remove(p)


def test_get_valid_access_token_rejects_malformed():
    for bad in ("\x16", "short", "has space " + "x" * 200):
        payload = {"access_token": bad, "token_type": "Bearer",
                   "obtained_at": time.time(), "expires_in": 86400}
        try:
            _with_token_file(payload, lambda: saxo_auth.get_valid_access_token(env="sim"))
            raise AssertionError(f"should have raised for {bad!r}")
        except RuntimeError as e:
            assert "malformed" in str(e)


def test_get_valid_access_token_passes_a_well_formed_unexpired_token():
    payload = {"access_token": _GOOD, "token_type": "Bearer",
               "obtained_at": time.time(), "expires_in": 86400}
    got = _with_token_file(payload, lambda: saxo_auth.get_valid_access_token(env="sim"))
    assert got == _GOOD


# ── keepalives do a real liveness call ─────────────────────────────────
def test_both_keepalives_call_test_connection():
    for mod, env in ((sk, "sim"), (lk, "live")):
        src = inspect.getsource(mod.run_once)
        assert f'saxo_client.test_connection(env="{env}")' in src, mod.__name__
        assert "get_valid_access_token" in src           # still does the refresh first
        assert "_send_alert" in src


def test_keepalive_run_once_fails_when_live_check_fails():
    import saxo_client
    o_gvat = saxo_auth.get_valid_access_token
    o_tc = saxo_client.test_connection
    o_alert = sk._send_alert
    sent = []
    saxo_auth.get_valid_access_token = lambda env="sim": _GOOD          # "refresh" ok
    saxo_client.test_connection = lambda env="sim": (_ for _ in ()).throw(RuntimeError("401"))
    sk._send_alert = lambda d: sent.append(d)
    try:
        assert sk.run_once() is False
        assert sent and "401" in sent[0]
    finally:
        saxo_auth.get_valid_access_token = o_gvat
        saxo_client.test_connection = o_tc
        sk._send_alert = o_alert


def test_keepalive_run_once_ok_when_both_pass():
    import saxo_client
    o_gvat = saxo_auth.get_valid_access_token
    o_tc = saxo_client.test_connection
    saxo_auth.get_valid_access_token = lambda env="sim": _GOOD
    saxo_client.test_connection = lambda env="sim": {"Name": "Tester", "UserId": "1"}
    try:
        assert sk.run_once() is True
    finally:
        saxo_auth.get_valid_access_token = o_gvat
        saxo_client.test_connection = o_tc


def test_modules_parse():
    for m in (set_token, saxo_auth, sk, lk):
        ast.parse(inspect.getsource(m))


# ── 2026-09-03: portal-24h-token (no refresh_token) detection ──────────
def test_wrong_token_type_flags_a_portal_token_and_passes_a_pkce_one():
    o_load = saxo_auth._load_tokens
    try:
        # A Developer-Portal 24h token: no refresh_token, expires_in 86400.
        saxo_auth._load_tokens = lambda env="sim": {
            "access_token": _GOOD, "expires_in": 86400, "obtained_at": time.time()}
        msg = sk._wrong_token_type()
        assert msg and "PKCE" in msg and "set_token" in msg
        # A proper PKCE token: has a refresh_token -> not flagged.
        saxo_auth._load_tokens = lambda env="sim": {
            "access_token": _GOOD, "expires_in": 1180,
            "refresh_token": "x" * 40, "obtained_at": time.time()}
        assert sk._wrong_token_type() is None
    finally:
        saxo_auth._load_tokens = o_load


def test_portal_token_makes_the_failure_alert_explicit():
    import saxo_client
    o_load = saxo_auth._load_tokens
    o_gvat = saxo_auth.get_valid_access_token
    o_tc = saxo_client.test_connection
    o_alert = sk._send_alert
    sent = []
    saxo_auth._load_tokens = lambda env="sim": {
        "access_token": _GOOD, "expires_in": 86400, "obtained_at": time.time()}
    saxo_auth.get_valid_access_token = lambda env="sim": _GOOD
    saxo_client.test_connection = lambda env="sim": (_ for _ in ()).throw(RuntimeError("401"))
    sk._send_alert = lambda d: sent.append(d)
    try:
        assert sk.run_once() is False
        assert sent and "401" in sent[0] and "PKCE" in sent[0]   # both, not one
    finally:
        saxo_auth._load_tokens = o_load
        saxo_auth.get_valid_access_token = o_gvat
        saxo_client.test_connection = o_tc
        sk._send_alert = o_alert


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

print(f"\n{B}{'=' * 66}{X}")
bad = [(n, e) for n, ok, e in _res if not ok]
for n, ok, e in _res:
    print(f"  [{G}PASS{X}]" if ok else f"  [{R}FAIL{X}]", n)
    if e:
        print(f"      {Y}{e}{X}")
print(f"{B}{'=' * 66}{X}")
if bad:
    print(f"{R}{B}  {len(bad)} / {len(_res)} FAILED{X}")
    sys.exit(1)
print(f"{G}{B}  ALL {len(_res)} TESTS PASSED{X}")
sys.exit(0)
