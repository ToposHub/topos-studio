"""P1 · 交付预设（2026-11-19 计划第二阶段 · 交接与自动 QC）。

预设按客户规范分版本（广播 / 影院 / 流媒体），包含标准、目标、容差
及报告；BWF 交付 stem 清单覆盖 Full Mix、DX、ADR、Foley、FX、MX、
M&E。``evaluate_delivery`` 把实测（LUFS/dBTP/峰值/静音）对到预设
目标，产出 QC findings（复用 ``delivery_qc`` 的报告结构）；报告摘要
携带起始 TC、帧率/DF、声道顺序、峰值与校验（SHA-256）字段。

广播/流媒体的 LUFS 目标遵循 EBU R 128 与主流平台规范——不用一个
固定 LUFS 目标覆盖所有场景。
"""
from __future__ import annotations

import math

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Optional, Tuple

#: 标准 stem 清单（BWF 交付；按客户规范可裁剪）。
FULL_STEM_SET: Tuple[str, ...] = (
    "FullMix", "DX", "ADR", "Foley", "FX", "MX", "ME",
)

#: stem 显示名（报告用）。
STEM_DISPLAY_NAMES: Dict[str, str] = {
    "FullMix": "Full Mix",
    "DX": "DX (Dialogue)",
    "ADR": "ADR",
    "Foley": "Foley",
    "FX": "FX",
    "MX": "MX (Music)",
    "ME": "M&E (Music & Effects)",
}


@dataclass(frozen=True)
class LoudnessTarget:
    """一个交付目标（带容差）。"""

    target_lufs: float
    tolerance_lu: float
    max_true_peak_dbtp: float
    max_sample_peak_dbfs: float = -0.3

    def within_loudness(self, measured_lufs: float) -> bool:
        return abs(measured_lufs - self.target_lufs) <= self.tolerance_lu


