# Batch-009 静态审查报告：color/core 降噪与 MSL/节点模块

- 日期：2026-09-28
- 范围：6 个文件（shader_nodes_msl / the_decompiler / video_denoise_gl / video_denoise_pipeline / video_denoise_shaders / video_spatial_denoise）
- 编号区间：AUD-0900 – AUD-0915
- 汇总：Critical 0 / Major 7 / Suggestion 9（共 16 条）
- 交叉验证说明：MSL 翻译与 GLSL 权威文本（shader_nodes.py）、反编译器前向模型与 GPU 管线、denoise 表面包边（clamp-to-edge）均做了跨文件 grep/sed 核对，核对结果在对应条目中注明。

---

## src/features/color/core/shader_nodes_msl.py (行数: 917)

### 发现

- [AUD-0910] [Major] blend mode 14（Color）是 HSV 恒等往返，退化为 Normal 混合，未保留 base 亮度
  - 位置: src/features/color/core/shader_nodes_msl.py:709
  - 证据:
    ```metal
    if (mode >= 12 && mode <= 15) {
        float3 b_hsv = msl_rgb2hsv(base);
        float3 a_hsv = msl_rgb2hsv(blend_layer);
        if (mode == 12) return msl_hsv2rgb(float3(a_hsv.x, b_hsv.y, b_hsv.z));
        if (mode == 13) return msl_hsv2rgb(float3(b_hsv.x, a_hsv.y, b_hsv.z));
        if (mode == 14) return msl_hsv2rgb(float3(a_hsv.x, a_hsv.y, a_hsv.z));
        if (mode == 15) return msl_hsv2rgb(float3(b_hsv.x, b_hsv.y, a_hsv.z));
    ```
  - 影响: mode 14 输出 = hsv2rgb(rgb2hsv(blend_layer)) ≈ blend_layer 本身，即与 mode 0（Normal）几乎等价；标准 Color 混合应为 `(a_hsv.x, a_hsv.y, b_hsv.z)`（取上层色相/饱和度 + 下层明度）。与 12/13/15 每个模式都混合 a/b 分量的体系明显不一致。GLSL 权威文本同源存在（shader_nodes.py:670 `if (mode == 14) return hsv2rgb(vec3(a_hsv.x, a_hsv.y, a_hsv.z));`），说明 MSL 翻译忠实、缺陷源于权威文本；文件头声称的 G2 逐效果对拍通过意味着两侧一致地保留了该缺陷。
  - 建议: 按 ADR 0004 契约两侧同步修改为 `float3(a_hsv.x, a_hsv.y, b_hsv.z)`（GLSL shader_nodes.py:670 + 本文件 :709），并跑 G2 复验；若确为有意行为需在两处加注释说明。
  - 置信度: 中（基于标准混合模式定义与 12/13/15 模式体系推断为缺陷；不能完全排除有意设计）

### 通过项
- 浮点字面量：全文件抽查（grading/curves/lut3d/mixer/blur/blend/soft_clip/film_grain）所有数字均带 `.0` 或使用合法科学计数（`1.0e-10`、`1.0e-5`），无隐式 int→float。
- 循环边界：所有 for 循环边界为常量（`i < 8`、`i <= 8.0`、`const int samples = 28`、`i < 12` 等），无动态 uniform 边界。
- 与 GLSL 权威逐条对照：抽查 GRADING（曝光/WB/对比/LGG/gamma 三分支/暗部保护/阴影/高光/vibrance/mid_detail/sharpness）、SOFT_CLIP（Toe 双分支 + 平衡块 + Shoulder/elif）、BLUR_SHARPEN（USM 迭代 + blur 混合）、MIXER（8 通道掩码）均逐条一致（shader_nodes.py 对应段落已核对）。
- HDR 正确性：`clamp(color, 0.0, 1.0)` 均配 HDR 恢复手段（LUT3D 的 max_orig 比例缩放 + 负值加法偏移；Shadows 的 `(color - clamped_c)` 回加），无无理由 clamp。
- film grain 哈希链按 P6-06 契约显式展开同序四则（dot/mix 展开），未使用内建随机原语。
- LUT3D texel 中心映射 `(i + 0.5)/n` 修正了半 texel 拉伸，边界采样不越界。
- 该文件为纯字符串常量模块，无线程/GPU 资源/信号维度。

