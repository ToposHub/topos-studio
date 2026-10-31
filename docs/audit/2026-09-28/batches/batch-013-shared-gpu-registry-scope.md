# Batch 013 — shared/gpu 注册表与示波器引擎静态审查

- 日期：2026-09-28
- 范围：`src/shared/gpu/` 下 4 个文件（gl_resource_registry.py / gpu_scope_engine.py / graphics_backend.py / host_gpu_info.py）
- 问题编号区间：AUD-1300 – AUD-1399
- 方法：逐行 Read 全部 4 个文件；为核实严重度另做了只读佐证检查（`src/features/color/core/scope_types.py` 的模式谓词、`src/features/color/shaders/gpu_scope.glsl` 的采样 clamp、两处 `ScopeDemand.analysis_size` 构造点）。佐证文件本身属其它批次，本文仅引用不立案。
- 汇总：Critical 0 / Major 1 / Suggestion 8

---

## src/shared/gpu/gl_resource_registry.py (行数: 156)

### 发现

- [AUD-1300] [Major] release_fn 抛异常时台账条目已被 pop，泄漏资源从登记表中消失且无法重试
  - 位置: src/shared/gpu/gl_resource_registry.py:127（配合 132-141）
  - 证据:
    ```python
    with self._lock:
        group_records = self._records.pop(group, {})
    released: List[str] = []
    for name, record in reversed(list(group_records.items())):
        if record.release_fn is None:
            continue
        try:
            record.release_fn()
            released.append(name)
        except Exception:
    ```
  - 影响: GL 调用失败（上下文丢失/驱动错误）时资源并未真正释放，但其登记已在回调前整体 pop 掉；后续 `release_all` / `snapshot` 诊断再也看不到该资源，deferred-release 机制在异常路径同时失去释放与可观测性（连"哪些没放掉"都查不到）。
  - 建议: 回调失败时将条目重新插回 `self._records`（或移入独立的 failed 列表供诊断/重试），再继续同组其余释放。
  - 置信度: 高（行为由代码确定。注意：按文件头注释，P3-02/03 阶段 AsyncRenderer 默认走 release_fn=None 纯台账模式，当前未触发；风险在 Phase 4 backend 切换启用 release_fn 后落地，故仍定 Major）

- [AUD-1301] [Suggestion] register 覆盖同名条目时静默丢弃旧 release_fn，旧资源成孤儿
  - 位置: src/shared/gpu/gl_resource_registry.py:74、87
  - 证据:
    ```python
    """登记（或覆盖登记）一个资源。同组同名以最后一次登记为准。"""
    ...
    with self._lock:
        self._records.setdefault(group, {})[name] = record
    ```
  - 影响: 若调用方在未释放旧资源前重复 register 同组同名，旧条目的 release_fn 被直接覆盖且无任何日志，GPU 资源泄漏无从追溯。覆盖语义是文档化设计，但"覆盖带未执行 release_fn 的条目"值得告警。
  - 建议: 覆盖目标已存在且 `release_fn is not None` 时记 warning。
  - 置信度: 高（代码确定；实际影响取决于调用方是否存在覆盖用法）

- [AUD-1302] [Suggestion] release_all 的组名快照与逐组释放之间存在窗口，并发 register 的新组会被本次遍历遗漏
  - 位置: src/shared/gpu/gl_resource_registry.py:146-150
  - 证据:
    ```python
    with self._lock:
        groups = list(self._records.keys())
    counts: Dict[str, int] = {}
    for group in groups:
        counts[group] = len(self.release_group(group))
    ```
  - 影响: 关停路径若与渲染线程注册并发，快照之后新增的组/条目不会被本次 release_all 释放。单线程顺序关停（渲染线程 join 后再调用）无影响。
  - 建议: 循环结束后复查 `_records` 是否新增组并补释放，或在文档强调必须在渲染线程停止后调用。
  - 置信度: 高（竞态窗口确定）/ 影响低

### 通过项
- 线程安全：register/unregister/get/has_group/snapshot/record_count 全部在 `self._lock` 内；release_fn 回调在锁外执行，避免回调内再入登记表造成死锁。
- 不做任何 GL 调用，符合"UI 线程绝不调 GL"契约（文件头线程契约段落明确）。
- release_group 按 dict 插入序 LIFO 释放，符合依赖逆序直觉；返回实际触发过 release_fn 的名单。
- 纯台账模式（release_fn=None）只移除登记不触发回调，与文档一致。
- 类型注解完整（dataclass + 全方法签名）。
- 性能：无每帧全表遍历路径；snapshot/record_count 仅供诊断调用。
- 重复注册：同键覆盖语义已在 docstring 文档化（残留风险见 AUD-1301）。

