"""合成域 Metal 链（迁移计划阶段 5.1 / 5.2 部分 / 5.3）。

模块一览：

- ``roi``            tile halo / ROI 推导（含计划参考实现 5A 的 ``input_roi``）。
- ``kernels``        MSL kernel 集与参数 dtype（与 GLSL 语义逐条对齐）。
- ``node_support``   节点能力判定（未迁移 → 整图回退 + 节点 ID/类型诊断）。
- ``graph_plan``     执行计划与几何推导（CPU/Metal 双后端共享）。
- ``cpu_reference``  numpy 参考实现（对拍与无 GPU 测试）。
- ``texture_pool``   按字节计量的纹理池（GPU 完成后回收、租约键）。
- ``tile_cache``     内容寻址 tile 缓存（键含修订/帧/端口/尺寸/色彩）。
- ``renderer``       ``MetalCompositeRenderer``（发布 CompositeGpuFrameHandle）。

启用开关：``TOPOS_COMPOSITE_METAL=1``（默认关闭；另需本机 Metal compute
可用）。``CompositeGraphRenderService.render_metal`` 是生产入口。
"""

from .node_support import (
    METAL_COMPOSITE_SUPPORTED_TYPES,
    MetalSupportIssue,
    MetalSupportReport,
    evaluate_metal_support,
)
from .roi import (
    HaloPropagation,
    input_roi,
    iter_tile_rects,
    node_halo_pixels,
    propagate_input_halos,
    transform_input_rect,
)

__all__ = [
    "METAL_COMPOSITE_SUPPORTED_TYPES",
    "MetalSupportIssue",
    "MetalSupportReport",
    "evaluate_metal_support",
    "HaloPropagation",
    "input_roi",
    "iter_tile_rects",
    "node_halo_pixels",
    "propagate_input_halos",
    "transform_input_rect",
]
