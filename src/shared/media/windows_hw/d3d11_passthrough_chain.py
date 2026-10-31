"""T0 零回读直通链编排器（计划 2026-09-15 §6 步骤 2）。

计划原文：

    打通最短的零回读 T0 链路：H.264/HEVC decode surface -> 同 GPU 的
    NV12/P010 encode surface；先不加效果，压缩码流回 CPU mux。
    验收：cpu_upload_bytes == 0、cpu_download_bytes == 0；帧数/PTS/码流
    可播放。若不能通过，禁止开始 HLSL 效果迁移。

职责边界：

- **真实实现**：三段 LUID 一致性校验（计划 §3.3 "解码、渲染、编码不在
  同一 adapter 时默认回退并记录 adapter_mismatch"）、逐帧
  surface 流转（frame → encode surface → submit → release）、
  `ExportGpuTrail` 计数（零回读是 trail 证明的，不是口头声明的）；
- **fail-closed 边界**：解码 surface 源与硬编提交都依赖原生桥，落地前
  由词表 `not_yet_implemented` 显式挡住——链路对齐了计划 §6 步骤 2 的
  禁止条件："若不能通过，禁止开始 HLSL 效果迁移"。

解码源协议：调用方注入实现 :class:`DecodeSurfaceSource` 的对象（未来
原生 MF D3D11VA 桥的原生侧）；本模块只消费协议，不做鸭子类型猜测。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Protocol, Tuple

from src.shared.export.gpu_trail import TrailStage

from .d3d11_device import D3D11Device
from .d3d11_frame import D3D11GpuFrame
from .gpu_encode_surface import D3D11EncodeSurfaceProvider
from .reasons import FallbackReason

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from src.shared.export.gpu_trail import ExportGpuTrail

logger = logging.getLogger(__name__)

__all__ = [
    "DecodeSurfaceSource",
    "PassthroughOutcome",
    "D3D11PassthroughChain",
]


class DecodeSurfaceSource(Protocol):
    """解码 surface 源协议（原生桥实现；测试注入假实现）。"""

    def read_frame(self) -> Tuple[Optional[D3D11GpuFrame], str]:
        """返回 (frame, reason)。frame 为 None 时 reason 非空。"""
        ...

    def adapter_luid(self) -> str:
        ...

    def close(self) -> None:
        ...


@dataclass(frozen=True)
class PassthroughOutcome:
    """run() 的结构化结果（成功帧数 / 中止原因 / trail 快照）。"""

    frames_completed: int
    ok: bool
    reason: str
    cpu_upload_bytes: int
    cpu_download_bytes: int
    zero_pixel_transfer: bool


class D3D11PassthroughChain:
    """decode surface → encode surface → (原生桥) 硬编 → 码流回 CPU mux。"""

    def __init__(
        self,
        device: D3D11Device,
        decode_source: DecodeSurfaceSource,
        encode_provider: D3D11EncodeSurfaceProvider,
        trail: "ExportGpuTrail",
    ) -> None:
        self._device = device
        self._decode_source = decode_source
        self._encode_provider = encode_provider
        self._trail = trail
        self._prepared = False

    def prepare(self) -> Tuple[bool, str]:
        """三段 LUID 一致性 + 编码 provider 预检（全真实校验）。"""
        ok, why = self._encode_provider.prepare()
        if not ok:
            return False, why
        device_luid = self._device.adapter_luid
        decode_luid = self._decode_source.adapter_luid()
        if not decode_luid:
            return False, (
                f"{FallbackReason.ADAPTER_MISMATCH.value}:"
                "decode_luid_unavailable"
            )
        if decode_luid != device_luid:
            # 计划 §3.3：绝不静默跨卡复制。
            return False, (
                f"{FallbackReason.ADAPTER_MISMATCH.value}:"
                f"decode={decode_luid}|device={device_luid}"
            )
        self._prepared = True
        return True, ""

    def run(self, frame_count: int) -> PassthroughOutcome:
        """逐帧流转。任何一段 fail-closed 立即中止并保留 trail 证据。"""
        if frame_count < 1:
            raise ValueError("frame_count 至少为 1")
        if not self._prepared:
            reason = FallbackReason.NOT_PROBED.value
            self._trail.set_fallback_reason(reason)
            outcome = PassthroughOutcome(
                frames_completed=0, ok=False,
                reason=reason,
                cpu_upload_bytes=0, cpu_download_bytes=0,
                zero_pixel_transfer=True,
            )
            return outcome
        completed = 0
        recorded_stages: set[str] = set()

        def record_stage(name: str, backend: str, gpu_ms: object) -> None:
            if name in recorded_stages:
                return
            recorded_stages.add(name)
            try:
                self._trail.record_stage(
                    TrailStage(
                        name=name,
                        backend=backend,
                        gpu_submit_ms=(float(gpu_ms) if gpu_ms is not None else None),
                    )
                )
            except (TypeError, ValueError) as exc:
                # AUD-36105：ExportGpuTrail 对同名段显式抛 ValueError——
                # 同一 trail 二次 run() 必然命中。重复登记不是本帧的失败
                # （首轮证据仍有效），此处吸收并留痕，绝不让异常带着
                # 已读出的 frame 逃逸（frame 泄漏 = decode 池槽位泄漏）。
                logger.warning(
                    "T0 直通链: trail 段 %s 登记被拒绝（已由首轮 run 记录）: %s",
                    name, exc,
                )

        for index in range(frame_count):
            frame, why = self._decode_source.read_frame()
            if frame is None:
                reason = f"decode_read_frame:{why or FallbackReason.GPU_PROBE_FAILED.value}"
                self._trail.set_fallback_reason(reason)
                return self._outcome(completed, False, reason)
            record_stage("decode", "d3d11va", frame.gpu_decode_ms)
            # T0 has no effect pass, but retaining explicit stage names makes
            # the evidence schema reject an unmeasured direct path instead of
            # treating passthrough as a complete render proof.
            record_stage("render", "d3d11_passthrough", frame.gpu_render_ms)
            record_stage("yuv_conversion", "d3d11", frame.gpu_yuv_ms)
            surface, why = self._encode_provider.surface_from_frame(frame)
            if surface is None:
                frame.release()
                reason = f"encode_surface:{why}"
                self._trail.set_fallback_reason(reason)
                return self._outcome(completed, False, reason)
            submitted, why = self._encode_provider.submit_to_encoder(surface)
            record_stage(
                "encode_submit", "d3d11_encoder",
                self._encode_provider.last_gpu_submit_ms,
            )
            if not submitted:
                surface.release()
                reason = f"encode_submit:{why}"
                self._trail.set_fallback_reason(reason)
                return self._outcome(completed, False, reason)
            fence = self._encode_provider.last_completion_fence
            if fence is None:
                surface.release()
                reason = (
                    "encode_submit:"
                    f"{FallbackReason.GPU_PROBE_FAILED.value}:completion_fence_missing"
                )
                self._trail.set_fallback_reason(reason)
                return self._outcome(completed, False, reason)
            try:
                surface.release_after(fence)
            except (RuntimeError, ValueError) as exc:
                # AUD-36104：release_after 失败时 surface 持有的共享 lease
                # （与来源帧同一个 decode 池槽位）绝不能悬空——立即同步
                # 归还兜底，否则池槽位永久损失（耗尽后导出死锁）。
                try:
                    surface.release()
                except (RuntimeError, TypeError, ValueError) as release_exc:
                    logger.error(
                        "T0 直通链: release_after 失败后的兜底归还也失败"
                        "（decode 池槽位可能泄漏）: %s", release_exc,
                    )
                reason = f"encode_submit:{FallbackReason.GPU_PROBE_FAILED.value}:{exc}"
                self._trail.set_fallback_reason(reason)
                return self._outcome(completed, False, reason)
            completed += 1
        return self._outcome(completed, True, "")

    def _outcome(
        self, completed: int, ok: bool, reason: str,
    ) -> PassthroughOutcome:
        snap = self._trail.snapshot()
        outcome = PassthroughOutcome(
            frames_completed=completed,
            ok=ok,
            reason=reason,
            cpu_upload_bytes=snap.cpu_upload_bytes,
            cpu_download_bytes=snap.cpu_download_bytes,
            zero_pixel_transfer=snap.zero_pixel_transfer,
        )
        logger.info(
            "T0 直通链: frames=%d ok=%s reason=%s up=%d down=%d",
            completed, ok, reason or "-",
            outcome.cpu_upload_bytes, outcome.cpu_download_bytes,
        )
        return outcome

    def close(self) -> None:
        self._decode_source.close()
