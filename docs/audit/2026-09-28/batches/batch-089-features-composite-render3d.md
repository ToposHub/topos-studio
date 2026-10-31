# Batch 089 审查报告：src/features/composite/render3d/（8 文件，2,843 行）

- 编号区间：AUD-8900–AUD-8999（实际使用 AUD-8900–AUD-8920）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
- 交叉验证（只读）：`primitives.py`（MeshData/TextureDescriptor/MaterialData 定义）、`scene3d_node.py` / `model3d_node.py` / `deformer3d_node.py` / `material_panel.py`（调用方）、`composite_renderer.py`（release_context 调用点）
- 统计：Critical 1 / Major 4 / Suggestion 16

---

## src/features/composite/render3d/lod_builder.py (行数: 261)

### 发现

- [AUD-8900] [Suggestion] 多处宽泛 `except Exception` 静默吞错，其中 `choose_lod_level` 连日志都没有
  - 位置: src/features/composite/render3d/lod_builder.py:83、117、251、260
  - 证据:
    ```python
    # _estimate_cluster_size (83-84)
    except Exception:
        return 0.0
    # choose_lod_level (251-252)
    except Exception:
        pass
    # triangle_count (260-261)
    except Exception:
        return 0
    ```
  - 影响: 违反 `.clinerules`「不得宽泛 except 掩盖领域错误」；`_estimate_cluster_size` 吞掉 numpy 形状错误后会退化成 cell=0（不简化）而非暴露坏 mesh 数据；`choose_lod_level`/`triangle_count` 静默返回默认值，问题模型难排查。
  - 建议: 至少 `logger.debug(..., exc_info=True)`（`build_lod` 已有此写法，统一即可）；对形状类错误显式捕获。
  - 置信度: 高

### 通过项
- 线程安全：纯 numpy 无共享可变状态，任意线程可调用
- GPU 资源生命周期：无 GL 对象
- 输入验证：`build_lod` 对蒙皮/morph/顶点过少/目标 1.0 全部安全降级返回 `_copy_mesh`
- 聚类实现正确性：cell key 编码无碰撞（先偏移非负）、`np.add.at` 累加均值、法线归一化、退化三角形（三顶点同 cell）剔除均正确
- 蒙皮 mesh 排除（`is_skinned`/`has_morph_targets`）防止权重丢失
- 性能模式：纯向量化，无 Python 逐顶点循环

---

## src/features/composite/render3d/material_model.py (行数: 668)

### 发现

- [AUD-8901] [Critical] `material_override_to_dict` 静默丢弃 list 类型值，材质面板的 base_color 覆盖无法持久化（保存/重载后丢失）
  - 位置: src/features/composite/render3d/material_model.py:377-384
  - 证据:
    ```python
    scalar_values: Dict[str, Any] = {}
    for key, value in override.overrides.items():
        if isinstance(value, np.ndarray):
            scalar_values[str(key)] = value.tolist()
        elif isinstance(value, (np.floating, np.integer)):
            scalar_values[str(key)] = value.item()
        elif isinstance(value, (str, int, float, bool)) or value is None:
            scalar_values[str(key)] = value
    ```
  - 影响: 调用方 `src/features/composite/ui/material_panel.py:348` 写入 `override.set_scalar("base_color", [spin.value() for spin in self._rgb])`、`:371` 写入 `preset.model.base_color.tolist()`——都是普通 Python list，不匹配任何 `isinstance` 分支，被无声丢弃。`_emit_overrides` → `encode_material_overrides` 生成的节点参数 JSON 中缺少 base_color，工程保存后重载时用户对材质颜色的修改全部丢失（数据丢失）；而内存中的 `apply_override` 用 `np.asarray(value)` 接受 list，所以会话内正常、重载即丢，极难被用户察觉。反序列化侧 `material_override_from_dict` 反而接受 list（原样透传），读写下不对称。
  - 建议: 序列化分支补 `isinstance(value, (list, tuple))` → `list(value)`；或让 `set_scalar` 在入口统一转 np.ndarray/标量。
  - 置信度: 高（调用方与序列化路径已逐行核实）

