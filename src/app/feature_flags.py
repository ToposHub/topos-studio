"""Backward-compatible wrapper — canonical API is src.shared.feature_flags.

All callers should migrate to import from src.shared.feature_flags.
"""
from functools import lru_cache

from src.shared.feature_flags import (
    load_feature_flags,
    DEFAULT_FEATURE_FLAGS,
)

# Legacy API compatibility
DEFAULT_FEATURES = DEFAULT_FEATURE_FLAGS


# AUD-3200: this shim has no in-repo callers and sits on no hot path, but it
# used to re-read ~4 env vars and rebuild the dict on every single call.
# ``lru_cache`` makes repeated calls reuse one snapshot; callers that need to
# observe runtime TOPOS_FEATURE_* changes should call ``cache_clear()`` or
# import the canonical src.shared.feature_flags API instead.
@lru_cache(maxsize=1)
def get_enabled_features() -> dict[str, bool]:
    flags = load_feature_flags()
    return {f: flags.is_enabled(f) for f in DEFAULT_FEATURE_FLAGS}


@lru_cache(maxsize=None)
def is_feature_enabled(name: str) -> bool:
    return load_feature_flags().is_enabled(name)
