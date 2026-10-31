# batch-091 审查报告 — src/features/composite/render3d/（8 文件，2,976 行）

审查日期：2026-09-28。编号区间 AUD-9100–AUD-9199。
维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。
本批次均为纯 numpy 数学/数据层（无 Qt、无 GL 调用），GLSL·HDR 与信号防递归维度大体不适用；重点在几何正确性、输入验证与跨线程数据约定。

跨文件事实（已核实）：
- backface culling 由 quality profile 开关：`src/features/composite/nodes/scene3d_node.py:3548` `_backface_cull = bool(_qp_params.get("backface_culling", False))`，`4256-4258` `ctx.enable(moderngl.CULL_FACE); ctx.cull_face = 'back'`。全仓库无 `front_face` 覆盖（默认 CCW）。
- VBO 池按 `(content_hash, is_skinned, has_tangent)` 共享 VBO/IBO：`src/features/composite/render3d/mesh_vbo_pool.py:90`。
- `raycast.ray_mesh_intersect` 在 src/ 与 tests/ 中无任何调用方（拾取实际走 `mesh_picking.py` 的 BVH）。

---

## src/features/composite/render3d/node_icon_overlay.py (行数: 726)

### 发现

- [AUD-9100] [Suggestion] add_camera_icon 的 near/far 与 add_light_icon 的 range 参数存而不用，docstring 与实现不符
  - 位置: src/features/composite/render3d/node_icon_overlay.py:139-150, 186, 379-398, 462-504
  - 证据:
    ```python
    139:            near: 裁剪面（视锥延伸距离用）。
    ...
    386:        lens_tip = eye + forward * size * 0.36
    387:        near_d = size * 0.55
    388:        far_d = size * FRUSTUM_LENGTH
    ...
    485:        if light_type == LIGHT_TYPE_DIRECTIONAL:
    486:            lines = self._build_directional_icon(
    487:                icon["icon_pos"], direction, base_color, icon_world_size)
    ```
    视锥 near/far 距离由 `size * 常数` 推出（387-388），`_build_camera_geometry`（231-274）与 `_build_light_geometry`（462-504）从不读取 `icon["near"]`/`icon["far"]`/`icon["range"]`。
  - 影响: API 误导。属性面板调整 near/far/range 时图标形态无任何反馈，与参数文档承诺矛盾。
  - 建议: 让 near/far 参与视锥长度换算、range 参与 point/spot 图标尺寸，或删除参数并修正 docstring。
  - 置信度: 高

- [AUD-9101] [Suggestion] 跨近平面折线直接剔除后方点，不做近平面裁剪，穿越视相机时线段错连
  - 位置: src/features/composite/render3d/node_icon_overlay.py:204-215, 643-658
  - 证据:
    ```python
    211:        if w <= 1e-6:
    212:            return None  # 相机后方或裁剪面外
    ...
    645:                pts_uv = []
    646:                for pw in points_world:
    647:                    uv = self._world_to_tex_uv(pw)
    648:                    if uv is not None:
    649:                        pts_uv.append(uv)
    650:                if len(pts_uv) >= 2:
    ```
  - 影响: 图标几何横跨查看相机近平面时，后方点被丢弃后剩余点直接相连（或整段消失），线框绘制错误。仅视口辅助 overlay，非合成输出。
  - 建议: 对 w<=0 与 w>0 相邻的线段在近平面处裁剪（插值交点）后再投影。
  - 置信度: 中

- [AUD-9102] [Suggestion] hit_test 每次调用全量重建所有图标几何并逐点投影，无缓存
  - 位置: src/features/composite/render3d/node_icon_overlay.py:663-709
  - 证据:
    ```python
    677:        for icon in self._icons:
    678:            if icon["type"] == "camera3d":
    679:                geom_groups = self._build_camera_geometry(icon)
    680:                anchor = icon["eye"]
    681:            else:
    682:                geom_groups = self._build_light_geometry(icon)
    ...
    687:                for pw in points_world:
    688:                    px = self._world_to_tex_px(pw)
    ```
    每个几何点一次 Python 级 4x4 双矩阵乘（204-215），与 build_draw_list 的每帧计算完全重复。
  - 影响: 鼠标移动/拖动拾取期间 UI 线程重复做整帧量级的几何生成；相机/灯光图标数量多时拾取延迟可感知。
  - 建议: update()/build_draw_list() 时缓存世界空间线段（含 icon 索引），hit_test 只做投影与距离判定。
  - 置信度: 高

