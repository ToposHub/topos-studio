# batch-060 审查报告：src/features/composite/evaluators/

- 批次：batch-060 [P1]
- 编号区间：AUD-6000–AUD-6099
- 审查日期：2026-09-28
- 文件数：4（2,676 行），全部完整通读
- 交叉验证：`render/gpu_resource_pool.py`、`evaluators/base_evaluator.py`、`evaluators/color_evaluator.py`、`evaluators/keyer_evaluator.py`、`nodes/paint_node.py`、`nodes/erode_dilate_node.py`、`nodes/warp_node.py`、`shared/gpu/metal/context.py`（只读）

## src/features/composite/evaluators/filter_evaluator.py (行数: 1717)

### 发现

- [AUD-6001] [Critical] erode/dilate 迭代≥2 引用不存在的 `_resource_pool._texture_by_id`，节点求值必然抛 AttributeError
  - 位置: src/features/composite/evaluators/filter_evaluator.py:129
  - 证据:
    ```python
    else:
        self._bind_external_texture(current_tex.texture_id)
        if current_tex is not input_tex:
            self._resource_pool.release_texture(
                self._resource_pool._texture_by_id.get(current_tex.texture_id)
            )
    ```
    `self._resource_pool` 是 `GPUResourcePool`（composite_renderer.py:458 / composite_export_renderer.py:466 赋值），其属性为 `_active_textures: Dict[int, TextureInfo]`（gpu_resource_pool.py:531）；全库 grep 无任何 `_texture_by_id` 赋值，类也无 `__getattr__`。该 else 分支在第 2 次及以后迭代执行（iterations≥2、fragment 路径；compute 路径仅 size>16 且可用时启用，macOS GL 4.1 下基本走 fragment）。
  - 影响: `ErodeDilateNode` 的 iterations UI 范围 1–10（erode_dilate_node.py:58，节点侧 cap 3），matte_cleanup 也透传用户 iterations（keyer_evaluator.py:470）。iterations≥2 时抛 `AttributeError`，被 base_evaluator.py:1034-1039 捕获后 `cache[node_id] = {}`——该节点及其全部下游边输出为空，画面静默丢失，仅一条 warning 日志。属确定性渲染错误。退一步即使属性存在，`.get()` 返回 None 时 `release_texture(None)` 也会在 `tex.glo`（gpu_resource_pool.py:949）再次抛错。
  - 建议: 改用现有公开 API `self._resource_pool.release_texture_by_id(current_tex.texture_id)`（同文件 compute 路径及 base_evaluator.py:1240 均是该用法），并补一条 iterations≥2 的回归测试。
  - 置信度: 高

- [AUD-6002] [Major] 3D LUT 纹理缓存无界、绕过资源池、无释放路径
  - 位置: src/features/composite/evaluators/filter_evaluator.py:1687-1698, 1700-1717
  - 证据:
    ```python
    def _get_or_upload_lut(self, lut_path: str):
        if not hasattr(self, '_lut_cache'):
            self._lut_cache: Dict[str, object] = {}
        if lut_path in self._lut_cache:
            return self._lut_cache[lut_path]
        lut_tex = self._upload_3dlut(lut_path)
        if lut_tex is not None:
            self._lut_cache[lut_path] = lut_tex
        return lut_tex
    ...
        lut_tex = self.ctx.texture3d((size, size, size), 3, data=data, dtype='f4')
    ```
    纹理直接经 `ctx.texture3d` 创建（不经 `_resource_pool` 内存预算），按路径存入普通 dict，无 LRU/上限，也无任何 `release()` 调用点。
  - 影响: 会话期间按 LUT 文件数无界增长 GPU 显存（64³ f4 约 3MB/个）；composite 与 export 渲染器各持一份实例缓存造成双份；绕过 VRAM 预算监控，与项目"memory-tracked texture allocation"约定不符。
  - 建议: 加 LRU 上限（按个数或字节预算），并在渲染器销毁路径统一 release；或纳入 resource_pool 的跟踪。
  - 置信度: 高（机制确凿；量级取决于用户使用的 LUT 数量）

