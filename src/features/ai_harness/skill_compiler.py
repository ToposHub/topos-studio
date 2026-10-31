"""SkillCompiler —— 统一编译（plan §0.4 / §10.3 P1.3）。

把 :class:`SkillManifest`（+ P5 的 :class:`WorkflowDefinition`）收敛为同一
**编译产物** :class:`CompiledSkill`，再由 :class:`HarnessRuntime` 的唯一路径
执行。

核心目标（plan §10.3 P1.3 验收）：

> 同一内置"口播精剪"可由 chat、快捷卡、后续 workflow 模板启动，最终都走
> 同一 plan repo / approval / undo。

实现策略（避免一次性大重构，plan §14 step 3 的延续）：

- **不**新建一套与 ``run_workflow``/``run_skill`` 平行的 executor。``SkillCompiler``
  只做"编译"（决定执行目标 + 产出类型化输入），**执行**仍由
  :class:`LegacyHarnessAdapter`（包装既有 ``AssistantService``）完成。
- 编译产物 :class:`CompiledSkill` 携带：
  - ``target``：``workflow``（官方 skill, roles=()，走 M7 WorkflowRunner）或
    ``taskgraph``（多角色 skill，走 TaskGraph）或 ``legacy_unsupported``。
  - ``workflow_input`` / ``skill_input``：按 target 产出的类型化输入（脱敏）。
  - ``manifest``：来源 SkillManifest（不变量、风险、工具策略）。
- :class:`HarnessRuntime` 通过 ``LegacyHarnessAdapter`` 消费 CompiledSkill，
  保证 chat / 快捷卡 / workflow 模板三入口同底层。

红线（plan §10.3 P1.3 / §1.3）：

- 不让 ``run_workflow``、``run_skill`` 各自维护 variant/消息/状态副本
  （它们已通过 LegacyHarnessAdapter 归入同一 RunRecord；本编译器只决定路由）。
- 编译期校验输入 schema（缺必填 → 拒绝，不静默进 provider）。
- 不执行 provider（纯 Python；plan §10.1）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from src.features.ai_harness.invocation_models import Invocation, sanitize_for_storage
from src.features.edit.ai_editing.skill_manifest import SkillManifest


CompiledTarget = Literal["workflow", "taskgraph", "editorial", "legacy_unsupported"]


#: P2 editorial builder skill_ids（plan §10.4 P2.2/P2.3）。
#: 这些 skill 不走 M7 WorkflowRunner（无对应 WorkflowId），而是走
#: ``editorial_skills.build_*`` 构造器产 EditPlan 提议。
EDITORIAL_BUILDER_SKILL_IDS: frozenset[str] = frozenset({
    "topos.auto_caption",
    "topos.subtitle_localization",
    # plan §9.5 合成渲染后导入：真实渲染由 host provider 执行（headless
    # CompositeRenderOutput），产物先入生成素材箱——与 editorial "只产提议"
    # 的边界一致。
    "topos.composite_render_import",
})


# ---------------------------------------------------------------------- #
# SkillDefinition（plan §0.4：统一能力定义）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class SkillDefinition:
    """统一的、可版本化的能力定义（plan §0.4）。

    P1.3 阶段它是 :class:`SkillManifest` 的薄包装（携带 manifest + 编译提示）。
    P5 的 :class:`WorkflowDefinition` 在编译时也会产出本对象，使 chat / 快捷卡 /
    workflow 模板三入口共用同一编译产物。

    不重复 manifest 的字段（避免双份数据）；只新增编译相关的元信息。
    """

    manifest: SkillManifest
    #: 执行目标提示（编译器据此路由；空表示由编译器按 roles 推断）。
    execution_hint: str = ""
    #: 该 skill 是否为官方内置（影响是否允许 legacy workflow backend）。
    is_official: bool = False

    @property
    def skill_id(self) -> str:
        return self.manifest.skill_id

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def roles(self) -> tuple[str, ...]:
        return self.manifest.roles


# ---------------------------------------------------------------------- #
# 编译产物
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class CompiledSkill:
    """编译产物：决定执行目标 + 类型化输入（脱敏）。

    由 :class:`HarnessRuntime` 通过 :class:`LegacyHarnessAdapter` 消费——
    **不**新建平行 executor。
    """

    definition: SkillDefinition
    target: CompiledTarget
    #: workflow 路径的类型化输入（target=workflow 时非 None）。
    workflow_input: Any = None
    #: taskgraph 路径的类型化输入（target=taskgraph 时非 None）。
    skill_input: Any = None
    #: 编译期诊断（缺必填 / schema 不符 / 无法路由）。
    diagnostics: tuple[dict[str, Any], ...] = ()
    #: 是否可执行（无 error 级诊断 + target != legacy_unsupported）。
    runnable: bool = True

    @property
    def skill_id(self) -> str:
        return self.definition.skill_id

    @property
    def has_errors(self) -> bool:
        return any(str(d.get("severity", "")).lower() == "error" for d in self.diagnostics)


# ---------------------------------------------------------------------- #
# SkillCompiler
# ---------------------------------------------------------------------- #


class SkillCompiler:
    """把 :class:`SkillDefinition` + :class:`Invocation` 编译成 :class:`CompiledSkill`。

    用法（宿主装配侧）::

        compiler = SkillCompiler()
        compiled = compiler.compile(definition, invocation)
        if compiled.runnable:
            run_id = adapter.invoke_with_compiled(compiled, invocation)
    """

    def __init__(self) -> None:
        # 内联复用 legacy_adapters 的映射函数（避免循环依赖，按需 import）。
        pass

    # ---------------- 主入口 ---------------- #

    def compile(
        self,
        definition: SkillDefinition,
        invocation: Invocation,
    ) -> CompiledSkill:
        manifest = definition.manifest
        diagnostics: list[dict[str, Any]] = []

        # 1. 决定执行目标
        target = self._resolve_target(definition)

        # 2. 输入 schema 校验（必填字段）
        schema_errors = self._validate_inputs(manifest, invocation)
        diagnostics.extend(schema_errors)

        # 3. 产出类型化输入（按 target）
        workflow_input = None
        skill_input = None
        if target == "workflow":
            try:
                from src.features.ai_harness.legacy_adapters import (
                    invocation_to_workflow_input,
                )
                workflow_input = invocation_to_workflow_input(invocation)
            except ValueError as exc:
                diagnostics.append({
                    "severity": "error", "code": "WORKFLOW_MAPPING_FAILED",
                    "message": str(exc),
                })
                target = "legacy_unsupported"
        elif target == "taskgraph":
            try:
                from src.features.ai_harness.legacy_adapters import (
                    invocation_to_skill_input,
                )
                skill_input = invocation_to_skill_input(invocation)
            except Exception as exc:  # noqa: BLE001
                diagnostics.append({
                    "severity": "error", "code": "SKILL_INPUT_FAILED",
                    "message": str(exc),
                })

        runnable = (
            target != "legacy_unsupported"
            and not any(str(d.get("severity", "")).lower() == "error" for d in diagnostics)
        )
        return CompiledSkill(
            definition=definition,
            target=target,
            workflow_input=workflow_input,
            skill_input=skill_input,
            diagnostics=tuple(sanitize_for_storage(d) for d in diagnostics),
            runnable=runnable,
        )

    # ---------------- 目标解析 ---------------- #

    def _resolve_target(self, definition: SkillDefinition) -> CompiledTarget:
        # 显式提示优先
        if definition.execution_hint == "workflow":
            return "workflow"
        if definition.execution_hint == "taskgraph":
            return "taskgraph"
        if definition.execution_hint == "editorial":
            return "editorial"
        # P2 editorial builder skill（无 M7 WorkflowId，走 build_* 构造器）
        if definition.skill_id in EDITORIAL_BUILDER_SKILL_IDS:
            return "editorial"
        # 按 roles 推断（与 SkillRuntime 现有路由一致）：
        # roles 非空 → taskgraph；roles 空（官方）→ workflow。
        if definition.roles:
            return "taskgraph"
        return "workflow"

    # ---------------- 输入校验 ---------------- #

    def _validate_inputs(
        self, manifest: SkillManifest, invocation: Invocation,
    ) -> list[dict[str, Any]]:
        """校验 invocation.inputs 是否满足 manifest.input_schema 的必填字段。

        asset_ids 来自 scope（不是 invocation.inputs）；timeline_id/project_id 同。
        brief_id 是 workflow/skill 必填的；这里检查它非空。
        """
        diags: list[dict[str, Any]] = []
        schema = manifest.input_schema or {}
        if not isinstance(schema, dict):
            return diags
        required: list[str] = list(schema.get("required", []))
        inputs = invocation.inputs or {}
        scope = invocation.scope

        # 把 scope 字段也算作"已提供"（asset_ids/timeline_id/project_id 来自 scope）
        effective: dict[str, Any] = {
            "asset_ids": scope.selected_asset_ids or (),
            "timeline_id": scope.timeline_id,
            "project_id": scope.project_id,
            **inputs,
        }

        for field_name in required:
            val = effective.get(field_name)
            if val is None or val == "" or val == ():
                diags.append({
                    "severity": "error",
                    "code": "MISSING_REQUIRED_INPUT",
                    "field": field_name,
                    "message": f"缺少必填输入：{field_name}",
                })
        return diags


# ---------------------------------------------------------------------- #
# 便捷：从 SkillRegistry 构造 SkillDefinition
# ---------------------------------------------------------------------- #


def definition_from_manifest(
    manifest: SkillManifest, *, is_official: bool = False,
) -> SkillDefinition:
    """从 manifest 构造 SkillDefinition（P1.3 便捷入口）。"""
    return SkillDefinition(
        manifest=manifest,
        execution_hint="",  # 让编译器按 roles 推断
        is_official=is_official,
    )


def official_definitions() -> tuple[SkillDefinition, ...]:
    """三个官方 skill 的 SkillDefinition（用于注册 / 测试）。"""
    from src.features.edit.ai_editing.skill_manifest import OFFICIAL_SKILLS
    return tuple(
        definition_from_manifest(m, is_official=True) for m in OFFICIAL_SKILLS
    )


__all__ = [
    "CompiledSkill",
    "CompiledTarget",
    "SkillCompiler",
    "SkillDefinition",
    "definition_from_manifest",
    "official_definitions",
]
