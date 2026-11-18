"""G4：decode_benchmark 接管——诊断 command 直接接既有 host service。

G 节行验收：**来源/时间线上下文、预算、取消、零素材写入；不借旧 bridge**。

* **直接接既有 host service**：解码跑分本体就是既有
  ``DecodeBenchmarkRunner``（PyAV 抽帧 + 帧边界协作取消 + 预算档位）与
  ``DecodeBenchmarkWindow``（QThread worker/进度/取消按钮/结果导出）——
  本模块不重写第二套跑分（生产调用约束："不新增第二套"），只做**宿主
  接线**：从受管插件目录加载既有实现，经 F5 singleton 注册表打开；
* **来源/时间线上下文**：打开时携带 Files 的真实选中数组（诊断对象明
  确），窗口内可继续拖入补充；
* **预算**：既有 ``BenchmarkConfig``（warmup/frames 档位）即预算口径；
  诊断执行零素材写入——runner 只读解码；结果导出是用户显式选择的
  输出路径（CSV/JSON），不属于素材目录写入；
* **取消**：窗口关闭即请求 worker 取消（帧边界生效）——无遗留解码。
"""
from __future__ import annotations

from importlib import import_module
from typing import Any


def load_benchmark_module() -> Any:
    """Load the single native benchmark implementation from its feature package."""
    return import_module("src.features.files.benchmark.benchmark_gui")


class DecodeBenchmarkHostService:
    """既有跑分实现的宿主适配（诊断 command；零素材写入）。"""

    def __init__(self) -> None:
        self._module: Any = None

    @property
    def module(self) -> Any:
        if self._module is None:
            self._module = load_benchmark_module()
        return self._module

    def create_window(self, parent: Any, paths: tuple[str, ...]) -> Any:
        """创建既有 ``DecodeBenchmarkWindow``（携带来源/时间线上下文）。

        零素材写入契约：窗口与 runner 只**读**解码素材；导出走用户显式
        选择的输出文件（CSV/JSON），绝不写素材目录。
        """
        window_type = self.module.DecodeBenchmarkWindow
        service = getattr(parent, "_edit_benchmark_host_service", None)
        window = window_type(paths=paths, host_service=service)
        if parent is not None:
            from PyQt6.QtCore import Qt
            window.setParent(parent, Qt.WindowType.Window)
        return window

    def request_cancel(self, window: Any) -> None:
        """请求取消（帧边界协作）——关闭路径调用，无遗留解码。"""
        worker = getattr(window, "_worker", None)
        if worker is not None and hasattr(worker, "cancel"):
            worker.cancel()


__all__ = ["DecodeBenchmarkHostService", "load_benchmark_module"]
