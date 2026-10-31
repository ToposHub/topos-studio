# batch-092 [P1] — src/features/composite/render3d/

编号区间：AUD-9200–AUD-9299（实际使用 AUD-9200–AUD-9214）。
审查方式：7 个文件全文通读；关键疑点已跨文件核实
（`nodes/model3d_node.py`、`nodes/scene3d_node.py`、`render3d/model_loader.py`、`render3d/aov.py`、`render3d/scene_graph.py` 均为只读引用）。

---

## src/features/composite/render3d/scene_payload.py (行数: 421)

### 发现

- [AUD-9200] [Critical] ModelPayload.signature 遗漏 material_params，材质控制参数变化不触发重渲染（陈旧输出）
  - 位置: src/features/composite/render3d/scene_payload.py:366
  - 证据:
    ```python
    return (
        "model",
        bool(self.has_model), self.model_path, float(self.model_mtime),
        float(self.anim_time), int(len(self.flat_meshes)),
        tuple(sorted(str(value) for value in self.hidden_mesh_ids)),
        ...
        self.transform_sig,
        anim_sig,
        tuple(sorted((str(key), float(value))
                     for key, value in (self.morph_weights or {}).items())),
    )
    ```
    签名包含 transform_sig / material_overrides / morph_weights，但不包含本文件 317 行定义的字段：
    `material_params: Optional[Dict[str, Any]] = None  # material_mode/normal_format/orm_layout/...`
  - 影响: 已跨文件核实完整链路：`nodes/model3d_node.py:857-864` 用节点参数填充 material_params（material_mode / normal_format / orm_layout / texture_filter / texture_wrap / emissive_intensity），且这些值不在 transform_sig（model3d_node.py:1051-1056 仅含变换/单位/UV/纹理端口/morph）；`nodes/scene3d_node.py:3641-3675` 用 `_model_sig` 组装 content_sig，注释 3684 明确 cache-hit 帧"只是回传缓存的纹理"。因此用户修改 Model3D 的材质模式/法线格式/ORM 布局/纹理过滤/包裹模式/自发光强度后，签名不变 → Scene3D 直接回传陈旧 `_gpu_texture`，视口渲染结果错误（与 transform_sig 修复前的"P0 缺口"同类缺陷，见本文件 311 行注释）。
  - 建议: 在 ModelPayload.signature() 中加入 `tuple(sorted((str(k), str(v)) for k, v in (self.material_params or {}).items()))`（或将 material_params 并入 transform_sig），与 material_overrides/morph_weights 的处理保持一致。
  - 置信度: 高

- [AUD-9201] [Suggestion] CameraPayload.signature 把纯 UI 开关 show_safe_areas 纳入签名，切换安全框触发无谓重渲染
  - 位置: src/features/composite/render3d/scene_payload.py:110
  - 证据:
    ```python
    float(self.lens_shift_x), float(self.lens_shift_y),
    bool(self.show_safe_areas),
    )
    ```
    而字段自身注释（82-83 行）写明："安全框开关（视口 UI overlay 用，不进渲染）"。
  - 影响: 已核实 `nodes/scene3d_node.py:3667-3672` 将 `_cam_sig` 放入 view_sig，view_sig 变化即 content_changed=True。安全框由 `viewport_overlays.draw_safe_areas`（QPainter 屏幕空间）绘制，切换它本不需 GPU 重渲染，却会让 Scene3D 重发全部 draw call（4K 下为可感知开销）。
  - 建议: 从 signature() 移除 show_safe_areas（与 node_id 同理：纯 UI 状态由 overlay 侧自行监听刷新）。
  - 置信度: 高