- [AUD-9103] [Suggestion] hit_test 循环内 anchor 为死赋值
  - 位置: src/features/composite/render3d/node_icon_overlay.py:680, 683, 703-704
  - 证据:
    ```python
    679:                geom_groups = self._build_camera_geometry(icon)
    680:                anchor = icon["eye"]
    681:            else:
    682:                geom_groups = self._build_light_geometry(icon)
    683:                anchor = icon.get("icon_pos") or icon["pos"]
    ...
    703:        anchor = best_icon["eye"] if best_icon["type"] == "camera3d" else \
    704:            (best_icon.get("icon_pos") or best_icon["pos"])
    ```
    循环内赋值的 `anchor` 在循环体内从未使用，命中后在 703-704 重新计算。
  - 影响: 死代码，误导维护者以为循环内会用到。
  - 建议: 删除 680/683 两行。
  - 置信度: 高

- [AUD-9104] [Suggestion] `icon.get("icon_pos") or icon["pos"]` 用 or 而非 is not None 判空
  - 位置: src/features/composite/render3d/node_icon_overlay.py:683, 703-704
  - 证据:
    ```python
    703:        anchor = best_icon["eye"] if best_icon["type"] == "camera3d" else \
    704:            (best_icon.get("icon_pos") or best_icon["pos"])
    ```
  - 影响: 调用方传入 numpy 数组形态的 pos/icon_pos 时 `or` 触发 "truth value of an array is ambiguous" ValueError（类型注解为 tuple，但上游 payload 常用 ndarray）；空 tuple 也会意外回退到 pos。
  - 建议: 改为 `icon_pos if icon_pos is not None else pos`（add_light_icon 189-200 内部已是此写法，保持一致）。
  - 置信度: 中

### 通过项
- 线程安全：纯 UI 线程状态（类 docstring 明示），无跨线程共享可变数据
- GPU 资源生命周期：纯 numpy，不持有任何 GL 对象
- 信号防递归：无 Qt 信号（状态锁维度不适用）
- 状态提交一致性：每帧重建的临时数据，不涉及 undo/持久化
- 输入验证：_camera_frustum_geometry 对 fov/aspect/shift/focus/ortho_scale 有 clamp（380-384）；_camera_basis 对 forward 长度与 world_up 平行退化有保护（279-291）；_circle_points 基向量退化有处理（617-621）
- 异常路径：_point_to_segment_dist 对退化线段有保护（719-721）

---

## src/features/composite/render3d/perf_stats.py (行数: 264)

### 发现

- [AUD-9105] [Suggestion] reset() 不重置 quality_profile/viewport_layout/active_view，与"清零所有计数器"的 docstring 不符
  - 位置: src/features/composite/render3d/perf_stats.py:99-129
  - 证据:
    ```python
    99:    def reset(self, frame_id: int = 0) -> None:
    100:        """清零所有计数器，开始新一帧采集。frame_id 用于追踪。"""
    101:        self.frame_id = frame_id
    ...
    129:        self.resolution = (0, 0)
    ```
    reset 体中无 `self.quality_profile`/`self.viewport_layout`/`self.active_view` 赋值，三字段保留上一帧值。
  - 影响: 调用方 reset 后若未重新设置上下文字段，overlay/日志读到上一帧的陈旧 profile/layout（诊断数据失真，非功能性）。
  - 建议: reset 一并恢复默认值，或在 docstring 明示哪些字段跨帧保留。
  - 置信度: 高

### 通过项
- 线程安全：模块 docstring 明示"render thread 独占写、UI 只读"的单写者模型；UI 侧 to_dict() 可能读到混帧快照属文档化的诊断容忍范围（asdict 无锁为已知设计）
- GPU 资源生命周期：无 GL/Qt 调用（符合模块设计原则）
- GLSL·HDR：纯计时/计数，不涉颜色数据
- 异常与取消路径：ScopedTimer.__exit__ 返回 False 不吞异常；StatsLogger 对 logger=None/enabled=False 有保护
- 性能模式：ScopedTimer 极低开销设计正确；StatsLogger 首帧必输出 + interval 节流逻辑正确（206-210）
- 类型注解：完整

