"""Color grading AI contracts: context, evidence, plan, shot match, HDR guard (CG-01~CG-09).

The audit found the color domain is almost entirely missing from the agent
layer: ``features/color/`` has the manual grading engine (ColorSession,
shader nodes, GPU scopes) but none of the typed objects an AI agent needs to
*safely propose* a grade — no ColorContextSnapshot, no ColorGradePlan, no shot
match workflow, no HDR validator.

This module fills CG-01 through CG-09 in one Qt-free contract layer under
``features/color/ai/``.  It composes with the existing rendering engine
rather than replacing it: the planner produces a :class:`ColorGradePlan`, the
host's grading engine applies it via its existing command stack.

Design rules (per plan §4.4):
* ColorContextSnapshot carries every color-management boundary (CG-01);
* ColorEvidence holds shot-level measurements the grade is based on (CG-02);
* ColorGradePlan is the dry-run, hash-bound, approval-gated execution plan
  (CG-03) — the model never touches shader state directly;
* ShotMatchWorkflow matches a reference shot with confidence + locked-shot
  protection (CG-04);
* HDRGuard validates peak nits / gamut / tone mapping and refuses
  incompatible transforms (CG-06);
* LookRecipe / BrandColorProfile carry versioned look + brand protection
  (CG-07);
* GradeDiffReport gives before/after comparison + automatic rollback on
  metric regression (CG-09).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a read-only, defensively-copied view of *value*.

    AUD-24301: frozen dataclass 的 Mapping 字段若持有可变 dict，审批后
    原地修改 ``op.parameters`` / ``context.metadata`` 即可改变
    ``plan_hash()`` 的结果，冻结声明形同虚设。MappingProxyType 封装后
    写入抛 TypeError，frozen 语义完整。
    """
    if isinstance(value, MappingProxyType):
        return value
    return MappingProxyType(dict(value))


# --------------------------------------------------------------------------- #
# Color space vocabulary (CG-01)
# --------------------------------------------------------------------------- #

SPACE_REC709 = "rec709"
SPACE_REC2020 = "rec2020"
SPACE_P3_D65 = "p3_d65"
SPACE_ACES_CG = "aces_cg"
SPACE_SRGB = "srgb"
SPACE_LOG_C = "arri_log_c"
SPACE_S_LOG3 = "sony_slog3"
SPACE_RED_LOG = "red_log3g10"

KNOWN_COLOR_SPACES: frozenset[str] = frozenset({
    SPACE_REC709, SPACE_REC2020, SPACE_P3_D65, SPACE_ACES_CG,
    SPACE_SRGB, SPACE_LOG_C, SPACE_S_LOG3, SPACE_RED_LOG,
})

HDR_PQ = "hdr_pq"
HDR_HLG = "hdr_hlg"
SDR = "sdr"

TRANSFER_FUNCTIONS: frozenset[str] = frozenset({HDR_PQ, HDR_HLG, SDR})


