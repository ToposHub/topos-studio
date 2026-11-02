"""
ColorSession - Color 页面会话管理。

根据统一工程包架构方案 6，Color 页面必须从 ColorSession 判断
自己正在编辑谁，而不是只看 current_file_path。

## 设计原则

- ColorSession 描述 Color 页面的完整状态
- source_kind 决定保存目标区域
- frozen=True 确保不可变，线程安全

## 保存策略

| source_kind | 写入区域 |
|---|---|
| single_asset | color_stacks[primary_color_stack_id] |
| project_asset | color_stacks[asset_default_stack_id] |
| timeline_clip | color_stacks[clip.color_stack_id] |

## 与 PageContext 的关系

- PageContext: 用于页面切换时传递上下文（临时性）
- ColorSession: 用于 Color 页面完整状态管理（持久性）

ColorSession 从 PageContext 创建，但包含更多信息。
"""

from __future__ import annotations

import uuid
import time
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ================================================================
# SourceKind - 来源类型枚举
# ================================================================

class SourceKind(Enum):
    """
    Color 页面的编辑来源类型。

    决定保存时写入哪个区域。

    Values:
        SINGLE_ASSET: Library 打开的单素材工程
        PROJECT_ASSET: [DEPRECATED for video] Media Pool 图片素材进入 Color;
            video assets must use TIMELINE_CLIP via Edit timeline.
        TIMELINE_CLIP: Timeline 剪辑进入 Color (required for all video)
    """
    SINGLE_ASSET = "single_asset"
    PROJECT_ASSET = "project_asset"  # DEPRECATED for video — only images may use this
    TIMELINE_CLIP = "timeline_clip"


# ================================================================
# SavePolicy - 保存策略枚举
# ================================================================

class SavePolicy(Enum):
    """
    保存策略。

    决定数据写入的目标位置。

    Values:
        TOPOS_PROJECT: 写入统一 .topos 工程包
    """
    TOPOS_PROJECT = "topos_project"


# ================================================================
# ColorSession - Color 页面会话
# ================================================================

