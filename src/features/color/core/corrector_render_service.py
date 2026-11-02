"""Corrector 渲染服务（N06，B07）——单层完整算子的显式化提取。

从 ``AsyncRenderer._do_render_impl`` 的单层 ping-pong 循环拆出"一个
Corrector = 一套完整 Layer 能力"的执行体，**不改变数学次序**（B07.1）：

- ``prepare_corrector``：旗标计算 + 插件 binding 解析 + composition
  shader 编译（复用 E04 ShaderPipeline）。编译失败返回**显式诊断**
  （``CorrectorDiagnostic``）——最终输出依赖锥内失败必须阻断导出
  （B07.4），禁止静默丢层。
- ``render_corrector_gl``：一次合成 pass——绑定 grade_input/matte/aux
  纹理、上传 uniforms（复用 E05 composition_uniforms）、渲染 quad。
  输入显式区分（§4.1）：``grade_input``（栈前缀/AI 结果）、
  ``matte_tex``（render_key_gl 产物）、外部 key 已在 matte 内融合。
- ``render_key_gl``：standalone matte 组合（Window 形状 CPU 光栅化 +
  外部 key + AI matte 按 §4.1 顺序布尔合成）——与 E03
  ``_generate_matte`` 的组合语义同源；E03 内部路径继续用自己的缓存
  版本，本函数服务 standalone 执行器（B09）与等价测试。
- ``run_serial_passes``：串行链 standalone 执行（B00 基线对照/B09
  执行器前身）。

线程约束：全部函数只在渲染线程调用（GL 上下文持有中）；不创建 Qt
对象、不访问 UI 活模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from src.features.color.core.shader_compiler import (
    ShaderPipeline,
    plugin_bindings_from_instances,
)


@dataclass(frozen=True)
class CorrectorDiagnostic:
    """算子准备/执行失败的显式诊断（不静默跳层）。"""

    code: str            # shader_compile_failed / plugin_binding_failed …
    message: str
    layer_id: str | None = None
    plugin_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorrectorProgram:
    """一次 Corrector pass 的编译产物（可复用，同旗标同缓存键）。"""

    program: Any                      # moderngl.Program
    flags: Mapping[str, bool]
    bindings: tuple = ()              # PluginBinding 序列
    layer_id: str | None = None


def prepare_corrector(pipeline: ShaderPipeline, layer: Mapping[str, Any], *,
                      request_id: Any = None,
                      layer_id: Any = None,
                      bindings_override: Sequence | None = None
                      ) -> CorrectorProgram | CorrectorDiagnostic:
    """E03 单层循环的编译阶段提取（旗标计算 + 插件 binding + 编译）。

    与 ``_do_render_impl`` 原 inline 逻辑逐项一致（B07.3 对照入口）；
    失败路径由调用方记录诊断并**阻断依赖锥**（不 continue 丢层——
    导出侧硬门在 B13，此处先给出显式返回值）。
    """
    from src.features.color.core.curves_soft_clip import (
        curves_soft_clip_is_identity,
    )

    def _get(key: str, default: Any = None) -> Any:
        """dict DTO / Layer 对象双形态访问（与 composition_uniforms 约定）。"""
        if isinstance(layer, Mapping):
            return layer.get(key, default)
        return getattr(layer, key, default)

    has_curves = pipeline.has_active_curves_lut(_get('curves_lut'))
    has_warper = _get('warper_params') is not None
    has_lut3d = _get('active_lut_path') is not None
    scp = _get('soft_clip_params') or {}
    has_soft_clip = scp.get('enabled', False)
    has_curves_soft_clip = not curves_soft_clip_is_identity(
        _get('curves_soft_clip'))
    has_hsl_curve = pipeline.has_active_hsl_curve_lut(_get('hsl_curve_lut'))
    has_mixer = pipeline.has_active_mixer_params(_get('mixer_params'))

    try:
        if bindings_override is not None:
            # 测试/执行器注入显式 PluginBinding（如 B00 插件 stub 对照）
            plugin_bindings = tuple(bindings_override)
        else:
            plugin_bindings = tuple(plugin_bindings_from_instances(
                _get('plugins') or []))
    except Exception as exc:  # noqa: BLE001 —— binding 解析失败 = 显式诊断
        return CorrectorDiagnostic(
            code="plugin_binding_failed", message=str(exc),
            layer_id=str(layer_id or _get('id') or ""),
            plugin_ids=tuple(
                p.get('plugin_id') or p.get('type') or ''
                for p in (_get('plugins') or []) if isinstance(p, Mapping)))

    program = pipeline.build_composition_shader(
        has_curves=has_curves, has_warper=has_warper,
        has_lut3d=has_lut3d, has_soft_clip=has_soft_clip,
        grain_enabled=False, has_hsl_curve=has_hsl_curve,
        has_mixer=has_mixer, has_curves_soft_clip=has_curves_soft_clip,
        plugins=plugin_bindings or None,
        request_id=request_id, layer_id=layer_id,
    )
    if program is None:
        return CorrectorDiagnostic(
            code="shader_compile_failed",
            message="composition shader 编译失败（详见诊断缓冲）",
            layer_id=str(layer_id or _get('id') or ""),
            plugin_ids=tuple(
                (b.spec.plugin_id or b.spec.effect_type)
                for b in plugin_bindings))
    return CorrectorProgram(
        program=program,
        flags={
            "has_curves": has_curves, "has_warper": has_warper,
            "has_lut3d": has_lut3d, "has_soft_clip": has_soft_clip,
            "has_curves_soft_clip": has_curves_soft_clip,
            "has_hsl_curve": has_hsl_curve, "has_mixer": has_mixer,
        },
        bindings=plugin_bindings,
        layer_id=str(layer_id or _get('id') or ""))


def render_corrector_gl(
    ctx: Any,
    prepared: CorrectorProgram,
    *,
    layer: Mapping[str, Any],
    grade_input_tex: Any,
    matte_tex: Any,
    viewport: tuple[int, int],
    update_uniforms: Callable[[Any], None],
    bind_aux: Callable[[Any], None] | None = None,
    render_quad: Callable[[Any], None],
    clear: bool = True,
) -> None:
    """E03 单层循环的执行阶段提取（一次合成 pass，数学次序不变）。

    调用序与原 inline 块逐行一致：target fbo（调用方已绑定）→
    viewport/clear → input/matte 纹理单元 0/1 → uniforms（E05）→
    aux 纹理 → quad 渲染。
    """
    if isinstance(prepared, CorrectorDiagnostic):
        raise RuntimeError(
            f"render_corrector_gl 拒绝执行诊断算子: {prepared.code}")
    program = prepared.program
    ctx.viewport = (0, 0, int(viewport[0]), int(viewport[1]))
    if clear:
        ctx.clear(0.0, 0.0, 0.0, 0.0)
    grade_input_tex.use(0)
    matte_tex.use(1)
    program['u_input_tex'].value = 0
    program['u_matte_tex'].value = 1
    update_uniforms(program)
    if bind_aux is not None:
        bind_aux(program)
    render_quad(program)


# ── standalone key 组合（§4.1 顺序；B10 冻结正式规格） ──────────────────

def render_key_gl(ctx: Any, layer: Mapping[str, Any],
                  key_source_image: np.ndarray | None,
                  external_key: np.ndarray | None = None,
                  size: tuple[int, int] = (64, 64)) -> np.ndarray:
    """组合该层的有效 key（CPU oracle 形态；B09 执行器上传为纹理）。

    顺序（§4.1）：Window 形状布尔合成 → 外部 key 相乘（初始规格）→
    AI matte（add/subtract/intersect，E03 同公式）→ 输出 0–1 float32。
    qualifier 的 GPU keyer 采样属于 E03 缓存路径；standalone 形态在
    B10 冻结 key 处理链规格后由 N09 完整接线。
    """
    h, w = int(size[1]), int(size[0])
    mask = np.ones((h, w), dtype=np.float32)
    shapes = layer.get('mask_shapes') or []
    for shape_dict in shapes:
        if not isinstance(shape_dict, Mapping) or not shape_dict.get(
                'visible', True):
            continue
        contribution = _rasterize_shape(shape_dict, w, h)
        operation = shape_dict.get('operation', 'add')
        if operation == 'add':
            mask = np.maximum(mask, contribution)
        elif operation == 'subtract':
            mask = np.clip(mask - contribution, 0.0, 1.0)
        elif operation == 'intersect':
            mask = mask * contribution
    if external_key is not None:
        key = np.asarray(external_key, dtype=np.float32)
        if key.shape != mask.shape:
            import cv2
            key = cv2.resize(key, (w, h), interpolation=cv2.INTER_LINEAR)
        # 外部 key 与内部有效 key 相乘（初始规格，B10 正式冻结）
        mask = mask * np.clip(key, 0.0, 1.0)
    ai_matte = layer.get('ai_matte')
    if ai_matte is not None and layer.get('ai_matte_enabled', True):
        import cv2
        ai = np.asarray(ai_matte, dtype=np.float32)
        if ai.shape != mask.shape:
            ai = cv2.resize(ai, (w, h), interpolation=cv2.INTER_LINEAR)
        operation = layer.get('ai_matte_operation', 'add')
        if operation == 'add':
            mask = np.maximum(mask, ai)
        elif operation == 'subtract':
            mask = np.clip(mask - ai, 0.0, 1.0)
        elif operation == 'intersect':
            mask = mask * ai
    return np.clip(mask, 0.0, 1.0)


def _rasterize_shape(shape_dict: Mapping[str, Any], w: int, h: int
                     ) -> np.ndarray:
    """形状 → 归一化 [0,1] 贡献图（生产 CPU 光栅化器同源）。"""
    from src.features.color.core.layer import MaskShape
    from src.features.color.core.mask_shapes_cpu import rasterize_shape_cpu
    shape = MaskShape.from_dict(dict(shape_dict))
    try:
        result = rasterize_shape_cpu(shape, w, h)
    except Exception:  # noqa: BLE001 —— 单形状失败不阻断整张 matte
        return np.zeros((h, w), dtype=np.float32)
    rendered = result[0] if isinstance(result, tuple) else result
    if rendered is None:
        # curve/bitmap/brush 是 GPU 管线专属：standalone 形态按空贡献，
        # E03 内部路径继续用其完整实现（B10 冻结 key 链时统一）
        return np.zeros((h, w), dtype=np.float32)
    rendered = np.asarray(rendered, dtype=np.float32)
    if rendered.ndim == 3:
        rendered = rendered[..., 0]
    if rendered.shape != (h, w):
        import cv2
        rendered = cv2.resize(rendered, (w, h), interpolation=cv2.INTER_LINEAR)
    if shape_dict.get('invert', False):
        rendered = 1.0 - rendered
    opacity = float(shape_dict.get('opacity', 1.0) or 1.0)
    return np.clip(rendered * opacity, 0.0, 1.0)


# ── 串行链 standalone 执行（B00 基线对照 / B09 执行器前身） ─────────────

def run_serial_passes(
    ctx: Any,
    pipeline: ShaderPipeline,
    source: np.ndarray,
    layer_dtos: Sequence[Mapping[str, Any]],
    *,
    bindings_per_layer: Sequence[Sequence | None] | None = None,
) -> np.ndarray:
    """按序执行 Corrector 链（RGBA16F ping-pong 同生产精度）。

    与 B00 基线生成器同口径：曲线 LUT 上传 unit 2、白 matte、
    composition_uniform_values 全量 uniforms。用于 B07.5 golden 对照
    与 B09 图执行器的串行子集。
    """
    from src.features.color.core.composition_uniforms import (
        composition_uniform_values,
        upload_composition_uniforms,
    )
    import moderngl

    h, w = source.shape[:2]
    current = np.ascontiguousarray(source)
    for index, layer in enumerate(layer_dtos):
        if not layer.get('visible', True):
            continue  # bypass：跳过该层（E03 同语义）
        override = None
        if bindings_per_layer is not None and index < len(bindings_per_layer):
            override = bindings_per_layer[index]
        prepared = prepare_corrector(pipeline, layer,
                                     bindings_override=override)
        if isinstance(prepared, CorrectorDiagnostic):
            raise RuntimeError(
                f"串行执行遇编译失败: {prepared.code} {prepared.message}")
        src_tex = ctx.texture((w, h), components=4, dtype="f2",
                              data=current.astype(np.float16))
        out_tex = ctx.texture((w, h), components=4, dtype="f2")
        matte = np.ones((h, w, 4), dtype=np.float16)
        matte_tex = ctx.texture((w, h), components=4, dtype="f2", data=matte)
        fbo = ctx.framebuffer(color_attachments=[out_tex])
        program = prepared.program
        program['u_input_tex'].value = 0
        program['u_matte_tex'].value = 1
        src_tex.use(0)
        matte_tex.use(1)
        curves_tex = None
        if 'u_curves_tex' in program:
            lut = layer.get('curves_lut')
            if lut is None:
                ramp = np.linspace(0.0, 1.0, 256, dtype=np.float32)
                data = np.stack([ramp] * 3, axis=-1).reshape(256, 1, 3)
            else:
                data = np.asarray(lut, dtype=np.float32).reshape(256, 1, 3)
            curves_tex = ctx.texture((256, 1), components=3, dtype="f4",
                                     data=np.ascontiguousarray(data).tobytes())
            curves_tex.filter = (ctx.LINEAR, ctx.LINEAR)
            curves_tex.use(2)
            program['u_curves_tex'].value = 2
        values = composition_uniform_values(
            layer, bindings=prepared.bindings, width=w, height=h)
        upload_composition_uniforms(program, values)
        fbo.use()
        vao = ctx.vertex_array(program, [(ctx.buffer(np.array([
            -1.0, -1.0, 0.0, 0.0, 1.0, -1.0, 1.0, 0.0,
            -1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0],
            dtype="f4").tobytes()), "2f 2f", "in_vert", "in_texcoord")])
        vao.render(moderngl.TRIANGLE_STRIP)
        current = np.frombuffer(
            fbo.read(components=4, dtype="f2"),
            dtype=np.float16).reshape(h, w, 4).astype(np.float32)
        for tex in (src_tex, out_tex, matte_tex, curves_tex):
            if tex is not None:
                tex.release()
        fbo.release()
    return np.ascontiguousarray(current)
