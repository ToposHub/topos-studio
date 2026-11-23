"""Metal 效果运行器：参数打包、按缓存键编译、dispatch 与读回（阶段 6.2 / 6.4）。

宿主执行接口。预览 / 导出管线拿到 ``PluginBinding``（或注册表内置条目）后：

    compiled = runtime.compile(binding)             # 缓存键命中则不重编译
    out_tex  = runtime.run(compiled, in_tex, width=w, height=h, binding=binding)
    runtime.submit()                                # 一次 CPU/GPU 同步点

契约
    - 每个效果实例一次 dispatch、一份独立的参数缓冲：同一插件的两个实例各传
      各自的绑定值，互不串扰；
    - 编译产物以 :class:`MetalCompileKey` 的摘要为身份（源码指纹、宿主 ABI、
      编译参数、像素格式、设备能力、插件版本……缺一即重编译）；kernel 函数名
      带摘要，因此底层 ``MetalComputeContext`` 的按名缓存不会串用产物；
    - 任何失败都抛 :class:`TglslMetalError`（诊断含效果 ID、阶段、后端与精简
      编译消息），绝不静默返回输入或丢弃效果。

线程边界：compile / run 可在适配工作线程调用（不碰 GL、不碰 Qt）。编译在
独立于缓存锁的编译锁下串行进行——并发请求同一键只编译一次。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from src.shared.gpu.metal.context import MetalComputeError

from src.features.shared.effects.metal_abi import (
    BUFFER_SLOT_PARAMS,
    HOST_ABI,
    COORD_GL_ROW0_IS_V0,
    COORD_CONVENTIONS,
    COMPILE_OPTIONS_CONTEXT_DEFAULT,
    PIXEL_FORMATS,
    PIXEL_FORMAT_RGBA16F,
    SAMPLER_LINEAR_CLAMP,
    TEXTURE_SLOT_INPUT,
    TEXTURE_SLOT_OUTPUT,
    MetalCompileKey,
    ParamsLayout,
    ParamField,
    device_capability_fingerprint,
    layout_fingerprint,
    trim_compile_message,
)
from src.features.shared.effects.plugin_identity import compute_parameter_schema_hash
from src.features.shared.effects.tglsl_front import (
    BACKEND_METAL,
    TRANSPILER_VERSION,
    TranspileStage,
    TglslMetalDiagnostic,
    TglslMetalError,
)
from src.features.shared.effects.tglsl_msl_transpiler import TranspiledEffect, transpile_tglsl

__all__ = [
    "CompiledMetalEffect",
    "MetalEffectRuntime",
    "binding_param_values",
    "pack_params",
    "resolve_param_value",
]

_PLACEHOLDER_KERNEL = "topos_fx_kernel"
_HOST_HELPER_ABI_VERSION = "1"


def resolve_param_value(pdef: Any, value: Any, raw_default: Any = None) -> Any:
    """用户侧参数值 → uniform 值，口径与 ``EffectPassRunner._pack_plugin_params`` 一致。

    ``value`` 为 None 时依次回退到 ``.tglsl`` 原始默认值（select 是 int 索引）、
    ``ParamDef.default``，最后是类型中性值。
    """
    ptype = pdef.ptype
    if value is None:
        value = raw_default if raw_default is not None else pdef.default
    if ptype == "float":
        return float(value) if value is not None else 0.0
    if ptype == "bool":
        return 1 if bool(value) else 0
    if ptype == "int":
        return int(value) if value is not None else 0
    if ptype == "option":
        labels = tuple(pdef.labels or ())
        if isinstance(value, str):
            if value in labels:
                return labels.index(value)
            return int(value) if value.isdigit() else 0
        return int(value) if value is not None else 0
    if ptype == "color":
        if isinstance(value, (list, tuple)):
            rgb = tuple(float(v) for v in value[:3])
            while len(rgb) < 3:
                rgb = rgb + (rgb[-1],)
            return rgb
        v = float(value) if value is not None else 0.0
        return (v, v, v)
    if ptype in ("vec2", "vec3"):
        return tuple(float(v) for v in value)
    # 未知 ptype：GLSL 类型投影会在布局阶段显式报错（G5），这里只兜住取值。
    v = float(value) if value is not None else 0.0
    return (v, v, v)


def binding_param_values(binding: Any) -> Dict[str, Any]:
    """从 ``PluginBinding`` 取出每个参数的 uniform 值（values → defaults → 中性值）。"""
    spec = binding.spec
    out: Dict[str, Any] = {}
    for pdef in spec.param_defs:
        out[pdef.name] = resolve_param_value(
            pdef, binding.values.get(pdef.name), binding.defaults.get(pdef.name))
    return out


def schema_default_values(param_defs: Sequence[Any]) -> Dict[str, Any]:
    """只按参数 schema 取默认 uniform 值（注册表内置条目用）。"""
    return {pdef.name: resolve_param_value(pdef, None, None) for pdef in param_defs}


def pack_params(layout: ParamsLayout, values: Mapping[str, Any], width: int, height: int) -> np.ndarray:
    """把 uniform 值写进与 ``ToposFxParams`` 对齐的结构化数组（一个小端标量缓冲）。

    Raises:
        TglslMetalError: 参数值无法表示（例如 int 溢出）——显式诊断，不截断。
    """
    arr = np.zeros((), dtype=layout.numpy_dtype)
    arr["u_tex_size_x"] = np.float32(width)
    arr["u_tex_size_y"] = np.float32(height)
    for field in layout.param_fields:
        _write_field(arr, field, values.get(field.param_name))
    return arr


def _write_field(arr: np.ndarray, field: ParamField, value: Any) -> None:
    try:
        if len(field.fields) == 1 and field.msl_scalar == "float":
            with np.errstate(over="ignore"):
                arr[field.fields[0]] = np.float32(float(value))
        elif len(field.fields) == 1:
            arr[field.fields[0]] = np.int32(int(value))
        else:
            for name, component in zip(field.fields, value):
                with np.errstate(over="ignore"):
                    arr[name] = np.float32(float(component))
    except (TypeError, ValueError, OverflowError) as exc:
        raise TglslMetalError(TglslMetalDiagnostic(
            plugin_id="", plugin_version="", stage=TranspileStage.RUN.value,
            backend=BACKEND_METAL, code="param_value_invalid",
            message=f"参数 {field.param_name!r} 的值 {value!r} 无法写入 {field.glsl_type} uniform：{exc}")) from exc


@dataclass(frozen=True)
class CompiledMetalEffect:
    """一次成功编译的产物：kernel + 布局 + 键 + 默认值。"""

    key: MetalCompileKey
    transpiled: TranspiledEffect
    kernel: Any                              # MetalKernel（持有 pipeline state）
    pixel_format: str
    coord_convention: str
    sampler_spec: str
    default_values: Tuple[Tuple[str, Any], ...]
    msl_source: str                          # 实际送编译的源（kernel 名带摘要）
    compile_seconds: float = 0.0

    @property
    def effect_id(self) -> str:
        return self.key.effect_id

    def packed_params(self, values: Optional[Mapping[str, Any]], width: int, height: int) -> np.ndarray:
        merged = dict(self.default_values)
        if values:
            merged.update(values)
        return pack_params(self.transpiled.layout, merged, width, height)

    def evidence(self) -> Dict[str, Any]:
        """对拍证据：键、反射绑定、生成的 MSL 源与特征。"""
        return {
            "compile_key": self.key.to_dict(),
            "reflection": self.transpiled.reflection(),
            "features": list(self.transpiled.features),
            "matched_helper": self.transpiled.matched_helper,
            "process_kind": self.transpiled.process_kind,
            "compile_seconds": self.compile_seconds,
            "msl_source": self.msl_source,
        }


class MetalEffectRuntime:
    """在一个 ``MetalComputeContext`` 上编译并执行转译出来的效果。

    生命周期：每个使用方（预览工作线程 / 导出线程）持有一个实例；不跨线程
    共享 context。``retire`` 用于热重载后让旧修订的产物离场。
    """

    def __init__(self, context: Any, *, registry: Any = None, cache_size: int = 64,
                 device_capability: Optional[str] = None) -> None:
        self._context = context
        self._registry = registry
        self._cache_size = max(1, int(cache_size))
        self._cache: "OrderedDict[str, CompiledMetalEffect]" = OrderedDict()
        self._lock = threading.RLock()
        self._compile_lock = threading.Lock()
        self._device_capability = (device_capability if device_capability is not None
                                   else device_capability_fingerprint(context.device))
        self.stats: Dict[str, int] = {"compiles": 0, "cache_hits": 0, "evictions": 0}

    # ── 内部工具 ──
    @property
    def device_capability(self) -> str:
        return self._device_capability

    @property
    def context(self) -> Any:
        return self._context

    def _helpers(self) -> Tuple[Any, ...]:
        if self._registry is None:
            from src.features.shared.effects.msl_effect_registry import get_msl_effect_registry

            self._registry = get_msl_effect_registry()
        return tuple(self._registry.helpers())

    @staticmethod
    def _pixel_format_const(pixel_format: str) -> Any:
        import Metal  # pyobjc-framework-Metal；非 macOS 宿主由调用方的可用性探测挡在前面

        return getattr(Metal, PIXEL_FORMATS[pixel_format][0])

    # ── 编译 ──
    def compile(self, binding: Any, *, pixel_format: str = PIXEL_FORMAT_RGBA16F,
                coord_convention: str = COORD_GL_ROW0_IS_V0,
                sampler_spec: str = SAMPLER_LINEAR_CLAMP) -> CompiledMetalEffect:
        """编译一个 ``PluginBinding``（缓存键命中则直接返回已有产物）。"""
        from src.features.shared.effects.effect_spec import PluginBinding

        if not isinstance(binding, PluginBinding):
            raise TypeError("MetalEffectRuntime.compile 只接受 PluginBinding（ADR 0004 G5），拒绝原始 instance 数据")
        spec = binding.spec
        plugin_id = spec.plugin_id[len("plugin:"):] if spec.plugin_id.startswith("plugin:") else spec.plugin_id
        return self._compile_source(
            spec.plugin_source_glsl, spec.param_defs,
            effect_id=spec.effect_type, plugin_id=plugin_id, plugin_version=spec.plugin_version,
            content_hash=spec.plugin_content_hash,
            param_schema_hash=(spec.plugin_parameter_schema_hash
                               or compute_parameter_schema_hash(spec.param_defs)),
            default_values=binding_param_values(binding),
            pixel_format=pixel_format, coord_convention=coord_convention, sampler_spec=sampler_spec)

    def compile_entry(self, entry_id: str, *, pixel_format: str = PIXEL_FORMAT_RGBA16F,
                      coord_convention: str = COORD_GL_ROW0_IS_V0,
                      sampler_spec: str = SAMPLER_LINEAR_CLAMP) -> CompiledMetalEffect:
        """编译注册表内置条目（如 ``builtin:exposure``）：由同一受控模板生成 kernel。"""
        from src.features.shared.effects.msl_effect_registry import get_msl_effect_registry

        registry = self._registry if self._registry is not None else get_msl_effect_registry()
        entry = registry.entry(entry_id)
        if entry is None:
            raise TglslMetalError(TglslMetalDiagnostic(
                plugin_id=entry_id, plugin_version="", stage=TranspileStage.VALIDATE.value,
                backend=BACKEND_METAL, code="unknown_msl_effect",
                message=f"MSL 效果注册表没有条目 {entry_id!r}"))
        return self._compile_source(
            entry.glsl_source, entry.param_defs,
            effect_id=entry.effect_id, plugin_id=entry.effect_id, plugin_version=entry.version,
            content_hash="", param_schema_hash=compute_parameter_schema_hash(entry.param_defs),
            default_values=schema_default_values(entry.param_defs),
            pixel_format=pixel_format, coord_convention=coord_convention, sampler_spec=sampler_spec)

    def _compile_source(self, source: str, param_defs: Sequence[Any], *, effect_id: str,
                        plugin_id: str, plugin_version: str, content_hash: str,
                        param_schema_hash: str, default_values: Mapping[str, Any],
                        pixel_format: str, coord_convention: str,
                        sampler_spec: str) -> CompiledMetalEffect:
        if pixel_format not in PIXEL_FORMATS:
            raise ValueError(f"未知像素格式 {pixel_format!r}（支持: {', '.join(sorted(PIXEL_FORMATS))}）")
        if coord_convention not in COORD_CONVENTIONS:
            raise ValueError(f"未知坐标约定 {coord_convention!r}")
        transpiled = transpile_tglsl(
            source, param_defs, plugin_id=plugin_id, plugin_version=plugin_version,
            content_hash=content_hash, kernel_name=_PLACEHOLDER_KERNEL,
            coord_convention=coord_convention, sampler_spec=sampler_spec,
            registry_helpers=self._helpers())
        host_helpers = tuple(sorted(f.split(":", 1)[1] for f in transpiled.features
                                    if f.startswith("host_helper:")))
        key = MetalCompileKey(
            host_abi=f"{HOST_ABI}", effect_id=effect_id,
            msl_fingerprint=transpiled.msl_fingerprint,
            source_fingerprint=transpiled.source_fingerprint,
            plugin_version=plugin_version, plugin_content_hash=content_hash,
            param_schema_hash=param_schema_hash,
            layout_fingerprint=layout_fingerprint(transpiled.layout),
            compile_options=COMPILE_OPTIONS_CONTEXT_DEFAULT, pixel_format=pixel_format,
            device_capability=self._device_capability, translator_version=TRANSPILER_VERSION,
            helper_versions=tuple(sorted([(h, self._helper_version(h)) for h in transpiled.helper_ids]
                                         + [(f"host:{n}", _HOST_HELPER_ABI_VERSION) for n in host_helpers])),
            coord_convention=coord_convention, sampler_spec=sampler_spec)
        digest = key.digest
        with self._lock:
            cached = self._cache.get(digest)
            if cached is not None:
                self._cache.move_to_end(digest)
                self.stats["cache_hits"] += 1
                return cached
        with self._compile_lock:
            with self._lock:
                cached = self._cache.get(digest)
                if cached is not None:          # 等锁期间另一线程已编译同一键
                    self.stats["cache_hits"] += 1
                    return cached
            msl_source = transpiled.with_kernel_name(key.kernel_name)
            started = time.perf_counter()
            try:
                kernel = self._context.compile_kernel(key.kernel_name, msl_source)
            except MetalComputeError as exc:
                raise TglslMetalError(TglslMetalDiagnostic(
                    plugin_id=plugin_id, plugin_version=plugin_version,
                    stage=TranspileStage.COMPILE.value, backend=BACKEND_METAL,
                    code="msl_compile_failed", message=trim_compile_message(str(exc)))) from exc
            compiled = CompiledMetalEffect(
                key=key, transpiled=transpiled, kernel=kernel, pixel_format=pixel_format,
                coord_convention=coord_convention, sampler_spec=sampler_spec,
                default_values=tuple(sorted(default_values.items())), msl_source=msl_source,
                compile_seconds=time.perf_counter() - started)
            with self._lock:
                self._cache[digest] = compiled
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
                    self.stats["evictions"] += 1
                self.stats["compiles"] += 1
            return compiled

    def _helper_version(self, helper_id: str) -> str:
        for helper in self._helpers():
            if helper.helper_id == helper_id:
                return helper.version
        return "unknown"

    def cached_digests(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(self._cache.keys())

    def retire(self, effect_id: Optional[str] = None, *, keep_digest: Optional[str] = None) -> int:
        """让缓存中的编译产物离场（热重载 / 关闭时调用）。返回移除的条目数。

        ``effect_id`` 为 None 时移除全部；``keep_digest`` 指定要保留的当前修订。
        注意：底层 ``MetalComputeContext`` 仍按 kernel 名持有 pipeline state，
        现有 context API 不提供逐个释放。
        """
        with self._lock:
            doomed = [d for d, c in self._cache.items()
                      if (effect_id is None or c.effect_id == effect_id) and d != keep_digest]
            for digest in doomed:
                del self._cache[digest]
            self.stats["evictions"] += len(doomed)
            return len(doomed)

    # ── 执行 ──
    def run(self, compiled: CompiledMetalEffect, input_texture: Any, *, width: int, height: int,
            values: Optional[Mapping[str, Any]] = None, output_texture: Any = None) -> Any:
        """把一次效果 dispatch 编码进当前 command buffer（不提交），返回输出纹理。

        ``values`` 是**已解析的 uniform 值**（参数名 → float / int / 三元组）；
        未给出的参数用编译时的默认值。需要用户侧取值（如 option 标签）时先用
        :func:`binding_param_values` 解析。
        """
        if width <= 0 or height <= 0:
            raise TglslMetalError(TglslMetalDiagnostic(
                plugin_id=compiled.transpiled.plugin_id,
                plugin_version=compiled.transpiled.plugin_version,
                stage=TranspileStage.RUN.value, backend=BACKEND_METAL, code="invalid_size",
                message=f"效果尺寸必须为正（实际 {width}x{height}）"))
        params = compiled.packed_params(values, width, height)
        buffer = self._context.create_buffer_from_numpy(params)
        out = output_texture
        if out is None:
            out = self._context.create_texture_2d(
                self._pixel_format_const(compiled.pixel_format), width, height, shader_write=True)
        self._context.dispatch(
            compiled.kernel,
            buffers={BUFFER_SLOT_PARAMS: buffer},
            textures={TEXTURE_SLOT_INPUT: input_texture, TEXTURE_SLOT_OUTPUT: out},
            threads=(width, height, 1))
        return out

    def run_chain(self, steps: Sequence[Tuple[CompiledMetalEffect, Optional[Mapping[str, Any]]]],
                  input_texture: Any, *, width: int, height: int) -> Any:
        """依次执行多个效果实例（各自独立参数缓冲），最后一次提交并等待完成。"""
        current = input_texture
        for compiled, values in steps:
            current = self.run(compiled, current, width=width, height=height, values=values)
        self.submit()
        return current

    def submit(self) -> Any:
        """提交当前 command buffer 并等待完成（诊断 / 验证路径的同步点）。"""
        return self._context.submit(wait=True)

    # ── 纹理搬运 ──
    def upload_rgba(self, rgba: np.ndarray, *, pixel_format: str = PIXEL_FORMAT_RGBA16F) -> Any:
        """(H, W, 4) float 数组 → 可采样输入纹理（按像素格式量化）。"""
        height, width = rgba.shape[0], rgba.shape[1]
        np_dtype = np.dtype(PIXEL_FORMATS[pixel_format][1])
        data = np.asarray(rgba, dtype=np.float32)
        if np_dtype == np.uint8:
            # 归一化 8-bit：先裁到 [0, 1] 再四舍五入到码值（直接 cast 会把 [0, 1) 截成 0）。
            data = np.rint(np.clip(data, 0.0, 1.0) * 255.0)
        return self._context.create_texture_from_numpy(
            np.ascontiguousarray(data.astype(np_dtype)),
            self._pixel_format_const(pixel_format), width, height)

    def read_rgba(self, texture: Any, width: int, height: int, *,
                  pixel_format: str = PIXEL_FORMAT_RGBA16F) -> np.ndarray:
        """读回 (H, W, 4) float32（验证 / 诊断路径专用，不在逐帧热循环使用）。"""
        raw = self._context.read_texture(
            texture, width * 4, height, dtype=np.dtype(PIXEL_FORMATS[pixel_format][1]),
            region_width=width)
        return raw.reshape(height, width, 4).astype(np.float32)