# --------------------------------------------------------------------------- #
# ColorContextSnapshot (CG-01)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ColorContextSnapshot:
    """Everything a color agent must know before proposing a grade.

    Captures source / working / display / output color management state plus
    user-locked items so the agent cannot accidentally clobber a manual grade.
    """

    project_id: str
    timeline_id: str | None
    working_space: str = SPACE_REC709
    display_transform: str = SPACE_REC709
    output_transform: str = SPACE_REC709
    timeline_transfer_function: str = SDR
    monitor_profile: str | None = None
    ocio_config: str | None = None
    locked_grade_node_ids: tuple[str, ...] = ()
    locked_clip_ids: tuple[str, ...] = ()
    detected_input_spaces: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.project_id:
            raise ValueError("ColorContextSnapshot.project_id is required")
        for name, value in {
            "working_space": self.working_space,
            "display_transform": self.display_transform,
            "output_transform": self.output_transform,
        }.items():
            if value not in KNOWN_COLOR_SPACES:
                raise ValueError(f"{name} not a known color space: {value!r}")
        if self.timeline_transfer_function not in TRANSFER_FUNCTIONS:
            raise ValueError(f"Unknown transfer function: {self.timeline_transfer_function!r}")
        # AUD-24301: Mapping 字段冻结为只读视图
        object.__setattr__(
            self, "detected_input_spaces",
            _freeze_mapping(self.detected_input_spaces))
        object.__setattr__(
            self, "metadata", _freeze_mapping(self.metadata))

    @property
    def is_hdr(self) -> bool:
        return self.timeline_transfer_function in {HDR_PQ, HDR_HLG}

    def is_locked(self, clip_id: str, node_id: str | None = None) -> bool:
        if clip_id in self.locked_clip_ids:
            return True
        if node_id and node_id in self.locked_grade_node_ids:
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "working_space": self.working_space,
            "display_transform": self.display_transform,
            "output_transform": self.output_transform,
            "timeline_transfer_function": self.timeline_transfer_function,
            "monitor_profile": self.monitor_profile,
            "ocio_config": self.ocio_config,
            "locked_grade_node_ids": list(self.locked_grade_node_ids),
            "locked_clip_ids": list(self.locked_clip_ids),
            "detected_input_spaces": dict(self.detected_input_spaces),
            "metadata": dict(self.metadata),
            "is_hdr": self.is_hdr,
        }


# --------------------------------------------------------------------------- #
# Color evidence (CG-02)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ColorMeasurement:
    """One shot-level color measurement (waveform / scope statistics)."""

    clip_id: str
    timestamp: float = 0.0
    mean_luma: float | None = None
    peak_luma: float | None = None          # 0..1 for SDR; nits for HDR
    shadow_point: float | None = None
    highlight_point: float | None = None
    saturation_mean: float | None = None
    skin_tone_deviation: float | None = None  # degrees off ideal skin-tone line
    warm_tint: float | None = None            # >0 warm, <0 cool
    clipping_ratio: float | None = None       # fraction of clipped pixels
    noise_floor: float | None = None
    vectorscope_tint: Mapping[str, float] = field(default_factory=dict)
    histogram_quantiles: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # AUD-24301: Mapping 字段冻结为只读视图
        object.__setattr__(
            self, "vectorscope_tint", _freeze_mapping(self.vectorscope_tint))
        object.__setattr__(
            self, "histogram_quantiles",
            _freeze_mapping(self.histogram_quantiles))

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "timestamp": self.timestamp,
            "mean_luma": self.mean_luma,
            "peak_luma": self.peak_luma,
            "shadow_point": self.shadow_point,
            "highlight_point": self.highlight_point,
            "saturation_mean": self.saturation_mean,
            "skin_tone_deviation": self.skin_tone_deviation,
            "warm_tint": self.warm_tint,
            "clipping_ratio": self.clipping_ratio,
            "noise_floor": self.noise_floor,
            "vectorscope_tint": dict(self.vectorscope_tint),
            "histogram_quantiles": dict(self.histogram_quantiles),
        }


# --------------------------------------------------------------------------- #
# Grade operations (CG-03 / CG-05)
# --------------------------------------------------------------------------- #


OP_WHITE_BALANCE = "white_balance"
OP_PRIMARY = "primary"                # lift / gamma / gain
OP_CURVES = "curves"
OP_QUALIFIER = "qualifier"            # HSL key
OP_WINDOW = "window"                  # mask shape
OP_TRACKER = "tracker"
OP_SKIN_PROTECTION = "skin_protection"
OP_DENOISE = "denoise"
OP_SHARPEN = "sharpen"
OP_VIGNETTE = "vignette"
OP_HALATION = "halation"
OP_LUT = "lut"
OP_CDL = "cdl"
OP_COLOR_TOKEN = "color_token"

