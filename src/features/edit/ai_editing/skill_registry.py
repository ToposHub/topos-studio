"""TAEP M9 — Skill Registry：安装 / 启用 / 禁用 / 工具过滤（§3.1 / §5.3 / §11.1 M9 step 2）。

``SkillRegistry`` 是 in-memory 的 Skill 安装表（v1 决策：不做磁盘 skill 包加载，那是
扩展市场批次）。它负责：

- **安装 / 禁用 / 升级**（§5.3）：``register`` 注册 manifest（含版本/来源/签名）；
  ``disable``/``enable`` 控制可用性；``upgrade`` 仅允许版本号更高的同名 Skill 替换。
- **来源分级 + 未验证标识**（§5.3 第二条）：``local_dev`` Skill 注册时 ``unverified=True``，
  UI 需醒目标记。
- **工具 allowlist 过滤**（§5.3 第一条 / §3.1 红线："Skill 不拥有裸权限，只声明所需
  工具/权限"）：``tools_for(skill_id)`` 返回该 Skill 可见的 ``ToolDefinition``（在
  ``ToolFacade.list_tools`` 基础上按 manifest ``tool_policy`` 双重过滤）；
  ``validate_tool_call`` 在调用前校验是否符合 policy（不符 → 诊断，不静默放行）。

红线（遵循 .clinerules / §0 / §1.2 不变量 #4）：

- **最小权限且可见**（§1.2 不变量 #4）：无授权/无 capability 时 Skill 明确受限，绝不
  静默升级权限。``register`` 拒绝声明 ``ToolFacade.FORBIDDEN_CAPABILITIES`` 的 Skill。
- **content_hash 完整性自检**（§5.3 第三条）：注册时重算 hash 与 manifest 自带的
  ``signature.content_hash`` 比对，不一致 → 拒绝（manifest 被篡改）。
- **不自动注册到 AnalysisService / ToolFacade**（与 M8 OcrProvider 同策略）：本表是
  独立的查询/过滤层，不改变既有 ToolFacade 行为；接线由控制器/UI 批次完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.features.edit.ai_editing.diagnostics import (
    Diagnostic,
    DiagnosticSeverity,
    TaepDiagnosticCode,
    make_diagnostic,
)
from src.features.edit.ai_editing.skill_manifest import (
    OFFICIAL_SKILLS,
    SkillManifest,
    compute_content_hash,
)


# ---------------------------------------------------------------------- #
# 注册条目（manifest + 启用状态）
# ---------------------------------------------------------------------- #


@dataclass
class _RegistryEntry:
    """注册表条目：manifest + 启用状态 + 安装时间。"""

    manifest: SkillManifest
    enabled: bool = True
    installed_at: str = ""

    @property
    def skill_id(self) -> str:
        return self.manifest.skill_id

    @property
    def version(self) -> str:
        return self.manifest.version


# ---------------------------------------------------------------------- #
# SkillRegistry
# ---------------------------------------------------------------------- #


class SkillRegistry:
    """Skill 安装 / 启用 / 禁用 / 工具过滤表（§3.1 / §5.3）。

    用法::

        registry = SkillRegistry()           # 默认注册官方 Skill
        registry.register(my_skill_manifest)  # 注册第三方/本地 Skill
        registry.disable("some.skill_id")     # 禁用
        tools = registry.tools_for("topos.voiceover_polish", facade_tools)

    红线：注册时校验 manifest 完整性（content_hash）+ 来源 + 工具策略合法性。
    """

    # ToolFacade.FORBIDDEN_CAPABILITIES 的镜像（避免硬依赖导入 ToolFacade 循环）。
    # Skill 的 allowed_tools 不得包含这些（即使包含，validate_tool_call 也会拒）。
    FORBIDDEN_CAPABILITIES = frozenset({
        "exec_python", "execute_code", "read_arbitrary_path",
        "call_shell", "click_ui", "inject_glsl", "download_url",
    })

    def __init__(self, *, register_official: bool = True) -> None:
        self._entries: dict[str, _RegistryEntry] = {}
        if register_official:
            for m in OFFICIAL_SKILLS:
                # 官方 Skill 自带签名，跳过完整性自检（信任内置）
                self._entries[m.skill_id] = _RegistryEntry(manifest=m, enabled=True)

    # ---------------- 查询 ---------------- #

    def get(self, skill_id: str) -> SkillManifest | None:
        """返回已注册的 manifest（None = 未注册）。"""
        e = self._entries.get(skill_id)
        return e.manifest if e else None

    def require(self, skill_id: str) -> SkillManifest:
        """返回已注册 manifest，未注册抛 KeyError。"""
        m = self.get(skill_id)
        if m is None:
            raise KeyError(f"skill not registered: {skill_id}")
        return m

    def list_skills(self, *, include_disabled: bool = False) -> list[SkillManifest]:
        """列出已注册 Skill（默认不含禁用的）。"""
        out: list[SkillManifest] = []
        for e in self._entries.values():
            if not include_disabled and not e.enabled:
                continue
            out.append(e.manifest)
        # 稳定排序：skill_id
        out.sort(key=lambda m: m.skill_id)
        return out

    def is_enabled(self, skill_id: str) -> bool:
        """该 Skill 是否启用（未注册返回 False）。"""
        e = self._entries.get(skill_id)
        return bool(e and e.enabled)

    @property
    def count(self) -> int:
        return len(self._entries)

    # ---------------- 安装 / 禁用 / 升级 ---------------- #

    def register(self, manifest: SkillManifest, *, enabled: bool = True) -> list[Diagnostic]:
        """注册一个 Skill manifest（安装）。返回诊断列表（空 = 成功）。

        校验（§5.3）：
        - content_hash 完整性自检（manifest 自带 signature 时重算比对）。
        - 工具策略合法（allowed_tools 不含 FORBIDDEN_CAPABILITIES）。
        - 同 skill_id 已注册时：仅当新版本号更高才升级（``upgrade`` 语义）。
        - local_dev 来源 → unverified=True（manifest 自带，registry 不改）。
        """
        diags: list[Diagnostic] = []

        # 1. content_hash 完整性自检
        if manifest.signature is not None:
            recomputed = compute_content_hash(manifest)
            if recomputed != manifest.signature.content_hash:
                diags.append(make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
                    detail=(
                        f"content_hash mismatch for {manifest.skill_id}: manifest tampered "
                        f"(expected {manifest.signature.content_hash[:12]}…, got {recomputed[:12]}…)"
                    ),
                ))
                return diags

        # 2. 工具策略合法性
        forbidden_declared = [
            t for t in manifest.tool_policy.allowed_tools
            if t in self.FORBIDDEN_CAPABILITIES
        ]
        if forbidden_declared:
            diags.append(make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.PERMISSION_DENIED,
                reason=(
                    f"skill {manifest.skill_id} declares forbidden capabilities: "
                    f"{forbidden_declared} (no exec/shell/fs/ui)"
                ),
            ))
            return diags

        # 3. 升级检查：同 skill_id 已注册
        existing = self._entries.get(manifest.skill_id)
        if existing is not None:
            if not _version_is_higher(manifest.version, existing.version):
                # AUD-14410：REVISION_CONFLICT 模板需要 {base}/{current}
                # 变量（此前只传 detail=，模板渲染 KeyError 后回退原始
                # 占位符，版本冲突的真实原因整体丢失）。现在传入模板变量，
                # 并把完整说明（skill_id + 双版本号）放进 details 供审计。
                diags.append(make_diagnostic(
                    DiagnosticSeverity.ERROR, TaepDiagnosticCode.REVISION_CONFLICT,
                    base=existing.version,
                    current=manifest.version,
                    details={
                        "skill_id": manifest.skill_id,
                        "registered_version": existing.version,
                        "new_version": manifest.version,
                        "reason": (
                            f"skill {manifest.skill_id} already registered at "
                            f"v{existing.version}; new v{manifest.version} is "
                            "not higher (use upgrade with higher version)"
                        ),
                    },
                ))
                return diags

        # 通过校验 → 注册
        self._entries[manifest.skill_id] = _RegistryEntry(
            manifest=manifest, enabled=enabled,
        )
        return diags

    def disable(self, skill_id: str) -> bool:
        """禁用 Skill（已注册才有效）。返回是否变更。"""
        e = self._entries.get(skill_id)
        if e is None or not e.enabled:
            return False
        e.enabled = False
        return True

    def enable(self, skill_id: str) -> bool:
        """启用 Skill（已注册才有效）。返回是否变更。"""
        e = self._entries.get(skill_id)
        if e is None or e.enabled:
            return False
        e.enabled = True
        return True

    def remove(self, skill_id: str) -> bool:
        """卸载 Skill（官方内置不可卸，返回 False）。"""
        if skill_id in {m.skill_id for m in OFFICIAL_SKILLS}:
            return False
        return self._entries.pop(skill_id, None) is not None

    # ---------------- 工具过滤（§5.3 第一条）---------------- #

    def tools_for(
        self, skill_id: str, all_tools: list[Any],
    ) -> list[Any]:
        """返回该 Skill 可见的工具（在 ``all_tools`` 上按 manifest tool_policy 过滤）。

        ``all_tools`` 通常是 ``ToolFacade.list_tools(max_permission=...)`` 的结果（每个
        元素需有 ``.name`` 和 ``.permission`` 属性，duck-typed 与 ``ToolDefinition``
        兼容）。返回的是 ``all_tools`` 的子集（保持原对象）。

        Skill 未注册或已禁用时返回空列表（最小权限：不可见即不可调）。
        """
        if not self.is_enabled(skill_id):
            return []
        manifest = self.require(skill_id)
        policy = manifest.tool_policy
        out: list[Any] = []
        for t in all_tools:
            name = getattr(t, "name", "")
            perm = getattr(t, "permission", "")
            perm_value = perm.value if hasattr(perm, "value") else str(perm)
            if policy.permits(name, perm_value):
                out.append(t)
        return out

    def validate_tool_call(
        self, skill_id: str, tool_name: str, tool_permission: str,
    ) -> list[Diagnostic]:
        """校验某次工具调用是否符合该 Skill 的 tool_policy。

        不符 → 返回诊断（调用方据此拒绝，不静默放行）。Skill 未注册/禁用 → 拒绝。
        """
        if not self.is_enabled(skill_id):
            return [make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.PERMISSION_DENIED,
                reason=f"skill {skill_id} not enabled or not registered",
            )]
        manifest = self.require(skill_id)
        if not manifest.tool_policy.permits(tool_name, tool_permission):
            return [make_diagnostic(
                DiagnosticSeverity.ERROR, TaepDiagnosticCode.PERMISSION_DENIED,
                reason=(
                    f"skill {skill_id} tool_policy denies {tool_name} "
                    f"(permission={tool_permission}, allowed={list(manifest.tool_policy.allowed_tools)[:3]}…, "
                    f"max={manifest.tool_policy.max_permission})"
                ),
            )]
        return []


# ---------------------------------------------------------------------- #
# 版本比较（简单 semver: major.minor.patch）
# ---------------------------------------------------------------------- #


def _version_is_higher(new: str, old: str) -> bool:
    """new 版本号是否高于 old（semver major.minor.patch）。

    非法版本按字符串比较（保守，不崩）。
    """
    try:
        new_parts = tuple(int(x) for x in new.split("."))
        old_parts = tuple(int(x) for x in old.split("."))
        # 补齐到同长度
        length = max(len(new_parts), len(old_parts))
        new_parts = new_parts + (0,) * (length - len(new_parts))
        old_parts = old_parts + (0,) * (length - len(old_parts))
        return new_parts > old_parts
    except (ValueError, TypeError):
        return new > old


__all__ = ["SkillRegistry"]