---

## src/features/composite/render3d/point_cloud.py (行数: 191)

### 发现

- [AUD-9106] [Major] 外部点云文件的 NaN/Inf 坐标未过滤，rasterize 出现未定义整型转换与静默错误输出
  - 位置: src/features/composite/render3d/point_cloud.py:96-103, 157-171
  - 证据:
    ```python
    97:                rows.append([float(parts[0]), float(parts[1]),
    98:                             float(parts[2])])
    ...
    168:    px = np.clip((xy[:, 0] + width / 2.0).round().astype(np.int64),
    169:                 0, width - 1)
    170:    py = np.clip(((height / 2.0) - (xy[:, 1])).round().astype(np.int64),
    171:                 0, height - 1)
    ```
    `float("nan")`/`float("inf")` 可被 xyz 解析接受；NaN 经 round→astype(int64) 触发 "RuntimeWarning: invalid value encountered in cast"（已在本地 .venv 复现），落点像素平台相关（本机实测钉到 0 号像素）；Inf 使 `span=inf → scale=0`，全部点静默堆叠在画面中心。
  - 影响: 违反"外部数据须验证"铁律。畸形但可解析的点云文件产出错误渲染结果（坏像点或整幅塌缩），无任何报错。PLY 路径同理（trimesh 顶点可为 NaN）。
  - 建议: 加载后 `np.isfinite(positions).all(axis=1)` 过滤并记录丢弃数；rasterize 前对 px/py 再防御一次。
  - 置信度: 高（cast 警告已本机复现；具体坏像素位置平台相关已注明）

- [AUD-9107] [Suggestion] points_from_track_data 对 tx/ty 直接 float() 无容忍，单帧坏值中断整个导入
  - 位置: src/features/composite/render3d/point_cloud.py:127-135
  - 证据:
    ```python
    128:        try:
    129:            frame_int = int(frame)
    130:        except (TypeError, ValueError):
    131:            continue  # tolerate junk keys instead of aborting the import
    133:        transform = transforms[frame] or {}
    134:        if str(transform.get("status", "valid")) != "valid":
    135:            continue
    136:        rows.append([float(transform.get("tx", 0.0)),
    ```
    帧 key 的 int() 有 try，但 136-137 行的 `float(tx)` 无：tx 为非数字字符串时抛 ValueError 直接终止导入，与 130 行"容忍垃圾"的意图不一致。
  - 影响: 单个坏帧值导致整个 tracker 导入失败。
  - 建议: float() 纳入同一 try 或单独捕获 ValueError 后 continue。
  - 置信度: 中（track_data 来自内部 tracker，格式通常受控）

### 通过项
- GLSL·HDR 正确性：srgb_to_linear 分段公式符合 IEC 61966-2-1；HDR 值无盲目 clamp（颜色域 clip 仅用于显示色，有注释理由）
- 输入验证（其余）：xyz 空文件/短行/注释行处理正确（88-105）；颜色行数与点数不一致回退 None 正确（108）；0..255 启发式有文档说明
- 异常路径：trimesh 缺失时 RuntimeError 带原因链（56-57），非宽泛 except
- 性能模式：rasterize 用花式索引批量写像素，无逐点 Python 循环
- 状态提交/信号：纯函数式数据层，不适用

---

## src/features/composite/render3d/primitives.py (行数: 645)

### 发现

