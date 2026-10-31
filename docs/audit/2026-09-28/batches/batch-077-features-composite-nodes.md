# 批次报告：batch-077 — src/features/composite/nodes//

编号区间：AUD-7700–AUD-7799。审查文件 3 个（605 行）。
核对过的关联模块：`nodes/base_node.py`、`nodes/depth_common.py`、`render3d/volume_grid.py`、
`core/frame_sequence.py`、`core/node_capabilities.py`、`core/tile_padding_computer.py`、
`core/shaders.py`、`evaluators/base_evaluator.py`（`_evaluate_node`/`_resolve_output`/
`_dispatch_effect`/`_create_ephemeral_node`/`_collect_owned_texture_ids`）、
`evaluators/filter_evaluator.py`（`_apply_mesh_warp`/`_apply_depth_tool`）、
`evaluators/merge_evaluator.py`、`nodes/constant_node.py`、`nodes/pointcloud3d_node.py`。

---

## src/features/composite/nodes/volume3d_node.py (行数: 201)

### 发现

- [AUD-7700] [Major] 每次求值新建 ModernGL 纹理但既不持有也不释放、不经 resource_pool，GL 纹理无主泄漏
  - 位置: src/features/composite/nodes/volume3d_node.py:190-197
  - 证据:
    ```python
    gl = getattr(ctx, "gl", ctx)
    data = np.ascontiguousarray(np.flipud(frame_img))
    tex = gl.texture((width, height), 4, data=data.tobytes(),
                     dtype="f4")
    return {"result": TextureResult(
        texture_id=tex.glo, width=width, height=height,
        alpha_premultiplied=False,
        color_space="linear",
    )}
    ```
  - 影响: `tex` 包装对象在 `_do_evaluate` 返回后即失去引用，节点也不保存引用（对照
    constant_node 的自管模式：`self._gpu_texture` + 释放 + `_cache_invalidated_needed`）。
    volume3d 在 `node_capabilities.py:897` 中 `action_type="source"`、`owns_color_texture=False`，
    不在 CACHE_OWNED_TYPES，`_collect_owned_texture_ids`（base_evaluator.py:1261）返回空集，
    缓存逐出时永远不会释放该纹理。ModernGL 5.12 默认 `gc_mode=None`（全库未配置 gc_mode），
    此时 `Texture.__del__` 两个分支（auto→立即 release / context_gc→入队）都不执行，包装器
    被 GC 后 GL 纹理名无人删除 → 每次缓存未命中求值泄漏一张 RGBA32F 纹理（480×360 约
    2.7MB，8192² 约 1GB）；帧序列擦洗/参数调整时无上限累积，最终 GPU OOM。若日后把
    gc_mode 设为 "auto"，纹理在返回前即被删除，返回的 texture_id 变悬垂。同款问题也存在于
    pointcloud3d_node.py:172-175（不在本批次范围，建议一并修复）。
  - 建议: 仿照 constant_node：把 `tex` 存到 `self._gpu_texture`，重建前 `release()` 旧纹理，
    并置 `self._cache_invalidated_needed = True`；或改用 `resource_pool.acquire_texture` 交由
    池管理并把 id 计入 owned。
  - 置信度: 高

- [AUD-7701] [Critical] RGB 已按 alpha 预乘却标记 alpha_premultiplied=False，合成时被二次乘 alpha，渲染结果错误
  - 位置: src/features/composite/nodes/volume3d_node.py:184-198
  - 证据:
    ```python
    alpha = 1.0 - transmittance
    frame_img = np.ones((height, width, 4), dtype=np.float32)
    frame_img[..., :3] = absorption[None, None, :] * alpha[..., None]
    frame_img[..., 3] = alpha
    ...
    return {"result": TextureResult(
        texture_id=tex.glo, width=width, height=height,
        alpha_premultiplied=False,
    ```
  - 影响: 存储的 RGB = absorption × alpha，即预乘形式，却声明直通 alpha。下游 Merge 对
    `alpha_premultiplied is False` 的输入会在着色器里再做一次预乘
    （merge_evaluator.py:185-186 置 `u_a_straight`，core/shaders.py:166
    `if (u_a_straight) { a.rgb *= a.a; }`），实际合成结果变成 absorption × alpha² + B×(1−alpha)，
    体积层被系统性压暗，主用途（把体积叠加到底片上）渲染错误。
  - 建议: 二选一：输出保持预乘数据并把 `alpha_premultiplied=True`；或保留 `False` 但 RGB 存
    未乘的 absorption 本色。
  - 置信度: 高