---

## src/shared/gpu/gpu_scope_engine.py (行数: 662)

### 发现

- [AUD-1310] [Suggestion] `_pool_cache` / `_points_vao_cache` 按尺寸键缓存且永不淘汰，所有历史尺寸的资源常驻至 release()
  - 位置: src/shared/gpu/gpu_scope_engine.py:145-153（缓存写入）、226-235（唯一释放点）、273-281（VAO 缓存）
  - 证据:
    ```python
    pool_key = (
        tuple(int(v) for v in analysis_size),
        tuple(int(v) for v in waveform_size),
        tuple(int(v) for v in vectorscope_size,
    )
    pool = self._pool_cache.get(pool_key)
    if pool is None:
        pool = {}
        self._pool_cache[pool_key] = pool
    self._pool = pool
    ```
    （原文 148 行为 `tuple(int(v) for v in vectorscope_size),`，上面为节录排版）
    以及：
    ```python
    key = (prog.glo, int(vertex_count))
    cached = self._points_vao_cache.get(key)
    if cached is not None:
        return cached[0]
    count = max(1, int(vertex_count))
    ibo = self.ctx.buffer(np.arange(count, dtype='u4').tobytes())
    ```
  - 影响: 已核实调用方在 PLAYBACK / 高质量两档 analysis_size 间动态切换（layer_controller.py:1104-1108 空闲升档、main_window_adapter.py:510-512），每档常驻一组约 14 张纹理+FBO（含多张 f2 密度图）以及每个 POINTS 程序一个 `(aw*ah)*4` 字节的 u4 IBO，直到引擎销毁。档位数为有限小集合，故为常驻冗余而非无界泄漏。
  - 建议: 保留最近 1-2 个尺寸键（LRU 淘汰旧 pool 时逐对象 release），或至少在切换尺寸时释放非当前 pool。
  - 置信度: 高（代码与调用方两档切换均已核实；因键空间有限降为 Suggestion）

- [AUD-1311] [Suggestion] wf_display 纹理被 waveform/parade/luma 三种输出复用，正确性依赖 scope_types 模式互斥这一跨模块不变量
  - 位置: src/shared/gpu/gpu_scope_engine.py:626-642
  - 证据:
    ```python
    if mode_needs_waveform(mode):
        self.accumulate_waveform(pool)
        self.display_waveform(pool)
        outputs['waveform'] = (pool['wf_display'].glo, pool['waveform_size'])
    ...
    if mode_needs_parade(mode):
        ...
        outputs['parade'] = (pool['wf_display'].glo, pool['waveform_size'])
    if mode_needs_luma(mode):
        ...
        outputs['luma'] = (pool['wf_display'].glo, pool['waveform_size'])
    ```
  - 影响: 三个输出键指向同一张纹理，若同一 demand 同时命中其中两个 pass，后渲染者覆盖先渲染者，消费者按 glo blit 时前一个模式显示错误内容。已核实 scope_types.py:174-192 当前谓词互斥（waveform∈{waveform,split}、parade={parade}、luma={luma}），现在无触发路径——但该不变量定义在另一个模块，本文件无任何防御。
  - 建议: 在 render_demand 内断言三个谓词至多命中一个，或为不同输出分配独立显示纹理。
  - 置信度: 高（共享事实确定；当前无缺陷，纯防御性）

- [AUD-1312] [Suggestion] 各 pass 修改 ctx.viewport 后不恢复进入前取值，GL 全局状态外泄给调用方
  - 位置: src/shared/gpu/gpu_scope_engine.py:324-328（写入）、464-466（仅 parade 部分恢复）
  - 证据:
    ```python
    def _use_target(self, fbo, size: Tuple[int, int], clear=(0.0, 0.0, 0.0, 1.0)) -> None:
        fbo.use()
        self.ctx.viewport = (0, 0, int(size[0]), int(size[1]))
        self.ctx.scissor = None
        fbo.clear(*clear)
    ```
    以及 parade 的恢复（恢复到的是 wf 尺寸而非进入前尺寸）：
    ```python
    finally:
        self._restore_blend()
        self.ctx.viewport = (0, 0, ww, wh)
    ```
  - 影响: sample/display_waveform/display_vectorscope/display_histogram/display_cie/display_falsecolor 返回后 viewport 停留在示波器纹理尺寸；调用方（AsyncRenderer/CompositeRenderer）后续 pass 若未重设 viewport 会渲染到错误区域。blend 状态有 `_restore_blend` 契约，viewport 却无对应文档说明。
  - 建议: render_demand 进入时保存并在 finally 恢复 viewport/scissor，或在类 docstring 明确"调用方须自行重设 viewport"。
  - 置信度: 中（状态外泄确定；是否致错取决于调用方代码，本批未审）

