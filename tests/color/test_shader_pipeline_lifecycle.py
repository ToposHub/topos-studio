"""Lifecycle regression tests for cached Color shader programs.

P7-01 起 ShaderPipeline 只持有 composition 程序缓存（build_shader 融合
路径已删除，见台账 0004a）。
"""

from unittest.mock import MagicMock

from src.features.color.core.shader_compiler import ShaderPipeline


def test_release_releases_cached_programs_once() -> None:
    pipeline = ShaderPipeline(MagicMock())
    composition_program = MagicMock()
    # One cached entry can appear twice under different keys after callers
    # reuse a program object; shutdown must remain idempotent.
    pipeline.comp_cache = {
        ("one",): composition_program,
        ("duplicate",): composition_program,
    }

    pipeline.release()

    composition_program.release.assert_called_once()
    assert pipeline.comp_cache == {}
