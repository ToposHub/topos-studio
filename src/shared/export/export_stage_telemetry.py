"""ExportStageTelemetry——导出阶段 telemetry 与 GPU truthfulness（T-P0-20）。

统一 JSON schema（``topos-export-stage-telemetry/1``），供 Timeline export、
Color export、Composite export、AI export job 共用：

- 规范阶段词表：``probe`` / ``decode`` / ``cpu_to_gpu_upload`` / ``render`` /
  ``gpu_to_cpu_download`` / ``encode_submit`` / ``encode`` / ``mux`` /
  ``queue_wait``。
- 每阶段固定字段：backend、wall p50/p95、GPU submit（均值/p50/p95，不可测
  为 null）、GPU wait、CPU upload/download bytes、sample count、段级
  fallback reason。
- **full_gpu 唯一判据**：必经阶段（decode/render/encode_submit）全部存在、
  backend 属 GPU/硬件词表、encode 阶段为硬件编码器、无 ``cpu_encoder_surface_path``
  等显式 CPU 路径标记、像素 upload/download 为 0（或该阶段显式豁免）。
  任何一条不满足 → ``full_gpu=false`` + 机器可读 blockers。
- fallback 根因**只记录一次**（first-wins）：后续症状不得覆盖根因；
  保留 frame index 与 source/effect/node origin（有界环形缓冲）。

本模块不依赖 Qt/GL，可在任意线程导入；线程安全由内部锁保证。
"""

from __future__ import annotations

import json
import statistics
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "EXPORT_STAGE_NAMES",
    "REQUIRED_FULL_GPU_STAGES",
    "GPU_BACKEND_TOKENS",
    "HARDWARE_ENCODER_TOKENS",
    "EXPORT_STAGE_TELEMETRY_SCHEMA_VERSION",
    "StageTelemetry",
    "ExportStageTelemetryReport",
    "ExportTelemetryRecorder",
]

EXPORT_STAGE_TELEMETRY_SCHEMA_VERSION = 1
SCHEMA_ID = "topos-export-stage-telemetry/1"

#: 规范阶段词表（总计划 T-P0-20 步骤 1）。
EXPORT_STAGE_NAMES: Tuple[str, ...] = (
    "probe",
    "decode",
    "cpu_to_gpu_upload",
    "render",
    "gpu_to_cpu_download",
    "encode_submit",
    "encode",
    "mux",
    "queue_wait",
)

#: full_gpu 判定的必经像素/提交阶段。
REQUIRED_FULL_GPU_STAGES: Tuple[str, ...] = (
    "decode",
    "render",
    "encode_submit",
)

#: stage backend 中视为 GPU/硬件路径的 token（大小写不敏感的包含匹配）。
GPU_BACKEND_TOKENS: Tuple[str, ...] = (
    "metal",
    "videotoolbox",
    "d3d11",
    "nvenc",
    "amf",
    "qsv",
    "vaapi",
    "opengl_compute",
    "opencl",
    "vulkan",
    # T-P0-31：原生 TOPOS 解码器直接写映射 IOSurface 平面（零拷贝，
    # cpu_upload_bytes 恒 0，全局 upload 门仍 fail-closed 把关）。注意
    # 直出 planar CPU 编码路径同样使用该 token——但那条路带
    # cpu_encoder_surface_path 标记 + CPU encode blocker，不会被误判
    # full_gpu。
    "topos_native",
)

#: encode 阶段视为硬件编码器的 token。
HARDWARE_ENCODER_TOKENS: Tuple[str, ...] = (
    "videotoolbox",
    "nvenc",
    "amf",
    "qsv",
    "vaapi",
    "mediafoundation",
)

#: 显式 CPU 路径标记：出现任一即 full_gpu=false（总计划 §0.2/步骤 3）。
CPU_PATH_MARKERS: Tuple[str, ...] = (
    "cpu_encoder_surface_path",
    "cpu_fallback",
    "cpu_encoder",
)

#: fallback 归因环形缓冲上限（防止长导出无界增长）。
_MAX_FRAME_ATTRIBUTIONS = 64


