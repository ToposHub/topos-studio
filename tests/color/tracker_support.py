"""Compatibility shim (A1 tracking migration).

tracker_support moved to ``tests.shared.tracking.tracker_support`` together
with the algorithm tests; this re-export keeps the remaining Color-page
test imports working.
"""

from tests.shared.tracking.tracker_support import *  # noqa: F401,F403
from tests.shared.tracking import tracker_support as _impl  # noqa: F401

for _name in dir(_impl):
    if _name not in dir():
        globals()[_name] = getattr(_impl, _name)
