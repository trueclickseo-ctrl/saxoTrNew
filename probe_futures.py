"""Find exact UICs and asset types for futures instruments in Saxo SIM."""
import sys
sys.path.insert(0, r"E:\SaxoTrNew\SaxoTrNew")
import requests, saxo_auth

BASE = "https://gateway.saxobank.com/sim/openapi"
HDR  = {"Authorization": f"Bearer {saxo_auth.get_valid_access_token()}"}

def get(path, **params):
    r = requests.get(f"{BASE}{path}", headers=HDR, params=params, timeout=15)
    return r.json()

print("=== ContractFutures search ===")
for kw in ["Gold", "Crude", "WTI", "Treasury", "T-Note", "Bond", "NASDAQ", "S&P", "EUR"]:
    resp = get("/ref/v1/instruments", Keywords=kw, AssetTypes="ContractFutures", **{"$top": 3})
    hits = resp.get("Data", [])
    if hits:
        print(f"\n'{kw}':")
        for h in hits:
            print(f"  UIC={h.get('Identifier'):<12}  Sym={h.get('Symbol','?'):<15}  "
                  f"{h.get('Description','?')}")

print("\n\n=== CfdOnIndex — all available ===")
resp = get("/ref/v1/instruments", AssetTypes="CfdOnIndex", **{"$top": 20})
for h in resp.get("Data", []):
    print(f"  UIC={h.get('Identifier'):<8}  Sym={h.get('Symbol','?'):<20}  "
          f"{h.get('Description','?')}")

print("\n\n=== CfdOnEtc — commodity ETCs ===")
for kw in ["Gold", "Oil", "Silver", "Platinum", "Copper"]:
    resp = get("/ref/v1/instruments", Keywords=kw, AssetTypes="CfdOnEtc", **{"$top": 2})
    hits = resp.get("Data", [])
    if hits:
        print(f"\n'{kw}':")
        for h in hits:
            print(f"  UIC={h.get('Identifier'):<10}  Sym={h.get('Symbol','?'):<20}  "
                  f"{h.get('Description','?')}")

print("\n\n=== FxSpot — commodity / index spots ===")
for kw in ["XAU", "XAG", "OIL", "XPT"]:
    resp = get("/ref/v1/instruments", Keywords=kw, AssetTypes="FxSpot", **{"$top": 2})
    hits = resp.get("Data", [])
    if hits:
        print(f"\n'{kw}':")
        for h in hits:
            print(f"  UIC={h.get('Identifier'):<8}  Sym={h.get('Symbol','?'):<15}  "
                  f"{h.get('Description','?')}")