---

## src/features/color/core/the_decompiler.py (行数: 541)

### 发现

- [AUD-0911] [Major] apply_soft_clip 分支门控与 GPU 语义背离：丢失两条 elif 路径
  - 位置: src/features/color/core/the_decompiler.py:263, src/features/color/core/the_decompiler.py:280
  - 证据:
    ```python
        if black_point < low:
            T, B = low, black_point
            ...
                mask_clamped = res[:, i] < black_point
                res[mask_clamped, i] = black_point
    ...
        if white_point > high:
            max_c = np.max(res, axis=-1)
            mask = max_c > high
    ```
    GLSL/MSL 权威语义（shader_nodes_msl.py:733-736、shader_nodes.py SOFT_CLIP_SNIPPET）：
    ```metal
        if (c < low && black_point < low) { ... }
        else if (c < black_point) { return black_point; }
    ...
        if (max_c > high && white_point > high) { ... }
        else if (max_c > white_point) { res *= white_point / max_c; }
    ```
  - 影响: (1) `white_point <= high` 时（优化边界允许：wp∈[0.5,5.0]、high∈[0.1,1.5]，如 high=1.2/wp=0.5）GPU 会经 elif 把超过 wp 的像素钳到 wp，CPU 完全不做；(2) `black_point >= low` 时（bp≤0.2、low≥-0.5 可达）GPU 的 `else if (c < black_point)` 钳制路径 CPU 缺失。CPU 前向模型与 GPU 渲染在这些可达参数区域不一致，反编译拟合出的参数应用到 GPU 后不复现 LUT。
  - 建议: 按权威文本重写为逐元素等价的分支结构（`where` 实现两条 elif），并加 CPU↔GPU 对拍单测覆盖 wp<high 与 bp>low 区域。
  - 置信度: 高（两侧代码均已读到，分支差异客观存在）

- [AUD-0912] [Major] 前向模型缺失 GPU 管线的"暗部保护"与 toe RGB 平衡两个处理级
  - 位置: src/features/color/core/the_decompiler.py:182-192, src/features/color/core/the_decompiler.py:263-273
  - 证据:
    ```python
        res = np.where(mask_mid, b_mid,
                       np.where(mask_high, b_high, b_low))

        # 5. Shadows
        shadow = params.get('shadow', 0.0)
    ```
    GPU 权威在 gamma 之后有独立的暗部保护级（shader_nodes.py:72-89 / shader_nodes_msl.py:124-136）：
    ```metal
        float luma_post_grading = dot(color, float3(0.299, 0.587, 0.114));
        if (luma_post_grading < 0.0) {
            color.r = _grading_shadow_guard(color.r);
            ...
    ```
    同样，CPU apply_soft_clip 的 toe 段（263-273 行）没有 GPU 的 toe_min/toe_max RGB 平衡块（shader_nodes_msl.py:761-773）。
  - 影响: 拟合目标函数 loss_func（399-404 行）用的是缺级的 CPU 模型，而拟合结果最终在含这两级的 GPU 管线上渲染；暗部/负值场景（Lift/Gamma 下溢时触发暗部保护）拟合失真，反编译结果与 LUT 目标出现系统性偏差。
  - 建议: 将两个缺失级按权威公式移植到 GradingMath（numpy 向量化），或至少在文档中登记已知的拟合误差来源。
  - 置信度: 高（块缺失客观；影响幅度取决于触发频率，属中低频但系统性）

