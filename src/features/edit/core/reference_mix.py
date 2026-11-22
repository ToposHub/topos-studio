"""B20 · 参考 Mix A/B 与声音版本对照（主计划 §20/B20）。

- ``ReferenceMixRef``：专用监听源引用（asset/锚点/源入点/通道映射/出处），
  不走普通节目轨 route——比较前检查布局/采样率。
- ``ComparisonState``：仅监听会话（active_source/match_gain_db/过渡长度），
  A/B 与匹配增益不写入工程声音模型。
- ``comparison_gain``：已有合格测量结果间的监听比较增益（LUFS 差值；
  提升上限保护监听余量；静音/无效测量禁用）。
- ``align_reference_offset``：整数延迟参考的互相关对齐（带限噪声探针
  语义；有损参考以解码延迟对齐后的容差报告——不静默 trim 文件）。
- ``MixVersion``：版本登记（冻结 source revision + render spec hash）；
  登记在打印完成/校验后发生，后续工程变动不覆盖旧版。

监听隔离不变量：A/B 切换与 match 增益只作用于监听支路；Master/Stem
样本完全相同（测试锁定）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

import numpy as np

REFERENCE_MIX_REF_VERSION = 1
MIX_VERSION_VERSION = 1


@dataclass(frozen=True)
class ReferenceMixRef:
    """参考源引用（工程可序列化；asset 走项目资源，不存绝对路径）。"""

    version_id: str
    asset_id: str
    source_start_sample: int = 0
    timeline_anchor_sample: int = 0     # 时间线锚（源起点对齐的时间线样本）
    channel_map: Tuple[int, ...] = ()   # 空 = 原布局直通
    original_rate: int = 48000
    label: str = ""
    provenance: str = ""                # 客户参考/旧版打印等（审计需要）

    def __post_init__(self) -> None:
        if not self.version_id:
            raise ValueError("version_id cannot be empty")
        if not self.asset_id:
            raise ValueError("asset_id cannot be empty")


@dataclass(frozen=True)
class MixVersion:
    """版本登记（打印完成/校验后冻结；不因后续工程变动覆盖）。"""

    version_id: str
    source_revision: int
    render_spec_hash: str
    asset_id: str
    label: str = ""
    notes: str = ""
    measured_report_ref: str = ""       # QC/测量报告引用（B04/B05 闭环）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": MIX_VERSION_VERSION,
            "version_id": self.version_id,
            "source_revision": self.source_revision,
            "render_spec_hash": self.render_spec_hash,
            "asset_id": self.asset_id,
            "label": self.label,
            "notes": self.notes,
            "measured_report_ref": self.measured_report_ref,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MixVersion":
        return cls(
            version_id=str(data.get("version_id", "")),
            source_revision=int(data.get("source_revision", 0) or 0),
            render_spec_hash=str(data.get("render_spec_hash", "")),
            asset_id=str(data.get("asset_id", "")),
            label=str(data.get("label", "")),
            notes=str(data.get("notes", "")),
            measured_report_ref=str(data.get("measured_report_ref", "")),
        )


@dataclass
class ComparisonState:
    """监听比较会话状态（本机；A/B/匹配增益不入工程 undo）。"""

    active_source: str = ""             # "" = 当前节目；否则 ReferenceMixRef.version_id
    match_gain_db: float = 0.0
    measurement_key: str = ""           # 测量口径（算法版本+布局+模式）
    transition_samples: int = 0         # 块边界短 crossfade（同一输出时钟）


def comparison_gain(
    reference_lufs: float,
    programme_lufs: float,
    max_boost_db: float = 12.0,
) -> float:
    """监听比较增益（线性倍率）——已有合格测量结果间的数学转换。

    不是 LUFS 测量算法；静音/无效测量（非有限值）禁用。目标提升超过
    ``max_boost_db`` 拒绝（监听余量策略，不用限幅偷改所谓参考）。
    """
    if not (math.isfinite(reference_lufs) and math.isfinite(programme_lufs)):
        raise ValueError("comparison needs finite measurements")
    boost = programme_lufs - reference_lufs
    if boost > max_boost_db:
        raise ValueError("required boost exceeds audition headroom policy")
    return 10.0 ** (boost / 20.0)


def align_reference_offset(
    programme: np.ndarray,
    reference: np.ndarray,
    *,
    max_offset_samples: int = 48000,
) -> Tuple[int, float]:
    """整数延迟参考对齐：返回 (参考相对节目的延迟样本数, 置信度)。

    正值 = 参考内容比节目晚 |offset| 样本（对齐时需将参考前移该量）；
    负值 = 参考更早。消费方按锚点应用——本函数只报错位量与方向，
    不静默 trim 文件。

    programme 与 reference 单声道化后互相关（FFT 全长）；|lag| ≤
    max_offset_samples。非周期带限噪声片段语义（B19 手册同源算法），
    避免正弦相关歧义。有损参考（编码延迟）的残差按容差报告。
    """
    prog = np.asarray(programme, dtype=np.float64)
    ref = np.asarray(reference, dtype=np.float64)
    if prog.ndim > 1:
        prog = prog.mean(axis=0)
    if ref.ndim > 1:
        ref = ref.mean(axis=0)
    if prog.size == 0 or ref.size == 0:
        raise ValueError("empty signals")
    if float(np.max(np.abs(prog))) < 1e-6 or float(np.max(np.abs(ref))) < 1e-6:
        raise ValueError("silent signals cannot be aligned")
    prog = prog - prog.mean()
    ref = ref - ref.mean()
    n = 1 << int(np.ceil(np.log2(prog.size + ref.size)))
    corr = np.fft.irfft(np.fft.rfft(prog, n) * np.conj(np.fft.rfft(ref, n)), n)
    # 窗口必须非退化：max_lag=0 时 corr[-0:] 是整个数组（正/负窗重复），
    # 峰会被解码成大数值"高置信"错位——同源实现 estimate_alignment_offset
    # 对同一形态显式拒绝。
    if int(max_offset_samples) < 1:
        raise ValueError(
            f"max_offset_samples must be >= 1 (got {max_offset_samples!r})")
    max_lag = max(1, int(min(max_offset_samples, n // 2)))
    lags = np.concatenate([corr[:max_lag + 1], corr[-max_lag:]])
    peak = int(np.argmax(lags))
    lag = peak if peak <= max_lag else peak - len(lags)
    denom = float(np.sqrt(np.sum(prog ** 2) * np.sum(ref ** 2))) or 1.0
    confidence = float(np.abs(lags[peak]) / denom)
    # 相关峰语义：峰在 lag L 处表示 prog[t] ≈ reference[t − L]；实测
    # reference 比 programme 晚 L_ref = −L 样本（ref 延迟 1001 → L=−1001）。
    # 返回正数 = 参考更晚。
    return int(-lag), min(1.0, confidence)


__all__ = [
    "ComparisonState",
    "MIX_VERSION_VERSION",
    "MixVersion",
    "REFERENCE_MIX_REF_VERSION",
    "ReferenceMixRef",
    "align_reference_offset",
    "comparison_gain",
]
