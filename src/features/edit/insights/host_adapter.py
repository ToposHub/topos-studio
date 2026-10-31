"""T02 · 注入式宿主适配器：G1 前后同形，工具包不导入 ``src.app.*``。

G1 集成时，宿主（EditModule 边界的 mixin / commands_facade 薄方法）把
三个既有能力以回调注入本适配器，``InsightsHostProtocol`` 即满足：

- ``capture_state_fn``：返回 ``ProjectSequences`` 形态（复用宿主
  ``get_project_sequences`` 的读取方式：``_current_edit_project`` +
  ``_current_timeline_sequences``；无工程返回 None——**不造空序列**）；
- ``navigate_fn``：UI 线程既有序列切换/播放头路径；拒绝语义见
  ``host_protocol`` 的 REASON_*；
- ``thumbnail_fn``：接既有 ThumbnailProvider 异步服务（源缩略图≠最终
  画面；离线→placeholder）。

G1 未合入期间，测试用 ``FakeHost``/注入 lambda 即可全量驱动工具包。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from src.features.edit.insights.capture import capture_readonly_state as _capture
from src.features.edit.insights.host_protocol import (
    CAPTURE_FAILED,
    CAPTURE_INCONSISTENT,
    CAPTURE_NO_PROJECT,
    CAPTURE_OK,
    CaptureResult,
    NavigateRequest,
    NavigateResult,
    REASON_NOT_SUPPORTED,
    THUMB_UNAVAILABLE,
    ThumbnailRequest,
    ThumbnailResult,
)


@dataclass
class InjectedHostAdapter:
    """由宿主注入回调组成的协议实现（无 app 依赖、无状态）。"""

    capture_state_fn: Callable[[], object | None]
    navigate_fn: Callable[[NavigateRequest], NavigateResult] | None = None
    thumbnail_fn: Callable[[ThumbnailRequest], ThumbnailResult] | None = None
    extra: dict = field(default_factory=dict)  # 宿主侧诊断信息（只读）

    def capture_readonly_state(self) -> CaptureResult:
        try:
            state = self.capture_state_fn()
        except Exception as exc:  # 宿主读取异常：结构化失败，不冒充空状态
            return CaptureResult(
                ok=False, snapshot=None,
                error_code=CAPTURE_FAILED, message=str(exc),
            )
        if state is None:
            return CaptureResult(
                ok=False, snapshot=None,
                error_code=CAPTURE_NO_PROJECT, message="no project loaded",
            )
        try:
            snapshot = _capture(state)
        except ValueError as exc:
            return CaptureResult(
                ok=False, snapshot=None,
                error_code=CAPTURE_INCONSISTENT, message=str(exc),
            )
        except Exception as exc:
            return CaptureResult(
                ok=False, snapshot=None,
                error_code=CAPTURE_FAILED, message=str(exc),
            )
        return CaptureResult(ok=True, snapshot=snapshot, error_code=CAPTURE_OK)

    def navigate_to(self, request: NavigateRequest) -> NavigateResult:
        if self.navigate_fn is None:
            return NavigateResult(
                ok=False, status="rejected", reason=REASON_NOT_SUPPORTED,
                message="host navigation not wired (G1 pending)",
            )
        return self.navigate_fn(request)

    def request_thumbnail(self, request: ThumbnailRequest) -> ThumbnailResult:
        if self.thumbnail_fn is None:
            return ThumbnailResult(
                ok=False, quality=THUMB_UNAVAILABLE,
                message="host thumbnails not wired (G1 pending)",
            )
        return self.thumbnail_fn(request)