- [AUD-6003] [Suggestion] 计算着色器回退路径的宽泛 `except Exception` 掩盖编程错误
  - 位置: src/features/composite/evaluators/filter_evaluator.py:104-105, 730-732, 738-740
  - 证据:
    ```python
    except Exception as e:
        logger.warning(f"ErodeDilate compute shader failed (size={size}): {e}")
    ...
    except Exception:
        logger.exception("Unable to upload Roto point uniforms")
    ```
  - 影响: AttributeError/TypeError 等代码缺陷与真正的 GPU 能力不足被同等降级为"回退 fragment/沿用旧 uniform"，掩盖领域错误（.clinerules 铁律），roto 路径甚至在上传失败后继续渲染出错误 matte（仅日志）。
  - 建议: 收窄为 moderngl 相关异常（如 `Exception` 之外先捕获具体类型），对 uniform 上传失败应中止该节点渲染而非带病输出。
  - 置信度: 中

- [AUD-6004] [Suggestion] `_apply_roto` 着色器缺失时不产出 `matte` 输出，破坏双端口契约
  - 位置: src/features/composite/evaluators/filter_evaluator.py:716-717
  - 证据:
    ```python
    prog = self.utility_shaders.get("roto")
    if prog is None:
        return {"result": input_tex} if input_tex else {}
    ```
    对比 `_apply_mask`（filter_evaluator.py:509-521）在 prog 缺失时仍构造并返回 `{"result": ..., "matte": matte}`；`_apply_roto` 正常路径（775-785）也总是同时返回 matte。
  - 影响: roto shader 编译失败时下游消费 matte 的连线（mask 链）静默断开，行为与其余路径不一致。
  - 建议: prog 缺失分支补一个清零 matte 纹理输出，与 `_apply_mask` 对齐。
  - 置信度: 中

- [AUD-6005] [Suggestion] `_apply_paint` clone 采样的双重高级索引产生整帧拷贝，叠加全帧 f4 回读
  - 位置: src/features/composite/evaluators/filter_evaluator.py:923-931, 963-964, 1093-1095
  - 证据:
    ```python
    raw = fbo.read(components=4, dtype="f4")
    ...
    src = self._sample_clone_source(img, stroke, w, h)
    img = src * m3 + img * (1.0 - m3)
    ...
    ys = np.clip(np.arange(h) - dy, 0, h - 1)
    xs = np.clip(np.arange(w) - dx, 0, w - 1)
    return img[ys][:, xs]
    ```
    `img[ys][:, xs]` 每笔 clone stroke 都物化一份整帧副本（4K RGBA f4 ≈ 141MB），再叠加逐 stroke 的逐元素混合与逐帧 `fbo.read` 下载。
  - 影响: 4K 实时基准下，多笔 clone stroke 的求值每帧产生数百 MB 级临时分配与 GPU→CPU 同步，交互明显卡顿（CPU 光栅为已知 MVP 设计，但该副本是可避免的额外开销）。
  - 建议: 用四次切片拼装（`np.concatenate` of 行/列切片）实现 clamp 采样，避免整帧 fancy-index 拷贝；中期考虑 stroke bbox 内局部处理。
  - 置信度: 高（代码事实；性能量级为估算）

- [AUD-6006] [Suggestion] 效果输出 TextureResult 未透传 `bit_depth`/`revision`，HDR 元数据在链路中被降级
  - 位置: src/features/composite/evaluators/filter_evaluator.py:275-279（透传）对比 229-237、336-344、1258-1266、1532-1540 等（未透传）
  - 证据:
    ```python
    # lens_distortion（透传）：
        bbox=input_tex.effective_bbox(),
        color_space=input_tex.color_space,
        alpha_premultiplied=input_tex.alpha_premultiplied,
        bit_depth=input_tex.bit_depth,
        revision=input_tex.revision,
    # glow / edge_detect / denoise / godrays 等（未透传，bit_depth 回落默认 8）：
        "result": TextureResult(
            texture_id=out_tex.glo, width=w, height=h,
            format=input_tex.format,
            bbox=input_tex.bbox,
    ```
    TextureResult 定义中 `bit_depth: int = 8`，注释称其"Used for HDR processing and display conversion"（base_node.py:93-94）。
  - 影响: HDR 源（16/32-bit）经 glow、edge_detect、denoise、godrays、mesh_warp 等节点后元数据变为 8-bit，下游显示转换/HDR 判定可能按 SDR 处理；同类不一致也存在于 `effective_bbox()` vs `bbox` 的混用。
  - 建议: 在公共出口（如 `_apply_simple_per_pixel` 及各手动路径）统一复制 `bit_depth`（必要时含 `frame_metadata`）。
  - 置信度: 中