SUPPORTED_GRADE_OPS: frozenset[str] = frozenset({
    OP_WHITE_BALANCE, OP_PRIMARY, OP_CURVES, OP_QUALIFIER, OP_WINDOW,
    OP_TRACKER, OP_SKIN_PROTECTION, OP_DENOISE, OP_SHARPEN,
    OP_VIGNETTE, OP_HALATION, OP_LUT, OP_CDL, OP_COLOR_TOKEN,
})


@dataclass(frozen=True)
class GradeOperation:
    """One typed, dry-run-safe color operation in a grade plan."""

    op_id: str
    kind: str
    clip_id: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    mask_ref: str | None = None
    tracker_ref: str | None = None
    label: str = ""

    def __post_init__(self) -> None:
        if not self.op_id:
            raise ValueError("GradeOperation.op_id is required")
        if self.kind not in SUPPORTED_GRADE_OPS:
            raise ValueError(f"Unsupported grade op kind: {self.kind!r}")
        if not self.clip_id:
            raise ValueError("GradeOperation.clip_id is required")
        # AUD-24301: Mapping 字段冻结为只读视图——审批后原地修改
        # parameters 不再能改变 plan_hash() 绑定
        object.__setattr__(
            self, "parameters", _freeze_mapping(self.parameters))

    def to_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "kind": self.kind,
            "clip_id": self.clip_id,
            "parameters": dict(self.parameters),
            "mask_ref": self.mask_ref,
            "tracker_ref": self.tracker_ref,
            "label": self.label,
        }


# --------------------------------------------------------------------------- #
# ColorGradePlan (CG-03)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ColorGradePlan:
    """The dry-run, hash-bound, approval-gated color grade plan.

    The model proposes this; the host's grading engine applies it through the
    existing color command stack.  The model never touches shader state.
    """

    plan_id: str
    project_id: str
    timeline_id: str
    context_snapshot_hash: str
    base_revision: int
    operations: tuple[GradeOperation, ...]
    target_look: str = ""
    render_cost_estimate: float = 0.0
    risk_flags: tuple[str, ...] = ()
    preview_frame_refs: tuple[str, ...] = ()
    lut_refs: tuple[str, ...] = ()
    source: str = "llm"

    def __post_init__(self) -> None:
        if not self.plan_id:
            raise ValueError("ColorGradePlan.plan_id is required")
        if not self.operations:
            raise ValueError("ColorGradePlan.operations cannot be empty")
        if not self.context_snapshot_hash:
            raise ValueError("ColorGradePlan.context_snapshot_hash is required")

    @property
    def is_empty(self) -> bool:
        return not self.operations

    def affected_clip_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for op in self.operations:
            if op.clip_id not in seen:
                seen.append(op.clip_id)
        return tuple(seen)

    def plan_hash(self) -> str:
        payload = {
            "plan_id": "",  # excluded; hash is content-based
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "context_snapshot_hash": self.context_snapshot_hash,
            "base_revision": self.base_revision,
            "operations": [op.to_dict() for op in self.operations],
            "target_look": self.target_look,
            "lut_refs": list(self.lut_refs),
            "source": self.source,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "context_snapshot_hash": self.context_snapshot_hash,
            "base_revision": self.base_revision,
            "operations": [op.to_dict() for op in self.operations],
            "target_look": self.target_look,
            "render_cost_estimate": self.render_cost_estimate,
            "risk_flags": list(self.risk_flags),
            "preview_frame_refs": list(self.preview_frame_refs),
            "lut_refs": list(self.lut_refs),
            "source": self.source,
            "plan_hash": self.plan_hash(),
        }


# --------------------------------------------------------------------------- #
# Shot match (CG-04)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ShotMatchSuggestion:
    """One shot's match toward a reference, with confidence."""

    clip_id: str
    reference_clip_id: str
    confidence: float
    suggested_operations: tuple[GradeOperation, ...]
    exposure_delta: float | None = None
    wb_delta: float | None = None
    saturation_delta: float | None = None
    excluded: bool = False
    exclude_reason: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")


