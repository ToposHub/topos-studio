# Batch-012 静态审查报告：src/shared/gpu 核心（8 文件）

- 审查日期: 2026-09-28
- 审查范围: `src/shared/gpu/` 下 `__init__.py`、`backend_selector.py`、`backend_types.py`、`capabilities.py`、`composite_gpu_frame_handle.py`、`gl_backend.py`、`gl_context_hub.py`、`gl_pass_timer.py`
- 问题编号区间: [AUD-1200–AUD-1219]
- 审查方法: 逐文件通读全部行；对关键疑点用只读 grep 交叉验证了调用方（`composite_viewport.py`、`gl_viewport.py`、`async_renderer.py`、`composite_renderer.py`、`edit_gpu_preview_renderer.py`、`timeline_preview_renderer.py`）。

## 批次摘要

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-1214 |
| Major | 4 | AUD-1207, AUD-1210, AUD-1211, AUD-1215 |
| Suggestion | 14 | AUD-1200–1206, AUD-1208, AUD-1209, AUD-1212, AUD-1213, AUD-1216–1219 |

Critical 概述: `gl_context_hub._do_pixel_probe` 声称"在 ctx_b 中验证 ctx_a 的纹理"，但 `ctx_b` 除判空外从未参与任何 GL 调用，探测结果完全取决于调用线程的隐含当前上下文；且唯一调用方（`composite_viewport._probe_context_sharing`）在 UI 线程 makeCurrent viewer 上下文后调用，一旦 renderer 上下文被注册，`ctx_a.texture()` / `tex.release()` 将在错误的线程与错误的上下文上执行（假阳性直接呈现 或 误删无关纹理）。

---

## src/shared/gpu/__init__.py (行数: 43)

### 发现

（无发现。）

### 通过项
- 导入的三个包内模块（`present_frame`、`gl_resource_registry`、`graphics_backend`）均实际存在（目录清单已核对），无悬空导入。
- `__all__` 与实际导入符号一一对应，无遗漏/多余项。
- 无运行时逻辑、无线程/资源风险面；模块边界（不 import features/app）未违反。

---

## src/shared/gpu/backend_selector.py (行数: 231)

### 发现

- [AUD-1200] [Suggestion] 非 macOS 平台 AUTO compute 命中后续候选时误报 `metal_compute_unavailable` 作为回退原因
  - 位置: src/shared/gpu/backend_selector.py:186-201
  - 证据:
    ```python
    for candidate in _PREFERENCES[dimension]:
        cap = _capability_for(capabilities, candidate)
        trail.append(candidate.value)
        if cap is not None and cap.available:
            if len(trail) == 1:
                ...
            return DimensionResolution(
                dimension=dimension, requested="auto",
                selected=candidate.value, available=True, forced=False,
                fallback_reason=fallback_reason or f"{trail[0]}_unavailable",
    ```
  - 影响: `_COMPUTE_PREFERENCE` 首选是 `METAL_COMPUTE`（行 34-39），但 `capabilities.compute` 的 kind 只会是 GLSL_COMPUTE/GLSL_FRAGMENT，`capabilities.platform` 也没有 `metal_compute` 键，故 cap 为 None、无 reason。在 Windows/Linux（GL>=4.3）上 AUTO 选中 `glsl_compute` 时 `trail[0]=="metal_compute"`，`diagnostics()` 会输出 `compute: auto -> glsl_compute (metal_compute_unavailable)`——对不存在的 Metal 选项的"回退"记录，污染 §4.3 fallback 日志的诊断语义。
  - 建议: 对 `cap is None`（从未探测的候选）与"探测后不可用"区分处理；或在生成 `trail[0]_unavailable` 前过滤掉本平台根本不存在的候选。
  - 置信度: 高（逻辑可直接从代码推演；未实际运行验证诊断文本）

