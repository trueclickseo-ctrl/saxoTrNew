"""
autochartist_pattern_stats.py
------------------------------
Fetch + tabulate Autochartist's public "Performance Stats" for a broker
(https://component.autochartist.com/performancestats-v2/?broker_id=<id>).

WHAT THIS IS: a rolling ~12-month AGGREGATE hit-rate table -- of all the
chart patterns Autochartist identified for this broker's instrument set,
what fraction reached their forecast level within one pattern length.
Broken down by asset class, pattern name, bar interval, direction, hour
of day, and a few internal quality scores (clarity / quality / initial
trend / breakout strength / symmetry).

WHAT THIS IS NOT: it is NOT a live signal feed. There are no instrument-
level entries / targets / stops / timestamps here -- just counts. The
tradeable Autochartist signal stream is a separate, authenticated product
(api.autochartist.com, bundled with a Saxo account via SaxoTraderGO), not
this widget endpoint.

USE IN ATOS: reference / calibration only, in the same observe-only spirit
as the AI journal -- e.g. "Autochartist's Forex 'Triangle' breakouts ran
~65% to forecast this year" is a prior you can sanity-check a future
pattern-based idea against. Nothing here feeds a trade decision.

Usage:
    python reports/autochartist_pattern_stats.py                # broker 492, pretty table
    python reports/autochartist_pattern_stats.py --broker 123
    python reports/autochartist_pattern_stats.py --json OUT.json # save raw payload
    python reports/autochartist_pattern_stats.py --csv OUT.csv   # tidy long CSV
    python reports/autochartist_pattern_stats.py --offline PATH  # parse a saved payload
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request

_URL = "https://component.autochartist.com/performancestats-v2/resources/broker/v2?broker_id={bid}"
_DEFAULT_BROKER = 492  # the id in the link this was built from

# the sub-tables worth showing by default (the payload has more)
_CATS = ["overall", "pattern", "interval", "direction", "hourofday",
         "significant", "quality", "clarity"]


def fetch(broker_id: int = _DEFAULT_BROKER, timeout: int = 20) -> dict:
    req = urllib.request.Request(_URL.format(bid=broker_id),
                                 headers={"User-Agent": "atos-reference/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def rows(payload: dict):
    """Flatten to (asset_class, phase, category, bucket, total, correct, pct)."""
    for g in payload.get("group", []):
        asset = g.get("name", "?")
        for phase in ("breakout", "emerging"):
            block = g.get(phase)
            if not isinstance(block, dict):
                continue
            for cat in block.get("category", []):
                cname = cat.get("name", "?")
                for d in cat.get("data", []):
                    total = int(d.get("total", 0) or 0)
                    correct = int(d.get("correct", 0) or 0)
                    pct = (correct / total * 100) if total else 0.0
                    yield (asset, phase, cname, str(d.get("value", "")), total, correct, pct)


def _print_table(payload: dict):
    win = payload.get("from", "?"), payload.get("to", "?")
    print(f"\nAutochartist Performance Stats -- broker {payload.get('brokerId')}  "
          f"({win[0][:10]} -> {win[1][:10]})")
    print("Aggregate hit-rate: fraction of identified patterns that reached "
          "forecast within one pattern length.\n")

    by = {}
    for asset, phase, cat, bucket, total, correct, pct in rows(payload):
        by.setdefault((asset, phase, cat), []).append((bucket, total, correct, pct))

    for asset in dict.fromkeys(a for a, _, _ in by):
        print(f"{'=' * 66}\n {asset}\n{'=' * 66}")
        for phase in ("breakout", "emerging"):
            for cat in _CATS:
                key = (asset, phase, cat)
                if key not in by:
                    continue
                print(f"\n  [{phase}] {cat}")
                data = by[key]
                # keep numeric buckets in numeric order, else by hit-rate desc
                try:
                    data.sort(key=lambda t: float(t[0]))
                except ValueError:
                    data.sort(key=lambda t: -t[3])
                for bucket, total, correct, pct in data:
                    if total < 20 and cat not in ("overall",):
                        continue  # tiny samples are noise
                    bar = "#" * round(pct / 4)
                    print(f"    {bucket:<26} n={total:>7,}  {pct:5.1f}%  {bar}")
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--broker", type=int, default=_DEFAULT_BROKER)
    ap.add_argument("--json", metavar="PATH", help="save the raw payload here")
    ap.add_argument("--csv", metavar="PATH", help="save a tidy long-format CSV here")
    ap.add_argument("--offline", metavar="PATH", help="parse a saved payload instead of fetching")
    a = ap.parse_args(argv)

    if a.offline:
        with open(a.offline, encoding="utf-8") as f:
            payload = json.load(f)
    else:
        try:
            payload = fetch(a.broker)
        except Exception as exc:  # noqa: BLE001
            print(f"fetch failed: {exc}", file=sys.stderr)
            return 1

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
        print(f"wrote {a.json}")
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["asset_class", "phase", "category", "bucket", "total", "correct", "hit_rate_pct"])
            for r in rows(payload):
                w.writerow([r[0], r[1], r[2], r[3], r[4], r[5], f"{r[6]:.2f}"])
        print(f"wrote {a.csv}")
    if not (a.json or a.csv):
        _print_table(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