@dataclass(frozen=True)
class ColorSession:
    """
    Color 页面会话数据。

    描述 Color 页面的完整状态，用于：
    1. 判断正在编辑谁
    2. 确定保存目标区域
    3. 页面切换时的上下文传递

    Attributes:
        session_id: 会话唯一标识符
        source_kind: 来源类型（决定保存目标）
        project_id: 项目 ID
        color_stack_id: 调色栈 ID（必填）
        asset_id: 素材 ID（project_asset/timeline_clip 时必有）
        timeline_id: 时间线 ID（timeline_clip 时必有）
        clip_id: 剪辑 ID（timeline_clip 时必有）
        frame: 当前帧号（视频时）
        source_path: 源文件路径（用于渲染）
        save_policy: 保存策略
        created_at: 创建时间戳
    """
    session_id: str
    source_kind: SourceKind
    project_id: str
    color_stack_id: str  # 必填，必须在可选参数之前
    asset_id: str | None = None
    timeline_id: str | None = None
    clip_id: str | None = None
    timeline_frame: int | None = None
    source_frame: int | None = None
    clip_local_frame: int | None = None
    frame: int | None = None  # Deprecated. Use source_frame/timeline_frame instead.
    source_path: str | None = None
    save_policy: SavePolicy = SavePolicy.TOPOS_PROJECT
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """验证数据有效性"""
        if not self.session_id:
            raise ValueError("session_id cannot be empty")
        if not self.project_id:
            raise ValueError("project_id cannot be empty")
        if not self.color_stack_id:
            raise ValueError("color_stack_id cannot be empty")

        # 验证 source_kind 对应的字段
        if self.source_kind == SourceKind.PROJECT_ASSET:
            if not self.asset_id:
                raise ValueError("asset_id required for PROJECT_ASSET")
        elif self.source_kind == SourceKind.TIMELINE_CLIP:
            if not self.asset_id:
                raise ValueError("asset_id required for TIMELINE_CLIP")
            if not self.clip_id:
                raise ValueError("clip_id required for TIMELINE_CLIP")
            if not self.timeline_id:
                raise ValueError("timeline_id required for TIMELINE_CLIP")

    # ==================== 工厂方法 ====================

    @staticmethod
    def create_single_asset(
        project_id: str,
        color_stack_id: str,
        source_path: str,
        frame: int | None = None,
    ) -> ColorSession:
        """
        创建单素材工程的 ColorSession。

        用于 Library 打开的单张图片。视频调色必须通过 Edit 时间线 clip 进入 Color。

        Args:
            project_id: 项目 ID
            color_stack_id: 主调色栈 ID
            source_path: 源文件路径
            frame: 保留字段；单图场景通常为 None

        Returns:
            ColorSession 实例
        """
        return ColorSession(
            session_id=str(uuid.uuid4()),
            source_kind=SourceKind.SINGLE_ASSET,
            project_id=project_id,
            color_stack_id=color_stack_id,
            source_path=source_path,
            frame=frame,
        )

    @staticmethod
    def create_project_asset(
        project_id: str,
        asset_id: str,
        color_stack_id: str,
        source_path: str,
        frame: int | None = None,
    ) -> ColorSession:
        """
        创建项目素材的 ColorSession。

        用于 Media Pool 图片素材进入 Color。
        视频素材必须通过 Edit timeline clip (TIMELINE_CLIP) 进入 Color。

        Args:
            project_id: 项目 ID
            asset_id: 素材 ID
            color_stack_id: 素材默认调色栈 ID
            source_path: 源文件路径
            frame: 当前帧号（视频时）

        Returns:
            ColorSession 实例

        Raises:
            ValueError: 如果 source_path 是视频文件
        """
        import warnings

        # Guard: reject video files (only warn when actually video)
        if source_path:
            from src.shared.media.format_registry import is_video
            if is_video(source_path):
                warnings.warn(
                    "PROJECT_ASSET is deprecated for video. "
                    "Video must go through Edit timeline (TIMELINE_CLIP).",
                    DeprecationWarning,
                    stacklevel=2,
                )
                raise ValueError(
                    "Video must be opened from an Edit timeline clip, "
                    "not directly as PROJECT_ASSET."
                )

        return ColorSession(
            session_id=str(uuid.uuid4()),
            source_kind=SourceKind.PROJECT_ASSET,
            project_id=project_id,
            asset_id=asset_id,
            color_stack_id=color_stack_id,
            source_path=source_path,
            frame=frame,
        )

    @staticmethod
    def create_timeline_clip(
        project_id: str,
        asset_id: str,
        clip_id: str,
        timeline_id: str,
        color_stack_id: str,
        source_path: str,
        frame: int | None = None,
        timeline_frame: int | None = None,
        source_frame: int | None = None,
        clip_local_frame: int | None = None,
    ) -> ColorSession:
        """
        创建时间线剪辑的 ColorSession。

        用于 Timeline 剪辑进入 Color。

        Args:
            project_id: 项目 ID
            asset_id: 素材 ID
            clip_id: 剪辑 ID
            color_stack_id: 剪辑调色栈 ID
            source_path: 源文件路径
            timeline_id: 时间线 ID
            frame: 当前帧号
            timeline_frame: 时间线绝对帧号
            source_frame: 源素材帧号
            clip_local_frame: 片段本地帧号

        Returns:
            ColorSession 实例
        """
        return ColorSession(
            session_id=str(uuid.uuid4()),
            source_kind=SourceKind.TIMELINE_CLIP,
            project_id=project_id,
            asset_id=asset_id,
            clip_id=clip_id,
            color_stack_id=color_stack_id,
            source_path=source_path,
            timeline_id=timeline_id,
            frame=frame,
            timeline_frame=timeline_frame,
            source_frame=source_frame,
            clip_local_frame=clip_local_frame,
        )

    @staticmethod
    def from_page_context(
        context: Any,
        project_id: str,
        color_stack_id: str,
        source_path: str,
    ) -> ColorSession:
        """
        从 PageContext 创建 ColorSession。

        Args:
            context: PageContext 实例
            project_id: 项目 ID
            color_stack_id: 调色栈 ID
            source_path: 源文件路径

        Returns:
            ColorSession 实例

        Raises:
            ValueError: 如果是视频但没有 clip_id（视频必须走 TIMELINE_CLIP）

        Note:
            根据 context 的字段判断 source_kind：
            - 有 clip_id → TIMELINE_CLIP
            - 有 asset_id 但无 clip_id:
                - 图片 → PROJECT_ASSET
                - 视频 → ValueError
            - 无 asset_id 无 clip_id → SINGLE_ASSET
        """
        clip_id = context.clip_id
        asset_id = context.asset_id
        timeline_id = context.timeline_id

        # 读取新的帧字段（带 fallback）
        timeline_frame = getattr(context, 'timeline_frame', None)
        source_frame = getattr(context, 'source_frame', None)
        clip_local_frame = getattr(context, 'clip_local_frame', None)
        # Deprecated display frame: prefer explicit values without treating 0 as missing.
        frame = timeline_frame if timeline_frame is not None else source_frame

        # 判断 source_kind
        if clip_id:
            return ColorSession.create_timeline_clip(
                project_id=project_id,
                asset_id=asset_id or "",
                clip_id=clip_id,
                timeline_id=timeline_id or "",
                color_stack_id=color_stack_id,
                source_path=source_path,
                frame=frame,
                timeline_frame=timeline_frame,
                source_frame=source_frame,
                clip_local_frame=clip_local_frame,
            )
        elif asset_id:
            # Guard: video without clip_id is not allowed
            if source_path:
                from src.shared.media.format_registry import is_video
                if is_video(source_path):
                    raise ValueError(
                        "Video must be opened from an Edit timeline clip, "
                        "not directly from Media Pool. "
                        f"source_path={source_path}"
                    )

            # Image: PROJECT_ASSET still allowed
            return ColorSession.create_project_asset(
                project_id=project_id,
                asset_id=asset_id,
                color_stack_id=color_stack_id,
                source_path=source_path,
                frame=frame,
            )
        else:
            return ColorSession.create_single_asset(
                project_id=project_id,
                color_stack_id=color_stack_id,
                source_path=source_path,
                frame=frame,
            )

    # ==================== 实用方法 ====================

    def get_save_target_region(self) -> str:
        """
        获取保存目标区域描述。

        三种 SourceKind 共用同一区域格式：``color_stacks[<stack_id>]``。
        ``color_stack_id`` 在会话创建时已按来源解析为最终目标栈
        （primary / asset default / clip 绑定），故此处无需再分支。

        Returns:
            保存区域描述字符串

        Examples:
            - SINGLE_ASSET → "color_stacks[<已解析的 stack id>]"
            - PROJECT_ASSET → "color_stacks[<已解析的 stack id>]"
            - TIMELINE_CLIP → "color_stacks[<已解析的 stack id>]"
        """
        # AUD-0403: 三个分支返回值完全相同——收敛为单行返回，避免读者误以为
        # 分支间存在语义差异；未解析的 stack id 差异由会话创建入口保证。
        return f"color_stacks[{self.color_stack_id}]"

    def is_single_asset(self) -> bool:
        """是否为单素材工程"""
        return self.source_kind == SourceKind.SINGLE_ASSET

    def is_project_asset(self) -> bool:
        """是否为项目素材（DEPRECATED for video — only images should use this）"""
        return self.source_kind == SourceKind.PROJECT_ASSET

    def is_timeline_clip(self) -> bool:
        """是否为时间线剪辑"""
        return self.source_kind == SourceKind.TIMELINE_CLIP

    def has_frame(self) -> bool:
        """是否有帧号（视频）"""
        return self.frame is not None

    def is_standalone_image(self) -> bool:
        """是否为独立图片（单素材工程）"""
        return self.source_kind == SourceKind.SINGLE_ASSET

    # ==================== 序列化 ====================

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典"""
        return {
            "session_id": self.session_id,
            "source_kind": self.source_kind.value,
            "project_id": self.project_id,
            "asset_id": self.asset_id,
            "timeline_id": self.timeline_id,
            "clip_id": self.clip_id,
            "timeline_frame": self.timeline_frame,
            "source_frame": self.source_frame,
            "clip_local_frame": self.clip_local_frame,
            "frame": self.frame,
            "color_stack_id": self.color_stack_id,
            "source_path": self.source_path,
            "save_policy": self.save_policy.value,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ColorSession:
        """从字典反序列化"""
        return ColorSession(
            session_id=data.get("session_id", str(uuid.uuid4())),
            source_kind=SourceKind(data.get("source_kind", "single_asset")),
            project_id=data.get("project_id", ""),
            asset_id=data.get("asset_id"),
            timeline_id=data.get("timeline_id"),
            clip_id=data.get("clip_id"),
            timeline_frame=data.get("timeline_frame"),
            source_frame=data.get("source_frame"),
            clip_local_frame=data.get("clip_local_frame"),
            frame=data.get("frame"),
            color_stack_id=data.get("color_stack_id", ""),
            source_path=data.get("source_path"),
            save_policy=SavePolicy(data.get("save_policy", "topos_project")),
            created_at=data.get("created_at", time.time()),
        )

    # ==================== ProjectDocument Protocol ====================

    @property
    def document_type(self) -> str:
        """ProjectDocument protocol: document type identifier."""
        return "color_session"

    @property
    def dirty(self) -> bool:
        """ProjectDocument protocol: sessions are transient, always dirty."""
        return True

    def save_to_path(self, path: str) -> None:
        """ProjectDocument protocol: save session data to JSON file."""
        import json
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load_from_path(cls, path: str) -> ColorSession:
        """ProjectDocument protocol: load session from JSON file."""
        import json
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls.from_dict(data)

    # ==================== 比较和哈希 ====================

    def __hash__(self) -> int:
        """哈希值，基于 session_id"""
        return hash(self.session_id)

    def __eq__(self, other: object) -> bool:
        """相等比较，基于 session_id"""
        if not isinstance(other, ColorSession):
            return NotImplemented
        return self.session_id == other.session_id

    def __repr__(self) -> str:
        """详细表示"""
        return (
            f"ColorSession(session_id={self.session_id[:8]}, "
            f"source_kind={self.source_kind.value}, "
            f"stack_id={self.color_stack_id[:8]})"
        )


# ================================================================
# ColorSessionManager - 会话管理器
# ================================================================

class ColorSessionManager:
    """
    ColorSession 管理器。

    管理当前活跃的 ColorSession，提供：
    - 当前 session 存储
    - session 创建和切换
    - 与 SaveRouter 的集成

    Usage:
        manager = ColorSessionManager()
        session = manager.create_from_page_context(context, ...)
        manager.set_current_session(session)

        # 获取当前 session
        current = manager.get_current_session()
    """

    def __init__(self) -> None:
        """初始化管理器"""
        self._current_session: ColorSession | None = None
        # AUD-0401: 历史用 OrderedDict 承载并设容量上限——每次进入 Color 页
        # 都会创建新 uuid4 session，无限增长的 dict 在长会话中缓慢泄漏。
        # 最近使用的 session 移到尾部，超限时从头部（最旧）淘汰。
        self._session_history: 'OrderedDict[str, ColorSession]' = OrderedDict()
        self._max_history: int = 64
        # B06.4：会话切换观察者（节点图宿主注册；见 add_session_observer）
        self._session_observers: list = []

        logger.debug("ColorSessionManager initialized")

    # ==================== Session 管理 ====================

    def get_current_session(self) -> ColorSession | None:
        """获取当前 session"""
        return self._current_session

    def add_session_observer(self, callback) -> None:
        """注册会话切换观察者（B06.4）。

        回调签名 ``callback(session_or_none)``；``set_current_session`` /
        ``clear_current_session`` 时同步触发。节点图宿主用它执行：
        结束/取消当前事务 → generation 递增 → 重新水合 scope → 丢弃
        晚到事件。观察者异常只记录不阻断会话切换。
        """
        self._session_observers.append(callback)

    def _notify_observers(self, session) -> None:
        for observer in list(self._session_observers):
            try:
                observer(session)
            except Exception:  # noqa: BLE001 —— 观察者故障不阻断切换
                logger.warning("session observer failed", exc_info=True)

    def set_current_session(self, session: ColorSession) -> None:
        """
        设置当前 session。

        Args:
            session: ColorSession 实例
        """
        self._current_session = session
        # 记录历史（AUD-0401: 有界 LRU，超限淘汰最旧条目）
        history = self._session_history
        if session.session_id in history:
            history.move_to_end(session.session_id)
        history[session.session_id] = session
        while len(history) > self._max_history:
            history.popitem(last=False)
        self._notify_observers(session)

        logger.info(f"ColorSession set: {session}")

    def clear_current_session(self) -> None:
        """清除当前 session"""
        self._current_session = None
        self._notify_observers(None)
        logger.debug("ColorSession cleared")

    def has_active_session(self) -> bool:
        """是否有活跃 session"""
        return self._current_session is not None

    def get_session_by_id(self, session_id: str) -> ColorSession | None:
        """根据 ID 获取历史 session"""
        return self._session_history.get(session_id)

    # ==================== Session 创建 ====================

    def create_single_asset_session(
        self,
        project_id: str,
        color_stack_id: str,
        source_path: str,
        frame: int | None = None,
    ) -> ColorSession:
        """
        创建并设置单素材 session。

        Args:
            project_id: 项目 ID
            color_stack_id: 调色栈 ID
            source_path: 源路径
            frame: 帧号

        Returns:
            创建的 session
        """
        session = ColorSession.create_single_asset(
            project_id=project_id,
            color_stack_id=color_stack_id,
            source_path=source_path,
            frame=frame,
        )
        self.set_current_session(session)
        return session

    def create_project_asset_session(
        self,
        project_id: str,
        asset_id: str,
        color_stack_id: str,
        source_path: str,
        frame: int | None = None,
    ) -> ColorSession:
        """
        创建并设置项目素材 session。

        DEPRECATED for video. Only images should use PROJECT_ASSET.
        Video must go through Edit timeline (TIMELINE_CLIP).

        Args:
            project_id: 项目 ID
            asset_id: 素材 ID
            color_stack_id: 调色栈 ID
            source_path: 源路径
            frame: 帧号

        Returns:
            创建的 session
        """
        session = ColorSession.create_project_asset(
            project_id=project_id,
            asset_id=asset_id,
            color_stack_id=color_stack_id,
            source_path=source_path,
            frame=frame,
        )
        self.set_current_session(session)
        return session

    def create_timeline_clip_session(
        self,
        project_id: str,
        asset_id: str,
        clip_id: str,
        timeline_id: str,
        color_stack_id: str,
        source_path: str,
        frame: int | None = None,
        timeline_frame: int | None = None,
        source_frame: int | None = None,
        clip_local_frame: int | None = None,
    ) -> ColorSession:
        """
        创建并设置时间线剪辑 session。

        Args:
            project_id: 项目 ID
            asset_id: 素材 ID
            clip_id: 剪辑 ID
            color_stack_id: 调色栈 ID
            source_path: 源路径
            timeline_id: 时间线 ID
            frame: 帧号
            timeline_frame: 时间线绝对帧号
            source_frame: 源素材帧号
            clip_local_frame: 片段本地帧号

        Returns:
            创建的 session
        """
        session = ColorSession.create_timeline_clip(
            project_id=project_id,
            asset_id=asset_id,
            clip_id=clip_id,
            timeline_id=timeline_id,
            color_stack_id=color_stack_id,
            source_path=source_path,
            frame=frame,
            timeline_frame=timeline_frame,
            source_frame=source_frame,
            clip_local_frame=clip_local_frame,
        )
        self.set_current_session(session)
        return session

    def create_from_page_context(
        self,
        context: Any,
        project_id: str,
        color_stack_id: str,
        source_path: str,
    ) -> ColorSession:
        """
        从 PageContext 创建并设置 session。

        Args:
            context: PageContext 实例
            project_id: 项目 ID
            color_stack_id: 调色栈 ID
            source_path: 源路径

        Returns:
            创建的 session
        """
        session = ColorSession.from_page_context(
            context=context,
            project_id=project_id,
            color_stack_id=color_stack_id,
            source_path=source_path,
        )
        self.set_current_session(session)
        return session

    # ==================== 状态获取 ====================

    def get_current_color_stack_id(self) -> str | None:
        """获取当前调色栈 ID"""
        if self._current_session:
            return self._current_session.color_stack_id
        return None

    def get_current_source_path(self) -> str | None:
        """获取当前源路径"""
        if self._current_session:
            return self._current_session.source_path
        return None

    def get_current_source_kind(self) -> SourceKind | None:
        """获取当前来源类型"""
        if self._current_session:
            return self._current_session.source_kind
        return None

    def get_current_project_id(self) -> str | None:
        """获取当前项目 ID"""
        if self._current_session:
            return self._current_session.project_id
        return None

    def get_current_timeline_id(self) -> str | None:
        """获取当前时间线 ID"""
        if self._current_session:
            return self._current_session.timeline_id
        return None

    def get_current_asset_id(self) -> str | None:
        """获取当前素材 ID"""
        if self._current_session:
            return self._current_session.asset_id
        return None

    def get_current_clip_id(self) -> str | None:
        """获取当前剪辑 ID"""
        if self._current_session:
            return self._current_session.clip_id
        return None

    def get_current_frame(self) -> int | None:
        """获取当前帧号"""
        if self._current_session:
            return self._current_session.frame
        return None
