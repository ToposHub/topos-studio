"""w6-g4 回归测试：BUG-A4-06 / BUG-B1-03 / AUD-R2-004 / AUD-R2-006。

锁定本轮 4 处源侧/测试侧修复的行为（AUD-R2-008 由
tests/edit/test_plugin_identity.py 的修正断言锁定，不在此重复）：

- BUG-A4-06：EyeDropperOverlay 未成功 start() 时，点击/启动失败路径
  防御——不再 AttributeError，半初始化覆盖层按取消处理并保证不可见。
- BUG-B1-03：CompositeViewer 的 get_state/set_state 只允许定义一次
  （重复定义的前一份是死代码，修改会静默失效）。
- AUD-R2-004：暖偏素材 + 注入蓝偏（先灰世界归一）时 auto_balance 仍给
  出显著正 temp——真实素材为 gitignored 本地文件，此合成版不依赖素材。
- AUD-R2-006：磁盘缓存 HALF .toos 载荷对超界 HDR 值写入前饱和到 ±f16
  max，恢复侧不再出现 ±inf。
"""

from __future__ import annotations

import inspect

import numpy as np
import pytest

pytestmark = pytest.mark.app


# ── BUG-A4-06：EyeDropperOverlay 启动失败的交互防御 ────────────────────

class TestEyeDropperOverlayStartFailureDefence:
    def test_init_presets_full_screen_img_none(self, qapp):
        from src.shared.color.color_picker import EyeDropperOverlay

        overlay = EyeDropperOverlay()
        assert overlay.full_screen_img is None

    def test_failed_start_emits_cancelled_and_hides(self, qapp, monkeypatch):
        """primaryScreen 为 None → 只发 cancelled，覆盖层不可见。"""
        from PyQt6.QtWidgets import QApplication

        from src.shared.color.color_picker import EyeDropperOverlay

        monkeypatch.setattr(QApplication, "primaryScreen",
                            staticmethod(lambda: None))
        overlay = EyeDropperOverlay()
        cancelled = []
        overlay.cancelled.connect(lambda: cancelled.append(True))
        overlay.show()
        assert overlay.isVisible()  # 前置：调用方强行 show 过

        overlay.start()

        assert cancelled == [True]
        assert overlay.full_screen_img is None
        assert not overlay.isVisible()  # 失败路径主动 hide

    def test_null_screenshot_start_emits_cancelled(self, qapp, monkeypatch):
        """截图为空 → 同样只发 cancelled 且成员保持 None。"""
        from PyQt6.QtCore import QRect
        from PyQt6.QtGui import QPixmap
        from PyQt6.QtWidgets import QApplication

        from src.shared.color.color_picker import EyeDropperOverlay

        class _NullScreen:
            @staticmethod
            def grabWindow(_wid):
                return QPixmap()  # isNull() → True

            @staticmethod
            def geometry():
                return QRect(0, 0, 64, 64)

        monkeypatch.setattr(QApplication, "primaryScreen",
                            staticmethod(lambda: _NullScreen()))
        overlay = EyeDropperOverlay()
        cancelled = []
        overlay.cancelled.connect(lambda: cancelled.append(True))

        overlay.start()

        assert cancelled == [True]
        assert overlay.full_screen_img is None

    def test_click_after_failed_start_cancels_without_crash(self, qapp,
                                                            monkeypatch):
        """BUG-A4-06 核心：start() 失败后 mousePressEvent 不抛
        AttributeError，按取消语义处理。"""
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtWidgets import QApplication

        from src.shared.color.color_picker import EyeDropperOverlay

        monkeypatch.setattr(QApplication, "primaryScreen",
                            staticmethod(lambda: None))
        overlay = EyeDropperOverlay()
        overlay.start()  # 失败：full_screen_img 保持 None
        assert overlay.full_screen_img is None

        cancelled = []
        overlay.cancelled.connect(lambda: cancelled.append(True))
        event = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(5.0, 5.0), QPointF(5.0, 5.0),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        overlay.mousePressEvent(event)  # 修复前：AttributeError

        assert cancelled == [True]


# ── BUG-B1-03：get_state/set_state 不得重复定义 ────────────────────────

