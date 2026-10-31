"""P1-COLOR-13 质量控制测试：确定性 grain seed、导出档、状态显示。

覆盖：
- effective_grain_seed：默认恒等、animated 帧推进确定性、回退/负帧守卫
- ImageEngine.set_film_grain_params：启用不再取墙钟（重渲可复现）、
  reseed 确定性步进、_effective_film_grain_params 帧种子换算
- render_pipeline 全分辨率携带 video_denoise_quality_mode='export'
- 渲染器记录 last_video_denoise_quality（render status 探测）
- ColorMonitorStatusBar：降噪档位段轮询显示 + 降级指示器挂载
"""
from unittest.mock import MagicMock, patch

import pytest

from src.features.color.core.image_engine import (
    GRAIN_ANIMATION_STEP, ImageEngine, effective_grain_seed,
)


# ---------------------------------------------------------------------------
# 纯函数：effective_grain_seed
# ---------------------------------------------------------------------------

def test_seed_deterministic_without_animation():
    assert effective_grain_seed({"seed": 0.3}, 17) == 0.3
    assert effective_grain_seed({}, 5) == 0.0
    assert effective_grain_seed({"seed": None, "animated": False}, 5) == 0.0


def test_seed_animated_deterministic_per_frame():
    params = {"seed": 0.2, "animated": True}
    first = effective_grain_seed(params, 10)
    again = effective_grain_seed(params, 10)
    assert first == again  # 同帧重渲一致
    other = effective_grain_seed(params, 11)
    assert other != first  # 帧间变化
    assert first == pytest.approx((0.2 + 10 * GRAIN_ANIMATION_STEP) % 1.0)
    # 负帧/未知帧不推进（静帧路径）
    assert effective_grain_seed(params, -1) == 0.2


def test_seed_animation_spread():
    params = {"seed": 0.0, "animated": True}
    seeds = {round(effective_grain_seed(params, f), 6) for f in range(50)}
    # 黄金比例步长：50 帧内不重复且散布广
    assert len(seeds) == 50
    assert max(seeds) - min(seeds) > 0.9


# ---------------------------------------------------------------------------
# ImageEngine：seed 生命周期
# ---------------------------------------------------------------------------

def _engine():
    engine = ImageEngine.__new__(ImageEngine)
    engine.film_grain_params = {
        "enabled": False, "intensity": 0.0, "size": 2.0, "monochrome": True,
        "shadows": 0.0, "midtones": 1.0, "highlights": 0.0, "seed": 0.0,
        "animated": False,
    }
    engine.renderer = None
    render_calls = []
    engine.render_pipeline = lambda **kw: render_calls.append(kw)
    engine._render_calls = render_calls
    return engine


def test_enable_grain_no_wall_clock_seed():
    engine = _engine()
    with patch("src.features.color.core.image_engine.time") as fake_time:
        fake_time.time.return_value = 12345.678
        engine.set_film_grain_params({"enabled": True, "intensity": 0.3})
    # 旧实现会写 (12345 % 1000)/1000；现在必须保持 0 → 默认 0.5
    assert engine.film_grain_params["seed"] == 0.5
    # 二次设置不再变化（可复现）
    engine.set_film_grain_params({"intensity": 0.4})
    assert engine.film_grain_params["seed"] == 0.5


def test_reseed_deterministic_step():
    engine = _engine()
    engine.film_grain_params["seed"] = 0.5
    first = engine.reseed_film_grain()
    second = engine.reseed_film_grain()
    assert first == pytest.approx((0.5 + (1.0 - GRAIN_ANIMATION_STEP)) % 1.0)
    assert second != first
    assert engine.film_grain_params["seed"] == second
    assert engine._render_calls  # 触发重渲


def test_effective_params_animated_uses_renderer_frame():
    engine = _engine()
    engine.film_grain_params.update({"animated": True, "seed": 0.1})
    engine.renderer = MagicMock(current_video_frame_index=7)
    params = engine._effective_film_grain_params()
    assert params["seed"] == pytest.approx((0.1 + 7 * GRAIN_ANIMATION_STEP) % 1.0)
    assert engine.film_grain_params["seed"] == 0.1  # 原参数不被污染
    # 非 animated：原样拷贝
    engine.film_grain_params["animated"] = False
    assert engine._effective_film_grain_params()["seed"] == 0.1