- [AUD-7702] [Suggestion] load_volume 的两个 except 分支代码完全重复，且后者为宽泛 Exception
  - 位置: src/features/composite/nodes/volume3d_node.py:134-143
  - 证据:
    ```python
    try:
        grid = load_volume(path)
    except VolumeDependencyError as exc:
        self.load_error = str(exc)
        self.error_message = f"Volume: {exc}"
        return {}
    except Exception as exc:
        self.load_error = str(exc)
        self.error_message = f"Volume: {exc}"
        return {}
    ```
  - 影响: 两分支行为一致，`VolumeDependencyError` 分支冗余；宽泛 `except Exception` 违反
    ".clinerules 不得宽泛 except 掩盖领域错误"（会把 OSError/MemoryError 等一律折叠成同一
    错误文案，难以区分缺依赖 vs 文件损坏 vs 资源耗尽）。
  - 建议: 收窄为 `except (VolumeDependencyError, ValueError, OSError)`，或保留领域异常单独
    分支给出可操作文案，其余异常 `logger.exception` 后再降级。
  - 置信度: 高

- [AUD-7703] [Suggestion] 纹理上传失败只记 warning，error_message 已被清空，节点呈现"无错误也无输出"的不一致状态
  - 位置: src/features/composite/nodes/volume3d_node.py:144,189-201
  - 证据:
    ```python
    self.error_message = ""
    ...
    try:
        gl = getattr(ctx, "gl", ctx)
        ...
    except Exception as exc:
        logger.warning("Volume upload failed: %s", exc)
        return {}
    ```
  - 影响: 体积加载成功、光线积分成功，但 GL 上传失败时（如 GL 上下文异常）`error_message`
    为空，UI 不显示任何错误，下游却拿到空输出；与 depth_common 约定的"缺输入必须可见空白+
    错误提示"精神不一致，用户无从排查。
  - 建议: 失败分支同时设置 `self.error_message = f"Volume upload failed: {exc}"`（或至少
    保留 load_error）。
  - 置信度: 高

- [AUD-7704] [Suggestion] output_width/height 上限 8192 × step_count 上限 128 的 CPU gather 无内存预算，极端参数可致渲染线程 MemoryError
  - 位置: src/features/composite/nodes/volume3d_node.py:75-78,166-177
  - 证据:
    ```python
    ParamDef("output_width", "int", 480, min_val=1, max_val=8192, ...),
    ParamDef("output_height", "int", 360, min_val=1, max_val=8192, ...),
    ...
    indices = sample_indices(depth, steps)
    ...
    slabs = grid.density[np.ix_(indices, ys, xs)]  # (n, h, w)
    ```
  - 影响: slabs 为 n_samples×height×width 的 float32 数组：2048²×128 采样约 2.1GB，
    8192²×128 约 34GB。参数完全在 PARAM_DEFS 允许范围内即可触发渲染线程 MemoryError，
    仅被求值器外层 `except Exception` 记 warning 后节点空白（base_evaluator.py:1042），
    表现为无法解释的空白帧。
  - 建议: 对 output 尺寸×采样数乘积设上限（如 ≤ 512MB）并降采样或分块累积；或在 UI 层
    收紧 max_val。
  - 置信度: 中

- [AUD-7705] [Suggestion] absorption_color 取自外部项目数据但未做类型/长度验证
  - 位置: src/features/composite/nodes/volume3d_node.py:154-156
  - 证据:
    ```python
    color = self.parameters.get("absorption_color",
                                (1.0, 1.0, 1.0, 1.0))
    absorption = np.asarray(color[:3], dtype=np.float32)
    ```
  - 影响: 旧版/手改工程把 absorption_color 存成字符串或长度不足时，`color[:3]`/asarray 抛
    TypeError，整个求值失败（外层仅记 warning），节点空白且 error_message 不含具体原因；
    违反".clinerules 外部数据须验证"。
  - 建议: 校验为长度 ≥3 的数值序列并 clamp 到 [0,1]，否则回退默认色并设置 error_message。
  - 置信度: 中

### 通过项
- 线程安全：节点在渲染线程经 live node 求值（source action），`gl = getattr(ctx, "gl", ctx)` 对
  CompositeEvalContext/裸 context 双兼容；`load_error`/`error_message` 为简单字符串写入。
- GLSL·HDR：输出为吸收式 alpha≤1 的生成图，`alpha = 1 - T` 是物理积分结果，不存在无理由
  clamp(0,1)（除 AUD-7701 的 alpha 标记问题外）。
- 信号防递归：本文件无 Qt 信号，不适用。
- 状态提交一致性：参数经 `set_parameter` 修订；`params_require_frame_cache`（####/%0d 判定）与
  `resolve_frame_pattern_path` 的正则约定一致（frame_sequence.py:20,25）；`runtime_file_signature`
  正确处理缺失/OSError 并入缓存键。
