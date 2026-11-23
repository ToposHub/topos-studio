"""R08: 逐节点已处理缩略图（node-output demand → 低分辨率图执行 → GUI 投递）。

架构（计划 R08）：

- **demand 驱动**：只评估可见 Corrector/Compound 卡片请求的节点输出
  （上限预算），**不逐帧同步 readback 4K 节点**——按（图 revision + 资源
  指纹 + 源身份 + 尺寸）缓存，去抖合并 demand。
- **独立 worker 线程，GUI 线程零 GL 调用**。低分辨率（默认 320×180）经
  既有 ``compile_graph``（viewer target = ``viewer:<node_id>``，依赖锥自然
  裁剪）执行；两个后端，会话开关 ``TOPOS_COLOR_METAL=1`` 选定（Metal
  迁移 4.6）：
  - GL：每个任务一个 standalone GL 上下文 + ``ColorGraphExecutor``；
    LUT3D / 曲线 / HSL 曲线 aux 纹理由本评估器按内容缓存提供（含 R05
    强度规格路径的 u_lut_intensity uniform），插件经 ShaderPipeline 正常
    编译执行。
  - Metal：worker 线程自持一个 Metal 上下文与 ``MetalNodeThumbnailService``
    （阶段 4 共享图入口），不建任何 GL 上下文；Metal 不可用、被
    ``TOPOS_METAL_DISABLE=color_graph_metal`` 关闭或出错时回到 GL。
- **缓存/失效**：键 = (node_id, graph_revision, size, backend)；改上游
  节点 → 依赖节点 revision 变化自然失效重算（独立分支不受影响——它们的
  键未变，直接命中缓存）。两个后端的图不互认缓存。
- **世代守卫**：切 clip/换会话 generation+1，在途与晚到结果全部丢弃；
  删节点后回调按 node_id 查无卡片即忽略。失败/未评估 → 保留源缩略图
  fallback（视图层已保证 per-node 缩略图不覆盖、源图不被冒充）。
- **与 R07 分离**：本通道只做 node-output；LUT 浏览器候选图
  （``lut_thumbnail_service``）绝不混入节点卡片。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Iterable, Mapping

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap

from src.shared.gpu.metal.feature_switches import MetalFeature, opt_in_enabled

import logging

_LOGGER = logging.getLogger(__name__)


class ColorNodeOutputPreview(QObject):
    """可见节点卡片的已处理输出缩略图（异步、限频、有界缓存）。"""

    node_thumbnail_ready = pyqtSignal(str, QImage)     # node_id, 已评估输出
    evaluation_failed = pyqtSignal(str, str)           # node_id, 错误码

    def __init__(self, *, size: tuple[int, int] = (320, 180),
                 max_nodes: int = 12, cache_size: int = 96,
                 debounce_ms: int = 350) -> None:
        super().__init__()
        self._size = tuple(size)
        self._max_nodes = int(max_nodes)
        self._cache_size = int(cache_size)
        self._lock = threading.RLock()
        self._cache: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._generation = 0
        self._identity: tuple = ("", "", 0)
        self._demand: tuple[str, Iterable[str]] | None = None
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(int(debounce_ms))
        self._debounce_timer.timeout.connect(self._dispatch)
        self._worker: threading.Thread | None = None
        self._queue: tuple | None = None
        self._wakeup = threading.Event()
        self._shutdown = False
        self._last_dispatch = 0.0
        # Metal 路径（仅 worker 线程读写）：(线程 id, 服务)；线程换过就重建
        self._metal: tuple | None = None
        self._metal_failure: str | None = None     # set once Metal failed: GL from then on
        self._jobs_gl = 0
        self._jobs_metal = 0

    # ── demand（GUI 线程调用） ─────────────────────────────────────
    def set_identity(self, session_id: str, stack_id: str,
                     revision: int) -> None:
        """更新会话身份；**身份未变时幂等**（不推进世代、不清 demand）——
        周期驱动方可以安全地每拍调用。"""
        incoming = (str(session_id), str(stack_id), int(revision))
        with self._lock:
            if incoming == self._identity:
                return
            self._generation += 1
            self._identity = incoming
            self._demand = None
        self._debounce_timer.stop()

    def request(self, graph_payload_provider: Callable[[], Mapping[str, Any]
                                                         | None],
                source_provider: Callable[[], np.ndarray | None],
                node_ids: Iterable[str]) -> None:
        """登记一次 demand（去抖合并；worker 里才取图/源）。"""
        node_list = [str(n) for n in node_ids][:self._max_nodes]
        if not node_list:
            return
        with self._lock:
            generation = self._generation
            identity = self._identity
            self._demand = (generation, identity, node_list,
                            graph_payload_provider, source_provider)
        self._debounce_timer.start()

    def _dispatch(self) -> None:
        with self._lock:
            demand = self._demand
            self._demand = None
        if demand is None or self._shutdown:
            return
        _LOGGER.debug("node thumbnail dispatch: %d nodes", len(demand[2]))
        generation, identity, node_list, graph_provider, source_provider = demand
        backend, _why = _thumbnail_backend()
        with self._lock:
            # Metal 出过错之后任务都按 GL 评估、按 GL 的键缓存：查缓存要用同一个键
            if backend == "metal" and self._metal_failure is not None:
                backend = "gl"
        # 缓存命中先行（GUI 线程直接投递）
        pending_nodes: list[str] = []
        for node_id in node_list:
            cached = self._cache_get(self._cache_key(node_id, identity, backend))
            if cached is not None:
                self.node_thumbnail_ready.emit(node_id, cached.toImage())
            else:
                pending_nodes.append(node_id)
        if not pending_nodes:
            return
        # 限频：距上次派发太近则顺延一拍（去抖定时器会因新 demand 重启）
        now = time.monotonic()
        if now - self._last_dispatch < 0.25:
            with self._lock:
                self._demand = (generation, identity, pending_nodes,
                                graph_provider, source_provider)
            self._debounce_timer.start()
            return
        self._last_dispatch = now
        self._ensure_worker()
        with self._lock:
            self._queue = (generation, identity, pending_nodes,
                           graph_provider, source_provider, backend)
        self._wakeup.set()

    def _cache_key(self, node_id: str, identity: tuple, backend: str) -> tuple:
        """One thumbnail per node, revision, size and backend (two backends never share a picture)."""
        return (node_id, identity[2], self._size, backend)

    # ── worker（独立线程 + 独立 GL 上下文） ────────────────────────
    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="node-thumbnail-evaluator")
        self._worker.start()

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
        self._wakeup.set()

    def _run(self) -> None:
        while True:
            self._wakeup.wait()
            self._wakeup.clear()
            while True:
                with self._lock:
                    if self._shutdown:
                        self._release_metal()
                        return
                    job = self._queue
                    self._queue = None
                    generation = self._generation
                if job is None:
                    break
                if job[0] != generation:
                    continue  # 世代已过：丢弃
                try:
                    self._evaluate_job(job)
                except Exception as exc:  # noqa: BLE001 — 失败按节点可见
                    for node_id in job[2]:
                        self._emit_failed(node_id, generation,
                                          str(exc)[:120] or
                                          type(exc).__name__)

    def _emit_ready(self, node_id: str, generation: int, image: QImage) -> None:
        with self._lock:
            stale = self._shutdown or generation != self._generation
        if stale:
            return
        self.node_thumbnail_ready.emit(node_id, image)

    def _emit_failed(self, node_id: str, generation: int, code: str) -> None:
        with self._lock:
            stale = self._shutdown or generation != self._generation
        if stale:
            return
        _LOGGER.warning("node thumbnail failed: %s (%s)", node_id, code)
        self.evaluation_failed.emit(node_id, code)

    def _cache_get(self, key: tuple) -> QPixmap | None:
        with self._lock:
            pixmap = self._cache.get(key)
            if pixmap is not None:
                self._cache.move_to_end(key)
            return pixmap

    def _cache_put(self, key: tuple, pixmap: QPixmap) -> None:
        with self._lock:
            self._cache[key] = pixmap
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    def _evaluate_job(self, job: tuple) -> None:
        from src.shared.project.color_graph import ColorGraphData
        _generation, identity, node_list, graph_provider, source_provider, backend = job
        graph_payload = graph_provider()
        source = source_provider()
        if not isinstance(graph_payload, Mapping) or source is None:
            for node_id in node_list:
                self._emit_failed(node_id, _generation, "no_input")
            return
        graph = ColorGraphData.from_dict(graph_payload)
        # Compound 目标重映射：编译器把 compound 内联展开（成员直入平面图），
        # 其输出等价于内部 output 节点的上游——编译目标落在内部链尾。
        # 投递/缓存必须保持请求方（卡片）id：GUI 侧按 card.node.id 查收，
        # 内部 id 外泄会让 compound 卡片永远收不到缩略图，且缓存键
        # （dispatch 按请求 id 查）永不命中 → 每 600ms 全量重评（V01）。
        targets: list[tuple[str, str]] = []
        resolved_seen: set[str] = set()
        for requested in node_list:
            resolved = _resolve_viewer_node(graph_payload, requested)
            if resolved and resolved not in resolved_seen:
                resolved_seen.add(resolved)
                targets.append((requested, resolved))
        source = _resize_to(source, *self._size)
        if backend == "metal":            # dispatch 把出过错之后的任务都派给 GL
            self._jobs_metal += 1
            try:
                self._evaluate_on_metal(_generation, identity, targets, graph, source)
                return
            except Exception as exc:  # noqa: BLE001 — 缩略图照样要送到：这个任务起改走 GL
                self._metal_failure = f"{type(exc).__name__}: {exc}"[:200]
                _LOGGER.warning(
                    "node thumbnail: Metal evaluation failed, using OpenGL from "
                    "now on (%s)", self._metal_failure)
                self._release_metal()
            # 这个任务按 GL 的键缓存，图与 GL 的一致
            backend = "gl"
        self._jobs_gl += 1
        self._evaluate_on_gl(_generation, identity, targets, graph, source, backend)

    def _evaluate_on_metal(self, generation: int, identity: tuple,
                           targets: list[tuple[str, str]], graph, source: np.ndarray) -> None:
        """Metal：worker 线程自持的服务逐节点出图，不建 GL 上下文。

        图里不支持的域（如 GLSL 插件效果）由服务按节点给出诊断——不拿
        源图顶替，也不回头再走 GL。
        """
        service = self._metal_service()
        rgba = np.concatenate(
            [source[..., :3], np.ones(source.shape[:2] + (1,), np.float32)], axis=-1)
        results = service.evaluate(
            graph, rgba, tuple(resolved for _requested, resolved in targets),
            identity=identity)
        for requested, resolved in targets:
            picture, outcome = results[resolved]
            if picture is None:
                self._emit_failed(requested, generation, outcome)
                continue
            image = _rgba8_to_qimage(picture)
            self._cache_put(self._cache_key(requested, identity, "metal"),
                            QPixmap.fromImage(image))
            self._emit_ready(requested, generation, image)

    def _metal_service(self):
        """本线程的 Metal 服务（首次使用时建；只在 worker 线程调用）。

        Metal 上下文与服务跟着创建它们的线程走：worker 线程换过了就重建。
        """
        from src.features.color.core.color_graph_entry import SharedColorGraphEntry
        from src.features.color.core.metal_node_thumbnail import (
            MetalNodeThumbnailService,
        )
        from src.shared.gpu.metal.context import MetalComputeContext
        thread_id = threading.get_ident()
        if self._metal is None or self._metal[0] != thread_id:
            self._metal = (thread_id, MetalNodeThumbnailService(
                SharedColorGraphEntry(MetalComputeContext())))
        return self._metal[1]

    def _release_metal(self) -> None:
        """Drop the Metal service (on the worker thread, before it exits or after a failure)."""
        self._metal = None

    def _evaluate_on_gl(self, generation: int, identity: tuple,
                        targets: list[tuple[str, str]], graph, source: np.ndarray,
                        backend: str) -> None:
        """GL：每个任务一个 standalone 上下文。``backend`` 是缓存键里的名字
        （Metal 出错后的任务用 ``"gl"``）。"""
        import moderngl
        from src.features.color.core.color_graph_compiler import compile_graph
        from src.features.color.core.color_graph_executor import ColorGraphExecutor
        from src.features.color.core.color_graph_snapshot import freeze_graph
        from src.features.color.core.shader_compiler import ShaderPipeline
        width, height = self._size
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx, pipeline=ShaderPipeline(ctx))
            lut_cache: dict[str, Any] = {}

            def bind_aux(prog, layer, _cache=lut_cache):
                _bind_aux_with_lut(ctx, prog, layer, _cache)

            src_tex = ctx.texture((width, height), components=4, dtype="f2",
                                  data=_to_f16_rgba(source))
            for requested, resolved in targets:
                snapshot = freeze_graph(
                    graph, session_id=identity[0] or "node-thumb",
                    viewer_target=f"viewer:{resolved}", output_size=self._size)
                plan = compile_graph(snapshot, size=self._size)
                result = executor.execute_plan(
                    plan, src_tex, request_id=f"thumb-{requested}",
                    source_size=self._size,
                    layer_param_provider=_layer_provider(graph),
                    bind_aux=bind_aux)
                if not result.published or result.output_texture is None:
                    self._emit_failed(requested, generation,
                                      result.diagnostic_code or "not_published")
                    executor.release_revision(f"thumb-{requested}")
                    continue
                fbo = ctx.framebuffer(color_attachments=[result.output_texture])
                fbo.use()
                raw = np.frombuffer(fbo.read(components=4, dtype="f1"),
                                    dtype=np.uint8).reshape(height, width, 4)
                image = _rgba8_to_qimage(raw)
                fbo.release()
                executor.release_revision(f"thumb-{requested}")
                self._cache_put(self._cache_key(requested, identity, backend),
                                QPixmap.fromImage(image))
                self._emit_ready(requested, generation, image)
            src_tex.release()
            executor.release_pool()
        finally:
            ctx.release()

    def stats(self) -> dict[str, Any]:
        """Cache size, generation, how many jobs each backend ran, and why Metal gave up (if it did)."""
        with self._lock:
            return {"cached": len(self._cache), "generation": self._generation,
                    "jobs_gl": self._jobs_gl, "jobs_metal": self._jobs_metal,
                    "metal_failure": self._metal_failure}


def _thumbnail_backend() -> tuple[str, str]:
    """``(backend, reason)``：本会话的节点缩略图用 ``"metal"`` 还是 ``"gl"``。

    调色域的会话开关（``TOPOS_COLOR_METAL=1``）开启且有 Metal 设备时用 Metal；
    否则 GL，``reason`` 说明为什么是 GL。
    """
    enabled, reason = opt_in_enabled(MetalFeature.COLOR_GRAPH_TEXTURE)
    if not enabled:
        return "gl", reason
    from src.shared.gpu.metal.context import MetalComputeContext

    ok, why = MetalComputeContext.availability_reason()
    if not ok:
        return "gl", f"metal_unavailable:{why}"
    return "metal", ""


def _resolve_viewer_node(payload: Mapping[str, Any], node_id: str) -> str:
    """viewer 目标解析：compound → 其 children 图的 output 上游节点。"""
    container = payload
    while True:
        node = next((n for n in container.get("nodes", ())
                     if isinstance(n, dict) and n.get("node_id") == node_id), None)
        if node is None:
            return ""
        children = node.get("compound_children")
        if not isinstance(children, Mapping):
            return node_id
        inner_output = children.get("output_node_id")
        incoming = next((e for e in children.get("edges", ())
                         if isinstance(e, dict)
                         and e.get("dst_node") == inner_output), None)
        if incoming is None:
            return ""
        node_id = str(incoming.get("src_node"))
        container = children


def _resize_to(source: np.ndarray, width: int, height: int) -> np.ndarray:
    """源低分辨率帧 → 缩略图尺寸（bilinear；worker 线程，无 GUI）。"""
    arr = np.asarray(source, dtype=np.float32)
    if arr.shape[0] == height and arr.shape[1] == width:
        return arr
    from scipy.ndimage import zoom
    factors = (height / arr.shape[0], width / arr.shape[1], 1.0)
    return zoom(arr, factors, order=1).astype(np.float32)


def _layer_provider(graph) -> Callable[[str], Mapping[str, Any] | None]:
    from src.features.color.core.color_graph_param_source import (
        graph_node_layer_provider,
    )
    provider = graph_node_layer_provider(graph)

    def provide(node_id: str):
        payload = provider(node_id)
        if isinstance(payload, Mapping):
            payload = dict(payload)
            payload.pop("_plugin_bindings", None)  # 真实注册表路径
            payload = _with_hsl_curve_lut(payload)
        return payload
    return provide


def _with_hsl_curve_lut(payload: dict) -> dict:
    """HSL 曲线的 LUT 由控制点重建（图层只持久化控制点，LUT 是渲染侧缓存）。

    GL 图着色器只凭 ``hsl_curve_lut`` 判定 HSL 曲线是否生效；CPU 与 Metal
    图后端在 LUT 缺失时自己按数据重建。评估器不补的话，带 HSL 曲线的节点
    缩略图看起来像没调过。
    """
    if payload.get("hsl_curve_lut") is None and payload.get("hsl_curve_data"):
        from src.features.color.core.layer import build_hsl_curve_lut_from_data
        payload["hsl_curve_lut"] = build_hsl_curve_lut_from_data(
            dict(payload["hsl_curve_data"]))
    return payload


def _curve_array(layer) -> np.ndarray:
    """The layer's curve as ``(256, 3)`` in [0, 1]; a straight line when it has none."""
    lut = layer.get("curves_lut") if isinstance(layer, Mapping) else None
    if lut is None:
        ramp = np.linspace(0.0, 1.0, 256, dtype=np.float32)
        return np.stack([ramp] * 3, axis=-1)
    return np.asarray(lut, dtype=np.float32).reshape(256, 3)


