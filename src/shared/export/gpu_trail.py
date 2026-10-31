"""ExportGpuTrail：Windows 全 GPU 导出的证据契约。

对应计划 docs/windows_timeline_gpu_export_optimization_plan_2026-09-15.md §3.3：

    | ExportGpuTrail | 每段后端、回读字节数、上传字节数、等待时间、回退 reason |
    | 任何"全 GPU"结论必须由该 trail 证明。 |

硬规则（计划 §4.3 / §4.2）：

- 已宣布支持的完整 GPU 用例中 ``cpu_upload_bytes == 0`` 且
  ``cpu_download_bytes == 0``；只有压缩码流允许回到 CPU 供封装写盘。
- 像素必须 GPU 驻留的段（decode / render / yuv_conversion / encode_submit）
  缺失或未测量 GPU 时间时，"全 GPU" 声明必须给出 reason，不得默认通过。
- 字节计数只增不减且不可为负；归因修正属于缺陷，必须重建 trail 而不是
  回退计数。

线程模型：管线各段运行在工作线程（编码线程、未来 D3D11 提交线程），
计数与登记方法线程安全（内部锁）；:meth:`ExportGpuTrail.snapshot` 返回
frozen 只读视图供报告/门禁消费。本模块不依赖 Qt / PyAV，可在任何线程导入。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
import statistics

__all__ = [
    "PIXEL_RESIDENT_STAGE_NAMES",
    "TRAIL_SCHEMA_VERSION",
    "TrailStage",
    "ExportGpuTrailSnapshot",
    "ExportGpuTrail",
]

#: 像素必须 GPU 驻留的段。任何一段缺失，"全 GPU" 声明不成立。
PIXEL_RESIDENT_STAGE_NAMES: Tuple[str, ...] = (
    "decode",
    "render",
    "yuv_conversion",
    "encode_submit",
)

#: mux / 写盘段：压缩码流回 CPU 属于计划允许的边界（§4.3），显式命名
#: 以便与像素段区分，不参与零回读判定。
MUX_STAGE_NAME = "mux"

TRAIL_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TrailStage:
    """单个管线段的证据记录。

    Attributes:
        name: 段名（``decode`` / ``render`` / ``yuv_conversion`` /
            ``encode_submit`` / ``mux`` / 自定义诊断段）。
        backend: 该段实际使用的后端（如 ``d3d11``、``opengl``、
            ``cpu_swscale``、``amf``、``ffmpeg_cli``）。
        gpu_submit_ms: GPU 侧提交/执行耗时（Direct3D timestamp query 为主
            证据，计划 §4.2）；未测量必须为 ``None``，不得填 0。
        gpu_wait_ms: CPU 等待 GPU 完成的时间；逐帧 wait 尖峰由调用方在
            p95 层面另行报告，此处是聚合值。
        cpu_upload_bytes: 该段 CPU→GPU 上传字节数（像素数据）。
        cpu_download_bytes: 该段 GPU→CPU 回读字节数（像素数据）。
        note: 人类可读备注（如回退细节、驱动差异）。
    """

    name: str
    backend: str
    gpu_submit_ms: Optional[float] = None
    gpu_wait_ms: Optional[float] = None
    cpu_upload_bytes: int = 0
    cpu_download_bytes: int = 0
    note: str = ""
    sample_count: int = 1
    gpu_submit_p50_ms: Optional[float] = None
    gpu_submit_p95_ms: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("TrailStage.name 不能为空")
        if not self.backend:
            raise ValueError("TrailStage.backend 不能为空")
        for attr in ("gpu_submit_ms", "gpu_wait_ms"):
            value = getattr(self, attr)
            if value is not None and value < 0.0:
                raise ValueError(f"TrailStage.{attr} 不可为负: {value}")
        if self.cpu_upload_bytes < 0:
            raise ValueError("TrailStage.cpu_upload_bytes 不可为负")
        if self.cpu_download_bytes < 0:
            raise ValueError("TrailStage.cpu_download_bytes 不可为负")
        if self.sample_count < 1:
            raise ValueError("TrailStage.sample_count 必须 >= 1")
        for attr in ("gpu_submit_p50_ms", "gpu_submit_p95_ms"):
            value = getattr(self, attr)
            if value is not None and value < 0.0:
                raise ValueError(f"TrailStage.{attr} 不可为负")

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "backend": self.backend,
            "gpu_submit_ms": self.gpu_submit_ms,
            "gpu_wait_ms": self.gpu_wait_ms,
            "cpu_upload_bytes": self.cpu_upload_bytes,
            "cpu_download_bytes": self.cpu_download_bytes,
            "note": self.note,
            "sample_count": self.sample_count,
            "gpu_submit_p50_ms": self.gpu_submit_p50_ms,
            "gpu_submit_p95_ms": self.gpu_submit_p95_ms,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "TrailStage":
        if not isinstance(data, dict):
            raise ValueError("TrailStage 反序列化输入必须是 dict")
        known = {
            "name", "backend", "gpu_submit_ms", "gpu_wait_ms",
            "cpu_upload_bytes", "cpu_download_bytes", "note",
            "sample_count", "gpu_submit_p50_ms", "gpu_submit_p95_ms",
        }
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"TrailStage 未知字段: {sorted(unknown)}")
        return cls(
            name=str(data["name"]),
            backend=str(data["backend"]),
            gpu_submit_ms=_opt_float(data.get("gpu_submit_ms"), "gpu_submit_ms"),
            gpu_wait_ms=_opt_float(data.get("gpu_wait_ms"), "gpu_wait_ms"),
            cpu_upload_bytes=_non_neg_int(data.get("cpu_upload_bytes", 0), "cpu_upload_bytes"),
            cpu_download_bytes=_non_neg_int(data.get("cpu_download_bytes", 0), "cpu_download_bytes"),
            note=str(data.get("note", "")),
            sample_count=_non_neg_int(data.get("sample_count", 1), "sample_count"),
            gpu_submit_p50_ms=_opt_float(
                data.get("gpu_submit_p50_ms"), "gpu_submit_p50_ms"),
            gpu_submit_p95_ms=_opt_float(
                data.get("gpu_submit_p95_ms"), "gpu_submit_p95_ms"),
        )


@dataclass(frozen=True)
class ExportGpuTrailSnapshot:
    """一次导出的只读 trail 快照（报告与门禁的唯一消费形态）。"""

    export_id: str
    adapter_luid: str
    stages: Tuple[TrailStage, ...]
    cpu_upload_bytes: int
    cpu_download_bytes: int
    fallback_reason: str
    schema_version: int = TRAIL_SCHEMA_VERSION

    @property
    def zero_pixel_transfer(self) -> bool:
        """像素帧 CPU 上传/回读是否均为零（计划 §4.3 GPU 驻留门槛）。"""
        return self.cpu_upload_bytes == 0 and self.cpu_download_bytes == 0

    def full_gpu_claim_reason(self) -> str:
        """可声明"全 GPU 导出"时返回空串，否则返回机器可读 reason。

        词表：
        - ``trail_stage_missing:<name>``：像素驻留段未登记；
        - ``gpu_time_unmeasured:<name>``：像素段没有 GPU timestamp 证据；
        - ``cpu_upload_bytes_positive`` / ``cpu_download_bytes_positive``；
        - ``fallback:<reason>``：管线登记了段级回退。
        """
        by_name = {stage.name: stage for stage in self.stages}
        for stage_name in PIXEL_RESIDENT_STAGE_NAMES:
            stage = by_name.get(stage_name)
            if stage is None:
                return f"trail_stage_missing:{stage_name}"
            if stage.gpu_submit_ms is None:
                return f"gpu_time_unmeasured:{stage_name}"
        if self.cpu_upload_bytes > 0:
            return "cpu_upload_bytes_positive"
        if self.cpu_download_bytes > 0:
            return "cpu_download_bytes_positive"
        if self.fallback_reason:
            return f"fallback:{self.fallback_reason}"
        return ""

    def stage(self, name: str) -> Optional[TrailStage]:
        for item in self.stages:
            if item.name == name:
                return item
        return None

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "export_id": self.export_id,
            "adapter_luid": self.adapter_luid,
            "stages": [stage.to_dict() for stage in self.stages],
            "cpu_upload_bytes": self.cpu_upload_bytes,
            "cpu_download_bytes": self.cpu_download_bytes,
            "fallback_reason": self.fallback_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ExportGpuTrailSnapshot":
        if not isinstance(data, dict):
            raise ValueError("ExportGpuTrailSnapshot 反序列化输入必须是 dict")
        if int(data.get("schema_version", -1)) != TRAIL_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version 不匹配: {data.get('schema_version')!r}"
                f" (期望 {TRAIL_SCHEMA_VERSION})"
            )
        stages = tuple(TrailStage.from_dict(item) for item in data.get("stages", []))
        names = [stage.name for stage in stages]
        if len(names) != len(set(names)):
            raise ValueError("stages 存在重复段名")
        return cls(
            export_id=str(data["export_id"]),
            adapter_luid=str(data.get("adapter_luid", "")),
            stages=stages,
            cpu_upload_bytes=_non_neg_int(data.get("cpu_upload_bytes", 0), "cpu_upload_bytes"),
            cpu_download_bytes=_non_neg_int(data.get("cpu_download_bytes", 0), "cpu_download_bytes"),
            fallback_reason=str(data.get("fallback_reason", "")),
        )


class ExportGpuTrail:
    """导出过程中的线程安全 trail 累加器。

    用法：管线启动时创建，各段工作线程调用 :meth:`record_stage` /
    :meth:`add_cpu_upload` / :meth:`add_cpu_download` /
    :meth:`set_fallback_reason`；导出结束后 :meth:`snapshot` 产出报告。
    """

    def __init__(self, export_id: str, adapter_luid: str = "") -> None:
        if not export_id:
            raise ValueError("export_id 不能为空")
        self._lock = threading.Lock()
        self._export_id = export_id
        self._adapter_luid = adapter_luid
        self._stages: Dict[str, TrailStage] = {}
        self._cpu_upload_bytes = 0
        self._cpu_download_bytes = 0
        self._fallback_reason = ""
        self._samples: Dict[str, list[tuple[str, Optional[float], Optional[float], int, int]]] = {}

    @property
    def export_id(self) -> str:
        return self._export_id

    @property
    def adapter_luid(self) -> str:
        return self._adapter_luid

    def record_stage(self, stage: TrailStage) -> None:
        """登记一个段。同名重复登记视为缺陷（计划：归因必须唯一、可审计）。"""
        with self._lock:
            if stage.name in self._stages:
                raise ValueError(f"trail 段重复登记: {stage.name}")
            self._stages[stage.name] = stage
            self._cpu_upload_bytes += stage.cpu_upload_bytes
            self._cpu_download_bytes += stage.cpu_download_bytes

    def add_cpu_upload(self, byte_count: int) -> None:
        if byte_count < 0:
            raise ValueError("cpu upload 字节计数不可为负")
        with self._lock:
            self._cpu_upload_bytes += byte_count

    def add_cpu_download(self, byte_count: int) -> None:
        if byte_count < 0:
            raise ValueError("cpu download 字节计数不可为负")
        with self._lock:
            self._cpu_download_bytes += byte_count

    def set_fallback_reason(self, reason: str) -> None:
        if not reason:
            raise ValueError("fallback reason 不可为空（fail-closed 词表）")
        with self._lock:
            self._fallback_reason = reason

    def record_stage_sample(
        self,
        *,
        name: str,
        backend: str,
        gpu_submit_ms: Optional[float],
        gpu_wait_ms: Optional[float] = None,
        cpu_upload_bytes: int = 0,
        cpu_download_bytes: int = 0,
    ) -> None:
        """Record one frame sample without rebuilding a stage per frame.

        Snapshot materializes the stage with mean GPU time in
        ``gpu_submit_ms`` and p50/p95 fields. This keeps the existing fail-closed
        claim API while making real 480-frame reports auditable.
        """
        # 校验性构造（fail-closed）：__post_init__ 在此对负值耗时/空名抛
        # 错；样本本体随后以原始元组登记（P2-3c：原先赋给死变量 sample）。
        TrailStage(
            name=name,
            backend=backend,
            gpu_submit_ms=gpu_submit_ms,
            gpu_wait_ms=gpu_wait_ms,
            cpu_upload_bytes=cpu_upload_bytes,
            cpu_download_bytes=cpu_download_bytes,
        )
        with self._lock:
            if name in self._stages:
                raise ValueError(f"trail 段重复登记: {name}")
            self._samples.setdefault(name, []).append(
                (backend, gpu_submit_ms, gpu_wait_ms,
                 cpu_upload_bytes, cpu_download_bytes)
            )

    @staticmethod
    def _percentile(values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
        return float(ordered[index])

    def snapshot(self) -> ExportGpuTrailSnapshot:
        with self._lock:
            materialized = dict(self._stages)
            for name, samples in self._samples.items():
                submit = [item[1] for item in samples if item[1] is not None]
                waits = [item[2] for item in samples if item[2] is not None]
                uploads = sum(item[3] for item in samples)
                downloads = sum(item[4] for item in samples)
                backend = samples[-1][0]
                materialized[name] = TrailStage(
                    name=name,
                    backend=backend,
                    gpu_submit_ms=(statistics.fmean(submit) if submit else None),
                    gpu_wait_ms=(statistics.fmean(waits) if waits else None),
                    cpu_upload_bytes=uploads,
                    cpu_download_bytes=downloads,
                    sample_count=len(samples),
                    gpu_submit_p50_ms=self._percentile(submit, 0.50) if submit else None,
                    gpu_submit_p95_ms=self._percentile(submit, 0.95) if submit else None,
                )
            stages = tuple(materialized[name] for name in sorted(materialized))
            upload_bytes = self._cpu_upload_bytes + sum(
                item[3] for values in self._samples.values() for item in values
            )
            download_bytes = self._cpu_download_bytes + sum(
                item[4] for values in self._samples.values() for item in values
            )
            return ExportGpuTrailSnapshot(
                export_id=self._export_id,
                adapter_luid=self._adapter_luid,
                stages=stages,
                cpu_upload_bytes=upload_bytes,
                cpu_download_bytes=download_bytes,
                fallback_reason=self._fallback_reason,
            )


def _opt_float(value: object, field_name: str) -> Optional[float]:
    if value is None:
        return None
    result = float(value)  # type: ignore[arg-type]
    if result < 0.0:
        raise ValueError(f"{field_name} 不可为负: {result}")
    return result


def _non_neg_int(value: object, field_name: str) -> int:
    result = int(value)  # type: ignore[arg-type]
    if result < 0:
        raise ValueError(f"{field_name} 不可为负: {result}")
    return result
