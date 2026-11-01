"""WIRE-01/02 · 片段版本菜单的宿主接线（controller 单一请求出口）。

计划（docs/plans/2026-11-01-color-clip-versions/IMPLEMENTATION_PLAN.md
§5 步骤 C/D）的接线实现：

- **读取**：``strip.version_state_requested`` → 从权威工程经
  ``color_version_domains`` 解析 → 组装 ``ClipVersionCollection`` →
  ``strip.set_clip_version_collection()`` 回填。读取核验工程/timeline/
  clip/asset 身份，过期目标直接忽略（不把旧读结果刷新为新状态）。
- **写入**：``strip.version_operation_requested`` → 校验（工程身份、
  revision、目标存在、版本名、删除保护策略）→ 必要时 flush 当前调色
  手势（flush 绑定栈必须与请求目标核对）→ 经**真实媒体池项目命令栈**
  提交 → 发布新工程（dirty/renderer/媒体池/片段栏）→ 必要时（WIRE-02）
  重新 hydrate 当前 Color 会话 → 成功回读权威状态组装
  ``ClipVersionResult`` → ``strip.finish_clip_version_operation()``。
  失败/拒绝零提交、错误原文回填，对话框保留输入可重试。

**操作语义（§4.5 后端要求）**：
- CREATE：flush 后保存 live 栈；空域在同一事务初始化默认版本（空域
  栈不存在时先按域盖章建栈——LOCAL 携带当前 live 内容、REMOTE 携带
  当前 live 内容，两域策略一致：初始化默认版本 = 镜头当前所见调色）。
- LOAD：加载指定快照并**切到该域**（非当前域时先经时间线命令切换引用，
  再激活）；discard_unsaved=True 表示用户已确认用快照覆盖未保存修改
  （激活=内容互换天然覆盖）。不自动锁共享栈（与显式 Use Shared Grade
  的自动锁区分：版本加载是版本管理，不改共享状态）。
- RENAME/DUPLICATE/DELETE：仅改版本表，不触发重解码/预览刷新；
  DELETE 在宿主层执行保护策略（default、域 active、最后一个版本
  禁删——旧 M07 工具的底层命令保持原语义）。
- DELETE_ALL：无 default 键的旧表先迁移（promote）再执行原子
  DeleteAllVersionsCommand；成功后内容变更（加载 default）。
- COPY_TO_SCOPE：复制**指定源快照**到另一域新名；目标域为空时先按
  空域初始化策略建栈+默认版本，同一宏事务内完成 Duplicate；不切换
  实际使用域、不动 live 栈。

**发布顺序（§4.4）**：先发布新工程/片段绑定，再回填 UI 结果。内容
变更操作（LOAD/DELETE_ALL）后：renderer 栈更新、当前会话 hydrate
（``_color_stack_hydration_in_progress`` 防递归）、依赖该共享栈的片段
缩略图失效（CACHE-01：经 ``invalidate_graded_preview_stacks`` +
``refresh_color_timeline_clip_strip`` 重建绑定）。

宿主（MainWindow）duck-typed 依赖全部经 ``getattr`` 解析，缺失时
明确降级（读取 connected=False / 写入拒绝），便于无头测试。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from src.features.color.ui.clip_version_ui import (
    ClipVersionCollection,
    ClipVersionRequest,
    ClipVersionResult,
    ClipVersionSummary,
    ClipVersionTarget,
    VersionOperation,
    VersionScope,
)
from src.features.edit.core import color_version_domains as domains
from src.features.edit.core.color_versions import (
    ActivateColorVersionCommand,
    CopyColorVersionCommand,
    DeleteAllVersionsCommand,
    DeleteColorVersionCommand,
    DuplicateColorVersionCommand,
    InitializeDefaultVersionCommand,
    RenameColorVersionCommand,
    SaveColorVersionCommand,
    create_version_with_default,
    promote_default_version,
    validate_version_name,
    version_plugin_diagnostics,
    version_restorable,
    versions_state,
)

logger = logging.getLogger(__name__)

#: 空域仍可执行的操作（宿主会按策略建栈）
_EMPTY_DOMAIN_CAPABILITIES = frozenset({
    VersionOperation.CREATE, VersionOperation.COPY_TO_SCOPE})


def _scope_to_domain(scope: VersionScope) -> domains.VersionDomain:
    return (domains.VersionDomain.LOCAL
            if scope == VersionScope.LOCAL
            else domains.VersionDomain.REMOTE)


def _domain_to_scope(domain: domains.VersionDomain) -> VersionScope:
    return (VersionScope.LOCAL
            if domain == domains.VersionDomain.LOCAL
            else VersionScope.REMOTE)


class ClipVersionRequestHost:
    """片段版本请求的权威宿主；连接 strip 信号并完成读取/写入/回填。"""

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    # ── 接线 ─────────────────────────────────────────────────────────

    def connect_strip(self, strip: Any) -> None:
        strip.version_state_requested.connect(self.read_versions)
        strip.version_operation_requested.connect(self.execute_operation)

    # ── 宿主环境解析（全部 duck-typed，缺失降级） ─────────────────────

    def _project(self) -> Any | None:
        return getattr(self._owner, "_current_edit_project", None)

    def _strip(self) -> Any | None:
        return getattr(self._owner, "color_timeline_clip_strip", None)

    def _project_stack(self) -> Any | None:
        panel = getattr(self._owner, "media_pool_panel", None)
        getter = getattr(panel, "get_command_stack", None)
        return getter() if callable(getter) else None

    def _timeline(self) -> Any | None:
        edit_page = getattr(self._owner, "edit_page", None)
        return getattr(edit_page, "_current_timeline", None)

    def _grading_panel_stack_id(self) -> str | None:
        panel = getattr(self._owner, "grading_panel", None)
        return getattr(panel, "current_stack_id", None) if panel else None

    def _active_color_session(self) -> Any | None:
        manager = getattr(self._owner, "_color_session_manager", None)
        getter = getattr(manager, "get_current_session", None)
        try:
            return getter() if callable(getter) else None
        except Exception:  # noqa: BLE001 — 无会话时静默
            return None

    # ── 读取（state_requested → set_clip_version_collection） ────────

    def read_versions(self, target: ClipVersionTarget,
                      scope: VersionScope) -> None:
        project = self._project()
        if project is None:
            return
        # 过期读取：工程身份不符直接忽略（不回填，不误报未接线）
        if str(getattr(project, "project_id", "")) != target.project_id:
            return
        collection = self.build_collection(target, scope, project)
        strip = self._strip()
        accept = getattr(strip, "set_clip_version_collection", None)
        if callable(accept):
            accept(collection)

    def build_collection(self, target: ClipVersionTarget,
                         scope: VersionScope,
                         project: Any | None = None) -> ClipVersionCollection:
        """从权威工程组装只读集合（WIRE-01 成功回读同一函数）。"""
        project = project if project is not None else self._project()
        domain = _scope_to_domain(scope)
        binding = domains.resolve_clip_binding(
            project, target.timeline_id, target.clip_id)
        if binding is None:
            return ClipVersionCollection(
                target, scope, connected=False,
                unavailable_reason="目标片段不存在（可能已被删除），请刷新。",
            )
        if binding.asset_id != target.asset_id:
            return ClipVersionCollection(
                target, scope, connected=False,
                unavailable_reason="片段素材引用已变化，请刷新版本列表。",
            )
        snapshot = domains.domain_snapshot(
            project, target.timeline_id, target.clip_id,
            target.asset_id, domain)
        state = snapshot["versions_state"]
        default_name = state["default_version"]
        versions = tuple(
            ClipVersionSummary(
                version_id=name, name=name,
                is_default=(name == default_name),
                diagnostic=self._version_diagnostic(name, state, snapshot))
            for name in state["versions"])
        capabilities: frozenset[VersionOperation] = frozenset()
        if snapshot["stack_id"]:
            capabilities = frozenset({
                VersionOperation.CREATE, VersionOperation.LOAD,
                VersionOperation.RENAME, VersionOperation.DUPLICATE,
                VersionOperation.DELETE, VersionOperation.COPY_TO_SCOPE})
            if len(versions) > 1:
                # 无 default 键的旧表由宿主在同一请求内先迁移再执行
                # （计划 §4.5：宿主先迁移或移除 capability——取迁移路径）
                capabilities = capabilities | {VersionOperation.DELETE_ALL}
        else:
            # 空域：仍可创建（宿主按策略建栈）与作为跨域复制目标
            capabilities = _EMPTY_DOMAIN_CAPABILITIES
        # target 恒为片段当前绑定（strip 按条目身份验收集合；域解析栈
        # 是宿主内部知识）。LOAD/DELETE_ALL 后绑定若变化，先刷新片段栏
        # 再回填，集合 target 自然携带新绑定。
        return ClipVersionCollection(
            target=_rebind_target(target, binding.stack_id),
            scope=scope,
            versions=versions,
            active_version_id=state["active"],
            revision=int(getattr(project, "revision", 0)),
            loaded=True,
            connected=True,
            live_modified=(bool(state["versions"])
                           and not snapshot["live_matches_active"]),
            in_use=snapshot["domain_in_use"],
            capabilities=capabilities,
            unavailable_reason=snapshot["shared_binding_conflict"],
        )

    @staticmethod
    def _version_diagnostic(name: str, state: dict, snapshot: dict) -> str:
        """DIAG-01 · 统一消费 plugin_diagnostics：缺失/未核对明确区分。"""
        payload = (state["versions"].get(name) or {})
        diagnostics = version_plugin_diagnostics(payload)
        if diagnostics["unverified"]:
            return "插件清单未核对（注册表不可用）"
        missing = diagnostics["missing"]
        if missing:
            return f"缺失插件：{', '.join(missing[:3])}"
        return ""

    # ── 写入（operation_requested → 校验 → flush → 提交 → 回填） ─────

    def execute_operation(self, request: ClipVersionRequest) -> None:
        """写入请求的单一出口：校验 → flush → 命令栈提交 → 发布 → 回填。"""
        target: ClipVersionTarget = request.target
        fail = lambda message: self._finish_with_error(request, message)  # noqa: E731
        project = self._project()
        project_stack = self._project_stack()
        if project is None or project_stack is None:
            fail("版本服务未连接（无工程或命令栈不可用）。")
            return

        def stack_exec(command, current_project):
            return project_stack.execute(command, current_project)
        if str(getattr(project, "project_id", "")) != target.project_id:
            fail("工程已切换，请求已过期。请重新打开版本菜单。")
            return
        if int(getattr(project, "revision", 0)) != request.expected_revision:
            fail("工程已被其他操作修改（修订号变化）。请刷新版本列表后重试。")
            return
        binding = domains.resolve_clip_binding(
            project, target.timeline_id, target.clip_id)
        if binding is None:
            fail("目标片段不存在（可能已被删除）。")
            return
        if binding.asset_id != target.asset_id:
            fail("片段素材引用已变化，请刷新。")
            return
        domain = _scope_to_domain(request.scope)
        domain_stack_id = domains.resolve_domain_stack_id(
            project, target.timeline_id, target.clip_id,
            target.asset_id, domain)
        state = (versions_state(project, domain_stack_id)
                 if domain_stack_id else None)

        try:
            handler = self._dispatch(request, project, binding,
                                     domain_stack_id, state)
        except ValueError as exc:
            fail(str(exc))
            return
        try:
            new_project = handler(stack_exec)
        except ValueError as exc:
            fail(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — 命令失败统一明示
            logger.exception("Clip version operation failed")
            fail(f"版本操作失败：{exc}")
            return
        if new_project is None:
            return  # handler 已自行回填（不应发生；防御）
        # 发布新工程（先发布，再回填——§4.4）。命令已提交是事实；发布/
        # 回填阶段的异常不允许把 UI 留在 pending 到超时——兜底回填错误，
        # 用户可刷新核对（§4.4：超时不代表后端撤销事务）。
        try:
            self._publish_project(new_project)
            # 内容变更类操作：renderer / hydrate / 缩略图失效
            # （WIRE-02 + CACHE-01）
            content_changed = request.operation in (
                VersionOperation.LOAD, VersionOperation.DELETE_ALL)
            self._after_content_change(request, new_project, content_changed)
        except Exception:  # noqa: BLE001 — 提交后阶段失败不吞成功事实
            logger.exception(
                "Clip version post-commit publish failed (request %s)",
                request.request_id)
        try:
            self._finish_with_success(request, new_project)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Clip version success backfill failed (request %s)",
                request.request_id)
            fail("版本已提交，但回读状态失败；请刷新版本列表核对。")

    def _dispatch(self, request: ClipVersionRequest, project: Any,
                  binding: domains.ClipStackBinding,
                  domain_stack_id: str | None,
                  state: dict | None) -> Callable[[Callable], Any]:
        """按操作构造执行闭包；校验失败抛 ValueError（零提交）。"""
        operation: VersionOperation = request.operation
        if operation == VersionOperation.CREATE:
            return self._plan_create(request, project, binding,
                                     domain_stack_id, state)
        if operation == VersionOperation.RENAME:
            return self._plan_rename(request, project, domain_stack_id, state)
        if operation == VersionOperation.DUPLICATE:
            return self._plan_duplicate(request, project, domain_stack_id,
                                        state)
        if operation == VersionOperation.DELETE:
            return self._plan_delete(request, project, domain_stack_id, state)
        if operation == VersionOperation.DELETE_ALL:
            return self._plan_delete_all(request, project, domain_stack_id,
                                         state)
        if operation == VersionOperation.LOAD:
            return self._plan_load(request, project, binding,
                                   domain_stack_id, state)
        if operation == VersionOperation.COPY_TO_SCOPE:
            return self._plan_copy_to_scope(request, project, binding,
                                            domain_stack_id, state)
        raise ValueError(f"未知版本操作：{operation}")

    # ── 各操作计划（返回 executor(stack) -> new_project） ─────────────

    def _plan_create(self, request: ClipVersionRequest, project: Any,
                     binding: domains.ClipStackBinding,
                     domain_stack_id: str | None, state: dict | None):
        name = validate_version_name(request.name)
        target_scope_domain = _scope_to_domain(request.scope)

        def execute(stack_exec):
            stack_id = domain_stack_id
            if stack_id is None:
                # 空域：先 flush 内容源（镜头当前 live 调色），再按域盖章
                # 建栈（内容 = flush 后的 live），保证快照含最后参数。
                self._flush_edited_stack(binding.stack_id)
                current = self._project()
                stack_id = self._ensure_domain_stack(
                    current, binding, target_scope_domain,
                    content_source_binding=binding)
                current = self._project()
            else:
                self._flush_edited_stack(stack_id)
                current = self._project()
            table = versions_state(current, stack_id)
            if table["versions"]:
                command = SaveColorVersionCommand.create(
                    current, stack_id, name)
            else:
                command = create_version_with_default(current, stack_id, name)
            return stack_exec(command, current)

        return execute

    def _plan_rename(self, request: ClipVersionRequest, project: Any,
                     domain_stack_id: str | None, state: dict | None):
        if not domain_stack_id:
            raise ValueError("该域没有版本，无法重命名。")
        version = self._require_version(request, state)
        command = RenameColorVersionCommand.create(
            project, domain_stack_id, version, request.name)

        def execute(stack_exec):
            return stack_exec(command, self._project())

        return execute

    def _plan_duplicate(self, request: ClipVersionRequest, project: Any,
                        domain_stack_id: str | None, state: dict | None):
        if not domain_stack_id:
            raise ValueError("该域没有版本，无法复制。")
        version = self._require_version(request, state)
        command = DuplicateColorVersionCommand.create(
            project, domain_stack_id, version, request.name)

        def execute(stack_exec):
            return stack_exec(command, self._project())

        return execute

    def _plan_delete(self, request: ClipVersionRequest, project: Any,
                     domain_stack_id: str | None, state: dict | None):
        if not domain_stack_id:
            raise ValueError("该域没有版本，无法删除。")
        version = self._require_version(request, state)
        # 宿主层删除保护（§4.5 DELETE）：default / 域 active / 最后一个
        if version == state["default_version"]:
            raise ValueError("默认版本受保护，不能删除。")
        if version == state["active"]:
            raise ValueError("当前使用中的版本不能删除；请先切换到其他版本。")
        if len(state["versions"]) <= 1:
            raise ValueError("最后一个版本不能删除（域至少保留一个版本）。")
        command = DeleteColorVersionCommand.create(
            project, domain_stack_id, version)

        def execute(stack_exec):
            return stack_exec(command, self._project())

        return execute

    def _plan_delete_all(self, request: ClipVersionRequest, project: Any,
                         domain_stack_id: str | None, state: dict | None):
        if not domain_stack_id:
            raise ValueError("该域没有版本。")

        def execute(stack_exec):
            from src.features.edit.core.commands import ProjectMacroCommand

            current = self._project()
            if not versions_state(current, domain_stack_id)[
                    "default_version"]:
                # 旧数据迁移：提升 active/最早版本为默认。审查 S2：迁移
                # 与删除合并为单宏事务——迁移单独提交后再失败会让命令栈
                # 与宿主工程脱钩（迁移的 undo stale 校验将永久失败）。
                # DeleteAll 从迁移预览工程上构建（before 快照与宏 apply
                # 顺序一致）。
                preview, migrate_cmd = promote_default_version(
                    current, domain_stack_id)
                if migrate_cmd is None:
                    return stack_exec(
                        DeleteAllVersionsCommand.create(
                            preview, domain_stack_id), current)
                macro = ProjectMacroCommand.create(
                    "Migrate default + delete all other color versions",
                    [migrate_cmd, DeleteAllVersionsCommand.create(
                        preview, domain_stack_id)])
                return stack_exec(macro, current)
            command = DeleteAllVersionsCommand.create(
                current, domain_stack_id)
            return stack_exec(command, current)

        return execute

    def _plan_load(self, request: ClipVersionRequest, project: Any,
                   binding: domains.ClipStackBinding,
                   domain_stack_id: str | None, state: dict | None):
        if not domain_stack_id:
            raise ValueError("该域没有版本，无法加载。")
        version = self._require_version(request, state)
        scope_domain = _scope_to_domain(request.scope)
        in_use = binding.domain == scope_domain

        def execute(stack_exec):
            # 先 flush 正在编辑的域栈：让未落库手势显式进入 live_modified
            # 判定（不静默丢弃，也不无确认覆盖）。
            self._flush_edited_stack(domain_stack_id)
            current = self._project()
            if not request.discard_unsaved:
                state_now = versions_state(current, domain_stack_id)
                if state_now["versions"] and not (
                        domains.live_matches_active_version(
                            current, domain_stack_id)):
                    raise ValueError(
                        "当前调色有未保存修改；请确认覆盖后重试。")
            if not in_use:
                # 换域 = 切换片段引用（既有时间线命令），再激活版本。
                # 审查 S3：切换引用前先预校验载荷可重建（激活路径同款
                # 重建试跑），畸形载荷在零提交阶段拒绝。
                if not version_restorable(current, domain_stack_id, version):
                    raise ValueError(
                        "版本快照无法重建（载荷损坏），已拒绝加载。")
                # 切走前 flush 当前面板手势（真实 flush 的"切栈前提交"）。
                flush = getattr(self._owner,
                                "_flush_active_grading_stack_before_switch",
                                None)
                if callable(flush):
                    flush(domain_stack_id)
                current = self._project()
                ref = domains.find_clip_ref(
                    current, request.target.timeline_id,
                    request.target.clip_id)
                if ref is None:
                    raise ValueError("目标片段不存在。")
                track_id, _clip = ref
                switch = getattr(
                    self._owner, "_apply_clip_color_stack_change", None)
                if not callable(switch):
                    raise ValueError("时间线命令入口不可用，无法切换版本域。")
                if not switch(track_id, request.target.clip_id,
                              domain_stack_id,
                              shared=(scope_domain
                                      == domains.VersionDomain.REMOTE)):
                    raise ValueError("切换版本域失败。")
                current = self._project()
                try:
                    command = ActivateColorVersionCommand.create(
                        domain_stack_id, version)
                    return stack_exec(command, current)
                except Exception:
                    # 审查 S3：引用已切但激活失败——切回原栈，不留
                    # "已换域未加载"的半提交，再原样上抛。
                    logger.exception(
                        "Cross-scope LOAD failed after switching %s to %s; "
                        "rolling back reference", request.target.clip_id,
                        domain_stack_id)
                    switch(track_id, request.target.clip_id,
                           binding.stack_id,
                           shared=(binding.domain
                                   == domains.VersionDomain.REMOTE))
                    raise
            command = ActivateColorVersionCommand.create(
                domain_stack_id, version)
            return stack_exec(command, current)

        return execute

    def _plan_copy_to_scope(self, request: ClipVersionRequest, project: Any,
                            binding: domains.ClipStackBinding,
                            domain_stack_id: str | None, state: dict | None):
        if not domain_stack_id:
            raise ValueError("该域没有版本，无法跨域复制。")
        version = self._require_version(request, state)
        if request.destination_scope is None \
                or request.destination_scope == request.scope:
            raise ValueError("跨域复制必须指定另一个版本域。")
        dest_domain = _scope_to_domain(request.destination_scope)
        name = validate_version_name(request.name)
        dest_stack = domains.resolve_domain_stack_id(
            project, request.target.timeline_id, request.target.clip_id,
            request.target.asset_id, dest_domain)

        def execute(stack_exec):
            from src.features.edit.core.commands import ProjectMacroCommand

            current = self._project()
            target_stack = dest_stack
            commands = []
            if target_stack is None:
                # 空目标域：先 flush 内容源，再建栈+初始化默认+复制（一致
                # 的空域初始化策略）
                self._flush_edited_stack(binding.stack_id)
                current = self._project()
                target_stack = self._ensure_domain_stack(
                    current, binding, dest_domain,
                    content_source_binding=binding)
                current = self._project()
            target_state = versions_state(current, target_stack)
            if not target_state["versions"]:
                # 空目标域：同事务初始化默认版本 + 跨栈复制（§4.3/§4.5）
                init = InitializeDefaultVersionCommand.create(
                    current, target_stack)
                intermediate = init.apply(current)
                copy_cmd = CopyColorVersionCommand.create(
                    intermediate, domain_stack_id, version,
                    target_stack, name)
                commands.extend([init, copy_cmd])
            else:
                commands.append(CopyColorVersionCommand.create(
                    current, domain_stack_id, version, target_stack, name))
            macro = ProjectMacroCommand.create(
                f"Copy Color Version「{version}」→「{name}」", commands)
            new_project = stack_exec(macro, current)
            # 目标域集合单独回填（同一原子提交产生的权威状态）
            self._backfill_destination(request, new_project)
            return new_project

        return execute

    # ── 校验/辅助 ────────────────────────────────────────────────────

    @staticmethod
    def _require_version(request: ClipVersionRequest,
                        state: dict | None) -> str:
        if state is None or not state["versions"]:
            raise ValueError("该域没有版本。")
        if request.version_id not in state["versions"]:
            raise ValueError("目标版本不存在（可能已被删除），请刷新。")
        return request.version_id

    def _flush_edited_stack(self, stack_id: str | None) -> None:
        """提交正在编辑 ``stack_id`` 的未落库手势（§5C/§7.3 手势 flush）。

        仅当面板正在编辑的就是该栈才触发 ``trigger_save``（真实
        ``_flush_active_grading_stack_before_switch`` 的语义是"切走前
        flush"，target==active 时会跳过，不能用于保存当前栈）；面板
        回调绑定栈≠活动栈时跳过（防误写上一镜头）。水合进行中不写。
        """
        if not stack_id:
            return
        owner = self._owner
        if getattr(owner, "_color_stack_hydration_in_progress", False):
            return
        panel = getattr(owner, "grading_panel", None)
        if panel is None:
            return
        if getattr(panel, "current_stack_id", None) != stack_id:
            return
        bound = getattr(owner, "_grading_save_stack_id", None)
        if bound is not None and bound != stack_id:
            return
        trigger = getattr(panel, "trigger_save", None)
        if callable(trigger):
            trigger()

    def _ensure_domain_stack(self, project: Any, binding: domains.ClipStackBinding,
                             domain: domains.VersionDomain,
                             content_source_binding: domains.ClipStackBinding
                             ) -> str:
        """空域建栈（WIRE-01 策略）：按域盖章；内容 = 镜头当前 live 调色
        （两域一致——初始化默认版本即"镜头当前所见调色"）。返回新栈 id。"""
        from src.app.controllers.edit_controller import (
            _CreateStampedColorStackCommand,
        )
        source_stack = None
        current = project if project is not None else self._project()
        if content_source_binding is not None and content_source_binding.stack_id:
            source_stack = current.color_stacks.get(
                content_source_binding.stack_id)
        stamp_fields: dict[str, Any] = {}
        if source_stack is not None:
            # 审查 S1：与 Make Local Grade Copy 共用同一内容字段清单
            # （V2_SNAPSHOT_CONTENT_FIELDS，含曲线侧栏 v3 键），防漂移。
            from src.app.controllers.edit_controller import (
                V2_SNAPSHOT_CONTENT_FIELDS,
            )
            stamp_fields.update({
                field_name: getattr(source_stack, field_name)
                for field_name in V2_SNAPSHOT_CONTENT_FIELDS
                if getattr(source_stack, field_name, None) is not None
            })
        if domain == domains.VersionDomain.LOCAL:
            stamp_fields.update({
                "scope": "clip_local", "source_kind": "timeline_clip",
                "timeline_id": binding.timeline_id,
                "clip_id": binding.clip_id,
            })
            name = f"Local_{binding.clip_id[:8]}"
        else:
            stamp_fields.update({
                "scope": "asset_default", "source_kind": "asset_default",
            })
            name = f"Asset_{binding.asset_id[:8]}"
        command = _CreateStampedColorStackCommand.create(
            asset_id=(binding.asset_id if domain
                      == domains.VersionDomain.REMOTE else None),
            name=name, stamp_fields=stamp_fields,
            label=f"Create {domain.value} version stack "
                  f"({binding.clip_id[:8]})")
        stack = self._project_stack()
        if stack is None:
            raise ValueError("工程命令栈不可用。")
        self._publish_project(stack.execute(command, current))
        created = command.created_stack_id or ""
        if not created:
            raise ValueError("版本域建栈失败。")
        return created

    # ── 发布与回填 ───────────────────────────────────────────────────

    def _publish_project(self, new_project: Any) -> None:
        owner = self._owner
        owner._current_edit_project = new_project
        owner.edit_project_dirty = True
        panel = getattr(owner, "media_pool_panel", None)
        try:
            set_project = getattr(panel, "set_project", None)
            if callable(set_project):
                set_project(new_project)
            # project_changed 是 pyqtSignal（不可直接调用，须 .emit）；
            # 测试替身可能是普通可调用——两种形态都支持（审查 C1）。
            changed = getattr(panel, "project_changed", None)
            if changed is not None:
                emitter = getattr(changed, "emit", None)
                if callable(emitter):
                    emitter(new_project)
                elif callable(changed):
                    changed(new_project)
        except Exception:  # noqa: BLE001 — 广播失败不撤销已提交事实
            logger.exception("clip version project_changed broadcast failed")

    def _after_content_change(self, request: ClipVersionRequest,
                              new_project: Any,
                              content_changed: bool) -> None:
        owner = self._owner
        if not content_changed:
            # 仅表操作：不重解码；但版本标签经回填刷新
            return
        renderer = getattr(owner, "timeline_preview_renderer", None)
        update = getattr(renderer, "update_color_stacks", None)
        if callable(update):
            update(new_project)
        domain = _scope_to_domain(request.scope)
        stack_id = domains.resolve_domain_stack_id(
            new_project, request.target.timeline_id,
            request.target.clip_id, request.target.asset_id, domain)
        # 当前 Color 会话即目标片段时重新 hydrate（防递归护栏）
        session = self._active_color_session()
        if stack_id and session is not None \
                and getattr(session, "clip_id", None) \
                == request.target.clip_id:
            hydrate = getattr(
                owner, "_load_color_stack_to_grading_panel", None)
            if callable(hydrate):
                previous = getattr(
                    owner, "_color_stack_hydration_in_progress", False)
                owner._color_stack_hydration_in_progress = True
                try:
                    hydrate(stack_id)
                finally:
                    owner._color_stack_hydration_in_progress = previous
            # CACHE-01：失效后调度一次新截图（复用已绘制视口，token
            # 护栏拒绝迟到帧）——卡片先回到原始缩略图（"待更新"），
            # 新版本画面由捕获路径异步补上，不拿旧画面顶替。
            schedule = getattr(
                owner, "_schedule_color_timeline_clip_preview_sync", None)
            if callable(schedule):
                schedule(request.target.clip_id)
        # CACHE-01：依赖该栈的缩略图失效 + 片段栏重建绑定
        if stack_id:
            strip = self._strip()
            invalidate = getattr(
                strip, "invalidate_graded_preview_stacks", None)
            if callable(invalidate):
                invalidate({stack_id})
        refresh = getattr(owner, "refresh_color_timeline_clip_strip", None)
        if callable(refresh):
            refresh()

    def _backfill_destination(self, request: ClipVersionRequest,
                               new_project: Any) -> None:
        if request.destination_scope is None:
            return
        strip = self._strip()
        accept = getattr(strip, "set_clip_version_collection", None)
        if not callable(accept):
            return
        collection = self.build_collection(
            request.target, request.destination_scope, new_project)
        accept(collection)

    def _finish_with_success(self, request: ClipVersionRequest,
                              new_project: Any) -> None:
        # 集合 target 取片段当前绑定（内容变更操作已先刷新片段栏）。
        collection = self.build_collection(
            request.target, request.scope, new_project)
        result = ClipVersionResult(
            request_id=request.request_id, target=request.target,
            collection=collection)
        strip = self._strip()
        finish = getattr(strip, "finish_clip_version_operation", None)
        if callable(finish):
            finish(result)

    def _finish_with_error(self, request: ClipVersionRequest,
                           message: str) -> None:
        result = ClipVersionResult(
            request_id=request.request_id, target=request.target,
            error=message)
        strip = self._strip()
        finish = getattr(strip, "finish_clip_version_operation", None)
        if callable(finish):
            finish(result)


def _rebind_target(target: ClipVersionTarget,
                   stack_id: str | None) -> ClipVersionTarget:
    if target.stack_id == stack_id:
        return target
    from dataclasses import replace
    return replace(target, stack_id=stack_id)


__all__ = ["ClipVersionRequestHost"]