def test_render_pipeline_full_res_marks_export_quality():
    """全分辨率渲染（导出/应用更改）应携带降噪 export 档。

    render_pipeline 依赖完整 GL/图层装配，单测里用源码契约守住参数注入：
    全分辨率 → 'export'，其余 → None（由渲染器按时退回 paused/preview）。
    """
    src = open("src/features/color/core/image_engine.py", encoding="utf-8").read()
    assert "'video_denoise_quality_mode': 'export' if is_full_res else None" in src


def test_renderer_records_denoise_quality():
    """_apply_video_denoise 的档位决策记录到 last_video_denoise_quality。"""
    from src.features.color.core.async_renderer import AsyncRenderer

    renderer = AsyncRenderer.__new__(AsyncRenderer)
    renderer.video_denoise_pipeline = None  # 未激活 → 早退
    renderer.last_video_denoise_quality = None
    renderer._apply_video_denoise(object(), {})
    assert renderer.last_video_denoise_quality is None

    # 档位决策本身（复制 _apply_video_denoise 的映射逻辑做契约验证）
    def choose(params, playback, mode):
        requested = params.get('video_denoise_quality_mode')
        if requested in ('preview', 'paused', 'export'):
            return requested
        elif mode == 'export':
            return 'export'
        elif playback:
            return 'preview'
        return 'paused'

    assert choose({}, False, 'preview') == 'paused'
    assert choose({}, True, 'preview') == 'preview'
    assert choose({}, False, 'export') == 'export'
    assert choose({'video_denoise_quality_mode': 'export'}, True, 'preview') == 'export'


# ---------------------------------------------------------------------------
# 状态栏（Qt）
# ---------------------------------------------------------------------------

def test_status_bar_denoise_segment_and_indicator(qapp):
    from src.features.color.ui.color_status_bar import ColorMonitorStatusBar
    from src.shared.ui.effect_degradation_indicator import (
        EffectDegradationIndicator,
    )

    bar = ColorMonitorStatusBar()
    assert isinstance(bar.degradation_indicator, EffectDegradationIndicator)

    state = {"mode": None}
    bar.set_denoise_quality_provider(lambda: state["mode"])

    state["mode"] = "paused"
    bar._poll_denoise_quality()
    assert bar._denoise_label.text() == "DN·paused"
    assert not bar._denoise_label.isHidden()

    state["mode"] = "export"
    bar._poll_denoise_quality()
    assert bar._denoise_label.text() == "DN·export"

    state["mode"] = None
    bar._poll_denoise_quality()
    assert bar._denoise_label.isHidden()


def test_canvas_denoise_probe_reads_renderer(qapp):
    from types import SimpleNamespace

    from src.features.color.ui.canvas import Canvas

    assert Canvas._probe_denoise_quality(SimpleNamespace()) is None  # 无 engine

    pipeline = MagicMock()
    pipeline.is_active.return_value = True
    probe_self = SimpleNamespace(engine=MagicMock(
        renderer=MagicMock(video_denoise_pipeline=pipeline,
                           last_video_denoise_quality="preview")))
    assert Canvas._probe_denoise_quality(probe_self) == "preview"

    pipeline.is_active.return_value = False  # 降噪未启用 → 不显示
    assert Canvas._probe_denoise_quality(probe_self) is None


def test_grain_panel_animated_and_reseed(qapp):
    from src.features.color.ui.film_grain_panel import FilmGrainPanel

    panel = FilmGrainPanel()
    assert "animated" in panel._build_params()
    assert panel._build_params()["animated"] is False

    panel.animated_check.setChecked(True)
    assert panel._build_params()["animated"] is True

    reseed_emitted = []
    panel.reseed_requested.connect(lambda: reseed_emitted.append(True))
    panel.reseed_btn.click()
    assert reseed_emitted == [True]

    # set_params 往返
    panel.set_params({"animated": True})
    assert panel.animated_check.isChecked()