- [AUD-8902] [Suggestion] `MaterialEditModel.signature()` 漏掉 bump_scale / displacement_scale / clearcoat_roughness / subsurface
  - 位置: src/features/composite/render3d/material_model.py:209-222
  - 证据:
    ```python
    return (
        "mat_edit_v1",
        self.base_color.tobytes(),
        float(self.metallic), float(self.roughness), float(self.specular),
        float(self.ior), float(self.normal_strength),
        self.emissive.tobytes(), float(self.emissive_intensity),
        float(self.opacity), str(self.alpha_mode), float(self.alpha_cutoff),
        float(self.transmission), float(self.clearcoat), float(self.sheen),
        ...
    ```
  - 影响: docstring 声明「缓存/比较用」；这 4 个可编辑字段变化时签名不变，未来接线缓存/重渲染判定时会漏失效（陈旧渲染）。当前无外部调用方，属潜伏缺口。
  - 建议: 补齐 4 个字段，或用 `dataclasses.fields` 驱动生成避免手工遗漏。
  - 置信度: 高（缺失确定；当前无调用方，影响为潜在）

- [AUD-8903] [Suggestion] `MaterialPreset` 只有 `to_dict` 没有 `from_dict`，材质库缺「加载」半边
  - 位置: src/features/composite/render3d/material_model.py:604-627
  - 证据:
    ```python
    @dataclass
    class MaterialPreset:
        """材质预设（master_plan §21.7 F4-8 材质库）。"""
        name: str
        category: str = "basic"
        model: MaterialEditModel = field(default_factory=MaterialEditModel)

        def to_dict(self) -> dict:
            """序列化为 dict（保存到材质库 JSON）。"""
    ```
  - 影响: 模块 docstring 承诺「MaterialLibrary：材质预设库（保存/加载常用材质配置）」，但只实现了保存；用户自定义预设写出的 JSON 无代码能读回。
  - 建议: 增加带输入校验的 `MaterialPreset.from_dict`（对齐 `texture_slot_from_dict` 的防御式风格）。
  - 置信度: 高

- [AUD-8904] [Suggestion] `material_summary` 在渲染线程安全路径上做 `os.path.isfile` 磁盘 IO
  - 位置: src/features/composite/render3d/material_model.py:514
  - 证据:
    ```python
    slot_values[label] = {
        "path": path,
        "color_space": str(getattr(slot, "color_space", default_space)),
        "missing": bool(path and not os.path.isfile(resolved_path)),
    }
    ```
  - 影响: docstring 称「render-thread-safe summary」；每材质每 slot 一次 stat 调用，网络盘/冷缓存时会阻塞，5 slot × N 材质在构建摘要时累积。
  - 建议: missing 标记异步化（UI 线程或后台任务刷新），或缓存 stat 结果按 mtime 失效。
  - 置信度: 中（阻塞量取决于挂载环境）

### 通过项
- 状态提交一致性：`decode_material_overrides`/`material_override_from_dict` 对畸形 JSON/形状未知字段均安全跳过并产出诊断码，白名单字段集与 `MATERIAL_SLOT_NAMES` 一致
- 输入验证：`texture_slot_from_dict` 对 uv_set/float/Mapping 逐项防御，color_space 白名单回退
- 非破坏编辑：`apply_override`/`apply_override_to_material_data` 均 deepcopy 后修改，不污染导入态
- HDR 正确性：base_color/emissive 全程 float32 线性，无 clamp(0,1)
- 线程安全：纯数据类无锁需求；`apply_override_to_material_data` 内局部 import 规避循环依赖
- 预设数据：`default_material_presets` 数值合理（浮点字面量规范）

---

## src/features/composite/render3d/material_state.py (行数: 156)

### 发现

- [AUD-8905] [Major] `MaterialStateKey` 未纳入 per-mesh `has_tangent` 状态，缓存命中时 `u_has_normal_map` 沿用上一 mesh 的值
  - 位置: src/features/composite/render3d/material_state.py:53-64（key 字段）、66-103（from_material）
  - 证据:
    ```python
    material_id: Optional[int] = None
    base_color_tex: Optional[Tuple[str, float]] = None
    ...
    texture_filter: str = "mipmap"
    texture_wrap: str = "repeat"
    material_mode: str = "imported"
    override_glos: Tuple = ()
    emissive_intensity: float = 1.0
    normal_format: str = "OpenGL"
    ```
  - 影响: 消费方 `scene3d_node.py:4410-4416` 在 `if not matches(...)` 块内设置 `u_has_normal_map = (not _nav_skip_maps) and bool(m.get('has_tangent')) and bool(_has_map.get("normal"))`——`has_tangent` 是 per-mesh 标志（model3d_node.py:566-574，`compute_tangents()` 失败时该 mesh 为 False）。两个 mesh 共享同一 MaterialData（同 id、同贴图 → 同 key）但 tangent 可用性不同时：后一个 mesh 跳过重绑，沿用前者的 `u_has_normal_map`。若前者=1 后者应为 0，后者的 VAO 无 `in_tangent` 属性却启用 normal map 采样 → 读取未定义顶点属性，渲染错误（花纹破碎/法线错乱）；反向则 normal map 静默失效。
  - 建议: 把 `has_tangent`（或整个 `u_has_*_map` 影响因子）加入 `MaterialStateKey`；或把 `u_has_normal_map` 的设置移出缓存守卫块。
  - 置信度: 中（机制确定；触发需同材质 mesh 的 tangent 计算部分失败，model3d_node.py:569-572 的宽泛 except 使其可能发生）

