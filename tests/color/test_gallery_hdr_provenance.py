"""G09 HDR／alpha／精度 provenance 测试。

门槛映射：meta.still_precision 只按 EXR 实际写成功记录（降级不标
HDR）；四通道参考 undeclared 拒绝、straight/premultiplied 对齐；超
1.0／负值不被 clamp；rename／export／import 后 alpha 声明与精度不丢。
"""
import json
import os
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.features.color.core.preset_meta import (
    PresetMeta,
    align_alpha_domain,
    load_preset_meta,
    load_still_best,
    save_preset_meta,
    still_alpha_declaration,
    write_still_exr,
    write_still_images,
)


def _img(h=16, w=24, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.random((h, w, 3)) * 255).astype(np.uint8)


def _hdr(h=16, w=24, alpha=False, seed=1):
    rng = np.random.default_rng(seed)
    channels = 4 if alpha else 3
    frame = rng.uniform(-0.5, 4.0, size=(h, w, channels)).astype(
        np.float32)     # 负值 + 超 1.0（场景线性值，禁止 clamp）
    if alpha:
        frame[..., 3] = rng.uniform(0.0, 1.0, size=(h, w)).astype(
            np.float32)
    return frame


class TestHdrValuesPreserved:
    def test_exr_roundtrip_keeps_super_white_and_negative(self, tmp_path):
        topos = str(tmp_path / "H.topos")
        frame = _hdr()
        assert write_still_exr(topos, frame) is not None
        loaded, precision = load_still_best(topos)
        assert precision == "exr_float32"
        assert loaded.dtype == np.float32
        np.testing.assert_allclose(loaded, frame, rtol=1e-5, atol=1e-6)
        assert loaded.max() > 1.0 and loaded.min() < 0.0   # 无 clamp

    def test_alpha_declaration_roundtrip(self, tmp_path):
        topos = str(tmp_path / "A.topos")
        write_still_exr(topos, _hdr(alpha=True),
                        alpha_semantics="straight")
        assert still_alpha_declaration(topos) == "straight"
        # 对齐到 premultiplied：alpha=0 像素 RGB 归零（straight 语义）
        img, _ = load_still_best(topos)
        aligned = align_alpha_domain(img, "straight", "premultiplied")
        zero_alpha = aligned[..., 3] == 0
        assert np.all(aligned[zero_alpha][..., :3] == 0)
        assert aligned.dtype == np.float32

    def test_undeclared_alpha_never_guessed(self, tmp_path):
        topos = str(tmp_path / "U.topos")
        write_still_exr(topos, _hdr(alpha=True))   # 声明写失败的场景
        os.remove(str(tmp_path / "U.still.exr.json"))
        assert still_alpha_declaration(topos) == "undeclared"
        img, _ = load_still_best(topos)
        with pytest.raises(ValueError):
            align_alpha_domain(img, "undeclared")


class TestCapturePrecisionHonesty:
    def _make_ctrl(self, tmp_path, monkeypatch, *, hdr_frame):
        from src.app.controllers.layer_controller import LayerOpsMixin
        from src.features.color.core.gallery_repository import (
            GalleryFolderRepository,
        )
        monkeypatch.chdir(tmp_path)
        ctrl = LayerOpsMixin.__new__(LayerOpsMixin)

        class _Engine:
            img_high = _img()
            img_full_original = _img(seed=2)

            def grab_viewport_image(self, *a, **k):
                return _img()

            def render_full_image_for_scope(self, max_dim):
                if isinstance(hdr_frame, Exception):
                    raise hdr_frame
                return hdr_frame

        ctrl.engine = _Engine()
        ctrl.status_bar = MagicMock()
        ctrl.current_file_is_video = False
        ctrl.save_router = MagicMock()

        def _fake_save(path, *_a, **_k):
            with open(path, "wb") as fh:
                fh.write(b"PK-fake")
            return True
        ctrl.save_router._save_single_asset_project.side_effect = _fake_save
        root = os.path.join(os.getcwd(), "Presets")
        os.makedirs(os.path.join(root, "User"), exist_ok=True)
        ctrl.presets_panel = MagicMock()
        ctrl.presets_panel.get_current_folder.return_value = os.path.join(
            root, "User")
        ctrl.presets_panel._repository = GalleryFolderRepository(root)
        ctrl.presets_panel.presets_root = root
        ctrl._page_context_manager = None
        ctrl._current_edit_project = None
        ctrl._get_composite_graph_data = lambda: None
        ctrl._get_composite_state_data = lambda: None
        return ctrl

    def test_successful_exr_records_float32(self, tmp_path, monkeypatch):
        ctrl = self._make_ctrl(tmp_path, monkeypatch,
                               hdr_frame=_hdr(seed=5))
        monkeypatch.setattr(
            "src.app.controllers.layer_controller.QMessageBox", MagicMock())
        ctrl.on_capture_still()
        folder = ctrl.presets_panel.get_current_folder()
        meta = load_preset_meta(os.path.join(
            folder, sorted(f for f in os.listdir(folder)
                           if f.endswith(".topos"))[0]))
        assert meta.still_precision == "float32"    # 实际写成功才标

    def test_exr_degrade_records_no_precision_and_no_stale_exr(
            self, tmp_path, monkeypatch):
        ctrl = self._make_ctrl(tmp_path, monkeypatch,
                               hdr_frame=RuntimeError("FBO readback fail"))
        monkeypatch.setattr(
            "src.app.controllers.layer_controller.QMessageBox", MagicMock())
        ctrl.on_capture_still()
        folder = ctrl.presets_panel.get_current_folder()
        names = os.listdir(folder)
        assert not any(n.endswith(".still.exr") for n in names)
        meta = load_preset_meta(os.path.join(
            folder, sorted(f for f in names if f.endswith(".topos"))[0]))
        assert meta.still_precision is None            # 不因尝试过标 HDR
        # 描述口径如实：8-bit 精度受限
        assert "8-bit" in meta.describe()