@dataclass(frozen=True)
class ShotMatchReport:
    """Result of a shot-match workflow."""

    reference_clip_id: str
    suggestions: tuple[ShotMatchSuggestion, ...]
    hero_locked: bool = False
    auto_apply_threshold: float = 0.75  # confidence below this requires confirmation

    def auto_applicable(self) -> tuple[ShotMatchSuggestion, ...]:
        return tuple(
            s for s in self.suggestions
            if not s.excluded
            and s.confidence >= self.auto_apply_threshold
            and not s.exclude_reason
        )

    def needs_confirmation(self) -> tuple[ShotMatchSuggestion, ...]:
        return tuple(
            s for s in self.suggestions
            if not s.excluded
            and (s.confidence < self.auto_apply_threshold or s.exclude_reason)
        )


# --------------------------------------------------------------------------- #
# HDR guard (CG-06)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HDRViolation:
    code: str
    severity: str            # error / warning
    message: str
    clip_id: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # AUD-24301: Mapping 字段冻结为只读视图
        object.__setattr__(
            self, "details", _freeze_mapping(self.details))


@dataclass(frozen=True)
class HDRGuardReport:
    """Result of validating a grade plan against HDR / gamut constraints."""

    violations: tuple[HDRViolation, ...] = ()
    peak_nits_observed: float | None = None
    max_code_value: float | None = None

    @property
    def is_blocking(self) -> bool:
        return any(v.severity == "error" for v in self.violations)

    @property
    def errors(self) -> tuple[HDRViolation, ...]:
        return tuple(v for v in self.violations if v.severity == "error")


def validate_hdr(
    plan: ColorGradePlan,
    context: ColorContextSnapshot,
    *,
    measurements: Mapping[str, ColorMeasurement] | None = None,
    peak_nit_limit: float = 1000.0,
) -> HDRGuardReport:
    """Check a grade plan for HDR / gamut violations (CG-06).

    The actual measurement values come from the host's GPU scopes; this
    function validates them against the plan + context.  It refuses:

    * an SDR-targeted LUT applied to an HDR timeline (and vice versa);
    * a plan whose measured peak exceeds the nit limit;
    * an incompatible transfer-function transform.
    """

    violations: list[HDRViolation] = []
    peak_nits: float | None = None
    max_code: float | None = None

    for op in plan.operations:
        if op.kind == OP_LUT:
            target_tf = str(op.parameters.get("transfer_function", context.timeline_transfer_function))
            if target_tf not in TRANSFER_FUNCTIONS:
                # AUD-24302: 未知取值（拼写错误/非标准值/str(None)）此前
                # 两个兼容分支都不命中而被静默放行；CG-06 的职责是拒绝
                # 不兼容变换，超词表取值必须报 error 违规。
                violations.append(HDRViolation(
                    code="HDR_UNKNOWN_TRANSFER_FUNCTION",
                    severity="error",
                    message=f"未知的 LUT 传递函数 {target_tf!r}（合法值："
                            f"{sorted(TRANSFER_FUNCTIONS)}）。",
                    clip_id=op.clip_id,
                    details={"op_id": op.op_id,
                             "transfer_function": target_tf},
                ))
            elif context.is_hdr and target_tf == SDR:
                violations.append(HDRViolation(
                    code="HDR_SDR_LUT_ON_HDR_TIMELINE",
                    severity="error",
                    message="不能在 HDR 时间线上应用 SDR LUT。",
                    clip_id=op.clip_id,
                    details={"op_id": op.op_id},
                ))
            elif not context.is_hdr and target_tf in {HDR_PQ, HDR_HLG}:
                violations.append(HDRViolation(
                    code="HDR_LUT_ON_SDR_TIMELINE",
                    severity="error",
                    message="不能在 SDR 时间线上应用 HDR LUT。",
                    clip_id=op.clip_id,
                    details={"op_id": op.op_id},
                ))
        if op.kind == OP_PRIMARY:
            gain = op.parameters.get("gain")
            if isinstance(gain, (int, float)) and gain > 1.0 and context.is_hdr:
                violations.append(HDRViolation(
                    code="HDR_UNCLAMPED_GAIN",
                    severity="warning",
                    message="HDR 时间线上的 gain > 1.0 可能导致高光溢出。",
                    clip_id=op.clip_id,
                    details={"gain": gain},
                ))

    if measurements:
        for clip_id, m in measurements.items():
            if m.peak_luma is not None and context.is_hdr:
                peak_nits = m.peak_luma if peak_nits is None else max(peak_nits, m.peak_luma)
                if m.peak_luma > peak_nit_limit:
                    violations.append(HDRViolation(
                        code="HDR_PEAK_EXCEEDS_LIMIT",
                        severity="error",
                        message=f"{clip_id} 峰值 {m.peak_luma:.0f} nits 超过上限 {peak_nit_limit:.0f}。",
                        clip_id=clip_id,
                        details={"peak": m.peak_luma, "limit": peak_nit_limit},
                    ))
            if m.clipping_ratio is not None and m.clipping_ratio > 0.02:
                violations.append(HDRViolation(
                    code="CLIPPING_DETECTED",
                    severity="warning",
                    message=f"{clip_id} 检测到 {(m.clipping_ratio * 100):.1f}% 像素 clipping。",
                    clip_id=clip_id,
                    details={"ratio": m.clipping_ratio},
                ))

    return HDRGuardReport(
        violations=tuple(violations),
        peak_nits_observed=peak_nits,
        max_code_value=max_code,
    )


