"""材质状态缓存 — 减少每帧重复 texture bind / filter-wrap / uniform 设置（Milestone D / P3）。

对应 master_plan_2026-07-07.md §7 (P3)。Scene3D 每帧、每视图、每 mesh 重复做：
1. 设 PBR 标量 uniform（u_albedo/u_metallic/roughness/emissive）
2. 调 _bind_null_material_samplers
3. 调 _bind_material_textures（bind + filter/wrap）
4. 设 u_has_*_map
5. render 后解绑
四视图下这些重复多次，Python 调用 + OpenGL state change 都是瓶颈。

本模块提供：
- MaterialStateKey：稳定可哈希的材质状态 key（材质 id + 纹理 path + filter/wrap/
  mode + override glo）。用于"同材质连续绘制跳过重复 bind"。
- MaterialStateCache：记录"上一次成功绑定的材质 key"，连续同 key 跳过 rebind。
  缓存范围：单个 Scene3D 节点的一次 _do_evaluate（per-frame-per-view）。
  跨帧失效（每帧 reset），跨节点独立（每节点一个实例）。

设计原则：
- 只缓存"无副作用"的状态（uniform + texture bind）。GL context 状态（VAO/FBO/
  DEPTH_TEST）不在此缓存职责内。
- 纹理 override glo 变化必须进 key（外部 TextureResult 纹理换了 = 不同材质）。
- 缓存命中只是跳过重复 bind，不改渲染结果（视觉等价）。
- 透明材质不参与排序/批处理（master_plan §7.4 风险1，第一阶段不优化透明）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


# ──────────────────────────────────────────────────────────────────────────
# 材质状态 Key
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MaterialStateKey:
    """稳定可哈希的材质状态 key。

    用于判定"连续两个 mesh 是否用了完全相同的材质 + 纹理 + 采样参数"。
    若 key 相同，则第二个 mesh 可跳过 _bind_material_textures +
    _bind_null_material_samplers + u_has_*_map 设置（它们已是正确状态）。

    字段：
        material_id: MaterialData 的稳定 id（用 id() 或 content hash）。None=无材质。
        base_color_tex / normal_tex / orm_tex / ao_tex / emissive_tex:
            各 slot 的贴图路径 + mtime（导入材质 TextureDescriptor）。None=无贴图。
        texture_filter / texture_wrap: 采样参数（linear/nearest/mipmap × repeat/clamp/mirror）。
        material_mode: imported/override/mix（影响 bind 顺序）。
        override_glos: 外部 TextureResult override 的 (port_name, glo) 元组排序后
            的 tuple。外部纹理换了 = 不同 key。
        emissive_intensity / normal_format: 影响渲染的材质参数。
        has_tangent: per-mesh tangent 可用性（AUD-8905）。u_has_normal_map
            取决于 mesh 的 has_tangent 标志（Model3D 的 compute_tangents
            失败时该 mesh 为 False），两个 mesh 共享同一 MaterialData 但
            tangent 可用性不同时，key 必须区分——否则缓存命中会让后者
            沿用前者的 u_has_normal_map（未定义顶点属性采样/法线贴图
            静默失效）。
    """
    material_id: Optional[int] = None
    base_color_tex: Optional[Tuple[str, float]] = None
    normal_tex: Optional[Tuple[str, float]] = None
    orm_tex: Optional[Tuple[str, float]] = None
    ao_tex: Optional[Tuple[str, float]] = None
    emissive_tex: Optional[Tuple[str, float]] = None
    texture_filter: str = "mipmap"
    texture_wrap: str = "repeat"
    material_mode: str = "imported"
    override_glos: Tuple = ()
    emissive_intensity: float = 1.0
    normal_format: str = "OpenGL"
    has_tangent: bool = True

    @classmethod
    def from_material(cls, mat, texture_params: dict,
                      override_glos: Optional[dict] = None,
                      has_tangent: bool = True) -> "MaterialStateKey":
        """从 MaterialData + 材质参数 + 外部 override 构建 key。

        mat: MaterialData（导入材质，含 *_tex TextureDescriptor），可 None。
        texture_params: {material_mode, texture_filter, texture_wrap,
                         emissive_intensity, normal_format, ...}。
        override_glos: {port_name: glo}（外部 TextureResult override）。
        has_tangent: 当前 mesh 的 tangent 可用性（per-mesh 状态，进 key；
            见 MaterialStateKey.has_tangent 注释，AUD-8905）。
        """
        def _tex_tuple(descriptor) -> Optional[Tuple[str, float]]:
            """TextureDescriptor → (path, mtime) 元组（None=无）。"""
            if descriptor is None:
                return None
            path = getattr(descriptor, "path", "") or ""
            if not path:
                return None
            # mtime 不在 descriptor 里（loader 解析时算）；用 path 做近似 key。
            # path 相同即视为同一贴图（mtime 变化极少，且 loader 有自己的 mtime 缓存）。
            return (path,)

        mat_id = id(mat) if mat is not None else None
        # override_glos → 排序后的 tuple（dict 不可哈希）
        og = tuple(sorted((override_glos or {}).items())) if override_glos else ()
        return cls(
            material_id=mat_id,
            base_color_tex=_tex_tuple(getattr(mat, "base_color_tex", None)),
            normal_tex=_tex_tuple(getattr(mat, "normal_tex", None)),
            orm_tex=_tex_tuple(getattr(mat, "orm_tex", None)),
            ao_tex=_tex_tuple(getattr(mat, "ao_tex", None)),
            emissive_tex=_tex_tuple(getattr(mat, "emissive_tex", None)),
            texture_filter=str(texture_params.get("texture_filter", "mipmap")),
            texture_wrap=str(texture_params.get("texture_wrap", "repeat")),
            material_mode=str(texture_params.get("material_mode", "imported")),
            override_glos=og,
            emissive_intensity=float(texture_params.get("emissive_intensity", 1.0)),
            normal_format=str(texture_params.get("normal_format", "OpenGL")),
            has_tangent=bool(has_tangent),
        )


# ──────────────────────────────────────────────────────────────────────────
# 材质状态缓存（per-Scene3D-node，per-frame）
# ──────────────────────────────────────────────────────────────────────────

class MaterialStateCache:
    """记录上一次绑定的材质 key，连续同 key 跳过 rebind。

    生命周期：一次 _do_evaluate（含一个视图的 mesh 循环）。
    每帧开头 reset()；每视图开头也可 reset()（避免跨视图状态串场，§7.4 风险2）。
    命中：当前 key == _last_key → 调用方跳过 _bind_material_textures 等。
    未命中：调用方正常 bind，然后 update(key)。

    注意：material_switches 统计仍按"实际 bind 次数"计（命中时不算 switch），
    这样 perf stats 能反映缓存收益（P0 的 texture_binds/material_switches 下降）。
    """

    __slots__ = ("_last_key", "_hits", "_misses")

    def __init__(self) -> None:
        self._last_key: Optional[MaterialStateKey] = None
        self._hits: int = 0
        self._misses: int = 0

    def reset(self) -> None:
        """每帧/每视图开头清空缓存状态（不重置统计，统计供 perf 用）。"""
        self._last_key = None

    def matches(self, key: MaterialStateKey) -> bool:
        """当前 key 是否与上一次绑定的相同（命中=可跳过 rebind）。"""
        if self._last_key is not None and self._last_key == key:
            self._hits += 1
            return True
        return False

    def update(self, key: MaterialStateKey) -> None:
        """记录本次绑定的 key（未命中后调用）。"""
        self._last_key = key
        self._misses += 1

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return (self._hits / total) if total > 0 else 0.0
