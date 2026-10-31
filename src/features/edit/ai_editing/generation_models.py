"""Compatibility shim (C1 migration): moved to shared.

The implementation lives in src/shared/ai_models/generation/generation_models.py so composite can
use generation without importing edit.  Every historical consumer keeps importing this path unchanged (the shim swaps
this module entry for the shared implementation, so private
names resolve too).
"""
import sys as _sys

from src.shared.ai_models.generation import generation_models as _impl

_sys.modules[__name__] = _impl