- [AUD-8906] [Suggestion] `_tex_tuple` 注释与类型注解声明 `(path, mtime)` 二元组，实际返回 `(path,)` 一元组
  - 位置: src/features/composite/render3d/material_state.py:76-85
  - 证据:
    ```python
    def _tex_tuple(descriptor) -> Optional[Tuple[str, float]]:
        """TextureDescriptor → (path, mtime) 元组（None=无）。"""
        ...
        # mtime 不在 descriptor 里（loader 解析时算）；用 path 做近似 key。
        # path 相同即视为同一贴图（mtime 变化极少，且 loader 有自己的 mtime 缓存）。
        return (path,)
    ```
  - 影响: 字段声明 `Optional[Tuple[str, float]]`（54-58 行）与实现不符，类型检查器报错/误导维护者；docstring 与实现描述不一致。
  - 建议: 改注解为 `Optional[Tuple[str]]` 并修正 docstring（或返回 `(path, 0.0)`）。
  - 置信度: 高

### 通过项
- 线程安全：单 Scene3D 节点单渲染线程使用，无跨线程共享
- 缓存语义：reset 只清 key 不清统计，hit_rate 语义正确；命中跳过的只是 bind/uniform 类无副作用状态
- PBR 标量（u_albedo 等）由调用方在缓存守护块之外每 mesh 设置（scene3d_node.py:4345-4358 已核实），未受缓存影响
- `override_glos` 排序元组化使 key 可哈希、外部纹理替换能正确失效
- `material_id=id(mat)`：帧内材质对象被 flat mesh 持有存活，同帧 id 复用风险可忽略

---

## src/features/composite/render3d/material_texture_loader.py (行数: 281)

### 发现

- [AUD-8907] [Major] `_apply_texture_wrap` 用裸 `glBindTexture` 绑定到「当前激活纹理单元」且不设置/恢复单元，多 slot 首次应用 wrap 时逐级串位覆盖 sampler 单元绑定
  - 位置: src/features/composite/render3d/material_texture_loader.py:249-260
  - 证据:
    ```python
    if glo > 0:
        from OpenGL import GL as gl
        wrap_map = {
            "repeat": gl.GL_REPEAT,
            "clamp": gl.GL_CLAMP_TO_EDGE,
            "mirror": gl.GL_MIRRORED_REPEAT,
        }
        gl.glBindTexture(gl.GL_TEXTURE_2D, glo)
        value = int(wrap_map.get(wrap, gl.GL_REPEAT))
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, value)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, value)
        return
    ```
  - 影响: 调用方 `scene3d_node.py:1065-1071` 的顺序是「先 `apply_filter_wrap`（P3-3 未命中时）→ 再 `glActiveTexture(loc)` + `glBindTexture`」。处理 slot N+1 的 wrap 时激活单元仍是 slot N 的单元（loc 3..6），`glBindTexture` 把 N+1 的纹理绑到单元 N 上，覆盖 N 刚绑好的纹理。一个带 5 张贴图的材质在首次绑定时最终状态为 unit3=normal、unit4=orm、unit5=ao、unit6=emissive（base_color 单元被覆盖）→ shader 以 normal map 当 albedo 采样，首帧渲染错误。后续帧因 P3-3 标记跳过 apply、由重绑纠正，表现为加载/换贴图/改 filter-wrap 后的一帧材质错乱闪烁。
  - 建议: 在 `_apply_texture_wrap` 内先 `gl.glGetIntegerv(gl.GL_ACTIVE_TEXTURE)` 保存，操作后恢复；或由调用方在 `glActiveTexture(loc)` 之后再应用 wrap。
  - 置信度: 高（机制与调用顺序均已逐行核实）

