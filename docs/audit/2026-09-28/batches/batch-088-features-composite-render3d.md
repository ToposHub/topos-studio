# 批次报告：batch-088 — src/features/composite/render3d

- 审查日期：2026-09-28
- 任务书：docs/audit/2026-09-28/assignments/batch-088.md
- 编号区间：AUD-8800–AUD-8899
- 文件数：7（2,974 行），全部 Read 通读
- 汇总：Critical 1 / Major 2 / Suggestion 15

---

## src/features/composite/render3d/fields.py (行数: 165)

### 发现

- [AUD-88001] [Suggestion] falloff 字符串未校验，`_VALID_FALLOFFS` 常量定义后从未使用
  - 位置: src/features/composite/render3d/fields.py:29
  - 证据:
    ```python
    _VALID_FALLOFFS = {FALLOFF_NONE, FALLOFF_LINEAR, FALLOFF_SMOOTH, FALLOFF_RADIAL}
    ```
    以及 fields.py:52-53（`_apply_falloff` 的兜底分支）：
    ```python
    else:
        w = 1.0 - t  # 默认线性
    ```
  - 影响: 调用方传入拼写错误的 falloff（如 "smoth"）不报错，静默按线性衰减，权重分布与预期不符且难以排查；模块级校验集合形同虚设。
  - 建议: 在 `FieldSpec.__post_init__` 中用 `_VALID_FALLOFFS` 校验 `falloff`，非法值抛 `ValueError`（或记日志并回落默认值）。
  - 置信度: 高

### 通过项
- 线程安全：纯 numpy 无共享可变状态（每个 FieldSpec 实例持有自己的数组副本）
- GLSL·HDR 正确性：不涉及 GL；权重输出有 `np.clip(0.0,1.0)` 约束，属权重域而非 HDR 数据域，合理
- 异常与取消路径：空顶点数组（n==0）、零范数轴向（n>1e-9 守卫）、全零 size（avg_half 兜底 1.0）均有守卫
- 输入验证：center/axis/size 强制 reshape(3) + dtype 转换（长度错误会显式抛 ValueError）
- 性能模式：全向量化实现，无逐元素 Python 循环
- 类型注解：完整（公开函数均有注解）

---

## src/features/composite/render3d/format_capabilities.py (行数: 798)

### 发现

- [AUD-88010] [Major] `convert_vertices` 的 Z-up→Y-up 轴翻转是镜像（行列式 -1）而非旋转，几何会被翻面
  - 位置: src/features/composite/render3d/format_capabilities.py:786-789
  - 证据:
    ```python
    if self.needs_axis_flip:
        # Z up → Y up：swap Y/Z（x,y,z → x,z,y）
        v = v[:, [0, 2, 1]]
    if self.unit_scale != 1.0:
        v *= self.unit_scale
    ```
  - 影响: 纯分量交换 `[0,2,1]` 是置换行列式为 -1 的反射：旧 +Y（Z-up 源的前向）映射到新 +Z（目标的后向），正确旋转（绕 X 轴 -90°）应得到 `(x, z, -y)`。一旦该转换器被 loader 采用，模型会左右镜像、三角形绕序反转、法线/光照反转。当前全库无调用者（grep 确认仅定义处），属"明确风险"的潜伏缺陷；且只转顶点不同时修正 winding/法线也与之相关。
  - 建议: 改为旋转实现（`v = v[:, [0, 2, 1]] * np.array([1, 1, -1], ...)` 即 (x, z, -y)，或用旋转矩阵），并在注释中说明绕序/法线需同步处理；补一个已知向量的单元测试（如 (0,1,0)→(0,0,-1)）。
  - 置信度: 高（数学推导确凿）；影响评级基于当前无调用者的事实

- [AUD-88011] [Suggestion] 冗余的函数内 `import os`；`ModelLoaderBackend.load` 缺返回类型注解
  - 位置: src/features/composite/render3d/format_capabilities.py:373、458、449-454
  - 证据:
    ```python
    def detect_format(path: str) -> Optional[FormatCapability]:
        ...
        import os
        ext = os.path.splitext(path)[1].lower()
    ```
    以及：
    ```python
    def load(self, path: str, normalize: bool = False):
        """加载模型，返回 SceneGraphRoot（含 import_report）。"""
    ```
  - 影响: 模块顶部第 25 行已 `import os`，函数内重复导入徒增噪音；`load` 作为公共 ABC 接口返回类型仅存在于 docstring，调用方与类型检查器无法获得契约。
  - 建议: 删除两处局部 `import os`；为 `load` 标注返回类型（如 `-> "SceneGraphRoot"` 或 TYPE_CHECKING 前向引用）。
  - 置信度: 高