- [AUD-1201] [Suggestion] 多个公共/内部函数参数缺少类型注解
  - 位置: src/shared/gpu/backend_selector.py:141, 165-169, 213
  - 证据:
    ```python
    def _capability_for(capabilities: BackendCapabilities, kind) -> Optional[DimensionCapability]:
    ...
    def _resolve_dimension(
        dimension: str,
        requested,
        forced: bool,
    ...
    def _is_auto(kind) -> bool:
    ```
  - 影响: `kind`/`requested` 实为各 `*BackendKind` 枚举联合，注解缺失降低可读性与静态检查覆盖（.clinerules 强类型要求）。
  - 建议: 补充 `kind: GraphicsBackendKind | ComputeBackendKind | ...` 类联合注解或公共基类。
  - 置信度: 高

### 通过项
- 显式请求不静默降级（`_resolve_dimension` 显式分支 honored-or-diagnose），符合 .clinerules:11。
- `resolve_backend_plan` 为纯函数（selection, capabilities）→ 结果，无隐藏状态，backend 切换时状态一致性由不可变 dataclass 保证。
- `environment` 中外部字符串（`metal_graphics_available`）读取采用 `str(probed).strip().lower() == "true"` 归一化（行 150-152），`get` 缺键返回 None 有判空，输入验证安全。
- 无异常吞噬路径、无 GPU 资源操作、无信号递归面。

---

## src/shared/gpu/backend_types.py (行数: 179)

### 发现

- [AUD-1202] [Suggestion] `from_dict` 对 `forced` 字段用 `bool()` 强转，字符串 `"false"` 会被判为 `True`
  - 位置: src/shared/gpu/backend_types.py:154-157
  - 证据:
    ```python
    return cls(
        graphics=g, compute=c, decode=d, interop=i,
        graphics_forced=gf or bool(forced.get("graphics")),
        compute_forced=cf or bool(forced.get("compute")),
        decode_forced=df or bool(forced.get("decode")),
        interop=iff or bool(forced.get("interop")),
    )
    ```
  - 影响: `forced` 若来自 YAML/手写 JSON 等非标准来源（值为 `"false"`、`"0"` 之外的任意非空串），`bool()` 恒真，会话被误标为 forced（回归/诊断语义被污染，可能触发 force 路径日志与行为差异）。`to_json` 自产数据是真布尔，不受影响。
  - 建议: 显式做布尔归一化（`isinstance(v, bool)` 或对字符串解析 `"true"/"false"`），非法值按 `parse_dimension` 同样原则抛 ValueError。
  - 置信度: 高（缺陷确定；触发依赖外部输入形态，概率低）

### 通过项
- `parse_dimension` 对未知值抛带候选清单的 ValueError，不静默降级，符合 .clinerules 配置错误必须暴露的原则。
- `force_` 前缀剥离逻辑正确（前缀长度切片，不做 replace）。
- 枚举均为 `str, Enum`，序列化/反序列化闭环一致；`from_env` 只读不写环境。
- frozen dataclass，不可变，无线程共享风险。

---

## src/shared/gpu/capabilities.py (行数: 464)

### 发现

- [AUD-1203] [Suggestion] `_major_minor` 对非数字开头的版本串抛未捕获 ValueError
  - 位置: src/shared/gpu/capabilities.py:153-161
  - 证据:
    ```python
        def _major_minor(v: str) -> tuple[int, int]:
            parts = ""
            for ch in v:
                if ch.isdigit() or ch == ".":
                    parts += ch
                else:
                    break
            nums = parts.split(".")
            return (int(nums[0]), int(nums[1]) if len(nums) > 1 else 0)
    ```
  - 影响: 桌面 GL 的 `GL_VERSION` 以数字开头（"4.1 Metal - 83" 解析为 (4,1)，正确）；但 GLES 上下文（"OpenGL ES 3.2 ..."）或异常驱动串会使 `parts==""`、`int("")` 抛 ValueError，且 `detect_compute_backend` / `detect_backend_capabilities` 均无 try 包裹，探测链路整体崩溃。
  - 建议: `nums` 为空或含空串时返回 (0,0) 并落入 reason 分支，或捕获后降级为 `no_live_gl_context` 类显式 reason。
  - 置信度: 中（崩溃路径确定；但 moderngl 桌面上下文的 GL_VERSION 是否可能为 ES 形态未验证）

