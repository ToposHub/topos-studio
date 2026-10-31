"""Bounded decode -> render -> encode scheduler for D3D11 surfaces."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import (
    Future,
    InvalidStateError,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Tuple

from src.shared.export.gpu_trail import ExportGpuTrail, TrailStage

from .d3d11_frame import D3D11GpuFrame
from .d3d11_render_graph import D3D11RenderResult, D3D11RenderTarget
from .native_bridge import NativeEncodeSubmitResult

__all__ = ["D3D11PipelineTelemetry", "D3D11PipelineScheduler"]

logger = logging.getLogger(__name__)

# run() 收割单个 outcome 的超时（AUD-36100）：done-callback 逃逸或原生桥
# 僵死时 outcome 永不完成，无界等待会让导出线程永久阻塞。
_OUTCOME_TIMEOUT_S: float = 60.0


class _FrameSource(Protocol):
    def read_frame(self) -> Tuple[Optional[D3D11GpuFrame], str]:
        ...


RenderFunction = Callable[[D3D11GpuFrame], D3D11RenderResult]
EncodeFunction = Callable[[D3D11RenderTarget], NativeEncodeSubmitResult]


@dataclass(frozen=True)
class D3D11PipelineTelemetry:
    surface_depth: int
    submitted: int
    completed: int
    failed: int
    peak_in_flight: int
    render_waits: int
    encode_waits: int
    fallback_reasons: Tuple[str, ...]
    # AUD-36102：surface 池槽位获取超时的独立计数——此前被记在
    # encode_waits 名下（名不副实），render_waits 则恒为 0。
    surface_pool_timeouts: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "surface_depth": self.surface_depth,
            "submitted": self.submitted,
            "completed": self.completed,
            "failed": self.failed,
            "peak_in_flight": self.peak_in_flight,
            "render_waits": self.render_waits,
            "encode_waits": self.encode_waits,
            "fallback_reasons": list(self.fallback_reasons),
            "surface_pool_timeouts": self.surface_pool_timeouts,
        }


class D3D11PipelineScheduler:
    """Use bounded in-flight work without ever touching CPU pixel memory."""

    def __init__(
        self,
        render: RenderFunction,
        encode: EncodeFunction,
        *,
        surface_depth: int = 4,
        trail: ExportGpuTrail | None = None,
    ) -> None:
        if not 4 <= surface_depth <= 8:
            raise ValueError("D3D11 surface_depth 必须在 4–8 槽之间")
        self._render = render
        self._encode = encode
        self._surface_depth = surface_depth
        self._trail = trail
        self._trail_stages: set[str] = set()
        self._slots = threading.BoundedSemaphore(surface_depth)
        self._render_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="topos-d3d11-render"
        )
        self._encode_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="topos-d3d11-encode"
        )
        self._lock = threading.Lock()
        self._closed = False
        self._in_flight = 0
        self._peak_in_flight = 0
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._render_waits = 0
        self._encode_waits = 0
        self._surface_pool_timeouts = 0
        # AUD-36102：单工作线程执行器上的排队等待计数（提交时仍有同型
        # 任务未完成即计一次等待——按提交时刻判定，任务串行执行时也能
        # 反映排队）
        self._render_pending = 0
        self._encode_pending = 0
        self._fallback_reasons: list[str] = []

    def _tracked_render(self, frame: D3D11GpuFrame) -> D3D11RenderResult:
        """Render with pending-slot bookkeeping (AUD-36102)."""
        try:
            return self._render(frame)
        finally:
            with self._lock:
                self._render_pending -= 1

    def _tracked_encode(self, target: D3D11RenderTarget) -> NativeEncodeSubmitResult:
        """Encode with pending-slot bookkeeping (AUD-36102)."""
        try:
            return self._encode(target)
        finally:
            with self._lock:
                self._encode_pending -= 1

    def submit(self, frame: D3D11GpuFrame, timeout_s: float = 1.0) -> Future[bool]:
        if self._closed:
            raise RuntimeError("D3D11PipelineScheduler 已关闭")
        if not self._slots.acquire(timeout=max(0.0, timeout_s)):
            # AUD-36102：这是 surface 池槽位超时，不是 encode 排队等待——
            # 独立计数，encode_waits 只统计编码执行器的真实排队。
            with self._lock:
                self._surface_pool_timeouts += 1
            self._record_failure("surface_pool_timeout")
            frame.release()
            return _failed_future("surface_pool_timeout")
        with self._lock:
            self._in_flight += 1
            self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
            self._submitted += 1
        outcome: Future[bool] = Future()
        with self._lock:
            if self._render_pending:
                self._render_waits += 1  # AUD-36102：真实 render 排队等待
            self._render_pending += 1
        try:
            render_future = self._render_executor.submit(self._tracked_render, frame)
        except RuntimeError as exc:
            # AUD-36103：入口 closed 检查与 executor 提交之间存在 TOCTOU——
            # 并发 close() 在此间隙 shutdown 执行器时 submit 抛 RuntimeError，
            # 异常发生在记账之后、回调注册之前。按提交失败语义收尾：冲销
            # pending 记账、释放 frame lease，槽位/in_flight/outcome 由
            # _finish_failed 归还并落定（不向 run() 泄漏异常，不丢槽位）。
            with self._lock:
                self._render_pending -= 1
            if not frame.lease.released:
                frame.release()
            self._finish_failed(outcome, f"pipeline_closed:{exc}")
            return outcome
        except BaseException:
            with self._lock:
                self._render_pending -= 1
            raise

        def _render_done(done: Future[D3D11RenderResult]) -> None:
            # AUD-36100：done-callback 是线程边界——任何异常逃逸都会使
            # 信号量槽泄漏且 outcome 永不完成，run() 的 future.result()
            # 永久阻塞。已知原生错误走精确类型；其余异常统一降级为失败
            # 并归还资源（不捕获 KeyboardInterrupt/SystemExit 等
            # BaseException）。
            render_result: Optional[D3D11RenderResult] = None
            encode_submitted = False
            try:
                try:
                    render_result = done.result()
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    self._record_trail_stage("decode", "d3d11va", frame.gpu_decode_ms)
                    self._record_trail_stage("render", "d3d11", None)
                    self._record_trail_stage("yuv_conversion", "d3d11", None)
                    frame.release()
                    self._finish_failed(outcome, f"render:{exc}")
                    return
                self._record_trail_stage("decode", "d3d11va", frame.gpu_decode_ms)
                self._record_trail_stage("render", "d3d11", render_result.gpu_render_ms)
                self._record_trail_stage("yuv_conversion", "d3d11", render_result.gpu_yuv_ms)
                if not render_result.ok or render_result.target is None:
                    # AUD-36101：渲染失败但 target 已分配时，target 的
                    # lease 同样必须归还——否则渲染目标池槽位泄漏。
                    if render_result.target is not None:
                        render_result.target.lease.release()
                    frame.release()
                    self._finish_failed(outcome, render_result.reason or "render_failed")
                    return
                target = render_result.target
                try:
                    frame.lease.release_after(target.completion_fence)
                except (RuntimeError, ValueError) as exc:
                    # fence 契约违约：target lease 之外，frame 自身的解码纹理
                    # lease 也必须归还，否则池容量永久损失一槽。
                    if not frame.lease.released:
                        frame.release()
                    target.lease.release()
                    self._finish_failed(outcome, f"render_fence:{exc}")
                    return
                with self._lock:
                    if self._encode_pending:
                        self._encode_waits += 1  # AUD-36102：真实 encode 排队等待
                    self._encode_pending += 1
                try:
                    encode_future = self._encode_executor.submit(
                        self._tracked_encode, target)
                except BaseException:
                    with self._lock:
                        self._encode_pending -= 1
                    raise
                encode_submitted = True

                def _encode_done(encoded: Future[NativeEncodeSubmitResult]) -> None:
                    # AUD-36100：与 _render_done 同因——callback 不允许逃逸。
                    try:
                        submit_result = encoded.result()
                        self._record_trail_stage(
                            "encode_submit", "d3d11_encoder",
                            submit_result.gpu_submit_ms,
                        )
                        if not submit_result.accepted:
                            target.lease.release()
                            self._finish_failed(outcome, submit_result.reason or "encode_failed")
                            return
                        target.lease.release_after(submit_result.completion_fence)
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        if not target.lease.released:
                            target.lease.release()
                        self._finish_failed(outcome, f"encode:{exc}")
                        return
                    except Exception as exc:
                        if outcome.done():
                            logger.warning(
                                "encode callback error after outcome settled: %s", exc)
                            return
                        if not target.lease.released:
                            target.lease.release()
                        self._finish_failed(
                            outcome,
                            f"encode_callback_unexpected:{type(exc).__name__}:{exc}",
                        )
                        return
                    with self._lock:
                        self._completed += 1
                    self._finish_success(outcome)

                encode_future.add_done_callback(_encode_done)
            except Exception as exc:
                # AUD-36100 兜底：处理链抛出未预期异常时，归还尚未交出的
                # 资源并强制完成 outcome，保证信号量槽必定归还。
                if outcome.done():
                    # 已由内层 finish 归还槽位——不得重复扣减/释放
                    logger.warning(
                        "render callback error after outcome settled: %s", exc)
                    return
                if not encode_submitted:
                    if (render_result is not None
                            and render_result.target is not None
                            and not render_result.target.lease.released):
                        render_result.target.lease.release()
                    if not frame.lease.released:
                        frame.release()
                self._finish_failed(
                    outcome,
                    f"render_callback_unexpected:{type(exc).__name__}:{exc}",
                )

        render_future.add_done_callback(_render_done)
        return outcome

    def run(self, source: _FrameSource, frame_count: int) -> Tuple[int, D3D11PipelineTelemetry]:
        if frame_count < 1:
            raise ValueError("frame_count 至少为 1")
        futures: list[Future[bool]] = []
        for _ in range(frame_count):
            frame, reason = source.read_frame()
            if frame is None:
                self._record_failure(reason or "decode_end_of_stream")
                break
            futures.append(self.submit(frame))
        completed = 0
        for future in futures:
            try:
                if future.result(timeout=_OUTCOME_TIMEOUT_S):
                    completed += 1
            except FutureTimeoutError:
                # AUD-36100：无界等待会让导出线程在僵死 outcome 上永久
                # 阻塞——超时把 outcome 落定为失败（迟到的回调经
                # outcome.done() 防护不会重复扣减）并继续收割后续帧。
                try:
                    future.set_result(False)
                except InvalidStateError:
                    pass
                self._record_failure("pipeline_future_timeout")
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self._record_failure(f"pipeline_future_error:{exc}")
        return completed, self.telemetry()

    def telemetry(self) -> D3D11PipelineTelemetry:
        with self._lock:
            return D3D11PipelineTelemetry(
                surface_depth=self._surface_depth,
                submitted=self._submitted,
                completed=self._completed,
                failed=self._failed,
                peak_in_flight=self._peak_in_flight,
                render_waits=self._render_waits,
                encode_waits=self._encode_waits,
                fallback_reasons=tuple(self._fallback_reasons),
                surface_pool_timeouts=self._surface_pool_timeouts,
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._render_executor.shutdown(wait=True, cancel_futures=False)
        self._encode_executor.shutdown(wait=True, cancel_futures=False)

    def _finish_success(self, future: Future[bool]) -> None:
        with self._lock:
            self._in_flight -= 1
        self._slots.release()
        if not future.done():
            future.set_result(True)

    def _finish_failed(self, future: Future[bool], reason: str) -> None:
        self._record_failure(reason)
        with self._lock:
            self._in_flight -= 1
        self._slots.release()
        if not future.done():
            future.set_result(False)

    def _record_failure(self, reason: str) -> None:
        with self._lock:
            self._failed += 1
            if reason not in self._fallback_reasons:
                self._fallback_reasons.append(reason)
        if self._trail is not None:
            try:
                self._trail.set_fallback_reason(reason)
            except ValueError:
                pass

    def _record_trail_stage(
        self, name: str, backend: str, gpu_submit_ms: float | None,
    ) -> None:
        if self._trail is None:
            return
        with self._lock:
            if name in self._trail_stages:
                return
            self._trail_stages.add(name)
        try:
            self._trail.record_stage(
                TrailStage(name=name, backend=backend, gpu_submit_ms=gpu_submit_ms)
            )
        except ValueError:
            # The trail enforces one stage per export.  If an injected caller
            # already recorded it, keep scheduler progress intact.
            pass


def _failed_future(reason: str) -> Future[bool]:
    future: Future[bool] = Future()
    future.set_result(False)
    return future
