# 批次报告：batch-047 — src/features/composite/core//

- 编号区间：AUD-4700–AUD-4799
- 审查文件：8 个（2,397 行），全部 Read 通读
- 汇总：Critical 1 / Major 0 / Suggestion 10
- 交叉验证：`core/undo_stack.py:33`（UndoAction 签名）、`render3d/format_capabilities.py:655`（resolve_asset_path）、`core/node_engine.py:600,622,2150`（get_node/set_node_parameter/from_dict→migrate_graph）、`tests/composite/test_global_3d_scene_settings.py:276-292`（v8 Scene3D 参数键集合）

---

## src/features/composite/core/__init__.py (行数: 1)

### 发现

（无）

### 通过项
- 通过（无逻辑）：仅一行包注释，无导出、无代码。

---

## src/features/composite/core/asset_dependency.py (行数: 860)

### 发现

- [AUD-4707] [Suggestion] 依赖收集不考虑节点 disabled 状态，禁用节点的空路径会阻塞整个 collect
  - 位置: src/features/composite/core/asset_dependency.py:278-284（配合 619-620 的无条件 yield）
  - 证据:
    ```python
        missing = tuple(reference for reference in report.references
                        if reference.is_missing)
        if missing:
            names = ", ".join(
                f"{item.node_id}.{item.parameter}" for item in missing)
            raise AssetCollectError(
                f"cannot collect missing asset dependencies: {names}")
    ```
  - 影响: `_iter_node_paths` 对 `video_frame`/`image_input` 无条件 yield `file_path`（默认 `""` → `STATUS_MISSING_PATH`），且全程不读节点的 `disabled` 字段；一个已禁用、未填路径的输入节点会让整个 `AssetCollectService.plan` 直接抛错，用户无法收集其余有效资产。
  - 建议: 在 `collect_asset_dependencies` 中跳过 `node.get("disabled")` 为真的节点（或至少不让禁用节点的 `missing_path` 阻塞 collect，仅在报告中标注）。
  - 置信度: 中

- [AUD-4708] [Suggestion] collect 回滚只还原参数，不清理已拷贝的目标文件
  - 位置: src/features/composite/core/asset_dependency.py:374-389
  - 证据:
    ```python
            os.makedirs(os.path.dirname(item.target_path), exist_ok=True)
            shutil.copy2(item.source_path, item.target_path)
            copied_count += 1
        ...
        except Exception:
            for change in reversed(applied):
                relink._set_live_reference(change, change.old_path)
            raise
    ```
  - 影响: 参数更新中途失败时，回滚仅还原节点引用，本次 `copied_count` 个新拷贝文件残留在用户目标目录（孤儿文件）；图状态与磁盘内容不一致地"半成功"。
  - 建议: 回滚时删除本次新建且 digest 仍匹配的目标文件，或至少在异常信息中列出残留路径。
  - 置信度: 高

### 通过项
- 线程安全：图变更全部经 `graph.set_node_parameter`（node_engine.py:622 自带锁），本模块无共享可变状态。
- 状态提交一致性：relink/collect 均为 plan→stale 校验→apply→单条 UndoAction（`undo_fn/redo_fn` 签名与 core/undo_stack.py:33-40 一致），批量原子回滚方向正确。
- 输入验证：serialized graph 逐层 isinstance（Mapping/list/dict）校验，`_path_text` 规整 PathLike/str，损坏的 material_overrides JSON 安全返回 None。
- 异常路径：使用专用 `AssetRelinkError`/`AssetCollectError`，`_file_digest` 把 OSError 转为领域错误并链式抛出，无宽泛 except 掩盖。
- 并发/TOCTOU：apply 前 digest+size 复验源与目标，"destination contains different data" 防静默覆盖。
- 确定性：references / grouped / changes 均排序，`to_dict` 输出稳定可持久化。

---

## src/features/composite/core/color_space.py (行数: 70)

### 发现

（无）

### 通过项
- GLSL·HDR 正确性：sRGB↔linear 公式为标准分段 EOTF；负值走线性延拓不产生 NaN；encode 分支 `max(c, 0.0)` 防 pow 负底数；全程无 clamp(0,1)，HDR 安全。
- 线程安全：纯函数无状态；类型注解完整；DISPLAY/UNKNOWN 语义在文档中明确。

---

## src/features/composite/core/eval_context.py (行数: 157)

### 发现

