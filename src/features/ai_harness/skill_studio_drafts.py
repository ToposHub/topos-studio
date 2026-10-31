"""P4.1 开发者 Skill Studio —— 版本化草稿（plan §10.6）。

开发者**不应**通过复制 Python 代码"发布一个工作流"。本模块提供受控的草稿层：

- **来源**：从官方模板（``SkillManifest``）或现有 ``WorkflowDefinition``（dict 形态，
  plan §4.2 schema）创建版本化草稿；
- **声明**：草稿携带输入/输出 schema、风险、工具 allowlist、模型/资源要求、
  默认 policy、UI card、许可证；
- **版本化**：``SkillDraftVersion`` 单调递增；内容变更 → 新版本 + 新 content_hash；
- **隔离**：草稿只在 ``SkillStudioStore`` 内（in-memory）维护，**不**自动进入
  生产 ``SkillRegistry``；只有显式 publish（P4.4 ``SkillRelease``）才进入注册表。
- **未验证**：草稿 ``source=local_dev``，``unverified=True``（plan §10.6 红线）。

红线（plan §10.6 / §1.4 / §5.3）：

- **第三方 / local_dev 不得执行任意 Python / shell / filesystem / UI 自动化**。
  ``ExecutorKind`` 是受控枚举，``is_arbitrary_code`` 永远为 False；
  不存在 ``code_exec`` / ``python_exec`` / ``shell_exec`` / ``fs_write`` /
  ``ui_automation`` 之类。
- 草稿 ``risk`` 不得升级为绕过审批的写权限（local_dev 始终受
  ``WRITE_REQUIRES_APPROVAL`` 之上约束）。
- 不读磁盘 / 不联网（纯 Python；plan §10.1）。

与既有模块的关系：

- 复用 ``SkillManifest`` / ``compute_content_hash`` / ``with_signature``
  （manifest 已是 frozen + 版本化 + content_hash 自校验）。
- 草稿的 ``manifest`` 用 ``dataclasses.replace`` 从模板克隆，仅替换
  ``skill_id``（``draft.`` 前缀）、``source``（强制 local_dev）、``title`` 等。
- ``SkillStudioStore`` 与 ``SkillRegistry`` 完全解耦：前者管"未发布草稿"，
  后者管"已发布可用 Skill"。``publish`` 动作在 P4.4 实现。
"""
from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from src.features.edit.ai_editing.skill_manifest import SkillManifest, SkillRisk, SkillSource, ToolPolicy, with_signature


# ---------------------------------------------------------------------- #
# ExecutorKind —— 受控枚举（红线：禁止任意代码执行）
# ---------------------------------------------------------------------- #


class _ExecutorKindMeta(type):
    """让 ``ExecutorKind`` 类本身可迭代（``for k in ExecutorKind`` 枚举受控成员）。

    Enum 风格 API：``__iter__`` 在元类上，使 ``list(ExecutorKind)`` 可用，
    便于在红线测试中断言"受控集合不含任意代码执行后端"。
    """

    def __iter__(cls) -> Iterable["ExecutorKind"]:
        for v in cls.ALL:  # type: ignore[attr-defined]
            yield cls(v)

    def __contains__(cls, item: object) -> bool:
        if isinstance(item, ExecutorKind):
            return item.value in cls.ALL  # type: ignore[attr-defined]
        if isinstance(item, str):
            return item in cls.ALL  # type: ignore[attr-defined]
        return False