- [AUD-8908] [Major] `_cache` 按 `(path, mtime)` 只增不淘汰，旧 mtime 条目的 GL 纹理永不释放
  - 位置: src/features/composite/render3d/material_texture_loader.py:74-86
  - 证据:
    ```python
    cache_key = (path, mtime)
    cached = self._cache.get(cache_key)
    if cached is not None:
        ...
        return cached[0] if isinstance(cached, tuple) else cached
    self.was_last_cached = False
    tex = self._upload(ctx, path, descriptor.role)
    if tex is not None:
        self._cache[cache_key] = (tex, None)  # filter/wrap 未应用
    return tex
    ```
  - 影响: 贴图文件在磁盘上被修改（mtime 变化）后插入新 key，旧 `(path, old_mtime)` 条目仍被 `_cache` 强引用、其 GL 纹理不再被任何渲染使用却无法释放，直到节点 cleanup 的 `release_all`。热更新贴图的工作流下 GPU 内存随修改次数线性累积（明确资源泄漏风险）。
  - 建议: 插入新条目时同 path 不同 mtime 的旧条目立即 `tex.release()` 并移除（当前 render thread 单线程调用，无竞态）。
  - 置信度: 高（代码路径确定；泄漏量取决于贴图热更新频率）

- [AUD-8909] [Suggestion] `_filter_wrap_state` 未在 `__init__` 声明，靠 `getattr` 惰性创建，对象形状不完整
  - 位置: src/features/composite/render3d/material_texture_loader.py:94-96、106-109
  - 证据:
    ```python
    state = getattr(self, "_filter_wrap_state", None)
    if state is None:
        return False
    ...
    state = getattr(self, "_filter_wrap_state", None)
    if state is None:
        self._filter_wrap_state = {}
        state = self._filter_wrap_state
    ```
  - 影响: 实例属性集合依赖调用历史，`release_all` 还需 `hasattr` 防御（126-127 行）；调试/序列化/类型检查均受影响。
  - 建议: `__init__` 中显式 `self._filter_wrap_state: dict = {}`，两处 getattr 简化。
  - 置信度: 高

- [AUD-8910] [Suggestion] 多处 `except Exception: pass/return False` 宽泛吞错且无日志
  - 位置: src/features/composite/render3d/material_texture_loader.py:97-100、110-114、188-192、228-231、261-262、264-269
  - 证据:
    ```python
    try:
        glo = int(tex.glo)
    except Exception:
        return False
    ...
    try:
        tex.filter = (moderngl_filter_nearest(), moderngl_filter_nearest())
    except Exception:
        pass
    ```
  - 影响: 违反「不得宽泛 except 掩盖领域错误」；wrap 设置静默失败时用户看到的采样模式与参数面板不符且无诊断线索；mipmap 构建失败同样无声。
  - 建议: 收窄为 `(TypeError, ValueError, moderngl.Error)` 等具体异常，失败处 `logger.warning` 一次。
  - 置信度: 高（模式存在确定；多为 GL 容错场景，故仅 Suggestion）

### 通过项
- 线程安全：文档明确 render-thread-only，与 Scene3D 同线程，无锁设计成立
- GPU 资源生命周期：随节点 cleanup 的 `release_all` 统一释放；`was_last_cached` 读取时序（P0-4 修复）在调用方正确
- HDR 正确性：贴图按源数值 float32（`dtype='f4'`）上传，无 CPU 侧 clamp/伽马转换，sRGB→linear 交由 shader（与模块设计一致）
- 输入验证：空路径/文件不存在/mtime 不可得均返回 None 安全降级为标量材质
- 通道扩展（1/2/3→RGBA）逻辑正确，数组连续性保证

---

## src/features/composite/render3d/math_utils.py (行数: 439)

### 发现

