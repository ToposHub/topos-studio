"""E12: Deliver 菜单六项宿主回调。

审计实测：Export Current Source/Settings/Timeline/Composite、Queue
Start/Stop 六个 MenuActionDescriptor 因宿主回调缺失被 MenuBuilder 静默
跳过，Deliver 菜单只剩"导出字幕 (SRT)..."。

回归锁定：module.py 注册的全部 callback_name 都能在宿主 mixin 上解析
（MenuBuilder 的 resolver 语义 = getattr）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.app.controllers.deliver_controller import DeliverControllerMixin

_MODULE = (
    Path(__file__).resolve().parents[2] / "src" / "features" / "deliver" /
    "module.py"
)

EXPECTED_CALLBACKS = [
    "on_export_current_source",
    "on_export_settings",
    "on_export_timeline",
    "on_export_composite_graph",
    "on_queue_start",
    "on_queue_stop",
]


class TestDeliverMenuCallbacks:
    def test_all_registered_callbacks_resolve(self) -> None:
        """module.py 注册的每个 callback_name 必须在宿主上可解析。"""
        text = _MODULE.read_text(encoding="utf-8")
        callback_names = set(re.findall(r'callback_name="(\w+)"', text))
        assert callback_names, "descriptor 解析失败"
        missing = [
            name for name in callback_names
            if not callable(getattr(DeliverControllerMixin, name, None))
        ]
        assert not missing, f"Deliver 菜单回调缺失: {missing}"

    def test_six_audited_callbacks_exist(self) -> None:
        for name in EXPECTED_CALLBACKS:
            assert callable(getattr(DeliverControllerMixin, name, None)), name