- [AUD-9202] [Suggestion] LightPayload.signature 用 id() 标识 IBL GL 对象，存在地址复用漏检变化的理论风险
  - 位置: src/features/composite/render3d/scene_payload.py:213
  - 证据:
    ```python
    id(self.irradiance_cube), id(self.prefilter_cube), id(self.brdf_lut),
    float(self.env_intensity), bool(self.use_ibl),
    ```
  - 影响: Python 不保证生命周期不重叠对象的 id 唯一。Scene3D 保存的 `_last_scene_content_sig` 只持有 id 整数（不持对象引用），若旧 cube map 先被释放、新 cube map 恰好分配在同一地址，切换 HDRI 后签名不变 → 使用陈旧 IBL 照明。发生窗口取决于 light3d_node/hdri_baker 的释放顺序，概率低但后果为渲染错误。
  - 建议: 让烘焙侧维护单调递增的 generation/revision 计数并进 payload（替代裸 id()），或保证"先建新、后释放旧"的顺序并加注释固化。
  - 置信度: 中

- [AUD-9203] [Suggestion] GeometryPayload.model_matrix 类型注解 np.ndarray 但默认值为 None
  - 位置: src/features/composite/render3d/scene_payload.py:242
  - 证据:
    ```python
    model_matrix: np.ndarray = None    # 4x4（object 变换 + mesh_scale）
    ```
  - 影响: 注解与实际取值矛盾；类型检查器（mypy/pyright strict）会报错，调用方也无法据注解判断需要 None 判断（signature() 271-272 行确实做了 None 判断）。
  - 建议: 改为 `Optional[np.ndarray] = None`（同文件 instance_offsets 的写法）。
  - 置信度: 高

- [AUD-9204] [Suggestion] GeometryPayload.signature 每次求值对 instance_offsets 全量 astype+tobytes，大网格下为每帧 MB 级拷贝
  - 位置: src/features/composite/render3d/scene_payload.py:277
  - 证据:
    ```python
    (self.instance_offsets.astype(np.float32).tobytes()
     if self.instance_offsets is not None else b""),
    model_sig, mat_sig, int(self.content_revision),
    ```
  - 影响: 注释说明该数组是 grid_x×grid_z 实例偏移的 CPU 镜像；Scene3D 每次求值（含纯相机交互帧）都重算 `_geo_sig`，大网格（如 500×500 = 75 万 float）时 astype 拷贝 + tobytes 拷贝每帧可达数 MB memcpy，违背 4K 实时性能基准的"避免不必要计算"原则。
  - 建议: 已有 content_revision 递增机制（250-252 行），可用 `(content_revision, instance_count)` 替代全量 tobytes；或缓存 tobytes 结果（数组不可变时）。
  - 置信度: 中

### 通过项
- 线程安全：纯 dataclass 数据层，GL 对象引用按文档化的旁路设计跨线程传递，无共享可变状态竞争
- GPU 资源生命周期：payload 只携带引用不拥有资源（注释明确 GL 资源归源节点所有），signature 不缓存 GL 句柄本体
- HDR 正确性：payload 不做 clamp，材质/灯光颜色以线性 float 传递
- 状态提交一致性：四个 payload 的 signature 覆盖 transform/material_overrides/morph/render_flags/hidden/locked/solo 等持久化语义（除 AUD-9200 指出的缺口）
- 异常与取消路径：signature 对 None/缺省字段均有守卫（eye/target None 判断、material or {}）
- 输入验证：empty_light_payload 工厂保证降级路径 shader 数组合法

---

## src/features/composite/render3d/selection.py (行数: 335)

### 发现

- [AUD-9205] [Suggestion] select_objects_box 未实现 SELECT_TOGGLE 分支，框选切换模式静默 no-op
  - 位置: src/features/composite/render3d/selection.py:85
  - 证据:
    ```python
    if mode == SELECT_SET:
        self.clear()
        self.object_ids.update(target)
        ...
    elif mode == SELECT_SUBTRACT:
        self.object_ids -= target
        ...
    ```
    仅 SET/ADD/SUBTRACT 三个分支；常量 SELECT_TOGGLE = "toggle"（34 行）在框选路径无对应处理，单选路径 select_object 则四模式齐全（63-83 行）。
  - 影响: 若后续框选交互传入 toggle（如 Shift 框选切换语义），函数静默不做任何事，产生"框选无效"的功能缺陷且无报错。
  - 建议: 补充 toggle 分支（对称差集 `self.object_ids ^= target`），或对未知 mode 抛 ValueError 暴露契约。
  - 置信度: 高