class ExecutorKind(metaclass=_ExecutorKindMeta):
    """Skill 草稿的执行后端（plan §10.6："第三方不得执行任意 Python/shell/fs/UI"）。

    用 ``str`` 常量 + 校验，而非 ``Enum``，是为了让序列化形态稳定且便于跨进程。
    **红线**：受控枚举只允许声明式执行后端；``is_arbitrary_code`` 对所有成员恒为 False。
    """

    WORKFLOW_RUNNER_VALUE: str = "workflow_runner"      # M7 WorkflowRunner（声明式 WorkflowSpec）
    TASKGRAPH_ROLES_VALUE: str = "taskgraph_roles"      # TaskGraph 多角色（声明式 roles）
    EDITORIAL_BUILDER_VALUE: str = "editorial_builder"  # 纯 Python 构造器（auto_caption 等）

    ALL: Tuple[str, ...] = (WORKFLOW_RUNNER_VALUE, TASKGRAPH_ROLES_VALUE, EDITORIAL_BUILDER_VALUE)

    # 受控成员的 is_arbitrary_code 映射（全部 False）
    _ARBITRARY: Dict[str, bool] = {k: False for k in ALL}

    def __init__(self, value: str) -> None:
        if value not in ExecutorKind.ALL:
            raise ValueError(
                f"forbidden/unknown executor kind: {value!r}; "
                f"allowed: {ExecutorKind.ALL} (arbitrary code execution is prohibited)"
            )
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    @property
    def is_arbitrary_code(self) -> bool:
        """红线：所有受控成员恒为 False。"""
        return ExecutorKind._ARBITRARY.get(self._value, True)  # 未知 → True（保守拒绝）

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ExecutorKind):
            return self._value == other._value
        if isinstance(other, str):
            return self._value == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("ExecutorKind", self._value))

    def __repr__(self) -> str:
        return f"ExecutorKind({self._value!r})"

    @classmethod
    def values(cls) -> Tuple[str, ...]:
        """所有受控成员的字符串值（Enum 风格便捷方法）。"""
        return cls.ALL

    @staticmethod
    def kind_for_workflow(workflow_definition: Dict[str, Any]) -> "ExecutorKind":
        """根据 WorkflowDefinition 推断受控执行后端。

        - 有 ``roles`` 字段 → ``taskgraph_roles``
        - 否则默认 ``workflow_runner``
        - 仅含纯构造器节点（如 topos.caption.propose_track）→ ``editorial_builder``

        红线：永不返回任意代码执行后端。
        """
        nodes = workflow_definition.get("nodes", []) or []
        editorial_only = all(
            str(n.get("spec_id", "")).startswith(("topos.caption.", "topos.text."))
            for n in nodes
        ) and bool(nodes)
        if workflow_definition.get("roles"):
            return ExecutorKind.TASKGRAPH_ROLES
        if editorial_only:
            return ExecutorKind.EDITORIAL_BUILDER
        return ExecutorKind.WORKFLOW_RUNNER


# 便捷常量（供外部比较 / 默认值）—— 真正的 ExecutorKind 实例。
# 类内 ``*_VALUE`` 是字符串值（序列化稳定），此处把 ``WORKFLOW_RUNNER`` 等
# 暴露为实例，使 ``ExecutorKind.WORKFLOW_RUNNER.is_arbitrary_code`` 可用
# （Enum 风格 API；红线测试据此断言"受控成员不可执行任意代码"）。
ExecutorKind.WORKFLOW_RUNNER = ExecutorKind(ExecutorKind.WORKFLOW_RUNNER_VALUE)  # type: ignore[attr-defined]
ExecutorKind.TASKGRAPH_ROLES = ExecutorKind(ExecutorKind.TASKGRAPH_ROLES_VALUE)  # type: ignore[attr-defined]
ExecutorKind.EDITORIAL_BUILDER = ExecutorKind(ExecutorKind.EDITORIAL_BUILDER_VALUE)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------- #
# ModelRequirement —— 模型 / 资源要求声明
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelRequirement:
    """Skill 草稿声明的模型 / 资源要求（plan §10.6 "模型/资源要求"）。

    发布前由 dry-run（P4.2）检查所需 capability 是否具备；缺失 → 评测套件
    的 ``model-missing`` 用例应失败。
    """

    capability: str              # asr / ocr / scene_cut / tts / llm / translate / demucs ...
    required: bool = True        # 必需（True）或可选降级（False）
    min_pack_version: str = ""   # 所需 model pack 最低版本（""=不限制）
    estimated_vram_mb: int = 0   # 估计显存占用（0=未知/不适用）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capability": self.capability,
            "required": self.required,
            "min_pack_version": self.min_pack_version,
            "estimated_vram_mb": self.estimated_vram_mb,
        }


