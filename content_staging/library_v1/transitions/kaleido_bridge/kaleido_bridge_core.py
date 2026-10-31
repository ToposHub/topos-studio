"""kaleido_bridge CPU 核（暂存组件入口）。

L14/L15 集成起本模块**再导出**生产实现（src/features/edit/
transition_packs/library_v1/resample_cores/x17_kaleido_bridge）——组件测试与生产行为
同一实现，禁双份漂移；星号导入透传全部公开名（DEFAULTS/选项常量/
evaluate）。冲突记录/推导/手算向量见本目录 README 与 hand_vectors.json。
"""

from __future__ import annotations

from src.features.edit.transition_packs.library_v1.resample_cores.x17_kaleido_bridge import *  # noqa: F401,F403
from src.features.edit.transition_packs.library_v1.resample_cores.x17_kaleido_bridge import evaluate  # noqa: F401