- [AUD-9206] [Suggestion] 空选择状态下 SUBTRACT 组件选择会留下空 set 条目，is_empty()/signature 误报"有选择"
  - 位置: src/features/composite/render3d/selection.py:112
  - 证据:
    ```python
    vs = self.vertex_ids.setdefault(mesh_id, set())
    if mode == SELECT_SUBTRACT:
        vs.discard(vertex_idx)
    ```
    而 is_empty()（165-167 行）判定 `not self.vertex_ids`：
    ```python
    return (not self.object_ids and not self.vertex_ids
            and not self.edge_ids and not self.face_ids)
    ```
  - 影响: Alt/Option 点击未选中顶点（空选集减选）后 `vertex_ids = {mesh_id: set()}` → is_empty() 返回 False、signature() 出现非空项；依赖 is_empty 的调用方（如清除高亮、Deformer 空选判断）行为异常，且空条目随 mesh_id 累积。
  - 建议: discard 后若 `not vs` 则 `self.vertex_ids.pop(mesh_id, None)`（select_face/select_edge 同理），或在 is_empty 中按值判空。
  - 置信度: 高

### 通过项
- 线程安全：纯 Python 数据层（无 GL/Qt 依赖，模块 docstring 明确可独立单测），单一属主线程访问模型
- 信号防递归：无 Qt 信号，不适用
- 输入验证：get_vertex_mask/get_face_mask 对越界索引有 `0 <= vi < count` 守卫；apply_component_hit 对缺 vertex_index/edge 长度≠2/负 triangle_index 均拒绝且 duck-typed 无 GL 导入
- 状态一致性：signature() 各集合排序，稳定可比；edge 规范化为 (min,max)
- 性能：掩码生成 O(选中数)，合理

---

## src/features/composite/render3d/skeleton.py (行数: 413)

### 发现

- [AUD-9207] [Suggestion] build_skeleton_from_joints 不校验 parent_joint_index 合法性，违规输入直接 IndexError
  - 位置: src/features/composite/render3d/skeleton.py:161
  - 证据:
    ```python
    else:
        parent = bones[jn.parent_joint_index]
        parent.children_indices.append(i)
        rest_world = (parent.rest_world @ rest_local).astype(np.float32)
    ```
    docstring 仅以文字约定"parent_joint_index 必须指向更早出现的 joint"，代码无 `0 <= idx < i` 检查。
  - 影响: glTF/FBX 为外部数据（.clinerules：外部数据须验证）。当前两个调用方（model_loader.py:841-876 与 1359-1381）都先做拓扑排序并各自用 `except Exception` 兜底，故现网不崩；但本函数作为公共构建入口一旦被新代码直接调用，前向引用/越界即未捕获 IndexError。
  - 建议: 循环内加 `if not (0 <= jn.parent_joint_index < i): raise ValueError(...)`，把契约从注释升级为代码。
  - 置信度: 高（代码事实；现网风险已由调用方缓解，故降为 Suggestion）

- [AUD-9208] [Suggestion] compute_joint_matrices 与 skinning.compute_skin_joint_matrices 重复实现且缺失回退语义不一致，且前者当前无调用方
  - 位置: src/features/composite/render3d/skeleton.py:180
  - 证据:
    ```python
    cur_world = current_worlds.get(bone.name)
    if cur_world is None:
        logger.warning(
            "compute_joint_matrices: bone '%s' (idx=%d) not in current_worlds, "
            "using rest_world (bind pose)", bone.name, i)
        cur_world = bone.rest_world
    ```
    而 src/features/composite/render3d/skinning.py:108-112 的同用途实现回退为 `np.linalg.inv(inverse)`。全库 grep 确认 compute_joint_matrices 无调用点（实际调用的是 skinning 版本，见 nodes/scene3d_node.py:1306-1310）；weight_to_color/build_weight_colors/build_all_bone_weights_summary 亦无调用方。
  - 影响: skinning.py 模块 docstring 明言"Keep the matrix construction ... here so the two paths cannot silently drift apart"，但本模块保留并行版本且回退语义不同（rest_world vs inv(IBM)），一旦被误用即产生两套骨骼数学；未命中骨骼时逐骨逐帧 logger.warning 还有日志洪水隐患。
  - 建议: 删除或显式标记 compute_joint_matrices 为废弃并委托 skinning.compute_skin_joint_matrices；未接线的 weight display 函数一并注明"预留"。
  - 置信度: 高