class TestReferenceAlphaContract:
    def _make_ctrl(self, tmp_path, monkeypatch):
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

    def _bundle(self, tmp_path, name, *, alpha, semantics, declared=True):
        topos = str(tmp_path / f"{name}.topos")
        with zipfile.ZipFile(topos, "w") as zf:
            zf.writestr("project.json", "{}")
        write_still_exr(topos, _hdr(alpha=alpha, seed=7),
                        alpha_semantics=semantics)
        if not declared:
            os.remove(topos.replace(".topos", ".still.exr") + ".json")
        return topos

    def test_undeclared_four_channel_reference_refused(self, tmp_path,
                                                       monkeypatch):
        ctrl = self._make_ctrl(tmp_path, monkeypatch)
        topos = self._bundle(tmp_path, "U", alpha=True,
                             semantics=None, declared=False)
        ctrl.on_preset_reference_requested(topos)
        assert ctrl._loaded == []          # 不猜语义、不加载
        messages = [str(c.args[0]) for c in
                    ctrl.status_bar.showMessage.call_args_list]
        assert any("alpha" in m.lower() for m in messages)

    def test_straight_reference_aligned_to_premultiplied(self, tmp_path,
                                                         monkeypatch):
        ctrl = self._make_ctrl(tmp_path, monkeypatch)
        topos = self._bundle(tmp_path, "S", alpha=True,
                             semantics="straight")
        ctrl.on_preset_reference_requested(topos)
        assert len(ctrl._loaded) == 1
        aligned = ctrl._loaded[0]
        zero_alpha = aligned[..., 3] == 0
        if zero_alpha.any():
            assert np.all(aligned[zero_alpha][..., :3] == 0)

    def test_three_channel_reference_unaffected(self, tmp_path, monkeypatch):
        ctrl = self._make_ctrl(tmp_path, monkeypatch)
        topos = self._bundle(tmp_path, "RGB", alpha=False,
                             semantics=None)
        ctrl.on_preset_reference_requested(topos)
        assert len(ctrl._loaded) == 1


class TestPrecisionSurvivesTransfer:
    def test_rename_and_export_import_keep_alpha_and_precision(
            self, tmp_path):
        from src.features.color.core.gallery_bundle_service import (
            GalleryBundleService,
        )
        from src.features.color.core.gallery_repository import (
            GalleryFolderRepository,
        )
        from src.features.color.core.gallery_transfer_service import (
            GalleryTransferService,
        )
        root = tmp_path / "Presets"
        user = root / "User"
        system = root / "System"
        user.mkdir(parents=True)
        system.mkdir()
        topos = str(user / "P.topos")
        with zipfile.ZipFile(topos, "w") as zf:
            zf.writestr("project.json", "{}")
        write_still_images(topos, _img(), thumb_only=True)
        write_still_exr(topos, _hdr(alpha=True),
                        alpha_semantics="premultiplied")
        save_preset_meta(topos, PresetMeta(still_id="S-keep00000000001",
                                           still_precision="float32",
                                           label="Keep"))

        bundles = GalleryBundleService(str(root))
        assert bundles.rename_bundle(str(user), "P", "Q").succeeded
        assert still_alpha_declaration(str(user / "Q.topos")) == \
            "premultiplied"
        assert load_preset_meta(str(user / "Q.topos")).still_precision == \
            "float32"

        transfer = GalleryTransferService(
            GalleryFolderRepository(str(root)), bundles)
        dest = str(root / "q.toposbundle")
        assert transfer.export_bundle(str(user), "Q", dest).succeeded
        result = transfer.import_bundle(dest, str(system))
        assert result.succeeded
        imported = result.affected_still_ids[0]
        assert still_alpha_declaration(
            str(system / f"{imported}.topos")) == "premultiplied"
        assert load_preset_meta(
            str(system / f"{imported}.topos")).still_precision == "float32"