#: 一些常见 Skill 类型的默认模型要求（供模板克隆时参考）。
DRAFT_MODEL_REQUIREMENTS: Dict[str, Tuple[ModelRequirement, ...]] = {
    "auto_caption": (
        ModelRequirement(capability="asr", required=True, min_pack_version="1.0"),
        ModelRequirement(capability="demucs", required=False),
    ),
    "subtitle_localization": (
        ModelRequirement(capability="asr", required=True),
        ModelRequirement(capability="translate", required=True),
        ModelRequirement(capability="tts", required=True),
    ),
    "voiceover_polish": (
        ModelRequirement(capability="asr", required=True),
        ModelRequirement(capability="llm", required=True),
    ),
    "product_ad": (
        ModelRequirement(capability="asr", required=True),
        ModelRequirement(capability="llm", required=True),
        ModelRequirement(capability="scene_cut", required=False),
    ),
}


# ---------------------------------------------------------------------- #
# DraftTemplateOrigin —— 草稿来源追溯
# ---------------------------------------------------------------------- #


OriginKindLiteral = Literal["official_template", "workflow_definition", "blank"]


@dataclass(frozen=True)
class DraftTemplateOrigin:
    """草稿的来源（官方模板 / WorkflowDefinition / 空白）。

    用于审计：每个草稿可追溯到其源头（plan §10.6 "从官方模板或现有 WorkflowDefinition
    创建版本化草稿"）。
    """

    kind: OriginKindLiteral
    template_id: str = ""        # kind=official_template 时填（如 "topos.auto_caption"）
    workflow_id: str = ""        # kind=workflow_definition 时填
    workflow_hash: str = ""      # kind=workflow_definition 时填（源 graph 内容 hash）

    @staticmethod
    def official_template(template_id: str) -> "DraftTemplateOrigin":
        return DraftTemplateOrigin(kind="official_template", template_id=template_id)

    @staticmethod
    def workflow_definition(workflow_id: str, workflow_hash: str = "") -> "DraftTemplateOrigin":
        return DraftTemplateOrigin(
            kind="workflow_definition",
            workflow_id=workflow_id,
            workflow_hash=workflow_hash,
        )

    @staticmethod
    def blank() -> "DraftTemplateOrigin":
        return DraftTemplateOrigin(kind="blank")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "template_id": self.template_id,
            "workflow_id": self.workflow_id,
            "workflow_hash": self.workflow_hash,
        }


# ---------------------------------------------------------------------- #
# SkillDraftVersion —— 草稿版本号（< 1.0 = 开发阶段）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class SkillDraftVersion:
    """草稿版本号（semver，``< 1.0.0`` 视为开发阶段 ``is_dev=True``）。

    发布为 Skill（P4.4）时版本号应 ≥ 1.0.0；开发期持续 bump patch/minor。
    """

    major: int
    minor: int
    patch: int

    @staticmethod
    def first() -> "SkillDraftVersion":
        return SkillDraftVersion(0, 1, 0)

    @staticmethod
    def from_string(s: str) -> "SkillDraftVersion":
        parts = s.split(".")
        if len(parts) != 3:
            raise ValueError(f"invalid semver: {s!r}")
        try:
            major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError as exc:
            raise ValueError(f"invalid semver: {s!r}") from exc
        if major < 0 or minor < 0 or patch < 0:
            raise ValueError(f"invalid semver (negative): {s!r}")
        return SkillDraftVersion(major, minor, patch)

    @property
    def is_dev(self) -> bool:
        """< 1.0.0 视为开发阶段（plan §10.6 "本地开发 Skill 醒目标未验证"）。"""
        return self.major < 1

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def bump_patch(self) -> "SkillDraftVersion":
        return SkillDraftVersion(self.major, self.minor, self.patch + 1)

    def bump_minor(self) -> "SkillDraftVersion":
        return SkillDraftVersion(self.major, self.minor + 1, 0)

    def bump_major(self) -> "SkillDraftVersion":
        return SkillDraftVersion(self.major + 1, 0, 0)


