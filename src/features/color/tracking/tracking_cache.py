"""Compatibility shim (A1 tracking migration).

The implementation moved to ``src.shared.tracking.tracking_cache`` so both the Color
page and the Composite page can use it.  This module re-exports everything
so existing ``src.features.color.tracking.tracking_cache`` imports keep working.
"""

from src.shared.tracking.tracking_cache import *  # noqa: F401,F403
from src.shared.tracking import tracking_cache as _impl  # noqa: F401

# Re-export private/underscore names too (some callers reach into helpers).
for _name in dir(_impl):
    if _name not in dir():
        globals()[_name] = getattr(_impl, _name)