- [AUD-9108] [Critical] plane/sphere/torus 及 cylinder 顶/底盖、cone 底盖的三角形缠绕与法线方向相反，backface culling 开启时整面被剔除
  - 位置: src/features/composite/render3d/primitives.py:299-305 (plane), 373-379 (sphere), 445-471 (cylinder 盖), 527-539 (cone 底盖), 582-589 (torus)
  - 证据（plane，声明法线 +Y，几何缠绕为 -Y）:
    ```python
    299:    verts = np.array([
    300:        [-s, 0.0, -s], [s, 0.0, -s], [s, 0.0, s], [-s, 0.0, s],
    301:    ], dtype=np.float32)
    302:    norms = np.tile([0.0, 1.0, 0.0], (4, 1)).astype(np.float32)
    304:    idx = np.array([0, 1, 2, 0, 2, 3], dtype=np.uint32)
    ```
    三角形 (v0,v1,v2)=(0,1,2) 的几何法线 (v1-v0)×(v2-v0) = (0,-4s²,0)，与声明法线 +Y 相反。同法验证：sphere `[a, b, a+1, a+1, b, b+1]`（379）在 (θ=π/3,φ=0) 处几何法线 (-0.866,0,-0.866) 指向球内；torus `[a, b, d, d, b, c]`（589）几何法线指向管内；cylinder 顶盖 `[top_center, s, s+1]`（457）与 cone 底盖（539）几何法线为 -Y/+Y 与声明的 +Y/-Y 相反。而 cube（282）、cylinder 侧面（443）、cone 侧面（525）经同样计算为正确的 CCW 朝外——同文件内两套缠绕并存。
  - 影响: 渲染错误。renderer 按 quality profile 启用 backface culling（scene3d_node.py:3548、4256-4258 `cull_face='back'`，全仓库无 front_face 覆盖、默认 CCW）：导航/快速 profile 下 plane 从上方看、sphere/torus 外表面、cylinder/cone 端盖全部作为背面被剔除——物体在导航时消失或中空，静止/导航间切换时闪烁；选中描边 inverted-hull pass（cull front，2710-2711）对这些网格行为同样颠倒。默认 two_sided=True 关闭剔除的路径下因法线显式提供而掩盖了该缺陷。
  - 建议: 统一翻转为朝外缠绕（plane → [0,2,1,0,3,2]；sphere → [a, a+1, b, a+1, b+1, b]；torus/caps 同理反向），并补一条单测断言每个基元每组三角 (v1-v0)×(v2-v0) 与声明法线点积 > 0。
  - 置信度: 高（缠绕方向经具体坐标逐例核算，cube 正确佐证 GL 默认 CCW 约定下其余确为反向；用户可见影响依赖 culling profile 开启，该条件由 NAVIGATION/FAST_SOLID 质量档满足）

- [AUD-9109] [Major] content_hash 不含 normals/uvs 字节，注释却声称包含；VBO 池据此共享缓冲致错误 UV/法线
  - 位置: src/features/composite/render3d/primitives.py:203-228（消费者 mesh_vbo_pool.py:90）
  - 证据:
    ```python
    212:        h = hashlib.sha1()
    213:        # 用 vertices/indices 的字节（足以区分不同几何；normals/uvs 也算上保证完整）
    214:        h.update(self.vertices.tobytes())
    215:        h.update(self.indices.tobytes())
    216:        if self.is_skinned:
    217:            h.update(b'skinned')
    218:            h.update(self.bone_indices.tobytes())
    219:            h.update(self.bone_weights.tobytes())
    ```
    213 行注释称"normals/uvs 也算上"，但 214-226 只哈希 vertices/indices/骨骼/morph，normals 与 uvs 不参与；而 interleaved()/interleaved_with_tangent() 产出的 VBO 包含 normals+uvs，mesh_vbo_pool.py:90 `key = (mesh.content_hash(), mesh.is_skinned, bool(has_tangent))` 直接复用。
  - 影响: 顶点+索引相同但 UV/法线不同的两个 MeshData（glTF 中共享 POSITION/INDEX accessor、不同 TEXCOORD/NORMAL set 的多 primitive 属合法且常见）哈希相同 → VBO 池命中错误缓冲，渲染出错误的 UV/法线；morph 数据反而参与哈希，主属性却不参与，逻辑失衡。
  - 建议: 将 self.normals.tobytes() 与 self.uvs.tobytes() 纳入哈希（或按不含它们的明确契约修正注释与池键）。
  - 置信度: 中（哈希缺失与注释矛盾为事实；实际碰撞需导入场景中出现同几何异 UV/法线的 mesh）