- [AUD-9209] [Suggestion] build_all_bone_weights_summary 未钳制骨骼索引上限，损坏数据可致 O(B×N×4) 近乎挂起
  - 位置: src/features/composite/render3d/skeleton.py:410
  - 证据:
    ```python
    bi = skin.bone_indices.flatten()  # (N*4,)
    bw = skin.bone_weights.flatten()  # (N*4,)
    summary: Dict[int, float] = {}
    for bone_idx in range(int(bi.max()) + 1 if bi.size > 0 else 0):
        mask = bi == bone_idx
        summary[bone_idx] = float(bw[mask].sum())
    ```
  - 影响: bone_indices 是外部文件来的 uint32；若含越界大索引（skinning.validate_skinning_contract 会标记 `influence_index_ignored` 但不清洗），`bi.max()` 可达 4×10⁹ → 循环次数天文数字，UI/渲染线程挂起。当前无调用方（weight display 未接线），属防御性缺口。
  - 建议: `for bone_idx in np.unique(bi)` 或钳制到 `min(bi.max()+1, MAX_BONES)`，并跳过越界索引。
  - 置信度: 中

### 通过项
- 线程安全：纯 numpy 计算，无共享可变状态，可在渲染线程安全调用
- GPU 资源生命周期：模块无 GL 对象（docstring 明确），只产出 CPU buffer
- 数学正确性：行主序 M @ v 约定与 scene_graph 一致；rest_world 累乘、MAX_BONES 截断 + identity 填充正确（compute_joint_matrices 210-212 行）；unique_bone_names 的生成名冲突处理（含 "a_2" 先于 "a" 出现的对抗样例）正确
- HDR 正确性：权重热力色 clip(0,1) 属 UI 显示语义（颜色表定义域），不违反 HDR 不许无理由 clamp 的铁律
- 输入验证：BoneData.__post_init__ reshape(4,4) 对畸形矩阵即抛 ValueError；空骨架/空 skin 均有早退分支

---

## src/features/composite/render3d/skeleton_renderer.py (行数: 239)

### 发现

- [AUD-9210] [Suggestion] 渲染热路径使用 __import__('moderngl') 内联导入而非模块顶部导入
  - 位置: src/features/composite/render3d/skeleton_renderer.py:185
  - 证据:
    ```python
    self._line_vao.render(__import__('moderngl').LINES)
    ...
    self._point_vao.render(__import__('moderngl').POINTS)
    ```
    （207 行同样写法；模块顶部未 import moderngl）
  - 影响: 每帧两次动态导入查找 + 可读性差；且掩盖了模块对 moderngl 的真实依赖，静态分析/打包扫描（如生成依赖图）会漏检。
  - 建议: 顶部 `import moderngl`，render 内直接用 `moderngl.LINES` / `moderngl.POINTS`。
  - 置信度: 高

- [AUD-9211] [Suggestion] _release_vbos/release 使用裸 except Exception: pass 吞掉 GL 释放异常
  - 位置: src/features/composite/render3d/skeleton_renderer.py:213
  - 证据:
    ```python
    for attr in ('_line_vao', '_point_vao'):
        obj = getattr(self, attr, None)
        if obj is not None:
            try:
                obj.release()
            except Exception:
                pass
            setattr(self, attr, None)
    ```
    （219-226、231-238 行同模式）
  - 影响: 与 .clinerules"不得宽泛 except 掩盖领域错误"冲突。释放路径 best-effort 可接受，但完全静默会使 context 已销毁/双重释放等问题无法诊断（该 renderer 由 scene3d_node 持有，context 生命周期错位时无从发现）。
  - 建议: 至少捕获后 `logger.debug("release %s failed", attr, exc_info=True)`，或窄化到 moderngl.Error。
  - 置信度: 高（代码事实；影响低故为 Suggestion）