- [AUD-1204] [Suggestion] Metal 设备探测在同一次 capabilities 快照内执行两次
  - 位置: src/shared/gpu/capabilities.py:113 与 459-461
  - 证据:
    ```python
    if _os_name(system) == "darwin":
        metal_available = _metal_device_available()
    ...
                "metal_graphics_available": str(
                    _metal_device_available() if _os_name(system) == "darwin" else False
                ).lower(),
    ```
  - 影响: darwin 无 GL 上下文时 `_metal_device_available()`（含 `MetalComputeContext.is_supported()` 导入与探测）在一次 `detect_backend_capabilities` 中跑两遍；若 is_supported 无内部缓存则为双倍探测开销。
  - 建议: 在 `detect_backend_capabilities` 内探测一次并复用结果（graphics 与 environment 共享）。
  - 置信度: 高（重复调用是代码事实；实际开销取决于 `is_supported` 是否有缓存，未核实）

- [AUD-1205] [Suggestion] `detect_backend_capabilities` 无任何缓存，每次调用全量重探（含解码真实媒体文件的 VT probe）
  - 位置: src/shared/gpu/capabilities.py:429-464（并见 213）
  - 证据:
    ```python
    def detect_backend_capabilities(
        selection: Optional[BackendSelection] = None,
        ...
    ) -> BackendCapabilities:
        """Snapshot all four dims; ``selection`` is recorded for diagnostics."""
        graphics = detect_graphics_backend(gl_version, gl_renderer, system=system)
        ...
    ```
    以及 darwin 分支的 `verdict = vt_hardware_decode_available(path, codec, bit_depth, width, height)`（行 213）。
  - 影响: docstring 自述 macOS 探测"decodes a real file"。若未来调用方（backend 切换 UI、每会话 resolve）高频调用，会产生可感知的 IO/GPU 探测延迟。grep 显示 `src/` 内当前无生产调用方，风险处于休眠状态。
  - 建议: 按参数键（system/path/codec/gl_version）做快照缓存，或在调用方约定低频调用并写入文档。
  - 置信度: 低（无生产调用方，实际调用频率未知）

- [AUD-1206] [Suggestion] `_metal_device_available` 吞掉全部异常，"不支持"与"探测失败"不可区分
  - 位置: src/shared/gpu/capabilities.py:128-133
  - 证据:
    ```python
    def _metal_device_available() -> bool:
        try:
            from src.shared.gpu.metal import MetalComputeContext
            return MetalComputeContext.is_supported()
        except Exception:
            return False
    ```
  - 影响: import 失败/内部错误与"硬件不支持 Metal"同样返回 False，上层 reason 固定为 `metal_compute_unavailable`，违背本模块自己的诊断规则（"unavailable or unprobed dimension is reported with an explicit reason"）——故障被报告为能力缺席。
  - 建议: 捕获后至少将异常摘要记入 reason（如 `metal_probe_error:<msg>`）或 log.warning。
  - 置信度: 高

### 通过项
- 输入验证整体良好：`gl_version` 空值走 static 分支并给显式 reason（`no_live_gl_context`）；Windows probe 失败给 `GPU_PROBE_FAILED` 参数化 reason；`probe_ok=True + reason="ok"` 的自相矛盾诊断已在行 262-266/348-353 显式处理。
- 各探测函数为纯函数式（参数进、dataclass 出），无共享可变状态，天然线程安全（并发调用只是重复计算，不竞争）。
- W0-03 非 Windows 平台条目统一 `not_windows` 显式 reason，无静默假阳性。
- `detect_graphics_backend` 的 `gl_renderer` 参数仅透传到 environment 记录，无解析风险。

---