- [AUD-6007] [Suggestion] uniform 赋值未做数值类型归一，与同文件其他路径风格不一致
  - 位置: src/features/composite/evaluators/filter_evaluator.py:306-327, 376-394, 437-460
  - 证据:
    ```python
    prog['u_threshold'].value = params.get("threshold", 0.1)   # edge_detect，无 float()
    ...
    prog['u_scale'].value = params.get("scale", 100.0)          # noise
    ...
    prog['u_radius'].value = params.get("radius", 0.5)          # ramp
    ```
    而 lens_distortion（258-267）、denoise（1246-1250）等均显式 `float(...)`。
  - 影响: 若参数经持久化 JSON 以字符串形式回流（外部数据），moderngl 赋值抛异常导致节点求值失败；当前节点层多数已 coercion，属防御纵深缺口。
  - 建议: 统一 `float()/int()/bool()` 归一，或抽公共 `_set_float_param` 辅助。
  - 置信度: 低

- [AUD-6008] [Suggestion] 多个辅助函数缺少参数/返回类型注解
  - 位置: src/features/composite/evaluators/filter_evaluator.py:1687, 1700, 1011, 1081, 544, 748
  - 证据:
    ```python
    def _get_or_upload_lut(self, lut_path: str):
    def _upload_3dlut(self, path: str):
    def _rasterize_stroke_mask(self, stroke: dict, w: int, h: int):
    def _sample_clone_source(img, stroke: dict, w: int, h: int):
    def render_shape(target_tex, mode: int, source: TextureResult | None = None) -> None:
    ```
    前四个无返回注解，`_sample_clone_source` 的 `img` 无注解，内嵌 `render_shape` 的 `target_tex` 无注解。
  - 影响: 违反项目强类型约定（.clinerules），降低可读性与静态检查覆盖率。
  - 建议: 补 `-> Optional[object]`/`-> "TextureResult"`/`np.ndarray` 等注解。
  - 置信度: 高

### 通过项
- 线程安全：本文件所有 GPU 调用均经由渲染线程执行（GraphEvaluatorMixin 契约），无 Qt 信号/控件操作，无跨线程共享可变状态。
- 信号防递归：无 Qt 信号代码，不适用。
- GLSL·HDR 正确性：Python 侧无 clamp(0.0,1.0) 于 HDR 数据（roto 的 feather/opacity 钳制为 UI 参数域，合理）；纹理均为池化 RGBA16F；mask/roto matte 的 RGB+alpha 双携带设计有注释依据。
- 状态提交一致性：无 undo/redo/持久化写入；效果为纯函数式求值。
- 异常与取消路径：绝大多数 shader 缺失/回退路径返回输入透传（glow、lut3d、denoise、relight 等），paint 回读/上传失败回退 passthrough 且注释明确防"黑底替换"；上层 base_evaluator.py:1034 有逐节点兜底。
- 输入验证：paint strokes、mesh_warp offsets、cryptomatte selected_ids 均在上游节点层做过 per-field coercion（paint_node.py:118-189、warp_node.py:89-104），evaluator 收到的 `_effect_params` 为已净化数据；尺寸参数统一 `max(1, int(...))` 防护。
- GPU 资源生命周期（除 AUD-6001/6002 外）：glow 的 temp、mesh_warp 的 lattice、compare/denoise/relight/depth 的纹理单元均在 finally/渲染后复位与归还；`_paint_passthrough` 用 GPU clear 代替 CPU 上传（注释记录了旧缺陷）。