- [AUD-8911] [Suggestion] `spot_intensity` docstring 声称 smoothstep 且「与 shader spot_cone_intensity 一致」，实现是线性斜坡
  - 位置: src/features/composite/render3d/math_utils.py:361-370
  - 证据:
    ```python
    """聚光灯锥角强度（与 shader spot_cone_intensity 一致）。
    ...
    返回 0..1：锥内全亮，外锥外全黑，内外间 smoothstep。
    """
    denom = inner_cos - outer_cos
    if abs(denom) < 1e-4:
        return 1.0 if cos_theta >= inner_cos else 0.0
    return max(0.0, min(1.0, (cos_theta - outer_cos) / denom))
    ```
  - 影响: 若 shader 侧确用 smoothstep，CPU 预览/图标与渲染的软边过渡不一致；至少文档与实现矛盾误导维护者。当前无外部调用方，属潜伏不一致。
  - 建议: 实现改 smoothstep 或修正 docstring，二选一并与 shader 侧对齐。
  - 置信度: 中（shader 侧未在本批次核实；文档-实现矛盾确定）

- [AUD-8912] [Suggestion] `IDENTITY4` 是可变共享模块级常量
  - 位置: src/features/composite/render3d/math_utils.py:17-18
  - 证据:
    ```python
    # 4x4 单位矩阵
    IDENTITY4 = np.eye(4, dtype=np.float32)
    ```
  - 影响: 任何调用方原地修改（`IDENTITY4[0,3] = x`）会污染全部使用处，且极难排查。
  - 建议: 提供 `identity4()` 工厂函数，或文档标注只读并 `.setflags(write=False)`。
  - 置信度: 高（风险潜在，当前无修改发生）

- [AUD-8913] [Suggestion] `focal_from_fov` 对 fov→180° 无防护，除零产生 inf
  - 位置: src/features/composite/render3d/math_utils.py:375-382
  - 证据:
    ```python
    fov_rad = np.radians(fov_deg)
    return float(sensor_height / (2.0 * np.tan(fov_rad * 0.5)))
    ```
  - 影响: fov_deg 接近/等于 180 时 `tan` 发散 → focal→0/inf，下游无 NaN/inf 检查会扩散。当前无外部调用方（camera3d 用 camera_model 的独立实现），潜伏。
  - 建议: 对 `fov_deg` 钳制到 (0, 179) 并在越界时返回安全值。
  - 置信度: 高

### 通过项
- 矩阵约定正确：行主序 + `matrix_to_tuple` 转置展平为列主序，perspective/ortho/look_at 与 gluLookAt/gluPerspective 语义一致（含平行 up 的备选轴退化处理）
- `perspective_with_shift` 修改 [0][2]/[1][2] 的 lens shift 语义正确
- `pick`/相机辅助（orbit_to_eye、pitch 钳制、Rodrigques 旋转）数学正确
- 输入防御：near/far/aspect/scale 的除零钳制完备
- 类型注解完整（除 `orbit_to_eye` 的 `target: np.ndarray = None` 隐式 Optional，轻微）

---

## src/features/composite/render3d/mesh_diagnostics.py (行数: 247)

（注：文件首行带 `DORMANT(2026-08-19 审计)` 标记，已实现有测试但暂无生产入口。）

### 发现

- [AUD-8914] [Suggestion] `uv_stretch_to_color` 用本批最大偏差归一化，均匀拉伸的整体被显示为「无拉伸」
  - 位置: src/features/composite/render3d/mesh_diagnostics.py:217-219
  - 证据:
    ```python
    deviation = np.abs(np.log1p(np.abs(s - 1.0)))  # log 缩放使分布更均匀
    t = np.clip(deviation / (deviation.max() + 1e-9), 0.0, 1.0)
    # 绿(0,无拉伸)→黄(0.5)→红(1,最大拉伸)
    ```
  - 影响: 所有三角形拉伸量相同且远离 1 时（如整体均匀 4× 拉伸），`deviation.max()` 即每项值 → t=0 → 全绿显示「无拉伸」，绝对拉伸信息丢失；仅相对热力图语义。
  - 建议: 用固定参考尺度（如 log1p 阈值常数）而非批内 max，或同时输出绝对偏差供 UI 显示数值。
  - 置信度: 中

### 通过项
- 线程安全 / GPU 资源：纯 numpy 数据生成，无 GL/Qt 依赖（与模块设计一致）
- `build_unique_edges`：min/max 规范化 + `np.unique(axis=0)` 正确且无 int64 编码溢出风险
- `build_normal_lines`/`build_tangent_lines`：长度不匹配/畸形 shape 安全返回空数组，法线防御性归一化
- `compute_uv_stretch`：UV 零面积退化三角形返回 0 且不产生除零
- DORMANT 标记清晰（接线前不进生产依赖）

---