### 通过项
- 线程安全：`FormatCapability` 为 frozen dataclass；`LOADER_REGISTRY` 为模块级可变 dict，但注册设计为启动期 `ensure_builtin_loader_backends()` 幂等注册，运行期只读
- 异常与取消路径：backend 可用性探测的宽 `except Exception` 有明确注释且仅用于软依赖探测（返回 False 不掩盖加载阶段错误），可接受；`probe_import` 的 reason_code 分支完整（unknown/planned/missing/unavailable/ready）
- 输入验证：`probe_import`/`resolve_asset_path` 对空路径、PathLike、不存在文件均有处理；`unit_to_meter` 未知单位显式回落 1.0
- 性能模式：`detect_format`/`supported_extensions` 为查表操作，开销可忽略
- 状态提交一致性：纯查询模块，不涉及 undo/持久化

---

## src/features/composite/render3d/frustum.py (行数: 183)

### 发现

- [AUD-88020] [Suggestion] `aabb_screen_size` 文档承诺"<0 表示 AABB 在相机后"，实现不存在任何返回负值的路径（死注释）
  - 位置: src/features/composite/render3d/frustum.py:108、123-125、131
  - 证据:
    ```python
    Returns:
        估算的屏幕高度像素（float）。<0 表示 AABB 在相机后（应剔除）。
    ...
    # 相机后（dot < 0）→ 剔除
    # 角度尺寸 = diag / dist（弧度近似，小角度）
    angular_size = diag / dist
    ...
    return angular_size / fov_rad * float(viewport_height_px)
    ```
  - 影响: 距离用欧氏范数而非视空间深度，`angular_size` 恒 ≥ 0，函数永不返回负值；注释里的"dot < 0 → 剔除"分支从未实现。依赖该契约的调用方会对相机背后的物体得出正的屏幕尺寸而永不剔除。当前唯一调用方 scene3d_node.py:4326 先做视锥剔除（`aabb_in_frustum` 已处理相机后方）且判断写成 `0.0 < _screen_px < _small_obj_px`，风险被实际缓解，但文档契约与实现不符会误导后续调用者。
  - 建议: 二选一：实现视轴方向判断（`dist_vec @ cam_forward < 0` 时返回 -1.0）；或修正 docstring 删除"<0"承诺与死注释。
  - 置信度: 高（契约与实现的不一致确凿）

- [AUD-88021] [Suggestion] `mesh_local_aabb` 全库无调用者，与 `primitives.MeshData.local_aabb` 重复实现且缺少失效机制
  - 位置: src/features/composite/render3d/frustum.py:167-183
  - 证据:
    ```python
    def mesh_local_aabb(mesh) -> Tuple[np.ndarray, np.ndarray]:
        """mesh（MeshData）的 local-space AABB：对 vertices 取 min/max。

        缓存到 mesh._local_aabb 避免每帧重算（vertices 不变）。
        """
        cached = getattr(mesh, "_local_aabb", None)
        if cached is not None:
            return cached
    ```
  - 影响: grep 确认该函数无任何调用（scene3d_node 仅导入 `transform_aabb`/`aabb_screen_size`）；`primitives.py` 自带带初始化失效（`__init__` 置 None）的同名逻辑。若未来被误用，顶点被修改（morph/编辑）后 `mesh._local_aabb` 永不失效，视锥剔除会用陈旧 AABB 导致物体被错误剔除（消失）。
  - 建议: 删除该函数，或让其委托 `mesh.local_aabb()` 并补类型注解与失效说明。
  - 置信度: 高（无调用者与重复事实）；影响评级为潜伏风险

### 通过项
- 线程安全：纯 numpy 无状态函数（Gribb-Hartmann 行提取与 p-vertex 测试数学正确，法向归一化含零长度守卫）
- 性能模式：`transform_aabb` 用 8 角点一次矩阵乘；`aabb_in_frustum` 每帧 6 平面 × 3 分支，开销极小
- GLSL·HDR 正确性：不涉及 GL；无 clamp 破坏数据
- 类型注解：除 `mesh_local_aabb(mesh)` 参数外完整