## src/features/composite/evaluators/graph_evaluator_mixin.py (行数: 98)

### 发现
（无——纯组合层，无逻辑。）

### 通过项
- 通过（无逻辑）：纯 mixin 组合器，导入并合并 13 个 evaluator mixin 为 `GraphEvaluatorMixin`，向后兼容导出 `GraphEvaluatorMixin`、`TileContext`、`_AI_NODE_TYPES`。MRO 顺序清晰，无方法冲突面（各 mixin 职责不重叠）。
- 备注（不计编号）：模块 docstring 中路径描述为 `src/composite/evaluators/`，实际为 `src/features/composite/evaluators/`，属陈旧文档；`__all__` 导出下划线私有名 `_AI_NODE_TYPES` 属风格问题。

## src/features/composite/evaluators/histogram_evaluator.py (行数: 29)

### 发现
（无。）

### 通过项
- 通过（无逻辑）：占位 mixin，无字段无方法；percentile 逻辑实际在 `ColorEvaluatorMixin._compute_percentile_range`（docstring 已注明）。`TYPE_CHECKING` 块与未使用的导入（`Tuple/List/TextureResult`）为无害冗余。

## src/features/composite/evaluators/histogram_manager.py (行数: 832)

### 发现

- [AUD-6009] [Major] 直方图缓存键依赖 glo 且 revision 恒为 0，池化纹理复用导致跨图像缓存串用
  - 位置: src/features/composite/evaluators/histogram_manager.py:641-645, 402, 448
  - 证据:
    ```python
    cache_key = self._cache.compute_key(
        input_tex.glo,
        getattr(input_tex, 'revision', 0),
        mask_bits,
        f"{low_percentile:.2f}:{high_percentile:.2f}",
    )
    ...
    texture_revision=getattr(input_tex, 'revision', 0),
    ```
    调用方传入的是原生 moderngl Texture 对象（color_evaluator.py:513 `actual_tex = self._get_texture_by_id(...)` → 518-519 直接传入），其上不存在 `revision` 属性，恒取 0；而 `GPUResourcePool` 会把 release 的纹理按 key 池化并在下次 `acquire` 时复用同一对象/同一 glo（gpu_resource_pool.py:863-898、942-978），纹理名在删除后也会被驱动回收复用。
  - 影响: docstring 声称"Cache is invalidated when source texture changes"（histogram_manager.py:15-16），实际失效机制失效——图像 A 在 glo X 上算得的 percentile 被缓存后，另一幅图像 B 复用 glo X（同 percentile 参数）时直接命中 A 的缓存，Normalize percentile 得到错误的高低点，产生错误调色且无任何告警。另外不可用路径返回的全空直方图同样被写入缓存（663-665），把退化结果钉死在该键上。
  - 建议: 缓存键改用内容相关量（上游 TextureResult.revision 由节点层维护，应在 `_compute_percentile_range` 透传进 manager），或对纹理内容做轻量指纹；空结果不入缓存。
  - 置信度: 高（机制经交叉验证；实际触发频率取决于池复用与参数重合度）

- [AUD-6010] [Suggestion] `_downsample` 在无 resource_pool/fb_manager 的直建路径泄漏纹理与 FBO
  - 位置: src/features/composite/evaluators/histogram_manager.py:468-479, 393-395, 440-441
  - 证据:
    ```python
    if resource_pool is None:
        ds_tex = self._ctx.texture((dst_w, dst_h), 4, dtype='f2')
    ...
    if fb_manager is None:
        fbo = self._ctx.framebuffer([ds_tex])
    ...
    if downsample_factor < 1.0 and resource_pool is not None:
        resource_pool.release_texture(ds_tex)
    ```
    直建的 `ds_tex` 在归还得判断 `resource_pool is not None` 时被跳过，直建 `fbo` 在任何路径下都无 `release()`。
  - 影响: 当前唯一调用方（color_evaluator.py:522-523）同时传入 fb_manager 与 resource_pool，故为潜在路径；一旦有新调用方走默认 None，每次 4K→512 降采样泄漏 512×512 f2 纹理（约 2MB）+ FBO，且不经资源池统计。
  - 建议: 直建路径在使用后就地 `ds_tex.release()/fbo.release()`，或干脆要求调用方必须传入 pool/fb 并在签名上强制。
  - 置信度: 高（代码路径确凿；现网影响为潜在）

