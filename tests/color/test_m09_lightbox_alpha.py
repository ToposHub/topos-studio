"""M09.3 · 四通道 alpha 声明 + Lightbox 比较对话框回归。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytestmark = pytest.mark.color_core


def _rgba_ref(h=8, w=8):
    """straight 语义四通道参考：左半不透明纯红、右半 alpha 0.5。"""
    img = np.zeros((h, w, 4), np.float32)
    img[..., 0] = 1.0                       # B 通道 0；G 0；R=1
    img[..., 2] = 1.0
    img[..., 3] = 1.0
    img[:, w // 2:, 3] = 0.5
    return img


class TestAlphaSemanticsModel:
    def test_declaration_roundtrip(self, tmp_path):
        from src.features.color.core.preset_meta import (
            alpha_semantics_for,
            still_alpha_declaration,
            write_still_exr,
        )
        img = _rgba_ref()
        assert alpha_semantics_for(img) == "straight"
        assert alpha_semantics_for(img[..., :3]) == "none"
        topos = str(tmp_path / "p.topos")
        path = write_still_exr(topos, img)
        assert path and os.path.isfile(path)
        assert still_alpha_declaration(topos) == "straight"
        # 显式覆盖声明同样落盘
        write_still_exr(topos, img, alpha_semantics="premultiplied")
        assert still_alpha_declaration(topos) == "premultiplied"

    def test_missing_declaration_is_undeclared(self, tmp_path):
        from src.features.color.core.preset_meta import (
            still_alpha_declaration,
        )
        assert still_alpha_declaration(str(tmp_path / "none.topos")) \
            == "undeclared"

    def test_align_domain_conversions(self):
        from src.features.color.core.preset_meta import (
            align_alpha_domain,
            premultiply_rgba,
            unpremultiply_rgba,
        )
        img = _rgba_ref()
        pre = align_alpha_domain(img, "straight", "premultiplied")
        # 右半（alpha 0.5）RGB 减半；左半不变
        assert pre[0, 2, 2] == pytest.approx(1.0)
        assert pre[0, 6, 2] == pytest.approx(0.5)
        back = unpremultiply_rgba(pre)
        assert back[..., :3] == pytest.approx(img[..., :3], abs=1e-6)
        # undeclared → 拒绝（不猜）
        with pytest.raises(ValueError):
            align_alpha_domain(img, "undeclared", "premultiplied")

    def test_premultiply_zero_alpha_zeros_rgb(self):
        from src.features.color.core.preset_meta import premultiply_rgba
        img = np.array([[[0.7, 0.8, 0.9, 0.0]]], np.float32)
        out = premultiply_rgba(img)
        assert out[0, 0, 0] == 0.0 and out[0, 0, 3] == 0.0


class TestStillLightboxDialog:
    def test_badges_show_precision_and_semantics(self, qtbot):
        from src.features.color.ui.still_lightbox import StillLightboxDialog
        dlg = StillLightboxDialog(
            _rgba_ref(), _rgba_ref(),
            ref_precision="exr_float32", cur_precision="jpeg8",
            ref_alpha="straight", cur_alpha="undeclared")
        qtbot.addWidget(dlg)
        text = dlg._badges.text()
        assert "exr_float32" in text and "jpeg8" in text
        assert "straight" in text and "undeclared" in text

    def test_difference_requires_declared_alpha(self, qtbot):
        """undeclared 四通道 → 差异拒绝并明示（不猜语义）。"""
        from src.features.color.ui.still_lightbox import StillLightboxDialog
        dlg = StillLightboxDialog(
            _rgba_ref(), _rgba_ref(),
            ref_alpha="straight", cur_alpha="undeclared")
        qtbot.addWidget(dlg)
        dlg._on_toggle_difference()
        assert dlg._difference_mode is False
        assert "未声明" in dlg._status.text()

    def test_declared_difference_enters_mode(self, qtbot):
        from src.features.color.ui.still_lightbox import StillLightboxDialog
        dlg = StillLightboxDialog(
            _rgba_ref(), _rgba_ref(),
            ref_alpha="straight", cur_alpha="straight")
        qtbot.addWidget(dlg)
        dlg._on_toggle_difference()
        assert dlg._difference_mode is True
        assert "差异视图" in dlg._status.text()

    def test_difference_display_values(self):
        from src.features.color.ui.still_lightbox import difference_display
        a = np.zeros((2, 2, 3), np.float32)
        b = np.full((2, 2, 3), 0.2, np.float32)
        diff = difference_display(a, b)
        # |0-0.2|*4 = 0.8（放大 4×显示域）
        assert diff[0, 0, 0] == pytest.approx(0.8)
        assert difference_display(a, None) is None
        assert difference_display(a, np.zeros((3, 3, 3), np.float32)) is None