# --------------------------------------------------------------------------- #
# Look recipe + brand color profile (CG-07)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LookRecipe:
    """A versioned, reusable look (CG-07)."""

    recipe_id: str
    version: int
    label: str
    palette: Mapping[str, str] = field(default_factory=dict)
    lut_ref: str | None = None
    cdl_ref: str | None = None
    protected_skin_tones: bool = True
    protected_product_colors: tuple[str, ...] = ()
    license_state: str = "unknown"
    stylize_after_base_grade: bool = True

    def __post_init__(self) -> None:
        if not self.recipe_id:
            raise ValueError("LookRecipe.recipe_id is required")
        if self.version < 1:
            raise ValueError("version must be >= 1")
        # AUD-24301: Mapping 字段冻结为只读视图
        object.__setattr__(
            self, "palette", _freeze_mapping(self.palette))

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "version": self.version,
            "label": self.label,
            "palette": dict(self.palette),
            "lut_ref": self.lut_ref,
            "cdl_ref": self.cdl_ref,
            "protected_skin_tones": self.protected_skin_tones,
            "protected_product_colors": list(self.protected_product_colors),
            "license_state": self.license_state,
            "stylize_after_base_grade": self.stylize_after_base_grade,
        }


@dataclass(frozen=True)
class BrandColorProfile:
    """Brand-level color constraints (CG-07)."""

    brand_id: str
    label: str
    primary_colors: Mapping[str, str] = field(default_factory=dict)
    max_delta_e: Mapping[str, float] = field(default_factory=dict)
    forbidden_shifts: tuple[str, ...] = ()     # e.g. "shift_skin_to_brand"

    def __post_init__(self) -> None:
        # AUD-24301: Mapping 字段冻结为只读视图
        object.__setattr__(
            self, "primary_colors", _freeze_mapping(self.primary_colors))
        object.__setattr__(
            self, "max_delta_e", _freeze_mapping(self.max_delta_e))

    def to_dict(self) -> dict[str, Any]:
        return {
            "brand_id": self.brand_id,
            "label": self.label,
            "primary_colors": dict(self.primary_colors),
            "max_delta_e": dict(self.max_delta_e),
            "forbidden_shifts": list(self.forbidden_shifts),
        }