## src/shared/gpu/composite_gpu_frame_handle.py (行数: 215)

### 发现

- [AUD-1207] [Major] `wait_fence` 的 `timeout_ns` 语义错误：非零值传给 `glWaitSync` 违反 GL 规范（timeout 必须为 GL_TIMEOUT_IGNORED）
  - 位置: src/shared/gpu/composite_gpu_frame_handle.py:193-204
  - 证据:
    ```python
    def wait_fence(fence_ns: int, timeout_ns: int = 0) -> bool:
        """Consumer-side: block until the producer's fence signals.

        Uses glWaitSync (server-side, no CPU stall beyond the GPU work itself).
        Returns False when the fence handle is invalid (0).
        """
        if not fence_ns:
            return False
        from OpenGL.GL import GL_TIMEOUT_IGNORED, glWaitSync
        glWaitSync(_int_to_sync(fence_ns), 0,
                   GL_TIMEOUT_IGNORED if not timeout_ns else timeout_ns)
        return True
    ```
  - 影响: OpenGL 规范要求 `glWaitSync` 的 timeout 参数必须是 `GL_TIMEOUT_IGNORED`，传入其它值产生 `GL_INVALID_VALUE`（且 glWaitSync 本身就是无限等待，timeout 不生效）。调用方一旦按参数名传入限时（如 `wait_fence(f, timeout_ns=1_000_000)`）期望超时返回，实际得到 GL 错误 + 依然无限阻塞，服务端等待可能挂起合成线程——正是该句柄契约要防的消费端停顿。附带风险（中置信度）：GLsync 对象不在共享对象集内，跨非共享上下文（producer/consumer 各持上下文）调用 `glWaitSync` 本身是未定义路径，契约需明确 fence 只能在创建它的上下文（或已同步归属后）等待。
  - 建议: 若需限时等待应改用 `glClientWaitSync(sync, 0, timeout_ns)`；保持 `glWaitSync` 则删除/忽略 `timeout_ns` 参数并在 docstring 写明无限等待语义。
  - 置信度: 高（glWaitSync 参数约束是规范事实；跨上下文 sync 归属问题为中）

- [AUD-1208] [Suggestion] `release()` 回调异常被静默吞掉且无日志，"恰好归还一次"契约失去可观测性
  - 位置: src/shared/gpu/composite_gpu_frame_handle.py:110-114
  - 证据:
    ```python
        if callback is not None:
            try:
                callback()
            except Exception:  # pragma: no cover - defensive
                pass
    ```
  - 影响: `_released` 已在锁内置 True；若回调半途失败，生产者池永久少一个纹理（泄漏）且无任何诊断线索，违反 P0-5 契约"returns to its pool exactly once"的可验证性。
  - 建议: 至少 `logger.warning` 记录异常；理想情况回调失败可被上层感知（返回值或计数器）。
  - 置信度: 高

- [AUD-1209] [Suggestion] `delete_fence` 吞掉 `glDeleteSync` 失败，GLsync 泄漏无诊断
  - 位置: src/shared/gpu/composite_gpu_frame_handle.py:207-215
  - 证据:
    ```python
    try:
        from OpenGL.GL import glDeleteSync
        glDeleteSync(_int_to_sync(fence_ns))
    except Exception:
        pass
    ```
  - 影响: fence 是驱动侧同步对象，删除失败（如错误线程/上下文）后无人重试也无人知晓，长会话下累积泄漏。
  - 建议: 记录 warning；并约定 delete_fence 只在生产者 GL 线程调用。
  - 置信度: 高

### 通过项
- 句柄生命周期核心设计正确：`release()` 锁内置位、锁外调用回调，幂等且线程安全；`released` 只读 bool 读取在 GIL 下原子。
- `_lock`/`_released`/`release_callback` 均标注 `compare=False`，不会进入 dataclass 相等比较；`as_dict` 排除回调与 fence 值，跨线程传递的 trace 快照安全。
- `is_presentable` 同时校验 available、texture_id>0、generation 匹配，stale-generation 防护符合 P0-5。
- `make_unavailable_handle` 显式 fallback，不冒充可用。
- `fence_to_int` 对 None/0/b""/"" 哨兵与 ctypes 指针/bytes/int 的归一化覆盖完整，非法类型返回 0（fail-closed）。
- 模块不依赖 Qt/moderngl，纯状态机，测试友好。