- [AUD-9110] [Suggestion] compute_tangents 的 denom+1e-12 处理数值不稳健
  - 位置: src/features/composite/render3d/primitives.py:156-158
  - 证据:
    ```python
    156:        denom = duv1[:, 0] * duv2[:, 1] - duv1[:, 1] * duv2[:, 0]
    157:        r = 1.0 / (denom + 1e-12)            # (T,)
    158:        t_tri = (e1 * duv2[:, 1, None] - e2 * duv1[:, 1, None]) * r[:, None]  # (T,3)
    ```
    denom 可为负：denom ≈ -1e-12 时除零得 inf；接近零的负值产生巨大 t_tri 污染累加。
  - 影响: 退化 UV 三角形（零面积 UV）导入时可能产生 inf/NaN 切线进 VBO。
  - 建议: `r = np.where(np.abs(denom) > 1e-12, 1.0 / np.where(np.abs(denom) > 1e-12, denom, 1.0), 0.0)`，对应 t_tri 置零。
  - 置信度: 高（数值机制确凿，触发需退化 UV）

- [AUD-9111] [Suggestion] MeshData.__post_init__ 不校验 vertices/normals/uvs 形状一致
  - 位置: src/features/composite/render3d/primitives.py:47-58
  - 证据:
    ```python
    49:        self.vertices = np.ascontiguousarray(self.vertices, dtype=np.float32)
    50:        self.normals = np.ascontiguousarray(self.normals, dtype=np.float32)
    51:        self.uvs = np.ascontiguousarray(self.uvs, dtype=np.float32)
    52:        self.indices = np.ascontiguousarray(self.indices, dtype=np.uint32)
    ```
    仅做 dtype/连续性归一，不检查 vertices.shape==(N,3)、normals.shape==(N,3)、uvs.shape==(N,2)、indices 最大值 < N。
  - 影响: loader 侧形状错误延迟到 interleaved() 广播失败或渲染期索引越界才暴露，定位困难；越界索引在 GL 侧行为未定义。
  - 建议: __post_init__ 中校验三数组行数一致与索引上界，不符即 ValueError。
  - 置信度: 高

- [AUD-9112] [Suggestion] PRIMITIVES 用 None 占位再于模块尾回填的脆弱模式
  - 位置: src/features/composite/render3d/primitives.py:391-400, 624-628
  - 证据:
    ```python
    396:    "cylinder": None,  # 占位，下方赋值（函数定义在 dict 之后）
    ...
    642:    gen = PRIMITIVES.get(name)
    643:    if gen is None:
    644:        raise ValueError(f"Unknown primitive: {name!r} (known: {list(PRIMITIVES)})")
    ```
    若回填语句与 dict 之间被插入代码或回填被误删，get_primitive 对已知名误报 "Unknown primitive"（None 与缺失不可区分）。
  - 影响: 可维护性风险，无当前功能缺陷。
  - 建议: 把 dict 定义移到全部函数之后一次性完整构造。
  - 置信度: 高

- [AUD-9113] [Suggestion] 类型注解缺失：local_aabb 返回值、_normalize_morph_map 入参
  - 位置: src/features/composite/render3d/primitives.py:65, 230
  - 证据:
    ```python
    65:    def _normalize_morph_map(self, values, label: str) -> dict[str, np.ndarray]:
    ...
    230:    def local_aabb(self):
    ```
  - 影响: 公共 API local_aabb 无返回注解（返回 tuple[np.ndarray, np.ndarray]）；values 应为 Optional[Mapping[str, np.ndarray]]。
  - 建议: 补齐注解。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：MeshData 为纯 numpy 数据，不持有 GL 句柄；缓存（_tangents/_local_aabb/_content_hash）为进程内惰性缓存无泄漏面
- 输入验证（几何生成器）：segments/rings/divisions 均有 max() 下限钳制（315, 353-354, 416, 494, 563-564）；cone slope 除零保护（504）；morph 形状不符 raise ValueError 明确（69-77）
- 性能模式：compute_tangents 全向量化（np.add.at 累加重复索引正确，161-163）；content_hash 结果缓存
- 异常路径：interleaved_skinned 前置 is_skinned 校验 raise ValueError（116-117）
- cube/cylinder 侧面/cone 侧面缠绕与法线一致（已逐面核算）

---

## src/features/composite/render3d/procedural_hdri.py (行数: 79)

### 发现

（无）

