"""Motion Graphic quality gate (MG-18).

MG-18 gives the Motion Graphic pipeline its own quality gate, separate from
the general delivery gate.  "首帧可渲染" is necessary but not sufficient for a
15-second animation; the gate runs a battery of checks — first / middle / last
frame render, transparency, text overflow, safe area, flicker, lens blank,
asset resolvability, audio / video duration match, final encoding — and only
marks the clip as "deliverable" when every blocking check passes.

The gate composes the design validator (MG-10), the asset provenance store
(MG-14), the audio plan (MG-15) and the export postflight report (MG-17) into
one :class:`MotionGraphicQualityReport`.  It never re-renders itself; it
inspects the artifacts the host already produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .design_validator import DesignValidationOptions, validate_scene_design
from .error_codes import MOTION_GRAPHIC_ASSET_LICENSE_MISSING, MOTION_GRAPHIC_ASSET_UNRESOLVED, MOTION_GRAPHIC_RENDER_FAILED


# --------------------------------------------------------------------------- #
# Quality check kinds
# --------------------------------------------------------------------------- #


CHECK_FIRST_FRAME = "first_frame"
CHECK_MIDDLE_FRAME = "middle_frame"
CHECK_LAST_FRAME = "last_frame"
CHECK_KEY_FRAMES = "key_frames"
CHECK_TRANSPARENCY = "transparency"
CHECK_TEXT_OVERFLOW = "text_overflow"
CHECK_SAFE_AREA = "safe_area"
CHECK_FLICKER = "flicker"
CHECK_LENS_BLANK = "lens_blank"
CHECK_ASSET_RESOLVED = "asset_resolved"
CHECK_AUDIO_DURATION = "audio_duration"
CHECK_LOUDNESS = "loudness"
CHECK_FINAL_ENCODING = "final_encoding"
CHECK_DESIGN_VALIDATOR = "design_validator"

ALL_CHECKS: frozenset[str] = frozenset({
    CHECK_FIRST_FRAME, CHECK_MIDDLE_FRAME, CHECK_LAST_FRAME,
    CHECK_KEY_FRAMES, CHECK_TRANSPARENCY, CHECK_TEXT_OVERFLOW,
    CHECK_SAFE_AREA, CHECK_FLICKER, CHECK_LENS_BLANK,
    CHECK_ASSET_RESOLVED, CHECK_AUDIO_DURATION, CHECK_LOUDNESS,
    CHECK_FINAL_ENCODING, CHECK_DESIGN_VALIDATOR,
})


# --------------------------------------------------------------------------- #
# Finding + report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QualityFinding:
    check: str
    severity: str               # error / warning / info
    code: str
    message: str
    element_id: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "element_id": self.element_id,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class MotionGraphicQualityReport:
    findings: tuple[QualityFinding, ...] = ()
    checks_run: tuple[str, ...] = ()
    checks_skipped: tuple[str, ...] = ()

    @property
    def errors(self) -> tuple[QualityFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> tuple[QualityFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")

    @property
    def is_deliverable(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_deliverable": self.is_deliverable,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "checks_run": list(self.checks_run),
            "checks_skipped": list(self.checks_skipped),
            "findings": [f.to_dict() for f in self.findings],
        }


# --------------------------------------------------------------------------- #
# Gate inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RenderFrameResult:
    """One rendered frame's outcome, fed into the gate."""

    label: str                  # first / middle / last / keyframe
    frame_index: int
    succeeded: bool
    is_blank: bool = False
    has_transparency: bool = False
    error: str | None = None


@dataclass(frozen=True)
class QualityGateInputs:
    """All artifacts the gate needs to run its checks."""

    scene: Any
    render_frames: tuple[RenderFrameResult, ...] = ()
    asset_ids: tuple[str, ...] = ()
    resolved_asset_ids: tuple[str, ...] = ()
    asset_license_states: Mapping[str, str] = field(default_factory=dict)
    audio_duration_seconds: float | None = None
    clip_duration_seconds: float | None = None
    loudness_lufs: float | None = None
    target_loudness_lufs: float | None = None
    export_postflight_errors: tuple[str, ...] = ()
    flicker_frame_indices: tuple[int, ...] = ()
    design_options: DesignValidationOptions | None = None
    expected_duration_frames: int | None = None


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #


