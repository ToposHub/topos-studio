"""Metal compute 子包（计划 P4-01）。

`MetalComputeContext`：MSL 编译 / dispatch / buffer 与 texture 绑定 /
finish 同步。PyObjC 路线（K4 复核结论），见 `context.py` 模块文档。

子模块布局：

- `context.py`：设备 / 命令队列 / kernel 缓存 / dispatch 与同步（线程
  与阻塞契约见该模块 docstring）。
- `kernels*.py`：MSL kernel 源与 numpy 参考实现（blur / erode /
  histogram / 颜色转换 / DCT / 编解码 / 视频）。
- `submission_scheduler.py`：有界异步提交调度（completion-driven
  资源回收与显式背压）。
"""

from src.shared.gpu.metal.context import (
    MetalComputeContext,
    MetalComputeError,
    MetalKernel,
)

__all__ = [
    "MetalComputeContext",
    "MetalComputeError",
    "MetalKernel",
]