def _is_gpu_backend(backend: str) -> bool:
    lowered = backend.lower()
    return any(token in lowered for token in GPU_BACKEND_TOKENS)


def _is_hw_encoder(backend: str) -> bool:
    lowered = backend.lower()
    return any(token in lowered for token in HARDWARE_ENCODER_TOKENS)


@dataclass(frozen=True)
class StageTelemetry:
    """单个导出阶段的聚合证据（固定字段，JSON-safe）。"""

    name: str
    backend: str
    sample_count: int = 0
    wall_p50_ms: Optional[float] = None
    wall_p95_ms: Optional[float] = None
    gpu_submit_ms: Optional[float] = None
    gpu_submit_p50_ms: Optional[float] = None
    gpu_submit_p95_ms: Optional[float] = None
    gpu_wait_ms: Optional[float] = None
    cpu_upload_bytes: int = 0
    cpu_download_bytes: int = 0
    fallback_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "backend": self.backend,
            "sample_count": self.sample_count,
            "wall_p50_ms": self.wall_p50_ms,
            "wall_p95_ms": self.wall_p95_ms,
            "gpu_submit_ms": self.gpu_submit_ms,
            "gpu_submit_p50_ms": self.gpu_submit_p50_ms,
            "gpu_submit_p95_ms": self.gpu_submit_p95_ms,
            "gpu_wait_ms": self.gpu_wait_ms,
            "cpu_upload_bytes": self.cpu_upload_bytes,
            "cpu_download_bytes": self.cpu_download_bytes,
            "fallback_reason": self.fallback_reason,
        }


@dataclass(frozen=True)
class _Attribution:
    reason: str
    stage: str
    frame_index: Optional[int]
    origin: str


@dataclass(frozen=True)
class ExportStageTelemetryReport:
    """一次导出的阶段 telemetry 快照（只读）。"""

    export_id: str
    pipeline: str
    stages: Tuple[StageTelemetry, ...]
    cpu_upload_bytes: int
    cpu_download_bytes: int
    fallback_root_cause: Optional[str]
    frame_attributions: Tuple[Dict[str, Any], ...] = ()
    schema_version: int = EXPORT_STAGE_TELEMETRY_SCHEMA_VERSION
    #: T-P1-10：交付路径标记（如 topos_direct_planar_cpu_encode）。
    delivery_path: str = ""
    #: T-P1-11：native 编码阶段统计（worker_cpu_ns / wall_ns 分组，见
    #: ``topos_binding.group_native_encode_stage_stats``）；仅 TOPOS 编码
    #: 路径收集。None = 非 TOPOS 路径、native 未暴露或 profiling 未启用
    #: （TOPOS_CODEC_PROFILE=1 时 native 才累加计数器；零值不写入）。
    native_encode_stage_stats: Optional[Dict[str, Any]] = None

    @property
    def full_gpu_blockers(self) -> Tuple[str, ...]:
        """机器可读的 full_gpu 阻断项；空元组 = 可以声明 full_gpu。"""
        blockers: List[str] = []
        by_name = {s.name: s for s in self.stages}
        for name in REQUIRED_FULL_GPU_STAGES:
            stage = by_name.get(name)
            if stage is None or stage.sample_count == 0:
                blockers.append(f"stage_missing:{name}")
                continue
            if not _is_gpu_backend(stage.backend):
                blockers.append(f"stage_backend_cpu:{name}:{stage.backend}")
            if stage.fallback_reason:
                blockers.append(f"stage_fallback:{name}:{stage.fallback_reason}")
        encode = by_name.get("encode")
        if encode is not None and encode.sample_count > 0:
            if not _is_hw_encoder(encode.backend):
                blockers.append(f"encode_backend_cpu:{encode.backend}")
        upload_exempt = by_name.get("cpu_to_gpu_upload")
        if upload_exempt is None or upload_exempt.sample_count == 0:
            if self.cpu_upload_bytes > 0:
                blockers.append("cpu_upload_bytes_positive")
        if self.cpu_download_bytes > 0:
            blockers.append("cpu_download_bytes_positive")
        if self.fallback_root_cause:
            if any(marker in self.fallback_root_cause
                   for marker in CPU_PATH_MARKERS):
                blockers.append(f"cpu_path_marker:{self.fallback_root_cause}")
            else:
                blockers.append(f"fallback_root_cause:{self.fallback_root_cause}")
        return tuple(blockers)

    @property
    def full_gpu(self) -> bool:
        return not self.full_gpu_blockers

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA_ID,
            "schema_version": self.schema_version,
            "export_id": self.export_id,
            "pipeline": self.pipeline,
            "delivery_path": self.delivery_path,
            "stages": [s.to_dict() for s in self.stages],
            "cpu_upload_bytes": self.cpu_upload_bytes,
            "cpu_download_bytes": self.cpu_download_bytes,
            "fallback_root_cause": self.fallback_root_cause,
            "native_encode_stage_stats": self.native_encode_stage_stats,
            "frame_attributions": list(self.frame_attributions),
            "full_gpu": self.full_gpu,
            "full_gpu_blockers": list(self.full_gpu_blockers),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