class TestCompositeViewerStateMethodsSingleDefinition:
    def test_get_and_set_state_defined_once(self):
        from src.features.composite.ui.composite_viewport import CompositeViewer

        source = inspect.getsource(CompositeViewer)
        assert source.count("def get_state") == 1, (
            "CompositeViewer.get_state 重复定义——前一份是死代码"
            "（BUG-B1-03 回归）"
        )
        assert source.count("def set_state") == 1, (
            "CompositeViewer.set_state 重复定义——前一份是死代码"
            "（BUG-B1-03 回归）"
        )

    def test_effective_get_state_carries_expanded_contract(self, qapp):
        """生效副本（后一份）包含扩展键，证明删除的是死代码而非生效版。"""
        from src.features.composite.ui.composite_viewport import CompositeViewer

        source = inspect.getsource(CompositeViewer.get_state)
        for key in ("resolution_mode", "magnifier_enabled",
                    "background_mode", "user_roi"):
            assert f'"{key}"' in source


# ── AUD-R2-004：暖偏素材 + 蓝偏注入（合成，不依赖 gitignored 素材）────

class TestAutoBalanceWarmSourceBlueInjection:
    def test_warm_source_normalized_then_blue_cast_positive_temp(self):
        from src.features.color.core.auto_color import FrameStats, auto_balance

        rng = np.random.default_rng(2026)
        img = rng.random((96, 128, 3))  # BGR
        img[:, :, 2] *= 1.26  # 素材自带暖偏（R 高），复现 4K ProRes 帧形态

        # 与修正后的真实素材测试同方法：先灰世界归一再注入 B×1.3
        cast = img
        means = cast.reshape(-1, 3).mean(axis=0)
        cast = cast * (float(means.mean()) / np.maximum(means, 1e-6))
        cast[:, :, 0] *= 1.3
        cast8 = (np.clip(cast, 0.0, 1.0) * 255.0).astype(np.uint8)

        s = auto_balance(FrameStats.from_image(cast8))
        assert s.temp > 5.0

    def test_warm_source_without_normalization_is_not_blue_dominant(self):
        """对照：不归一时 B×1.3 后 B≈R，灰世界 temp 不显著——证明旧测试
        的前提依赖素材中性，而非算法缺陷。"""
        from src.features.color.core.auto_color import FrameStats, auto_balance

        rng = np.random.default_rng(2026)
        img = rng.random((96, 128, 3))
        img[:, :, 2] *= 1.26
        img[:, :, 0] *= 1.3
        cast8 = (np.clip(img, 0.0, 1.0) * 255.0).astype(np.uint8)

        s = auto_balance(FrameStats.from_image(cast8))
        assert abs(s.temp) < 5.0  # B≈R：无蓝偏主导，小 temp 是正确解


# ── AUD-R2-006：HALF .toos 载荷 f16 饱和 ───────────────────────────────

class TestDiskCacheHalfPayloadF16Saturation:
    def test_store_round_trip_saturates_positive_overflow(self, tmp_path):
        from src.features.composite.render import disk_cache_store as dcs
        from src.features.composite.render.topos_cache_codec import (
            probe_topos_payload_codec,
        )

        if not probe_topos_payload_codec().available:
            pytest.skip("native TOPOS image codec unavailable")

        store = dcs.DiskCacheStore(root_dir=str(tmp_path), capacity_gb=1.0)
        arr = np.full((4, 4, 4), 1e5, dtype=np.float32)  # > f16 max
        assert store.store("big", arr) is True
        back = store.load("big")
        assert back is not None
        assert np.isfinite(back).all()
        assert back.flat[0] == pytest.approx(65504.0, rel=1e-3)

    def test_store_round_trip_saturates_negative_overflow(self, tmp_path):
        from src.features.composite.render import disk_cache_store as dcs
        from src.features.composite.render.topos_cache_codec import (
            probe_topos_payload_codec,
        )

        if not probe_topos_payload_codec().available:
            pytest.skip("native TOPOS image codec unavailable")

        store = dcs.DiskCacheStore(root_dir=str(tmp_path), capacity_gb=1.0)
        arr = np.full((4, 4, 4), -3e5, dtype=np.float32)  # < -f16 max
        assert store.store("neg", arr) is True
        back = store.load("neg")
        assert back is not None
        # RGB 通道饱和到 -65504；alpha 编码侧恒 [0,1]——全部有限
        assert np.isfinite(back).all()
        assert back.flat[0] == pytest.approx(-65504.0, rel=1e-3)