- [AUD-0913] [Major] hue_shift 前 `np.clip(res, 0, 1)`，GPU 两侧均不 clip，HDR/负值区域 CPU/GPU 分叉
  - 位置: src/features/color/core/the_decompiler.py:227
  - 证据:
    ```python
        # 9. Hue Shift
        hue_shift = params.get('hue_shift', 0.0)
        if hue_shift != 0.0:
            hsv = GradingMath.rgb2hsv(np.clip(res, 0, 1))
            hsv[..., 0] = (hsv[..., 0] + hue_shift / 360.0) % 1.0
            res = GradingMath.hsv2rgb(hsv)
    ```
    GPU 权威（shader_nodes.py:169 `vec3 hsv = rgb2hsv(color);` / shader_nodes_msl.py:206 `float3 hsv = msl_rgb2hsv(color);`）直接用未 clip 的 color。
  - 影响: exposure/gain 把 res 推到 >1（HDR 域）或负值后，CPU 的 clip 会把 v 分量压到 ≤1/0，GPU 保留 >1 的 v；hue_shift != 0 时反编译前向模型与 GPU 渲染在 HDR 区间产生可见分叉。
  - 建议: 去掉 clip，使 rgb2hsv/hsv2rgb 与 GPU 公式在超界输入下行为一致（或两侧同步登记差异）。
  - 置信度: 高（分叉客观；对 SDR LUT 拟合的实际影响中等）

- [AUD-0914] [Suggestion] decompile 无输入校验且重计算可能阻塞调用线程
  - 位置: src/features/color/core/the_decompiler.py:350-362
  - 证据:
    ```python
    def decompile(self, lut_path):
        if not HAS_SCIPY:
            logger.error("scipy is required for LUT decompilation. Install with: pip install scipy")
            return None

        self.load_cube(lut_path)
        N = self.lut_size
    ```
  - 影响: `load_cube` 失败（路径无效/坏 cube）直接抛出到调用方（image_engine.decompile_lut 有宽泛 try/except 兜底，但仅 logger.error 后吞掉，用户无感知失败原因）；`lut_size` 无下限校验。另外三阶段 least_squares（max_nfev 100+100+300 × 4000 样本 × 全链 numpy）是秒级以上重计算，调用点 image_engine.py:1320-1322 在引擎方法内同步执行，若由 UI 动作直调会冻结 UI。
  - 建议: 在 decompile 入口校验路径存在与 lut.size>=2 并抛领域异常；调用方应移到 worker 线程并以进度/失败信号回 UI（该部分属 image_engine，超出本批次）。
  - 置信度: 中（校验缺失客观；线程影响取决于调用链，未完整追踪到 UI slot）

- [AUD-0915] [Suggestion] 类型注解缺失（GradingMath/Decompiler 公共方法均无注解）
  - 位置: src/features/color/core/the_decompiler.py:20-21, src/features/color/core/the_decompiler.py:341, src/features/color/core/the_decompiler.py:350
  - 证据:
    ```python
    class GradingMath:
        """NumPy implementation of the GPU grading pipeline."""

        @staticmethod
        def rgb2hsv(rgb):
    ```
  - 影响: 与项目强类型约束不符；`load_cube`/`decompile` 无参数与返回类型注解，IDE/审查工具无法校验 ndarray shape 契约（(N,3) vs (...,3)）。
  - 建议: 补 `np.ndarray` + shape 说明的注解（或 Tuple[float,...] 返回）。
  - 置信度: 高

### 通过项
- 异常路径：HAS_SCIPY 缺依赖分支返回 None 并给出明确日志，不抛裸异常。
- apply_grading 与 GPU 逐步对照（曝光/WB/对比/LGG/gamma 三分支/阴影/高光/vibrance/饱和）公式与次序一致；2026-09-17 就地优化有等价性说明与 ulp 差异登记（124-147 行注释）。
- apply_mixer 与 MIXER_SNIPPET 逐项一致（abs+sign、8 通道、每分量非零门控、hue wrap fract 等价）。
- extract_warper_grid 的 hue wrap-around 最短路径处理与越界下标 clip(0, grid_size-1) 正确。
- 性能修订保持输入 dtype、避免 float64 升级，方向符合项目性能基准要求。

---

## src/features/color/core/video_denoise_gl.py (行数: 145)

### 发现

