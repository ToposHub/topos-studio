"""F4.4 · 批量镜头匹配提交——F4.1 微调栈 + F4.2 版本锚的领域命令。

``ApplyShotMatchCommand``：把 ``plan_shot_match`` 的可应用建议一次提交：
1. 每个适用镜头建/改 **F4.1 微调栈**（``scope="clip_trim"``，参数 =
   匹配量的 GradingMath 映射；已有 trim 栈时**设置**匹配参数、保留
   其他参数——重跑匹配收敛不叠加）并绑定 clip；
2. 每个受影响镜头的**主栈**先保存 ``pre-shot-match`` 命名版本
   （F4.2 回退锚：一键激活即回匹配前）；
3. 受锁定镜头/F4.2 已锁栈/被排除建议**零写入**；
4. **过时 revision 拒绝**：命令创建时记录工程 revision，apply 时工程
   已前进则整体拒绝（建议基于过期状态）；
5. undo 对称恢复（栈表 + 时间线片段 + 版本表）。

导出一致：微调栈经 F4.1 链（pre→主栈→trim→post）在预览/成片/Stem
三路生效——匹配结果即导出真相，非当前序列同样取激活内容。
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from src.features.color.ai.color_context import ShotMatchReport
from src.shared.project.color_stack import ColorStackData

PRE_MATCH_VERSION_NAME = "pre-shot-match"

#: 匹配写进 trim 栈的参数键（设置语义；其余 trim 参数保留）
_MATCH_PARAM_KEYS = ("exposure", "temp", "tint", "saturation")


@dataclass(frozen=True)
class ApplyShotMatchCommand:
    """批量镜头匹配提交（受限事务：过期/缺失整体拒绝，零半写）。"""

    command_id: str
    label: str
    timeline_id: str
    expected_revision: int
    suggestions: Tuple[Tuple[str, Dict[str, Any]], ...]  # (clip_id, params)
    _before_stacks: Optional[Dict[str, Any]] = None
    _before_clip_trims: Optional[Dict[str, Optional[str]]] = None
    _before_versions: Optional[Dict[str, Any]] = None
    created_trim_stack_ids: Tuple[str, ...] = ()
    versioned_stack_ids: Tuple[str, ...] = ()

    def apply(self, project: Any) -> Any:
        current_revision = int(getattr(project, "revision", 0) or 0)
        if current_revision != self.expected_revision:
            raise ValueError(
                f"工程已前进（revision {current_revision} ≠ 计划时 "
                f"{self.expected_revision}），匹配建议过期——请重新分析。")
        timeline = (getattr(project, "timelines", {}) or {}).get(
            self.timeline_id)
        if timeline is None:
            raise ValueError(f"时间线不存在：{self.timeline_id}")

        before_stacks = dict(getattr(project, "color_stacks", {}) or {})
        before_versions = copy.deepcopy(
            getattr(project, "color_stack_versions", None) or {})
        before_trims: Dict[str, Optional[str]] = {}
        after_stacks = dict(before_stacks)
        after_versions = copy.deepcopy(before_versions)
        created: list = []
        versioned: list = []

        for clip_id, params in self.suggestions:
            clip = timeline.get_clip_by_id(clip_id)
            if clip is None:
                raise ValueError(
                    f"镜头 {clip_id} 不在时间线 {self.timeline_id} 上"
                    "——建议过期，请重新分析。")
            before_trims[clip_id] = getattr(
                clip, "color_trim_stack_id", None)

            # F4.2：主栈回退锚（每镜头一次；重复提交不覆盖首个锚）
            main_id = getattr(clip, "color_stack_id", None)
            if main_id and main_id in after_stacks:
                state = after_versions.get(main_id) or {}
                versions = dict(state.get("versions", {}) or {})
                if PRE_MATCH_VERSION_NAME not in versions:
                    payload = copy.deepcopy(
                        after_stacks[main_id].to_dict())
                    payload["plugin_versions"] = {}
                    payload["saved_at"] = time.time()
                    versions[PRE_MATCH_VERSION_NAME] = payload
                    after_versions[main_id] = {
                        "active": state.get("active"),
                        "locked": bool(state.get("locked", False)),
                        "versions": versions,
                    }
                    versioned.append(main_id)

            # F4.1：微调栈建/改（设置语义）
            trim_id = before_trims[clip_id]
            if trim_id and trim_id in after_stacks:
                existing = after_stacks[trim_id]
                merged = dict(existing.grading_params)
                merged.update(params)
                after_stacks[trim_id] = ColorStackData.from_dict({
                    **existing.to_dict(),
                    "grading_params": merged,
                    "modified_at": time.time(),
                })
            else:
                trim_id = f"trim-match-{uuid.uuid4()}"
                created.append(trim_id)
                base = dict(ColorStackData(
                    stack_id=trim_id).grading_params)
                base.update(params)
                after_stacks[trim_id] = ColorStackData(
                    stack_id=trim_id, name="Shot Match Trim",
                    grading_params=base,
                    scope="clip_trim", source_kind="timeline_clip",
                    timeline_id=self.timeline_id, clip_id=clip_id,
                    created_at=time.time(), modified_at=time.time(),
                )

            updated_clip = clip.with_color_trim_stack(trim_id)
            track = timeline.get_track_by_id(updated_clip.track_id)
            if track is None:
                raise ValueError(f"轨道不存在：{updated_clip.track_id}")
            timeline = timeline.with_track_updated(
                track.with_clip_updated(updated_clip))

        timelines = dict(getattr(project, "timelines", {}) or {})
        timelines[self.timeline_id] = timeline

        from dataclasses import replace as _replace
        after_project = _replace(
            project, timelines=timelines, color_stacks=after_stacks,
            color_stack_versions=after_versions)
        updated_cmd = ApplyShotMatchCommand(
            command_id=self.command_id, label=self.label,
            timeline_id=self.timeline_id,
            expected_revision=self.expected_revision,
            suggestions=self.suggestions,
            _before_stacks=before_stacks,
            _before_clip_trims=before_trims,
            _before_versions=before_versions,
            created_trim_stack_ids=tuple(created),
            versioned_stack_ids=tuple(versioned),
        )
        return after_project, updated_cmd

    def undo(self, project: Any) -> Any:
        if self._before_stacks is None:
            raise ValueError("命令未 apply，无法撤销。")
        timeline = (getattr(project, "timelines", {}) or {}).get(
            self.timeline_id)
        if timeline is None:
            raise ValueError(f"时间线不存在：{self.timeline_id}")
        for clip_id, trim_id in (self._before_clip_trims or {}).items():
            clip = timeline.get_clip_by_id(clip_id)
            if clip is None:
                continue
            restored = clip.with_color_trim_stack(trim_id)
            track = timeline.get_track_by_id(restored.track_id)
            if track is not None:
                timeline = timeline.with_track_updated(
                    track.with_clip_updated(restored))
        timelines = dict(getattr(project, "timelines", {}) or {})
        timelines[self.timeline_id] = timeline
        from dataclasses import replace as _replace
        return _replace(
            project, timelines=timelines,
            color_stacks=dict(self._before_stacks),
            color_stack_versions=copy.deepcopy(
                self._before_versions or {}))

    @staticmethod
    def create(
        project: Any,
        timeline_id: str,
        report: ShotMatchReport,
        *,
        auto_apply_threshold: Optional[float] = None,
    ) -> "ApplyShotMatchCommand":
        """从匹配报告构造提交命令（只收可应用建议；锁定/排除零进入）。"""
        threshold = (auto_apply_threshold
                     if auto_apply_threshold is not None
                     else report.auto_apply_threshold)
        applicable = [
            s for s in report.suggestions
            if not s.excluded and not s.exclude_reason
            and s.confidence >= threshold and s.suggested_operations
        ]
        suggestions: list = []
        for s in applicable:
            params: Dict[str, Any] = {}
            for op in s.suggested_operations:
                for key in _MATCH_PARAM_KEYS:
                    if key in op.parameters:
                        params[key] = float(op.parameters[key])
            if params:
                suggestions.append((s.clip_id, params))
        if not suggestions:
            raise ValueError("无可应用的匹配建议（全部需人工确认或被排除）。")
        return ApplyShotMatchCommand(
            command_id=str(uuid.uuid4()),
            label=f"Apply Shot Match ({len(suggestions)} clips "
                  f"→ {report.reference_clip_id})",
            timeline_id=timeline_id,
            expected_revision=int(getattr(project, "revision", 0) or 0),
            suggestions=tuple(suggestions))


__all__ = ["ApplyShotMatchCommand", "PRE_MATCH_VERSION_NAME"]
