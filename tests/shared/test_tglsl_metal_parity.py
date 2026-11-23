"""真实 GL ↔ Metal 对拍（阶段 6.2 / 6.3 的验收）。

参考值：生产的 DCTL wrapper 在 standalone GL 上渲染；被测值：转译器生成的 MSL
在真实 Metal 设备上 dispatch 后读回。两侧输入相同，逐像素比较。

这是「登记 Metal 能力」的依据：``msl_plugin_verification`` 里状态为 verified 的
每一个插件都必须在本机重新通过门限；噪声类插件必须不通过（且不声明 metal）。
没有 Metal 设备或没有 standalone GL 3.3 上下文时按原因 skip。

证据：设置 ``TOPOS_METAL_EVIDENCE_DIR=<目录>`` 后，本模块结束时写出对拍 JSON
（度量、编译缓存键、反射绑定、编译器身份）与每个效果的生成 MSL 源。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pytest

from src.features.shared.effects import get_effect_catalog
from src.features.shared.effects import msl_effect_registry as registry
from src.features.shared.effects import msl_plugin_verification as verification
from src.features.shared.effects.effect_spec import ParamDef, PluginBinding, ShaderKind, EffectSpec
from src.features.shared.effects.metal_abi import (
    COORD_GL_ROW0_IS_V0,
    COORD_TOP_DOWN,
    PIXEL_FORMAT_RGBA16F,
    PIXEL_FORMAT_RGBA8,
    SAMPLER_LINEAR_CLAMP,
    SAMPLER_LINEAR_REPEAT,
    describe_default_compile_options,
)
from src.features.shared.effects.metal_effect_runtime import MetalEffectRuntime, resolve_param_value
from src.features.shared.effects.tglsl_front import TglslMetalError
from src.shared.gpu.metal.context import MetalComputeContext, MetalComputeError
from tests.shared import tglsl_metal_parity as P

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGINS_DIR = REPO_ROOT / "plugins"
_SHADABLE = ("slider", "checkbox", "color", "select")
_VERIFIED = tuple(registry.get_msl_effect_registry().verified_plugins())
_NOISE_GATED = tuple(sorted(k for k, r in verification.PLUGIN_VERIFICATION_RECORDS.items()
                            if r.status == registry.STATUS_NOISE_GATED))


# ── 夹具与工具 ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def runtime():
    if not MetalComputeContext.is_supported():
        pytest.skip(f"无 Metal 设备：{MetalComputeContext.availability_reason()[1]}")
    return MetalEffectRuntime(MetalComputeContext())


@pytest.fixture(scope="module")
def gl():
    try:
        ctx = P.create_gl_context()
    except Exception as exc:  # pragma: no cover - 无 GPU 环境
        pytest.skip(f"standalone GL 3.3 context 不可用: {exc}")
    yield ctx
    ctx.release()


@pytest.fixture(scope="module")
def evidence():
    data: Dict[str, Any] = {"plugins": {}, "noise_gated": {}, "builtin": {}, "sources": {}}
    yield data
    target = os.environ.get("TOPOS_METAL_EVIDENCE_DIR")
    if target:
        _write_evidence(target, data)


def _plugin(plugin_id: str):
    from src.features.color.core.plugin_manager import PluginManager

    data = PluginManager.__new__(PluginManager)._parse_plugin_file(
        str(PLUGINS_DIR / f"{plugin_id}.tglsl"), plugin_id)
    spec = get_effect_catalog().get_spec(f"plugin:{plugin_id}")
    assert spec is not None and spec.plugin_source_glsl, plugin_id
    raw = {p["var_name"]: P.default_value(p) for p in data["params"] if p.get("type") in _SHADABLE}
    return data, spec, raw


def _uniform_values(spec: EffectSpec, raw: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    return {p.name: resolve_param_value(p, values.get(p.name), raw.get(p.name)) for p in spec.param_defs}


def _sweep(runtime: MetalEffectRuntime, gl: Any, compiled: Any, data: Dict[str, Any], spec: EffectSpec,
           raw: Dict[str, Any], *, wrap_repeat: bool = False, sizes: Sequence[Tuple[int, int]] = P.SIZES,
           kinds: Sequence[str] = P.CORPUS_KINDS) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    results: List[Dict[str, Any]] = []
    for width, height in sizes:
        for kind in kinds:
            image = P.corpus_image(kind, width, height)
            texture = runtime.upload_rgba(image)
            scale = P.input_scale(image)
            for label, values in P.param_sets(data["params"]):
                reference = P.gl_render_reference(gl, data["shader_code"], data["params"], image, values,
                                                  wrap_repeat=wrap_repeat)
                out = runtime.run(compiled, texture, width=width, height=height,
                                  values=_uniform_values(spec, raw, values))
                runtime.submit()
                metrics = P.parity_metrics(reference, runtime.read_rgba(out, width, height), scale=scale)
                metrics["case"] = f"{width}x{height}/{kind}/{label}"
                results.append(metrics)
    return P.merge_metrics(results), results


def _worst(results: List[Dict[str, Any]], count: int = 3) -> List[List[Any]]:
    return [[r["case"], round(r["worst_ratio"], 4), r["max_abs"], r["max_rel"]]
            for r in sorted(results, key=lambda r: -r["worst_ratio"])[:count]]


def _record(store: Dict[str, Any], key: str, compiled: Any, merged: Dict[str, Any],
            worst: List[List[Any]], **extra: Any) -> None:
    entry = {
        "status": "verified" if merged["gate_ok"] else "noise_gated",
        "metrics": merged,
        "worst_cases": worst,
        "compile_key": compiled.key.to_dict(),
        "reflection": compiled.transpiled.reflection(),
        "features": list(compiled.transpiled.features),
        "matched_helper": compiled.transpiled.matched_helper,
        "msl_fingerprint": compiled.transpiled.msl_fingerprint,
        "source_fingerprint": compiled.transpiled.source_fingerprint,
        "compile_seconds": round(compiled.compile_seconds, 4),
    }
    entry.update(extra)
    store[key] = entry


def _write_evidence(target: str, data: Dict[str, Any]) -> None:
    directory = pathlib.Path(target)
    directory.mkdir(parents=True, exist_ok=True)
    sources_dir = directory / "effect-metal-msl"
    sources_dir.mkdir(exist_ok=True)
    for name, msl in data["sources"].items():
        (sources_dir / f"{name}.metal").write_text(msl, encoding="utf-8")
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                          cwd=str(REPO_ROOT)).stdout.strip()
    device = MetalComputeContext()
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_head": head,
        "compiler": dict(describe_default_compile_options(), device=device.device_name,
                         registry_id=int(device.device.registryID()),
                         has_unified_memory=bool(device.device.hasUnifiedMemory())),
        "gate": {"abs_tol": P.ABS_TOL, "rel_tol": P.REL_TOL, "scale_tol": P.SCALE_TOL,
                 "rule": "|a-b| <= max(ABS_TOL, REL_TOL*max(|a|,|b|), SCALE_TOL*input_scale)；"
                         "Alpha 逐位相同；非有限值的位置必须一致",
                 "abs_tol_meaning": "1/16 个 8-bit 码值、1/4 个 10-bit 码值",
                 "rel_tol_meaning": "正常范围数值的 1–2 个 fp16 ulp",
                 "scale_tol_meaning": "fp32 舍入相对运算对象 / 采样邻域幅度的比例（实测 ≤ 2.3e-7）"},
        "corpus": verification.PLUGIN_CORPUS,
        "sizes": list(P.SIZES),
        "images": list(P.CORPUS_KINDS),
        "plugins": data["plugins"],
        "noise_gated": data["noise_gated"],
        "builtin_exposure": data["builtin"],
        "msl_sources": sorted(data["sources"]),
    }
    out = directory / f"effect-metal-parity-{verification.VERIFICATION_DATE}.json"
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


# ── 已验证的插件：在本机重新通过门限 ───────────────────────────────────


def test_verified_set_is_the_declared_set():
    declared = {s.plugin_id[7:] for s in get_effect_catalog().get_all_specs()
                if s.effect_type.startswith("plugin:") and "metal" in s.graphics_backends}
    assert set(_VERIFIED) == declared and len(_VERIFIED) == 21
    assert not set(_NOISE_GATED) & declared and len(_NOISE_GATED) == 3


@pytest.mark.parametrize("plugin_id", _VERIFIED)
def test_verified_plugin_matches_the_gl_reference(runtime, gl, evidence, plugin_id):
    data, spec, raw = _plugin(plugin_id)
    compiled = runtime.compile(PluginBinding(spec=spec, values={}, defaults=raw, index=0))
    merged, results = _sweep(runtime, gl, compiled, data, spec, raw)
    assert merged["gate_ok"], f"{plugin_id} 未通过门限：{_worst(results)}"
    assert merged["renders"] > 50 and merged["alpha_identical"]
    record = verification.PLUGIN_VERIFICATION_RECORDS[plugin_id]
    assert record.status == registry.STATUS_VERIFIED
    # 本机度量不劣于登记记录一个数量级以上（登记记录是特定设备的实测，允许设备间差异）
    assert merged["max_abs"] <= max(record.max_abs_diff, P.ABS_TOL) * 1.5 + 1e-9
    wrap_dependent = False
    if compiled.transpiled.uses_sampling:
        merged_repeat, _ = _sweep(runtime, gl, compiled, data, spec, raw, wrap_repeat=True,
                                  sizes=P.SIZES[:1], kinds=("edges",))
        wrap_dependent = merged_repeat["gate_ok"] is False
    _record(evidence["plugins"], plugin_id, compiled, merged, _worst(results),
            wrap_dependent=wrap_dependent)
    evidence["sources"][f"plugin-{plugin_id}"] = compiled.msl_source


# ── 噪声类：可转译、可编译，但逐像素结果不可复现 ──────────────────────


@pytest.mark.parametrize("plugin_id", _NOISE_GATED)
def test_noise_gated_plugin_compiles_but_does_not_pass_the_gate(runtime, gl, evidence, plugin_id):
    data, spec, raw = _plugin(plugin_id)
    compiled = runtime.compile(PluginBinding(spec=spec, values={}, defaults=raw, index=0))
    assert compiled.kernel is not None                                   # 可转译、可在真机编译
    assert "metal" not in spec.graphics_backends                          # 所以不声明 metal
    merged, results = _sweep(runtime, gl, compiled, data, spec, raw)
    assert merged["gate_ok"] is False, f"{plugin_id} 意外通过了门限——请复核噪声分类"
    assert merged["identical_fraction"] < 0.99, "噪声类插件应只有部分像素逐位相同"
    assert merged["alpha_identical"]
    _record(evidence["noise_gated"], plugin_id, compiled, merged, _worst(results),
            note=verification.PLUGIN_VERIFICATION_RECORDS[plugin_id].note)


# ── 内置曝光（参考实现 6B）──────────────────────────────────────────────

_EXPOSURE_PARAMS = [{"type": "slider", "var_name": "exposure", "label": "Exposure",
                     "default": 0.0, "min": -5.0, "max": 5.0}]
_EXPOSURE_VALUES = (-5.0, -4.0, -2.0, -1.0, -0.25, 0.0, 0.25, 1.0, 2.0, 4.0, 5.0)
_W, _H = 19, 13


def _fp16_store(values: np.ndarray) -> np.ndarray:
    """RGBA16F 写入：四舍五入到最近偶数；超出 ±65504 饱和（GPU 实测行为，非 inf）。"""
    values = np.asarray(values, dtype=np.float32)
    stored = values.astype(np.float16).astype(np.float32)
    return np.where(np.isinf(stored), np.copysign(65504.0, values), stored)


_FP16_MIN_NORMAL = float(np.finfo(np.float16).smallest_normal)        # 2^-14
_FP16_SUBNORMAL_STEP = float(np.nextafter(np.float16(0.0), np.float16(1.0)))   # 2^-24


def _assert_matches_model(got: np.ndarray, expected: np.ndarray) -> None:
    """与 numpy 模型比较：正规范围逐位相等；次正规范围至多差 1 个步长。

    次正规区间的舍入中点由 GPU 自行取舍（实测：-5 档 9216 个分量里有 1 个恰在
    中点上，numpy 取偶、GPU 取另一侧，差 1 个 2^-24）。这不影响 GL 与 Metal 的
    一致性——它们之间仍然逐位相同。
    """
    assert got.shape == expected.shape
    normal = np.abs(expected) >= _FP16_MIN_NORMAL
    assert np.array_equal(got[normal], expected[normal])
    if (~normal).any():
        assert float(np.abs(got[~normal] - expected[~normal]).max()) <= _FP16_SUBNORMAL_STEP


def _exposure_chain(runtime, compiled, texture, stops, width=_W, height=_H):
    current = texture
    for stop in stops:
        current = runtime.run(compiled, current, width=width, height=height, values={"exposure": stop})
    runtime.submit()
    return runtime.read_rgba(current, width, height)


def _exposure_model(image: np.ndarray, stops) -> np.ndarray:
    """独立 numpy 模型：输入先按纹理存储量化到 fp16，每一级输出再做一次 fp16 舍入。

    源不先量化的话，-5 档会把小值压进次正规区间，模型与 GPU 会差 1 个次正规步长。
    只对 2 的整数次幂精确（GPU 的 exp2 与 numpy 在小数档位上相差 fp32 舍入）。
    """
    values = _fp16_store(image[..., :3])
    for stop in stops:
        values = _fp16_store(values * 2.0 ** stop)
    return values


def _exposure_sweep(runtime, gl, compiled):
    results = []
    for width, height in P.SIZES:
        for kind in P.CORPUS_KINDS:
            image = P.corpus_image(kind, width, height)
            texture = runtime.upload_rgba(image)
            scale = P.input_scale(image)
            for stop in _EXPOSURE_VALUES:
                reference = P.gl_render_reference(gl, registry.EXPOSURE_GLSL, _EXPOSURE_PARAMS,
                                                  image, {"exposure": stop})
                out = runtime.run(compiled, texture, width=width, height=height, values={"exposure": stop})
                runtime.submit()
                metrics = P.parity_metrics(reference, runtime.read_rgba(out, width, height), scale=scale)
                metrics["case"] = f"{width}x{height}/{kind}/exposure={stop}"
                results.append(metrics)
    return P.merge_metrics(results), results


def test_exposure_entry_is_served_by_the_registered_helper(runtime):
    compiled = runtime.compile_entry("builtin:exposure")
    assert compiled.transpiled.matched_helper == "builtin:exposure"
    assert ("inline float3 migration_exposure(float3 linear_rgb, float exposure) {\n"
            "    return linear_rgb * exp2(exposure);\n}") in compiled.msl_source
    assert ("inline float3 f_process(float3 t_color, float2 t_uv, constant ToposFxParams& params) {\n"
            "    return migration_exposure(t_color, params.p_exposure);\n}") in compiled.msl_source
    assert compiled.transpiled.process_kind == "vec3"
    assert "u_output_tex.write(float4(result, src.a), gid);" in compiled.msl_source   # 宿主保持 Alpha


def test_exposure_sweep_is_bit_identical_to_gl(runtime, gl, evidence):
    compiled = runtime.compile_entry("builtin:exposure")
    merged, results = _exposure_sweep(runtime, gl, compiled)
    assert merged["gate_ok"] and merged["identical_fraction"] == 1.0 and merged["max_abs"] == 0.0
    assert merged["renders"] == len(P.SIZES) * len(P.CORPUS_KINDS) * len(_EXPOSURE_VALUES) == 165
    record = verification.EXPOSURE_VERIFICATION
    assert record.status == registry.STATUS_VERIFIED and record.max_abs_diff == 0.0
    _record(evidence["builtin"], "builtin:exposure", compiled, merged, _worst(results))
    evidence["sources"]["builtin-exposure"] = compiled.msl_source


def test_exposure_zero_is_a_bit_exact_passthrough(runtime, gl):
    compiled = runtime.compile_entry("builtin:exposure")
    image = P.corpus_image("hdr", _W, _H)
    texture = runtime.upload_rgba(image)
    out = runtime.run(compiled, texture, width=_W, height=_H, values={"exposure": 0.0})
    runtime.submit()
    got = runtime.read_rgba(out, _W, _H)
    assert np.array_equal(got, _fp16_store(image))                            # 含 Alpha 与负值


@pytest.mark.parametrize(("stop", "factor"), [(1.0, 2.0), (-1.0, 0.5), (2.0, 4.0), (-2.0, 0.25), (4.0, 16.0)])
def test_exposure_applies_an_exact_power_of_two_multiplier(runtime, stop, factor):
    """逐像素倍率、负值与超白：结果 == fp16(src × 2^档)（fp16 范围内的像素误差为 0）。"""
    compiled = runtime.compile_entry("builtin:exposure")
    image = P.corpus_image("hdr", _W, _H)
    got = _exposure_chain(runtime, compiled, runtime.upload_rgba(image), (stop,))
    expected = _exposure_model(image, (stop,))
    assert factor == 2.0 ** stop and int((np.abs(expected) > 0.0).sum()) > 700
    _assert_matches_model(got[..., :3], expected)
    assert np.array_equal(np.sign(got[..., :3]), np.sign(expected))         # 负值保持为负
    assert bool((expected > 1.0).any()) and bool((expected < 0.0).any())    # 超白与负值都在场
    assert np.array_equal(got[..., 3], _fp16_store(image)[..., 3])          # Alpha 原样透传


def test_exposure_endpoints_and_fp16_saturation_agree_with_gl(runtime, gl):
    compiled = runtime.compile_entry("builtin:exposure")
    for stop in (-5.0, 5.0):
        for width, height in P.SIZES:
            image = P.corpus_image("hdr", width, height)
            reference = P.gl_render_reference(gl, registry.EXPOSURE_GLSL, _EXPOSURE_PARAMS,
                                              image, {"exposure": stop})
            got = _exposure_chain(runtime, compiled, runtime.upload_rgba(image), (stop,), width, height)
            assert np.array_equal(got[..., :3], reference[..., :3])          # 含饱和像素，逐位相同
            _assert_matches_model(got[..., :3], _exposure_model(image, (stop,)))


@pytest.mark.parametrize("stops", [(1.0, -2.0), (-2.0, 1.0), (-5.0, 5.0), (3.0, -3.0), (0.0, 0.0)])
def test_exposure_two_instances_match_an_independent_model(runtime, stops):
    """同图两个实例：每个实例各传各的绑定值，链式结果与独立 numpy 模型逐位相同。

    曝光按档数相乘、可交换，所以「顺序不同」只可能来自中间纹理的 fp16 舍入；
    模型在每一级之后都做同样的 fp16 舍入。
    """
    compiled = runtime.compile_entry("builtin:exposure")
    image = P.corpus_image("hdr", _W, _H)
    got = _exposure_chain(runtime, compiled, runtime.upload_rgba(image), stops)
    _assert_matches_model(got[..., :3], _exposure_model(image, stops))


def test_exposure_instances_are_independent_and_interleave_safe(runtime):
    compiled = runtime.compile_entry("builtin:exposure")
    image = P.corpus_image("hdr", _W, _H)
    texture = runtime.upload_rgba(image)
    # 同一个 command buffer 里交错编码两条链：A = (+2, -3)，B = (-2, +1)
    a = runtime.run(compiled, texture, width=_W, height=_H, values={"exposure": 2.0})
    b = runtime.run(compiled, texture, width=_W, height=_H, values={"exposure": -2.0})
    a = runtime.run(compiled, a, width=_W, height=_H, values={"exposure": -3.0})
    b = runtime.run(compiled, b, width=_W, height=_H, values={"exposure": 1.0})
    runtime.submit()
    got_a = runtime.read_rgba(a, _W, _H)[..., :3]
    got_b = runtime.read_rgba(b, _W, _H)[..., :3]
    _assert_matches_model(got_a, _exposure_model(image, (2.0, -3.0)))
    _assert_matches_model(got_b, _exposure_model(image, (-2.0, 1.0)))
    assert not np.array_equal(got_a, got_b)
    # 只改第一个实例，结果随之改变（第二个实例读的是第一个实例的输出，不是原图）
    changed = _exposure_chain(runtime, compiled, texture, (1.0, -3.0))
    _assert_matches_model(changed[..., :3], _exposure_model(image, (1.0, -3.0)))
    assert not np.array_equal(changed[..., :3], got_a)


def test_exposure_fractional_stops_match_gl_when_chained(runtime, gl):
    """小数档位无法用 numpy 精确建模（GPU exp2 的 fp32 舍入）：与 GL 链式结果按门限比较。"""
    compiled = runtime.compile_entry("builtin:exposure")
    image = P.corpus_image("ramp", 33, 19)
    texture = runtime.upload_rgba(image)
    got = _exposure_chain(runtime, compiled, texture, (0.5, -3.5), 33, 19)
    first = P.gl_render_reference(gl, registry.EXPOSURE_GLSL, _EXPOSURE_PARAMS, image, {"exposure": 0.5})
    second = P.gl_render_reference(gl, registry.EXPOSURE_GLSL, _EXPOSURE_PARAMS,
                                   first.astype(np.float16).astype(np.float32), {"exposure": -3.5})
    metrics = P.parity_metrics(second, got, scale=P.input_scale(image))
    assert metrics["gate_ok"], metrics


def test_exposure_save_reopen_renders_identically(runtime):
    """保存 → 重开：参数、未知字段与 schema 身份往返不变，重渲染逐位相同。"""
    from src.features.edit.core.effect_instance import EffectInstance

    compiled = runtime.compile_entry("builtin:exposure")
    entry = registry.get_msl_effect_registry().entry("builtin:exposure")
    assert entry is not None and entry.param_schema_hash == compiled.key.param_schema_hash
    instance = EffectInstance(effect_id="fx-exposure-1", effect_type="builtin:exposure",
                              params={"exposure": 2.0, "future_param": {"keep": True}}, order=0)
    saved = instance.to_dict()
    saved["schema_future"] = {"top": "level"}
    reopened = EffectInstance.from_dict(json.loads(json.dumps(saved)))
    assert reopened.effect_type == "builtin:exposure" and reopened.effect_id == "fx-exposure-1"
    assert reopened.params == {"exposure": 2.0, "future_param": {"keep": True}}
    assert reopened.to_dict() == saved                                     # 未知字段不丢
    image = P.corpus_image("hdr", _W, _H)
    texture = runtime.upload_rgba(image)
    before = _exposure_chain(runtime, compiled, texture, (2.0,))
    after = _exposure_chain(runtime, compiled, texture, (float(reopened.params["exposure"]),))
    assert np.array_equal(before, after)
    _assert_matches_model(after[..., :3], _exposure_model(image, (2.0,)))