- [AUD-1313] [Suggestion] SCOPE_RANGE_MIN/MAX 与采样着色器中硬编码的 clamp(-0.1, 1.2) 跨文件重复维护，且 HDR >1.2 值在采样阶段即被压平
  - 位置: src/shared/gpu/gpu_scope_engine.py:44-46、367-368（佐证：gpu_scope.glsl:46，属其它批次文件）
  - 证据:
    ```python
    # Scope value axis (HDR headroom) shared by waveform / parade / luma.
    SCOPE_RANGE_MIN = -0.1
    SCOPE_RANGE_MAX = 1.2
    ```
    以及本文件把它作为 uniform 传入 accum/display：
    ```python
    prog['u_range_min'].value = SCOPE_RANGE_MIN
    prog['u_range_max'].value = SCOPE_RANGE_MAX
    ```
    佐证（gpu_scope.glsl 采样 pass，非本批文件）：`f_color = vec4(clamp(c, -0.1, 1.2), clamp(mask, 0.0, 1.0));`
  - 影响: (1) 双处常量需人工同步——若扩大 Python 侧轴范围（如 HDR 更高 headroom），采样截断仍停留在 1.2，轴与数据不一致；(2) scene-linear HDR 值 >1.2 在采样阶段即被截断，波形/矢量示波/CIE 对该部分不可见。注：GLSL 内注释明确说明该 clamp 是防 NaN 毒化加法密度 pass + 削波诊断的有意设计（配合 >1.0 标红/<0.0 标蓝），故不按"无理由 clamp"立 Major，仅立同步隐患。
  - 建议: 将 range_min/max 作为 uniform 传入采样 pass（或 shader 生成时注入），消除双处字面量；若未来需展示 >1.2 的 HDR 内容再评估专门的 HDR 轴模式。
  - 置信度: 高

- [AUD-1314] [Suggestion] 部分公开/内部方法缺参数与返回类型注解
  - 位置: src/shared/gpu/gpu_scope_engine.py:267-268、333-339、600-601
  - 证据:
    ```python
    def _points_vao(self, prog: moderngl.Program, vertex_count: int):
    ```
    ```python
    def sample(
        self,
        source_texture,
        pool: dict,
        mask_texture=None,
    ```
    ```python
    def render_demand(self, demand, source_texture, mask_texture=None,
                      mask_uv_rect=(0.0, 0.0, 1.0, 1.0)) -> dict:
    ```
  - 影响: 与 .clinerules 强类型要求不一致；`demand` 实际是 ScopeGpuDemand（为避免循环导入未标注），可用 `TYPE_CHECKING` 前置注解解决。
  - 建议: 补全注解，demand 用字符串注解 + TYPE_CHECKING import。
  - 置信度: 高

- [AUD-1315] [Suggestion] release/换新路径的 `except Exception: pass` 共 7 处且零日志，销毁期 GL 错误完全不可见
  - 位置: src/shared/gpu/gpu_scope_engine.py:161-163、178-180、230-233、239-242、245-248、251-254、258-260
  - 证据:
    ```python
    for pool in list(self._pool_cache.values()):
        for key, obj in list(pool.items()):
            if key.endswith('_size') or not hasattr(obj, 'release'):
                continue
            try:
                obj.release()
            except Exception:
                pass
    ```
  - 影响: 尽力而为的销毁语义本身合理（对比 gl_resource_registry.py 释放失败会 logger.exception），但完全静默使上下文丢失场景下的资源释放失败无法诊断。
  - 建议: 至少以 logger.debug/exception 记录失败对象名。
  - 置信度: 高

### 通过项
- 线程安全：类本身无共享可变状态跨线程暴露，docstring 明确契约"必须在持有 GL context 的线程使用，两个调用方都持全局 GL mutex"；无 Qt 类型，无信号（维度 4 不适用）。
- GPU 资源生命周期：ensure_tex/ensure_fbo 尺寸变化时先 release 旧对象再建新；release() 幂等、覆盖 pool/VAO/VBO/program 全部类别并清标志；accum pass 均以 try/finally 恢复混合状态（369-377、412-417、457-465、486-493、536-541）。
- GLSL 正确性边界说明：着色器实际位于外部文件 src/features/color/shaders/gpu_scope.glsl（非本批清单），字面量/循环边界/UV 越界的逐行检查归属该文件所在批次；本文件 Python 侧浮点 uniform 均带 .0（如 395-397、519、556），falsecolor 的 aspect-fit uv 缩放/偏移恒在 [0,1] 内（575-586）不越界。
- 异常路径：load_shaders 单程序编译失败仅记日志并继续，shaders_loaded 汇总判定；所有 pass 以 `self.programs.get(...)` 早退，编译失败不崩溃、不做半渲染。
- 状态提交一致性：render_demand 按 demand.mode 条件执行各 pass，输出键与纹理一一对应（waveform/parade/luma 共享纹理的风险已单列 AUD-1311）；密度归一化用固定增益（expected_peak/u_max_inv）避免 read() 回读阻塞。
- 性能：零 read() 回读设计；POINTS 驱动 gl_VertexID + 缓存 VAO/IBO，避免每帧重建；每帧开销主要是每 pass 一次全屏 clear + aw*ah 点绘制，属需求驱动（demand）而非无条件每帧。
- 取消/取消后残留：引擎无内部取消状态；帧的有效性由调用方 revision/source_revision 判定（scope_types.is_frame_valid_for_demand，已佐证）。

