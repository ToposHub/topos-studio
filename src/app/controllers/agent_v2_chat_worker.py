"""Qt worker that runs a unified AgentRun chat on a background thread (plan §A1).

Wraps :class:`ChatRunDriver` (synchronous, Qt-free) in a ``QThread`` so the
existing controller wiring keeps working: it emits the **same signal names**
as ``_ChatWorker`` (``finished_reply`` / ``stream_delta`` / ``failed`` /
``cancelled``), which means ``AiEditController`` can reuse its current
callbacks (``_on_chat_finished`` / ``_on_chat_delta`` / ``_on_chat_failed`` /
``_on_chat_cancelled``) without changes.

It additionally emits ``finished_result`` with the full :class:`ChatRunResult`
so the controller can bridge real usage into the top bar (plan §A4).

Cooperative cancellation mirrors ``_ChatWorker``: setting ``cancel_requested``
does not interrupt the in-flight HTTP call; the driver checks the run state at
each phase boundary.  The actual cancel token is the ``AgentOrchestrator``
that the driver holds — the controller calls ``orchestrator.request_cancel``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from PyQt6.QtCore import QThread, pyqtSignal

from src.features.edit.ai_editing.chat_run_driver import (
    ChatRunDriver,
    ChatRunResult,
    classify_chat_result,
)

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from PyQt6.QtCore import QObject


class AgentV2ChatWorker(QThread):
    """Run one ``ChatRunDriver.run_chat`` call off the UI thread."""

    # --- signal surface (mirrors _ChatWorker so callbacks are reusable) --- #
    finished_reply = pyqtSignal(str)
    stream_delta = pyqtSignal(str)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()
    #: Full result object, emitted exactly once alongside the terminal signal
    #: so the controller can record usage / artifacts.
    finished_result = pyqtSignal(object)

    def __init__(
        self,
        driver: ChatRunDriver,
        *,
        session_id: str,
        user_text: str,
        messages: list[dict[str, Any]],
        mode: str = "chat",
        temperature: float = 0.6,
        max_tokens: int | None = None,
        stream: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._driver = driver
        self._session_id = session_id
        self._user_text = user_text
        self._messages = messages
        self._mode = mode
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._stream = stream
        #: Cooperative cancel flag (mirrors _ChatWorker.cancel_requested).
        self.cancel_requested: bool = False

    def run(self) -> None:  # noqa: D401 - QThread entry point
        on_delta: Callable[[str], None] | None = None
        if self._stream:
            on_delta = self.stream_delta.emit
        try:
            result: ChatRunResult = self._driver.run_chat(
                session_id=self._session_id,
                user_text=self._user_text,
                messages=self._messages,
                mode=self._mode,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                stream=self._stream,
                on_delta=on_delta,
            )
        except Exception as exc:  # pragma: no cover - driver already catches, defensive
            self.failed.emit(str(exc))
            return

        # Always surface the full result so the controller can record usage.
        self.finished_result.emit(result)

        category = classify_chat_result(result)
        if category == "cancelled":
            self.cancelled.emit()
        elif category == "failed":
            self.failed.emit(result.error or "模型调用失败")
        else:
            # AUD-19003: 结果已完整返回（非 cancelled/failed 类别）时，晚到的
            # cancel_requested 不再把成功回复改判为"已取消"——取消令牌只用于
            # 驱动器内部的协作取消（阶段边界检查，命中时结果类别是
            # "cancelled"）。这里按结果的真实类别分发，保证 UI 展示与
            # finished_result 携带的完整 usage/artifacts 一致。
            self.finished_reply.emit(result.reply or "")


__all__ = ["AgentV2ChatWorker"]
