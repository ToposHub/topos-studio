# F403 noqa 说明（P2-3c，2026-10-28）：icon_manager 未定义 ``__all__``
# （103+ 公共名，star 的有效导出面含传递导入名，随其内部 import 演进），
# 静态展开清单无法与之保持等价；仓库内消费者均直接 import 子模块
# ``src.shared.assets.icon_manager``，此聚合仅为兼容潜在外部引用而保留。
from src.shared.assets.icon_manager import *  # noqa: F403
