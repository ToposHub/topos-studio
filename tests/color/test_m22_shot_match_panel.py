"""M22（R07）· 镜头匹配前台面板回归。

验收（计划 §M22 首片）：接通已有 plan_shot_match / ApplyShotMatchCommand
——选参考（真实测量）→ 逐镜头规划 → 勾选/排除（低置信需显式确认）→
一次事务提交（过期拒绝/零半写）。锁定/排除建议零写入。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytestmark = pytest.mark.edit_core

from src.features.color.ui.shot_match_panel import (
    DEFAULT_LOW_CONFIDENCE,
    ShotMatchPanel,
)


def _frame(value: float, size: int = 16) -> np.ndarray:
    return np.full((size, size, 3), value, dtype=np.float32)


def _measure_factory(values: dict[str, float]):
    def measure(clip_id: str) -> np.ndarray:
        return _frame(values[clip_id])
    return measure


def _project(timeline_id="tl1", revision=7):
    timeline = NS(
        timeline_id=timeline_id,
        video_tracks=[NS(clips=[
            NS(clip_id="shotA", color_stack_id="s-a"),
            NS(clip_id="shotB", color_stack_id="s-b"),
            NS(clip_id="shotC", color_stack_id="s-c"),
        ])],
    )
    stacks = {
        "s-a": NS(stack_id="s-a"),
        "s-b": NS(stack_id="s-b"),
        "s-c": NS(stack_id="s-c"),
    }
    return NS(
        revision=revision,
        timelines={timeline_id: timeline},
        color_stacks=stacks,
        color_stack_versions={},
    )


class TestShotMatchPanel:
    def test_low_confidence_requires_explicit_selection(self, qtbot: Any) -> None:
        from PyQt6.QtCore import Qt

        panel = ShotMatchPanel(
            _project(), "tl1", clip_ids=["ref", "shotA"],
            measure_fn=_measure_factory({"ref": 0.5, "shotA": 0.48}),
            reference_clip_id="ref", low_confidence=1.1)
        qtbot.addWidget(panel)
        panel._on_analyze()
        item = panel._shots_list.item(0)
        assert item.flags() & Qt.ItemFlag.ItemIsUserCheckable
        assert item.checkState() == Qt.CheckState.Unchecked
        panel._on_apply()
        assert panel.selection.included_clip_ids == ()

    def test_failed_reanalysis_discards_previous_suggestions(self, qtbot: Any) -> None:
        from PyQt6.QtWidgets import QDialogButtonBox

        panel = ShotMatchPanel(
            _project(), "tl1", clip_ids=["ref", "shotA"],
            measure_fn=_measure_factory({"ref": 0.5, "shotA": 0.48}),
            reference_clip_id="ref")
        qtbot.addWidget(panel)
        panel._on_analyze()
        assert panel._report is not None
        panel._measure_fn = None
        panel._on_analyze()
        assert panel._report is None
        assert panel._shots_list.count() == 0
        assert not panel._buttons.button(
            QDialogButtonBox.StandardButton.Ok).isEnabled()
        panel._on_apply()
        assert panel.applied_command is None

    def test_analyze_plans_and_lists_shots(self, qtbot):
        panel = ShotMatchPanel(
            _project(), "tl1",
            clip_ids=["ref", "shotA", "shotB", "shotC"],
            measure_fn=_measure_factory({
                "ref": 0.5, "shotA": 0.45, "shotB": 0.8, "shotC": 0.05}),
            reference_clip_id="ref")
        qtbot.addWidget(panel)
        panel._on_analyze()
        assert panel._report is not None
        assert panel._shots_list.count() == 3  # 参考不入列表
        # 状态含可应用计数
        assert "可应用" in panel._status.text()

    def test_apply_produces_command_with_checked_only(self, qtbot):
        panel = ShotMatchPanel(
            _project(), "tl1",
            clip_ids=["ref", "shotA", "shotB"],
            measure_fn=_measure_factory({"ref": 0.5, "shotA": 0.48,
                                         "shotB": 0.52}),
            reference_clip_id="ref")
        qtbot.addWidget(panel)
        panel._on_analyze()
        assert panel._report is not None
        # 取消勾选 shotB（人工排除）
        from PyQt6.QtCore import Qt
        for i in range(panel._shots_list.count()):
            item = panel._shots_list.item(i)
            sug = item.data(Qt.ItemDataRole.UserRole)
            if sug.clip_id == "shotB":
                item.setCheckState(Qt.CheckState.Unchecked)
        panel._on_apply()
        cmd = panel.applied_command
        assert cmd is not None
        applied_ids = {cid for cid, _ in cmd.suggestions}
        assert "shotB" not in applied_ids
        assert panel.selection is not None
        assert "shotB" in panel.selection.excluded_clip_ids

    def test_measure_failure_shown_not_crash(self, qtbot):
        def broken(clip_id):
            raise RuntimeError("no frame")
        panel = ShotMatchPanel(
            _project(), "tl1", clip_ids=["ref", "a"],
            measure_fn=broken, reference_clip_id="ref")
        qtbot.addWidget(panel)
        panel._on_analyze()
        assert panel._report is None
        assert "测量失败" in panel._status.text()

    def test_no_measure_fn_refuses_fake_measurement(self, qtbot):
        panel = ShotMatchPanel(
            _project(), "tl1", clip_ids=["ref", "a"],
            reference_clip_id="ref")
        qtbot.addWidget(panel)
        panel._on_analyze()
        assert panel._report is None  # 不用占位假测量


class TestControllerMeasureFn:
    """N10：逐镜头真实帧源 + fail-closed（旧同图假测量移除）。"""

    def test_per_clip_frame_source_used(self):
        import numpy as np
        from types import SimpleNamespace as NS
        from src.features.color.ui.shot_match_panel import controller_measure_fn

        seen = []

        def source(clip_id, frame):
            seen.append((clip_id, frame))
            return np.full((8, 8, 3), 0.5, np.float32)

        fn = controller_measure_fn(
            NS(), lambda cid: 12, frame_source=source)
        img = fn("shotA")
        assert img.shape == (8, 8, 3)
        assert seen == [("shotA", 12)]  # 逐镜头帧号真实传入

    def test_no_source_fails_closed_not_same_image(self):
        """无逐镜头源 → 显式失败（不再退回同一缓存图假测量）。"""
        from types import SimpleNamespace as NS
        from src.features.color.ui.shot_match_panel import controller_measure_fn
        import pytest as _pytest
        engine = NS(render_cached_image=lambda: object())  # 即使引擎可用
        fn = controller_measure_fn(engine, lambda cid: None)
        with _pytest.raises(ValueError, match="逐镜头帧源"):
            fn("shotA")

    def test_source_none_or_bad_shape_raises(self):
        from types import SimpleNamespace as NS
        from src.features.color.ui.shot_match_panel import controller_measure_fn
        import numpy as np
        import pytest as _pytest
        fn = controller_measure_fn(
            NS(), lambda cid: 0,
            frame_source=lambda cid, frame: None)
        with _pytest.raises(ValueError, match="取帧失败"):
            fn("shotA")
        fn2 = controller_measure_fn(
            NS(), lambda cid: 0,
            frame_source=lambda cid, frame: np.zeros((8, 8)))
        with _pytest.raises(ValueError, match="形态非法"):
            fn2("shotA")


class TestShotMatchFrameSourceWiring:
    """N10 接线：菜单回调构建的逐镜头解码源端到端（真实素材）。"""

    def test_frame_source_decodes_real_media(self, monkeypatch):
        import os
        from types import SimpleNamespace as NS
        import numpy as np
        from src.features.edit.core.project import EditProject, MediaAsset
        media = "tests/test_videos/2K_topos_422.mov"
        if not os.path.isfile(media):
            media = next(
                (m for m in sorted(os.listdir("tests/test_videos"))
                 if m.endswith((".mov", ".mp4"))), None)
            if media:
                media = f"tests/test_videos/{media}"
        if not media or not os.path.isfile(media):
            import pytest as _pytest
            _pytest.skip("tests/test_videos 真实素材不在库")

        import inspect
        from src.app.controllers.menu_toolbar_controller import (
            MenuToolbarMixin,
        )

        clip = NS(clip_id="c1", asset_id="a1", source_start=3)
        track = NS(clips=(clip,))
        timeline = NS(timeline_id="t1", video_tracks=(track,))
        project = EditProject(
            project_id="p", name="P",
            assets={"a1": MediaAsset("a1", media, "video", "m", {})})

        host = NS()
        for name, fn in inspect.getmembers(
                MenuToolbarMixin, predicate=inspect.isfunction):
            if name == "_build_shot_match_frame_source":
                setattr(host, name, fn.__get__(host))
        import numpy as np

        def _fake_manager():
            planes = (np.full((8, 8), 128, np.uint8),
                      np.full((4, 4), 128, np.uint8),
                      np.full((4, 4), 128, np.uint8))
            return NS(get_frame=lambda path, frame: (
                NS(planes=planes), "planar"))

        monkeypatch.setattr(
            "src.features.edit.core.edit_gpu_decode_manager."
            "EditGpuDecodeManager", _fake_manager)

        source, clip_frame_fn = host._build_shot_match_frame_source(
            project, timeline)
        assert source is not None
        assert clip_frame_fn("c1") == 3
        rgb = source("c1", 3)
        assert rgb.ndim == 3 and rgb.shape[2] == 3
        assert rgb.dtype == np.float32
        assert 0.0 <= float(rgb.min()) and float(rgb.max()) > 0.0  # 非全黑

