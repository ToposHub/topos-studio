"""G13（可离屏子集）：wipe 参考会话 provenance ＋ split 协议如实回填。

分屏 presenter（真实渲染队列消费者）未实现——不冒充。本文件覆盖：
- wipe 会话记录 still 身份／bundle revision／实际精度；
- 换参考＝换会话（旧会话释放）；加载失败保留上一张有效会话；
- 清除参考结束会话；快照无会话时如实为空；
- split_requested 显式拒绝且不改工程状态；面板信号协议闭环。
"""
import os
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np

from src.features.color.core.preset_meta import (
    PresetMeta,
    save_preset_meta,
    write_still_exr,
    write_still_images,
)


def _make_ctrl():
    from src.app.controllers.layer_controller import LayerOpsMixin
    ctrl = LayerOpsMixin.__new__(LayerOpsMixin)
    loaded = []

    class _Engine:
        def load_reference_still(self, img):
            loaded.append(img)
            return img is not None

    ctrl.engine = _Engine()
    ctrl.status_bar = MagicMock()
    ctrl.canvas = SimpleNamespace()
    ctrl._loaded = loaded
    return ctrl


def _bundle(tmp_path, name, *, hdr=False, still_id="S-comp00000000001"):
    topos = str(tmp_path / f"{name}.topos")
    with zipfile.ZipFile(topos, "w") as zf:
        zf.writestr("project.json", "{}")
    rng = np.random.default_rng(abs(hash(name)) % 1000)
    image = (rng.random((12, 16, 3)) * 255).astype(np.uint8)
    write_still_images(topos, image)
    if hdr:
        write_still_exr(topos, rng.uniform(-0.2, 2.0,
                                           size=(12, 16, 3)).astype("float32"))
    save_preset_meta(topos, PresetMeta(
        still_id=still_id, label=f"Label {name}", clip_id="c1",
        source_frame=77))
    return topos


class TestWipeSessionProvenance:
    def test_reference_load_begins_session_with_identity(self, tmp_path):
        ctrl = _make_ctrl()
        topos = _bundle(tmp_path, "Ref", hdr=True)
        ctrl.on_preset_reference_requested(topos)
        snapshot = ctrl.gallery_compare_snapshot()
        assert snapshot["active"] is True
        assert snapshot["layout"] == "single"
        slot = snapshot["slots"][0]
        assert slot["item_id"] == "S-comp00000000001"
        assert slot["label"] == "Label Ref"
        assert slot["kind"] == "gallery_still"
        assert slot["fidelity"] == "hdr_exr"          # 实际加载精度
        # BR06：revision 是 bundle 全成员内容哈希（sidecar 原地改写也变）
        from src.features.color.core.gallery_bundle_service import (
            GalleryBundleService,
        )
        revision_hash = GalleryBundleService.bundle_revision(
            os.path.dirname(str(topos)), "Ref")
        assert slot["revision"] == int(revision_hash.split(":")[1][:8], 16)

    def test_jpeg_reference_records_limited_fidelity(self, tmp_path):
        ctrl = _make_ctrl()
        topos = _bundle(tmp_path, "Jpeg")
        ctrl.on_preset_reference_requested(topos)
        assert ctrl.gallery_compare_snapshot()["slots"][0][
            "fidelity"] == "uint8_still"

    def test_switching_reference_releases_previous_session(self, tmp_path):
        ctrl = _make_ctrl()
        first = _bundle(tmp_path, "First", still_id="S-first00000000001")
        second = _bundle(tmp_path, "Second", still_id="S-second0000000001")
        ctrl.on_preset_reference_requested(first)
        ctrl.on_preset_reference_requested(second)
        snapshot = ctrl.gallery_compare_snapshot()
        assert snapshot["slots"][0]["item_id"] == "S-second0000000001"

    def test_failed_load_keeps_previous_session(self, tmp_path):
        ctrl = _make_ctrl()
        good = _bundle(tmp_path, "Good", still_id="S-good000000000001")
        ctrl.on_preset_reference_requested(good)
        ctrl.engine.load_reference_still = lambda img: False   # 加载失败
        ctrl.on_preset_reference_requested(_bundle(tmp_path, "Bad"))
        assert ctrl.gallery_compare_snapshot()["slots"][0][
            "item_id"] == "S-good000000000001"

    def test_clear_reference_ends_session(self, tmp_path):
        ctrl = _make_ctrl()
        ctrl.on_preset_reference_requested(_bundle(tmp_path, "X"))
        assert ctrl.gallery_compare_snapshot()["active"] is True
        ctrl.end_gallery_compare_session()
        assert ctrl.gallery_compare_snapshot() == {"active": False}
        ctrl.end_gallery_compare_session()      # 幂等

    def test_no_session_snapshot_is_honestly_empty(self):
        assert _make_ctrl().gallery_compare_snapshot() == {"active": False}


class TestSplitProtocolClosure:
    def test_split_requested_rejected_with_reason(self):
        ctrl = _make_ctrl()
        snapshot_before = ctrl.gallery_compare_snapshot()
        ctrl.on_split_compare_requested({"layout": "grid_2x2"})
        messages = [str(c.args[0]) for c in
                    ctrl.status_bar.showMessage.call_args_list]
        assert any("未实现" in m for m in messages)
        assert ctrl.gallery_compare_snapshot() == snapshot_before

    def test_panel_signal_and_dialog_forwarding_exist(self, tmp_path,
                                                      monkeypatch, qtbot):
        monkeypatch.chdir(tmp_path)
        from src.features.color.ui import presets_panel as module
        monkeypatch.setattr(
            module, "_resolve_presets_root",
            lambda: str(tmp_path / "Presets"))
        panel = module.PresetsPanel()
        qtbot.addWidget(panel)
        assert hasattr(panel, "split_comparison_requested")

        captured = []
        panel.split_comparison_requested.connect(captured.append)

        class _Dialog:
            def __init__(self, *args, **kwargs) -> None:
                pass

            wipe_requested = SimpleNamespace(connect=lambda *a: None)
            split_requested = SimpleNamespace(
                connect=lambda slot: slot({"layout": "side_by_side"}))

            def exec(self):
                return 0

        import src.features.color.ui.gallery_compare as compare_module
        monkeypatch.setattr(compare_module, "GalleryCompareDialog", _Dialog)
        panel.open_comparison("split")
        assert captured == [{"layout": "side_by_side"}]
