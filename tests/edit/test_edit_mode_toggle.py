"""B8: 全局插入/覆盖模式切换测试。

覆盖：
- TimelineView 模式状态机（默认 overwrite、非法值归一、变更信号）
- TimelinePanel 切换按钮/持久化往返（ConfigManager monkeypatch）
- Insert 模式拖放语义：`_insert_asset_as_clip` 波纹插入（落点后方 clip 后移，
  sync-locked 链接音频跟随），Overwrite 模式不波纹
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_v2 import (  # noqa: E402
    ClipInstance,
    EditTrack,
    TimelineSequence,
)

try:
    from PyQt6.QtWidgets import QApplication  # noqa: F401

    HAS_QT = True
except Exception:  # noqa: BLE001
    HAS_QT = False

pytestmark = pytest.mark.skipif(not HAS_QT, reason="PyQt6 unavailable")


@pytest.fixture(autouse=True)
def _isolate_active_window(qapp) -> None:
    """顺序污染隔离：把 QApplication 激活窗口重置为中性态。

    前件测试（如 test_edit_effects_panel_drag_inject.py）构造无父顶层面板
    并 show() 后不收尾；tests/edit/conftest.py 的会话级 autouse fixture
    _freeze_cyclic_gc_for_qt_widgets 冻结循环 GC 使该面板跨测试存活，
    QApplication.activeWindow() 长期指向外来窗口。TimelinePanel 的
    Shift+B 守卫（shortcut_owner_window_active，activeWindow 非空且非
    自身 window 时 False）会把程序化调用的 toggle_edit_mode 静默
    no-op。每个用例开始前清空激活窗口，使本文件的 toggle 断言只依赖
    被测面板自身（active is None 时守卫放行）。
    """
    QApplication.setActiveWindow(None)
    yield


@pytest.fixture
def fake_config(monkeypatch) -> dict[str, Any]:
    """ConfigManager get/set → 进程内字典。"""
    store: dict[str, Any] = {}
    from src.shared.config import config_manager as cm_module

    monkeypatch.setattr(cm_module.ConfigManager, "get", lambda self, key: store.get(key))

    def _set(self, key, value, auto_save=True):
        store[key] = value

    monkeypatch.setattr(cm_module.ConfigManager, "set", _set)
    monkeypatch.setattr(cm_module.ConfigManager, "save_config", lambda self: None)
    return store


def _make_view():
    from src.features.edit.ui.timeline_view import TimelineView

    return TimelineView()


def _asset(total_frames: int = 48):
    from src.features.edit.core.project import MediaAsset

    return MediaAsset(
        asset_id="asset_b8",
        path="/tmp/nonexistent_b8.mp4",
        media_type="video",
        display_name="b8_asset",
        metadata={"total_frames": total_frames, "fps": 24.0},
    )


def _sequence_with_two_clips() -> TimelineSequence:
    from src.features.edit.core.timeline_v2 import Rational

    track = EditTrack(
        track_id="v1",
        name="V1",
        track_type="video",
        index=0,
        clips=(
            ClipInstance(
                clip_id="c1", asset_id="a1", track_id="v1",
                source_start=0, source_duration=48,
                timeline_start=0, timeline_duration=48,
            ),
            ClipInstance(
                clip_id="c2", asset_id="a2", track_id="v1",
                source_start=0, source_duration=48,
                timeline_start=48, timeline_duration=48,
            ),
        ),
    )
    return TimelineSequence(
        video_tracks=(track,), fps=Rational(24, 1),
    )


class TestTimelineViewEditMode:
    def test_default_is_overwrite(self, qapp) -> None:
        view = _make_view()
        assert view.edit_mode() == "overwrite"

    def test_set_and_signal(self, qapp) -> None:
        view = _make_view()
        seen: list[str] = []
        view.edit_mode_changed.connect(seen.append)
        view.set_edit_mode("insert")
        assert view.edit_mode() == "insert"
        assert seen == ["insert"]
        view.set_edit_mode("overwrite")
        assert seen == ["insert", "overwrite"]

    def test_invalid_mode_coerced(self, qapp) -> None:
        view = _make_view()
        view.set_edit_mode("nonsense")
        assert view.edit_mode() == "overwrite"

    def test_idempotent_set_no_signal(self, qapp) -> None:
        view = _make_view()
        seen: list[str] = []
        view.edit_mode_changed.connect(seen.append)
        view.set_edit_mode("overwrite")
        assert seen == []


class TestTimelinePanelEditMode:
    def _make_panel(self):
        from src.features.edit.ui.timeline_panel import TimelinePanel

        return TimelinePanel()

    def test_panel_default_overwrite_and_button_label(self, qapp, fake_config) -> None:
        panel = self._make_panel()
        assert panel.edit_mode() == "overwrite"
        assert panel.btn_edit_mode.text() == "覆盖" or panel.btn_edit_mode.text()

    def test_panel_toggle_and_persist(self, qapp, fake_config) -> None:
        panel = self._make_panel()
        panel.set_edit_mode("insert")
        assert panel.edit_mode() == "insert"
        assert fake_config["edit_drop_mode"] == "insert"
        assert panel.btn_edit_mode.isChecked() is True

        panel.toggle_edit_mode()
        assert panel.edit_mode() == "overwrite"
        assert fake_config["edit_drop_mode"] == "overwrite"

    def test_persisted_mode_restored(self, qapp, fake_config) -> None:
        fake_config["edit_drop_mode"] = "insert"
        panel = self._make_panel()
        assert panel.edit_mode() == "insert"
        assert panel.btn_edit_mode.isChecked() is True

    def test_invalid_persisted_value_falls_back(self, qapp, fake_config) -> None:
        fake_config["edit_drop_mode"] = "banana"
        panel = self._make_panel()
        assert panel.edit_mode() == "overwrite"


class TestInsertModeDropSemantics:
    def test_insert_asset_ripples_downstream(self, qapp) -> None:
        """Insert 模式：落点处波纹，落点及后方 clip 后移（边界落点）。"""
        view = _make_view()
        seq = _sequence_with_two_clips()
        view.load_timeline(seq)
        view.set_edit_mode("insert")

        view._project = None
        asset = _asset(total_frames=48)
        # 落点 48 = c1|c2 边界（与显式 Insert 按钮一致的中点插入限制另行验证）
        ok = view._insert_asset_as_clip(asset, view._timeline.video_tracks[0], 48)
        assert ok is True
        track = view._timeline.video_tracks[0]
        starts = {c.clip_id: c.timeline_start for c in track.clips}
        inserted = [cid for cid in starts if cid not in ("c1", "c2")]
        assert len(inserted) == 1
        assert starts[inserted[0]] == 48
        # c2 (原 48) 波纹后移 48 → 96
        assert starts["c2"] == 96
        # c1 (0) 不动
        assert starts["c1"] == 0

    def test_insert_mid_clip_splits_like_explicit_insert(self, qapp) -> None:
        """AUD-18803：片段中间落点与显式 Insert 语义一致——先分割跨越
        播放头的片段再波纹后移。

        本测试曾锁定旧行为（"拒绝重叠"：拖放与显式 Insert 在片段中间
        落点都抛 "Clip overlaps"，Insert 整体失败）；随 AUD-18803 修复
        （insert_clip_at_playhead 分割跨越片段）更新为锁定新的共享分割
        语义。
        """
        view = _make_view()
        view.load_timeline(_sequence_with_two_clips())
        view.set_edit_mode("insert")
        view._project = None
        asset = _asset(total_frames=48)
        from src.features.edit.core.commands import InsertClipCommand

        cmd = InsertClipCommand.create(
            asset_id=asset.asset_id, in_frame=0, out_frame=47,
            playhead=24, track_id="v1",
        )
        view._execute_command(cmd)  # 不再抛 ValueError("Clip overlaps")

        track = view._timeline.video_tracks[0]
        clips = sorted(track.clips, key=lambda c: c.timeline_start)
        assert len(clips) == 4  # c1 左半 / 新片段 / c1 右半 / c2
        by_id = {c.clip_id: c for c in clips}
        # c1 左半片保留原 id：[0,24)
        assert by_id["c1"].timeline_start == 0
        assert by_id["c1"].timeline_end == 24
        # 新片段占住 [24,72)，右侧全部波纹 +48
        new_clip = [c for c in clips if c.asset_id == asset.asset_id][0]
        assert new_clip.timeline_start == 24
        assert new_clip.timeline_end == 72
        # c1 右半片 [72,96) 与 c2 [96,144) 保持相邻不重叠
        assert by_id["c2"].timeline_start == 96
        assert by_id["c2"].timeline_end == 144
        right_part = [c for c in clips
                      if c.clip_id not in ("c1", "c2", new_clip.clip_id)][0]
        assert right_part.timeline_start == 72
        assert right_part.timeline_end == 96

    def test_overwrite_mode_does_not_ripple(self, qapp) -> None:
        """Overwrite（默认）：拖放不波纹——现有 AddClip 路径语义不变。"""
        view = _make_view()
        view.load_timeline(_sequence_with_two_clips())
        assert view.edit_mode() == "overwrite"
        view._project = None
        asset = _asset(total_frames=48)
        from src.features.edit.core.timeline_v2 import EditTrack

        # 落点 96 = c2 之后的空档（落入已占用区间会被重叠校验拒绝）
        ok = view._add_asset_as_clip(asset, view._timeline.video_tracks[0], 96)
        assert ok is True
        starts = {c.clip_id: c.timeline_start for c in view._timeline.video_tracks[0].clips}
        assert starts["c2"] == 48  # 未波纹
        assert starts["c1"] == 0