### 通过项
- 通过（无逻辑问题）。HDR 关键帧 >1.0 高光保留、无盲目 clamp；np.interp 对 v 越界做端点钳制语义正确；broadcast_to(...).copy() + ascontiguousarray 输出 C-contiguous float32 与 hdri.load_equirect 格式约定一致；纯函数无状态、无线程/资源/信号维度；DEFAULT_SKY_KEY 固定键与文件加载 IBL 区分明确

---

## src/features/composite/render3d/raycast.py (行数: 193)

### 发现

- [AUD-9114] [Suggestion] ray_mesh_intersect 为 O(M) 纯 Python 循环且全仓库无调用方（死代码）
  - 位置: src/features/composite/render3d/raycast.py:133-193
  - 证据:
    ```python
    164:    for i in range(0, idx.shape[0], 3):
    165:        i0, i1, i2 = int(idx[i]), int(idx[i + 1]), int(idx[i + 2])
    166:        v0 = verts[i0]
    167:        edge1 = verts[i1] - v0
    168:        edge2 = verts[i2] - v0
    ```
    src/ 与 tests/ 中除本文件定义外无引用（实际拾取走 mesh_picking.py 的 BVH：194 行 `def intersect(...)`）。
  - 影响: 若将来接线，大网格（数十万三角形）点选将阻塞 UI 线程数秒；当前则为无人维护的死路径，与 mesh_picking 双实现漂移。
  - 建议: 删除或改为 numpy 向量化 Möller–Trumbore，或委托 mesh_picking 的 BVH。
  - 置信度: 高

- [AUD-9115] [Suggestion] ray_mesh_intersect 假定 indices 为 3 的倍数，畸形数组越界 IndexError
  - 位置: src/features/composite/render3d/raycast.py:156-165
  - 证据:
    ```python
    156:    idx = np.asarray(indices).reshape(-1)
    157:    if idx.shape[0] < 3:
    158:        return None
    ...
    164:    for i in range(0, idx.shape[0], 3):
    165:        i0, i1, i2 = int(idx[i]), int(idx[i + 1]), int(idx[i + 2])
    ```
    idx.shape[0] % 3 == 1/2 时最后一次迭代 i+1/i+2 越界；7 行守卫只挡 <3。
  - 影响: 非法索引长度直接 IndexError（功能上当前无调用方，见 AUD-9114）。
  - 建议: `idx = idx[: (idx.shape[0] // 3) * 3]`（与 primitives.compute_tangents 148 行的写法一致）。
  - 置信度: 高（机制确凿；触发需畸形输入）

- [AUD-9116] [Suggestion] screen_to_ray 对奇异 proj@view 直接求逆无防护；ray_plane_intersect 的 t<0 语义未在契约中说明
  - 位置: src/features/composite/render3d/raycast.py:38-39, 126-130
  - 证据:
    ```python
    38:    vp = np.asarray(proj, dtype=np.float64) @ np.asarray(view, dtype=np.float64)
    39:    vp_inv = np.linalg.inv(vp)
    ...
    129:    t = float(np.dot(pp - o, pn)) / denom
    130:    return t
    ```
    inv 对奇异矩阵抛 LinAlgError 未包装；ray_plane_intersect 对交点在射线后方（t<0）仍返回数值，docstring（120 行）只说明"平行返回 None"，把 t>=0 判断隐式留给调用方。
  - 影响: 病态投影矩阵（NaN/退化）下异常以 LinAlgError 裸露；拖拽平面若穿过相机 origin，调用方漏判 t<0 会产生"反方向投射"跳变。
  - 建议: inv 前用条件数/行列式检测退化并 raise 带上下文的错误；ray_plane_intersect docstring 明示调用方须判 t>0（或加参数）。
  - 置信度: 中

### 通过项
- 线程安全：纯函数无共享状态
- ray_axis_intersect 公式与标准两线最近点解一致（a*e-b*d / denom），近平行 denom 保护返回 0 保持原地（99-103）
- ray_plane_intersect 平行判定 denom<1e-9 返回 None 正确；法线无需归一化的实现正确（122-130）
- Möller–Trumbore 主判定（u/v/t 区间与 eps< t < best_t）正确，cull_backface 分支语义正确（171-176, 187）
- 类型注解完整

---

## src/features/composite/render3d/scene_graph.py (行数: 599)

### 发现