- [AUD-4709] [Suggestion] 大量字段注解为 Any，注释中已写明精确类型却未落进签名
  - 位置: src/features/composite/core/eval_context.py:79-80（同类：92、102、107、119、125、132、141、147、150-157）
  - 证据:
    ```python
        # 求值器在每次 frame 开始时清空；None 表示未启用旁路（向后兼容）。
        scene_payloads: Any = None  # Optional[Dict[str, Dict[str, "ScenePayload"]]]
    ```
  - 影响: `scene_payloads`/`scene_connections`/`ortho_view_textures`/`node_frame_overrides`/`view_mode_overrides`/`interaction_active`/`quad_view` 等跨线程传递的字段全部是 `Any`，渲染线程写入的字典结构无法被静态检查，键名/类型错误只能在运行期暴露。
  - 建议: 按 upgrade 为 `Optional[Dict[...]]`、`bool` 等精确注解（`ScenePayload` 等可用 `TYPE_CHECKING` 前向引用），注释保留说明线程所有权即可。
  - 置信度: 高

### 通过项
- 通过（纯数据容器）：无逻辑分支、无 GL 操作、无信号；所有字段的线程所有权与每帧清空约定在注释中明确（求值器负责生命周期）。
- 向后兼容：所有新增旁路字段默认 None/False，旧路径行为不变；`quality_profile` 有 str 注解。

---

## src/features/composite/core/format.py (行数: 330)

### 发现

- [AUD-4710] [Suggestion] `from_dict` 构造器对反序列化数据无类型校验，非数值直接抛未捕获 ValueError
  - 位置: src/features/composite/core/format.py:153-159、294-302
  - 证据:
    ```python
        return cls(
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
            pixel_aspect=float(data.get("pixel_aspect", 1.0)),
    ```
  - 影响: `data` 若来自损坏的工程/缓存 JSON（如 `"width": "1920px"`），`int()`/`float()` 在加载期抛 ValueError 且无上下文。当前 src 内无生产调用点（仅 tests/composite 使用），属潜在风险而非现行缺陷。
  - 建议: 接入持久化路径前增加 isinstance 校验 + 回退默认（或 try/except 转 ValueError 带字段名）。
  - 置信度: 高（代码行为确定；影响为潜在——无生产调用点）

### 通过项
- GLSL·HDR 正确性：`to_uniform_dict` 全部经显式 `float()` 转换，uniform 元组宽度一致；无任何 clamp(0,1) 侵入 HDR 数据。
- 几何正确性：`from_corners` floor/ceil 保证子像素包含性；`intersection`/`clamped` 空重叠返回规范化空 bbox；`expanded` 负 margin 由 `is_empty` 兜底。
- 线程安全：全部 frozen dataclass 不可变。
- 类型注解：除 `to_dict`/`from_dict` 返回裸 `dict` 外基本完整。

---

## src/features/composite/core/frame_sequence.py (行数: 39)

### 发现

（无）

### 通过项
- 输入验证：空路径/无模式路径原样返回；`%0` 误报场景（如 `file%0.png`）下 `_PATTERN.sub` 无匹配同样原样返回，安全。
- 正确性：`####` 用 zfill 保位、`%04d` 用 printf 语义，两种记法一致；纯函数、无状态、注解完整。

---

## src/features/composite/core/gl_bind.py (行数: 57)

### 发现

- [AUD-4711] [Suggestion] `glGetCurrentContext` 外围 `except Exception` 宽泛吞异常并静默跳过绑定
  - 位置: src/features/composite/core/gl_bind.py:45-52
  - 证据:
    ```python
        get_current = getattr(_gl, "glGetCurrentContext", None)
        if get_current is not None:
            try:
                current = get_current()
            except Exception:
                current = None
            if not current:
                return
    ```
  - 影响: 与 `.clinerules` "不得宽泛 except" 相抵：平台入口点以外的真异常也被当作"无上下文"处理，绑定静默 no-op，下游采样到旧纹理单元且无任何日志可查。
  - 建议: 收窄到 PyOpenGL 具体异常（如 `NullFunctionError`/`GLException`），其余异常打 warning 日志后再降级。
  - 置信度: 中

### 通过项
- GPU 资源生命周期：只绑定不创建/不释放，无泄漏面；每次调用经模块属性重解析入口点，晚建上下文仍可用。
- 线程安全：文档明确"须在渲染线程调用"，模块自身无共享状态、无上下文管理。
- 死上下文防护：`glGetCurrentContext` 为空时提前返回，规避文档所述 GL 1282/segfault 场景。

---

## src/features/composite/core/graph_migration.py (行数: 883)

### 发现

