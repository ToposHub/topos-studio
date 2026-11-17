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

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from typing import Any

_PLUGIN_DIR = (Path(__file__).resolve().parents[3]
               / "external" / "file_explorer" / "plugins" / "decode_benchmark")


def load_benchmark_module() -> Any:
    """加载既有跑分模块（受管插件目录；不复制实现、不走旧 HTTP bridge）。

    模块内有相对导入（``.benchmark``）——必须作为**包成员**加载：把插件
    目录注册为合成包 ``files_host_plugins.decode_benchmark`` 再导入其
    ``benchmark_gui``，避免与旧宿主的全局包名冲突。
    """
    gui_name = "files_host_plugins.decode_benchmark.benchmark_gui"
    if gui_name in sys.modules:
        return sys.modules[gui_name]
    package_name = "files_host_plugins.decode_benchmark"
    if package_name not in sys.modules:
        package_spec = importlib.machinery.ModuleSpec(package_name, None,
                                                      is_package=True)
        package_spec.submodule_search_locations = [str(_PLUGIN_DIR)]
        package = importlib.util.module_from_spec(package_spec)
        package.__path__ = [str(_PLUGIN_DIR)]
        sys.modules[package_name] = package
        parent_name = "files_host_plugins"
        if parent_name not in sys.modules:
            parent_spec = importlib.machinery.ModuleSpec(parent_name, None,
                                                         is_package=True)
            parent_spec.submodule_search_locations = [str(_PLUGIN_DIR.parent)]
            parent = importlib.util.module_from_spec(parent_spec)
            parent.__path__ = [str(_PLUGIN_DIR.parent)]
            sys.modules[parent_name] = parent
    spec = importlib.util.spec_from_file_location(
        gui_name, _PLUGIN_DIR / "benchmark_gui.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载既有跑分实现：{_PLUGIN_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[gui_name] = module
    spec.loader.exec_module(module)
    return module


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
        return window_type(paths=paths)

    def request_cancel(self, window: Any) -> None:
        """请求取消（帧边界协作）——关闭路径调用，无遗留解码。"""
        worker = getattr(window, "_worker", None)
        if worker is not None and hasattr(worker, "cancel"):
            worker.cancel()


__all__ = ["DecodeBenchmarkHostService", "load_benchmark_module"]