---

## src/shared/gpu/gl_backend.py (行数: 132)

### 发现

- [AUD-1210] [Major] `initialize` 完全忽略 `pyqt_context` 参数且无线程守卫，UI 线程调用即把 ModernGL 上下文绑到 UI 线程
  - 位置: src/shared/gpu/gl_backend.py:35-45
  - 证据:
    ```python
    def initialize(self, pyqt_context=None):
        """
        Initializes the ModernGL context using an existing PyQt OpenGL context.
        """
        if self.initialized and self.ctx:
            return

        # Create ModernGL context from existing context
        # If no context is provided, it tries to detect the current one
        self.ctx = moderngl.create_context()
        self.initialized = True
    ```
  - 影响: docstring 承诺"using an existing PyQt OpenGL context"，实现却丢弃参数、依赖 `moderngl.create_context()` 隐式探测当前线程上下文。已核实 UI 入口存在：`src/features/color/ui/gl_viewport.py:46` 在 QOpenGLWidget 内调用 `self.gl_manager.initialize(self.context())`——传入的 Qt context 无效化，实际包装的是调用瞬间 UI 线程的当前上下文，随后渲染线程经 `self.gl_manager.ctx`（image_engine.py:3065 等大量使用）跨线程操作同一上下文，正是 .clinerules 禁止的 GPU 线程边界破坏点。另外 `initialize` 与外部 `mutex` 使用者（async_renderer.py:424 等）之间无协同，初始化本身不在 GPU 命令锁内。
  - 建议: 明确单线程归属（初始化只在渲染线程执行并断言），删除误导性参数或真正使用它；`instance()` 增加记录创建线程并在跨线程访问 `ctx` 时告警。
  - 置信度: 高（参数未用与 UI 调用点均为代码事实；是否已因此产生实际故障未验证）

- [AUD-1211] [Major] `create_shared_context` 调用 `moderngl.create_context()`，不产生共享上下文
  - 位置: src/shared/gpu/gl_backend.py:47-49
  - 证据:
    ```python
    def create_shared_context(self):
        """Returns a new ModernGL context object for the current thread."""
        return moderngl.create_context()
    ```
  - 影响: ModernGL 的共享上下文正确创建方式是 `self.ctx.create_shared_context()`；`moderngl.create_context()` 只是重新探测/包装当前上下文（或新建 standalone），返回的对象与 `self.ctx` 之间无 share-list 保证。一旦调用方按名字信任该方法，得到的"共享"上下文实际不共享纹理——直接纹理呈现全部失效或读回黑帧。当前 grep 无调用方，缺陷处于休眠状态。
  - 建议: 改为 `self.ctx.create_shared_context()`，或直接删除该方法避免误用。
  - 置信度: 高（API 语义错误确定；无现行调用方使其暂未触发）

- [AUD-1212] [Suggestion] `cleanup()` 三处裸 `except:` 连 BaseException 一并吞掉
  - 位置: src/shared/gpu/gl_backend.py:118-131
  - 证据:
    ```python
        for fbo in self._fbo_cache.values():
            try: fbo.release()
            except: logger.debug("Failed to release FBO in cleanup")
        ...
        for tex in self._active_textures:
            try: tex.release()
            except: logger.debug("Failed to release active texture in cleanup")
    ```
  - 影响: 裸 `except:` 捕获 `KeyboardInterrupt`/`SystemExit`/`GeneratorExit`，退出路径上的 Ctrl-C 会被吞成 debug 日志；且吞掉的真实 GL 错误无异常内容。
  - 建议: 改为 `except Exception:` 并把异常文本写入日志。
  - 置信度: 高