---

## src/features/composite/render3d/gizmo_overlay.py (行数: 656)

### 发现

- [AUD-88030] [Suggestion] `update()` 之前调用 `build_draw_list()`/`begin_drag()` 会因 `_proj`/`_view` 为 None 崩溃（防御缺口）
  - 位置: src/features/composite/render3d/gizmo_overlay.py:150-151、293、407-409、608-609
  - 证据:
    ```python
    self._proj: Optional[np.ndarray] = None
    self._view: Optional[np.ndarray] = None
    ...
    clip = self._proj @ self._view @ p   # _world_to_tex_ndc
    ...
    def _build_center_draw(self) -> GizmoCenterDraw:
        return GizmoCenterDraw(center_uv=self._world_to_tex_uv(self.object_pos))
    ```
  - 影响: 构造后 `_proj/_view` 为 None；`_build_center_draw` 在 `build_draw_list()` 三种模式下都会无条件执行（轴/环为空列表不拦截它），`None @ None` 抛 TypeError；`begin_drag` 里 `raycast.screen_to_ray(..., self._proj, self._view)` 对 None 走 `np.asarray(None, dtype=np.float64)` 同样抛 TypeError。当前调用方（composite_viewport.py）每帧 paint 先 `update()`（4643/4651/4659）再取 draw_list（5837），顺序正确，故为防御缺口而非现行 bug。
  - 建议: 在 `build_draw_list`/`begin_drag`/`_world_to_tex_ndc` 入口检查 `self._proj is None or self._view is None` 时返回空列表/直接 return。
  - 置信度: 高（崩溃路径确凿；可达性取决于调用顺序，当前调用方正确）

- [AUD-88031] [Suggestion] `drag_rotate` 实现与注释矛盾：注释称按轴取主导屏幕分量，实现对所有轴一律 `(dx+dy)*90`
  - 位置: src/features/composite/render3d/gizmo_overlay.py:568-573
  - 证据:
    ```python
    # 用较大的屏幕分量作为旋转驱动（X 轴旋转看 dy，Y 轴看 dx，Z 轴看 dx）
    ...
    delta_deg = (dx + dy) * 90.0
    new_rot = base_rot + delta_deg
    ```
  - 影响: 绕 X 轴旋转时水平移动也会叠加角度，拖动手感与 C4D 惯例（垂直于环的方向驱动）不符，侧向拖动产生非预期旋转。
  - 建议: 按轴取驱动分量（X→dy，Y/Z→dx），或按环在屏幕上的投影切向计算（完整 trackball 见注释中的 Phase 6 计划）。
  - 置信度: 高

- [AUD-88032] [Suggestion] 类型注解缺失；`end_drag` 清理从未被设置的 `_drag_anchor` 属性
  - 位置: src/features/composite/render3d/gizmo_overlay.py:142、169-170、623-629
  - 证据:
    ```python
    def __init__(self):
    ...
    def update(self, object_pos, proj, view, tex_w_px: int, tex_h_px: int,
               cam_dist: float, cam_fov: float) -> None:
    ...
    for attr in ("_drag_anchor", "_drag_anchor_pos", "_drag_anchor_ndc",
                 "_drag_start_rot", "_drag_start_scale",
                 "_drag_axis_start_t", "_drag_plane_start_hit"):
    ```
  - 影响: `__init__` 缺 `-> None`，`update` 的 object_pos/proj/view 无注解；`_drag_anchor` 从未被赋值（实际是 `_drag_anchor_pos`/`_drag_anchor_ndc`），清理列表含死条目。
  - 建议: 补注解（`object_pos: np.ndarray, proj: np.ndarray, view: np.ndarray`）；从清理列表删除 `"_drag_anchor"`，或在 `begin_drag` 统一初始化全部拖动属性。
  - 置信度: 高

