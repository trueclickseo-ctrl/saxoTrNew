import sys, os
# saxo_etf_strategy/ must be first so its 'core' and 'config' win
# over the parent project's same-named packages.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for p in (_HERE, _PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)
