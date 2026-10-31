"""分类调色复制/粘贴剪贴板（P1-COLOR-16，DaVinci Paste Attributes 语义）。

专业调色工作流中，"复制调色"不是全有或全无：调色员常需要从一个镜头
只取 primary + windows，或只取 qualifier。本模块提供类别化的 grade
剪贴板：

- ``GRADE_CATEGORIES``：类别注册表（类别键 → Layer 序列化 dict 键集合），
  与 DaVinci 的 Paste Attributes 分类对齐（primary/curves/HSL/mixer/
  warper/effects/LUT/soft clip/qualifier/windows/plugins + 栈级
  tracking/RAW）。
- ``GradeClipboard``：按类别提取图层数据快照、应用到目标图层、描述
  变更清单（供粘贴预览）。

使用方式：
    clip = GradeClipboard()
    clip.copy_grade(layers=engine.layers,
                    trackings=stack.trackings,
                    raw_params=stack.raw_params)      # 默认全类别
    clip.paste_onto_layers(target_layers, categories={"primary", "windows"})
    clip.describe()                                   # 变更清单

复制存储的是每层 ``to_dict`` 快照的类别子集（deepcopy 隔离源）；粘贴走
``Layer.apply_grade_patch``（与 from_dict 同一解析路径：tuple 重建、
LUT 重算、ndarray 恢复），未选类别不被触碰——满足"不相关字段不被意外
覆盖"的验收标准。

线程边界：纯内存数据结构，无自有线程或锁；复制/粘贴均在调用方线程
（通常为 UI 线程）同步执行。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set

import numpy as np

from src.features.color.core.layer import Layer, MaskGroup


def _iter_mask_shapes(items: Iterable[Any]) -> Iterator[Any]:
    """递归展平 mask_shapes（含 MaskGroup 嵌套）为 MaskShape 序列。"""
    for item in items or ():
        if isinstance(item, MaskGroup):
            yield from _iter_mask_shapes(item.children)
        else:
            yield item


# ── 类别注册表 ───────────────────────────────────────────────────────────
#
# 类别键 → 该类别在 Layer.to_dict 产物中的 dict 键集合。
# 必须与 Layer.apply_grade_patch 的应用白名单保持一致。

GRADE_CATEGORY_KEYS: Dict[str, Sequence[str]] = {
    "primary": ("grading_params",),
    "curves": ("curves_data",),
    "hsl": ("hsl_curve_data",),
    "mixer": ("mixer_params",),
    "warper": ("warper_params",),
    "effects": ("effects_params",),
    "lut": ("active_lut_path", "lut_intensity"),
    "soft_clip": ("soft_clip_params",),
    "qualifier": ("qualifier_params",),
    "windows": ("mask_shapes",
                "ai_matte_operation", "ai_matte_enabled", "ai_matte_file"),
    "plugins": ("plugins",),
}

#: UI 展示名（含 i18n 键名，按中文标签直出）
GRADE_CATEGORY_LABELS: Dict[str, str] = {
    "primary": "基础调色 (Primary)",
    "curves": "自定义曲线 (Curves)",
    "hsl": "HSL 曲线",
    "mixer": "RGB 混色器 (Mixer)",
    "warper": "色彩变形 (Warper)",
    "effects": "效果 (Effects)",
    "lut": "LUT",
    "soft_clip": "软剪裁 (Soft Clip)",
    "qualifier": "限定器 (Qualifier)",
    "windows": "窗口/蒙版 (Windows)",
    "plugins": "插件效果",
    "tracking": "跟踪数据 (Tracking)",
    "raw": "RAW 解码参数",
}

def grade_category_label(cat: str) -> str:
    """类别 UI 展示名（i18n 优先，GRADE_CATEGORY_LABELS 作缺省回退）。"""
    from src.shared.i18n.i18n import I18N
    return I18N.tr(f"gradecat.{cat}", GRADE_CATEGORY_LABELS.get(cat, cat))


#: 层级类别（应用到每个目标图层）
LAYER_CATEGORIES: Set[str] = set(GRADE_CATEGORY_KEYS)

#: 栈级类别（应用到目标 clip 的调色栈整体）
STACK_CATEGORIES: Set[str] = {"tracking", "raw"}

#: 全类别
ALL_CATEGORIES: Set[str] = LAYER_CATEGORIES | STACK_CATEGORIES


def _filter_category_keys(layer_dict: Dict[str, Any],
                          categories: Iterable[str]) -> Dict[str, Any]:
    """提取 layer.to_dict 产物中属于给定类别的键子集。"""
    wanted: Set[str] = set()
    for cat in categories:
        wanted.update(GRADE_CATEGORY_KEYS.get(cat, ()))
    return {k: copy.deepcopy(v) for k, v in layer_dict.items() if k in wanted}


@dataclass
class GradeClipboard:
    """类别化调色剪贴板（内存态、单槽）。

    Attributes:
        layer_patches: 每层的类别子集快照（与源层数量一致，按索引对应）。
        bitmap_masks: 每层 BITMAP 蒙版的位图数据快照（``{shape_id: ndarray}``，
            与 layer_patches 按索引对齐；AUD-0511——``to_dict(None)`` 不落盘
            bitmap_data，位图经侧存储携带、粘贴时回填）。
        trackings: 栈级跟踪数据快照（类别 "tracking"）。
        raw_params: 栈级 RAW 解码参数快照（类别 "raw"）。
        copied_categories: 复制时实际捕获的类别（源为空数据的类别剔除，
            使粘贴预览只列出真正会变化的项）。
        source_layer_count: 源图层总数（结构对齐校验用）。
    """

    layer_patches: List[Dict[str, Any]] = field(default_factory=list)
    bitmap_masks: List[Dict[str, np.ndarray]] = field(default_factory=list)
    trackings: Optional[Dict[str, Any]] = None
    raw_params: Optional[Dict[str, Any]] = None
    copied_categories: Set[str] = field(default_factory=set)
    source_layer_count: int = 0

    # ── 复制 ────────────────────────────────────────────────────────

    def copy_grade(self, layers: Sequence[Layer],
                   trackings: Optional[Dict[str, Any]] = None,
                   raw_params: Optional[Dict[str, Any]] = None,
                   categories: Optional[Iterable[str]] = None) -> None:
        """按类别捕获一个调色栈的快照。

        Args:
            layers: 源图层列表（快照 to_dict 的类别子集，deepcopy 隔离）。
            trackings: 栈级跟踪数据（engine.tracking_store 的序列化形式）。
            raw_params: 栈级 RAW 参数（engine.current_raw_params）。
            categories: 要捕获的类别；None = 全类别。不在注册表中的
                类别被忽略。
        """
        cats = set(categories) if categories is not None else set(ALL_CATEGORIES)
        cats &= ALL_CATEGORIES

        self.layer_patches = []
        self.bitmap_masks = []
        self.copied_categories = set()
        self.source_layer_count = len(layers)

        layer_cats = cats & LAYER_CATEGORIES
        for layer in layers:
            layer_dict = layer.to_dict(None)
            patch = _filter_category_keys(layer_dict, layer_cats)
            # 剔除空值类别（未启用的 LUT/插件/蒙版不进入变更清单）
            patch = {
                k: v for k, v in patch.items()
                if v not in (None, "", [], {}, ()) or k == "lut_intensity"
            }
            self.layer_patches.append(patch)
            # AUD-0511：to_dict(None) 不落盘 BITMAP 蒙版的 bitmap_data
            # （剪贴板无 assets_dir 可写），位图按 shape.id 存入侧存储，
            # 粘贴时回填到重建的形状上，避免位图蒙版静默失效。
            bitmaps: Dict[str, np.ndarray] = {}
            if "windows" in layer_cats:
                for shape in _iter_mask_shapes(getattr(layer, "mask_shapes", ())):
                    if (getattr(shape, "type", None) == "bitmap"
                            and shape.bitmap_data is not None):
                        bitmaps[shape.id] = shape.bitmap_data.copy()
            self.bitmap_masks.append(bitmaps)

        # 汇总实际非空的类别
        present_keys: Set[str] = set()
        for patch in self.layer_patches:
            present_keys.update(patch.keys())
        for cat, keys in GRADE_CATEGORY_KEYS.items():
            if any(k in present_keys for k in keys):
                self.copied_categories.add(cat)

        if "tracking" in cats and trackings:
            self.trackings = copy.deepcopy(trackings)
            self.copied_categories.add("tracking")
        else:
            self.trackings = None

        if "raw" in cats and raw_params:
            self.raw_params = copy.deepcopy(raw_params)
            self.copied_categories.add("raw")
        else:
            self.raw_params = None

    # ── 粘贴 ────────────────────────────────────────────────────────

    def paste_onto_layers(self, target_layers: Sequence[Layer],
                          categories: Optional[Iterable[str]] = None) -> int:
        """把快照应用到目标图层列表。

        源/目标层数不一致时按索引对齐（超出部分忽略——DaVinci 语义：
        单层源复制到多层目标时，只有结构匹配的第一层拿到对应数据，
        但分类参数通常来自 layer 0；这里采用保守的索引对齐 + 源只有
        一层时广播到全部目标层）。

        Args:
            target_layers: 目标图层（原地修改，标记 dirty）。
            categories: 要应用的类别；None = 复制时捕获的全部类别。

        Returns:
            实际修改的图层数。
        """
        if not self.layer_patches or not target_layers:
            return 0
        cats = (set(categories) if categories is not None
                else set(self.copied_categories))
        cats &= LAYER_CATEGORIES
        if not cats:
            return 0

        broadcast = len(self.layer_patches) == 1 and len(target_layers) > 1
        applied = 0
        for i, target in enumerate(target_layers):
            patch_idx = 0 if broadcast else i % len(self.layer_patches)
            patch = self.layer_patches[patch_idx]
            # AUD-0510：每次应用独立 deepcopy。broadcast/取模回绕会把
            # 同一 patch dict 复用到多个目标，mask_shapes 的 points/
            # grad_points 等嵌套列表会与剪贴板快照及其他目标共享同一
            # 对象，canvas 就地编辑（如拖动曲线点）将跨目标互相污染。
            target.apply_grade_patch(copy.deepcopy(patch), cats)
            if "windows" in cats:
                self._reattach_bitmap_masks(target, patch_idx)
            applied += 1
        return applied

    def _reattach_bitmap_masks(self, target: Layer, patch_idx: int) -> None:
        """AUD-0511：把快照中的 BITMAP 蒙版位图回填到目标层重建的形状。

        ``apply_grade_patch`` 经 ``MaskShape.from_dict`` 重建蒙版（无
        assets_dir 时 bitmap_data 恒为 None）；此处按 shape.id 匹配回填，
        每个目标获得独立副本，不与剪贴板快照或其他目标共享。
        """
        bitmaps = (self.bitmap_masks[patch_idx]
                   if patch_idx < len(self.bitmap_masks) else {})
        if not bitmaps:
            return
        for shape in _iter_mask_shapes(getattr(target, "mask_shapes", ())):
            if getattr(shape, "type", None) != "bitmap":
                continue
            data = bitmaps.get(getattr(shape, "id", None))
            if data is not None:
                shape.bitmap_data = data.copy()

    # ── 描述 ────────────────────────────────────────────────────────

    def describe(self, categories: Optional[Iterable[str]] = None) -> List[str]:
        """粘贴预览清单：将应用的类别（人类可读标签，按注册表顺序）。

        Args:
            categories: 要列出的类别；None = 复制时捕获的全部类别。

        Returns:
            类别 UI 标签列表（固定注册表顺序 + tracking/raw）。
        """
        cats = (set(categories) if categories is not None
                else set(self.copied_categories))
        ordered = list(GRADE_CATEGORY_KEYS) + ["tracking", "raw"]
        return [GRADE_CATEGORY_LABELS[c] for c in ordered if c in cats]

    def is_empty(self) -> bool:
        """剪贴板是否为空（无层快照且无栈级数据）。

        Returns:
            bool: True 表示尚无可粘贴内容（copy_grade 未调用或源为空）。
        """
        return not self.layer_patches and not self.trackings and not self.raw_params
