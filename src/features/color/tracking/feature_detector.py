"""Compatibility shim (A1 tracking migration).

The implementation moved to ``src.shared.tracking.feature_detector`` so both the Color
page and the Composite page can use it.  This module re-exports everything
so existing ``src.features.color.tracking.feature_detector`` imports keep working.
"""

from src.shared.tracking.feature_detector import *  # noqa: F401,F403
from src.shared.tracking import feature_detector as _impl  # noqa: F401

# Re-export private/underscore names too (some callers reach into helpers).
# AUD-24801: 仅拷贝 API 命名——跳过 dunder（避免把实现模块的 ``__all__``
# 列表对象拷入垫片）与模块别名（np/logging 等第三方导入不再泄漏进命名空间）。
import types as _types  # noqa: E402

for _name in dir(_impl):
    if _name.startswith("__"):
        continue
    if isinstance(getattr(_impl, _name), _types.ModuleType):
        continue
    if _name not in dir():
        globals()[_name] = getattr(_impl, _name)
del _types