def _hsl_curve_array(layer) -> np.ndarray:
    """The layer's six HSL curves as ``(256, 6)`` -- one texture row per sample
    point, as the production renderer lays the (6, 256) LUT out; all defaults
    (multipliers 1, hue shift 0) when it has none."""
    lut = layer.get("hsl_curve_lut") if isinstance(layer, Mapping) else None
    if lut is None:
        lut = np.ones((6, 256), dtype=np.float32)
        lut[1] = 0.0                                   # HueHue is a shift
    return np.asarray(lut, dtype=np.float32).reshape(6, 256).T.copy()


def _aux_texture(ctx, cache: dict, name: str, size: tuple, components: int, data: np.ndarray):
    """A float texture of ``data``, one per distinct content (the cache lives as long as the context)."""
    payload = np.ascontiguousarray(data, dtype=np.float32).tobytes()
    key = f"{name}:{hash(payload)}"
    tex = cache.get(key)
    if tex is None:
        tex = ctx.texture(size, components, data=payload, dtype="f4")
        tex.filter = (ctx.LINEAR, ctx.LINEAR)
        tex.repeat_x = tex.repeat_y = False
        cache[key] = tex
    return tex


def _bind_aux_with_lut(ctx, prog, layer, cache: dict) -> None:
    """One layer's auxiliary textures in the production order.

    The same four as ``AsyncRenderer._bind_aux_textures_single_layer``: the
    layer's own curves and HSL curves (a straight line / the defaults when it
    has none), the neutral warper (the evaluator has no warper textures, so a
    warper shows unwarped rather than wrong) and its LUT3D.  Every curve used
    to be bound as a straight line, so a node's thumbnail never showed its
    curve, and the other two samplers were left to whatever unit 0 held.

    LUT 纹理数据与生产 ``_load_lut_sync`` 同构（重采样单位域、f4 字节序）；
    强度 uniform（u_lut_intensity）由执行器通用 uniform 上传路径按
    layer.lut_intensity 设置（R05 投影）。
    """
    unit = 2
    mapping = layer if isinstance(layer, Mapping) else {}
    for name, size, components, data in (
            ("u_curves_tex", (256, 1), 3, _curve_array(mapping)),
            ("u_hsl_curve_tex", (6, 256), 1, _hsl_curve_array(mapping)),
            ("u_warper_tex", (2, 2), 2, np.zeros((2, 2, 2), dtype=np.float32))):
        if name in prog:
            _aux_texture(ctx, cache, name, size, components, data).use(unit)
            prog[name].value = unit
            unit += 1
    if "u_lut_tex" in prog:
        lut_path = mapping.get("active_lut_path")
        if not (isinstance(lut_path, str) and lut_path):
            raise RuntimeError("aux_texture_missing: LUT shader 无 LUT 引用")
        tex = cache.get(lut_path)
        if tex is None:
            from src.shared.color.lut_loader import (
                load_lut, resample_to_unit_domain,
            )
            loaded = resample_to_unit_domain(load_lut(lut_path))
            arr = np.ascontiguousarray(loaded.table, dtype="f4").tobytes()
            tex = ctx.texture3d((loaded.size,) * 3, 3, data=arr, dtype="f4")
            cache[lut_path] = tex
        tex.use(unit)
        prog["u_lut_tex"].value = unit


def _to_f16_rgba(source: np.ndarray) -> np.ndarray:
    arr = np.asarray(source, dtype=np.float32)
    if arr.ndim != 3 or arr.shape[-1] < 3:
        raise ValueError("bad source shape")
    if arr.shape[-1] == 3:
        alpha = np.ones(arr.shape[:2] + (1,), dtype=np.float32)
        arr = np.concatenate([arr, alpha], axis=-1)
    return arr[..., :4].astype(np.float16)


def _rgba8_to_qimage(raw: np.ndarray) -> QImage:
    """RGBA8 缓冲 → RGB888 QImage（copy 脱离 numpy 缓冲所有权）。"""
    h, w = raw.shape[:2]
    rgb = np.ascontiguousarray(raw[..., :3])
    image = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
    return image.copy()
