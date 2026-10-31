"""Skill 目录与推荐表单（plan §10.3 P1.2）。

把 :class:`SkillRegistry` 的 manifest 转成聊天可展示的**推荐卡 + 可编辑输入
表单**，并在运行前显示风险 / 模型 / 证据前置条件（plan §10.3 P1.2 验收：

"用户能在聊天中查看 Skill 所需模型、预计工作和输出，确认后才开始"）。

本模块纯 Python（不依赖 Qt）；UI 渲染由 ``ai_skill_card.py`` 负责。它做三件事：

1. :func:`build_form_fields` —— 从 manifest.input_schema + ui_card.form_fields
   推导出有序、可编辑的表单字段（含类型 / 默认值 / 是否必填 / 选项）。
2. :func:`evaluate_prerequisites` —— 评估风险 / 模型 / 证据 / 权限前置条件，
   返回 :class:`PrereqReport`（未满足项明确，不静默启动）。
3. :class:`SkillCatalogView` —— 把 registry 转成聊天可列出的推荐卡视图。

红线（plan §10.3 P1.2 / §1.3）：

- Skill manifest 不能自行提权（SkillRegistry 已检查 forbidden capabilities；
  本层只读 manifest）。
- local_dev 必须标未验证（manifest.source.unverified）。
- 未满足前置条件不静默启动（PrereqReport.ready=False 时 UI 必须先提示）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from src.features.edit.ai_editing.skill_manifest import SkillManifest


# ---------------------------------------------------------------------- #
# 表单字段
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class FormField:
    """一个可编辑的输入字段（从 manifest.input_schema 推导）。"""

    name: str
    label: str
    field_type: str = "string"     # string / integer / number / enum / boolean / asset_ids
    required: bool = False
    default: Any = None
    options: tuple[str, ...] = ()  # enum 选项
    min_value: float | None = None
    max_value: float | None = None
    help_text: str = ""
    #: 是否影响 cache key（提示用户改它会重跑）。
    affects_cache: bool = True


def _safe_int(value: Any, default: int | None = None) -> int | None:
    """Convert a manifest-supplied value to ``int`` without trusting its type.

    Third-party / local_dev manifests may declare ``"ideal": "auto"`` or other
    non-numeric garbage; the catalog must degrade to ``default`` instead of
    raising and taking the whole catalog view down (AUD-22322).
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value: Any, default: float | None = None) -> float | None:
    """Convert a manifest-supplied value to ``float`` without trusting its type."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def build_form_fields(manifest: SkillManifest) -> list[FormField]:
    """从 manifest 推导有序表单字段。

    优先使用 ``ui_card.form_fields`` 声明的字段顺序；未声明的 required 字段
    也加入（asset_ids / timeline_id / project_id / brief_id 由 invocation 自动
    填充，不进表单）。
    """
    schema = manifest.input_schema or {}
    props: dict[str, Any] = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required: list[str] = list(schema.get("required", [])) if isinstance(schema, dict) else []
    ui_card = manifest.ui_card or {}
    declared_order: tuple[str, ...] = tuple(ui_card.get("form_fields", ()) or ())

    fields: list[FormField] = []
    seen: set[str] = set()

    # brief_template 里的 target_duration 默认（供 target_duration 字段兜底）。
    # 数值一律走 _safe_int：畸形 manifest 不得炸掉整个目录渲染（AUD-22322）。
    bt = manifest.brief_template or {}
    td_default = bt.get("target_duration") if isinstance(bt, dict) else None
    td_ideal = (
        _safe_int(td_default["ideal"])
        if isinstance(td_default, dict) and "ideal" in td_default else None
    )
    td_min = (
        _safe_int(td_default.get("min"), default=1)
        if isinstance(td_default, dict) else None
    )
    td_max = (
        _safe_int(td_default.get("max"), default=100000)
        if isinstance(td_default, dict) else None
    )

    # 1. 声明顺序优先
    for name in declared_order:
        if name in seen:
            continue
        seen.add(name)
        spec = props.get(name, {})
        fields.append(_field_from_spec(name, spec, required, ui_card, td_ideal, td_min, td_max))

    # 2. 补充 schema 里 required 但未声明的用户可编辑字段
    # （排除由 invocation 自动填的：asset_ids/timeline_id/project_id/brief_id）
    auto_filled = {"asset_ids", "timeline_id", "project_id", "brief_id"}
    for name in required:
        if name in seen or name in auto_filled:
            continue
        spec = props.get(name, {})
        seen.add(name)
        fields.append(_field_from_spec(name, spec, required, ui_card, td_ideal, td_min, td_max))

    # 3. target_duration 没声明但 brief_template 有默认 → 自动补一个
    if "target_duration" not in seen and td_ideal is not None:
        fields.append(FormField(
            name="target_duration", label="目标时长（帧）",
            field_type="integer", required=False, default=td_ideal,
            min_value=td_min, max_value=td_max,
            help_text="改时长会重新生成方案（影响缓存）",
        ))

    return fields


def _field_from_spec(
    name: str, spec: dict[str, Any], required: list[str], ui_card: dict[str, Any],
    td_ideal: int | None = None, td_min: int | None = None, td_max: int | None = None,
) -> FormField:
    json_type = spec.get("type", "string")
    field_type = {
        "string": "string", "integer": "integer", "number": "number",
        "boolean": "boolean", "array": "asset_ids",
    }.get(json_type, "string")
    # enum 支持
    if "enum" in spec and isinstance(spec["enum"], list):
        field_type = "enum"
    label = ui_card.get("labels", {}).get(name, _default_label(name))
    # target_duration 用 brief_template 的默认兜底（spec 通常无 default）
    default = spec.get("default")
    min_v = _safe_float(spec["minimum"]) if "minimum" in spec else None
    max_v = _safe_float(spec["maximum"]) if "maximum" in spec else None
    if name == "target_duration":
        if default is None and td_ideal is not None:
            default = td_ideal
        if min_v is None and td_min is not None:
            min_v = float(td_min)
        if max_v is None and td_max is not None:
            max_v = float(td_max)
    return FormField(
        name=name,
        label=str(label),
        field_type=field_type,
        required=name in required,
        default=default,
        options=tuple(spec.get("enum", [])) if isinstance(spec.get("enum"), list) else (),
        min_value=min_v,
        max_value=max_v,
        help_text=str(spec.get("description", "")),
    )


def _default_label(name: str) -> str:
    labels = {
        "target_duration": "目标时长",
        "target_language": "目标语言",
        "voice_style": "声音风格",
        "platform": "发布平台",
        "negative_prompt": "反向提示词",
        "seed": "随机种子",
    }
    return labels.get(name, name.replace("_", " ").capitalize())


# ---------------------------------------------------------------------- #
# 前置条件评估
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class PrereqCheck:
    """单项前置条件检查结果。"""

    category: str          # risk / model / evidence / permission / source
    ok: bool
    label: str             # "需要转录证据" / "需要 Qwen3-ASR 模型"
    detail: str = ""       # 未满足时的具体原因


@dataclass(frozen=True)
class PrereqReport:
    """一个 Skill 的前置条件总报告。"""

    skill_id: str
    ready: bool                       # 全部 ok
    checks: tuple[PrereqCheck, ...] = ()
    missing_evidence_kinds: tuple[str, ...] = ()
    missing_models: tuple[str, ...] = ()
    unverified: bool = False          # local_dev 未验证
    risk_label: str = ""              # 人类可读风险等级

    @property
    def blocking_messages(self) -> list[str]:
        """未通过项的可读消息（供 UI 列出）。"""
        return [f"{c.label}：{c.detail}" for c in self.checks if not c.ok]


#: 证据类型 -> 提示哪个模型 / provider 产它（用于"需要 X 模型"提示）。
_EVIDENCE_PRODUCER: dict[str, str] = {
    "transcript": "Qwen3-ASR / WhisperX（转录）",
    "shot": "SceneCut（镜头切分）",
    "ocr": "RapidOCR（文字识别）",
    "tag": "语义标签",
    "beat": "节拍检测",
    "loudness": "响度分析",
}


def evaluate_prerequisites(
    manifest: SkillManifest,
    *,
    available_evidence_kinds: Iterable[str] = (),
    installed_models: Iterable[str] = (),
    has_profile: bool = False,
) -> PrereqReport:
    """评估 manifest 的前置条件。

    Args:
        manifest: Skill 定义。
        available_evidence_kinds: 当前已具备的证据类型（SegmentKind.value）。
        installed_models: 已安装的模型 / pack id。
        has_profile: 是否配置了模型 profile。
    """
    avail_ev = set(available_evidence_kinds)
    installed = set(installed_models)
    checks: list[PrereqCheck] = []

    # 1. 风险
    risk_labels = {
        "read_only": "只读（安全）",
        "proposal_only": "仅提议（不写时间线）",
        "write_requires_approval": "写入需逐次审批",
    }
    risk_val = getattr(manifest.risk, "value", str(manifest.risk))
    checks.append(PrereqCheck(
        category="risk", ok=True,
        label="风险等级", detail=risk_labels.get(risk_val, risk_val),
    ))

    # 2. 来源验证
    source_val = getattr(manifest.source, "value", str(manifest.source))
    unverified = bool(getattr(manifest.source, "unverified", False))
    checks.append(PrereqCheck(
        category="source",
        ok=not unverified,
        label="来源",
        detail=("未验证（local_dev）" if unverified else f"已验证（{source_val}）"),
    ))

    # 3. profile
    checks.append(PrereqCheck(
        category="permission",
        ok=has_profile,
        label="模型 profile",
        detail=("已配置" if has_profile else "未配置（请先在设置里选模型）"),
    ))

    # 4. 证据前置
    required_ev = tuple(
        getattr(k, "value", str(k)) for k in manifest.required_evidence_kinds
    )
    missing_ev = tuple(k for k in required_ev if k not in avail_ev)
    for kind in required_ev:
        ok = kind in avail_ev
        producer = _EVIDENCE_PRODUCER.get(kind, kind)
        checks.append(PrereqCheck(
            category="evidence",
            ok=ok,
            label=f"证据：{kind}",
            detail=("已具备" if ok else f"缺失（需先运行 {producer}）"),
        ))

    # 5. 模型（启发式：从 manifest.roles / produces_artifact_kind 推断需要的模型；
    #    真实模型需求声明留 P3 skill_compiler）
    needed_models = _infer_needed_models(manifest)
    missing_models = tuple(m for m in needed_models if m not in installed)
    for m in needed_models:
        ok = m in installed
        checks.append(PrereqCheck(
            category="model",
            ok=ok,
            label=f"模型：{m}",
            detail=("已安装" if ok else "未安装（需先下载）"),
        ))

    ready = all(c.ok for c in checks) and not missing_ev and not missing_models
    return PrereqReport(
        skill_id=manifest.skill_id,
        ready=ready,
        checks=tuple(checks),
        missing_evidence_kinds=missing_ev,
        missing_models=missing_models,
        unverified=unverified,
        risk_label=risk_labels.get(risk_val, risk_val),
    )


def _infer_needed_models(manifest: SkillManifest) -> tuple[str, ...]:
    """启发式推断该 skill 需要的模型 / pack。

    从 required_evidence_kinds 反推（transcript → ASR pack；ocr → OCR pack）。
    多角色 skill 的真实模型需求由 P3 SkillCompiler 声明。
    """
    models: list[str] = []
    for k in manifest.required_evidence_kinds:
        kind = getattr(k, "value", str(k))
        if kind == "transcript":
            models.append("qwen3_asr")
        elif kind == "ocr":
            models.append("rapidocr_ocr")
        elif kind == "shot":
            models.append("scene_cut")
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for m in models:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return tuple(out)


# ---------------------------------------------------------------------- #
# SkillCatalogView（聊天推荐卡视图）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class SkillCardView:
    """聊天里一张 Skill 推荐卡的数据（UI 渲染用）。"""

    skill_id: str
    title: str
    description: str
    category: str = ""           # 编辑 / 分析 / 生成 / 字幕 / 配音
    risk_label: str = ""
    unverified: bool = False
    produces: str = ""           # "粗剪方案" / "字幕草稿" / "脚本"
    estimated_work: str = ""     # "分析 + 规划，约 30 秒"
    trigger_examples: tuple[str, ...] = ()  # "生成字幕" / "口播精剪"
    form_fields: tuple[FormField, ...] = ()
    prereq: PrereqReport | None = None


#: skill_id → 推荐 category / produces / 触发示例（集中，避免散落）。
_SKILL_META: dict[str, dict[str, Any]] = {
    "topos.subtitle_draft": {
        "category": "字幕",
        "produces": "字幕草稿（可编辑）",
        "estimated_work": "转录 + 字幕生成",
        "trigger_examples": ("生成字幕", "自动字幕", "上字幕"),
    },
    "topos.voiceover_polish": {
        "category": "编辑",
        "produces": "A/B 粗剪方案",
        "estimated_work": "分析 + 规划（约 20–40 秒）",
        "trigger_examples": ("口播精剪", "剪一版", "粗剪方案"),
    },
    "topos.product_ad_30s": {
        "category": "编辑",
        "produces": "A/B 广告方案",
        "estimated_work": "分析 + 规划（约 20–40 秒）",
        "trigger_examples": ("30 秒产品广告", "带货视频", "广告片"),
    },
}


def _merge_p2_meta() -> None:
    """把 P2 editorial_skills.P2_SKILL_META 合并进 _SKILL_META（运行时一次性）。"""
    try:
        from src.features.ai_harness.editorial_skills import P2_SKILL_META
        for sid, meta in P2_SKILL_META.items():
            _SKILL_META.setdefault(sid, meta)
    except Exception:  # noqa: BLE001 — editorial_skills 可能在该 import 阶段未就绪
        pass


_merge_p2_meta()


class SkillCatalogView:
    """把 SkillRegistry 转成聊天推荐卡视图（plan §10.3 P1.2）。

    只读 manifest；不修改 registry。提供：

    - :meth:`cards` —— 所有可见 skill 的推荐卡（含表单字段 + 前置条件）。
    - :meth:`card` —— 单个 skill 的卡（含前置条件评估）。
    - :meth:`recommend` —— 按当前上下文推荐（已具备证据的优先）。
    """

    def __init__(
        self,
        registry: Any,  # SkillRegistry（duck-typed: list_skills / get / is_enabled）
        *,
        available_evidence_kinds: Iterable[str] = (),
        installed_models: Iterable[str] = (),
        has_profile: bool = False,
    ) -> None:
        self._registry = registry
        self._avail_ev = tuple(available_evidence_kinds)
        self._installed = tuple(installed_models)
        self._has_profile = has_profile

    def cards(self, *, include_disabled: bool = False) -> list[SkillCardView]:
        out: list[SkillCardView] = []
        for m in self._registry.list_skills(include_disabled=include_disabled):
            out.append(self._build_card(m))
        out.sort(key=lambda c: c.title)
        return out

    def card(self, skill_id: str) -> SkillCardView | None:
        m = self._registry.get(skill_id)
        if m is None:
            return None
        return self._build_card(m)

    def recommend(self, *, limit: int = 5) -> list[SkillCardView]:
        """推荐：前置条件 ready 的优先，其次按 title。"""
        all_cards = self.cards()
        ready = [c for c in all_cards if c.prereq and c.prereq.ready]
        not_ready = [c for c in all_cards if not (c.prereq and c.prereq.ready)]
        ready.sort(key=lambda c: c.title)
        not_ready.sort(key=lambda c: c.title)
        return (ready + not_ready)[:limit]

    def _build_card(self, m: SkillManifest) -> SkillCardView:
        meta = _SKILL_META.get(m.skill_id, {})
        produces_art = getattr(m, "produces_artifact_kind", None)
        produces_label = meta.get("produces", "")
        if not produces_label:
            if getattr(m, "produces_plan", False):
                produces_label = "编辑方案"
            elif produces_art is not None:
                produces_label = str(getattr(produces_art, "value", produces_art))
        return SkillCardView(
            skill_id=m.skill_id,
            title=m.title or m.skill_id,
            description=m.description,
            category=meta.get("category", ""),
            risk_label=risk_label_of(m),
            unverified=bool(getattr(m.source, "unverified", False)),
            produces=produces_label,
            estimated_work=meta.get("estimated_work", ""),
            trigger_examples=tuple(meta.get("trigger_examples", ())),
            form_fields=tuple(build_form_fields(m)),
            prereq=evaluate_prerequisites(
                m,
                available_evidence_kinds=self._avail_ev,
                installed_models=self._installed,
                has_profile=self._has_profile,
            ),
        )


def risk_label_of(manifest: SkillManifest) -> str:
    risk_val = getattr(manifest.risk, "value", str(manifest.risk))
    return {
        "read_only": "只读",
        "proposal_only": "仅提议",
        "write_requires_approval": "需审批",
    }.get(risk_val, risk_val)


__all__ = [
    "FormField",
    "PrereqCheck",
    "PrereqReport",
    "SkillCardView",
    "SkillCatalogView",
    "build_form_fields",
    "evaluate_prerequisites",
    "risk_label_of",
]
