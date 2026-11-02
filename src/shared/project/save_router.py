"""
SaveRouter - 统一保存路由器。

统一工程包架构的核心组件，确保所有页面的 Save 操作
都写入当前 .topos 工程包的正确区域。

## 保存路由表 (P9.1: ColorSession 集成)

| source_kind | 写入区域 |
|---|---|
| image project | color_stacks[primary_color_stack_id] |
| video asset | color_stacks[asset_default_stack_id] / clip stack |
| timeline_clip | color_stacks[clip.color_stack_id] |
| Composite节点编辑 | project.topos | compositions[active_composite_id] |
| Edit timeline编辑 | project.topos | timeline_sequences |

## 红线约束

- Save不生成旧格式工程文件
- Project内调色统一保存到 .topos 工程包
- 页面不能绕过SaveRouter直接写工程文件

## 设计原则

    - SaveRouter 是统一保存入口，内部只写 .topos 工程包
    - 图片与视频工程统一使用 EditProjectIO.save_project() 保存到 .topos ZIP
- 路由逻辑清晰，每个页面的保存逻辑独立处理
- 使用状态获取回调，避免直接依赖 MainWindow 内部状态
- P9.1: 使用 ColorSession 确定保存目标
"""

from __future__ import annotations

import os
import logging
import shutil
import threading
import tempfile
import uuid
from dataclasses import dataclass, is_dataclass, replace
from enum import Enum
from typing import Any, Callable
import time

# P0-4: 导入 ColorStackData 用于创建新调色栈
from src.shared.project.color_stack import ColorStackData

logger = logging.getLogger(__name__)


# ================================================================
# SaveContext - 保存上下文枚举
# ================================================================

class SaveContext(Enum):
    """
    保存上下文类型。

    标识当前用户正在编辑的内容类型，
    SaveRouter 根据上下文决定写入哪个区域。
    """
    LIBRARY_SINGLE = "library_single"      # Library 打开单张图片/视频
    COLOR_ASSET = "color_asset"            # Media Pool 素材进入 Color
    COLOR_CLIP = "color_clip"              # Timeline 剪辑进入 Color
    COMPOSITE = "composite"                 # Composite 页面节点编辑
    EDIT_TIMELINE = "edit_timeline"         # Edit 页面时间线编辑
    DELIVER = "deliver"                     # Deliver 页面导出设置


# ================================================================
# SaveTarget - 保存目标数据类
# ================================================================

@dataclass(frozen=True)
class SaveTarget:
    """
    保存目标描述。

    描述一次保存操作的目标位置和内容。

    Attributes:
        context: 保存上下文类型
        project_path: .topos 工程包路径
        target_region: 写入区域标识
        asset_id: 相关素材 ID（Color 相关）
        clip_id: 相关剪辑 ID（Color Clip）
        stack_id: 调色栈 ID
        composite_id: 合成 ID
        timeline_id: 时间线 ID
    """
    context: SaveContext
    project_path: str
    target_region: str
    asset_id: str | None = None
    clip_id: str | None = None
    stack_id: str | None = None
    composite_id: str | None = None
    timeline_id: str | None = None


# ================================================================
# SaveRouter - 统一保存路由器
# ================================================================