### 通过项
- 线程安全：全部状态在 UI 线程维护（docstring 明确），无渲染线程共享
- 信号防递归：模块不含 Qt 信号，拖动结果以参数 dict 返回由调用方提交（解耦正确）
- 状态提交一致性：`begin_drag` 记录锚点/起始值，`drag_offset` 返回增量参数，`end_drag` 清理完整（plane/axis 锚点均覆盖），配合调用方 undo 由节点层负责
- 异常与取消路径：`ray_axis_intersect` 平行时返回 0.0（raycast.py:100-102 已确认），`_ray_plane_intersect` 平行返回 None 且 `_drag_move_plane` 对 None 优雅返回 None
- 输入验证：`update` 对 tex_w/tex_h/cam_dist 做 max 下限；hit_test 对空几何各分支有早退
- 性能模式：每帧 O(轴数×段数) 的轻量 2D 距离计算，无可疑热点

---

## src/features/composite/render3d/gizmo_renderer.py (行数: 365)

### 发现

- [AUD-88040] [Suggestion] `_build` 无异常安全：中途失败后重试会泄漏已创建的 GL 对象；GL 上下文重建后 `_built` 不复位
  - 位置: src/features/composite/render3d/gizmo_renderer.py:172-224、258
  - 证据:
    ```python
    def _build(self, ctx) -> None:
        if self._built:
            return
        self._grid_prog = ctx.program(...)
        ...
        self._grid_vbo = vbo
        # ── 世界坐标轴 ──
        self._axes_prog = ctx.program(...)
        ...
        self._built = True
    ```
    以及 draw() 首行 `self._build(ctx)`。
  - 影响: 若第二次 `ctx.program`（坐标轴）编译失败，异常抛出时 `_built` 仍为 False，但网格 program/VAO/VBO 已赋值；下次 draw 重入 `_build` 会再次 `ctx.program`/`ctx.buffer` 覆盖旧引用，旧对象在 owner 调 `release()` 前泄漏（VAO 不强持有 VBO，注释自述）。另外若外层换了新 ctx 而未先 `release()`，`_built=True` 会继续使用旧 context 的陈旧对象，触发 GL 错误。当前依赖 Scene3DNode 的 cleanup 纪律，属边界风险。
  - 建议: `_build` 用 try/except 包裹，失败时立即释放已创建对象再重抛；`_build` 记录 `self._ctx = ctx`，发现 ctx 变化时先 release 再重建。
  - 置信度: 中（泄漏路径逻辑确凿；是否实际触发取决于 program 编译失败/ctx 重建是否发生）

- [AUD-88041] [Suggestion] 类型注解缺失（`__init__` 无 `-> None`，ctx/矩阵参数未注解）
  - 位置: src/features/composite/render3d/gizmo_renderer.py:165-171、226-238
  - 证据:
    ```python
    def __init__(self):
        self._grid_prog: Optional[moderngl.Program] = None
    ...
    def draw(self, ctx, proj: np.ndarray, view: np.ndarray,
             cam_target=None, ...
    ```
  - 影响: 与项目"所有函数有类型注解"约束不一致；`ctx`、`cam_target`、多个颜色参数未注解。
  - 建议: 补 `-> None` 与 `ctx: "moderngl.Context"`、`cam_target: Optional[np.ndarray]`、`tuple[float,float,float]` 等注解。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：`release()` 完整释放 VAO/VBO/program 三类对象并置 None + `_built` 复位；VBO 显式保引用防 GC（VAO 不强持有的已知 ModernGL 行为）
- GLSL·HDR 正确性：两套 shader 数字字面量全部带小数点（0.90、2.0、4.0、0.0001 等）；无循环；uniform 均经 Python 侧 clamp/下限防御后写入
- 线程安全：由 Scene3DNode 在渲染线程调用（docstring 明确），与 UI 线程 OpenGL 铁律一致
- 异常与取消路径：`glDepthMask` 的宽 except 为文档化的降级策略（PyOpenGL 不可用时退化为普通绘制，不拖垮 Viewer），恢复路径在 finally 中对称执行；BLEND enable/disable 平衡
- 输入验证：颜色分量、cell_size、line_width、fade 区间均在 Python 侧 clamp/取下限
- 性能模式：几何一次性懒构建；`u_grid_plane` uniform 存在性检查兼容旧缓存 program

---

## src/features/composite/render3d/hdri.py (行数: 705)

### 发现