### 通过项
- 线程安全：仅在渲染线程被 scene3d_node 调用（scene3d_node.py:4586-4604 已核实，含 try/except 降级），无 UI 线程 GL 操作
- GPU 资源生命周期：program 一次编译缓存（_built 标志）、VBO/VAO 每帧先 _release_vbos 再重建、release() 全量释放且重置 _built；消费方在 cleanup 调用 release（scene3d_node.py:5184-5186 已核实）
- GLSL 正确性：四个 shader 均为 330 core 严格类型，所有浮点字面量带 .0（1.0/0.5/0.25），无循环，无隐式转换；点精灵圆形裁剪 dot(coord,coord)>0.25 数学正确
- HDR 正确性：overlay 颜色为线性 RGB 常量经 u_alpha 混合，无 clamp
- 性能：每帧 VBO 重建为文档化的第一版取舍（"静态时可缓存"），骨骼数 ≤ MAX_BONES=64 时 Python 循环开销可忽略

---

## src/features/composite/render3d/skinning.py (行数: 211)

### 发现

- [AUD-9212] [Suggestion] skin_mesh_cpu 对 vertices/normals/uvs 的访问与形状校验顺序不一致，畸形 mesh 抛裸异常而非 SkinningError
  - 位置: src/features/composite/render3d/skinning.py:141
  - 证据:
    ```python
    vertices = np.asarray(mesh.vertices, dtype=np.float32)
    normals_input = np.asarray(mesh.normals, dtype=np.float32)
    vertex_count = int(vertices.shape[0])
    if vertices.ndim != 2 or vertices.shape[1] != 3:
        raise SkinningError("mesh vertices must have shape (N, 3)")
    ```
    1) `mesh.vertices`/`mesh.normals` 属性访问未包裹 try（bone_indices/inverse_bind 均有 try→SkinningError 包装，149-153、158-162 行）；2) `vertices.shape[0]` 在 ndim 检查之前执行，0-d 输入抛 IndexError 而非 SkinningError；3) 201 行 `np.asarray(mesh.uvs, ...)` 同样未包裹。
  - 影响: Deformer3D（deformer3d_node.py:256-258 唯一调用方）期望本模块以 SkinningError 表达领域错误；缺 normals/uvs 属性或标量 vertices 的畸形 mesh 会以 AttributeError/IndexError 逃逸，破坏统一的异常契约。
  - 建议: 将 vertices/normals/uvs 的访问与转换并入 try→SkinningError 块，并把 ndim/shape 检查移到 `shape[0]` 取值之前。
  - 置信度: 高

### 通过项
- 线程安全：模块无 ModernGL 对象（docstring 明确），可在渲染线程/单测安全使用
- GPU 资源生命周期：不适用（纯 CPU 数学）
- CPU/GPU 契约一致性：joint_matrices = current_world @ inverse_bind 与 shader 约定一致；skin_mesh_cpu 齐次位置变换 + 3x3 法线变换 + 零权重顶点保持原样，并刻意移除 bone 属性防止二次蒙皮（134-135 行注释）
- 输入验证：validate_skinning_contract 对外部 skin 数据的维度/有限性/归一化/索引范围给出稳定诊断码；compute_skin_joint_matrices 对逆矩阵奇异（LinAlgError）与非法世界矩阵均有回退或类型化异常
- 性能：einsum 向量化蒙皮，无 Python 逐顶点循环；法线归一化有 1e-8 守卫避免除零
- 类型注解：完整（含 Mapping | None 现代写法）

---

## src/features/composite/render3d/viewport_overlays.py (行数: 267)

### 发现

（无）