- [AUD-1213] [Suggestion] 未使用的 `QOpenGLWidget` 导入
  - 位置: src/shared/gpu/gl_backend.py:5
  - 证据:
    ```python
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    ```
  - 影响: 全文件无任何引用；徒增模块加载依赖与 lint 噪音（shared/gpu 其余模块刻意不依赖 Qt widget 层，此行破坏该洁癖）。
  - 建议: 删除该导入。
  - 置信度: 高

### 通过项
- `instance()` 双检锁正确（GIL 下安全），注释如实说明了并发首次访问的动机。
- `mutex` 字段并非死代码：已核实 `async_renderer.py:424`、`composite_renderer.py:447/1004/2019`、`edit_gpu_preview_renderer.py:1209`、`timeline_preview_renderer.py:2447/3172/3313` 以 `GLContextManager.instance().mutex` 作为统一 GPU 命令通道锁，跨模块约定成立（虽然锁的正确性依赖调用方自律，gl_backend 自身方法不加锁是既有架构决定，不计为缺陷）。
- `FramebufferManager` 池化键 `(width, height, components, dtype)` 完整，`release_temp_texture` 归还键与创建键一致，不会串池。
- `create_texture`/`get_temp_texture` 默认 `repeat_x/y=False`（CLAMP_TO_EDGE）+ LINEAR，与 opengl_render_backend.py:133 的注释约定一致。
- 未发现信号递归面（无 Qt 信号）。

---

## src/shared/gpu/gl_context_hub.py (行数: 120)

### 发现

- [AUD-1214] [Critical] `_do_pixel_probe` 从未在 ctx_b 上做任何验证：无上下文切换、无线程守卫，探测结果取决于调用线程的隐含当前上下文，且 `tex.release()` 会在当前上下文删除纹理
  - 位置: src/shared/gpu/gl_context_hub.py:77-105
  - 证据:
    ```python
        # Write a 2x2 test texture on ctx_a
        tex = ctx_a.texture(size=(2, 2), components=4, dtype="f1")

        # Verify from ctx_b: can we query the texture size?
        try:
            w = ctypes.c_int()
            _gl.glBindTexture(_gl.GL_TEXTURE_2D, tex.glo)
            _gl.glGetTexLevelParameteriv(
                _gl.GL_TEXTURE_2D, 0, _gl.GL_TEXTURE_WIDTH, ctypes.byref(w)
            )
            _gl.glBindTexture(_gl.GL_TEXTURE_2D, 0)
            return w.value == 2
        except Exception:
            return False
        finally:
            tex.release()
    ```
  - 影响: (1) `ctx_b` 除 `probe_sharing` 行 58 的判空外从未参与任何 GL 调用——PyOpenGL 的 `glBindTexture`/`glGetTexLevelParameteriv` 作用于**调用线程的当前上下文**，代码既不 make_current ctx_b 也不做任何线程断言，"verify from ctx_b" 完全依赖调用方碰巧在 ctx_b 线程内调用。(2) 已核实唯一调用链 `composite_viewport._probe_context_sharing`（composite_viewport.py:11403-11413）在 UI 线程 `self.makeCurrent()`（viewer 上下文）后调用 `hub.probe_sharing("composite_renderer", "composite_viewer")`；一旦 renderer 上下文被注册（目前 grep 显示 "composite_renderer" 从未注册，故暂走 `ctx_a is None → False` 负缓存，尚未触发本路径），`ctx_a.texture(...)` 将在 UI 线程/错误上下文上创建渲染线程上下文的纹理，探测恒为假阳性（当前上下文查询刚创建的名字必然宽=2）或引发未定义行为；`finally` 的 `tex.release()` 经当前（viewer）上下文 `glDeleteTextures`，若两上下文不共享则删除 viewer 中同名无关纹理——数据破坏。假阳性会直接开启 `is_direct_texture_enabled` 的直接纹理呈现，产生渲染错误/黑帧。(3) 该方法位于 shared/gpu 公共层且无文档声明线程契约，是典型的 UI 线程操作 GPU 入口。
  - 建议: 探测逻辑改为由各上下文拥有线程分段执行（producer 线程建纹理→consumer 线程查询），或显式传入两个上下文的 make_current 回调；入口加线程/上下文断言；`tex.release()` 必须回到 ctx_a 的线程执行。
  - 置信度: 高（ctx_b 未被使用是代码事实；调用方 UI 线程形态已核实；"当前未触发"因 renderer 上下文未注册）

