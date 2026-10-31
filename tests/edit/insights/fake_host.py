"""T03/T02 测试用假宿主：实现 InsightsHostProtocol，记录导航调用。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from src.features.edit.insights.capture import capture_readonly_state
from src.features.edit.insights.host_protocol import (
    CAPTURE_NO_PROJECT,
    CaptureResult,
    NavigateRequest,
    NavigateResult,
    REASON_NO_PROJECT,
    ThumbnailRequest,
    ThumbnailResult,
)


@dataclass
class FakeHost:
    """capture 每次生成新 capture_id（与真实宿主刷新语义一致）。"""

    state_provider: Callable[[], Any]
    navigate_calls: list[NavigateRequest] = field(default_factory=list)
    navigate_ok: bool = True
    thumbnail_calls: list[ThumbnailRequest] = field(default_factory=list)
    fail_capture: bool = False

    def capture_readonly_state(self) -> CaptureResult:
        if self.fail_capture:
            return CaptureResult(
                ok=False, snapshot=None,
                error_code=CAPTURE_NO_PROJECT, message="no project loaded",
            )
        state = self.state_provider()
        snapshot = capture_readonly_state(
            state, capture_id=f"fake-{uuid.uuid4().hex[:8]}", captured_at=0.0
        )
        return CaptureResult(ok=True, snapshot=snapshot)

    def navigate_to(self, request: NavigateRequest) -> NavigateResult:
        self.navigate_calls.append(request)
        if self.navigate_ok:
            return NavigateResult(
                ok=True, status="navigated", reason="",
                actual_sequence_id=request.sequence_id,
                actual_frame=request.target_frame,
            )
        return NavigateResult(
            ok=False, status="rejected", reason=REASON_NO_PROJECT
        )

    def request_thumbnail(self, request: ThumbnailRequest) -> ThumbnailResult:
        self.thumbnail_calls.append(request)
        return ThumbnailResult(
            ok=False, quality="placeholder", message="fake host has no thumbnails"
        )