- [AUD-88050] [Critical] 辐照度卷积遗漏 sin(theta) 立体角权重，漫反射 IBL 积分在数学上错误（渲染错误）
  - 位置: src/features/composite/render3d/hdri.py:121-133（关键行 130）
  - 证据:
    ```python
    for (float theta = 0.0; theta < 0.5 * PI; theta += 0.025) {  // 只在上半球
        float sin_theta = sin(theta);
        float cos_theta = cos(theta);
        // 球面坐标 → 笛卡尔（切线空间）
        vec3 tangent = vec3(sin_theta * cos_phi, sin_theta * sin_phi, cos_theta);
        vec3 sample_dir = tangent.x * T + tangent.y * B + tangent.z * N;
        irradiance += texture(u_env, sample_dir).rgb * cos_theta;  // cosine 加权
    ```
  - 影响: 球面 Riemann 求和的面积元 dω = sinθ·dθ·dφ，累积项必须乘 `cos_theta * sin_theta`（模块头引用的 learnopengl 参考实现即为 `* cos_theta * sin_theta`）。当前实现把 (φ,θ) 网格当等面积采样：极点附近（sinθ→0）样本被严重超权、赤道附近欠权。后果是漫反射环境光的能量与方向分布系统性偏差（天空/顶光被过量混入所有法线方向），所有使用 HDR 环境光的 3D 场景渲染结果错误。`sin_theta` 已在 125 行声明但只用于构造方向（128 行），未参与权重。
  - 建议: 130 行改为 `irradiance += texture(u_env, sample_dir).rgb * cos_theta * sin_theta;`，重烘焙验证（纯色天空应无变化，渐变/带太阳的 HDR 应观察到正确能量分布）。
  - 置信度: 高

- [AUD-88051] [Major] `load_equirect` 对单通道（灰度）全景图崩溃：cv2 返回二维数组，`img.shape[2]` 抛 IndexError（外部输入未验证）
  - 位置: src/features/composite/render3d/hdri.py:302-314
  - 证据:
    ```python
    img = cv2.imread(path, cv2.IMREAD_ANYDEPTH | cv2.IMREAD_ANYCOLOR)
    if img is None:
        raise ValueError(f"HDRI 加载失败（cv2 返回 None）: {path}")
    is_ldr = img.dtype != np.float32 and img.dtype != np.float16
    if img.dtype != np.float32:
        img = img.astype(np.float32)
    # OpenCV 默认 BGR → 转 RGB
    if img.shape[2] >= 3:
    ```
  - 影响: `IMREAD_ANYCOLOR` 加载灰度 jpg/png/EXR 时返回 (H,W) 二维数组，`img.shape[2]` 直接 IndexError（而非 docstring 承诺的结构化 ValueError）；即使是 (H,W,1) 三维形态，后续 `ctx.texture((w,h),3,...)` 上传也会因分量数不符失败。用户拖入灰度图即触发未捕获崩溃路径（取决于上层是否兜住 IndexError）。
  - 建议: 加载后立即 `if img.ndim == 2: img = np.stack([img]*3, axis=-1)`（灰度复制三通道）；`elif img.shape[2] == 1:` 同理；再进入颜色转换。
  - 置信度: 高（崩溃路径确凿）；触发频率取决于用户输入灰度图的概率

- [AUD-88052] [Suggestion] `_integrate_brdf_lut` 每次创建新 FBO 和空 VAO 且不显式 release，与其余 program 走 `_vaos` 缓存的模式不一致
  - 位置: src/features/composite/render3d/hdri.py:610-623
  - 证据:
    ```python
    tex = ctx.texture((size, size), 2, dtype='f4')
    fbo = ctx.framebuffer(color_attachments=[tex])
    fbo.use()
    ...
    vao = ctx.vertex_array(prog, [])
    vao.render(moderngl.TRIANGLE_STRIP, vertices=3)
    ...
    # 恢复默认 FBO（调用方负责再 use 自己的目标）
    return tex
    ```
  - 影响: fbo 与 vao 为局部变量，靠 CPython 引用计数回收触发 ModernGL 析构释放；项目整体采用显式 deferred-release 模式，靠 GC 释放 GL 对象与既定资源管理纪律不一致（换解释器/出现引用滞留时泄漏 FBO 名）。BRDF program 的 VAO 也没有复用 `_vao_for` 缓存，每次 bake 都新建。
  - 建议: 用 `self._vao_for(ctx, "brdf", _BRDF_LUT_FS)` 复用 VAO；FBO 用完 `fbo.release()`（纹理已 detach 使用完毕后释放 FBO 不影响 tex）。
  - 置信度: 高