- [AUD-4701] [Critical] migrate_v6_to_v7 借用 v9 的 `_SCENE3D_KEEP` 删参，v6 场景环境参数被静默丢弃，v8→v9 的全局设置迁移扑空
  - 位置: src/features/composite/core/graph_migration.py:677-683（`_SCENE3D_KEEP` 定义于 601-603）
  - 证据:
    ```python
        # 从 scene3d 删除已迁移的参数（只保留 _SCENE3D_KEEP）
        migrated_keys = set()
        for names in _SCENE3D_PARAM_SPLIT.values():
            migrated_keys |= names
        for k in list(params.keys()):
            if k not in _SCENE3D_KEEP:
                params.pop(k, None)
    ```
    其中 `_SCENE3D_KEEP = frozenset({"width", "height", "format_name"})`（注释自述为 v9"纯渲染器"语义），而本函数 docstring 承诺"只保留画布/背景/gizmo 参数"；`migrated_keys` 求并集后从未使用（死变量）。
  - 影响: `bg_r/g/b/bg_opaque`、`show_grid/show_axes`、`grid_*`、`output_show_grid/output_bg_opaque`、`msaa`、`active_camera_id`、`light_color_*`（可能含 `hdri_preset`）既不在 `_SCENE3D_PARAM_SPLIT` 也不在 keep 集合，v6→v7 即被删除；随后 `migrate_v8_to_v9`（753-757 行）专门设计为把这些键提升到 `viewer_state["default_3d_scene"]`，却已无键可迁。≤v6 旧工程升级后场景背景色/网格/MSAA/HDRI/活动相机等用户设置静默回默认并随重存固化——数据丢失且渲染结果改变（背景色/IBL 可直接体现在输出画面）。v8 测试（tests/composite/test_global_3d_scene_settings.py:276-278）证实这些键在 pre-v9 Scene3D 上真实存在；死变量 `migrated_keys` 表明实现从"删已迁移键"漂移为"删全部非保留键"。
  - 建议: v6→v7 只删除 `_SCENE3D_PARAM_SPLIT` 并集内的键（真正迁走的），场景环境键留给 v8→v9 处理；删除或恢复 `migrated_keys` 的用途；补一条 v6 场景参数经全链迁移到 `default_3d_scene` 的回归测试。
  - 置信度: 高

- [AUD-4702] [Suggestion] migrate_graph 不校验版本字段与 nodes 类型：字符串版本崩溃、异常版本"未迁移即盖章"、nodes:null 崩溃
  - 位置: src/features/composite/core/graph_migration.py:868-882（nodes 迭代同型问题：198、248、310、347、352、390、651、656、708、746；v10_to_v11:830 已正确防护）
  - 证据:
    ```python
    version = graph_data.get("composite_schema_version", 1)
    ...
    while version < COMPOSITE_SCHEMA_VERSION:
        migration_fn = _MIGRATIONS.get(version)
        if migration_fn is None:
            break  # No migration for this version — skip
        data = migration_fn(data)
        version += 1

    data["composite_schema_version"] = COMPOSITE_SCHEMA_VERSION
    ```
  - 影响: 该数据来自持久化工程文件（node_engine.py:2150 `from_dict` 直接调用），属外部输入：版本为字符串时 `version < 11` 抛 TypeError 在加载期；`version <= 0` 时 `_MIGRATIONS.get` 返回 None → break 后仍把数据盖为当前版本（未迁移却标记已迁移，后续永不再迁）；`"nodes": null` 时 `graph_data.get("nodes", [])` 返回 None → TypeError。
  - 建议: 版本字段 `int()` 化并夹取到 `[1, COMPOSITE_SCHEMA_VERSION]`（越界值按 1 处理或显式报错）；各迁移函数统一 `graph_data.get("nodes") or []`；break 分支不应执行盖章语句。
  - 置信度: 高

- [AUD-4703] [Suggestion] v5→v6 提升 legacy 动画参数后一律重置为 0.0，而非按节点默认值恢复
  - 位置: src/features/composite/core/graph_migration.py:420-423
  - 证据:
    ```python
        # Restore plain scalar defaults for hoisted params so the evaluator
        # sees a normal value rather than a dict.
        for pname in legacy_keys:
            params[pname] = 0.0
    ```
  - 影响: `scale_x/scale_y/opacity/gain/gamma/white_point` 等默认值为 1.0 的参数，静态值在迁移后变 0.0。有 keyframes 时由轨道驱动无碍；但 `{"animated": True}` 而 keyframes 为空的退化参数不会建轨（410-411 行 `if kfs:`），其值从原值直落 0.0，可能造成全透明/全黑等渲染错误。docstring 所说 "restored to its plain scalar default" 与实现（统一 0.0）不符。
  - 建议: 按节点类型默认表恢复（可复用 `_V2_NODE_DEFAULTS`），或回退到轨道首帧值。
  - 置信度: 中