- [AUD-0900] [Major] RenderSurfaceCache.get 创建失败后，已释放的旧表面残留缓存，可被后续复用（use-after-release）
  - 位置: src/features/color/core/video_denoise_gl.py:108-119
  - 证据:
    ```python
        surface = self._surfaces.get(key)
        if surface is not None and surface.size == (width, height):
            return surface
        if surface is not None:
            surface.release()
        try:
            surface = RenderSurface(self._ctx, width, height, components, dtype)
        except Exception:
            logger.exception("RenderSurfaceCache: failed to create %sx%s surface", width, height)
            return None
        self._surfaces[key] = surface
    ```
  - 影响: 尺寸变化时旧表面先被 release，随后新表面创建失败（OOM/上下文丢失）直接 return None，但 `self._surfaces[key]` 仍指向已释放的旧表面。之后一旦以旧尺寸再次请求（如切回原分辨率片段），`surface.size == (width, height)` 命中，返回已释放表面；调用方 `out.use()` 触发 ModernGL 对已释放对象的异常，被上层兜底成 passthrough 并累计错误直至降噪自动关闭（30+ 次）——表现为持续性的降噪失效而非崩溃，且每次失败都打异常日志刷屏。
  - 建议: 创建失败分支中 `self._surfaces.pop(key, None)`（或先 pop 再创建、失败时不回填），保证缓存内永远是可用对象。
  - 置信度: 高（控制流客观；触发需要表面创建失败这一低概率前置条件）

- [AUD-0901] [Suggestion] get() 忽略已缓存表面的 components/dtype 不匹配；total_bytes() 硬编码 comp=4/f2
  - 位置: src/features/color/core/video_denoise_gl.py:104-105, src/features/color/core/video_denoise_gl.py:139-145
  - 证据:
    ```python
    def get(self, key: Hashable, width: int, height: int,
            components: int = 4, dtype: str = "f2") -> Optional[RenderSurface]:
        ...
    def total_bytes(self) -> int:
        total = 0
        for s in self._surfaces.values():
            w, h = s.size
            comp = 4
            total += w * h * comp * 2  # f2
    ```
  - 影响: 尺寸相同但 components/dtype 不同的请求会静默复用错误格式的表面（当前所有调用方都用默认值，属潜在隐患）；total_bytes 在未来出现非 f2/4 分量表面时统计错误。
  - 建议: 将 (components, dtype) 纳入匹配条件或在 RenderSurface 上记录并在 get 中校验。
  - 置信度: 高（隐患客观，当前无实际触发路径）

- [AUD-0902] [Suggestion] QuadRenderer 以 program.glo 作 VAO 缓存键，存在 GL 对象名复用的误命中风险
  - 位置: src/features/color/core/video_denoise_gl.py:43-52
  - 证据:
    ```python
    def draw(self, program: moderngl.Program) -> None:
        vao = self._vaos.get(program.glo)
        if vao is None:
            try:
                vao = self._ctx.vertex_array(program, [(self._vbo, "2f", "in_pos")])
    ```
  - 影响: 若单个 program 在 QuadRenderer.release() 之外被单独释放，而新 program 复用了同一 glo，会命中为旧 program 构建的 VAO（属性绑定错位）。当前代码路径中 program 只随 QuadRenderer.release() 整体释放（pipeline.release 顺序：spatial/temporal 先 release program，最后 quad.release），暂无实际触发路径。
  - 建议: 防御性改进：以 program 对象 id 或弱引用为键，或在 draw 中校验 vao 归属。
  - 置信度: 低（机制成立，但现有生命周期下不可达）

### 通过项
- 线程契约明确：模块 docstring 声明"所有方法只能在渲染线程调用（持有 ModernGL 上下文时）"，无 UI 线程 GL 调用。
- 热路径零分配：表面按 (key, size) 缓存复用，符合 M6 性能原则。
- release 路径：QuadRenderer.release / RenderSurface.release / release_all 逐对象 try/except，防止单个失败中断整体清理。
- 表面包边：RenderSurface 显式 `repeat_x = repeat_y = False`（clamp-to-edge），配合源纹理侧（async_renderer.py:1060 `tex.repeat_x = False`）满足 shader 对边界采样的假设。
- draw 失败路径（VAO 构建异常）有日志并跳过绘制，不抛出中断播放。