- [AUD-88053] [Suggestion] 多处 `while glGetError() != GL_NO_ERROR: pass` 清空全局 GL 错误标志，会吞掉无关代码的真实 GL 错误
  - 位置: src/features/composite/render3d/hdri.py:443-444、524-525、633-634、697-698
  - 证据:
    ```python
    # 清空 bake 过程遗留的 GL 错误（FBO 渲染/cubemap 卷积可能留下错误标志，
    # 会被 PyOpenGL 错误检查器误报到后续 _RawCube.use() 的 glActiveTexture 上）
    while gl.glGetError() != gl.GL_NO_ERROR:
        pass
    ```
  - 影响: 注释说明是为规避 PyOpenGL 错误检查器的误报，但 glGetError 队列是进程内上下文全局的：这里会把此前任何模块（主渲染管线、其他节点）遗留在队列中的真实错误一并清空，导致真正的 GL 故障无法被上游错误检查发现，掩盖渲染问题。
  - 建议: 缩小清错范围——仅在明确的 raw GL 调用序列前后配对检查并记录日志（错误非 0 时 logger.warning 再继续），而不是无条件清空。
  - 置信度: 高（行为确凿；严重度取决于队列中是否有待消费的真实错误）

- [AUD-88054] [Suggestion] 辐照度着色器循环边界非常量（float 累加 phi/theta），不符合项目"循环上限编译期常量"铁律
  - 位置: src/features/composite/render3d/hdri.py:120-132
  - 证据:
    ```python
    float phi_step = 0.025;   // 经度步长
    for (float phi = 0.0; phi < 2.0 * PI; phi += phi_step) {
        ...
        for (float theta = 0.0; theta < 0.5 * PI; theta += 0.025) {  // 只在上半球
    ```
  - 影响: 桌面 GLSL 330 可以编译，但违反 .clinerules 循环上限须为编译期常量的约束；float 累加步进的舍入使迭代次数对实现敏感（边界迭代可能执行与否不一），且与同文件 prefilter/BRDF shader 的 `for (uint i = 0u; i < 1024u; ++i) { if (i >= N_samples) break; }` 风格不一致。
  - 建议: 改为常量上限 + break 的同款模式（`const uint PHI_STEPS = 251u; const uint THETA_STEPS = 63u;` 双重常量循环），同时消除 float 累加误差。
  - 置信度: 高

- [AUD-88055] [Suggestion] bake 结束后 viewport/FBO 状态不恢复（依赖调用方自律）；缓存键文档与实现不一致（docstring 称含 size，实际 key 无 size）
  - 位置: src/features/composite/render3d/hdri.py:368、387、622-623
  - 证据:
    ```python
    结果按 (path, mtime, 分辨率, 采样数) 缓存，相同输入不重 bake。   # docstring
    ...
    key = (equirect_path, mtime, _ENV_CUBEMAP_SIZE, _PREFILTER_SAMPLES)
    ...
    # 恢复默认 FBO（调用方负责再 use 自己的目标）
    return tex
    ```
  - 影响: bake 过程中 `glViewport` 被多次改写，结束时停在 BRDF LUT 的 512×512（或 raw FBO 绑定状态）；若调用方忘记立即重设 viewport/FBO，后续渲染画到错误区域。当前常量为模块级固定值、分辨率维度实际不变化，缓存键缺 size 暂无实际影响，但 docstring 误导维护者。
  - 建议: `_bake_from_array` 结尾显式保存/恢复进入时的 GL_VIEWPORT 与 FBO 绑定（glGetIntegerv/glBindFramebuffer），或至少在 bake() docstring 声明状态契约；修正缓存键文档。
  - 置信度: 高

- [AUD-88056] [Suggestion] LDR 全景图按图像自身 max 归一化（而非位深满量程），暗图被整体提亮，相对辐射度失真
  - 位置: src/features/composite/render3d/hdri.py:316-323
  - 证据:
    ```python
    if is_ldr:
        max_val = float(img.max()) if img.max() > 0 else 1.0
        # 常见位深：uint8→255, uint16→65535。按实际 max 归一化更稳健。
        if max_val > 1.0:
            img = img / max_val
        img = np.power(np.clip(img, 0.0, 1.0), 2.2).astype(np.float32)
    ```
  - 影响: 一张最亮像素为 30/255 的暗环境图会被拉伸 8.5 倍，最亮点强制变 1.0；不同 LDR 环境之间的相对亮度关系被破坏（每张图都被"自动曝光到满档"）。注释称"更稳健"，但对 IBL 而言这是辐射度失真；仅当图像低于位深满量程存储（罕见）时该策略才有收益。
  - 建议: 按 dtype 位深归一化（uint8→255、uint16→65535），保留相对曝光；或提供显式的曝光补偿参数。
  - 置信度: 中（数学后果确凿；因注释表明是有意取舍，故定级 Suggestion）