- [AUD-4704] [Suggestion] v2→v3 旧参数名重命名分支对 v1 工程不可达：v1→v2 已预先填入新名默认值
  - 位置: src/features/composite/core/graph_migration.py:255-265（与 117-127 `_V2_NODE_DEFAULTS["transform"]` 组合）
  - 证据:
    ```python
        # Handle old v2 default names that didn't match the node's actual params
        if "rotate" in params and "rotation" not in params:
            params["rotation"] = params.pop("rotate")
    ```
  - 影响: v1 工程先经 v1→v2 填满 `rotation/scale_x/scale_y/pivot_x/pivot_y` 默认值，随后 `"... not in params"` 守卫恒为 False——若 v1 节点确实以 `rotate/scale/anchor_x/anchor_y` 存值，用户值被静默丢弃（沿用默认 1.0/0.0）且旧键作为死数据残存于参数 dict 一路重存。
  - 建议: 改为"旧名存在即迁移（新名等于默认值时覆盖）"，至少在迁移尾部 pop 掉遗留旧名键。
  - 置信度: 中（机制确定；是否造成实际丢失取决于 v1 节点历史参数名）

- [AUD-4705] [Suggestion] 迁移链每步全量 deepcopy，v1 工程升级最多 11 次整图深拷贝
  - 位置: src/features/composite/core/graph_migration.py:873 与各迁移函数首行（196、243、308、338、383、650、705、742、807、828）
  - 证据:
    ```python
    data = copy.deepcopy(graph_data)

    while version < COMPOSITE_SCHEMA_VERSION:
        migration_fn = _MIGRATIONS.get(version)
        if migration_fn is None:
            break  # No migration for this version — skip
        data = migration_fn(data)
    ```
  - 影响: `migrate_graph` 已 deepcopy 一次并获得独占所有权，每个 `migrate_vN_to_vN+1` 内部再次 deepcopy；大工程（数千节点/连接/backdrops）打开时间无谓放大数倍。
  - 建议: 仅在 `migrate_graph` deepcopy 一次，逐步函数信任输入所有权（或提供 `_inplace` 内部变体供链式调用）。
  - 置信度: 高

- [AUD-4706] [Suggestion] `spawn_3d_scene_children` 参数与返回值缺类型注解
  - 位置: src/features/composite/core/graph_migration.py:478-484
  - 证据:
    ```python
    def spawn_3d_scene_children(scene3d_id: str, scene3d_position,
                                scene3d_params: dict, existing_ids,
                                include_model: bool = True,
    ```
  - 影响: `scene3d_position`/`existing_ids` 及返回值 `(child_nodes, child_conns)` 均无注解；该工厂被 migration 与实时创建两条路径共用，调用方只能靠 docstring 猜测类型（existing_ids 会被就地追加的 set 等）。
  - 建议: 补 `Tuple[float, float]`、`Set[str]`、`-> Tuple[List[dict], List[dict]]` 等注解。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 生命周期：纯 dict 变换，无 GL 对象、无节点实例化，加载期单线程调用。
- 状态提交一致性：迁移后统一盖章 `composite_schema_version`；v6→v7 与实时创建共用 `spawn_3d_scene_children` 内核消除"迁移 vs 新建"图结构分歧；子节点 id 冲突避免（existing_ids 就地登记）。
- 输入验证（局部）：v8→v9 对 `viewer_state` 非 dict、v9→v10 对 `layer_stacks/layers` 非 dict、v10→v11 对非 str/空白 `user_label` 均有容忍处理；v5→v6 对非 dict 节点/参数逐项 isinstance 过滤。
- 异常路径：迁移函数不吞异常（fail-fast）；v8→v9 多 Scene3D 差异仅 warning 不阻塞且每节点限报一条防日志爆炸。
- 确定性：分组/键集合均用 frozenset，遍历顺序稳定。

---

## 批次汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-4701 |
| Major | 0 | — |
| Suggestion | 10 | AUD-4702–4706, 4707–4711 |

核心结论：本批以纯数据结构/纯 dict 变换模块为主，线程与 GPU 维度整体干净；最重的问题集中在 `graph_migration.py` 的 v6→v7 删参逻辑漂移（AUD-4701，旧工程场景设置静默丢失），其余为外部输入校验、迁移健壮性与类型注解层面的改进项。
