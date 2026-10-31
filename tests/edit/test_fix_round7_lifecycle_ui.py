"""第七轮审计 0C 回归：实时链与 UI 生命周期（R08/R09/R20/R23）。

R08：实时 clip FX 动画能从 inactive 激活——saturation 静态 drive=0
     （is_active=False）+ 关键帧 drive=100 时，采样器必须先于 active
     判断执行（旧实现永远不采样，动画实时听不到）。
R09：响度分析取消从 GUI 线程直接置位（不等 worker 线程排队槽），
     busy 中的 worker 在下一检查点退出。
R20：DeliveryCapability 输出形态事实——GIF 容器=gif、PNG/TIFF 序列=
     sequence（非 mov），PNG/TIFF 序列支持 alpha 直通。
R23：效果面板响应式 section 的宿主 QTreeWidgetItem 被树重建销毁后，
     延迟 resize 不再抛 wrapped C/C++ object deleted（目标守卫自解绑）。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.keyframe import Keyframe, KeyframeTrack

pytestmark = pytest.mark.edit_core

SR = 48000


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([sys.argv])
    yield app


# ── R08：实时 FX 动画从 inactive 激活 ──


class _DCDecoder:
    """恒定 0.5 DC 假 decoder（明确非真实素材）。"""

    def __init__(self):
        self._timeline_start = 0
        self._timeline_duration = 10**6
        self._sample_rate = SR
        self._output_sample_rate = SR
        self._clip_id = "clip-1"
        self._speed = 1.0
        self._gain_db = 0.0
        self._fade = None

    def decode_samples_for_frame(self, frame, fps, num_samples):
        sig = np.full(num_samples, 0.5, dtype=np.float32)
        return np.stack([sig, sig])


class TestR08AnimatedFxActivatesFromInactive:
    def test_sampler_runs_when_static_chain_inactive(self, qapp):
        """saturation 静态 drive=0（is_active=False）+ 关键帧 drive=100：
        mix_samples 采样器被调用且输出被饱和处理（≠0.5 直通）。"""
        from src.features.edit.core.audio_playback import AudioMixer
        from src.features.edit.core.audio_retime import animated_fx_sampler
        from src.features.edit.core.effect_instance import (
            AnimatableParam,
            EffectInstance,
        )

        fx = EffectInstance(
            effect_type="saturation",
            params={"drive": 0.0, "enabled": True},
            animated_params={
                "drive": AnimatableParam(
                    base_value=0.0,
                    keyframes=KeyframeTrack(
                        param_name="drive",
                        keyframes=[Keyframe(0, 100.0)],
                        min_val=0.0, max_val=100.0),
                    min_value=0.0, max_value=100.0),
            },
        )
        assert fx.get_param_at_frame("drive", 0) == pytest.approx(100.0)
        sampler_calls = []
        base_sampler = animated_fx_sampler((fx,))

        def counting_sampler(frame):
            sampler_calls.append(frame)
            return base_sampler(frame)

        mixer = AudioMixer()
        mixer.add_track("t1", _DCDecoder())
        mixer.set_clip_effects(
            "clip-1", [("saturation", {"drive": 0.0, "enabled": True}, True)],
            track_id="t1", animated_sampler=counting_sampler)
        result = mixer.mix_samples(0, 25.0, 1920)
        out = result[0] if isinstance(result, tuple) else result

        assert sampler_calls, "静态 inactive 的链也必须执行动画采样（R08）"
        peak = float(np.max(np.abs(out)))
        assert peak != pytest.approx(0.5, abs=0.02), (
            f"drive=100 关键帧应饱和 DC 0.5（实测 {peak:.4f}——仍为直通，"
            "动画被 is_active 判断跳过）")

    def test_static_inactive_chain_without_sampler_unchanged(self, qapp):
        """无动画的静态 inactive 链保持直通（不进采样路径）。"""
        from src.features.edit.core.audio_playback import AudioMixer
        from src.features.edit.core.effect_instance import EffectInstance

        fx = EffectInstance(
            effect_type="saturation", params={"drive": 0.0, "enabled": True})
        mixer = AudioMixer()
        mixer.add_track("t1", _DCDecoder())
        mixer.set_clip_effects(
            "clip-1", [("saturation", {"drive": 0.0, "enabled": True}, True)],
            track_id="t1")
        result = mixer.mix_samples(0, 25.0, 1920)
        out = result[0] if isinstance(result, tuple) else result
        peak = float(np.max(np.abs(out)))
        assert peak == pytest.approx(0.5, abs=0.01)


# ── R09：取消即时置位 ──


class TestR09CancelImmediate:
    def test_cancel_sets_flag_while_worker_busy(self, qapp):
        """worker 忙于 run 的解码循环时，GUI 线程直接调用 cancel() 立即
        置位（旧 wiring 连接 worker.cancel 是跨线程排队调用，要等 run
        结束才执行）。"""
        from PyQt6.QtCore import QThread

        from src.features.edit.ui.timeline_view import _NormalizeGainWorker

        worker = _NormalizeGainWorker("/unused.wav")
        entered = threading.Event()

        def slow_decode(cancel_check=None):
            entered.set()
            while cancel_check is None or not cancel_check():
                time.sleep(0.01)
            return None, SR

        worker._decode_source = slow_decode
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        done = threading.Event()
        worker.finished.connect(lambda *a: done.set())
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        thread.start()
        try:
            assert entered.wait(3), "worker 未进入解码循环"
            # 模拟新 wiring 的 GUI 线程直接置位（lambda 无线程亲和）
            worker.cancel()
            assert worker._cancelled is True, "取消必须立即置位"
            # finished→lambda 为排队连接，主线程事件循环分发
            deadline = time.monotonic() + 3
            while not done.is_set() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.02)
            assert done.is_set(), "busy 中的 worker 应在下一检查点退出"
        finally:
            thread.quit()
            thread.wait(3000)


# ── R20：DeliveryCapability 输出形态事实 ──


class TestR20CapabilityFacts:
    def _capability(self, codec, **kw):
        from src.features.deliver.core.delivery_capability import (
            describe_delivery_capability,
        )

        return describe_delivery_capability(
            SimpleNamespace(codec=codec, pixel_format="", **kw))

    def test_gif_container_is_gif(self):
        cap = self._capability("gif", include_audio=False, burn_subtitles=True)
        assert cap.container == "gif"

    def test_png_tiff_sequence_container_and_alpha(self):
        for codec in ("png_sequence", "tiff_sequence"):
            cap = self._capability(codec, has_alpha=True, include_audio=False)
            assert cap.container == "sequence", \
                f"{codec} 是帧目录交付，不是 mov 容器"
            assert cap.alpha_capable, f"{codec} 按 rgba 直通 alpha"
            assert not cap.audio_capable

    def test_prores_container_unchanged(self):
        cap = self._capability("prores_4444", has_alpha=True,
                               include_audio=True)
        assert cap.container == "mov" and cap.alpha_capable


# ── R23：deleted-item 目标守卫 ──


class TestR23DeletedItemGuard:
    def test_sync_after_item_deleted_self_unbinds(self, qapp):
        """宿主 item 被 tree.clear() 销毁后的延迟 resize/reflow 不再抛
        RuntimeError（守卫捕获并自解绑）。"""
        from PyQt6.QtWidgets import QTreeWidget

        from src.features.edit.ui.edit_effects_panel import (
            _ResponsiveEffectSection,
        )

        tree = QTreeWidget()
        section = _ResponsiveEffectSection("Cat")
        item = _make_item()
        tree.addTopLevelItem(item)
        tree.setItemWidget(item, 0, section)
        section._host_tree = tree
        section._host_item = item

        tree.clear()  # item 的 C++ 对象被销毁
        with pytest.raises(RuntimeError):
            item.sizeHint(0)  # 确认 item 已销毁（wrapped C/C++ object）

        section._sync_host_row_height()  # 不抛
        assert section._host_item is None and section._host_tree is None, \
            "守卫触发后应自解绑"

    def test_unbind_responsive_sections_before_clear(self, qapp):
        """面板的树重建统一解绑入口：clear 后 section 的 host 引用为空。"""
        from src.features.edit.ui.edit_effects_panel import (
            _ResponsiveEffectSection,
        )
        from src.features.edit.ui.timeline_view import TimelineView

        from PyQt6.QtWidgets import QTreeWidget

        tree = QTreeWidget()
        section = _ResponsiveEffectSection("Cat")
        item = _make_item()
        tree.addTopLevelItem(item)
        tree.setItemWidget(item, 0, section)
        section._host_tree = tree
        section._host_item = item

        view = TimelineView.__new__(TimelineView)
        # 绑定面板的同名方法逻辑：直接使用 edit_effects_panel 的实现
        from src.features.edit.ui import edit_effects_panel as panel_mod
        unbind = panel_mod.EditEffectsPanel._unbind_responsive_sections.__get__(
            view, TimelineView)
        unbind(tree)
        tree.clear()
        assert section._host_item is None and section._host_tree is None


def _make_item():
    from PyQt6.QtWidgets import QTreeWidgetItem

    item = QTreeWidgetItem(["Cat"])
    return item