class ExportTelemetryRecorder:
    """导出过程中的线程安全阶段累加器。

    用法：导出开始时创建，各阶段调用 :meth:`record_stage_sample`；
    fallback 经 :meth:`set_fallback_root_cause` 记录（首次生效，
    后续症状不覆盖）；结束调用 :meth:`snapshot` 产出报告。
    """

    def __init__(self, export_id: str, pipeline: str = "timeline",
                 delivery_path: str = "") -> None:
        if not export_id:
            raise ValueError("export_id 不能为空")
        self._lock = threading.Lock()
        self._export_id = export_id
        self._pipeline = pipeline
        self._delivery_path = delivery_path
        # name -> list of sample dicts
        self._samples: Dict[str, List[Dict[str, Any]]] = {}
        # name -> 首个段级 fallback reason（first-wins）
        self._stage_fallbacks: Dict[str, str] = {}
        self._fallback_root_cause: Optional[str] = None
        self._native_encode_stage_stats: Optional[Dict[str, Any]] = None
        self._attributions: List[_Attribution] = []
        self._extra_upload = 0
        self._extra_download = 0

    # ---------------- 记录接口 ---------------- #

    def record_stage_sample(
        self,
        name: str,
        backend: str,
        *,
        wall_ms: Optional[float] = None,
        gpu_submit_ms: Optional[float] = None,
        gpu_wait_ms: Optional[float] = None,
        cpu_upload_bytes: int = 0,
        cpu_download_bytes: int = 0,
        frame_index: Optional[int] = None,
    ) -> None:
        """登记一次阶段采样（每帧/每事件一次）。"""
        if name not in EXPORT_STAGE_NAMES:
            raise ValueError(f"未知导出阶段: {name}（词表: {EXPORT_STAGE_NAMES}）")
        if not backend:
            raise ValueError("stage backend 不能为空")
        for label, value in (("wall_ms", wall_ms),
                             ("gpu_submit_ms", gpu_submit_ms),
                             ("gpu_wait_ms", gpu_wait_ms)):
            if value is not None and value < 0.0:
                raise ValueError(f"{label} 不可为负: {value}")
        if cpu_upload_bytes < 0 or cpu_download_bytes < 0:
            raise ValueError("字节计数不可为负")
        with self._lock:
            self._samples.setdefault(name, []).append({
                "backend": backend,
                "wall_ms": wall_ms,
                "gpu_submit_ms": gpu_submit_ms,
                "gpu_wait_ms": gpu_wait_ms,
                "cpu_upload_bytes": cpu_upload_bytes,
                "cpu_download_bytes": cpu_download_bytes,
                "frame_index": frame_index,
            })

    def set_stage_fallback(self, stage: str, reason: str) -> None:
        """记录段级 fallback reason；同段首次生效（first-wins）。"""
        if not reason:
            raise ValueError("fallback reason 不可为空")
        with self._lock:
            self._stage_fallbacks.setdefault(stage, reason)

    def set_fallback_root_cause(
        self,
        reason: str,
        *,
        stage: str = "",
        frame_index: Optional[int] = None,
        origin: str = "",
    ) -> None:
        """记录 fallback 根因（first-wins：后续症状不得覆盖根因）。

        Args:
            reason: 机器可读根因（如 ``cpu_encoder_surface_path``）。
            stage: 关联阶段名（可空）。
            frame_index: 发生根因的帧号。
            origin: 关联的 source/effect/node ID。
        """
        if not reason:
            raise ValueError("fallback root cause 不可为空")
        with self._lock:
            if self._fallback_root_cause is None:
                self._fallback_root_cause = reason
            if len(self._attributions) < _MAX_FRAME_ATTRIBUTIONS:
                self._attributions.append(
                    _Attribution(reason=reason, stage=stage,
                                 frame_index=frame_index, origin=origin))

    def add_cpu_transfer(
        self, upload_bytes: int = 0, download_bytes: int = 0
    ) -> None:
        """阶段外像素传输计数（例如 provider 级回读）。"""
        if upload_bytes < 0 or download_bytes < 0:
            raise ValueError("字节计数不可为负")
        with self._lock:
            self._extra_upload += upload_bytes
            self._extra_download += download_bytes

    def set_delivery_path(self, delivery_path: str) -> None:
        """设置/更正交付路径标记（T-P1-10；不参与 full_gpu 判定）。"""
        with self._lock:
            self._delivery_path = delivery_path

    def set_native_encode_stage_stats(self, stats: Any) -> None:
        """记录 native 编码阶段统计（T-P1-11；TOPOS 编码路径调用一次）。"""
        with self._lock:
            self._native_encode_stage_stats = stats

    # ---------------- 汇总 ---------------- #

    @staticmethod
    def _percentile(values: List[float], fraction: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
        return float(ordered[index])

    def snapshot(self) -> ExportStageTelemetryReport:
        with self._lock:
            stages: List[StageTelemetry] = []
            total_up = self._extra_upload
            total_down = self._extra_download
            for name in EXPORT_STAGE_NAMES:
                samples = self._samples.get(name)
                if not samples:
                    # 未采样阶段也显式输出（backend="none" = 未测量），
                    # 保证 schema 稳定、消费方不猜测缺段含义。
                    stages.append(StageTelemetry(
                        name=name, backend="none", sample_count=0,
                        fallback_reason=self._stage_fallbacks.get(name),
                    ))
                    continue
                walls = [s["wall_ms"] for s in samples if s["wall_ms"] is not None]
                submits = [s["gpu_submit_ms"] for s in samples
                           if s["gpu_submit_ms"] is not None]
                waits = [s["gpu_wait_ms"] for s in samples
                         if s["gpu_wait_ms"] is not None]
                uploads = sum(s["cpu_upload_bytes"] for s in samples)
                downloads = sum(s["cpu_download_bytes"] for s in samples)
                total_up += uploads
                total_down += downloads
                stages.append(StageTelemetry(
                    name=name,
                    backend=samples[-1]["backend"],
                    sample_count=len(samples),
                    wall_p50_ms=self._percentile(walls, 0.50),
                    wall_p95_ms=self._percentile(walls, 0.95),
                    gpu_submit_ms=(statistics.fmean(submits) if submits else None),
                    gpu_submit_p50_ms=self._percentile(submits, 0.50),
                    gpu_submit_p95_ms=self._percentile(submits, 0.95),
                    gpu_wait_ms=(statistics.fmean(waits) if waits else None),
                    cpu_upload_bytes=uploads,
                    cpu_download_bytes=downloads,
                    fallback_reason=self._stage_fallbacks.get(name),
                ))
            attributions = tuple(
                {
                    "reason": a.reason, "stage": a.stage,
                    "frame_index": a.frame_index, "origin": a.origin,
                }
                for a in self._attributions
            )
            return ExportStageTelemetryReport(
                export_id=self._export_id,
                pipeline=self._pipeline,
                delivery_path=self._delivery_path,
                stages=tuple(stages),
                cpu_upload_bytes=total_up,
                cpu_download_bytes=total_down,
                fallback_root_cause=self._fallback_root_cause,
                native_encode_stage_stats=self._native_encode_stage_stats,
                frame_attributions=attributions,
            )