# --------------------------------------------------------------------------- #
# Grade diff + auto-rollback (CG-09)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GradeDiffFinding:
    clip_id: str
    metric: str               # mean_luma / saturation / clipping / skin_deviation
    before: float | None
    after: float | None
    delta: float
    regressed: bool


@dataclass(frozen=True)
class GradeDiffReport:
    """Before/after comparison; any regression can trigger automatic rollback."""

    findings: tuple[GradeDiffFinding, ...] = ()
    snapshot_artifact_ref: str | None = None

    @property
    def regressions(self) -> tuple[GradeDiffFinding, ...]:
        return tuple(f for f in self.findings if f.regressed)

    @property
    def should_auto_rollback(self) -> bool:
        return bool(self.regressions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [
                {"clip_id": f.clip_id, "metric": f.metric, "before": f.before,
                 "after": f.after, "delta": f.delta, "regressed": f.regressed}
                for f in self.findings
            ],
            "snapshot_artifact_ref": self.snapshot_artifact_ref,
            "should_auto_rollback": self.should_auto_rollback,
        }


def compare_grade(
    before: Mapping[str, ColorMeasurement],
    after: Mapping[str, ColorMeasurement],
    *,
    regression_threshold: float = 0.1,
) -> GradeDiffReport:
    """Compare pre-grade vs post-grade measurements (CG-09).

    A metric "regresses" when it moves in an undesirable direction by more
    than ``regression_threshold``: clipping increases, skin-tone deviation
    increases, noise floor increases.
    """

    findings: list[GradeDiffFinding] = []
    for clip_id, after_m in after.items():
        before_m = before.get(clip_id)
        if before_m is None:
            continue
        for metric, worse_when_higher in (
            ("clipping_ratio", True),
            ("skin_tone_deviation", True),
            ("noise_floor", True),
            ("mean_luma", None),
            ("saturation_mean", None),
        ):
            b = getattr(before_m, metric, None)
            a = getattr(after_m, metric, None)
            if b is None or a is None:
                continue
            delta = a - b
            regressed = False
            if worse_when_higher is True and delta > regression_threshold:
                regressed = True
            elif worse_when_higher is False and delta < -regression_threshold:
                regressed = True
            findings.append(GradeDiffFinding(
                clip_id=clip_id, metric=metric,
                before=b, after=a, delta=delta, regressed=regressed,
            ))
    return GradeDiffReport(findings=tuple(findings))


__all__ = [
    # vocab
    "KNOWN_COLOR_SPACES", "TRANSFER_FUNCTIONS",
    "SPACE_REC709", "SPACE_REC2020", "SPACE_P3_D65", "SPACE_ACES_CG",
    "SPACE_SRGB", "SPACE_LOG_C", "SPACE_S_LOG3", "SPACE_RED_LOG",
    "HDR_PQ", "HDR_HLG", "SDR",
    "SUPPORTED_GRADE_OPS",
    "OP_WHITE_BALANCE", "OP_PRIMARY", "OP_CURVES", "OP_QUALIFIER", "OP_WINDOW",
    "OP_TRACKER", "OP_SKIN_PROTECTION", "OP_DENOISE", "OP_SHARPEN",
    "OP_VIGNETTE", "OP_HALATION", "OP_LUT", "OP_CDL", "OP_COLOR_TOKEN",
    # types
    "ColorContextSnapshot",
    "ColorMeasurement",
    "GradeOperation",
    "ColorGradePlan",
    "ShotMatchSuggestion",
    "ShotMatchReport",
    "HDRViolation",
    "HDRGuardReport",
    "LookRecipe",
    "BrandColorProfile",
    "GradeDiffFinding",
    "GradeDiffReport",
    # functions
    "validate_hdr",
    "compare_grade",
]