---

## src/features/color/core/video_denoise_pipeline.py (行数: 218)

### 发现

- [AUD-0903] [Major] stats 字典跨线程快照读取与 window_frames 动态键插入存在竞态（dict 迭代中 resize）
  - 位置: src/features/color/core/video_denoise_pipeline.py:56-64, src/features/color/core/video_denoise_pipeline.py:183, src/features/color/core/video_denoise_pipeline.py:196
  - 证据:
    ```python
        # 性能统计（渲染线程内更新，快照读取）
        self.stats: Dict[str, Any] = {
            "frames": 0,
            ...
    ...
                self.stats["window_frames"] = self.stats.get("window_frames", 0) + 1
    ...
        def get_stats(self) -> Dict[str, Any]:
            stats = dict(self.stats)
    ```
  - 影响: 类注释声明 stats 供"快照读取"（即其他线程调 get_stats 拷贝），而渲染线程在 process_window 首次执行时向 dict 插入新键 `window_frames`（改变 dict 大小）。若另一线程恰在执行 `dict(self.stats)`（遍历）则抛 `RuntimeError: dictionary changed size during iteration`，诊断面板读取崩溃。全库 grep 未发现现存跨线程 get_stats 调用方（canvas.py:4100 只读 is_active()，为原子 bool/引用读取，安全），故当前为契约层面的潜在缺陷而非在线故障。
  - 建议: 在 __init__ 中预置全部键（含 window_frames=0），杜绝运行期 dict 扩容；或用锁/不可变快照对象替换裸 dict。
  - 置信度: 中（竞态机制确定；无现存触发调用方，标注为潜在风险）

- [AUD-0904] [Suggestion] process_window 失败路径不计错误、不熔断，与 process 的 _note_error 行为不一致
  - 位置: src/features/color/core/video_denoise_pipeline.py:186-188
  - 证据:
    ```python
        except Exception:
            logger.exception("VideoDenoisePipeline: window process failed")
            return source_tex
    ```
  - 影响: 窗口模式（paused/export）持续失败时不会触发 30 次熔断自动关闭，异常逐帧重复抛出并刷日志；导出路径长时间运行时日志膨胀。spatial 段（189-190 行）在 try 块外，但 VideoSpatialDenoiser.process 内部有自身 try/except 兜底，功能上安全。
  - 建议: process_window 异常分支同样调用 _note_error()。
  - 置信度: 高（不一致客观，影响轻微）

- [AUD-0905] [Suggestion] params property 返回内部可变对象，外部可绕过 sanitized/锁直接改参
  - 位置: src/features/color/core/video_denoise_pipeline.py:76-78
  - 证据:
    ```python
    @property
    def params(self) -> VideoDenoiseParams:
        return self._params
    ```
  - 影响: set_params 入口有类型检查 + sanitized() 副本（69-74 行），但 property 暴露的引用允许调用方在 GL mutex 外原地修改 `pipeline.params.xxx`，渲染线程正在读同一对象时产生撕裂参数（数值读取原子、多字段不一致）。
  - 建议: property 返回副本，或文档化"只读"契约。
  - 置信度: 中（未发现现存原地修改调用方）

### 通过项
- 回退链语义正确：is_active 检查 enabled_flag + params.is_active + 两个子开关；process 异常 passthrough 返回原始纹理，不阻塞播放。
- set_params 走"渲染命令在 GL mutex 下执行"契约（注释明确），类型校验 + sanitized 副本，无跨线程裸写共享状态。
- _note_error 熔断逻辑正确：首错（或 60s 窗口重置）计数归 1，同窗口内 >30 次自动 _enabled_flag=False，防止热路径反复异常。
- process 中 t1/t2 无条件赋值，无"temporal 跳过时 t1 未定义"路径。
- 统计滑动平均公式（EMA，窗口 min(n,120)）数值正确。
- release 顺序正确：先释放持有表面的子模块（spatial/temporal），再 surfaces.release_all()，最后 quad（VBO/VAO）。

---

## src/features/color/core/video_denoise_shaders.py (行数: 559)

### 发现