- [AUD-9117] [Major] 节点身份仅由 (source_path, 名字路径) 哈希，同名兄弟节点 id/动画键冲突
  - 位置: src/features/composite/render3d/scene_graph.py:29-35, 383-398, 565-599
  - 证据:
    ```python
    29: def stable_scene_node_id(source_path: str, node_path: str) -> str:
    30:    """Return the stable id shared by hierarchy and render mesh metadata."""
    31:    digest = hashlib.md5()
    ...
    383:    def _visit(node: SceneNode, parent: np.ndarray, path: str) -> List[FlattenedMesh]:
    384:        world = parent @ node.local_transform
    385:        node_path = f"{path}/{node.name}" if path else node.name
    ...
    594:    local = sampled.get(root.name, root.local_transform)
    ...
    597:    for child in root.children:
    598:        out.update(apply_animation_to_scene(child, sampled, world))
    ```
    node_id = md5(source_path|名字路径)：两个同名兄弟的路径字符串完全相同 → 同一 id（glTF/FBX 不要求名字唯一）。sample_animation/apply_animation_to_scene 以 node_name 为 dict 键（581, 594, 598），同名节点互相覆盖变换。
  - 影响: 下游按 node_id 做选中同步与 hidden_mesh_ids 显隐（scene3d_node.py:2719-2724 消费）：重名 mesh 被耦合——隐藏/选中一个同时作用于另一个；动画通道按名字错绑到同名节点，模型动作错误。
  - 建议: node_path 追加同名消歧后缀（如 `#2`）或父级索引；动画匹配建议在 loader 侧建立 name→多节点列表并校验唯一性。
  - 置信度: 中（冲突机制确凿；触发依赖导入文件含重名兄弟节点）

- [AUD-9118] [Suggestion] compute_bbox 只检查 bmin 非有限，bmax 含 Inf 时穿透 → normalize_scene 产出退化缩放矩阵
  - 位置: src/features/composite/render3d/scene_graph.py:429-434, 452-471
  - 证据:
    ```python
    429:    bmin = np.min(mins, axis=0)
    430:    bmax = np.max(maxs, axis=0)
    431:    if not np.all(np.isfinite(bmin)):
    432:        bmin = np.array([-1, -1, -1], dtype=np.float32)
    433:        bmax = np.array([1, 1, 1], dtype=np.float32)
    434:    return bmin, bmax
    ```
    顶点某坐标含 +Inf 时该 mesh 的 min/max 同为 NaN 会被拦，但混合层级下 bmax 单独为 Inf 而 bmin 有限时不被拦（431 只验 bmin）；随后 normalize_scene `scale = target_size / max_extent` 得 0 或 inf，460 行直接生成全零/inf 缩放矩阵写进 root.local_transform。
  - 影响: 含 Inf 坐标的模型被静默压扁成一点或发散消失，无报错。
  - 建议: isfinite 同时校验 bmin 与 bmax；或在 compute_bbox 侧过滤非有限顶点（与 AUD-9106 同类根因）。
  - 置信度: 高（代码路径确凿；触发需非有限顶点）

- [AUD-9119] [Suggestion] flatten_scene/compute_bbox/apply_animation_to_scene 递归遍历无深度保护
  - 位置: src/features/composite/render3d/scene_graph.py:396-397, 425-427, 597-598
  - 证据:
    ```python
    396:        for child in node.children:
    397:            out.extend(_visit(child, world, node_path))
    ```
    （425-427 与 597-598 同型的自递归。）
  - 影响: 层级深度超过默认递归上限（~1000）的外部文件在导入路径抛 RecursionError（深层 FBX 骨架链可触及）。
  - 建议: 改显式栈迭代，或 _visit 带深度计数超限即日志+截断。
  - 置信度: 中（机制确凿，触发文件罕见）

### 通过项
- GPU 资源生命周期：纯 numpy 数据结构，无 GL 对象（符合"可独立单测"设计）
- 线程安全：不可变数据交换结构（load 后只读），normalize_scene 原地修改有 docstring 明示（450）
- _slerp 双覆盖（dot<0 取反最短路径）与近平行退化 lerp+归一化正确（483-500）；_sample_channel 二分查找 + 端点钳制 + 退化 denom 保护正确（503-529）
- quat_to_mat4/trs_to_mat4 公式核验正确（行主序 T@R@S 约定一致）
- __post_init__ 对 transform/color 做 dtype+shape 强制归一（111-113, 274-279, 315-317），形状错误立即 ValueError 而非延迟
- 输入验证：CameraData fov 缺省从 focal/sensor 推导有除零前置判断（277-279）
- 类型注解完整

