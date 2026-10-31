"""F2.7 · 素材解释覆盖（Interpretation Override）——用户解释与原始探测分离。

语义（对齐 DaVinci Resolve 的 Clip Attributes → Interpretation）：

- ``fps``：帧率解释覆盖（如 25fps 素材解释为 24fps）。原始探测 metadata
  **不改写**（``MediaAsset.metadata["fps"]`` 保持探测值）；覆盖存
  ``EditProject.interpretation_overrides[asset_id]``，经命令提交、随工程
  持久化、可撤销。
- 生效值 = 覆盖优先，无覆盖退探测值（``effective_asset_fps``）。新插入
  走生效值（插入四门的 fps 元数据解析）；既有片段经
  ``UpdateClipFpsInterpretationCommand`` 显式重锚（保持源帧窗、按新解释
  重算时间线时长——与 DaVinci "Use frame rate" 语义一致）。
- 代理失效：解释变更清 ``proxy_status`` 并失效在途代理任务（与 relink
  同语义——代理按探测参数生成，解释不同则串片）。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Optional


@dataclass(frozen=True)
class InterpretationOverride:
    """单素材的解释覆盖（不可变，命令栈替换）。None 字段 = 不覆盖。

    M04.3：扩展旋转/SAR/隔行声明域——字段可声明，但**各维度的消费
    状态按真实消费者定义**（见 ``interpretation_matrix`` 矩阵：fps
    覆盖已被帧率解释链消费；rotation/SAR/field_order 当前渲染侧未
    应用校正——矩阵明示 declared_unapplied，不静默装作已生效）。
    """

    fps: Optional[float] = None
    #: 旋转解释（度；探测 metadata.rotation 同域）
    rotation_deg: Optional[int] = None
    #: 像素宽高比覆盖 ((num, den)，正整数对)
    sar: Optional[tuple] = None
    #: 隔行场序（"progressive"/"tt"/"bb"/"tb"/"bt"）
    field_order: Optional[str] = None

    def to_dict(self) -> dict:
        # 稀疏写出：None 字段不落键（旧版读新工程不遇未知键；持久化
        # 形状与既有 fps-only 时代兼容）
        out: dict = {}
        if self.fps is not None:
            out["fps"] = self.fps
        if self.rotation_deg is not None:
            out["rotation_deg"] = self.rotation_deg
        if self.sar is not None:
            out["sar"] = list(self.sar)
        if self.field_order is not None:
            out["field_order"] = self.field_order
        return out

    @staticmethod
    def from_dict(data: dict) -> "InterpretationOverride":
        raw_fps = data.get("fps")
        raw_rot = data.get("rotation_deg")
        raw_sar = data.get("sar")
        raw_field = data.get("field_order")
        return InterpretationOverride(
            fps=float(raw_fps) if raw_fps else None,
            rotation_deg=(int(raw_rot)
                          if raw_rot in (0, 90, 180, 270) else None),
            sar=(tuple(int(v) for v in raw_sar)
                 if isinstance(raw_sar, (list, tuple)) and len(raw_sar) == 2
                 else None),
            field_order=(str(raw_field)
                         if raw_field in ("progressive", "tt", "bb",
                                          "tb", "bt") else None))

    def is_empty(self) -> bool:
        return (self.fps is None and self.rotation_deg is None
                and self.sar is None and self.field_order is None)

    def validate(self) -> None:
        """拒绝无意义/危险值（fps ≤0 或超常见范围；组合域校验）。"""
        if self.fps is None:
            pass
        elif not (1.0 <= self.fps <= 240.0):
            raise ValueError(
                f"帧率解释超出支持范围（1–240）：{self.fps}")
        if self.rotation_deg is not None \
                and self.rotation_deg not in (0, 90, 180, 270):
            raise ValueError(
                f"旋转解释只支持 0/90/180/270：{self.rotation_deg}")
        if self.sar is not None:
            num, den = self.sar
            if int(num) <= 0 or int(den) <= 0:
                raise ValueError(f"SAR 必须为正整数对：{self.sar}")
        if self.field_order is not None and self.field_order not in (
                "progressive", "tt", "bb", "tb", "bt"):
            raise ValueError(f"未知场序：{self.field_order}")


#: 素材级默认（无覆盖 = 完全按探测 metadata）
DEFAULT_INTERPRETATION = InterpretationOverride()


def interpretation_for_asset(
    overrides: dict, asset_id: str,
) -> InterpretationOverride:
    """取素材生效的解释覆盖（无覆盖/未知载荷 = 默认）。"""
    value = overrides.get(asset_id) if overrides else None
    if isinstance(value, InterpretationOverride):
        return value
    return DEFAULT_INTERPRETATION


def effective_asset_fps(asset: Any, overrides: dict) -> Optional[float]:
    """素材生效帧率：解释覆盖优先，无覆盖退探测值。"""
    override = interpretation_for_asset(overrides, getattr(asset, "asset_id", ""))
    if override.fps:
        return override.fps
    return getattr(asset, "fps", None)


def fps_as_fraction(fps: float) -> tuple[int, int]:
    """帧率 → 限分母四元组的 (num, den)（与既有 fps 快照口径一致）。"""
    frac = Fraction(float(fps)).limit_denominator(100000)
    return frac.numerator, frac.denominator


def serialize_interpretation_overrides(overrides: Any) -> dict:
    """覆盖 → 可 JSON 化 dict；未知载荷原样保留（重存不丢）。"""
    if not overrides:
        return {}
    result: dict = {}
    for asset_id, value in overrides.items():
        result[asset_id] = (
            value.to_dict() if isinstance(value, InterpretationOverride)
            else value)
    return result


def deserialize_interpretation_overrides(raw: Any) -> dict:
    """落盘 dict → 覆盖；未知/畸形载荷保留原样（运行时按无覆盖）。"""
    if not isinstance(raw, dict):
        return {}
    result: dict = {}
    for asset_id, payload in raw.items():
        if isinstance(payload, dict):
            try:
                result[asset_id] = InterpretationOverride.from_dict(payload)
                continue
            except Exception:
                pass
        result[asset_id] = payload
    return result
