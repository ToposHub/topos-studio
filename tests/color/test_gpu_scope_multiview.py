"""Multi-view output isolation, shared sampling and cached resource lifetime."""
from itertools import count
from unittest.mock import Mock

import pytest

from src.features.color.core.scope_types import ScopeDemand
from src.shared.gpu.gpu_scope_engine import GpuScopeEngine


@pytest.mark.parametrize(('mode', 'expected'), [
    ('quad', {'waveform', 'vectorscope', 'parade', 'histogram'}),
    ('all', {'waveform', 'vectorscope', 'parade', 'luma', 'histogram', 'cie', 'falsecolor'}),
])
def test_multiview_samples_once_and_publishes_independent_outputs(
    mode: str, expected: set[str],
) -> None:
    ctx = Mock()
    sequence = count(1)
    ctx.texture.side_effect = lambda *args, **kwargs: Mock(glo=next(sequence))
    ctx.framebuffer.side_effect = lambda *args, **kwargs: Mock()
    engine = GpuScopeEngine(ctx)
    for method in ('sample', 'accumulate_waveform', 'display_waveform',
                   'accumulate_vectorscope', 'display_vectorscope',
                   'accumulate_parade', 'display_parade', 'accumulate_histogram',
                   'reduce_histogram', 'display_histogram', 'accumulate_cie',
                   'display_cie', 'display_falsecolor'):
        setattr(engine, method, Mock())
    demand = ScopeDemand(1, 1, mode, (800, 600))
    output = engine.render_demand(demand, Mock())
    assert set(output) == expected
    ids = [texture_id for texture_id, _size in output.values()]
    assert len(set(ids)) == len(ids)
    engine.sample.assert_called_once()


def test_size_key_change_releases_stale_pool_n09() -> None:
    """N09 债务：尺寸键变化 = 旧池整体释放（单活动池语义）。

    浮窗/resize 每次产生新尺寸键——旧实现把每套全尺寸纹理永久留在
    _pool_cache（实测 1647MB/10min 泄漏主犯）。
    """
    ctx = Mock()
    sequence = count(1)
    released: list[Mock] = []
    ctx.texture.side_effect = lambda *a, **k: Mock(
        glo=next(sequence), release=lambda m=None: None)
    ctx.framebuffer.side_effect = lambda *a, **k: Mock()
    engine = GpuScopeEngine(ctx)
    first = engine.ensure_pool((320, 180), (1024, 300), (800, 800))
    first_texs = [v for k, v in first.items()
                  if not k.endswith('_size') and hasattr(v, 'release')]
    assert len(engine._pool_cache) == 1
    # 新尺寸键 → 旧池全部释放、缓存只剩新池
    second = engine.ensure_pool((480, 270), (1024, 300), (800, 800))
    assert len(engine._pool_cache) == 1
    assert engine._pool is second
    assert second is not first
    # 同键复用
    assert engine.ensure_pool((480, 270), (1024, 300), (800, 800)) is second
    engine.release()
    engine.release()  # 幂等


def test_multiview_display_resources_are_reused_and_released_once() -> None:
    ctx = Mock()
    sequence = count(1)
    ctx.texture.side_effect = lambda *args, **kwargs: Mock(glo=next(sequence))
    ctx.framebuffer.side_effect = lambda *args, **kwargs: Mock()
    engine = GpuScopeEngine(ctx)
    pool = engine.ensure_pool((320, 180), (1024, 300), (800, 800))
    allocated = ctx.texture.call_count, ctx.framebuffer.call_count
    assert engine.ensure_pool((320, 180), (1024, 300), (800, 800)) is pool
    assert (ctx.texture.call_count, ctx.framebuffer.call_count) == allocated
    assert len({pool[key].glo for key in
                ('wf_display', 'parade_display', 'luma_display')}) == 3
    resources = [value for key, value in pool.items()
                 if not key.endswith('_size') and hasattr(value, 'release')]
    engine.release()
    engine.release()
    for resource in resources:
        resource.release.assert_called_once()