@dataclass(frozen=True)
class DeliveryPreset:
    """客户规范的一个交付预设版本（不可变；可序列化）。"""

    preset_id: str
    name: str
    standard: str                     # "EBU R 128" / "影院" / "流媒体"
    version: str                      # 客户规范版本（报告显示来源与版本）
    loudness: LoudnessTarget
    stems: Tuple[str, ...] = FULL_STEM_SET
    start_timecode: str = "00:00:00:00"
    fps: str = "24/1"
    drop_frame: bool = False
    channel_order: Tuple[str, ...] = ("L", "R")
    sample_rate: int = 48000
    bit_depth: int = 24
    max_silence_seconds: float = 5.0  # 过量静音阈值（QC）

    def with_stems(self, stems: Tuple[str, ...]) -> "DeliveryPreset":
        unknown = [stem for stem in stems if stem not in FULL_STEM_SET]
        if unknown:
            raise ValueError(f"unknown stems: {unknown}")
        return replace(self, stems=tuple(stems))

    def to_dict(self) -> dict:
        return {
            "preset_id": self.preset_id,
            "name": self.name,
            "standard": self.standard,
            "version": self.version,
            "loudness": {
                "target_lufs": self.loudness.target_lufs,
                "tolerance_lu": self.loudness.tolerance_lu,
                "max_true_peak_dbtp": self.loudness.max_true_peak_dbtp,
                "max_sample_peak_dbfs": self.loudness.max_sample_peak_dbfs,
            },
            "stems": list(self.stems),
            "start_timecode": self.start_timecode,
            "fps": self.fps,
            "drop_frame": self.drop_frame,
            "channel_order": list(self.channel_order),
            "sample_rate": self.sample_rate,
            "bit_depth": self.bit_depth,
            "max_silence_seconds": self.max_silence_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeliveryPreset":
        loud = data.get("loudness") or {}
        return cls(
            preset_id=str(data["preset_id"]),
            name=str(data.get("name", "")),
            standard=str(data.get("standard", "")),
            version=str(data.get("version", "")),
            loudness=LoudnessTarget(
                target_lufs=float(loud.get("target_lufs", -23.0)),
                tolerance_lu=float(loud.get("tolerance_lu", 1.0)),
                max_true_peak_dbtp=float(loud.get("max_true_peak_dbtp", -2.0)),
                max_sample_peak_dbfs=float(
                    loud.get("max_sample_peak_dbfs", -0.3)),
            ),
            stems=tuple(data.get("stems", FULL_STEM_SET)),
            start_timecode=str(data.get("start_timecode", "00:00:00:00")),
            fps=str(data.get("fps", "24/1")),
            drop_frame=bool(data.get("drop_frame", False)),
            channel_order=tuple(data.get("channel_order", ("L", "R"))),
            sample_rate=int(data.get("sample_rate", 48000)),
            bit_depth=int(data.get("bit_depth", 24)),
            max_silence_seconds=float(data.get("max_silence_seconds", 5.0)),
        )


#: 内置预设目录（广播 / 流媒体 / 影院——各自的目标，不用一个固定值覆盖）。
BUILTIN_PRESETS: Tuple[DeliveryPreset, ...] = (
    DeliveryPreset(
        preset_id="broadcast-ebu-r128-v1",
        name="广播（EBU R 128）",
        standard="EBU R 128",
        version="v1",
        loudness=LoudnessTarget(-23.0, 1.0, -2.0),
    ),
    DeliveryPreset(
        preset_id="streaming-platform-v1",
        name="流媒体平台",
        standard="流媒体",
        version="v1",
        loudness=LoudnessTarget(-14.0, 1.0, -1.0),
    ),
    DeliveryPreset(
        preset_id="cinema-theatrical-v1",
        name="影院（剧场版）",
        standard="影院",
        version="v1",
        loudness=LoudnessTarget(-27.0, 2.0, -2.0),
        channel_order=("L", "R", "C", "LFE", "Ls", "Rs"),
    ),
)


def preset_by_id(preset_id: str) -> Optional[DeliveryPreset]:
    for preset in BUILTIN_PRESETS:
        if preset.preset_id == preset_id:
            return preset
    return None


def build_delivery_manifest(
    preset: DeliveryPreset,
    *,
    checksums: Optional[Dict[str, str]] = None,
    measured: Optional[Dict[str, Dict[str, float]]] = None,
) -> Dict[str, Any]:
    """交付清单：每条 stem 的规格 + 实测 + 校验摘要。

    ``checksums``: {stem: sha256}；``measured``: {stem: {lufs, dbtp,
    peak_dbfs}}。起始 TC、帧率/DF、声道顺序、峰值与校验字段按预设
    统一携带（交付每条 stem 共用起止样本由导出层保证）。
    """
    checksums = checksums or {}
    measured = measured or {}
    entries = []
    for stem in preset.stems:
        entry: Dict[str, Any] = {
            "stem": stem,
            "display_name": STEM_DISPLAY_NAMES.get(stem, stem),
            "start_timecode": preset.start_timecode,
            "fps": preset.fps,
            "drop_frame": preset.drop_frame,
            "channel_order": list(preset.channel_order),
            "sample_rate": preset.sample_rate,
            "bit_depth": preset.bit_depth,
        }
        if stem in checksums:
            entry["sha256"] = checksums[stem]
        if stem in measured:
            entry["measured"] = measured[stem]
        entries.append(entry)
    return {
        "preset": preset.to_dict(),
        "standard_source": f"{preset.standard} {preset.version}",
        "stems": entries,
    }


def _required_measurements(loudness: "LoudnessTarget") -> Tuple[Tuple[str, Optional[float]], ...]:
    """B04：preset 声明了限值的指标 → 必须有有效测量。

    没有测量值就无法校验限值；缺测/非数值/NaN/Inf 一律阻断，禁止
    沿用"NaN 越界比较为 False"的漏检路径。返回 (key, declared_limit)。
    """
    required: list[Tuple[str, Optional[float]]] = []
    if loudness.target_lufs is not None:
        required.append(("integrated_lufs", loudness.target_lufs))
    if loudness.max_true_peak_dbtp is not None:
        required.append(("true_peak_dbtp", loudness.max_true_peak_dbtp))
    if loudness.max_sample_peak_dbfs is not None:
        required.append(("peak_dbfs", loudness.max_sample_peak_dbfs))
    return tuple(required)


def _invalid_measurement(values: Dict[str, object],
                         required: Tuple[Tuple[str, Optional[float]], ...],
                         stem: str) -> Optional[dict]:
    """必测字段防线：缺字段/None/bool/字符串/NaN/±Inf → BLOCKING finding。"""
    for key, _limit in required:
        value = values.get(key)
        if value is None:
            return {
                "check_id": f"delivery.{stem}.measurement_missing",
                "severity": "blocking",
                "message": (
                    f"stem {stem} 缺少必测指标 {key}（preset 声明了对应限值，"
                    "缺测量无法校验交付）"),
                "details": {"stem": stem, "measurement": key},
            }
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            return {
                "check_id": f"delivery.{stem}.measurement_invalid",
                "severity": "blocking",
                "message": (
                    f"stem {stem} 必测指标 {key} 非有效数值（{value!r}）；"
                    "测量未完成或文件损坏时不得生成成功交付"),
                "details": {"stem": stem, "measurement": key, "value": repr(value)},
            }
    return None


def evaluate_delivery(
    preset: DeliveryPreset,
    measured: Dict[str, Dict[str, float]],
) -> Tuple[Tuple[dict, ...], bool]:
    """实测对目标：返回 (findings, passed)。

    findings 为轻量 dict（check_id/severity/message/details），语义与
    ``delivery_qc.QCFinding`` 对齐（调用方可转换入库）。缺 stem、缺必测
    指标、LUFS 超容差、dBTP/峰值越界、过量静音都是 BLOCKING——未知效果或
    渲染错误不能产出成功交付。B04：声明了限值的指标必须先通过必测防线
    （缺测/NaN/bool/字符串阻断），比较才允许进行。
    """
    findings: list[dict] = []
    required = _required_measurements(preset.loudness)
    for stem in preset.stems:
        values = measured.get(stem)
        if values is None:
            findings.append({
                "check_id": f"delivery.{stem}.missing",
                "severity": "blocking",
                "message": f"stem {stem} 缺少实测数据",
                "details": {"stem": stem},
            })
            continue
        invalid = _invalid_measurement(values, required, stem)
        if invalid is not None:
            findings.append(invalid)
            continue
        # 必测防线只强制"声明了限值"的键；限值为 None 的指标不参与比较
        # （此前无条件取键会在 target_lufs=None 时 KeyError——B04 防线
        # 本就为 None 限值场景预留）。
        lufs = values.get("integrated_lufs")
        dbtp = values.get("true_peak_dbtp")
        peak = values.get("peak_dbfs")
        silence = float(values.get("max_silence_seconds", 0.0))
        if (lufs is not None and preset.loudness.target_lufs is not None
                and not preset.loudness.within_loudness(float(lufs))):
            findings.append({
                "check_id": f"delivery.{stem}.loudness",
                "severity": "blocking",
                "message": (
                    f"stem {stem} 响度 {float(lufs):+.1f} LUFS 超出目标 "
                    f"{preset.loudness.target_lufs:+.1f} "
                    f"±{preset.loudness.tolerance_lu:.1f} LU"),
                "details": {"stem": stem, "measured_lufs": float(lufs),
                            "target_lufs": preset.loudness.target_lufs},
            })
        if (dbtp is not None
                and preset.loudness.max_true_peak_dbtp is not None
                and float(dbtp) > preset.loudness.max_true_peak_dbtp):
            findings.append({
                "check_id": f"delivery.{stem}.true_peak",
                "severity": "blocking",
                "message": (
                    f"stem {stem} True Peak {float(dbtp):+.1f} dBTP 超上限 "
                    f"{preset.loudness.max_true_peak_dbtp:+.1f}"),
                "details": {"stem": stem, "measured_dbtp": float(dbtp)},
            })
        if (peak is not None
                and preset.loudness.max_sample_peak_dbfs is not None
                and float(peak) > preset.loudness.max_sample_peak_dbfs):
            findings.append({
                "check_id": f"delivery.{stem}.sample_peak",
                "severity": "blocking",
                "message": (
                    f"stem {stem} 样本峰值 {float(peak):+.1f} dBFS 超上限 "
                    f"{preset.loudness.max_sample_peak_dbfs:+.1f}"),
                "details": {"stem": stem, "measured_peak_dbfs": float(peak)},
            })
        if silence > preset.max_silence_seconds:
            findings.append({
                "check_id": f"delivery.{stem}.excess_silence",
                "severity": "warning",
                "message": (
                    f"stem {stem} 存在 {silence:.1f}s 静音段（阈值 "
                    f"{preset.max_silence_seconds:.1f}s）"),
                "details": {"stem": stem, "measured_silence": silence},
            })
    passed = not any(f["severity"] == "blocking" for f in findings)
    return tuple(findings), passed