class SaveRouter:
    """
    统一保存路由器。

    所有页面的 Save 操作都通过 SaveRouter 路由，
    确保写入当前 .topos 工程包的正确区域。

    Usage:
        router = SaveRouter(main_window)
        success = router.save()  # 根据当前页面自动路由

    Callbacks Required:
        - get_current_page: 返回当前页面名称
        - get_current_file_path: 返回当前文件路径（Color 页面）
        - get_current_edit_project: 返回 EditProject 实例
        - get_current_timeline_sequences: 返回时间线序列字典
        - get_current_asset_id: 返回当前素材 ID
        - get_current_clip_id: 返回当前剪辑 ID
        - get_current_stack_id: 返回当前调色栈 ID
        - get_current_composite_id: 返回当前合成 ID
        - get_current_timeline_id: 返回当前时间线 ID
        - get_engine: 返回 ImageEngine 实例
        - get_composite_graph_data: 返回合成图数据
        - get_composite_viewer_state: 返回合成视图状态
        - get_ui_state: 返回 UI 状态
    """

    # 工程包扩展名
    PROJECT_EXTENSION = ".topos"

    def __init__(self) -> None:
        """
        初始化 SaveRouter。

        使用回调模式获取状态，避免直接依赖 MainWindow。
        """
        # 状态获取回调
        self._callbacks: dict[str, Callable] = {}
        
        # 工程包路径缓存
        self._project_path_cache: str | None = None
        
        # Phase 5: Handler 注册系统
        self._handlers: dict[str, Callable[[], bool]] = {}
        
        # 统一读取服务（P0-1: 职责分离，读取逻辑独立于保存）
        from src.shared.project.topos_project_read_service import ToposProjectReadService
        self._read_service = ToposProjectReadService()
        
        logger.info("SaveRouter initialized with handler system")

    # ==================== 回调设置 ====================

    def set_callback(self, name: str, callback: Callable) -> None:
        """
        设置状态获取回调。

        Args:
            name: 回调名称
            callback: 回调函数
        """
        self._callbacks[name] = callback
        logger.debug(f"Callback set: {name}")

    def set_callbacks(self, callbacks: dict[str, Callable]) -> None:
        """
        批量设置回调。

        Args:
            callbacks: 回调字典
        """
        for name, callback in callbacks.items():
            self._callbacks[name] = callback

    def _get_callback(self, name: str) -> Callable | None:
        """获取回调"""
        return self._callbacks.get(name)

    def _call_callback(self, name: str, *args, **kwargs) -> Any:
        """调用回调"""
        callback = self._get_callback(name)
        if callback is None:
            logger.warning(f"Callback not set: {name}")
            return None
        return callback(*args, **kwargs)
    
    # ==================== Handler 注册系统 (Phase 5) ====================
    
    def register_handler(self, page_id: str, handler: Callable[[], bool]) -> None:
        """
        注册页面保存处理器。
        
        Args:
            page_id: 页面标识（如 "color", "edit", "composite", "deliver"）
            handler: 保存处理函数，返回 bool 表示成功/失败
        
        Note:
            Handler 注册后，save() 方法会优先调用 handler。
            如果 handler 未注册，则回退到默认 _save_from_* 方法。
        """
        self._handlers[page_id.lower()] = handler
        logger.info(f"Registered save handler for page: {page_id}")
    
    def unregister_handler(self, page_id: str) -> None:
        """
        取消注册页面保存处理器。
        
        Args:
            page_id: 页面标识
        """
        if page_id.lower() in self._handlers:
            del self._handlers[page_id.lower()]
            logger.info(f"Unregistered save handler for page: {page_id}")
    
    def get_handler(self, page_id: str) -> Callable[[], bool] | None:
        """
        获取页面保存处理器。
        
        Args:
            page_id: 页面标识
        
        Returns:
            Handler 函数，如未注册则返回 None
        """
        return self._handlers.get(page_id.lower())
    
    def has_handler(self, page_id: str) -> bool:
        """
        检查是否有注册的处理器。
        
        Args:
            page_id: 页面标识
        
        Returns:
            True 如果已注册
        """
        return page_id.lower() in self._handlers

    # ==================== 保存入口 ====================

    def save(self) -> bool:
        """
        统一保存入口。

        根据当前页面上下文自动路由到正确的保存逻辑。

        Returns:
            是否成功
        """
        # 获取当前页面
        current_page = self._call_callback("get_current_page")
        if current_page is None:
            logger.warning("Save failed: cannot determine current page")
            return False

        # 根据页面路由
        page_lower = current_page.lower()
        
        # Phase 5: 优先使用注册的 handler
        if self.has_handler(page_lower):
            handler = self.get_handler(page_lower)
            logger.debug(f"Using registered handler for page: {page_lower}")
            return handler()
        
        # 回退到默认方法（硬编码分支）
        if page_lower == "library":
            return self._save_from_color()
        elif page_lower == "color":
            return self._save_from_color()
        elif page_lower == "edit":
            return self._save_from_edit()
        elif page_lower == "composite":
            return self._save_from_composite()
        elif page_lower == "deliver":
            return self._save_from_deliver()
        else:
            logger.warning(f"Save failed: unknown page: {current_page}")
            return False

    def save_with_context(self, context: SaveContext) -> bool:
        """
        指定上下文保存。

        Args:
            context: 保存上下文

        Returns:
            是否成功
        """
        if context == SaveContext.LIBRARY_SINGLE:
            return self._save_from_color()
        elif context in (SaveContext.COLOR_ASSET, SaveContext.COLOR_CLIP):
            return self._save_from_color()
        elif context == SaveContext.COMPOSITE:
            return self._save_from_composite()
        elif context == SaveContext.EDIT_TIMELINE:
            return self._save_from_edit()
        elif context == SaveContext.DELIVER:
            return self._save_from_deliver()
        else:
            logger.warning(f"Save failed: unknown context: {context}")
            return False

    # ==================== 页面保存逻辑 ====================

    def _save_from_color(self) -> bool:
        """
        Color 页面保存。

        场景：Media Pool 素材或 Timeline 剪辑进入 Color。
        写入区域：
        - image project → color_stacks[primary_color_stack_id]
        - video asset → color_stacks[asset_default_stack_id]
        - timeline clip → color_stacks[clip.color_stack_id]

        Returns:
            是否成功
        """
        # ColorSession identifies the active asset/clip, while the
        # EditProject is the only document that is persisted.
        color_session = self._call_callback("get_current_color_session")

        if color_session is None:
            clip_id = self._call_callback("get_current_clip_id")
            asset_id = self._call_callback("get_current_asset_id")
            file_path = self._call_callback("get_current_file_path")
            stack_id = self._call_callback("get_current_stack_id")
            source_kind = None
            timeline_id = self._call_callback("get_current_timeline_id")
        else:
            # 从 ColorSession 获取信息
            clip_id = color_session.clip_id
            asset_id = color_session.asset_id
            file_path = color_session.source_path
            stack_id = color_session.color_stack_id
            source_kind = color_session.source_kind
            timeline_id = color_session.timeline_id

            logger.info(
                f"Color save with ColorSession: source_kind={source_kind.value}, "
                f"stack_id={stack_id}, asset_id={asset_id}, clip_id={clip_id}, "
                f"timeline_id={timeline_id}"
            )

        # 确定工程包路径
        project_path = self._get_project_path_for_context()
        if not project_path:
            # 如果没有工程包路径，使用单素材工程方式
            if file_path:
                project_path = self._get_project_path_for_file(file_path)
            else:
                logger.warning("Color save failed: no project path")
                return False

        # 获取 Engine 状态
        engine = self._call_callback("get_engine")
        if engine is None:
            logger.warning("Color save failed: no engine")
            return False

        edit_project = self._call_callback("get_current_edit_project")
        if edit_project is None:
            logger.warning("Color save failed: no active project")
            return False

        success = self._save_color_to_edit_project(
            project_path,
            edit_project,
            engine,
            asset_id,
            clip_id,
            stack_id=stack_id,
            timeline_id=timeline_id,
        )

        if success:
            self._project_path_cache = project_path
            logger.info(f"Color saved to: {project_path}")

        return success

    def _save_from_edit(self) -> bool:
        """
        Edit 页面保存。

        场景：Edit timeline 编辑。
        写入区域：timeline_sequences

        Returns:
            是否成功
        """
        # 获取 EditProject
        edit_project = self._call_callback("get_current_edit_project")
        if edit_project is None:
            logger.warning("Edit save failed: no edit project")
            return False

        # 获取工程包路径
        project_path = self._get_project_path_for_context()
        if not project_path:
            # 尝试从 EditProject filepath 获取
            filepath = self._call_callback("get_edit_project_filepath")
            if filepath:
                # 确保扩展名是 .topos（统一使用 .topos）
                project_path = self._convert_to_topos_path(filepath)
            else:
                logger.warning("Edit save failed: no project path")
                return False

        # 获取 UI 状态
        ui_state = self._call_callback("get_ui_state")

        # 获取时间线序列
        timeline_sequences = self._call_callback("get_current_timeline_sequences")

        # 保存到工程包
        success = self._save_edit_project_to_topos(
            project_path,
            edit_project,
            ui_state,
            timeline_sequences,
        )

        if success:
            self._project_path_cache = project_path
            logger.info(f"Edit saved to: {project_path}")

        return success

    def _save_from_composite(self) -> bool:
        """
        Composite 页面保存。

        场景：Composite 节点编辑。
        写入区域：compositions[active_composite_id]

        Returns:
            是否成功
        """
        # 获取工程包路径
        project_path = self._get_project_path_for_context()
        if not project_path:
            file_path = self._call_callback("get_current_file_path")
            if file_path:
                project_path = self._get_project_path_for_file(file_path)
            else:
                logger.warning("Composite save failed: no project path")
                return False

        # 获取合成图数据
        composite_graph_data = self._call_callback("get_composite_graph_data")
        if composite_graph_data is None:
            logger.warning("Composite save failed: no composite graph data")
            return False

        # 获取合成视图状态
        composite_viewer_state = self._call_callback("get_composite_viewer_state")

        # 获取当前合成 ID
        composite_id = self._call_callback("get_current_composite_id")

        # Composite state always belongs to the active project.
        edit_project = self._call_callback("get_current_edit_project")
        if edit_project is None:
            logger.warning("Composite save failed: no active project")
            return False

        success = self._save_composite_to_edit_project(
            project_path,
            edit_project,
            composite_graph_data,
            composite_viewer_state,
            composite_id,
        )

        if success:
            self._project_path_cache = project_path
            logger.info(f"Composite saved to: {project_path}")

        return success

    def _save_from_deliver(self) -> bool:
        """
        Deliver 页面保存。

        Deliver 页面主要处理导出，保存主要是更新项目状态。
        使用当前项目路径保存。

        Returns:
            是否成功
        """
        return self._save_from_edit()

    # ==================== 工程包路径管理 ====================

    def _get_project_path_for_file(self, file_path: str) -> str | None:
        """
        根据文件路径获取工程包路径。

        Args:
            file_path: 源素材文件路径

        Returns:
            工程包路径（.topos）
        """
        if not file_path:
            return None

        # 单素材工程：{素材名}.topos
        base_path = os.path.splitext(file_path)[0]
        return base_path + self.PROJECT_EXTENSION

    def _get_project_path_for_context(self) -> str | None:
        """
        根据当前上下文获取工程包路径。

        Returns:
            工程包路径
        """
        # 优先使用缓存的路径
        if self._project_path_cache:
            return self._project_path_cache

        # 尝试获取 EditProject filepath
        filepath = self._call_callback("get_edit_project_filepath")
        if filepath:
            return self._convert_to_topos_path(filepath)

        # 尝试获取当前文件路径
        file_path = self._call_callback("get_current_file_path")
        if file_path:
            return self._get_project_path_for_file(file_path)

        return None

    def _convert_to_topos_path(self, filepath: str) -> str:
        """
        确保路径是 .topos 扩展名。

        所有工程统一使用 .topos 格式。

        Args:
            filepath: 可能是任意路径

        Returns:
            .topos 路径
        """
        # Recovery files are implementation details, never project paths.
        # Strip them before normalizing; otherwise loading an autosave could
        # turn ``demo.topos.autosave`` into ``demo.topos.topos``.
        normalized = str(filepath)
        for suffix in (".autosave", ".bak"):
            if normalized.lower().endswith(suffix):
                normalized = normalized[: -len(suffix)]
        if normalized.lower().endswith(self.PROJECT_EXTENSION):
            return normalized
        # 替换扩展名为 .topos
        return os.path.splitext(normalized)[0] + self.PROJECT_EXTENSION

    def get_current_project_path(self) -> str | None:
        """
        获取当前工程包路径。

        Returns:
            工程包路径
        """
        return self._get_project_path_for_context()

    # ==================== 保存实现 ====================

    def _save_single_asset_project(
        self,
        project_path: str,
        file_path: str,
        engine: Any,
        composite_graph_data: dict | None = None,
        composite_viewer_state: dict | None = None,
        composite_id: str | None = None,
    ) -> bool:
        """
        保存单素材工程到 .topos ZIP 包。

        使用 ToposProjectIO 统一保存，不再依赖 ProjectManager。
        单素材工程（Library 打开的单张图片/视频）保存 ImageEngine 状态
        （Layer、raw_params 等）到 ZIP .topos 格式。

        Args:
            project_path: 工程包路径
            file_path: 源素材路径
            engine: ImageEngine 实例
            composite_graph_data: 合成图数据（可选）
            composite_viewer_state: 合成视图状态（可选）

        Returns:
            是否成功
        """
        from src.shared.project.topos_project import ToposProject
        from src.shared.project.topos_project_io import ToposProjectIO, ToposProjectIOConfig

        try:
            # 确定媒体类型
            media_type = self._detect_media_type(file_path)

            # Schema v2: single asset project only allows images
            if media_type == "video":
                logger.error(
                    "Schema v2 rejection: cannot save video as standalone project. "
                    "Video must go through Edit timeline clips."
                )
                return False

            # 构建 ToposProject 单素材工程
            project_name = os.path.splitext(os.path.basename(project_path))[0]
            project = ToposProject.create_single_asset(
                name=project_name,
                asset_path=file_path,
                media_type=media_type,
            )
            # P0-COLOR-04：色彩管理设定随工程落盘（换机器/交接不丢）
            from src.shared.color.color_space_descriptor import (
                collect_color_management_settings,
            )
            project.settings = dict(project.settings or {})
            project.settings["color_management"] = (
                collect_color_management_settings())

            # 从 ImageEngine 提取调色栈数据
            color_stacks_data = self._extract_color_stacks_from_engine(engine, project)

            # 准备合成数据
            compositions_data = None
            ui_state_data = None
            if composite_graph_data or composite_viewer_state:
                # composite/graphs.json is an id-keyed mapping {composition_id: graph};
                # the reader (topos_project_read_service) takes the first entry. Wrap the
                # raw graph dict so the round-trip agrees (matches the edit-project path,
                # which already writes edit_project.compositions[composite_id]).
                cid = composite_id or "standalone_composite"
                compositions_data = {cid: composite_graph_data} if composite_graph_data else None
                ui_state_data = {"composite_viewer_state": composite_viewer_state or {}}

            # 使用 ToposProjectIO 保存
            config = ToposProjectIOConfig()
            success = ToposProjectIO.save(
                project=project,
                filepath=project_path,
                config=config,
                full_color_stacks_data=color_stacks_data,
                full_compositions_data=compositions_data,
                ui_state_data=ui_state_data,
            )

            if success:
                logger.info(f"Single asset project saved via ToposProjectIO: {project_path}")
            return success

        except Exception as e:
            logger.error(f"Single asset project save failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _save_edit_project_to_topos(
        self,
        project_path: str,
        edit_project: Any,
        ui_state: Any,
        timeline_sequences: dict | None,
    ) -> bool:
        """
        保存 EditProject 到 .topos 工程包。

        使用 EditProjectIO.save_project() 统一保存入口。

        Args:
            project_path: 工程包路径
            edit_project: EditProject 实例
            ui_state: UIState 实例
            timeline_sequences: TimelineSequence 字典

        Returns:
            是否成功
        """
        # 获取 EditProjectIO 回调
        edit_project_io = self._call_callback("get_edit_project_io")
        if edit_project_io is None:
            logger.warning("Save failed: no edit_project_io callback")
            return False

        try:
            # 使用 EditProjectIO.save_project() 统一保存入口
            # EditProjectIO 内部使用 ToposProjectIO.save() 写入 .topos ZIP 包
            success = edit_project_io.save_project(
                project_path,
                edit_project,
                ui_state,
                timeline_sequences,
                teaching_animation_data=self._call_callback("get_teaching_animation_payload"),
                force_project_mode=(
                    "edit_project"
                    if self._call_callback("get_work_mode") == "video"
                    else "standalone_image"
                ),
            )
            if success:
                logger.info(f"Edit project saved via EditProjectIO: {project_path}")
            return success
        except Exception as e:
            logger.error(f"Edit project save failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _save_color_to_edit_project(
        self,
        project_path: str,
        edit_project: Any,
        engine: Any,
        asset_id: str | None,
        clip_id: str | None,
        stack_id: str | None = None,
        timeline_id: str | None = None,
    ) -> bool:
        """
        保存 Color 状态到 EditProject。

        更新 color_stacks 区域。对于 TIMELINE_CLIP，验证 clip 属于正确的 timeline。

        Args:
            project_path: 工程包路径
            edit_project: EditProject 实例
            engine: ImageEngine 实例
            asset_id: 素材 ID
            clip_id: 剪辑 ID
            stack_id: 调色栈 ID（P9.1: 从 ColorSession 获取）
            timeline_id: 时间线 ID（TIMELINE_CLIP 时用于验证 clip 归属）

        Returns:
            是否成功
        """
        # TIMELINE_CLIP 验证：clip_id 和 timeline_id 应匹配 EditProject 中的数据
        if clip_id and timeline_id:
            self._validate_clip_timeline(edit_project, clip_id, timeline_id)

        # P9.1: 优先使用传入的 stack_id（来自 ColorSession）
        if not stack_id:
            # 回退到回调获取
            stack_id = self._call_callback("get_current_stack_id")
        if not stack_id:
            # AUD-36921: a missing stack_id previously minted a fresh UUID on
            # every save, and because the id was never synced back to the
            # session/clip, each subsequent save created yet another stack —
            # orphan stacks piled up. Reuse an existing stack bound to the
            # same clip (or, for asset-level saves, the asset's default
            # stack) before creating a new one.
            stack_id = self._find_existing_color_stack_id(
                edit_project, asset_id=asset_id, clip_id=clip_id
            )
        if not stack_id:
            stack_id = str(uuid.uuid4())
            # Mirror the composite path (on_composite_id_created): give the
            # host a chance to bind the fresh id back into the ColorSession
            # / clip. Optional — the reuse lookup above already prevents
            # orphan pile-up when the callback is not wired.
            created_callback = self._get_callback("on_color_stack_id_created")
            if created_callback is not None:
                created_callback(stack_id)

        # 从 Engine 提取完整调色数据
        color_data = self._extract_full_color_data_from_engine(engine)

        logger.info(
            f"Saving color to EditProject: stack_id={stack_id}, "
            f"asset_id={asset_id}, clip_id={clip_id}, "
            f"fields={list(color_data.keys())}"
        )

        # P0-4 修复：真正写入 color_stacks
        # EditProject 是不可变对象，使用工厂方法创建新实例
        try:
            # 检查 stack_id 是否已存在于 color_stacks
            existing_stack = edit_project.color_stacks.get(stack_id)

            if existing_stack is not None:
                # 更新已有调色栈 — 传递完整数据（含 Layer 快照）
                updated_project = edit_project.update_color_stack(
                    stack_id=stack_id,
                    grading_params=color_data.get('grading_params'),
                    curves_data=color_data.get('curves_data'),
                    hsl_curve_data=color_data.get('hsl_curve_data'),
                    mixer_params=color_data.get('mixer_params'),
                    soft_clip_params=color_data.get('soft_clip_params'),
                    lut_path=color_data.get('lut_path'),
                    lut_intensity=color_data.get('lut_intensity'),
                    layers=color_data.get('layers'),
                    active_layer_index=color_data.get('active_layer_index'),
                    raw_params=color_data.get('raw_params'),
                )
                logger.debug(f"Updated existing color stack: {stack_id}")
            else:
                # 创建新调色栈（使用传入的 stack_id，而不是自动生成 UUID）
                now = time.time()
                stack_factory = (
                    ColorStackData.create_standalone_image
                    if asset_id and not edit_project.timelines
                    else ColorStackData.create
                )
                new_stack = stack_factory(
                    asset_id=asset_id,
                    name=f"Color Stack {len(edit_project.color_stacks) + 1}",
                )
                new_stack = replace(
                    new_stack,
                    stack_id=stack_id,
                    grading_params=color_data.get('grading_params', {}),
                    curves_data=color_data.get('curves_data', {}),
                    hsl_curve_data=color_data.get('hsl_curve_data', {}),
                    mixer_params=color_data.get('mixer_params', {}),
                    soft_clip_params=color_data.get('soft_clip_params', {}),
                    lut_path=color_data.get('lut_path'),
                    lut_intensity=color_data.get('lut_intensity', 1.0),
                    created_at=now,
                    modified_at=now,
                    timeline_id=timeline_id,
                    clip_id=clip_id,
                    layers=tuple(color_data.get('layers') or ()),
                    active_layer_index=int(color_data.get('active_layer_index', 0) or 0),
                    raw_params=color_data.get('raw_params', {}),
                )

                # 手动添加到 color_stacks 并创建新 EditProject
                new_stacks = dict(edit_project.color_stacks)
                new_stacks[stack_id] = new_stack

                if is_dataclass(edit_project):
                    updated_project = replace(
                        edit_project,
                        color_stacks=new_stacks,
                        modified_at=now,
                    )
                else:
                    # Keep the router usable with lightweight test doubles
                    # and adapter objects; the production EditProject is a
                    # frozen dataclass and takes the branch above.
                    updated_project = edit_project.__class__(
                        project_id=edit_project.project_id,
                        name=edit_project.name,
                        assets=edit_project.assets,
                        bins=edit_project.bins,
                        timelines=edit_project.timelines,
                        compositions=edit_project.compositions,
                        color_stacks=new_stacks,
                        comp_clips=getattr(edit_project, "comp_clips", {}),
                        root_bin_id=edit_project.root_bin_id,
                        active_timeline_id=edit_project.active_timeline_id,
                        active_comp_clip_id=getattr(edit_project, "active_comp_clip_id", None),
                        created_at=edit_project.created_at,
                        modified_at=now,
                    )
                logger.debug(f"Created new color stack: {stack_id}")

            # 通过回调更新内存中的 EditProject（保持数据一致性）
            self._call_callback("on_edit_project_updated", updated_project)

            # 保存更新后的 EditProject 到 .topos
            ui_state = self._call_callback("get_ui_state")
            timeline_sequences = self._call_callback("get_current_timeline_sequences")
            return self._save_edit_project_to_topos(
                project_path,
                updated_project,
                ui_state,
                timeline_sequences,
            )
        except Exception as e:
            logger.error(f"Color save to edit project failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _save_composite_to_edit_project(
        self,
        project_path: str,
        edit_project: Any,
        composite_graph_data: dict,
        composite_viewer_state: dict | None,
        composite_id: str | None,
    ) -> bool:
        """
        保存 Composite 状态到 EditProject。

        将 composite_graph_data 写入 edit_project.compositions[composite_id]，
        然后保存更新后的 EditProject 到 .topos。

        Args:
            project_path: 工程包路径
            edit_project: EditProject 实例
            composite_graph_data: 合成图数据
            composite_viewer_state: 合成视图状态
            composite_id: 合成 ID

        Returns:
            是否成功
        """
        try:
            updated_project = edit_project

            # 无 composite_id 但有 graph_data 时，自动生成新 ID
            if not composite_id and composite_graph_data is not None:
                composite_id = str(uuid.uuid4())
                logger.info(f"Composite: auto-generated new composite_id={composite_id}")
                # 同步新 ID 到 UI
                self._call_callback("on_composite_id_created", composite_id)

            # 将 composite_graph_data 写入 edit_project.compositions
            if composite_id and composite_graph_data is not None:
                # U-11/U-12：以既有记录为 base 合并 graph-owned 字段，
                # 保留来源/身份/扩展元信息；viewer_state 按键合并（图
                # 编辑器键 + 显示查看器键 + 未知键），不整字典替换。
                from src.shared.project.composition_merge import (
                    merge_composition_record,
                )
                existing_record = edit_project.compositions.get(composite_id)
                comp_data = merge_composition_record(
                    existing_record,
                    composite_graph_data
                    if isinstance(composite_graph_data, dict) else {},
                    composite_id,
                    display_viewer_state=composite_viewer_state,
                )

                # 更新 compositions dict
                new_compositions = dict(edit_project.compositions)
                new_compositions[composite_id] = comp_data

                now = time.time()
                # AUD-36920: 整对象复制 + 只覆写差异字段，禁止显式字段
                # 白名单重建——旧写法漏传 smart_bins/render_cache_manifests/
                # render_cache_settings/proxy_policy/revision，Composite
                # 保存一次即把这些数据在内存与落盘两侧同时清空。
                if is_dataclass(edit_project):
                    updated_project = replace(
                        edit_project,
                        compositions=new_compositions,
                        modified_at=now,
                    )
                else:
                    # Keep the router usable with lightweight test doubles
                    # and adapter objects; the production EditProject is a
                    # frozen dataclass and takes the branch above. Fallback
                    # copies every public attribute of the source object so
                    # newly added fields are carried over as well.
                    base_kwargs = {
                        attr: getattr(edit_project, attr)
                        for attr in dir(edit_project)
                        if not attr.startswith("_")
                        and not callable(getattr(edit_project, attr, None))
                    }
                    base_kwargs["compositions"] = new_compositions
                    base_kwargs["modified_at"] = now
                    updated_project = edit_project.__class__(**base_kwargs)

                # 同步内存中的 EditProject
                self._call_callback("on_edit_project_updated", updated_project)
                logger.info(f"Composite saved to EditProject: composite_id={composite_id}, nodes={len(comp_data.get('nodes', []))}")

            ui_state = self._call_callback("get_ui_state")
            timeline_sequences = self._call_callback("get_current_timeline_sequences")
            return self._save_edit_project_to_topos(
                project_path,
                updated_project,
                ui_state,
                timeline_sequences,
            )
        except Exception as e:
            logger.error(f"Composite save to edit project failed: {e}")
            return False

    # ==================== 调色参数提取 ====================

    @staticmethod
    def _find_existing_color_stack_id(
        edit_project: Any,
        asset_id: str | None,
        clip_id: str | None,
    ) -> str | None:
        """
        查找已绑定到当前 clip/asset 的既有调色栈 ID。

        AUD-36921：当保存请求未携带 stack_id（会话尚未同步）时，
        复用上一次保存为同一目标创建的栈，而不是再铸造一个新 UUID。
        clip 级匹配优先；asset 级保存匹配该素材的默认栈（无 clip 绑定）。

        Args:
            edit_project: EditProject 实例（或兼容的测试替身）
            asset_id: 素材 ID（可选）
            clip_id: 剪辑 ID（可选）

        Returns:
            既有调色栈 ID，未找到返回 None
        """
        stacks = getattr(edit_project, "color_stacks", None) or {}
        if clip_id:
            for stack in stacks.values():
                if getattr(stack, "clip_id", None) == clip_id:
                    existing_id = getattr(stack, "stack_id", None)
                    if existing_id:
                        return existing_id
        if asset_id:
            for stack in stacks.values():
                if (
                    getattr(stack, "asset_id", None) == asset_id
                    and getattr(stack, "clip_id", None) is None
                ):
                    existing_id = getattr(stack, "stack_id", None)
                    if existing_id:
                        return existing_id
        return None

    def _validate_clip_timeline(
        self,
        edit_project: Any,
        clip_id: str,
        timeline_id: str,
    ) -> bool:
        """
        验证 clip 属于指定的 timeline（诊断日志，不阻断保存）。

        Args:
            edit_project: EditProject 实例
            clip_id: 剪辑 ID
            timeline_id: 时间线 ID

        Returns:
            True 如果验证通过或无法验证（不阻断保存）
        """
        try:
            timeline = edit_project.timelines.get(timeline_id)
            if timeline is None:
                logger.warning(
                    f"SaveRouter validation: timeline_id={timeline_id} not found "
                    f"in EditProject (proceeding with save)"
                )
                return True

            # 尝试在 timeline 的 tracks/clips 中查找 clip_id
            if hasattr(timeline, 'tracks') and timeline.tracks:
                found = False
                for track in timeline.tracks:
                    clips = getattr(track, 'clips', [])
                    for clip in clips:
                        if getattr(clip, 'clip_id', None) == clip_id:
                            found = True
                            break
                    if found:
                        break
                if not found:
                    logger.warning(
                        f"SaveRouter validation: clip_id={clip_id} not found "
                        f"in timeline_id={timeline_id} (proceeding with save)"
                    )
            return True
        except Exception as e:
            logger.debug(f"SaveRouter clip/timeline validation skipped: {e}")
            return True

    def _extract_full_color_data_from_engine(self, engine: Any) -> dict[str, Any]:
        """
        从 ImageEngine 提取完整调色数据（grading + curves + HSL + mixer + soft_clip + LUT）。

        Args:
            engine: ImageEngine 实例

        Returns:
            完整调色数据字典
        """
        if engine is None:
            return {}

        result: dict[str, Any] = {}

        # 从 Engine 的当前 Layer 提取所有调色参数
        if hasattr(engine, 'layers') and engine.layers:
            idx = engine.active_layer_index if engine.active_layer_index < len(engine.layers) else 0
            layer = engine.layers[idx]

            if hasattr(layer, 'grading_params') and layer.grading_params:
                result['grading_params'] = layer.grading_params
            if hasattr(layer, 'curves_data') and layer.curves_data:
                result['curves_data'] = layer.curves_data
            if hasattr(layer, 'hsl_curve_data') and layer.hsl_curve_data:
                result['hsl_curve_data'] = layer.hsl_curve_data
            if hasattr(layer, 'mixer_params') and layer.mixer_params:
                result['mixer_params'] = layer.mixer_params
            if hasattr(layer, 'soft_clip_params') and layer.soft_clip_params:
                result['soft_clip_params'] = layer.soft_clip_params
            if hasattr(layer, 'active_lut_path') and layer.active_lut_path:
                result['lut_path'] = layer.active_lut_path
            if hasattr(layer, 'lut_intensity') and layer.lut_intensity != 1.0:
                result['lut_intensity'] = layer.lut_intensity

        # Raw 参数（Engine 级别）
        if hasattr(engine, 'current_raw_params') and engine.current_raw_params:
            result['raw_params'] = engine.current_raw_params

        # P0-COLOR-02 复查：蒙版跟踪数据随统一路径持久化（此前仅
        # standalone 单素材保存写入，timeline/共享栈场景会丢跟踪结果）。
        tracking_store = getattr(engine, 'tracking_store', None)
        if tracking_store is not None:
            trackings_payload = tracking_store.to_dict()
            if trackings_payload:
                result['trackings'] = trackings_payload

        # P1-COLOR-13: per-clip 视频降噪参数（引擎会话级 → 工程数据）
        stabilization = getattr(engine, 'get_video_stabilization_state', None)
        if callable(stabilization):
            result['video_stabilization'] = stabilization()
        vd_params = getattr(engine, 'video_denoise_params', None)
        if vd_params is not None:
            result['video_denoise'] = vd_params.to_dict()

        # P0-COLOR-02：完整 Layer 快照（权威状态）。
        # 平铺精简键只覆盖核心调色参数；蒙版/限定器/变形器/效果/插件/
        # 混合/烘焙/图层身份只在 Layer.to_dict 里。持久化边界必须刷新
        # 快照，否则时间线片段与 standalone 保存都会丢高级状态。
        if hasattr(engine, 'layers') and engine.layers:
            result['layers'] = [
                layer.to_dict(None) for layer in engine.layers
            ]
            result['active_layer_index'] = (
                engine.active_layer_index
                if engine.active_layer_index < len(engine.layers)
                else 0
            )

        return result

    def _extract_grading_params_from_engine(self, engine: Any) -> dict:
        """向后兼容：仅返回 grading_params 平铺字典。"""
        data = self._extract_full_color_data_from_engine(engine)
        params = data.get('grading_params', {})
        if 'raw_params' in data:
            params['raw_params'] = data['raw_params']
        return params

    def _resolve_backup_source_path(self) -> str | None:
        """Resolve the current canonical project path without writing it."""
        source_file_path = self._call_callback("get_current_file_path")
        topos_path = self._get_project_path_for_context()
        if not topos_path and source_file_path:
            topos_path = self._get_project_path_for_file(source_file_path)
        return topos_path

    @staticmethod
    def _write_backup_copy(topos_path: str, backup_dir: str, max_backups: int) -> bool:
        """Copy and prune backups; safe to call from a worker thread."""
        from datetime import datetime

        if not backup_dir:
            backup_dir = os.path.join(os.path.dirname(topos_path), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        base_name = os.path.splitext(os.path.basename(topos_path))[0]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = os.path.join(backup_dir, f"{base_name}_{timestamp}.topos")
        collision_index = 2
        while os.path.exists(backup_path):
            backup_path = os.path.join(
                backup_dir, f"{base_name}_{timestamp}_{collision_index:06d}.topos"
            )
            collision_index += 1
        temporary_path = ""
        try:
            temp_fd, temporary_path = tempfile.mkstemp(
                prefix=f".{base_name}.", suffix=".backup.tmp", dir=backup_dir
            )
            os.close(temp_fd)
            shutil.copy2(topos_path, temporary_path)
            os.replace(temporary_path, backup_path)
            logger.info("Backup created: %s", backup_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Backup failed: %s", exc)
            return False
        finally:
            if temporary_path and os.path.exists(temporary_path):
                try:
                    os.remove(temporary_path)
                except OSError:
                    pass

        try:
            backups = sorted(
                [
                    f
                    for f in os.listdir(backup_dir)
                    if f.startswith(f"{base_name}_") and f.endswith(".topos")
                ],
                key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)),
            )
            while len(backups) > max_backups:
                old = os.path.join(backup_dir, backups.pop(0))
                os.remove(old)
                logger.debug("Removed old backup: %s", old)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Backup cleanup failed: %s", exc)
        return True

    def backup_project_async(self, backup_dir: str = "", max_backups: int = 5) -> bool:
        """Schedule backup copying without blocking the Qt event loop."""
        if getattr(self, "_backup_in_progress", False):
            logger.debug("Backup already running; skipping overlapping request")
            return False
        topos_path = self._resolve_backup_source_path()
        if not topos_path or not os.path.exists(topos_path):
            logger.warning("Async backup skipped: .topos not found at %s", topos_path)
            return False
        self._backup_in_progress = True

        def _run() -> None:
            try:
                self._write_backup_copy(topos_path, backup_dir, max_backups)
            finally:
                self._backup_in_progress = False

        threading.Thread(target=_run, name="ToposBackupWorker", daemon=True).start()
        return True

    def backup_project(self, backup_dir: str = "", max_backups: int = 5) -> bool:
        """
        备份当前 .topos 工程包到指定目录。

        Args:
            backup_dir: 备份目录（为空则使用工程包同目录下的 backups/）
            max_backups: 最大备份数量

        Returns:
            是否成功
        """
        # 确定源 .topos 路径：Edit/Media Pool 工程优先走 context，
        # Library 单素材才使用 current_file_path 推导。
        # AUD-36922: _resolve_backup_source_path 内部已读取
        # get_current_file_path —— 这里不再重复解析一次；只有走到下方
        # save 兜底分支时才按需补读。
        topos_path = self._resolve_backup_source_path()

        if not topos_path:
            logger.warning("Backup skipped: no current project path")
            return False

        if not os.path.exists(topos_path):
            # 先保存一次，确保 .topos 存在
            if not self.save():
                logger.warning("Backup skipped: failed to save before backup")
                return False
            source_file_path = self._call_callback("get_current_file_path")
            fallback_path = self._get_project_path_for_file(source_file_path) if source_file_path else None
            topos_path = self._get_project_path_for_context() or fallback_path

        if not topos_path or not os.path.exists(topos_path):
            logger.warning(f"Backup skipped: .topos not found at {topos_path}")
            return False

        return self._write_backup_copy(topos_path, backup_dir, max_backups)

    def _detect_media_type(self, file_path: str) -> str:
        """
        根据文件扩展名检测媒体类型。

        Args:
            file_path: 文件路径

        Returns:
            媒体类型（image, video, audio, image_sequence）
        """
        if not file_path:
            return "image"

        ext = os.path.splitext(file_path)[1].lower()

        # 图片格式
        image_extensions = {
            '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp',
            '.gif', '.webp', '.heic', '.heif', '.raw', '.cr2',
            '.nef', '.arw', '.dng', '.exr', '.hdr'
        }
        if ext in image_extensions:
            return "image"

        # 视频格式
        from src.shared.media.format_registry import VIDEO_EXTENSIONS
        if ext in VIDEO_EXTENSIONS:
            return "video"

        # 音频格式
        audio_extensions = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a'}
        if ext in audio_extensions:
            return "audio"

        # 默认为图片
        return "image"

    def _extract_color_stacks_from_engine(
        self,
        engine: Any,
        project: Any,
        assets_dir: str | None = None,
    ) -> dict[str, Any]:
        """
        从 ImageEngine 提取调色栈数据。

        构建 color_stacks.json 格式的数据结构。
        每个层使用 Layer.to_dict() 完整序列化，确保 load 时能完整恢复。

        Args:
            engine: ImageEngine 实例
            project: ToposProject 实例
            assets_dir: 资源目录（用于保存 AI matte 等）

        Returns:
            调色栈数据字典（color_stacks.json 格式）
        """
        if engine is None:
            return {}

        color_stacks_data = {}

        # 使用 project 的 primary_color_stack_id 作为主调色栈 ID
        stack_id = project.primary_color_stack_id
        if not stack_id:
            stack_id = str(uuid.uuid4())

        # 提取 Layer 数据（完整序列化）
        layers_data = []
        if hasattr(engine, 'layers') and engine.layers:
            for idx, layer in enumerate(engine.layers):
                layer_data = self._extract_layer_data(layer, idx, assets_dir=assets_dir)
                layers_data.append(layer_data)

        # 提取 raw_params
        raw_params = {}
        if hasattr(engine, 'current_raw_params'):
            raw_params = engine.current_raw_params

        # 构建调色栈数据
        stack_payload = {
            "stack_id": stack_id,
            "name": "Primary",
            "layers": layers_data,
            "raw_params": raw_params,
            "active_layer_index": engine.active_layer_index if hasattr(engine, 'active_layer_index') else 0,
        }

        # 蒙版跟踪数据（阶段 1）：仅在存在绑定时写入，旧版本加载器可忽略该键
        tracking_store = getattr(engine, "tracking_store", None)
        if tracking_store is not None:
            trackings_payload = tracking_store.to_dict()
            if trackings_payload:
                stack_payload["trackings"] = trackings_payload

        stabilization = getattr(engine, "get_video_stabilization_state", None)
        if callable(stabilization):
            stack_payload["video_stabilization"] = stabilization()
        color_stacks_data[stack_id] = stack_payload

        return color_stacks_data

    def _extract_layer_data(self, layer: Any, index: int, assets_dir: str | None = None) -> dict[str, Any]:
        """
        从 Layer 提取完整数据。

        优先使用 Layer.to_dict() 完整序列化（包含 curves、HSL、mixer、
        soft_clip、LUT、mask、plugins、baked 状态等），
        保证 load_single_asset_data() 能通过 Layer.from_dict() 完整恢复。

        Args:
            layer: Layer 实例
            index: Layer 索引
            assets_dir: 资源目录（用于保存 AI matte 等）

        Returns:
            Layer 数据字典
        """
        # 优先使用 Layer.to_dict() — 包含所有字段
        if hasattr(layer, 'to_dict') and callable(layer.to_dict):
            data = layer.to_dict(assets_dir=assets_dir)
            data["index"] = index
            return data

        # Fallback: 手动提取核心字段
        layer_data = {
            "index": index,
            "id": getattr(layer, 'id', None),
            "name": getattr(layer, 'name', f"Layer {index}"),
            "visible": getattr(layer, 'visible', True),
            "opacity": getattr(layer, 'opacity', 1.0),
            "blend_mode": getattr(layer, 'blend_mode', 'normal'),
        }

        # 调色相关字段
        for attr in ('grading_params', 'curves_data', 'hsl_curve_data',
                     'mixer_params', 'soft_clip_params', 'effects_params',
                     'qualifier_params', 'plugins'):
            val = getattr(layer, attr, None)
            if val:
                layer_data[attr] = val

        # LUT
        lut_path = getattr(layer, 'active_lut_path', None)
        if lut_path:
            layer_data['active_lut_path'] = lut_path
        lut_intensity = getattr(layer, 'lut_intensity', 1.0)
        if lut_intensity != 1.0:
            layer_data['lut_intensity'] = lut_intensity

        # Baked 状态
        if getattr(layer, 'is_baked', False):
            layer_data['is_baked'] = True
            layer_data['baked_file_name'] = getattr(layer, 'baked_file_name', None)

        # Mask shapes
        mask_shapes = getattr(layer, 'mask_shapes', None)
        if mask_shapes:
            layer_data['mask_shapes'] = [
                s.to_dict(assets_dir=assets_dir) if hasattr(s, 'to_dict') else s
                for s in mask_shapes
            ]

        return layer_data

    # ==================== 统一读取服务（委托 ToposProjectReadService） ====================

    def load_single_asset_data(self, topos_path: str) -> dict[str, Any] | None:
        """
        统一读取 .topos 工程包，返回兼容旧格式的 dict。

        委托 ToposProjectReadService.load() 返回强类型 ToposProjectBundle，
        再通过 to_legacy_dict() 转为兼容旧接口的 dict 格式。

        Args:
            topos_path: .topos 工程包路径

        Returns:
            兼容旧格式的 dict 或 None
        """
        bundle = self._read_service.load(topos_path)
        if bundle is None:
            return None
        return bundle.to_legacy_dict()

    # ==================== 工程包管理 ====================

    def create_new_project(
        self,
        project_path: str,
        name: str = "Untitled Project",
    ) -> bool:
        """
        创建新的工程包。

        使用 EditProjectIO 统一保存入口。

        Args:
            project_path: 工程包路径
            name: 项目名称

        Returns:
            是否成功
        """
        # 获取 EditProjectIO 回调
        edit_project_io = self._call_callback("get_edit_project_io")
        if edit_project_io is None:
            logger.warning("Create new project failed: no edit_project_io callback")
            return False

        try:
            from .edit_project_adapter import edit_project_adapter

            # 创建 EditProject (via adapter to avoid direct feature import)
            edit_project = edit_project_adapter.create_project(name)

            # 使用 EditProjectIO.save_project() 统一保存入口
            success = edit_project_io.save_project(
                project_path,
                edit_project,
                None,  # ui_state
                None,  # timeline_sequences
            )

            if success:
                logger.info(f"New project created: {project_path}")
            return success
        except Exception as e:
            logger.error(f"Failed to create new project: {e}")
            return False

    def load_project(self, project_path: str) -> tuple[Any, Any, dict] | None:
        """
        加载工程包。

        使用 EditProjectIO 统一加载入口。

        Args:
            project_path: 工程包路径

        Returns:
            (EditProject, UIState, TimelineSequences) 或 None
        """
        # 获取 EditProjectIO 回调
        edit_project_io = self._call_callback("get_edit_project_io")
        if edit_project_io is None:
            logger.warning("Load project failed: no edit_project_io callback")
            return None

        try:
            if not os.path.exists(project_path):
                logger.warning(f"Project file not found: {project_path}")
                return None

            # 使用 EditProjectIO.load_project() 统一加载入口
            result = edit_project_io.load_project(project_path)
            if result is None:
                logger.warning(f"Failed to load project via EditProjectIO: {project_path}")
                return None

            # EditProjectIO.load_project() 返回 (EditProject, UIState, TimelineSequences)
            return result

        except Exception as e:
            logger.error(f"Failed to load project: {project_path}, {e}")
            return None

    def _is_zip_file(self, filepath: str) -> bool:
        """检测是否是 ZIP 文件"""
        try:
            with open(filepath, 'rb') as f:
                magic = f.read(2)
            return magic == b'PK'
        except Exception:
            return False

    # ==================== 工具方法 ====================

    def clear_cache(self) -> None:
        """清除工程路径缓存"""
        self._project_path_cache = None

    def set_project_path(self, path: str) -> None:
        """手动设置工程路径"""
        self._project_path_cache = path

    def get_save_target(self) -> SaveTarget | None:
        """
        获取当前保存目标描述。

        Returns:
            SaveTarget 或 None
        """
        current_page = self._call_callback("get_current_page")
        if current_page is None:
            return None

        project_path = self._get_project_path_for_context()
        if not project_path:
            return None

        context = self._get_context_from_page(current_page)

        return SaveTarget(
            context=context,
            project_path=project_path,
            target_region=self._get_target_region(context),
            asset_id=self._call_callback("get_current_asset_id"),
            clip_id=self._call_callback("get_current_clip_id"),
            stack_id=self._call_callback("get_current_stack_id"),
            composite_id=self._call_callback("get_current_composite_id"),
            timeline_id=self._call_callback("get_current_timeline_id"),
        )

    def _get_context_from_page(self, page: str) -> SaveContext:
        """根据页面获取上下文"""
        page_lower = page.lower()
        if page_lower == "library":
            return SaveContext.LIBRARY_SINGLE
        elif page_lower == "color":
            clip_id = self._call_callback("get_current_clip_id")
            if clip_id:
                return SaveContext.COLOR_CLIP
            else:
                return SaveContext.COLOR_ASSET
        elif page_lower == "edit":
            return SaveContext.EDIT_TIMELINE
        elif page_lower == "composite":
            return SaveContext.COMPOSITE
        elif page_lower == "deliver":
            return SaveContext.DELIVER
        else:
            return SaveContext.LIBRARY_SINGLE

    def _get_target_region(self, context: SaveContext) -> str:
        """根据上下文获取写入区域"""
        region_map = {
            SaveContext.LIBRARY_SINGLE: "assets+color_stack",
            SaveContext.COLOR_ASSET: "color_stacks[asset_default_stack_id]",
            SaveContext.COLOR_CLIP: "ClipInstance.color_stack_id+color_stacks",
            SaveContext.COMPOSITE: "compositions[active_composite_id]",
            SaveContext.EDIT_TIMELINE: "timeline_sequences",
            SaveContext.DELIVER: "timeline_sequences",
        }
        return region_map.get(context, "unknown")