- [AUD-0906] [Suggestion] 上采样细节恢复着色器含 `residual * keep * 0.0` 死代码
  - 位置: src/features/color/core/video_denoise_shaders.py:266
  - 证据:
    ```glsl
        float y_out = y_den + y_res * y_keep;
        // RGB 整体平移保持色度：用亮度增量平移所有通道，色度不变
        vec3 result = denoised + vec3(y_out - y_den);
        result += residual * keep * 0.0;  // 色度残差不恢复（色度高频≈噪声）
    ```
  - 影响: 意图（抑制色度残差）明确，但以 `* 0.0` 保留会产生驱动编译告警，且 `keep`（254 行计算）在该语句外仅用于自身——读者易误以为色度恢复被门控而非删除；后续维护者可能把 0.0 改成系数引入未预期的色度噪声回注。
  - 建议: 删除该行与未使用的 keep/mag 计算（若确认无其他用途），以注释保留设计说明。
  - 置信度: 高（可读性问题，非功能缺陷）

### 通过项
- 浮点字面量：全文件（含 _COMMON_GLSL 与 9 个片元着色器）数字均带 `.0` 或合法科学计数（`1e9`、`1e-6` 在 GLSL 330 为合法 float 字面量），无裸 int 参与浮点运算。
- 循环边界：所有循环边界为 const（MAX_FAST_RADIUS=4、MAX_SEP_RADIUS=6、MAX_SEARCH=5、字面量 -2..2 等）；u_radius/u_search_radius 通过 `continue` 条件收窄而非充当循环边界，#version 330 下合规。
- HDR 正确性：YCbCr 正/逆变换与全部滤波/累积路径无 clamp(0,1)（132-134 行注释明确"internal float pipeline HDR-safe"）；输出直通 alpha；temporal 混合权重 clamp 的是权重（0.85/0.90）而非颜色。
- UV 越界：SPATIAL/块匹配的邻域采样假设 clamp-to-edge，已核实内部表面（RenderSurface repeat=False）与源纹理（async_renderer.py:1060）均设置 CLAMP；TEMPORAL_ACCUM 的 hist_uv 显式 clamp(vec2(0.0), vec2(1.0)) 正确。
- 除零保护：rat_w 分母有加法偏置；块匹配抛物线插值 den_x/den_y 有 1e-6 保护；置信度归一 max(u_match_scale, 0.0001)；块匹配权重和为 0 的 fallback 到 center。
- 有理权重近似（1/(1+k*x^2)）替代 exp 符合 M1.4/M6 无超越函数约束。
- 场景切换检测同时检查亮度和色度差（471 行），防"同亮度换色"硬切污染递归历史。
- 该文件为纯字符串常量模块，无线程/资源生命周期维度。

---

## src/features/color/core/video_spatial_denoise.py (行数: 237)

### 发现

- [AUD-0907] [Major] 表面缓存键内嵌尺寸 + 缓存无淘汰策略，多分辨率场景 GPU 内存无上界累积
  - 位置: src/features/color/core/video_spatial_denoise.py:86, src/features/color/core/video_spatial_denoise.py:111-112, src/features/color/core/video_spatial_denoise.py:157-160, src/features/color/core/video_spatial_denoise.py:214
  - 证据:
    ```python
        out = self._surfaces.get(("spatial_out", width, height), width, height)
    ...
        tmp = self._surfaces.get(("spatial_mid", width, height), width, height)
        out = self._surfaces.get(("spatial_out", width, height), width, height)
    ...
        low = self._surfaces.get(("enh_low", half_w, half_h), half_w, half_h)
    ```
    缓存侧（video_denoise_gl.py:108-110）仅在"同 key 且尺寸匹配"时复用、不匹配时释放重建；键本身含尺寸 ⇒ 不同尺寸各占一个条目且永不淘汰。temporal 侧同模式（video_temporal_denoise.py:198-206、427-430 多处 `(key, w, h)`）。
  - 影响: 4K 下单张 RGBA16F 表面 ≈ 3840*2160*8 ≈ 66MB；spatial 键 5 个（out/mid/detail_out/enh_low*3 半分辨率 ≈ 16MB each）+ temporal 全分辨率 hist_a/hist_b/out/debug 4 张 66MB —— 在 1080p/4K 片段间反复切换（时间线多片段混合分辨率、或 export 桥 headless 管线与预览管线各自独立持有一套）时，每种历史尺寸永久驻留，GPU 显存占用随出现过的分辨率种类线性增长，4K 实时路径上构成显存压力与分配颠簸风险。
  - 建议: 键中去掉尺寸（cache 已有尺寸不匹配重建逻辑，语义等价且天然有界），或在 RenderSurfaceCache 增加按容量/LRU 的淘汰。
  - 置信度: 高（机制与显存量客观；实际影响取决于用户分辨率切换行为）