- 异常与取消路径：路径为空/加载失败均设置 error_message 并返回 {}；求值器外层还有兜底。
- 性能模式：preview 下 `steps // 2`（下限 8）降采样；只 gather 采样切片而非全深度。
- 输入验证（除 AUD-7705 外）：width/height `max(1,...)`、sigma `max(0.0,...)`、
  sample_indices 的 `max(1, steps)` 对 0/负数安全。
- 类型注解：公开函数与 `_do_evaluate` 均有完整注解。

---

## src/features/composite/nodes/warp_node.py (行数: 263)

### 发现

- [AUD-7710] [Major] grid_size 变更按扁平索引截断/补零 offsets，不做格点重采样，位移点被重新绑到新格的不同 (row,col)，变形视觉错乱
  - 位置: src/features/composite/nodes/warp_node.py:158-162（配合 102-104 的 pad/truncate）
  - 证据:
    ```python
    elif key == "grid_size":
        # Re-encode the lattice to the new point count so stale extra
        # points/channels don't linger and get misinterpreted on the
        # new lattice.
        self.set_offsets(self.get_offsets())
    ```
    ```python
    while len(offsets) < count:
        offsets.append([0.0, 0.0])
    return offsets[:count]
    ```
  - 影响: 点索引定义为 `row*(grid+1)+col`（point_index，warp_node.py:81-83）。`get_offsets()`
    在 super().set_parameter 已写入新 grid_size 后、用新 count 解析旧 JSON：4→8 增格时旧
    索引 7 原是 (1,2)，在新 9×9 格上变成 (0,7)；8→4 减格时截断保留的索引在新 5×5 格上同
    样错位。用户拖好格点后调整 grid_size，全部位移跳到错误格点（原排布被覆盖重写，无 undo
    时不可恢复），渲染结果错乱。
  - 建议: grid_size 变更时按 (row,col) 归一化坐标对旧格做双线性重采样到新格；或至少将非零
    位移清零并提示用户，而不是按索引错位搬运。
  - 置信度: 高

- [AUD-7711] [Suggestion] from_dict 用宽泛 `except Exception: pass` 吞掉点通道同步失败
  - 位置: src/features/composite/nodes/warp_node.py:117-120
  - 证据:
    ```python
    node = super().from_dict(data)
    try:
        node._sync_point_parameters()
    except Exception:
        pass
    return node
    ```
  - 影响: 同步一旦失败，正是该 docstring 描述的"offsets 与 warp_point_* 通道不一致→整个格点
    被静默归零"的病灶会原样保留，且无任何日志；违反".clinerules 不得宽泛 except 掩盖领域
    错误"。
  - 建议: 至少 `logger.warning` 记录异常；同步失败时保持现状即可但必须可观测。
  - 置信度: 高

- [AUD-7712] [Suggestion] 网格缩小时旧 warp_point_* 通道键永久残留 parameters 并随 to_dict 持久化
  - 位置: src/features/composite/nodes/warp_node.py:131-137,158-162
  - 证据:
    ```python
    def _sync_point_parameters(self) -> None:
        """Mirror offsets into numeric animation channels (roto pattern)."""
        offsets = self.get_offsets()
        for i, (dx, dy) in enumerate(offsets):
            names = _point_param_names(i)
            self.parameters[names["dx"]] = float(dx)
            self.parameters[names["dy"]] = float(dy)
    ```
  - 影响: `_sync_point_parameters` 只写新 count 内的通道、从不删除旧键；注释声称"stale extra
    points/channels don't linger"，但 8→4 后 warp_point_25…288 仍留在 parameters 里并写入工程
    文件（连同可能指向它们的 keyframe_tracks），造成工程膨胀与关键帧面板潜在的死轨道。
  - 建议: grid_size 变更时删除超出 `point_count(new_grid)` 的 warp_point_* 键（注意先清理
    引用它们的 keyframe_tracks）。
  - 置信度: 高

- [AUD-7713] [Suggestion] _do_evaluate 等方法类型注解缺失，与 base 及邻节点不一致
  - 位置: src/features/composite/nodes/warp_node.py:61,242-244
  - 证据:
    ```python
    def __init__(self, node_id: Optional[str] = None):
    ...
    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx,
                     fb_manager, resource_pool=None,
                     render_mode="preview") -> Dict[str, TextureResult]:
    ```
  - 影响: `ctx`/`fb_manager`/`render_mode` 无注解、`__init__` 缺 `-> None`、默认值用裸
    `None`/字符串；同目录 volume3d/zblur 均为 `ctx: Any, fb_manager: Any, ..., render_mode:
    str = "preview"`，风格不一致且削弱静态检查。
  - 建议: 对齐 base 签名：`ctx: Any, fb_manager: Any, resource_pool: Any = None,
    render_mode: str = "preview"`，`__init__` 补 `-> None`。
  - 置信度: 高

