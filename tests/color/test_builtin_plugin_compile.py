"""P0-COL-01：全部内置 .tglsl 插件经真实 GL 3.3 编译门禁。

历史缺陷：warm_tint.tglsl 引用未声明的 ``u_strength``——参数名改写的
``\\b`` 词边界不命中 ``u_`` 前缀，残留未声明标识符使真实 ModernGL 编译
失败；渲染循环对编译失败的层直接 ``continue``，用户开启内置 Warm Tint
后整层被静默丢弃。

本门禁保证：
1. 每个内置 .tglsl 都能以声明默认参数通过 GL 3.3 composition 真实编译；
2. 插件源引用未声明 uniform 时，build 显式失败（返回 None）并写入
   渲染诊断缓冲（record_compile_failure），失败可见、可检索。
"""

from __future__ import annotations

import os
import textwrap

import pytest

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")

from src.features.color.core.plugin_manager import get_plugin_manager  # noqa: E402
from src.features.color.core.shader_compiler import (  # noqa: E402
    SOURCE_COMPOSITION,
    ShaderPipeline,
    plugin_binding_from_instance,
)

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PLUGIN_DIR = os.path.join(_REPO, "plugins")

# 收集期扫描内置插件目录（只取非 AI 的 GLSL 插件；.tai 由 AI 引擎加载，
# 不进入 fragment 编译入口）。
_PLUGIN_DATA = get_plugin_manager().scan_plugins(_PLUGIN_DIR)
_GLSL_PLUGIN_IDS = sorted(
    pid for pid, data in _PLUGIN_DATA.items()
    if (data.get("shader_code") or "").strip()
)


def _default_params(plugin_id: str) -> dict:
    """用插件声明的默认参数构造实例参数（structural 参数同源）。"""
    params = {}
    for p in _PLUGIN_DATA[plugin_id].get("params", []):
        params[p["var_name"]] = p.get("default")
    return params


@pytest.fixture(scope="module")
def gl():
    ctx = moderngl.create_standalone_context(require=330)
    yield ctx
    ctx.release()


@pytest.fixture(scope="module")
def pipeline(gl):
    p = ShaderPipeline(gl)
    yield p
    p.release()


@pytest.mark.skipif(not _GLSL_PLUGIN_IDS,
                    reason="未扫描到任何内置 GLSL 插件（目录/加载器回归）")
@pytest.mark.parametrize("plugin_id", _GLSL_PLUGIN_IDS)
def test_builtin_plugin_compiles_under_gl330(gl, pipeline, plugin_id):
    binding = plugin_binding_from_instance({
        "plugin_id": plugin_id,
        "enabled": True,
        "params": _default_params(plugin_id),
    })
    assert binding is not None, (
        f"内置插件 {plugin_id} 应能构造 PluginBinding（默认参数="
        f"{_default_params(plugin_id)}）"
    )
    prog = pipeline.build_composition_shader(plugins=[binding])
    assert prog is not None, (
        f"内置插件 {plugin_id} 在 GL 3.3 composition 真实编译失败"
        "（P0-COL-01：编译失败曾导致整层被静默跳过）"
    )


def test_warm_tint_strength_uniform_reachable(pipeline):
    """回归锚点：warm_tint 的 strength 改写后必须是已声明的命名空间 uniform。"""
    binding = plugin_binding_from_instance({
        "plugin_id": "warm_tint", "enabled": True,
        "params": {"strength": 0.35},
    })
    assert binding is not None
    prog = pipeline.build_composition_shader(plugins=[binding])
    assert prog is not None
    assert "u_P0_strength" in prog, (
        "warm_tint 的 strength 参数应被改写为 u_P0_strength 并真实声明"
    )


def test_undeclared_uniform_reference_fails_with_diagnostic(tmp_path):
    """引用未声明 u_* 的插件必须显式失败并写入诊断缓冲，而非含糊的 GL 报错。"""
    from src.shared.render_diagnostics import get_render_diagnostic_sink

    (tmp_path / "Bad_Ref.tglsl").write_text(textwrap.dedent("""\
        // @name Bad Ref
        // @param slider strength "Strength" 0.5 0.0 1.0
        vec3 process(vec3 color, vec2 uv) {
            float s = u_strength;
            return color * (1.0 + s);
        }
    """), encoding="utf-8")

    pm = get_plugin_manager()
    pm.scan_plugins(str(tmp_path))
    ctx = None
    try:
        binding = plugin_binding_from_instance(
            {"plugin_id": "Bad_Ref", "enabled": True,
             "params": {"strength": 0.5}})
        assert binding is not None
        ctx = moderngl.create_standalone_context(require=330)
        pipeline = ShaderPipeline(ctx)
        try:
            sink = get_render_diagnostic_sink()
            before = sink.count(source=SOURCE_COMPOSITION)
            prog = pipeline.build_composition_shader(
                plugins=[binding], request_id="render-test/1", layer_id="layer A"
            )
            assert prog is None, "引用未声明 uniform 的插件必须编译失败"
            entries = sink.recent(source=SOURCE_COMPOSITION)
            assert sink.count(source=SOURCE_COMPOSITION) > before, (
                "编译失败必须写入渲染诊断缓冲（record_compile_failure）"
            )
            assert entries and "u_strength" in entries[0].message, (
                f"诊断应指明未声明标识符，实际：{entries[:1]}"
            )
            assert entries[0].request_id == "render-test_1"
            assert entries[0].layer_id == "layer_A"
            assert entries[0].plugin_ids == ("plugin:Bad_Ref",)
            assert entries[0].code == "SHADER_UNDECLARED_UNIFORM"
        finally:
            pipeline.release()
    finally:
        if ctx is not None:
            ctx.release()
        # scan_plugins 只增不删：夹具条目必须显式逐出，重扫内置目录
        # 无法清除（会泄漏给后续使用单例的测试）。
        pm.plugins.pop("Bad_Ref", None)
        pm.scan_plugins(_PLUGIN_DIR)  # 恢复内置插件目录