- [AUD-0908] [Suggestion] enhanced 上采样 pass 注释与代码不符（"固定 0.5" vs 实际传 p.detail_recovery）
  - 位置: src/features/color/core/video_spatial_denoise.py:194, src/features/color/core/video_spatial_denoise.py:203
  - 证据:
    ```python
        # 3. 上采样 + 细节恢复（detail_recovery 由独立 pass 统一处理，这里固定 0.5）
        out.use()
        ...
        up_prog["u_detail_recovery"].value = p.detail_recovery
    ```
  - 影响: 行为本身自洽（enhanced 在 process() 66-67 行 early-return，不会叠加独立 detail recovery pass，恢复强度由上采样着色器内的 u_detail_recovery 控制），但注释描述的"固定 0.5 + 独立 pass 统一处理"与实现相反，误导后续维护——若有人按注释把 66-67 行改为落入统一 detail recovery 分支，会造成恢复双重施加。
  - 建议: 修正注释为"enhanced 模式由上采样 pass 内的 u_detail_recovery=p.detail_recovery 处理，不走独立 pass"。
  - 置信度: 高（已核对 process() 控制流确认无双重施加）

- [AUD-0909] [Suggestion] 类型注解不完整
  - 位置: src/features/color/core/video_spatial_denoise.py:37, src/features/color/core/video_spatial_denoise.py:55, src/features/color/core/video_spatial_denoise.py:210
  - 证据:
    ```python
        self._programs: dict = {}

    def process(self, source_tex, params: SpatialDenoiseParams) -> moderngl.Texture:
    ...
    def _apply_detail_recovery(self, original, denoised, p, width, height):
    ```
  - 影响: `dict` 无参数化类型；process/_apply_detail_recovery 等方法的 source_tex/original/denoised/p/width/height 无注解，与项目强类型约束不符。
  - 建议: 补 `Dict[str, moderngl.Program]`、`moderngl.Texture`、`SpatialDenoiseParams`、`Tuple[int, int]` 注解。
  - 置信度: 高

### 通过项
- 线程契约：类 docstring 明确"仅渲染线程"，无 UI 线程 GL 调用。
- 失败语义：所有 GL 操作（program 编译、表面获取、draw）失败均回退返回 source_tex，process 顶层 try/except 兜底并 logger.exception（非静默吞错），符合"任何异常不阻塞播放"。
- pass 间无读写反馈回路：separable 读 source→tmp 读 tmp→out；enhanced 读 source/low_out/low→out；detail recovery 读 source/denoised→detail_out，目标与源键均不重叠。
- viewport 均在 target.use() 之后设置，半分辨率 pass 用 half_w/half_h 正确。
- enhanced 降级链：program/表面获取失败自动降级 separable，再由 process 兜底 passthrough。
- 半径参数钳制正确（fast int(p.radius)、better min(r,6)、enhanced min(max(r,2),6)），与着色器 MAX 常量一致。
- 小尺寸守卫（width<8 or height<8 直通）与非激活参数直通。

---

## 汇总统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 7 | AUD-0900, AUD-0903, AUD-0907, AUD-0910, AUD-0911, AUD-0912, AUD-0913 |
| Suggestion | 9 | AUD-0901, AUD-0902, AUD-0904, AUD-0905, AUD-0906, AUD-0908, AUD-0909, AUD-0914, AUD-0915 |

共 16 条。