- [AUD-6011] [Suggestion] 可变默认参数 `channel_mask=[1.0, 1.0, 1.0, 0.0]`
  - 位置: src/features/composite/evaluators/histogram_manager.py:605
  - 证据:
    ```python
    def compute_percentiles(self, input_tex: object, w: int, h: int,
                            low_percentile: float,
                            high_percentile: float,
                            channel_mask: List[float] = [1.0, 1.0, 1.0, 0.0],
    ```
  - 影响: 经典 Python 陷阱——调用方若原地修改该列表会污染后续所有默认调用；对 `float` mask 列表做 `m > 0.5` 判定也偏脆弱。
  - 建议: 改为 `Optional[Sequence[float]] = None`，函数内 `channel_mask = list(channel_mask or (1.0, 1.0, 1.0, 0.0))`。
  - 置信度: 高

- [AUD-6012] [Suggestion] `release()` 未释放 quad VBO；`get_stats` 无锁读取缓存内部结构
  - 位置: src/features/composite/evaluators/histogram_manager.py:508-521, 722-727, 747
  - 证据:
    ```python
    vbo = self._ctx.buffer(vertices)
    self._quad_vao = self._ctx.simple_vertex_array(
        self._downsample_prog, vbo, 'in_vert', 'in_uv',
    )
    ...
    if hasattr(self, '_quad_vao') and self._quad_vao is not None:
        self._quad_vao.release()      # VBO 未 release
    ...
    'cache_entries': len(self._cache._cache),   # 越过 HistogramCache 的锁
    ```
  - 影响: VAO release 不释放属性 buffer（moderngl 语义），256 字节 VBO 泄漏至上下文销毁（量级微小）；`get_stats` 绕过 `_cache._lock` 读取 OrderedDict，理论上与并发 put/evict 竞争（GIL 下实际风险极低），且穿透私有属性。
  - 建议: 持有 `self._quad_vbo` 引用并在 release() 中一并释放；给 HistogramCache 加 `__len__`/`stats()`。
  - 置信度: 高（事实确凿，影响低）

### 通过项
- 线程安全：`compute_histogram` 全程持 `RLock`（GPU 调用 + 读回原子化）；`HistogramCache` 读写有锁且 LRU 正确；诊断去重告警有模块级锁。
- GPU 资源生命周期（除 AUD-6010/6012 外）：GL 路径降采样纹理经 pool 归还；`release()` 对 buffer/program/VAO 逐项 try-release 并置 None；Metal buffer 由 PyObjC `new...` 语义随代理释放，不累积。
- GLSL·HDR 正确性：GL 版本解析用正则前缀规避 Mesa 后缀误判（有注释依据）；Metal 路径 cdf/bins 缓冲零初始化与哨兵解码有明确注释；CDF 计算与百分位桶换算 (bin+0.5)/256 与 GL/CPU 两路径一致。
- 信号防递归：无 Qt 代码，不适用。
- 状态提交一致性：无 undo/持久化写入。
- 异常与取消路径：GL/Metal 双后端互为回退且 `_warn_unavailable_once` 显式诊断（不再静默空直方图）；Metal init 失败不阻塞渲染；上游 color_evaluator.py:529-531 兜底 (0,1)。
- 输入验证：`low/high_percentile` 的 0-100 → 0-1 换算由唯一入口统一处理；`ds_tex.read()` 按 RGBA16F 内部格式读回与 metal_compute_bridge.py:170 的项目约定一致。
- 类型注解：公共 API 注解完整（`HistogramResult` dataclass 字段类型齐全）。

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-6001 |
| Major | 2 | AUD-6002, AUD-6009 |
| Suggestion | 9 | AUD-6003–6008, AUD-6010–6012 |