## src/features/composite/render3d/mesh_picking.py (行数: 650)

### 发现

- [AUD-8915] [Suggestion] `pick_face` 兼容路径为 O(三角形数) 纯 Python 循环，大模型点击会阻塞调用线程
  - 位置: src/features/composite/render3d/mesh_picking.py:470-484
  - 证据:
    ```python
    # 遍历所有三角形
    n_tris = len(indices) // 3
    for i in range(n_tris):
        i0 = int(indices[i * 3])
        i1 = int(indices[i * 3 + 1])
        i2 = int(indices[i * 3 + 2])
        v0 = world_verts[i0]
        v1 = world_verts[i1]
        v2 = world_verts[i2]
        t, u, v = ray_triangle_intersect(ray_origin, ray_dir, v0, v1, v2)
    ```
  - 影响: 200 万面模型单次点击数百万次 Python 级 Möller–Trumbore（每次含 3 次 int()、3 次索引、numpy 标量运算），秒级阻塞；若在 UI 线程调用则界面冻结。BVH 路径已存在但该公开 API 仍是最慢实现。
  - 建议: `pick_face` 内部转调 `pick_face_bvh`（无 bvh 参数时临时构建），或文档标注 deprecated 并在入口打印一次性警告。
  - 置信度: 高（性能特征确定；实际影响取决于调用方选择哪条路径）

- [AUD-8916] [Suggestion] `MeshPickCache._signature` 依赖 `id(vertices)/id(indices)`，数组 GC 后地址复用可致误命中旧 BVH
  - 位置: src/features/composite/render3d/mesh_picking.py:278-288
  - 证据:
    ```python
    return (
        revision,
        id(vertices), id(indices),
        tuple(verts.shape), str(verts.dtype),
        tuple(idx.shape), str(idx.dtype),
    )
    ```
  - 影响: 同 mesh_id 的旧数组被回收、新数组恰好分配到同一地址且 shape/dtype 相同、revision 未显式递增时，签名相等 → 返回内容已变的旧 BVH → 拾取命中错误三角形/组件。长会话反复重载模型时可触发。
  - 建议: 生产方对可变几何始终传 revision（docstring 已建议）；或在签名中加入内容哈希兜底。
  - 置信度: 低（需地址复用巧合；机制成立）

- [AUD-8917] [Suggestion] `screen_to_world_ray` 只对 near 点做 w 除零检查，far 点除以≈0 会产生 NaN 方向且未被 norm 检查拦截
  - 位置: src/features/composite/render3d/mesh_picking.py:642-649
  - 证据:
    ```python
    if abs(near_world[3]) < _EPSILON:
        return (np.zeros(3), np.array([0.0, 0.0, -1.0]))
    near_world = near_world[:3] / near_world[3]
    far_world = far_world[:3] / far_world[3]
    direction = far_world - near_world
    norm = float(np.linalg.norm(direction))
    if norm < _EPSILON:
        return (near_world, np.array([0.0, 0.0, -1.0]))
    ```
  - 影响: far 点 w≈0（极端投影参数/视点位于远平面）时 `far_world` 变为巨大/NaN 向量；`norm` 为 NaN 时 `NaN < _EPSILON` 为 False，NaN 方向被归一化后返回，下游所有相交判定失效且无报错。
  - 建议: 对 far_world[3] 同样检查；norm 用 `not (norm >= _EPSILON)` 形式过滤 NaN。
  - 置信度: 中（触发条件罕见，机制确定）

- [AUD-8918] [Suggestion] `TriangleBVH.__init__` 物化 (T,3,3) float64 临时数组，超大模型构建时内存尖峰
  - 位置: src/features/composite/render3d/mesh_picking.py:124-128
  - 证据:
    ```python
    tri_vertices = verts[valid_triangles]
    self._triangle_mins = tri_vertices.min(axis=1)
    self._triangle_maxs = tri_vertices.max(axis=1)
    self._triangle_centroids = (
        self._triangle_mins + self._triangle_maxs) * 0.5
    ```
  - 影响: 200 万面模型该临时数组约 144MB（2M×9×8B），叠加 `_triangle_mins/maxs/centroids` 常驻约 144MB；构建期间峰值再翻倍，可能触发系统内存压力（master plan 目标场景为 50 万~200 万面）。
  - 建议: 分块（如每 100K 面一批）归约 min/max，避免整表物化；或 verts 保留 float32 构建。
  - 置信度: 高（算术确定；严重度取决于模型规模）