---

## src/features/composite/render3d/scene_hierarchy.py (行数: 279)

### 发现

- [AUD-9120] [Suggestion] compute_hierarchy_stats 的 hierarchy 参数完全未使用，docstring 声称"避免重复遍历"
  - 位置: src/features/composite/render3d/scene_hierarchy.py:232-243, 249-279
  - 证据:
    ```python
    232: def compute_hierarchy_stats(root_data, hierarchy: Optional[SceneHierarchyItem] = None
    233:                             ) -> HierarchyStats:
    ...
    238:        hierarchy: 可选预构建的层级树（避免重复遍历）。
    ...
    268:    _visit(root_data.root)
    ```
    函数体从不引用 `hierarchy`，始终自行遍历 root_data.root。
  - 影响: 误导调用方以为传入可省一遍遍历；参数为死接口。
  - 建议: 删除参数或真正复用 hierarchy 的 vertex_count/triangle_count 统计。
  - 置信度: 高

- [AUD-9121] [Suggestion] 局部 AABB 计算用 `except Exception: pass` 宽泛吞异常
  - 位置: src/features/composite/render3d/scene_hierarchy.py:154-158
  - 证据:
    ```python
    154:            try:
    155:                bmin = [float(x) for x in mesh.vertices.min(axis=0)]
    156:                bmax = [float(x) for x in mesh.vertices.max(axis=0)]
    157:            except Exception:
    158:                pass
    ```
  - 影响: 违反 .clinerules"不得宽泛 except 掩盖领域错误"。空 mesh 的 ValueError 是预期回退，但形状错误、dtype 错误、内存错误等也被静默吞掉，bounds 静默变 None 难以排查。
  - 建议: 收窄为 `except ValueError`，其余异常放行。
  - 置信度: 高

- [AUD-9122] [Suggestion] 同名兄弟节点生成重复 node_id（同 AUD-9117 根因，本文件消费侧）
  - 位置: src/features/composite/render3d/scene_hierarchy.py:109-115, 135-137
  - 证据:
    ```python
    114:    from .scene_graph import stable_scene_node_id
    115:    return stable_scene_node_id(source_path, node_path)
    ...
    135:    def _visit(node: sg.SceneNode, path: str) -> SceneHierarchyItem:
    136:        node_path = f"{path}/{node.name}" if path else node.name
    137:        node_id = _make_stable_id(source_path, node_path)
    ```
    find_by_id（85-93）永远返回首个命中；collect_mesh_ids（95-102）把重名 mesh 的 id 合并为一个。
  - 影响: Outliner 选中/显隐按 id 定位时重名节点行为耦合（docstring 58 行称 id 用于"选中/显隐持久化"）。
  - 建议: 随 AUD-9117 一并在 node_path 加同名消歧。
  - 置信度: 中

### 通过项
- 线程安全 / GPU 资源：纯 Python 元数据摘要，不复制 numpy 大数组、不涉 GL（符合模块设计说明）
- 信号防递归：无 Qt 信号
- 输入验证（其余）：mesh/material 属性用 getattr+hasattr 防御旧 loader 数据（148-151, 257-259）；hierarchy_to_dict 对 None 边界处理正确（179-196）
- 状态提交：visible/locked 为 viewport-only 状态且有字段注释，不冒充持久化数据
- 文件头 DORMANT(2026-08-19) 状态标注清晰，符合休眠代码规范
- 延迟导入 scene_graph 避免循环依赖（133, 243）
- HierarchyStats.to_dict 键完整（217-229）

---

## 汇总

| 严重度 | 数量 | 编号 |
|---|---|---|
| Critical | 1 | AUD-9108 |
| Major | 3 | AUD-9106, AUD-9109, AUD-9117 |
| Suggestion | 19 | AUD-9100~9105, 9107, 9110~9116, 9118, 9119, 9120~9122 |

另：procedural_hdri.py 全文件通过，无发现。