# ---------------------------------------------------------------------- #
# SkillDraft —— 版本化草稿（核心）
# ---------------------------------------------------------------------- #


#: 草稿可更新的"已知字段"白名单（避免任意 setattr 突破声明式约束）。
_DRAFT_UPDATABLE_FIELDS: Tuple[str, ...] = (
    "title", "description", "input_schema", "output_schema",
    "ui_card", "tool_policy", "risk", "license",
    "target_beats", "strategies", "degrade_to", "brief_template",
    "required_evidence_kinds",
)


@dataclass(frozen=True)
class SkillDraft:
    """开发者 Skill Studio 中的版本化草稿（plan §10.6）。

    frozen + 版本化 + 来源可追溯。每次 ``update_field`` 返回新版本（不可变）；
    content_hash 由 manifest 自带，内容变 → hash 变。

    红线：``source`` 强制为 ``local_dev``，``unverified=True``；
    ``executor_kind`` 受 ``ExecutorKind`` 枚举约束（禁止任意代码执行）。
    """

    draft_id: str                          # 草稿唯一 id（uuid4 hex）
    author: str                            # 草稿作者（用于 list_drafts 过滤）
    version: SkillDraftVersion
    origin: DraftTemplateOrigin
    manifest: SkillManifest                # frozen manifest（已强制 local_dev + draft. 前缀）
    executor_kind: ExecutorKind
    model_requirements: Tuple[ModelRequirement, ...] = ()
    workflow_template: Optional[Dict[str, Any]] = None  # 来自 WorkflowDefinition 时的源 graph
    default_policy: Dict[str, Any] = field(default_factory=dict)  # network / timeline_write / budget
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def skill_id(self) -> str:
        return self.manifest.skill_id

    @property
    def unverified(self) -> bool:
        """红线：草稿恒为未验证（local_dev）。"""
        return True

    def update_field(
        self, field_name: str, value: Any, *, kind: str = "patch",
        now: float = 0.0,
    ) -> "SkillDraft":
        """返回更新了单字段的新草稿（版本号自动 bump）。

        红线：``field_name`` 必须在白名单内（禁止改 ``skill_id`` / ``source`` /
        ``signature`` 等身份字段）；``source`` 始终保持 ``local_dev``。
        """
        if field_name not in _DRAFT_UPDATABLE_FIELDS:
            raise KeyError(
                f"field {field_name!r} is not updatable; allowed: {_DRAFT_UPDATABLE_FIELDS}"
            )
        if kind not in ("patch", "minor", "major"):
            raise ValueError(f"invalid bump kind: {kind!r}; allowed: patch/minor/major")
        # 版本 bump
        if kind == "patch":
            new_v = self.version.bump_patch()
        elif kind == "minor":
            new_v = self.version.bump_minor()
        else:
            new_v = self.version.bump_major()
        # manifest 字段替换 → 重算签名（content_hash 自动变）
        new_manifest = replace(self.manifest, **{field_name: value})
        new_manifest = with_signature(new_manifest, signed_by=f"local_dev:{self.author}")
        return replace(
            self,
            version=new_v,
            manifest=new_manifest,
            updated_at=now or self.updated_at,
        )

    def with_executor_kind(self, kind: ExecutorKind) -> "SkillDraft":
        """替换执行后端（必须是受控枚举）。"""
        if not isinstance(kind, ExecutorKind):
            raise ValueError("executor_kind must be ExecutorKind instance")
        if kind.is_arbitrary_code:
            raise ValueError("forbidden executor kind (arbitrary code)")
        return replace(self, executor_kind=kind)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "author": self.author,
            "version": str(self.version),
            "is_dev": self.version.is_dev,
            "origin": self.origin.to_dict(),
            "manifest": self.manifest.to_dict(),
            "executor_kind": self.executor_kind.value,
            "model_requirements": [m.to_dict() for m in self.model_requirements],
            "workflow_template": self.workflow_template,
            "default_policy": dict(self.default_policy),
            "unverified": self.unverified,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------- #