- [AUD-1215] [Major] 单例与共享 dict 无任何同步保护
  - 位置: src/shared/gpu/gl_context_hub.py:27-38, 41-60, 111-120
  - 证据:
    ```python
    @classmethod
    def instance(cls) -> "GLContextHub":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register_context(self, name: str, ctx: Any) -> None:
        ...
        self._contexts[name] = ctx
    ```
  - 影响: 与同包 `GLContextManager.instance()` 的双检锁形成对比，此处连实例创建都无锁；`_contexts`/`_sharing_cache` 的读写（UI 线程 register + 任意线程 probe）无锁并发下可产生撕裂视图（例如 `_invalidate_cache_for` 与 `probe_sharing` 的缓存写入交错，留下过期 True 判定 → 直接呈现决策错误）。
  - 建议: 对齐 `GLContextManager` 的 `_instance_lock` 模式，方法内用同一把 `threading.Lock` 保护两个 dict。
  - 置信度: 高（无同步原语是事实；实际并发频率取决于调用方，当前调用点单一，风险以架构层面为主）

- [AUD-1216] [Suggestion] 注册的上下文持强引用且无 unregister，上下文销毁后 hub 残留死引用
  - 位置: src/shared/gpu/gl_context_hub.py:32-43, 117-120
  - 证据:
    ```python
    def register_context(self, name: str, ctx: Any) -> None:
        """Register a GL context with a human-readable name."""
        old = self._contexts.get(name)
        if old is not None and old is not ctx:
            self._invalidate_cache_for(name)
        self._contexts[name] = ctx
    ...
    def reset(self) -> None:
        """Clear all registrations and cache (for testing or context teardown)."""
    ```
  - 影响: 上下文重建（context rebuild/device loss 是 P0-5 明确场景）后旧 ctx 若未 `reset()`（其 docstring 自述仅供测试）则永久滞留：`get_context` 返回已 release 的死上下文，后续 probe 在死对象上抛异常并负缓存 False。
  - 建议: 提供 `unregister_context(name)`；或持有弱引用 + 探测时校验存活。
  - 置信度: 高

### 通过项
- 共享结果缓存键 `tuple(sorted([name_a, name_b]))` 对称化处理正确，A→B 与 B→A 复用同一缓存项。
- `register_context` 同名换实例时主动失效相关缓存（`_invalidate_cache_for`），上下文替换的缓存一致性有考虑。
- probe 异常 fail-closed 返回 False（不会把异常抛进呈现决策路径），探测否定不冒充共享。
- 无 GPU 资源长期持有（探针纹理 finally 释放——尽管释放线程有 AUD-1214 的问题，释放意图存在）。

---

## src/shared/gpu/gl_pass_timer.py (行数: 178)

### 发现

- [AUD-1217] [Suggestion] 模块 docstring 引用不存在的 `last_composite_ms` API
  - 位置: src/shared/gpu/gl_pass_timer.py:25（对照 116-124）
  - 证据:
    ```python
    timer.collect()               # 读回上一帧的 query 结果
    ms = timer.last_composite_ms  # None = gpu_time_unavailable
    ```
    而类中实际定义的是：
    ```python
    def last_ms(self, pass_name: str) -> Optional[float]:
    ```
  - 影响: 照文档使用 `timer.last_composite_ms` 会得到 AttributeError；文档示例与实现脱节误导接入方。
  - 建议: docstring 改为 `ms = timer.last_ms("composite")`，或补一个 `last_composite_ms` 便利属性。
  - 置信度: 高

