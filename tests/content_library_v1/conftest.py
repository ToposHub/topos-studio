"""tests/content_library_v1 —— L 线内容库自有测试。

只测暂存区组件（content_staging/library_v1/**），不改父 conftest/共享夹具。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