### 通过项
- GPU 资源生命周期（主体）：`release()` 覆盖 ModernGL 纹理、raw GL cubemap 名、raw FBO、VAO/program 并全部置空/清 dict，幂等可重入；`_bake_from_array` 先 release 旧资源再重建，缓存键含 mtime 防陈旧
- GLSL·HDR 正确性（除 AUD-88050 外）：equirect→cube 的经纬映射、prefilter GGX 重要性采样（UE4 公式）、BRDF LUT 的 Schlick-GGX/Smith 与 k=roughness²/2 IBL 变体均正确；prefilter/BRDF 循环用常量上限 + break；内部格式 RGBA16F 保持 HDR 无 clamp；`_RawCube` 混入 ModernGL 绑定流的封装接口正确
- 输入验证（除 AUD-88051 外）：文件不存在走黑色占位路径（PBR 仍可渲染）；`glTexStorage2D` 失败显式检查错误码并回退 `glTexImage2D`，回退失败抛 RuntimeError
- 线程安全：docstring 明确必须渲染线程调用；无跨线程共享状态
- 异常与取消路径：cv2 返回 None 抛结构化 ValueError；占位纹理保证无 HDRI 时管线可继续
- 性能模式：缓存键短路重复 bake；env cubemap 128 中间产物 + 32/128 低分辨率卷积目标，一次性 bake 成本可控

---

## src/features/composite/render3d/ies_profile.py (行数: 102)

### 发现

- [AUD-88061] [Suggestion] IES 角度表单调性未验证：`sample_profile` 的线性扫描假设严格升序，降序表（LM-63 允许）会得到错误插值
  - 位置: src/features/composite/render3d/ies_profile.py:46-56
  - 证据:
    ```python
    angle = max(profile.vertical_angles[0],
                min(float(angle_deg), profile.vertical_angles[-1]))
    for index in range(1, len(profile.vertical_angles)):
        right = profile.vertical_angles[index]
        if angle <= right:
            left = profile.vertical_angles[index - 1]
            ...
            span = max(right - left, 1e-6)
            return lv + (rv - lv) * (angle - left) / span
    ```
  - 影响: LM-63 的垂直角度可按降序给出。降序表下 clamp 边界反转（min/max 先后失效），`angle <= right` 立即命中首个区间且 `span` 被 1e-6 兜底，插值退化为几乎 lv 恒定，光形分布错误（灯光渲染方向性失真，但不崩溃）。外部文件数据未按"外部数据须验证"铁律做单调性校验。
  - 建议: `load_ies_profile` 解析后校验 `vertical_angles` 严格单调（任一方向），非法抛 ValueError；或归一化为升序存储。
  - 置信度: 中（降序 IES 出现频率不高，但规范允许）

### 通过项
- 线程安全：frozen dataclass + 无状态解析函数
- GPU 资源生命周期：不涉及 GL（纯 CPU 解析，符合模块定位）
- GLSL·HDR 正确性：candela 经 `max(0.0, ...)` 与 scale（multiplier/lamp_count）归一，峰值除法含 1e-6 下限
- 异常与取消路径：文件不存在/无 TILT=NONE/数值段不完整/角度计数非法均抛带信息的 ValueError（结构化、可被节点转警告）
- 输入验证：正则仅从 TILT 标记之后取数（避免文件头数字污染）；表格长度不足显式校验；多 horizontal 切片取平均而不是盲取第一片
- 性能模式：4 样本采样成本可忽略；解析一次、frozen 缓存
- 类型注解：完整

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-88050 |
| Major | 2 | AUD-88010、AUD-88051 |
| Suggestion | 15 | AUD-88001、88011、88020、88021、88030、88031、88032、88040、88041、88052、88053、88054、88055、88056、88061 |