### 通过项
- 线程安全：纯 QPainter 屏幕空间绘制，由 paintEvent（UI 线程）调用，不触碰 OpenGL，符合"UI 线程不得操作 GL"铁律；painter.save/restore 配对
- 信号防递归：无 Qt 信号，不适用
- GLSL·HDR：不适用（无 shader）；模块 docstring 明确 overlay 不进合成输出、不写深度
- 输入验证：render_region 的 opacity/rgb/width 全部 clamp 到有效域；render_region_rect 对非正宽高/非正 aspect 有早退
- 性能：每 paint 常数次数 draw 调用，无分配热点
- 浮点字面量：全部显式 .0
- 类型注解：完整

---

## src/features/composite/render3d/volume_grid.py (行数: 100)

### 发现

- [AUD-9213] [Suggestion] VolumeGrid 构造器不校验 spacing > 0，负 spacing 使透射率物理错误
  - 位置: src/features/composite/render3d/volume_grid.py:42
  - 证据:
    ```python
    density: "np.ndarray"
    spacing: float = 1.0

    def __post_init__(self) -> None:
        self.density = np.ascontiguousarray(
            self.density, dtype=np.float32)
        if self.density.ndim != 3:
            raise ValueError("volume density must be 3D (D, H, W)")
    ```
    density 有完整验证（NaN/inf 清洗 + clip≥0），spacing 无任何检查；仅 load_volume（77-79 行）在加载路径兜底 `spacing if spacing > 0.0 else 1.0`。
  - 影响: 直接构造 `VolumeGrid(d, spacing=-1.0)`（绕过 load_volume 的路径，如测试/未来节点内联构造）会使 `integrate_transmittance` 的 `exp(-total * step)` 变为 exp(正数) → 透射率 > 1，体积渲染能量凭空增加，且无任何报错。
  - 建议: `__post_init__` 中 `if not (self.spacing > 0.0): raise ValueError(...)`（NaN 也一并拒绝），与 density 的防御级别对齐。
  - 置信度: 高

- [AUD-9214] [Suggestion] save_volume 未强制 .npz 后缀，numpy 会静默追加后缀导致写出文件与请求路径不一致
  - 位置: src/features/composite/render3d/volume_grid.py:82
  - 证据:
    ```python
    def save_volume(path: str, density: "np.ndarray",
                    spacing: float = 1.0) -> None:
        """Write the dense volume format (fixture/converter helper)."""
        np.savez_compressed(path,
                            density=np.ascontiguousarray(
                                density, dtype=np.float32),
                            spacing=np.float32(spacing))
    ```
  - 影响: numpy 的 savez 系列在路径无 .npz 后缀时自动追加后缀：`save_volume("/tmp/foo")` 实际写出 `/tmp/foo.npz`，调用方随后 `load_volume("/tmp/foo")` 会因 suffix 非 .npz 抛 "unsupported volume format"，往返不对称且难排查（converter/fixture 场景首当其冲）。
  - 建议: 入口处 `path = str(Path(path).with_suffix(".npz"))` 或断言后缀，保证写读路径一致。
  - 置信度: 中（numpy 追加后缀行为确定；是否触发取决于调用方传参习惯）

### 通过项
- 输入验证（density）：3D 形状检查、NaN/inf→0 清洗、clip≥0、np.load allow_pickle=False 防反序列化攻击、.vdb 给出可操作的 VolumeDependencyError 而非静默跳过
- 线程安全 / GPU 生命周期 / 信号防递归：纯 numpy 数据层，不适用
- HDR 正确性：density 不做上界 clamp（仅下界 0，物理约束非显示 clamp）
- 数学正确性：Beer–Lambert 解析式 T=exp(-Σσ·Δ) 与 docstring/节点约定一致
- 异常路径：全部抛类型化领域异常（VolumeDependencyError/ValueError），无宽泛 except

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-9200 |
| Major | 0 | — |
| Suggestion | 14 | AUD-9201–AUD-9214 |

viewport_overlays.py 全维度通过，无发现。