# 工厂：从官方模板 / WorkflowDefinition 创建草稿
# ---------------------------------------------------------------------- #


def _gen_draft_id() -> str:
    return f"draft-{uuid.uuid4().hex[:16]}"


def _clone_manifest_as_local_dev(
    template: SkillManifest,
    *,
    title: str,
    description: str,
    author: str,
) -> SkillManifest:
    """克隆官方模板为 local_dev 草稿 manifest。

    红线：``skill_id`` 加 ``draft.`` 前缀（避免覆盖生产 skill）；
    ``source`` 强制为 ``local_dev``；签名重算。
    """
    new_skill_id = f"draft.{template.skill_id}-{uuid.uuid4().hex[:8]}"
    cloned = replace(
        template,
        skill_id=new_skill_id,
        source=SkillSource.LOCAL_DEV,
        title=title or template.title,
        description=description or template.description,
        author=author or template.author,
        signature=None,  # 重算
    )
    return with_signature(cloned, signed_by=f"local_dev:{author}")


def new_draft_from_template(
    *,
    author: str,
    template: SkillManifest,
    title: str = "",
    description: str = "",
    executor_kind: Optional[ExecutorKind] = None,
    model_requirements: Iterable[ModelRequirement] = (),
    now: float = 0.0,
) -> SkillDraft:
    """从官方模板创建版本化草稿（plan §10.6）。

    继承模板的：输入/输出 schema、风险、工具 allowlist、produces_plan、
    brief_template、required_evidence_kinds 等所有声明字段。
    替换：skill_id（``draft.`` 前缀）、source（``local_dev``）、title/description。
    """
    if not author.strip():
        raise ValueError("author is required")
    if not isinstance(template, SkillManifest):
        raise TypeError("template must be SkillManifest")
    manifest = _clone_manifest_as_local_dev(
        template, title=title, description=description, author=author,
    )
    # 模型要求：显式传入优先；否则查默认表；都没有 → 空 tuple
    mr_tuple = tuple(model_requirements)
    if not mr_tuple:
        mr_tuple = DRAFT_MODEL_REQUIREMENTS.get(_template_key(template), ())
    # 执行后端：显式传入优先；否则按模板 roles 推断
    if executor_kind is None:
        executor_kind = _infer_executor_kind_from_template(template)
    return SkillDraft(
        draft_id=_gen_draft_id(),
        author=author,
        version=SkillDraftVersion.first(),
        origin=DraftTemplateOrigin.official_template(template.skill_id),
        manifest=manifest,
        executor_kind=executor_kind,
        model_requirements=mr_tuple,
        default_policy=_default_policy_from_template(template),
        created_at=now,
        updated_at=now,
    )


