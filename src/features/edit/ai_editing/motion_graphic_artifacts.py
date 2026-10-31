"""Motion Graphic build request + HTML artifact models (plan §2.6 / §2.5).

Typed replacements for the ``dict[str, Any]`` that currently threads through
the Motion Graphic pipeline.  These are the bridge objects the future
``TeachingAnimationAgentAdapter`` will hand to the ``AgentOrchestrator`` so
HTML generation, browser verification and the single confirm card all share
one observable, hashable contract.

Invariants baked into the types (plan §2.5):

- **Canvas comes from the timeline.**  ``MotionGraphicBuildRequest`` carries
  the current canvas width / height / fps; the model must not re-ask or
  substitute them.
- **Artifacts are not writable by default.**  ``HtmlArtifact.writable`` starts
  ``False``; only the explicit confirm action flips it, and only after the
  scene revision + hash checks pass.
- **Stale detection is data, not logic.**  ``scene_revision`` and
  ``context_fingerprint`` travel with the request and the artifact so a
  changed scene can be detected before writing.
- **No raw HTML in the chat log.**  The artifact stores hashes + a payload
  reference; the long document lives in the artifact store (plan §A4 §4.2 13).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from src.features.ai_harness.invocation_models import sanitize_for_storage


#: Current Motion Graphic transport protocol version (mirrors the agent layer).
MOTION_GRAPHIC_PROTOCOL_VERSION: str = "1.0"

#: Marker stamped into ``render_evidence`` by :meth:`HtmlArtifact.from_dict`.
#: Restored evidence is never trusted as fresh verification input (plan §2.5:
#: "a restored artifact is never trusted without fresh verification") —
#: :attr:`HtmlArtifact.has_verification_evidence` downgrades it so both write
#: gates require re-verification after a resume.
RESTORED_EVIDENCE_FLAG: str = "restored"


@dataclass(frozen=True)
class MotionGraphicBuildRequest:
    """A user's intent to build a Motion Graphic clip (plan §2.6).

    Fixed at creation time so the protected context (canvas, fps, scene
    revision, user brief) is immutable for the run; the compressor must never
    drop these fields (plan §A4 §4.2 step 12).
    """

    task_id: str
    run_id: str = ""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    scene_id: str = ""
    scene_revision: int = 0
    context_fingerprint: str = ""
    canvas_width: int = 0
    canvas_height: int = 0
    fps: float = 30.0
    insert_frame: int = 0
    render_mode: str = "html"
    user_brief: str = ""
    proposal_ref: str = ""
    protocol_version: str = MOTION_GRAPHIC_PROTOCOL_VERSION
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id cannot be empty")
        mode = str(self.render_mode or "html").strip().lower()
        object.__setattr__(self, "render_mode", mode)

    @property
    def canvas(self) -> tuple[int, int]:
        return (self.canvas_width, self.canvas_height)

    @property
    def has_canvas(self) -> bool:
        return self.canvas_width > 0 and self.canvas_height > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "scene_id": sanitize_for_storage(self.scene_id),
            "scene_revision": self.scene_revision,
            "context_fingerprint": self.context_fingerprint,
            "canvas_width": self.canvas_width,
            "canvas_height": self.canvas_height,
            "fps": self.fps,
            "insert_frame": self.insert_frame,
            "render_mode": self.render_mode,
            "user_brief": sanitize_for_storage(self.user_brief),
            "proposal_ref": self.proposal_ref,
            "protocol_version": self.protocol_version,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MotionGraphicBuildRequest":
        return MotionGraphicBuildRequest(
            task_id=str(d.get("task_id", "")),
            run_id=str(d.get("run_id", "")),
            request_id=str(d.get("request_id", "")) or uuid.uuid4().hex,
            scene_id=str(d.get("scene_id", "")),
            scene_revision=int(d.get("scene_revision", 0)),
            context_fingerprint=str(d.get("context_fingerprint", "")),
            canvas_width=int(d.get("canvas_width", 0)),
            canvas_height=int(d.get("canvas_height", 0)),
            fps=float(d.get("fps", 30.0)),
            insert_frame=int(d.get("insert_frame", 0)),
            render_mode=str(d.get("render_mode", "html")),
            user_brief=str(d.get("user_brief", "")),
            proposal_ref=str(d.get("proposal_ref", "")),
            protocol_version=str(
                d.get("protocol_version", MOTION_GRAPHIC_PROTOCOL_VERSION)
            ),
            created_at=float(d.get("created_at", 0.0)),
        )


@dataclass(frozen=True)
class HtmlArtifact:
    """A staged HTML document awaiting verification + approval (plan §2.6).

    Stores hashes + a payload reference only — the raw HTML lives in the
    artifact store, never inlined into the chat log.  ``writable`` is the
    single gate the confirm action must flip before any timeline write.
    """

    artifact_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    raw_html_hash: str = ""
    sanitized_html_hash: str = ""
    html_payload_ref: str = ""
    page_count: int = 0
    viewport_width: int = 0
    viewport_height: int = 0
    sanitizer_version: str = ""
    quality_report: dict[str, Any] = field(default_factory=dict)
    render_evidence: dict[str, Any] = field(default_factory=dict)
    approved_scene_hash: str = ""
    writable: bool = False
    protocol_version: str = MOTION_GRAPHIC_PROTOCOL_VERSION
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.artifact_id:
            raise ValueError("artifact_id cannot be empty")
        if self.page_count < 0:
            object.__setattr__(self, "page_count", 0)

    @property
    def viewport(self) -> tuple[int, int]:
        return (self.viewport_width, self.viewport_height)

    @property
    def has_verification_evidence(self) -> bool:
        """Whether real, freshly-captured browser evidence is present (§2.5 §9).

        AUD-14004：从持久化数据恢复的 render_evidence 带
        ``RESTORED_EVIDENCE_FLAG`` 标记，此处降权为"无证据"——恢复后必须
        重新走浏览器验证，不能沿用停机前的旧证据直接过写入门。
        """

        if not self.render_evidence:
            return False
        return not bool(self.render_evidence.get(RESTORED_EVIDENCE_FLAG))

    @property
    def is_staged(self) -> bool:
        """Whether a sanitized payload reference exists."""

        return bool(self.html_payload_ref and self.sanitized_html_hash)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "raw_html_hash": self.raw_html_hash,
            "sanitized_html_hash": self.sanitized_html_hash,
            "html_payload_ref": self.html_payload_ref,
            "page_count": self.page_count,
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "sanitizer_version": self.sanitizer_version,
            "quality_report": sanitize_for_storage(dict(self.quality_report)),
            "render_evidence": sanitize_for_storage(dict(self.render_evidence)),
            "approved_scene_hash": self.approved_scene_hash,
            "writable": self.writable,
            "protocol_version": self.protocol_version,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "HtmlArtifact":
        """Restore an artifact from persisted data (AUD-14004 修复后契约).

        不变量（motion_graphic_run.py："a restored artifact is never trusted
        without fresh verification"）：

        - ``writable`` **强制 False**：持久化里的确认态不复活，恢复后必须重新
          走用户确认；
        - 非空 ``render_evidence`` 打上 ``RESTORED_EVIDENCE_FLAG``，
          :attr:`has_verification_evidence` 据此降权，写入门要求重新验证。
        """
        restored_evidence = dict(d.get("render_evidence", {}))
        if restored_evidence:
            restored_evidence[RESTORED_EVIDENCE_FLAG] = True
        return HtmlArtifact(
            artifact_id=str(d.get("artifact_id", "")) or uuid.uuid4().hex,
            raw_html_hash=str(d.get("raw_html_hash", "")),
            sanitized_html_hash=str(d.get("sanitized_html_hash", "")),
            html_payload_ref=str(d.get("html_payload_ref", "")),
            page_count=int(d.get("page_count", 0)),
            viewport_width=int(d.get("viewport_width", 0)),
            viewport_height=int(d.get("viewport_height", 0)),
            sanitizer_version=str(d.get("sanitizer_version", "")),
            quality_report=dict(d.get("quality_report", {})),
            render_evidence=restored_evidence,
            approved_scene_hash=str(d.get("approved_scene_hash", "")),
            writable=False,
            protocol_version=str(
                d.get("protocol_version", MOTION_GRAPHIC_PROTOCOL_VERSION)
            ),
            created_at=float(d.get("created_at", 0.0)),
        )


# ---------------------------------------------------------------------- #
# Stale / write-gate helpers (plan §2.5 §8 / §2.7)
# ---------------------------------------------------------------------- #


def _has_staleness_basis(
    request: MotionGraphicBuildRequest,
    artifact: HtmlArtifact,
    *,
    current_scene_revision: int | None,
) -> bool:
    """Whether the caller supplied any basis to prove the scene is unchanged.

    两种依据（plan §2.5 §8）：当前 ``scene revision``，或可对比的
    ``approved_scene_hash`` vs ``request.context_fingerprint``。两者皆缺时
    stale 检测无法证明任何事。
    """
    if current_scene_revision is not None:
        return True
    return bool(artifact.approved_scene_hash and request.context_fingerprint)


def scene_is_stale(
    request: MotionGraphicBuildRequest,
    artifact: HtmlArtifact,
    *,
    current_scene_revision: int | None = None,
) -> bool:
    """Whether the scene changed between request and the artifact's approval.

    Plan §2.5 §8: confirmation must check scene revision + context fingerprint
    + artifact hash; a changed scene blocks the write.

    AUD-14003（fail-closed）：旧实现里两个守卫都是"可选的"——宿主未填指纹且
    未传当前 revision 时（``context_fingerprint`` 默认空串、
    ``current_scene_revision`` 默认 None），确认后的 artifact 无论场景怎么变
    都不会被判过期，写入门的 stale 检测被完全旁路。现在：

    - 提供了任一依据（revision 或可对比的指纹对）→ 按依据判定；
    - **确认已发生**（``writable=True`` 或盖了 ``approved_scene_hash`` 戳）但
      拿不到任何依据 → 无法证明场景未变更，视为 stale（fail-closed）——
      这正是审计指出的放行路径；
    - 未确认的 artifact（尚未进入确认流程）不做无依据的有罪推定：stale
      判定对它还不构成语义，写入本就被 staged/evidence/writable 门拦住。
    """
    if current_scene_revision is not None:
        if request.scene_revision != current_scene_revision:
            return True
        # revision 一致：若指纹也可对比则继续比对（双依据都要过）。
        if artifact.approved_scene_hash and request.context_fingerprint:
            return artifact.approved_scene_hash != request.context_fingerprint
        return False
    if artifact.approved_scene_hash and request.context_fingerprint:
        return artifact.approved_scene_hash != request.context_fingerprint
    # 既无 revision 依据也无指纹依据：确认已发生的 artifact 无法证明未过期。
    if artifact.writable or artifact.approved_scene_hash:
        return True
    return False


def can_write_artifact(
    request: MotionGraphicBuildRequest,
    artifact: HtmlArtifact,
    *,
    current_scene_revision: int | None = None,
) -> tuple[bool, str]:
    """Return ``(writable, reason)`` for staging a timeline write.

    The single gate: the artifact must be staged, verified, writable and not
    stale before the confirm action may proceed (plan §2.5 §9 / §2.6).
    """

    if not artifact.is_staged:
        return False, "HTML artifact 尚未完成清洗或未就绪"
    if not artifact.has_verification_evidence:
        return False, "缺少真实浏览器验证证据"
    if not artifact.writable:
        return False, "artifact 尚未被用户确认（writable=False）"
    if scene_is_stale(request, artifact, current_scene_revision=current_scene_revision):
        if _has_staleness_basis(
            request, artifact, current_scene_revision=current_scene_revision,
        ):
            return False, "场景在确认后已变更（scene revision / fingerprint 不一致）"
        return False, (
            "缺少场景版本依据（未提供 current_scene_revision，且 fingerprint/"
            "approved_scene_hash 不可对比），无法证明场景未变更——写入门 fail-closed 拒绝"
        )
    return True, ""


__all__ = [
    "HtmlArtifact",
    "MOTION_GRAPHIC_PROTOCOL_VERSION",
    "MotionGraphicBuildRequest",
    "RESTORED_EVIDENCE_FLAG",
    "can_write_artifact",
    "scene_is_stale",
]