- [AUD-1218] [Suggestion] query `__enter__` 抛出时对象不释放（构造探测与每帧测量两条路径）
  - 位置: src/shared/gpu/gl_pass_timer.py:61-68, 151-171
  - 证据:
    ```python
        try:
            probe = ctx.query(time=True)
            probe.__enter__()
            probe.__exit__(None, None, None)
            probe.release()
        except Exception as e:
            self._unsupported = True
    ```
    以及：
    ```python
        try:
            query = timer._ctx.query(time=True)
            query.__enter__()
            self._query = query
    ...
        except Exception:
            self._query = None
        return self
    ```
  - 影响: 若 `query()` 成功但 `__enter__()`（即 glBeginQuery）抛出，query 对象已创建却不 release，GL query 名泄漏；`_PassMeasurement` 路径下该帧测量静默丢失（`_query=None`）且无日志。
  - 建议: except 分支中对已创建的 query 补 `release()`（try/finally 结构）。
  - 置信度: 高（泄漏路径确定；`__enter__` 实际抛出概率低）

- [AUD-1219] [Suggestion] 每个 pass 每帧新建 query 对象，无池化
  - 位置: src/shared/gpu/gl_pass_timer.py:152-156
  - 证据:
    ```python
        try:
            query = timer._ctx.query(time=True)
            query.__enter__()
            self._query = query
            self._seq = timer._frame_seq
            timer._open.setdefault(self._name, []).append((self._seq, query))
    ```
  - 影响: 4K/60fps 下每帧每 pass 一次 `glGenQueries`/`glDeleteQueries` + Python 对象分配。相对合成 pass 本身开销小（非热点），但属可消除的每帧固定成本；ring 结构本身已保证同时只有 RING_DEPTH 个 in-flight query，天然适合预分配池。
  - 建议: 初始化时按 `RING_DEPTH × pass 数` 预建 query 池，measure 从池中取、collect 归还。
  - 置信度: 高（重复分配是事实；性能影响评级为次要）

### 通过项
- 生命周期管理总体正确：ring 中每个 query 在 `collect()` 读回后 release（含异常路径 finally），`release()` 释放全部 in-flight query，`__enter__` 的 ring 溢出防御（RING_DEPTH*4 上限 + 丢弃最旧 + warn-once）能有效兜住"collect 未被调用"的泄漏场景。
- 延迟一帧读回设计（RING_DEPTH-1）避免 glGetQueryObject 阻塞被测帧，符合 P0-03 目标；不支持时 `unsupported=True` fail-closed（所有 last_ms 返回 None，不冒充 0.0），无宽泛吞错导致的假数据。
- 线程契约在 docstring 明确（仅 GL 线程、实例无需加锁），与实际单线程使用模式一致。
- `ns >= 0` 校验 + None 区分"无数据"与 0.0ms，消费方语义正确。
- 类型注解完整（Dict/Optional）。

---

## 批次级横向观察（不计入编号，供后续批次/修复排期参考）

1. shared/gpu 内存在三套并行的上下文管理入口（`GLContextManager`、`GLContextHub`、`graphics_backend.GraphicsBackend`），锁策略互不一致（GLContextManager 有 mutex 供外部手动加锁、GLContextHub 完全无锁）。backend 切换/上下文重建时的状态一致性依赖各自调用方，建议统一收敛到 GraphicsBackend 契约层。
2. `CompositeGpuFrameHandle`、`GlPassTimer`、`capabilities`/`backend_selector` 均未从 `src/shared/gpu/__init__.py` 导出，消费方只能深路径导入（已核实现有调用方确实深路径 import），API 面收口时机可再评估。
3. AUD-1214 的触发条件（"composite_renderer" 上下文注册）与调用方 `composite_viewport.py` 属于 features 层，建议在该文件所属批次同步修复。