def new_draft_from_workflow(
    *,
    author: str,
    workflow_definition: Dict[str, Any],
    title: str,
    description: str = "",
    executor_kind: Optional[ExecutorKind] = None,
    model_requirements: Iterable[ModelRequirement] = (),
    license: str = "",
    now: float = 0.0,
) -> SkillDraft:
    """从现有 WorkflowDefinition 创建版本化草稿（plan §10.6 / §4.2 schema）。

    ``workflow_definition`` 是 plan §4.2 ``WorkflowGraph`` 最小 schema 的 dict
    形态。本函数不解析节点 spec（留给 P4.2 dry-run）；只把源 graph 作为
    ``workflow_template`` 保留，并据此推断 ``executor_kind`` 与默认 ``tool_policy``。
    """
    if not author.strip():
        raise ValueError("author is required")
    if not isinstance(workflow_definition, dict):
        raise TypeError("workflow_definition must be dict")
    if not title.strip():
        raise ValueError("title is required for workflow-derived draft")
    # AUD-22411: deep-snapshot the caller's graph once; hash 与存储都基于该
    # 私有快照，调用方事后原地改 workflow_definition 不会让草稿内容与
    # origin.workflow_hash 静默脱钩。
    wf_snapshot = copy.deepcopy(workflow_definition)
    wf_id = str(wf_snapshot.get("workflow_id") or f"wf-anon-{uuid.uuid4().hex[:8]}")
    wf_hash = _hash_workflow(wf_snapshot)
    new_skill_id = f"draft.wf-{wf_id}-{uuid.uuid4().hex[:8]}"
    policy = wf_snapshot.get("policy", {}) or {}
    # tool_policy：从 workflow policy 提取 timeline_write → 影响 max_permission
    timeline_write = str(policy.get("timeline_write", ""))
    max_perm = "proposal" if timeline_write.startswith("approval") else "read"
    tool_policy = ToolPolicy(max_permission=max_perm)
    risk = (
        SkillRisk.WRITE_REQUIRES_APPROVAL
        if timeline_write.startswith("approval")
        else SkillRisk.PROPOSAL_ONLY
    )
    # AUD-22410: produces_plan 只在 policy 真正允许"审批后写时间线"时为 True。
    # 旧表达式 `"timeline_write" in policy` 连 "forbidden" 也判 True（键存在
    # 即真），第二子句恒被短路；与 risk=PROPOSAL_ONLY 自相矛盾。
    produces_plan = timeline_write.startswith("approval")
    manifest = SkillManifest(
        skill_id=new_skill_id,
        version="0.1.0",
        title=title,
        description=description or wf_snapshot.get("name", ""),
        author=author,
        source=SkillSource.LOCAL_DEV,
        license=license or "UNLICENSED-local-dev",
        produces_plan=produces_plan,
        tool_policy=tool_policy,
        risk=risk,
        input_schema=_input_schema_from_workflow(wf_snapshot),
        output_schema={},
        ui_card={"form_fields": (), "result_card": "default"},
        eval_version="",
        roles=tuple(wf_snapshot.get("roles", []) or ()),
    )
    manifest = with_signature(manifest, signed_by=f"local_dev:{author}")
    ek = executor_kind or ExecutorKind.kind_for_workflow(wf_snapshot)
    return SkillDraft(
        draft_id=_gen_draft_id(),
        author=author,
        version=SkillDraftVersion.first(),
        origin=DraftTemplateOrigin.workflow_definition(wf_id, wf_hash),
        manifest=manifest,
        executor_kind=ek,
        model_requirements=tuple(model_requirements),
        workflow_template=wf_snapshot,
        default_policy=copy.deepcopy(policy),
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------- #
# SkillStudioStore —— in-memory 草稿存储（与 SkillRegistry 隔离）
# ---------------------------------------------------------------------- #


class SkillStudioStore:
    """开发者草稿存储（plan §10.6）。

    红线：

    - 与 ``SkillRegistry`` **完全解耦**：草稿保存 / 删除 / 更新都不触碰生产
      注册表；只有 ``publish`` 动作（P4.4 ``SkillRelease``）才把草稿"提升"
      为可用 Skill。
    - 不持久化到磁盘（v1 决策：与 SkillRegistry 一致，磁盘 skill 包留给
      扩展市场批次）。

    线程与容量契约（AUD-22413）：

    - ``save`` / ``delete`` 可来自 Studio UI 线程，``list_drafts`` / ``get``
      可来自 dry-run / 评测线程——内部由 ``threading.RLock`` 保护。
    - 草稿数量有上限（``max_drafts``，默认 500；草稿是纯内存对象但也非
      无限）：超限时按 ``updated_at`` 最旧的草稿淘汰（刚保存的那份除外）。
    """

    #: 草稿容量上限默认值（AUD-22413：纯内存存储不允许无限累积）。
    DEFAULT_MAX_DRAFTS = 500

    def __init__(self, *, max_drafts: int = DEFAULT_MAX_DRAFTS) -> None:
        if max_drafts <= 0:
            raise ValueError("max_drafts must be positive")
        self._max_drafts = int(max_drafts)
        self._lock = threading.RLock()
        self._drafts: Dict[str, SkillDraft] = {}

    def save(self, draft: SkillDraft) -> SkillDraft:
        """保存（或更新）草稿；超出容量时淘汰 updated_at 最旧的草稿。"""
        with self._lock:
            self._drafts[draft.draft_id] = draft
            if len(self._drafts) > self._max_drafts:
                self._evict_oldest_locked(keep=draft.draft_id)
        return draft

    def _evict_oldest_locked(self, *, keep: str) -> None:
        """淘汰 updated_at 最旧（且非 ``keep``）的草稿。调用方已持锁。"""
        overflow = len(self._drafts) - self._max_drafts
        if overflow <= 0:
            return
        candidates = sorted(
            (d for did, d in self._drafts.items() if did != keep),
            key=lambda d: (d.updated_at, d.draft_id),
        )
        for victim in candidates[:overflow]:
            self._drafts.pop(victim.draft_id, None)

    def get(self, draft_id: str) -> Optional[SkillDraft]:
        with self._lock:
            return self._drafts.get(draft_id)

    def list_drafts(
        self, *, author: Optional[str] = None,
    ) -> List[SkillDraft]:
        with self._lock:
            out = list(self._drafts.values())
        if author is not None:
            out = [d for d in out if d.author == author]
        return out

    def delete(self, draft_id: str) -> bool:
        with self._lock:
            if draft_id in self._drafts:
                del self._drafts[draft_id]
                return True
            return False

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._drafts)


