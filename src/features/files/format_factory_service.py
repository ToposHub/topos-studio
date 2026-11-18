"""G1：format_factory 接管——安全转码/封装服务 + 原生命令服务。

G 节退出验收（format_factory 行）：**输入快照、输出计划/冲突、进度/
取消/失败恢复；不走旧 /launch**。

* :class:`FormatFactoryService`——纯域层：输入快照（路径+大小+mtime
  冻结）、输出计划（目标名/已存在冲突逐项列出）、冲突政策
  （rename/skip/overwrite 明确选择，无静默覆盖）；执行复用 E6 的
  ``ClipExportJob``（deadline 预算/协作取消/有界输出/临时文件+原子改
  名）；逐项进度与失败恢复（失败项可按原参数重试）。
* :class:`NativeToolCommandService`——F3/F4 同款三段命令
  （prepare 冻结→revalidate 权威复核→dispatch 提交）用于**原生**工具
  窗口打开：身份/取消/期限/库 revision 复核 +
  ``DispatchAuthority`` 原子边界（epoch 变更拒绝过期调用）；dispatch
  是 Qt 线程安全的窗口打开回调——**不经旧 HTTP /launch**。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from src.shared.playback.clip_export import ClipExportJob
from src.shared.plugins.dispatch import AuthorityRejected, DispatchAuthority
from src.shared.plugins.services import (
    CommandRejected,
    PreparedInvocation,
    ServiceResult,
    accepted,
    rejected,
)
from src.shared.plugins.lifecycle import InvocationContext, make_invocation

GESTURE_DEADLINE_SEC = 30.0
# 转码预算：单文件上限（封装秒级；8K 转码另议——超限如实失败可重试）
DEFAULT_ITEM_DEADLINE_SEC = 600.0

# 预设表：参数明确、无 shell 拼接（列表直传 ffmpeg）
PRESETS: dict[str, dict[str, Any]] = {
    "copy-mp4": {"title": "封装转换 → MP4（流复制）",
                 "suffix": ".mp4", "args": ["-c", "copy"]},
    "h264-mp4": {"title": "转码 → H.264/MP4",
                 "suffix": ".mp4",
                 "args": ["-c:v", "libx264", "-preset", "veryfast",
                          "-crf", "23", "-c:a", "aac", "-b:a", "160k"]},
    "prores-mov": {"title": "转码 → ProRes 422/MOV",
                   "suffix": ".mov",
                   "args": ["-c:v", "prores_ks", "-profile:v", "2",
                            "-c:a", "pcm_s16le"]},
}


@dataclass(frozen=True)
class InputSnapshot:
    """输入快照：手势时冻结——执行期漂移（换文件/改大小）按失败处理。"""

    path: str
    size_bytes: int
    mtime_ns: int

    @classmethod
    def capture(cls, path: str) -> "InputSnapshot":
        info = os.stat(path)
        return cls(path=str(path), size_bytes=info.st_size,
                   mtime_ns=info.st_mtime_ns)

    def matches_disk(self) -> bool:
        try:
            info = os.stat(self.path)
        except OSError:
            return False
        return (info.st_size == self.size_bytes
                and info.st_mtime_ns == self.mtime_ns)


@dataclass(frozen=True)
class PlanItem:
    source: InputSnapshot
    output_path: str
    conflict: str          # "" / "exists" / "planned-duplicate"


class FormatFactoryService:
    """转码/封装计划与执行（域层，不依赖 Qt）。"""

    def __init__(self, *, ffmpeg: str,
                 item_deadline_sec: float = DEFAULT_ITEM_DEADLINE_SEC) -> None:
        self._ffmpeg = str(ffmpeg)
        self._item_deadline = float(item_deadline_sec)
        self.jobs: dict[str, ClipExportJob] = {}

    # ------------------------------------------------------------ 计划
    def build_plan(self, inputs: Iterable[str], preset: str, *,
                   conflict_policy: str = "rename") -> tuple[PlanItem, ...]:
        """输入快照 + 输出计划；冲突逐项可见（不静默覆盖）。

        ``conflict_policy``：rename（自动改名 -1/-2…）/ skip（执行期跳过
        冲突项）/ overwrite（显式覆盖）。
        """
        if preset not in PRESETS:
            raise ValueError(f"未知预设：{preset}")
        if conflict_policy not in ("rename", "skip", "overwrite"):
            raise ValueError(f"未知冲突政策：{conflict_policy}")
        plan: list[PlanItem] = []
        taken: set[str] = set()
        for raw in inputs:
            snapshot = InputSnapshot.capture(str(raw))
            stem = Path(snapshot.path).stem
            suffix = PRESETS[preset]["suffix"]
            candidate = str(Path(snapshot.path).with_name(stem + suffix))
            conflict = ""
            if os.path.exists(candidate):
                conflict = "exists"
                if conflict_policy == "rename":
                    index = 1
                    while os.path.exists(f"{candidate[:-len(suffix)]}-{index}{suffix}"):
                        index += 1
                    candidate = f"{candidate[:-len(suffix)]}-{index}{suffix}"
                    conflict = "exists(resolved)"
            while candidate in taken:   # 同批同名（不同目录同名不冲突：全路径）
                candidate = str(Path(candidate).with_name(
                    Path(candidate).stem + "-dup" + Path(candidate).suffix))
                conflict = conflict or "planned-duplicate"
            taken.add(candidate)
            plan.append(PlanItem(source=snapshot, output_path=candidate,
                                 conflict=conflict))
        return tuple(plan)

    # ------------------------------------------------------------ 执行
    def execute_item(self, item: PlanItem, preset: str, *,
                     conflict_policy: str = "rename",
                     on_done: Callable[[bool, str], None] | None = None) -> str:
        """按计划执行单项 → 作业号（异步；进度/取消经 ClipExportJob）。

        执行期再验：输入快照漂移或输出已被占（skip 政策）→ 如实失败，
        不读入非手势时的文件。
        """
        if not item.source.matches_disk():
            raise ValueError(
                f"输入已变化（快照失配）：{item.source.path}")
        if conflict_policy == "skip" and os.path.exists(item.output_path):
            raise FileExistsError(
                f"输出已存在（skip 政策跳过）：{item.output_path}")
        destination = item.output_path
        if conflict_policy == "rename" and os.path.exists(destination):
            # 执行期 rename 解析：计划可能以其他政策生成（重试换政策），
            # 或输出在计划之后被占——冲突仍然显式解决，不静默覆盖
            suffix = Path(destination).suffix
            stem = str(destination)[:-len(suffix)] if suffix else str(destination)
            index = 1
            while os.path.exists(f"{stem}-{index}{suffix}"):
                index += 1
            destination = f"{stem}-{index}{suffix}"
        args = ["-y", "-loglevel", "error", "-i", item.source.path,
                *PRESETS[preset]["args"]]
        job = ClipExportJob([self._ffmpeg, *args],
                            destination=destination,
                            deadline_sec=self._item_deadline)
        job_id = f"fmt:{Path(item.source.path).name}:{int(time.time() * 1000):x}"
        self.jobs[job_id] = job

        def done(ok: bool, message: str) -> None:
            self.jobs.pop(job_id, None)
            if on_done is not None:
                on_done(ok, message)

        job.start(done)
        return job_id

    def cancel(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is not None:
            job.cancel()

    def cancel_all(self) -> int:
        for job in list(self.jobs.values()):
            job.cancel()
        return len(self.jobs)

    def retry_item(self, item: PlanItem, preset: str, *,
                   conflict_policy: str = "rename",
                   on_done: Callable[[bool, str], None] | None = None) -> str:
        """失败恢复：按原快照与政策重试（快照再验，漂移如实失败）。"""
        return self.execute_item(item, preset,
                                 conflict_policy=conflict_policy, on_done=on_done)


class NativeToolCommandService:
    """F3/F4 同款三段命令（原生工具窗口打开；不经旧 HTTP /launch）。"""

    def __init__(self, *, command_id: str, plugin_id: str,
                 revision_provider: Callable[[], int | None] | None = None,
                 authority: DispatchAuthority | None = None,
                 enabled_provider: Callable[[], bool] | None = None) -> None:
        self.command_id = command_id
        self.plugin_id = plugin_id
        self._revision_provider = revision_provider
        self._enabled_provider = enabled_provider
        self.authority = authority or DispatchAuthority()

    def prepare(self, entry_paths: Iterable[str]) -> PreparedInvocation:
        raw = list(entry_paths)
        if any(not isinstance(path, str) for path in raw):
            raise CommandRejected("INVALID_SELECTION", "所选路径必须是字符串列表")
        context: InvocationContext = make_invocation(
            command_id=self.command_id, plugin_id=self.plugin_id,
            entry_paths=tuple(raw),
            entry_revision=self._current_revision(),
            host_revision=self.authority.epoch,
            deadline=time.monotonic() + GESTURE_DEADLINE_SEC,
        )
        return PreparedInvocation(context=context, command_id=self.command_id,
                                  plugin_id=self.plugin_id,
                                  payload={"paths": list(raw)})

    def revalidate(self, prepared: PreparedInvocation) -> PreparedInvocation:
        if self._enabled_provider is not None and not self._enabled_provider():
            raise CommandRejected("PLUGIN_DISABLED", "插件已停用，请重新启用后发起。")
        if (prepared.command_id != self.command_id
                or prepared.plugin_id != self.plugin_id
                or prepared.context.plugin_id != prepared.plugin_id):
            raise CommandRejected("INVALID_INVOCATION", "调用身份与命令不匹配。")
        if prepared.context.cancelled:
            raise CommandRejected("INVOCATION_CANCELLED", "插件调用已取消。")
        if prepared.context.timed_out():
            raise CommandRejected("INVOCATION_EXPIRED", "插件调用已过期。")
        frozen = prepared.context.entry_revision
        if frozen:
            current = self._current_revision()
            if current and current != frozen:
                raise CommandRejected(
                    "LIBRARY_REVISED",
                    "素材库在启动期间已更新（重扫/切库），请重新发起。", retriable=True)
        return replace(prepared, revalidated=True)

    def dispatch(self, prepared: PreparedInvocation,
                 opener: Callable[[tuple[str, ...]], Any]) -> ServiceResult:
        """锁内复核 + 提交（F4 原子边界）；opener 只做 Qt 线程安全打开。"""
        def recheck() -> None:
            self.revalidate(prepared)

        def submit() -> Any:
            return opener(tuple(prepared.context.entry_paths))

        try:
            self.authority.check_and_submit(
                prepared.context.host_revision, recheck, submit)
        except (AuthorityRejected, CommandRejected) as exc:
            return rejected(self.command_id, error_code=getattr(exc, "error_code", "AUTHORITY_EPOCH_MOVED"),
                            message=exc.message if hasattr(exc, "message") else str(exc),
                            retriable=getattr(exc, "retriable", True),
                            invocation_id=prepared.context.invocation_id)
        return accepted(self.command_id,
                        invocation_id=prepared.context.invocation_id,
                        stage="dispatched",
                        message="工具窗口已按冻结选择打开。")

    def _current_revision(self) -> int:
        try:
            return int(self._revision_provider() or 0) \
                if self._revision_provider else 0
        except Exception:
            return 0


__all__ = [
    "FormatFactoryService", "NativeToolCommandService", "PRESETS",
    "InputSnapshot", "PlanItem",
]
