"""P1-12/P1-13 媒体池 UI 接线冒烟测试。

覆盖：
- AssetPropertiesDialog 日志字段初始化与 log_field_values() 解析
  （顿号/逗号分隔关键词、评级/旗标/颜色下拉取值）
- BinTreeWidget 展示用户自建 Smart Bin（bookmark 区分）
- MediaPoolPanel 选中用户 Smart Bin → get_smart_bin_assets 命中
- 面板命令执行路径：_on_create_smart_bin 走 ProjectCommandStack
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.edit_ui

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from features.edit.core.project import EditProject, MediaAsset, SmartBin
from features.edit.ui.media_pool_panel import (
    AssetPropertiesDialog,
    BinTreeWidget,
)


def _asset(asset_id: str = "a1", **overrides) -> MediaAsset:
    params = dict(
        asset_id=asset_id,
        path=f"/tmp/{asset_id}.mov",
        media_type="video",
        display_name=f"{asset_id}.mov",
        metadata={},
    )
    params.update(overrides)
    return MediaAsset(**params)


def _project() -> EditProject:
    return EditProject(
        project_id="p1",
        name="proj",
        assets={
            "a1": _asset(keywords=("interview",), rating=3),
            "a2": _asset("a2"),
        },
        root_bin_id="root",
    )


class TestPropertiesDialogLogFields:
    def test_dialog_reads_current_values(self, qapp) -> None:
        asset = _asset(
            keywords=("采访", "室内"), rating=4, flag="reject",
            color_label="orange", comment="备用",
        )
        dlg = AssetPropertiesDialog(asset)
        keywords, rating, flag, color, comment = dlg.log_field_values()
        assert keywords == ("采访", "室内")
        assert rating == 4
        assert flag == "reject"
        assert color == "orange"
        assert comment == "备用"

    def test_keywords_parse_comma_and_ideographic(self, qapp) -> None:
        dlg = AssetPropertiesDialog(_asset())
        dlg._keywords_edit.setText("访谈、户外, 夜景 ,")
        keywords, *_ = dlg.log_field_values()
        assert keywords == ("访谈", "户外", "夜景")

    def test_rating_combo_indices(self, qapp) -> None:
        dlg = AssetPropertiesDialog(_asset(rating=5))
        assert dlg._rating_combo.count() == 6
        _, rating, _, _, _ = dlg.log_field_values()
        assert rating == 5


class TestBinTreeUserSmartBins:
    def test_user_smart_bins_listed_with_role(self, qapp) -> None:
        project = _project().add_smart_bin(SmartBin(
            bin_id="sb-1", name="访谈精选",
            filter_type="custom", filter_params={"keywords": ["interview"]},
        ))
        tree = BinTreeWidget()
        tree.populate(project)
        match = Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchRecursive
        found = None
        for item in tree.findItems("访谈精选", match, 0):
            found = item
            break
        assert found is not None
        assert found.data(0, tree.BIN_TYPE_ROLE) == "smart_bin"
        assert bool(found.data(0, tree.BIN_USER_ROLE)) is True
        # 内置 smart bin 不带 user role
        builtin = None
        for item in tree.findItems("All Video", match, 0):
            builtin = item
            break
        assert builtin is not None
        assert not builtin.data(0, tree.BIN_USER_ROLE)

    def test_get_smart_bin_assets_resolves_user_bins(self, qapp) -> None:
        project = _project().add_smart_bin(SmartBin(
            bin_id="sb-1", name="访谈精选",
            filter_type="custom", filter_params={"keywords": ["interview"]},
        ))
        assets = project.get_smart_bin_assets("sb-1")
        assert [a.asset_id for a in assets] == ["a1"]


from PyQt6.QtCore import Qt  # noqa: E402  (placed late to keep sys.path shim first)


class TestBatchEditLog:
    """P1-12 收尾：多选批量标记（BatchUpdateAssetLogCommand 接线）。"""

    def test_dialog_defaults_keep_unchanged(self, qapp) -> None:
        from features.edit.ui.media_pool_panel import BatchAssetLogDialog

        dlg = BatchAssetLogDialog(3)
        assert dlg.batch_params() == {
            "add_keywords": (),
            "remove_keywords": (),
            "rating": None,
            "flag": None,
            "color_label": None,
            "comment": None,
        }

    def test_dialog_parses_keywords_and_selections(self, qapp) -> None:
        from features.edit.ui.media_pool_panel import BatchAssetLogDialog
        from features.edit.core.project import ASSET_COLOR_LABELS

        dlg = BatchAssetLogDialog(2)
        dlg._add_kw_edit.setText("夜景、户外, 精选")
        dlg._remove_kw_edit.setText("interview, ")
        dlg._rating_combo.setCurrentIndex(dlg._rating_combo.findData(4))
        dlg._flag_combo.setCurrentIndex(dlg._flag_combo.findData("reject"))
        color = ASSET_COLOR_LABELS[0]
        dlg._color_combo.setCurrentIndex(dlg._color_combo.findData(color))
        dlg._comment_edit.setText("批量备注")

        params = dlg.batch_params()
        assert params["add_keywords"] == ("夜景", "户外", "精选")
        assert params["remove_keywords"] == ("interview",)
        assert params["rating"] == 4
        assert params["flag"] == "reject"
        assert params["color_label"] == color
        assert params["comment"] == "批量备注"

    def test_handler_applies_and_is_undoable(self, qapp, monkeypatch) -> None:
        import features.edit.ui.media_pool_panel as mpp
        from features.edit.ui.media_pool_panel import MediaPoolPanel
        from PyQt6.QtWidgets import QDialog

        project = EditProject(
            project_id="p1",
            name="proj",
            assets={
                "a1": _asset(keywords=("interview", "raw"), rating=3),
                "a2": _asset("a2"),
                "a3": _asset("a3"),
            },
            root_bin_id="root",
        )
        panel = MediaPoolPanel()
        try:
            panel.set_project(project)
            emitted = []
            panel.project_changed.connect(lambda p: emitted.append(p))

            class _FakeDialog:
                def __init__(self, count, parent=None) -> None:
                    assert count == 2  # 只统计存在的素材

                def exec(self):
                    return QDialog.DialogCode.Accepted

                def batch_params(self):
                    return {
                        "add_keywords": ("精选",),
                        "remove_keywords": ("interview",),
                        "rating": 5,
                        "flag": "reject",
                        "color_label": None,
                        "comment": None,
                    }

            monkeypatch.setattr(mpp, "BatchAssetLogDialog", _FakeDialog)
            monkeypatch.setattr(mpp.QMessageBox, "information", lambda *a, **k: None)

            panel._on_batch_edit_log(["a1", "a2", "ghost-id"])
            p = panel._project
            # a1: interview 移除、精选追加；评级/旗标统一覆盖
            assert p.assets["a1"].keywords == ("raw", "精选")
            assert p.assets["a1"].rating == 5
            assert p.assets["a1"].flag == "reject"
            # a2: 无原关键词 → 只有新增
            assert p.assets["a2"].keywords == ("精选",)
            assert p.assets["a2"].rating == 5
            # a3 不在批处理范围
            assert p.assets["a3"].rating == 0
            assert p.assets["a3"].keywords == ()
            assert emitted, "project_changed 应发射"

            # 单条命令整体可撤销
            restored = panel._command_stack.undo(p)
            assert restored.assets["a1"].keywords == ("interview", "raw")
            assert restored.assets["a1"].rating == 3
            assert restored.assets["a2"].keywords == ()
            assert restored.assets["a2"].flag == "none"
        finally:
            panel.deleteLater()


class TestProxyPolicyUi:
    """P1-14: 代理策略对话框与面板接线。"""

    def test_dialog_roundtrip(self, qapp) -> None:
        # 注意：对话框内部经 src.* 导入构建返回值，测试必须用同一模块身份比较
        from features.edit.ui.media_pool_panel import ProxyPolicyDialog
        from src.features.edit.core.proxy_policy import ProxyPolicySettings

        policy = ProxyPolicySettings(
            mode="all", max_native_width=3840, max_native_height=2160,
            target_height=720, auto_generate=False, export_use_master=False,
        )
        dlg = ProxyPolicyDialog(policy)
        restored = dlg.policy_values()
        assert restored == policy

        default_dlg = ProxyPolicyDialog(ProxyPolicySettings())
        assert default_dlg.policy_values() == ProxyPolicySettings()

    def test_handler_executes_command_and_bridges_service(
        self, qapp, monkeypatch
    ) -> None:
        import features.edit.ui.media_pool_panel as mpp
        import src.shared.media.disk_proxy_service as dps
        from features.edit.ui.media_pool_panel import MediaPoolPanel
        from features.edit.core.proxy_policy import ProxyPolicySettings
        from PyQt6.QtWidgets import QDialog

        class _FakeDialog:
            def __init__(self, policy, parent=None) -> None:
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            def policy_values(self):
                return ProxyPolicySettings(mode="all", target_height=720)

        monkeypatch.setattr(mpp, "ProxyPolicyDialog", _FakeDialog)

        update_calls = []

        class _FakeService:
            _config = None

            def update_config(self, config):
                update_calls.append(config)

        monkeypatch.setattr(dps, "get_disk_proxy_service", lambda: _FakeService())

        panel = MediaPoolPanel()
        try:
            panel.set_project(_project())
            emitted = []
            panel.project_changed.connect(lambda p: emitted.append(p))
            panel._on_proxy_policy_settings()

            policy = panel._project.get_proxy_policy()
            assert policy.mode == "all"
            assert policy.target_height == 720
            assert emitted, "project_changed 应发射"
            assert update_calls and update_calls[0].target_height == 720

            # 命令可撤销 → 恢复默认策略
            restored = panel._command_stack.undo(panel._project)
            assert restored.proxy_policy is None
            assert restored.get_proxy_policy().mode == "auto"
        finally:
            panel.deleteLater()


class TestConsolidateUi:
    """P1-13: 整理工程（按剪辑范围）对话框与面板接线。"""

    def test_dialog_defaults_and_roundtrip(self, qapp) -> None:
        from features.edit.ui.media_pool_panel import ConsolidateOptionsDialog

        dlg = ConsolidateOptionsDialog()
        assert dlg.values() == ("remux", 12)
        dlg._mode_combo.setCurrentIndex(1)
        dlg._handles_spin.setValue(30)
        assert dlg.values() == ("transcode", 30)

    def test_handler_no_clips_shows_info(self, qapp, monkeypatch) -> None:
        import features.edit.ui.media_pool_panel as mpp
        from features.edit.ui.media_pool_panel import MediaPoolPanel

        shown = []
        monkeypatch.setattr(
            mpp.QMessageBox, "information", lambda *a, **k: shown.append(a)
        )
        panel = MediaPoolPanel()
        try:
            panel.set_project(_project())  # 无时间线 → 无 clip
            panel._on_consolidate_project()
            assert shown, "无剪辑时应提示无需整理"
        finally:
            panel.deleteLater()

    def test_handler_full_flow_remux(self, qapp, monkeypatch, tmp_path) -> None:
        import shutil as _shutil

        if _shutil.which("ffmpeg") is None:
            pytest.skip("ffmpeg CLI not on PATH")

        from pathlib import Path

        import features.edit.ui.media_pool_panel as mpp
        from features.edit.ui.media_pool_panel import MediaPoolPanel
        from features.edit.core.project import ProjectBin, TimelineSequenceRef
        from features.edit.core.timeline_v2 import ClipInstance, EditTrack, Rational, TimelineSequence
        from PyQt6.QtWidgets import QDialog

        repo = Path(__file__).resolve().parents[2]
        real_video = str(repo / "tests" / "test_videos" / "2K_h264.mp4")
        asset = MediaAsset(
            asset_id="v1", path=real_video, media_type="video",
            display_name="2K_h264.mp4",
            metadata={"fps": 25.0, "width": 2048, "height": 1080},
        )
        project = EditProject(
            project_id="p1", name="ConsolidateUi",
            assets={"v1": asset},
            bins={"root": ProjectBin(
                bin_id="root", name="Root", parent_id=None,
                asset_ids=("v1",),
            )},
            root_bin_id="root",
            timelines={"t1": TimelineSequenceRef(
                timeline_id="t1", name="Main", fps=25.0,
            )},
        )
        seq = TimelineSequence(
            timeline_id="t1", name="Main", fps=Rational(25, 1),
            video_tracks=(EditTrack(
                track_id="v1t", name="V1", track_type="video",
                clips=(ClipInstance(
                    clip_id="c1", asset_id="v1", track_id="v1t",
                    timeline_start=0, timeline_duration=50,
                    source_start=50, source_duration=50,
                ),),
            ),),
        )

        class _FakeOptions:
            def __init__(self, parent=None) -> None:
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            def values(self):
                return ("remux", 0)

        out_dir = tmp_path / "archive"
        out_dir.mkdir()
        monkeypatch.setattr(mpp, "ConsolidateOptionsDialog", _FakeOptions)
        monkeypatch.setattr(
            mpp.QFileDialog, "getExistingDirectory",
            staticmethod(lambda *a, **k: str(out_dir)),
        )
        shown = []
        monkeypatch.setattr(
            mpp.QMessageBox, "information", lambda *a, **k: shown.append(a)
        )

        panel = MediaPoolPanel()
        try:
            panel.set_project(project)
            # 控制器回填：把序列交给 _collect_timeline_sequences
            panel.timeline_command_snapshot_requested.connect(
                lambda ids, update: update({"t1": seq})
            )
            panel._on_consolidate_project()

            assert shown, "完成后应弹出整理报告"
            topos = out_dir / "ConsolidateUi.topos"
            assert topos.exists(), "归档工程包应写入目标目录"
            assert list(out_dir.glob("media/2K_h264_c1.mp4")), "分段媒体应生成"
        finally:
            panel.deleteLater()