---

## src/shared/gpu/graphics_backend.py (行数: 120)

### 发现
（无——纯 Protocol/契约定义模块，无运行时逻辑。）

### 通过项
- 无任何 GL 调用与可变全局状态，线程契约（initialize/create_*/release 仅渲染线程、render_pass 走命令队列、present 不做 GL）在模块 docstring 明确。
- 错误体系显式（GraphicsBackendError / BackendNotReadyError / BackendThreadViolationError），契约要求"不静默降级"。
- TextureDescriptor 默认 dtype="f2"，与 HDR 内部处理约定一致；描述符 dataclass 均 frozen。
- 状态机枚举覆盖 uninitialized/ready/released/failed，与 §4.2 drain→release 切换流程对应。
- 类型注解完整；runtime_checkable Protocol 用法正确。
- 无 Qt 信号（维度 4 不适用）。

---

## src/shared/gpu/host_gpu_info.py (行数: 89)

### 发现

- [AUD-1320] [Suggestion] metal_info_from_gpu 直接下标访问 gpu["devices"]，缺键时抛 KeyError，违背模块"失败路径都返回稳定 reason_code"的承诺
  - 位置: src/shared/gpu/host_gpu_info.py:83
  - 证据:
    ```python
    if gpu.get("status") != "AVAILABLE":
        return {
            "status": "UNAVAILABLE",
            "reason_code": "METAL_INFO_REQUIRES_GPU_INFO",
            "detail": None,
        }
    metals = sorted({str(d.get("metal_support")) for d in gpu["devices"]
                     if d.get("metal_support")})
    ```
  - 影响: detect_host_gpu_info 的正常输出必然带 devices，但调用方若传入裁剪/手工构造的 dict（status=AVAILABLE 而 devices 缺失），将抛未捕获 KeyError 而非带 reason_code 的结构化失败；与同函数其余 .get 防御风格不一致。
  - 建议: 改为 `gpu.get("devices") or []`，空列表归入 METAL_SUPPORT_NOT_REPORTED 或新增 reason_code。
  - 置信度: 高（代码路径确定；触发依赖非标准输入，故仅 Suggestion）

### 通过项
- 输入验证：subprocess 带 timeout/check/capture_output；SubprocessError（含 TimeoutExpired/CalledProcessError）、OSError、JSONDecodeError 全部捕获并返回稳定 reason_code；stdout 以 utf-8/replace 解码，坏字节不致抛错。
- `data.get("SPDisplaysDataType", []) or []` 防御 None；Apple Silicon 与 AMD/Intel 的 Metal 字段双写法均处理（54-63 行）。
- 非 macOS 平台显式返回 GPU_INFO_PLATFORM_NOT_MACOS，不静默。
- 纯标准库，无 Qt/GL 依赖，符合模块定位；`__all__` 显式导出；类型注解完整。
- 输出结构 JSON-safe（模块 docstring 承诺满足）。

---

## 批次备注

1. 维度 3（GLSL 字面量/循环边界/UV 越界）：gpu_scope_engine 的着色器并非内嵌于该 py 文件，而是 `#define NAME """..."""` 形式集中在外部 `src/features/color/shaders/gpu_scope.glsl`，建议该文件归属的批次承接逐行 GLSL 检查；本批只核了 Python 侧传参（见 AUD-1313 与通过项）。
2. 维度 4（信号防递归）：4 个文件均无 Qt 信号，不适用。
3. 佐证用只读检查涉及：`src/features/color/core/scope_types.py`（模式谓词互斥、DEFAULT_ANALYSIS_SIZE=(320,180)）、`src/features/color/shaders/gpu_scope.glsl`（采样 clamp）、`src/app/controllers/layer_controller.py:1100-1140` 与 `src/features/composite/adapters/main_window_adapter.py:500-516`（analysis_size 两档切换）。均未修改任何文件。