### 通过项
- 线程安全：无 Qt 信号；求值走 `_create_ephemeral_node` 生成的临时实例（effect action），
  读的是快照参数，无共享可变状态竞争。
- GPU 资源生命周期：节点本身不创建 GL 资源，输出纹理由 `_apply_mesh_warp` 经
  resource_pool 分配并在返回后归还池（lattice scratch 也正确 release）。
- GLSL·HDR 正确性：本文件无 GLSL；`_apply_mesh_warp` 恒等格短路直通，RG32F lattice 上传
  与 shader 消费一致（dx 按 W、dy 按 H 归一，节点 `_tile_padding` 与 filter_evaluator 的
  缩放约定一致）。
- 信号防递归：不适用。
- 状态提交一致性：offsets↔warp_point_* 双写虽有直写 dict，但 revision 由通道写入统一 bump，
  缓存键取自序列化参数，二者同步；`from_dict` 后强制 `_sync_point_parameters` 保证旧工程
  一致性（除 AUD-7711 的吞异常）；__init__ 中替换 ports 后二次 `_init_port_revisions()` 是
  必要且正确的。
- 异常与取消路径：输入无效返回 {}（可见空白，符合约定）；parse_offsets 对 JSON 损坏回退
  全零、逐项 clamp ±1、pad/截断到 count，外部数据验证到位。
- 性能模式：恒等格零 padding 且 evaluator 短路直通；动态 padding 以格点最大位移范数为上界。
- 动画路径核实：`apply_point_parameter_overrides` 读 `self.parameters`，而
  `_create_ephemeral_node`（base_evaluator.py:3068-3077）先把插值后的 warp_point_* 写入
  parameters 再求值，关键帧动画可正确生效。
- marker 协议：`_effect_type`/`_effect_input`/`_effect_params` 与 `_dispatch_effect` 的
  mesh_warp 分支（base_evaluator.py:1995-1998）匹配；BYPASS_MAPPING 正确映射 result→image。

---

## src/features/composite/nodes/zblur_node.py (行数: 141)

### 发现

- [AUD-7720] [Suggestion] _tile_padding 用 int() 截断模糊半径，与 shader 实际可达 ceil(radius) 的采样范围差 1px
  - 位置: src/features/composite/nodes/zblur_node.py:90-91
  - 证据:
    ```python
    def _tile_padding(self, params: Dict[str, Any]) -> int:
        return int(max(0.0, float(params.get("max_blur_radius", 12.0))))
    ```
    对照 core/shaders.py（_ZBLUR_FRAGMENT，Vogel 螺旋采样）：
    ```glsl
    float radius = u_max_radius * coc;
    ...
    vec2 offset = vec2(cos(angle), sin(angle)) * rr * radius * texel;
    ```
  - 影响: max_blur_radius 步进 0.5，取 12.5 时采样偏移可达 12.5px（触及第 13 个 texel），
    而 padding 截断为 12px；zblur 为 roi_policy="expand" 的可平铺节点（node_capabilities.py:
    687-697），分数半径下 tile 边缘 1px 采样不足，可能在拼缝处产生轻微亮/暗缝。
  - 建议: 改为 `int(math.ceil(...))`（与 blur 链 `int(ceil(u_radius))` 的口径一致）。
  - 置信度: 中

### 通过项
- 线程安全：无 Qt 信号；effect action 走临时节点实例，快照参数只读。
- GPU 资源生命周期：节点不建 GL 资源，输出纹理由 `_apply_depth_tool` 从 resource_pool 获取。
- GLSL·HDR 正确性：节点仅传参；shader 常量上限循环（MAX_SAMPLES）、小数带 .0；RGB 线性平均
  且 alpha 直通，无无理由 clamp。
- 信号防递归：不适用。
- 状态提交一致性：全参数经 PARAM_DEFS/__init__ 对齐（quality 选项、cache_to_disk 默认一致）；
  `cache_to_disk` 非死参数，由求值器 A6 磁盘缓存层消费（base_evaluator.py:851-880）。
- 异常与取消路径：`depth_marker_inputs` 同时校验 image+depth 有效性，缺任一返回 {}（可见
  空白，符合 depth_common 约定）；调度端对无效 depth 再兜底直通+warning。
- 输入验证：focus_distance clamp [0,1]、focal_range 下限 0.001、max_blur_radius ≥0、quality
  未知值回退 16 采样、depth_params 对 depth_units 白名单校验回退 normalized。
- 性能模式：preview 减半采样（下限 4）且半径>40 时减半（下限 40），方向安全——tile padding
  仍按全半径计算，宁多勿少；depth 输入 ROI 不扩张是正确的（shader 仅在中心 UV 取深度，
  无深度邻域采样）。
- 类型注解：完整。
- bypass：默认首输入→首输出映射，inputs 首键即 "image"，禁用节点时行为正确。