# ---------------------------------------------------------------------- #
# 内部辅助
# ---------------------------------------------------------------------- #


def _template_key(template: SkillManifest) -> str:
    """从模板 skill_id 提取短键（查 DRAFT_MODEL_REQUIREMENTS）。"""
    sid = template.skill_id
    for key in DRAFT_MODEL_REQUIREMENTS:
        if key in sid:
            return key
    return ""


def _infer_executor_kind_from_template(template: SkillManifest) -> ExecutorKind:
    """根据模板 roles 推断执行后端。"""
    if template.roles:
        return ExecutorKind.TASKGRAPH_ROLES
    # 官方 editorial（auto_caption / subtitle_localization）
    if "auto_caption" in template.skill_id or "subtitle_localization" in template.skill_id:
        return ExecutorKind.EDITORIAL_BUILDER
    return ExecutorKind.WORKFLOW_RUNNER


def _default_policy_from_template(template: SkillManifest) -> Dict[str, Any]:
    """从模板风险等级推断默认 policy。"""
    if template.risk == SkillRisk.WRITE_REQUIRES_APPROVAL:
        return {"network": "ask", "timeline_write": "approval_required"}
    if template.risk == SkillRisk.PROPOSAL_ONLY:
        return {"network": "ask", "timeline_write": "forbidden"}
    return {"network": "forbidden", "timeline_write": "forbidden"}


def _input_schema_from_workflow(wf: Dict[str, Any]) -> Dict[str, Any]:
    """从 WorkflowDefinition 推断 input_schema（暴露的 input 节点参数）。"""
    props: Dict[str, Any] = {}
    required: List[str] = []
    for node in wf.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        spec_id = str(node.get("spec_id", ""))
        if spec_id.startswith("topos.media.selected_assets") or spec_id.startswith("topos.timeline.current"):
            # 输入节点：暴露其参数为 skill 输入
            params = node.get("params", {}) or {}
            for k, v in params.items():
                props[k] = {"type": _json_type_of(v)}
            node_id = str(node.get("node_id", ""))
            if node_id:
                props[f"_{node_id}"] = {"type": "string"}
    if not props:
        return {"type": "object", "properties": {}}
    return {"type": "object", "properties": props, "required": required}


def _json_type_of(v: Any) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, (list, tuple)):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "string"


def _hash_workflow(wf: Dict[str, Any]) -> str:
    """计算 WorkflowDefinition 内容 hash（用于来源追溯）。"""
    import hashlib
    import json
    raw = json.dumps(wf, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "ExecutorKind",
    "ModelRequirement",
    "DRAFT_MODEL_REQUIREMENTS",
    "DraftTemplateOrigin",
    "SkillDraftVersion",
    "SkillDraft",
    "SkillStudioStore",
    "new_draft_from_template",
    "new_draft_from_workflow",
]
