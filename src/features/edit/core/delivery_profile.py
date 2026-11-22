"""B05 · 版本化客户交付 profile、结构化测量结果与交付包发布（主计划 §11）。

设计要点（对应主计划 §11 实施步骤 1/2/5/11）：

- ``DeliveryProfile``：带 schema 版本、来源（URL/文档 hash/获取日期）、
  测量模式（programme / dialogue_gated / ungated）与逐 stem 规则的不可变
  模型。客户自定义必须显式标记；内置通用模板标 "unverified"（示例），
  不得声称规范效力。未知扩展字段往返保留（不丢、不猜）。
- 旧 ``DeliveryPreset`` 继续可读（legacy/unverified），迁移记录旧数值，
  不静默替换旧项目的响度目标。
- ``MeasurementResult``：区分 pending/complete/silence/failed/cancelled，
  绑定文件 hash、布局、样本范围与算法版本；静音是结构化状态而不是
  −∞ 假测量。consumers 只接受绑定当前文件且满足该 stem required 集合
  的 complete/silence 结果。
- ``publish_delivery_atomic``：临时目录 → 逐文件校验 → 原子改名发布；
  失败零污染正式目录。

单位约定：LUFS/LU/dBTP/dBFS 按各字段名；样本坐标为交付文件自身的
绝对样本索引（48kHz 参考域之外的文件按其自身率记录）。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from src.features.edit.core.delivery_preset import (
    FULL_STEM_SET,
    DeliveryPreset,
)

PROFILE_SCHEMA_VERSION = 1

#: 测量模式（§11 步骤 3）：programme=整节目门限；dialogue_gated 需可靠
#: 对白检测或批准时间段；ungated=无门限（院线对白表等）。
MEASUREMENT_MODES = ("programme", "dialogue_gated", "ungated")

#: 来源核实状态：verified=客户/规范原文核对；unverified=示例模板；
#: customer_custom=客户自定义；legacy=旧 preset 迁移（未核实）。
VERIFICATION_LEVELS = ("verified", "unverified", "customer_custom", "legacy")

#: 合法测量状态（§10/B05）。
MEASUREMENT_STATUSES = (
    "pending", "complete", "silence", "failed", "cancelled",
)


@dataclass(frozen=True)
class StemRule:
    """单 stem 的交付/测量规则（由 profile 声明 required 集合）。"""

    stem_id: str
    # 追加的必测键（默认空——required 主要由限值声明推导；此处仅用于
    # 声明额外指标）。缺测/非法值一律拒绝，不全局放行。
    required_measurements: Tuple[str, ...] = ()
    # 允许整段静音（FX/ME 国际版）：status=silence 且样本数>0 才放行。
    allow_silence: bool = False
    # 覆盖 profile 级限值（None=继承 profile 声明；False 不是特殊值——
    # "本 stem 无 LUFS 目标"目前不可表达，需显式扩展 schema）
    target_lufs: Optional[float] = None
    max_true_peak_dbtp: Optional[float] = None
    # pre/post Master 取点语义（§11 步骤 8）：None=跟随导出配置。
    pre_master: Optional[bool] = None
    # 交付尾音预算（秒）：长混响/delay 尾不得截断。
    tail_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stem_id": self.stem_id,
            "required_measurements": list(self.required_measurements),
            "allow_silence": self.allow_silence,
            "target_lufs": self.target_lufs,
            "max_true_peak_dbtp": self.max_true_peak_dbtp,
            "pre_master": self.pre_master,
            "tail_seconds": self.tail_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StemRule":
        stem_id = str(data.get("stem_id", ""))
        if stem_id not in FULL_STEM_SET:
            raise ValueError(f"unknown stem_id: {stem_id!r}")
        required = tuple(str(k) for k in data.get("required_measurements", ()) or ())
        target = data.get("target_lufs")
        dbtp = data.get("max_true_peak_dbtp")
        pre_master = data.get("pre_master")
        return cls(
            stem_id=stem_id,
            required_measurements=required,
            allow_silence=bool(data.get("allow_silence", False)),
            target_lufs=(None if target is None else float(target)),
            max_true_peak_dbtp=(None if dbtp is None else float(dbtp)),
            pre_master=(None if pre_master is None else bool(pre_master)),
            tail_seconds=float(data.get("tail_seconds", 0.0) or 0.0),
        )


@dataclass(frozen=True)
class DeliveryProfile:
    """版本化客户交付 profile（不可变；未知扩展字段往返保留）。"""

    profile_id: str
    name: str
    schema_version: int = PROFILE_SCHEMA_VERSION
    # 来源与核实状态（B05 步骤 1）：未核实模板不得当客户要求。
    verification: str = "unverified"
    customer: str = ""
    purpose: str = ""
    source_url: str = ""
    source_doc_hash: str = ""
    acquired_date: str = ""
    # 测量模式与目标：target_lufs=None 表示该 profile 无节目 LUFS 目标
    # （如院线约定），此时 integrated_lufs 不进 required 集合。
    measurement_mode: str = "programme"
    target_lufs: Optional[float] = None
    tolerance_lu: float = 1.0
    max_true_peak_dbtp: Optional[float] = -2.0
    max_sample_peak_dbfs: Optional[float] = -0.3
    # 文件规格
    channel_order: Tuple[str, ...] = ("L", "R")
    sample_rate: int = 48000
    bit_depth: int = 24
    file_format: str = "bwf"
    # 逐 stem 规则
    stems: Tuple[StemRule, ...] = ()
    # 未知扩展字段（往返保留；schema 升级时不丢）
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.measurement_mode not in MEASUREMENT_MODES:
            raise ValueError(
                f"measurement_mode must be one of {MEASUREMENT_MODES}: "
                f"{self.measurement_mode!r}")
        if self.verification not in VERIFICATION_LEVELS:
            raise ValueError(
                f"verification must be one of {VERIFICATION_LEVELS}: "
                f"{self.verification!r}")
        if not self.profile_id:
            raise ValueError("profile_id cannot be empty")
        for rule in self.stems:
            if rule.stem_id not in FULL_STEM_SET:
                raise ValueError(f"unknown stem: {rule.stem_id!r}")

    # ── required 集合推导（§11 步骤 2：由 profile 生成，不全局放行）──

    def required_measurements_for(self, stem_id: str) -> Tuple[str, ...]:
        """该 stem 必须具备的测量键（与限值声明一致；无 LUFS 目标不要求）。"""
        rule = self.stem_rule(stem_id)
        required: list[str] = []
        target = (rule.target_lufs if rule is not None
                  and rule.target_lufs is not None else self.target_lufs)
        if target is not None:
            required.append("integrated_lufs")
        dbtp = (rule.max_true_peak_dbtp if rule is not None
                and rule.max_true_peak_dbtp is not None else self.max_true_peak_dbtp)
        if dbtp is not None:
            required.append("true_peak_dbtp")
        # 样本峰值是文件完整性底线：profile 声明 None 才可豁免。
        if self.max_sample_peak_dbfs is not None:
            required.append("peak_dbfs")
        if rule is not None:
            for key in rule.required_measurements:
                if key not in required:
                    required.append(key)
        return tuple(required)

    def stem_rule(self, stem_id: str) -> Optional[StemRule]:
        for rule in self.stems:
            if rule.stem_id == stem_id:
                return rule
        return None

    def allows_silence(self, stem_id: str) -> bool:
        rule = self.stem_rule(stem_id)
        return bool(rule.allow_silence) if rule is not None else False

    # ── 序列化（未知字段往返保留）──

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "name": self.name,
            "verification": self.verification,
            "customer": self.customer,
            "purpose": self.purpose,
            "source_url": self.source_url,
            "source_doc_hash": self.source_doc_hash,
            "acquired_date": self.acquired_date,
            "measurement_mode": self.measurement_mode,
            "target_lufs": self.target_lufs,
            "tolerance_lu": self.tolerance_lu,
            "max_true_peak_dbtp": self.max_true_peak_dbtp,
            "max_sample_peak_dbfs": self.max_sample_peak_dbfs,
            "channel_order": list(self.channel_order),
            "sample_rate": self.sample_rate,
            "bit_depth": self.bit_depth,
            "file_format": self.file_format,
            "stems": [rule.to_dict() for rule in self.stems],
        }
        # 未知扩展字段原样保留（不并入已知键；schema 升级时不丢）
        for key, value in self.extra.items():
            if key not in payload:
                payload[key] = value
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DeliveryProfile":
        known = {
            "schema_version", "profile_id", "name", "verification", "customer",
            "purpose", "source_url", "source_doc_hash", "acquired_date",
            "measurement_mode", "target_lufs", "tolerance_lu",
            "max_true_peak_dbtp", "max_sample_peak_dbfs", "channel_order",
            "sample_rate", "bit_depth", "file_format", "stems",
        }
        extra = {k: v for k, v in data.items() if k not in known}
        stems = tuple(
            StemRule.from_dict(rule) for rule in (data.get("stems") or ())
        )
        target = data.get("target_lufs")
        dbtp = data.get("max_true_peak_dbtp")
        peak = data.get("max_sample_peak_dbfs")
        return cls(
            profile_id=str(data.get("profile_id", "")),
            name=str(data.get("name", "")),
            schema_version=int(data.get("schema_version", PROFILE_SCHEMA_VERSION)),
            verification=str(data.get("verification", "unverified")),
            customer=str(data.get("customer", "")),
            purpose=str(data.get("purpose", "")),
            source_url=str(data.get("source_url", "")),
            source_doc_hash=str(data.get("source_doc_hash", "")),
            acquired_date=str(data.get("acquired_date", "")),
            measurement_mode=str(data.get("measurement_mode", "programme")),
            target_lufs=(None if target is None else float(target)),
            tolerance_lu=float(data.get("tolerance_lu", 1.0)),
            max_true_peak_dbtp=(None if dbtp is None else float(dbtp)),
            max_sample_peak_dbfs=(None if peak is None else float(peak)),
            channel_order=tuple(data.get("channel_order", ("L", "R")) or ("L", "R")),
            sample_rate=int(data.get("sample_rate", 48000)),
            bit_depth=int(data.get("bit_depth", 24)),
            file_format=str(data.get("file_format", "bwf")),
            stems=stems,
            extra=extra,
        )

    # ── 旧 preset 迁移（§11 步骤 2：legacy/unverified，记录旧数值）──

    @classmethod
    def from_preset(cls, preset: DeliveryPreset) -> "DeliveryProfile":
        """旧 DeliveryPreset → profile：标 legacy，逐 stem required 由
        preset 限值推导；旧项目的响度目标原样保留（不静默替换）。"""
        required: list[str] = ["true_peak_dbtp", "peak_dbfs"]
        if preset.loudness.target_lufs is not None:
            required.insert(0, "integrated_lufs")
        stems = tuple(
            StemRule(
                stem_id=stem,
                required_measurements=tuple(required),
                allow_silence=stem in ("FX", "ME"),
            )
            for stem in preset.stems
        )
        return cls(
            profile_id=f"legacy-{preset.preset_id}",
            name=preset.name,
            verification="legacy",
            customer="",
            purpose=preset.standard,
            source_url="",
            source_doc_hash="",
            acquired_date="",
            measurement_mode="programme",
            target_lufs=(None if preset.loudness.target_lufs is None
                         else float(preset.loudness.target_lufs)),
            tolerance_lu=float(preset.loudness.tolerance_lu),
            max_true_peak_dbtp=float(preset.loudness.max_true_peak_dbtp),
            max_sample_peak_dbfs=float(preset.loudness.max_sample_peak_dbfs),
            channel_order=tuple(preset.channel_order),
            sample_rate=int(preset.sample_rate),
            bit_depth=int(preset.bit_depth),
            stems=stems,
            extra={"legacy_preset": preset.to_dict()},
        )


@dataclass(frozen=True)
class MeasurementResult:
    """结构化测量结果（B04 §10 / B05 §11 步骤 2）。

    status 区分 pending/complete/silence/failed/cancelled；complete 才携带
    有限数值 measurements。silence 仍是有效测量（样本数>0、全静音），
    只有 profile 允许的 stem 才能以 silence 验收。file_sha256 把结果绑定
    到实际写出的最终文件——文件变化即过期。
    """

    stem_id: str
    status: str
    file_sha256: str = ""
    sample_rate: int = 0
    channel_order: Tuple[str, ...] = ()
    start_sample: int = 0
    sample_count: int = 0
    algorithm_version: str = ""
    gate_mode: str = "programme"
    measurements: Dict[str, float] = field(default_factory=dict)
    warnings: Tuple[str, ...] = ()
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in MEASUREMENT_STATUSES:
            raise ValueError(
                f"status must be one of {MEASUREMENT_STATUSES}: {self.status!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stem_id": self.stem_id,
            "status": self.status,
            "file_sha256": self.file_sha256,
            "sample_rate": self.sample_rate,
            "channel_order": list(self.channel_order),
            "start_sample": self.start_sample,
            "sample_count": self.sample_count,
            "algorithm_version": self.algorithm_version,
            "gate_mode": self.gate_mode,
            "measurements": dict(self.measurements),
            "warnings": list(self.warnings),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MeasurementResult":
        return cls(
            stem_id=str(data.get("stem_id", "")),
            status=str(data.get("status", "pending")),
            file_sha256=str(data.get("file_sha256", "")),
            sample_rate=int(data.get("sample_rate", 0)),
            channel_order=tuple(data.get("channel_order", ()) or ()),
            start_sample=int(data.get("start_sample", 0) or 0),
            sample_count=int(data.get("sample_count", 0) or 0),
            algorithm_version=str(data.get("algorithm_version", "")),
            gate_mode=str(data.get("gate_mode", "programme")),
            # 忠实保留原始值类型（不做 float() 洗涤）：bool/str/None 载入后
            # 由 measurement_rejection 的 type 检查拒绝——float() 会把
            # True→1.0、"-2.0"→-2.0 洗成合法值，让持久化往返绕过
            # 非法值防线（encode≠decode 不对称）。
            measurements={
                str(k): v
                for k, v in (data.get("measurements") or {}).items()
            },
            warnings=tuple(str(w) for w in (data.get("warnings") or ())),
            error=data.get("error"),
        )


def measurement_rejection(
    result: MeasurementResult,
    profile: DeliveryProfile,
    *,
    expected_sha256: str = "",
    allow_silence_override: Optional[bool] = None,
) -> Optional[str]:
    """验收一个测量结果；返回拒绝原因（None=可用）。

    检查链（任一失败即拒绝，绝不静默降级）：
    1. status ∈ {complete, silence}；failed/cancelled/pending 一律不可用。
    2. 文件绑定：expected_sha256 给出时必须一致（文件已变化的旧结果过期）。
    3. complete：profile 的 required 集合逐键存在、为有限数值（NaN/Inf/
       bool/字符串拒绝）。
    4. silence：仅 allow_silence 的 stem；且 sample_count>0（空文件不是
       静音，是失败）、布局/率/hash 一致。
    """
    if result.status not in ("complete", "silence"):
        return f"measurement status not usable: {result.status}"
    if expected_sha256 and result.file_sha256 != expected_sha256:
        return ("measurement bound to a different file "
                f"({result.file_sha256[:12]}… != {expected_sha256[:12]}…)")
    if result.sample_rate != profile.sample_rate:
        return (f"sample rate mismatch: {result.sample_rate} "
                f"!= profile {profile.sample_rate}")
    if tuple(result.channel_order) != tuple(profile.channel_order):
        return (f"channel order mismatch: {result.channel_order} "
                f"!= profile {profile.channel_order}")
    if result.status == "silence":
        allow = (allow_silence_override if allow_silence_override is not None
                 else profile.allows_silence(result.stem_id))
        if not allow:
            return f"stem {result.stem_id} is silent but profile forbids silence"
        if result.sample_count <= 0:
            return "silence result with zero samples is not a valid measurement"
        return None
    import math

    for key in profile.required_measurements_for(result.stem_id):
        value = result.measurements.get(key)
        if value is None:
            return f"missing required measurement: {key}"
        if type(value) not in (int, float):
            return f"required measurement {key} is not a finite number: {value!r}"
        fv = float(value)
        if not math.isfinite(fv):
            return f"required measurement {key} is not a finite number: {value!r}"
        if abs(fv) > 1.0e6:
            # 物理合理界（LUFS/dBTP/dBFS 均为有界分贝值）：1e30 这类
            # 有限但荒谬的值不是可用测量。
            return (f"required measurement {key} outside plausible "
                    f"range: {value!r}")
    return None


def build_delivery_manifest_v2(
    profile: DeliveryProfile,
    *,
    sound_hash: str,
    files: Mapping[str, Mapping[str, Any]],
    measurements: Mapping[str, MeasurementResult] = {},
    unsupported: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    """交付 manifest v2（§11 步骤 11）。

    files: {stem_id: {path, sha256, sample_count, bytes}}；measurements 绑定
    stem → MeasurementResult；unsupported 列出未支持项（不静默丢）。
    """
    entries = []
    for stem_id, info in files.items():
        entry = {
            "stem": stem_id,
            "path": str(info.get("path", "")),
            "sha256": str(info.get("sha256", "")),
            "sample_count": int(info.get("sample_count", 0) or 0),
            "bytes": int(info.get("bytes", 0) or 0),
            "channel_order": list(profile.channel_order),
            "sample_rate": profile.sample_rate,
            "bit_depth": profile.bit_depth,
        }
        result = measurements.get(stem_id)
        if result is not None:
            entry["measurement"] = result.to_dict()
        entries.append(entry)
    return {
        "manifest_schema": "topos.delivery_manifest.v2",
        "profile": profile.to_dict(),
        "sound_hash": str(sound_hash),
        "files": entries,
        "unsupported": list(unsupported),
    }


def publish_delivery_atomic(
    files: Mapping[str, Path],
    out_dir: Path,
    *,
    validator: Optional[Any] = None,
) -> Tuple[Path, ...]:
    """把交付文件原子发布到 ``out_dir``（§10 步骤 4 / B05）。

    全部文件先复制到同一临时目录（可选 ``validator(path)`` 逐文件校验，
    抛异常即中止），再逐个 ``os.replace`` 到正式目录；任何失败清理临时
    产物且正式目录零污染。返回发布后的路径元组。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".delivery-tmp-", dir=out_dir))
    published: list[Path] = []
    # 发布前已存在的目标路径（回滚时不得误删调用方的旧文件——os.replace
    # 已覆盖的内容无法恢复，只能保留现状并在异常里说明）。
    preexisting: set[Path] = set()
    try:
        staged: list[tuple[Path, Path]] = []
        for stem_id, src in files.items():
            src = Path(src)
            if not src.is_file():
                raise FileNotFoundError(
                    f"delivery file missing for {stem_id}: {src}")
            final_path = out_dir / Path(src).name
            if any(final == final_path for _, final in staged):
                # 同名 basename 冲突：后一个 stem 会覆盖前一个的暂存/
                # 发布内容——发布名必须唯一，拒绝而非静默取其一。
                raise ValueError(
                    f"multiple delivery stems share basename "
                    f"{final_path.name!r}; published names must be unique")
            if final_path.exists():
                preexisting.add(final_path)
            # 暂存名按 stem_id（防同名互覆）；发布名保持源文件名。
            target = staging / f"{stem_id}{src.suffix or '.bin'}"
            shutil.copyfile(src, target)
            if validator is not None:
                validator(target)
            staged.append((target, final_path))
        # 全部校验通过 → 原子改名发布
        for tmp_path, final_path in staged:
            os.replace(tmp_path, final_path)
            published.append(final_path)
    except Exception:
        # 回滚：删除本次新发布的文件（不动发布前已存在的路径），
        # 保证"失败后正式目录零新增污染"的合同。
        for final_path in published:
            if final_path in preexisting:
                continue
            try:
                final_path.unlink()
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return tuple(published)