### 通过项
- 线程安全：纯 CPU/numpy，无 GL/Qt，文档明确可任意线程构建查询
- BVH 正确性：中位数分裂 + 近子节点先出栈的最近命中搜索正确；`_source_triangle_ids` 保原始面编号与 GPU ID pass 一致；越界三角形剔除不改变合法面编号
- `pick_face_bvh` 的 world↔local 变换正确（射线入 local、命中点回 world、世界距离重算、非均匀 scale 处理）
- `pick_component` 的 vertex/edge 由同一面命中推导（argmax/argmin 重心坐标），模式校验抛 ValueError（外部输入验证）
- `MeshPickCache` LRU + revision 失效机制设计清晰，统计接口完备
- `ray_triangle_intersect`（Möller–Trumbore）边界条件（平行、u/v 越界、t≤ε）处理正确

---

## src/features/composite/render3d/mesh_vbo_pool.py (行数: 141)

### 发现

- [AUD-8919] [Major] `acquire` 中 VBO 创建成功后 IBO 创建抛异常时无回滚，GL buffer 泄漏
  - 位置: src/features/composite/render3d/mesh_vbo_pool.py:101-105
  - 证据:
    ```python
    vbo = ctx.buffer(data)
    ibo = ctx.buffer(mesh.indices.tobytes())
    entry = _VBOEntry(vbo, ibo, int(mesh.indices.shape[0]),
                      is_skinned, bool(has_tangent))
    self._entries[key] = entry
    ```
  - 影响: `ctx.buffer(ibo)` 失败（如显存不足、上下文丢失）时异常向上抛，已创建的 `vbo` 既未进 `_entries` 也未 release → 永久泄漏；调用方（model3d_node.py:607-610）的 except 只跳过该 mesh，不清理半成品。反复触发（大模型逐 mesh 上传失败）会累积泄漏 GL buffer 直至 ctx 销毁。
  - 建议: 用 try/except 包住第二个 buffer 创建，失败时 `vbo.release()` 后再 raise。
  - 置信度: 高（代码路径确定；触发依赖异常场景）

- [AUD-8920] [Suggestion] `release`/`release_all` 用 `except Exception: pass` 吞掉 GL release 错误且无日志
  - 位置: src/features/composite/render3d/mesh_vbo_pool.py:122-127、131-135
  - 证据:
    ```python
    if entry.refcount == 0:
        try: entry.vbo.release()
        except Exception: pass
        try: entry.ibo.release()
        except Exception: pass
        del self._entries[key]
    ```
  - 影响: deferred-release 场景下宽泛捕获常见，但完全无日志时 ctx 已销毁等真实问题不可诊断；条目仍被 `del`，掩盖了「资源未真正释放」的事实。
  - 建议: 捕获后 `logger.debug("VBO release failed", exc_info=True)`（模块已有 logger）。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：per-ctx 池隔离正确；`release_context` 在 composite_renderer.py:2059 的 ctx 销毁路径有真实调用（已核实），dangling GL 对象风险受控
- 线程安全：`for_context` 的类级 `_lock` 保护 `_pools` 字典；`acquire/release` 按 docstring 约定仅 ctx 线程调用（三个调用节点均在渲染线程，已核实）
- 引用计数：重复 release 钳制不为负；`has_tangent` 参与缓存 key，model3d/deformer3d 的 release 均传第三参数（scene3d 的 acquire/release 一致地不带，已核实无失配泄漏）
- 缓存 key 设计：`(content_hash, is_skinned, has_tangent)` 正确区分三种 interleaved layout；skinned/tangent 布局互不误用
- 类型注解完整

---

## 批次总结

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-8901 |
| Major | 4 | AUD-8905, AUD-8907, AUD-8908, AUD-8919 |
| Suggestion | 16 | AUD-8900, AUD-8902-8904, AUD-8906, AUD-8909-8910, AUD-8911-8913, AUD-8914, AUD-8915-8918, AUD-8920 |

核心风险链：材质编辑持久化丢数据（AUD-8901，UI 写 list、序列化只认 ndarray/标量）；材质状态缓存缺 per-mesh 维度（AUD-8905）与裸 glBindTexture 单元串位（AUD-8907）两类渲染错误风险；贴图 mtime 缓存与 VBO 半成品两类 GL 资源泄漏（AUD-8908/8919）。
