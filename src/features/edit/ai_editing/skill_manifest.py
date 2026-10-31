"""TAEP M9 — Skill Manifest：可版本化工作流包的声明式定义（§5 / §10.1 / §11.1 M9）。

本模块把 M7 的 ``WorkflowSpec`` **提升**为正式的 ``SkillManifest``：一个 Skill 不
是任意 prompt 或任意代码，而是带 id/version/author/source/signature、输入/输出
schema、工具 allowlist、风险/成本、UI 卡片与 eval 的**声明式工作流包**（§5.1）。

设计边界（红线，遵循 .clinerules / §0 / §1.2 / §5.3）：

- **Skill 不拥有裸权限，只声明所需工具/权限**（§3.1 / §5.3 第一条）：manifest 的
  ``tool_policy`` 是**声明式 allowlist**，运行时由 ``SkillRegistry`` 在
  ``ToolFacade.list_tools`` 上过滤；Skill 不能在安装后自动取得网络/写入/导出/生成额度。
- **来源分级 + 未验证标识**（§5.3 第二条）：``SkillSource`` 分 official /
  verified_third_party / local_dev；``local_dev`` 必须有醒目的未验证标识
  （``unverified=True``），UI 需提示。
- **manifest 版本/hash 不可变**（§5.3 第三条）：``SkillManifest`` 是 frozen；
  ``content_hash`` 是 manifest 内容的稳定摘要（更新内容 → hash 变 → 视为新版本，
  不改变旧会话已批准结果的解释）。
- **不做密码学签名验证**（v1 决策）：``signature`` 作为字段保留（content_hash + 算法
  标识），crypto 验签留给扩展市场批次（M10+）。v1 用 content_hash 做完整性自检。
- **不读磁盘 skill 包**（v1 决策）：manifest 是 Python 原生 dataclass + 可序列化为
  §5.1 的 ``skill.json`` dict 形态；磁盘加载/安装/升级/迁移是更重的扩展市场批次。

与 M7 ``WorkflowSpec`` 的关系：``SkillManifest`` 是 ``WorkflowSpec`` 的**超集**——
保留 ``required_evidence_kinds`` / ``produces_plan`` / ``produces_artifact_kind`` /
``target_beats`` / ``strategies`` / ``degrade_to`` / ``brief_template``（M7 既有
字段，向后兼容），**新增** version/source/author/signature/tool_policy/risk/
input_schema/output_schema/ui_card/eval_version/roles。三个 M7 工作流在此注册为官方
Skill（``OFFICIAL_VOICEOVER_POLISH`` 等）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.features.edit.ai_editing.artifact_models import ArtifactKind
from src.features.edit.ai_editing.editorial_models import SegmentKind
from src.features.edit.ai_editing.variant_planner import (
    STRATEGY_A_TALKING_HEAD,
    STRATEGY_B_BROLL,
)


# ---------------------------------------------------------------------- #
# Skill 来源与风险
# ---------------------------------------------------------------------- #


class SkillSource(str, Enum):
    """Skill 安装来源（§5.3 第二条）。

    ``OFFICIAL``：官方内置（随软件分发）。
    ``VERIFIED_THIRD_PARTY``：已验证第三方（有签名/审核记录）。
    ``LOCAL_DEV``：本地开发（**未验证**，UI 必须醒目标记）。
    """

    OFFICIAL = "official"
    VERIFIED_THIRD_PARTY = "verified_third_party"
    LOCAL_DEV = "local_dev"

    @property
    def unverified(self) -> bool:
        """本地开发 Skill 必须有未验证标识（§5.3）。"""
        return self == SkillSource.LOCAL_DEV


class SkillRisk(str, Enum):
    """Skill 风险等级（§5.1 / §10.1 "风险/成本"）。"""

    READ_ONLY = "read_only"        # 只读分析/检索，无 timeline 写入
    PROPOSAL_ONLY = "proposal_only"  # 产 variant/artifact，需用户批准才落地
    WRITE_REQUIRES_APPROVAL = "write_requires_approval"  # 含写操作，每次需 approval


# ---------------------------------------------------------------------- #
# 工具策略（声明式 allowlist + permission level）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolPolicy:
    """Skill 的工具策略（§5.1 ``tool_policy.json`` / §5.3 第一条）。

    ``allowed_tools`` 是**白名单**：Skill 运行时只能调用此列表中的工具。
    ``max_permission`` 是 permission ceiling：即使 allowlist 含 write 工具，也不超过
    此级（``SkillRegistry.tools_for`` 在 ``ToolFacade.list_tools`` 上双重过滤）。
    红线：Skill 不能声明 ``ToolFacade.FORBIDDEN_CAPABILITIES`` 里的能力（exec/shell/fs）。
    """

    allowed_tools: tuple[str, ...] = ()      # 白名单（tool_name，如 "topos.search_assets"）
    max_permission: str = "read"             # ToolPermission value（read/preview/proposal/write/...）
    forbidden_tools: tuple[str, ...] = ()    # 显式禁止（即便在 allowed 里也拒）

    def __post_init__(self) -> None:
        # 规范化：去重保序
        object.__setattr__(self, "allowed_tools", _dedup(self.allowed_tools))
        object.__setattr__(self, "forbidden_tools", _dedup(self.forbidden_tools))

    def permits(self, tool_name: str, tool_permission: str) -> bool:
        """该工具是否被本策略允许（allowlist ∩ 未在 forbidden ∩ permission ≤ ceiling）。"""
        if tool_name in self.forbidden_tools:
            return False
        if self.allowed_tools and tool_name not in self.allowed_tools:
            return False
        # permission ceiling 检查
        _PERM_ORDER = ("read", "preview", "proposal", "write", "expensive", "file_write")
        try:
            return _PERM_ORDER.index(tool_permission) <= _PERM_ORDER.index(self.max_permission)
        except ValueError:
            return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_tools": list(self.allowed_tools),
            "max_permission": self.max_permission,
            "forbidden_tools": list(self.forbidden_tools),
        }


def _dedup(seq: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if not seq:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for s in seq:
        s = str(s).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return tuple(out)


# ---------------------------------------------------------------------- #
# SkillSignature（内容 hash + 算法；v1 不做 crypto 验签）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class SkillSignature:
    """Skill manifest 的内容签名（§5.1 ``signature`` / §10.1）。

    v1 决策：``content_hash`` 是 manifest 稳定摘要（sha256），``algorithm`` 标识算法；
    **不做密码学验签**（crypto 验签 + 密钥分发留给扩展市场批次）。v1 用 content_hash
    做完整性自检（重新计算应一致）。
    """

    content_hash: str          # manifest 内容 sha256（hex）
    algorithm: str = "sha256-self"  # v1：自校验 hash（非 crypto 签名）
    signed_by: str = ""        # 签名者标识（official 时为 "topos"）

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_hash": self.content_hash, "algorithm": self.algorithm,
            "signed_by": self.signed_by,
        }


# ---------------------------------------------------------------------- #
# SkillManifest —— 核心
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class SkillManifest:
    """单个 Skill 的声明式定义（§5.1 / §10.1 / §11.1 M9 step 1）。

    frozen + 版本化 + 来源可追溯。``content_hash`` 由 ``compute_content_hash`` 从
    manifest 的**语义内容**计算（不含 hash/signature 本身），保证内容变 → hash 变。

    与 M7 ``WorkflowSpec`` 字段对应：``required_evidence_kinds`` / ``produces_plan`` /
    ``produces_artifact_kind`` / ``target_beats`` / ``strategies`` / ``degrade_to`` /
    ``brief_template`` 直接迁移（语义不变，向后兼容）。
    """

    # ---- 身份与版本（§5.1 skill.json 顶层）----
    skill_id: str                        # 稳定 id（如 "topos.voiceover_polish"）
    version: str = "1.0.0"               # semver
    title: str = ""                      # 用户可见名（"工作流"）
    description: str = ""
    author: str = ""                     # 作者/组织
    source: SkillSource = SkillSource.OFFICIAL
    license: str = ""                    # SPDX 或自定义（official 默认随软件）

    # ---- 工作流语义（M7 WorkflowSpec 迁移字段）----
    required_evidence_kinds: tuple[SegmentKind, ...] = ()
    produces_plan: bool = False
    produces_artifact_kind: ArtifactKind | None = None
    target_beats: tuple[str, ...] = ()
    strategies: tuple[str, ...] = ()
    degrade_to: str = ""
    brief_template: dict[str, Any] = field(default_factory=dict)

    # ---- Skill 新增（§5.1 / §10.1）----
    tool_policy: ToolPolicy = field(default_factory=lambda: ToolPolicy())
    risk: SkillRisk = SkillRisk.PROPOSAL_ONLY
    input_schema: dict[str, Any] = field(default_factory=dict)    # JSON Schema 片段
    output_schema: dict[str, Any] = field(default_factory=dict)
    ui_card: dict[str, Any] = field(default_factory=dict)         # 参数表单/结果卡片/i18n keys
    eval_version: str = ""               # eval suite 版本（§5.1 evals/）
    roles: tuple[str, ...] = ()          # 子智能体角色（TaskRole value 列表；空=单节点 Skill）

    # ---- 签名（content_hash 由 compute_content_hash 填充）----
    signature: SkillSignature | None = None

    def __post_init__(self) -> None:
        if not self.skill_id:
            raise ValueError("skill_id cannot be empty")
        # roles 规范化
        object.__setattr__(self, "roles", _dedup(self.roles))

    # ---------------- 查询 ---------------- #

    @property
    def unverified(self) -> bool:
        """本地开发 Skill：UI 必须醒目标记未验证（§5.3）。"""
        return self.source.unverified

    @property
    def content_hash(self) -> str:
        """manifest 内容 hash（签名缺失时即时计算）。"""
        if self.signature is not None:
            return self.signature.content_hash
        return compute_content_hash(self)

    # ---------------- 序列化 ---------------- #

    def to_dict(self) -> dict[str, Any]:
        """序列化为 §5.1 ``skill.json`` 形态（JSON-safe）。"""
        d: dict[str, Any] = {
            "skill_id": self.skill_id,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "author": self.author,
            "source": self.source.value,
            "license": self.license,
            "required_evidence_kinds": [k.value for k in self.required_evidence_kinds],
            "produces_plan": self.produces_plan,
            "produces_artifact_kind": self.produces_artifact_kind.value if self.produces_artifact_kind else None,
            "target_beats": list(self.target_beats),
            "strategies": list(self.strategies),
            "degrade_to": self.degrade_to,
            "brief_template": dict(self.brief_template),
            "tool_policy": self.tool_policy.to_dict(),
            "risk": self.risk.value,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "ui_card": dict(self.ui_card),
            "eval_version": self.eval_version,
            "roles": list(self.roles),
            "content_hash": self.content_hash,
            "unverified": self.unverified,
        }
        if self.signature is not None:
            d["signature"] = self.signature.to_dict()
        return d


# ---------------------------------------------------------------------- #
# content_hash 计算（语义内容 → sha256）
# ---------------------------------------------------------------------- #


def compute_content_hash(manifest: SkillManifest) -> str:
    """计算 manifest 内容的稳定 sha256（hex）。

    只哈希**语义内容**（不含 signature 字段本身，避免循环）。结果稳定：相同 manifest
    内容 → 相同 hash（§5.3 第三条：内容变 → hash 变 → 视为新版本）。
    """
    payload = {
        "skill_id": manifest.skill_id,
        "version": manifest.version,
        "title": manifest.title,
        "description": manifest.description,
        "author": manifest.author,
        "source": manifest.source.value,
        "license": manifest.license,
        "required_evidence_kinds": sorted(k.value for k in manifest.required_evidence_kinds),
        "produces_plan": manifest.produces_plan,
        "produces_artifact_kind": manifest.produces_artifact_kind.value if manifest.produces_artifact_kind else None,
        "target_beats": list(manifest.target_beats),
        "strategies": list(manifest.strategies),
        "degrade_to": manifest.degrade_to,
        "brief_template": manifest.brief_template,
        "tool_policy": manifest.tool_policy.to_dict(),
        "risk": manifest.risk.value,
        "input_schema": manifest.input_schema,
        "output_schema": manifest.output_schema,
        "ui_card": manifest.ui_card,
        "eval_version": manifest.eval_version,
        "roles": list(manifest.roles),
    }
    # sort_keys 保证字段顺序不影响 hash；ensure_ascii 保持稳定
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def with_signature(manifest: SkillManifest, *, signed_by: str = "") -> SkillManifest:
    """返回带签名的 manifest 副本（content_hash 即时计算，algorithm=sha256-self）。

    官方内置 manifest 用此函数在构造后补签名（frozen dataclass 不能原地改）。
    """
    sig = SkillSignature(
        content_hash=compute_content_hash(manifest),
        algorithm="sha256-self",
        signed_by=signed_by or ("topos" if manifest.source == SkillSource.OFFICIAL else ""),
    )
    from dataclasses import replace
    return replace(manifest, signature=sig)


# ---------------------------------------------------------------------- #
# 三个官方内置 Skill（M7 工作流产品化为 Skill）
# ---------------------------------------------------------------------- #


# 公共工具策略：三个官方 Skill 首版都用只读 + proposal 级工具（不声明 write/expensive）。
# 红线：官方 Skill 也不自动取得 write 权限；写入仍经宿主 approval/commit 闭环。
_OFFICIAL_TOOL_POLICY = ToolPolicy(
    allowed_tools=(
        "topos.get_capabilities",
        "topos.get_project_snapshot",
        "topos.search_assets",
        "topos.get_asset_analysis",
        "topos.validate_edit_plan",   # preview 级：编译+校验，不写
        "topos.search_help",
        "topos.explain_diagnostic",
        "topos.show_relevant_panel",  # proposal 级：仅提议导航
    ),
    max_permission="proposal",
    forbidden_tools=(
        "topos.apply_edit_plan",      # 写入只能由宿主经 approval 触发，Skill 不直接调
        "topos.render_preview",
        "topos.request_export",
    ),
)

# 官方 Skill 首版不引入子智能体（roles=()）——TaskGraph 单节点跑 M7 WorkflowRunner
# 逻辑。多角色 TaskGraph 是 v1 提供的能力（见 task_graph.py），但官方工作流首版
# 沿用 M7 单路径，保证向后兼容；roles 非空的 Skill 由 SkillRuntime 走 TaskGraph。

OFFICIAL_VOICEOVER_POLISH = with_signature(SkillManifest(
    skill_id="topos.voiceover_polish",
    version="1.0.0",
    title="口播精剪",
    description="基于转录证据生成 A/B 粗剪方案（口播优先 / B-roll 优先）。",
    author="Topos",
    source=SkillSource.OFFICIAL,
    license="Topos-proprietary",
    required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
    produces_plan=True,
    produces_artifact_kind=None,
    target_beats=("hook", "product_value", "call_to_action"),
    strategies=(STRATEGY_A_TALKING_HEAD, STRATEGY_B_BROLL),
    degrade_to=(
        "缺少转录证据：只能生成低置信度草稿。建议先对所选素材运行 "
        "editorial_local 分析（含转录）。首版'去停顿'依赖转录覆盖率；"
        "静音/beat 级精剪在 M8 音频证据扩展后提供。"
    ),
    brief_template={
        "required_beats": ("hook", "product_value", "call_to_action"),
        "target_duration": {"ideal": 720, "min": 648, "max": 792},
        "platform": "vertical_social",
    },
    tool_policy=_OFFICIAL_TOOL_POLICY,
    risk=SkillRisk.PROPOSAL_ONLY,
    input_schema={
        "type": "object",
        "required": ["asset_ids", "timeline_id", "project_id", "brief_id"],
        "properties": {
            "asset_ids": {"type": "array", "items": {"type": "string"}},
            "timeline_id": {"type": "string"},
            "brief_id": {"type": "string"},
            "target_duration": {"type": "integer"},
        },
    },
    output_schema={"type": "object", "properties": {"variants": {"type": "array"}}},
    ui_card={"form_fields": ("target_duration",), "result_card": "variant_compare"},
    eval_version="v1",
    roles=(),
))

OFFICIAL_PRODUCT_AD_30S = with_signature(SkillManifest(
    skill_id="topos.product_ad_30s",
    version="1.0.0",
    title="30 秒产品广告",
    description="产品 claim + 已选素材生成 A/B 广告方案（突出卖点与行动号召）。",
    author="Topos",
    source=SkillSource.OFFICIAL,
    license="Topos-proprietary",
    required_evidence_kinds=(SegmentKind.TRANSCRIPT, SegmentKind.SHOT, SegmentKind.TAG),
    produces_plan=True,
    produces_artifact_kind=None,
    target_beats=("hook", "problem", "product_value", "call_to_action"),
    strategies=(STRATEGY_A_TALKING_HEAD, STRATEGY_B_BROLL),
    degrade_to=(
        "缺少必需证据（转录/镜头/标签）：只能生成低置信度草稿。建议先运行 "
        "editorial_local 或 visual_semantic_local 分析补齐证据。"
    ),
    brief_template={
        "required_beats": ("hook", "problem", "product_value", "call_to_action"),
        "target_duration": {"ideal": 720, "min": 648, "max": 792},
        "platform": "vertical_social",
    },
    tool_policy=_OFFICIAL_TOOL_POLICY,
    risk=SkillRisk.PROPOSAL_ONLY,
    input_schema={
        "type": "object",
        "required": ["asset_ids", "timeline_id", "project_id", "brief_id"],
        "properties": {
            "asset_ids": {"type": "array", "items": {"type": "string"}},
            "timeline_id": {"type": "string"},
            "brief_id": {"type": "string"},
            "target_duration": {"type": "integer"},
        },
    },
    output_schema={"type": "object", "properties": {"variants": {"type": "array"}}},
    ui_card={"form_fields": ("target_duration",), "result_card": "variant_compare"},
    eval_version="v1",
    roles=(),
))

OFFICIAL_SUBTITLE_DRAFT = with_signature(SkillManifest(
    skill_id="topos.subtitle_draft",
    version="1.0.0",
    title="字幕初稿",
    description="从转录证据生成可编辑的句级字幕草稿（typed artifact）。",
    author="Topos",
    source=SkillSource.OFFICIAL,
    license="Topos-proprietary",
    required_evidence_kinds=(SegmentKind.TRANSCRIPT,),
    produces_plan=False,
    produces_artifact_kind=ArtifactKind.SCRIPT,
    target_beats=(),
    strategies=(),
    degrade_to=(
        "缺少转录证据：无法生成字幕草稿。建议先对所选素材运行 "
        "editorial_local 分析（含转录）。"
    ),
    brief_template={
        "required_beats": (),
        "target_duration": {"ideal": 720, "min": 648, "max": 792},
        "platform": "vertical_social",
    },
    tool_policy=_OFFICIAL_TOOL_POLICY,
    risk=SkillRisk.PROPOSAL_ONLY,
    input_schema={
        "type": "object",
        "required": ["asset_ids", "timeline_id", "project_id", "brief_id"],
        "properties": {
            "asset_ids": {"type": "array", "items": {"type": "string"}},
            "brief_id": {"type": "string"},
        },
    },
    output_schema={"type": "object", "properties": {"artifact_id": {"type": "string"}}},
    ui_card={"form_fields": (), "result_card": "subtitle_draft"},
    eval_version="v1",
    roles=(),
))

# 内置官方 Skill 清单（供 SkillRegistry 默认注册）。
OFFICIAL_SKILLS: tuple[SkillManifest, ...] = (
    OFFICIAL_VOICEOVER_POLISH,
    OFFICIAL_PRODUCT_AD_30S,
    OFFICIAL_SUBTITLE_DRAFT,
)


__all__ = [
    "SkillSource",
    "SkillRisk",
    "ToolPolicy",
    "SkillSignature",
    "SkillManifest",
    "compute_content_hash",
    "with_signature",
    "OFFICIAL_VOICEOVER_POLISH",
    "OFFICIAL_PRODUCT_AD_30S",
    "OFFICIAL_SUBTITLE_DRAFT",
    "OFFICIAL_SKILLS",
]
