"""Deformer base — F13：非破坏变形器基类与栈（纯 numpy）。

设计目标（master_plan §16 / §21.16）：
- 非破坏：原始 MeshData 不被改写，变形结果来自运行时 evaluate。
- 可叠加：一个 Model3D 可挂多个 deformer，按顺序执行。
- 可限制：通过 FieldSpec 控制每顶点权重（局部影响）。
- 可开关：每个 deformer 有 enable / strength。

核心数据结构：
    DeformerSpec  — 单个变形器参数（类型/强度/轴/field 等）
    DeformerStack — 变形器列表（按顺序串联）
    DeformerResult — 变形后的顶点+法线（含是否重算法线标志）

执行顺序（master_plan §16.3）：
    source mesh → animation transform → skinning → deformer stack
                → normal update → VBO upload → render

本模块只定义「数据结构 + 执行框架」；具体 Bend/Twist/Bulge 在 cpu.py。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from ..fields import FieldSpec
from ..primitives import MeshData


# 变形器类型标识（DeformerSpec.deformer_type 取值）
DEFORMER_BEND = "bend"
DEFORMER_TWIST = "twist"
DEFORMER_BULGE = "bulge"
DEFORMER_TAPER = "taper"
DEFORMER_STRETCH = "stretch"
DEFORMER_NOISE = "noise"
DEFORMER_SHEAR = "shear"
DEFORMER_SPHERIFY = "spherify"


@dataclass
class DeformerSpec:
    """单个变形器参数（master_plan §16.8）。

    Attributes:
        deformer_type: DEFORMER_* 常量（bend/twist/bulge/...）。
        enabled: 是否启用（False 时跳过，原样输出）。
        strength: 总强度（0=无变形，1=标准，>1=夸张）。
        space: "object" / "world"（变形坐标系；object=局部轴）。
        axis: "x" / "y" / "z"（变形主轴）。
        center: (3,) 变形中心（对象/世界空间）。
        params: 额外参数（angle/radius/frequency/seed 等，因类型而异）。
        field: 可选 FieldSpec（None=均匀影响所有顶点）。
        selection_set_id: 可选选择集 id（限制到选中顶点，F3 联动）。
    """
    deformer_type: str = DEFORMER_BEND
    enabled: bool = True
    strength: float = 1.0
    space: str = "object"
    axis: str = "y"
    center: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=np.float32))
    params: dict = field(default_factory=dict)
    field: Optional[FieldSpec] = None
    selection_set_id: str = ""

    def __post_init__(self) -> None:
        self.center = np.asarray(self.center, dtype=np.float32).reshape(3)

    def signature(self) -> tuple:
        """变形器内容签名（缓存 key 用）。

        包含类型/强度/轴/中心/params/field 的稳定哈希。field 用其 dataclass 字段。
        """
        field_sig = None
        if self.field is not None:
            f = self.field
            field_sig = (f.field_type, f.center.tobytes(), f.axis.tobytes(),
                         float(f.radius), f.size.tobytes(), f.falloff, bool(f.invert))
        return (
            self.deformer_type, bool(self.enabled), float(self.strength),
            str(self.space), str(self.axis), self.center.tobytes(),
            tuple(sorted(self.params.items())) if self.params else (),
            field_sig,
        )


@dataclass
class DeformerResult:
    """变形器 evaluate 的输出。

    Attributes:
        vertices: (N,3) 变形后顶点（float32）。
        normals: (N,3) 变形后法线（float32）。
        recomputed_normals: 是否重算了法线（False=近似继承原法线）。
    """
    vertices: np.ndarray
    normals: np.ndarray
    recomputed_normals: bool = False


@dataclass
class DeformerStack:
    """变形器栈（按顺序串联执行）。

    master_plan §16.4：一个 Model3D 挂多个 deformer，按 list 顺序依次 evaluate。
    排序变化会改变结果（变形不满足交换律）。
    """
    deformers: List[DeformerSpec] = field(default_factory=list)

    def signature(self) -> tuple:
        """栈签名（缓存 key 用）。"""
        return tuple(d.signature() for d in self.deformers)

    def is_empty(self) -> bool:
        """是否为空栈（无变形器或全部禁用）。"""
        return not any(d.enabled for d in self.deformers)

    def evaluate(self, mesh: MeshData,
                 selection_mask: Optional[np.ndarray] = None) -> DeformerResult:
        """按顺序执行所有启用的变形器。

        Args:
            mesh: 源 MeshData（不被修改）。
            selection_mask: (N,) bool 选择集掩码（None=全部顶点）。
                selection_set_id 非空的 deformer 只影响 mask=True 的顶点。

        Returns:
            DeformerResult：最终变形后的顶点+法线。
        """
        if self.is_empty():
            return DeformerResult(
                vertices=mesh.vertices.copy(),
                normals=mesh.normals.copy(),
                recomputed_normals=False,
            )
        verts = mesh.vertices.astype(np.float32, copy=True)
        norms = mesh.normals.astype(np.float32, copy=True)
        for spec in self.deformers:
            if not spec.enabled:
                continue
            # 导入放函数内避免循环依赖（cpu.py 导入本模块）
            from .cpu import evaluate_deformer
            # AUD-8707: selection_set_id 非空的变形器只影响 mask=True 的
            # 顶点；selection_set_id 为空的变形器作用于全部顶点（不继承
            # 其他变形器的选择集），与 docstring 承诺的 F3 联动语义一致。
            mask = selection_mask if spec.selection_set_id else None
            verts, norms = evaluate_deformer(spec, verts, norms, mask)
        # AUD-8706: 如实上报法线状态。栈内变形器从不按面重算法线——
        # Bend/Twist 旋转法线、Taper/Stretch 做 inverse-transpose 变换
        # （AUD-8702）、Bulge/Noise 近似继承原法线——没有一个是"重算"。
        # 旧代码无条件 True 会让依赖该标志跳过 §16.3 normal update 的
        # 调用方带着失实状态渲染；统一 False，重算决策交还调用方。
        return DeformerResult(vertices=verts, normals=norms,
                              recomputed_normals=False)