class MotionGraphicQualityGate:
    """Run the Motion Graphic quality checks over collected artifacts.

    The gate is pure and synchronous; the host gathers the inputs (render
    results, asset states, audio analysis) and passes them in.  Each check
    appends findings to a list; the final report aggregates them.
    """

    def run(self, inputs: QualityGateInputs) -> MotionGraphicQualityReport:
        findings: list[QualityFinding] = []
        checks_run: list[str] = []
        checks_skipped: list[str] = []

        # Frame render checks.
        for frame_result in inputs.render_frames:
            check_name = self._frame_check_name(frame_result.label)
            if check_name is None:
                continue
            checks_run.append(check_name)
            if not frame_result.succeeded:
                findings.append(QualityFinding(
                    check=check_name,
                    severity="error",
                    code=MOTION_GRAPHIC_RENDER_FAILED,
                    message=f"{frame_result.label} 帧渲染失败：{frame_result.error or '未知原因'}",
                    details={"frame_index": frame_result.frame_index},
                ))
            elif frame_result.is_blank:
                findings.append(QualityFinding(
                    check=CHECK_LENS_BLANK,
                    severity="error",
                    code=MOTION_GRAPHIC_RENDER_FAILED,
                    message=f"{frame_result.label} 帧为空白。",
                    details={"frame_index": frame_result.frame_index},
                ))

        # Transparency (AUD-27006): the host flags rendered frames whose
        # pixels contain alpha < 255.  The gate cannot know whether the scene
        # *intends* a transparent background, so transparency is surfaced as a
        # review-needed warning (it never blocks deliverability by itself),
        # and CHECK_TRANSPARENCY is accounted in checks_run whenever frame
        # results were supplied — never silently ignored.
        if inputs.render_frames:
            checks_run.append(CHECK_TRANSPARENCY)
            transparent_frames = [
                frame_result for frame_result in inputs.render_frames
                if frame_result.succeeded and frame_result.has_transparency
            ]
            if transparent_frames:
                findings.append(QualityFinding(
                    check=CHECK_TRANSPARENCY,
                    severity="warning",
                    code=MOTION_GRAPHIC_RENDER_FAILED,
                    message=(
                        f"{len(transparent_frames)} 帧包含透明像素"
                        f"（{', '.join(fr.label for fr in transparent_frames[:5])}）；"
                        "请确认交付编码是否需要透明通道。"
                    ),
                    details={"frame_indices": [fr.frame_index for fr in transparent_frames[:10]]},
                ))

        # Asset resolution + license.
        unresolved = [aid for aid in inputs.asset_ids if aid not in inputs.resolved_asset_ids]
        if unresolved:
            checks_run.append(CHECK_ASSET_RESOLVED)
            findings.append(QualityFinding(
                check=CHECK_ASSET_RESOLVED,
                severity="error",
                code=MOTION_GRAPHIC_ASSET_UNRESOLVED,
                message=f"未解析的素材：{', '.join(unresolved[:5])}",
                details={"unresolved": unresolved},
            ))
        missing_license = [
            aid for aid, state in inputs.asset_license_states.items()
            if state in {"missing", "restricted"} and aid in inputs.asset_ids
        ]
        if missing_license:
            checks_run.append("asset_license")
            findings.append(QualityFinding(
                check="asset_license",
                severity="error",
                code=MOTION_GRAPHIC_ASSET_LICENSE_MISSING,
                message=f"缺少许可的素材：{', '.join(missing_license[:5])}",
                details={"missing_license": missing_license},
            ))

        # Audio duration match.
        if inputs.audio_duration_seconds is not None and inputs.clip_duration_seconds is not None:
            checks_run.append(CHECK_AUDIO_DURATION)
            delta = inputs.audio_duration_seconds - inputs.clip_duration_seconds
            if abs(delta) > 0.2:
                severity = "error" if abs(delta) > 1.0 else "warning"
                findings.append(QualityFinding(
                    check=CHECK_AUDIO_DURATION,
                    severity=severity,
                    code=MOTION_GRAPHIC_RENDER_FAILED,
                    message=f"音频时长 {inputs.audio_duration_seconds:.2f}s 与片段 {inputs.clip_duration_seconds:.2f}s 不一致。",
                    details={"delta": delta},
                ))

        # Loudness.
        if inputs.loudness_lufs is not None and inputs.target_loudness_lufs is not None:
            checks_run.append(CHECK_LOUDNESS)
            delta = abs(inputs.loudness_lufs - inputs.target_loudness_lufs)
            if delta > 2.0:
                findings.append(QualityFinding(
                    check=CHECK_LOUDNESS,
                    severity="warning",
                    code=MOTION_GRAPHIC_RENDER_FAILED,
                    message=f"响度 {inputs.loudness_lufs:.1f} LUFS 偏离目标 {inputs.target_loudness_lufs:.1f}。",
                    details={"delta": delta},
                ))

        # Flicker.
        if inputs.flicker_frame_indices:
            checks_run.append(CHECK_FLICKER)
            findings.append(QualityFinding(
                check=CHECK_FLICKER,
                severity="warning",
                code=MOTION_GRAPHIC_RENDER_FAILED,
                message=f"检测到 {len(inputs.flicker_frame_indices)} 帧可能的闪烁。",
                details={"frames": list(inputs.flicker_frame_indices)[:10]},
            ))

        # Final encoding.
        if inputs.export_postflight_errors:
            checks_run.append(CHECK_FINAL_ENCODING)
            for error in inputs.export_postflight_errors:
                findings.append(QualityFinding(
                    check=CHECK_FINAL_ENCODING,
                    severity="error",
                    code=MOTION_GRAPHIC_RENDER_FAILED,
                    message=f"最终编码检查失败：{error}",
                ))

        # Design validator.
        checks_run.append(CHECK_DESIGN_VALIDATOR)
        design_report = validate_scene_design(
            inputs.scene,
            options=inputs.design_options,
            expected_duration_frames=inputs.expected_duration_frames,
        )
        for issue in design_report.findings:
            findings.append(QualityFinding(
                check=CHECK_DESIGN_VALIDATOR,
                severity=issue.severity,
                code=issue.code,
                message=issue.message,
                element_id=issue.element_id,
                details={"path": issue.path, "suggestion": issue.suggestion},
            ))

        return MotionGraphicQualityReport(
            findings=tuple(findings),
            checks_run=tuple(dict.fromkeys(checks_run)),
            checks_skipped=tuple(checks_skipped),
        )

    def _frame_check_name(self, label: str) -> str | None:
        mapping = {
            "first": CHECK_FIRST_FRAME,
            "middle": CHECK_MIDDLE_FRAME,
            "last": CHECK_LAST_FRAME,
            "keyframe": CHECK_KEY_FRAMES,
        }
        return mapping.get(label)


__all__ = [
    "MotionGraphicQualityGate",
    "MotionGraphicQualityReport",
    "QualityFinding",
    "QualityGateInputs",
    "RenderFrameResult",
    "ALL_CHECKS",
    "CHECK_FIRST_FRAME",
    "CHECK_MIDDLE_FRAME",
    "CHECK_LAST_FRAME",
    "CHECK_KEY_FRAMES",
    "CHECK_TRANSPARENCY",
    "CHECK_TEXT_OVERFLOW",
    "CHECK_SAFE_AREA",
    "CHECK_FLICKER",
    "CHECK_LENS_BLANK",
    "CHECK_ASSET_RESOLVED",
    "CHECK_AUDIO_DURATION",
    "CHECK_LOUDNESS",
    "CHECK_FINAL_ENCODING",
    "CHECK_DESIGN_VALIDATOR",
]
