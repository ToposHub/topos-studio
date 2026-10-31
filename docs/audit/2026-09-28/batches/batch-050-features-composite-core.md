# 批次报告：batch-050 — src/features/composite/core

- 任务书：docs/audit/2026-09-28/assignments/batch-050.md
- 编号区间：AUD-5000–AUD-5099
- 审查日期：2026-09-28
- 文件数：3（2,204 行）

---

## src/features/composite/core/layer_stack_validator.py (行数: 338)

### 发现

- [AUD-5000] [Major] 图环检测为死代码：`topological_sort()` 有环时抛异常而非返回部分列表，`node_graph_cycle` 错误永远不会被报告
  - 位置: src/features/composite/core/layer_stack_validator.py:329-338
  - 证据:
    ```python
    try:
        order = self.graph.topological_sort()
    except Exception:
        order = None
    if order is not None and len(order) != len(self.graph.nodes):
        report.issues.append(ValidationIssue(
            severity=SEVERITY_ERROR, code="node_graph_cycle",
    ```
    交叉验证：`NodeGraph.topological_sort()`（src/features/composite/core/node_engine.py:1288-1289）为 Kahn 算法，有环时 `raise ValueError("Graph contains a cycle")`，成功时保证 `len(result) == len(self.nodes)`，永远不会返回"部分列表"。
  - 影响: 图中存在环时 `order` 必为 `None`，条件 `order is not None and ...` 恒为 False——注释声称"拓扑排序失败是权威信号并上报为 error"，实际行为完全相反：有环的图层栈校验结果为 `ok=True`（假阴性）。用户手动改线造成的环不会触发修复流程，会流向编译器/渲染。同时宽泛 `except Exception` 把所有非环异常（API 变更、属性错误等）一并吞掉，违反 .clinerules"不得宽泛 except 掩盖领域错误"。
  - 建议: 改为 `except ValueError: report.issues.append(... node_graph_cycle ...)`，其余异常单独捕获并记 warning/日志；或让 `topological_sort` 提供返回部分列表的非抛异常变体。
  - 置信度: 高

- [AUD-5001] [Suggestion] 无锁直接读取 `self.graph.nodes`，绕过 NodeGraph 的 RLock 保护
  - 位置: src/features/composite/core/layer_stack_validator.py:333
  - 证据:
    ```python
    if order is not None and len(order) != len(self.graph.nodes):
    ```
    而 `topological_sort()`/`get_node()`（node_engine.py:601、1269）内部均 `with self._lock:`。
  - 影响: 排序快照与 `nodes` 长度读取之间存在 TOCTOU：渲染线程并发增删节点时可产生虚假环报告或漏报（当前因 AUD-5000 该分支为死代码，实际风险被掩盖）。
  - 建议: 通过图对象提供的加锁访问器获取节点数，或在锁内完成比较。
  - 置信度: 高

- [AUD-5002] [Suggestion] Track Matte 校验对 `stack.layer_ids`（list）做逐层成员测试，整体 O(n²)
  - 位置: src/features/composite/core/layer_stack_validator.py:280
  - 证据:
    ```python
    if layer.track_matte_layer_id is not None:
        matte = layers.get(layer.track_matte_layer_id)
        if matte is None or layer.track_matte_layer_id not in stack.layer_ids:
    ```
    （`LayerStack.layer_ids: List[str]`，见 layer_model.py:234）
  - 影响: 层多的合成中校验耗时随层数平方增长；`_check_unique_ids` 已用 Set，此处不一致。
  - 建议: 在 `validate()` 开头构建 `layer_id_set = set(stack.layer_ids)` 复用。
  - 置信度: 高

- [AUD-5003] [Suggestion] `graph` 参数为 `Any` 且未做 None 防御，`stack` 合法但 `graph` 为 None 时直接 AttributeError
  - 位置: src/features/composite/core/layer_stack_validator.py:73-74、82-86、123
  - 证据:
    ```python
    def __init__(self, graph: Any):
        self.graph = graph
    ...
    if stack is None:
        ... return report
    ...
    if not self.graph.get_node(layer.root_node_id):
    ```
  - 影响: `validate()` 只对 `stack is None` 早退；graph 为 None 时第一个 `get_node` 即抛裸 AttributeError，调用方拿到的是崩溃而非 `ValidationReport`。同时 `Any` 类型注解丢失了图接口约束。
  - 建议: 构造或 validate 入口增加 graph None 检查（产出 `null_graph` issue），并将 `graph` 注解为协议类型（NodeGraph 或 Protocol）。
  - 置信度: 高

### 通过项
- 只读校验器，绝不改图（模块自述且各 check 均无 mutation）——状态提交一致性无风险
- 父链环检测逻辑正确：visited-set DFS，迭代上限 len(layers)+2 由鸽笼原理充分，收敛链/断链均正确终止
- 重复 layer id 检测 O(n) 正确（Set）
- 字段范围校验覆盖完整：blend_mode/track_matte_mode 白名单、opacity ∈ [0,1]、in_frame ≤ out_frame、self-track-matte、dangling parent/matte
- shared root、缺失 merge 节点、A/B 端口校验使用 getattr 容错访问，无崩溃路径
- 无 GPU/GLSL/信号代码（维度不适用）
- ValidationReport.ok/errors/warnings 语义正确

---

## src/features/composite/core/media_drop_payload.py (行数: 346)

### 发现

- [AUD-5004] [Major] 音频资产被静默误分类为 image，拖放后创建指向音频文件的 image_input 节点（坏图层）
  - 位置: src/features/composite/core/media_drop_payload.py:92-95、314-339（对照 :51、62-67）
  - 证据:
    ```python
    @property
    def node_type(self) -> str:
        """The source node type to instantiate for this media type."""
        return MEDIA_TYPE_TO_NODE_TYPE.get(self.media_type, "image_input")
    ```
    `_classify_media_type` 中别名表无 "audio" 分支，扩展名兜底也不含音频扩展名：
    ```python
    if p.endswith(vid_exts):
        return MEDIA_TYPE_VIDEO
    if p.endswith(img_exts):
        return MEDIA_TYPE_IMAGE
    return MEDIA_TYPE_IMAGE  # safe default
    ```
    而 `MEDIA_TYPE_AUDIO = "audio"`（:51）定义后全文件从未被返回/使用；`MEDIA_TYPE_TO_NODE_TYPE`（:62-67）也无 audio 条目。
  - 影响: 媒体池 `startDrag`（media_pool_panel.py:3697-3705）对所有选中资产（含音频，`asset.is_audio` 存在）无过滤地附带 asset_id；拖音频到 Keyframes 图层面板 → `resolve_drop_payloads` → media_type="image" → `add_media_payload` 创建 `image_input` 节点且 `file_path` 指向 .wav/.mp3 → 无法解码的坏图层，用户看到"拖放成功"但图层全黑/报错，而非被拒绝或路由到音频轨。
  - 建议: `_classify_media_type` 增加 audio 别名（"audio"/"sound"/"music"）与音频扩展名分支并返回 `MEDIA_TYPE_AUDIO`；`resolve_drop_payloads` 或 `_handle_keyframes_media_drop` 对 audio 类型跳过并记日志（模块自述 audio "not layer-renderable"）。
  - 置信度: 高

- [AUD-5005] [Suggestion] text/plain 兜底要求文本含逗号，单资产拖拽的兜底永远不生效
  - 位置: src/features/composite/core/media_drop_payload.py:150-161
  - 证据:
    ```python
    text = ""
    try:
        text = mime_data.text() or ""
    except Exception:
        text = ""
    if text and "," in text:
        tokens = [t.strip() for t in text.split(",") if t.strip()]
        if tokens and not any("/" in t or "\\" in t for t in tokens):
            return list(dict.fromkeys(tokens))
    return []
    ```
    拖拽源 `mime.setText(",".join(unique_ids))`（media_pool_panel.py:214）——单资产时文本为裸 id（无逗号）。
  - 影响: 自定义 MIME 缺失、仅有 text/plain 的旧/第三方拖拽源在单资产场景下解析为 []，与函数自述"text/plain fallback (comma-separated ids)"不符（多 id 才生效）。
  - 建议: 去掉 `"," in text` 门槛，统一按"拆分后所有 token 均无路径分隔符"判定。
  - 置信度: 高

- [AUD-5006] [Suggestion] 解析失败时按 index 把 asset_id 与 fallback_paths 配对的启发式可错配文件；`asset_lookup` 周围宽泛 except 掩盖领域错误
  - 位置: src/features/composite/core/media_drop_payload.py:201-219
  - 证据:
    ```python
        if asset_lookup is not None:
            try:
                asset = asset_lookup(aid)
            except Exception:
                asset = None
        elif project is not None:
            asset = _lookup_asset_in_project(project, aid)
        if asset is None:
            ...
            if index < len(fallback_paths):
                path = fallback_paths[index]
    ```
    拖拽源侧 `asset_ids` 与 urls 各自独立去重，且 urls 还被 `os.path.exists` 过滤（media_pool_panel.py:225-230），两组列表并非保证 1:1 对齐。
  - 影响: lookup 抛出真实领域异常（DB/IO 错误）被吞为 None 后落入该启发式；一旦 urls 因去重/存在性过滤与 ids 错位，会把 B 资产的文件路径安到 A 资产的 id 上，生成"内容错误的图层"且 asset_id 具有误导性（错误的数据比失败更糟）。
  - 建议: lookup 异常至少记 `logger.warning`（含异常）；配对前校验两组长度一致或改用 asset 自带 path 优先、仅在严格 1:1 时按序配对。
  - 置信度: 中（错配需同时满足 lookup 失败 + urls 列表错位，但路径已核实）

- [AUD-5007] [Suggestion] 显式携带 duration<=0 的视频/序列资产被置 duration=0，下游写入 `total_frames=0`
  - 位置: src/features/composite/core/media_drop_payload.py:281-289
  - 证据:
    ```python
    duration = _coerce_int(raw_duration, default=1)
    if duration <= 0:
        # Video/model duration is in frames; image/sequence is a single
        # frame. A model with no probe metadata still gets a usable one-frame
        # source marker; the layer itself uses the composition range.
        duration = 1 if media_type in (MEDIA_TYPE_IMAGE, MEDIA_TYPE_MODEL3D) else 0
    ```
    下游 `add_media_payload`（layer_stack_controller.py:703）`"total_frames": getattr(payload, "duration", 0)`。
  - 影响: 探测失败、元数据里 duration 显式为 0 的视频资产会创建 total_frames=0 的 video_frame 源，可能导致零长度/不可见的视频图层（image/model 有 1 帧兜底，视频没有）。
  - 建议: 视频缺有效时长时回退 1（与 image/model 一致）或标记为"时长未知"由控制器询问探测结果。
  - 置信度: 中（传播链已核实；video_frame 对 total_frames=0 的具体表现未逐行验证）

### 通过项
- 线程安全：纯数据变换函数，无模块级可变共享状态，无 Qt 导入（刻意解耦，见 `_local_paths_from_mime` docstring）
- `json.loads` 异常收窄为 (TypeError, ValueError)，符合规范
- 外部输入验证总体扎实：MIME 数据缺失/空/损坏均有容错，ids/paths 去重（保序），非本地 URL 过滤，`_coerce_int` 异常收窄
- 不涉及 GPU 资源、GLSL/HDR、信号防递归、undo/redo（维度不适用）
- `MediaDropPayload` 为 frozen 语义使用（默认 factory dict，无共享可变默认）

---

## src/features/composite/core/node_capabilities.py (行数: 1520)

### 发现

- [AUD-5008] [Major] 仅在 NODE_REGISTRY 动态注册的插件节点类型查询能力返回 None，被调度器归为 placeholder 而不渲染；docstring 与实现不符
  - 位置: src/features/composite/core/node_capabilities.py:1245-1248（对照 docstring :1230-1231）
  - 证据:
    ```python
    # Try capability table first
    cap = _NODE_CAPABILITY_TABLE.get(node_type)
    if cap is None:
        return None
    ```
    docstring 声称 "First checks if the node type is registered in NODE_REGISTRY, then merges class attributes with the capability table"，但代码在查注册表之前就已返回 None。下游（render_scheduler.py:595-596）：
    ```python
            elif is_placeholder_node_type(ns.node_type):
                action = "placeholder"
    ```
    而 `is_placeholder_node_type` 对未知类型返回 True（:1291-1293）。node_engine 提供 `register_node_type` / `register_plugin_nodes`（:2601、2673）作为一等插件入口，且注册表只做原地增删、从不整体重绑。
  - 影响: 任何经 `register_plugin_nodes` 注册且未手写进静态能力表的插件节点（正是一般插件场景——插件作者无法修改本文件）会被判为 placeholder，GPU 调度归类为 "placeholder"，无法走 effect 路径求值；且本模块没有提供 `register_capability` 之类的运行期注册 API，插件无合规途径声明能力。
  - 建议: 表未命中时回退到 NODE_REGISTRY，用类属性（IS_PLACEHOLDER、ROI_POLICY、TILE_PADDING 等类约定）合成默认 NodeCapability；或新增运行期能力注册 API 供 `register_node_type` 联动调用；同时修正 docstring。
  - 置信度: 高（代码路径全程核实；当前仓库内实际经此路径注册的非表内类型只有 motion_graphic，已在表中，故现网影响有限——风险针对插件工作流）

- [AUD-5009] [Suggestion] `get_node_capability` 每次调用都执行 `from .node_engine import NODE_REGISTRY`，热路径谓词函数逐节点逐帧付出重复导入开销
  - 位置: src/features/composite/core/node_capabilities.py:1250-1254
  - 证据:
    ```python
    # Check node class attributes for overrides
    # Lazy import to avoid circular dependency
    from .node_engine import NODE_REGISTRY

    node_cls = NODE_REGISTRY.get(node_type)
    ```
    本文件全部谓词（`is_effect_node_type`/`get_tile_padding`/`get_roi_policy`…）都经它分发，消费者为 render_scheduler、base_evaluator、tile_padding_computer、graph_preflight（每节点至少一次，调度循环内反复调用）。
  - 影响: 每次调用走 sys.modules 查找 + IMPORT_FROM 字节码；IS_PLACEHOLDER 覆盖命中时还会重新构造一个 NodeCapability（每节点一次堆分配）。4K 多节点合成下属可避免的每帧固定开销，违背"READ FIRST"模块被热路径广泛消费后的性能预期。
  - 建议: 首次成功导入后把 NODE_REGISTRY 的引用缓存到模块级变量（该 dict 原地变更、从不重绑，缓存引用安全）；IS_PLACEHOLDER 覆盖结果可按类型 memoize。
  - 置信度: 高

- [AUD-5010] [Suggestion] 导出的模块级派生集合不应用 IS_PLACEHOLDER 类属性覆盖，与 `get_node_capability` 形成两套可分歧的真值；一致性检查只覆盖 placeholder 字段
  - 位置: src/features/composite/core/node_capabilities.py:1168-1180（派生集合）、1256-1274（覆盖逻辑）、1450-1512（检查范围）
  - 证据:
    ```python
    (
        SOURCE_NODE_TYPES,
        ...
        PLACEHOLDER_NODE_TYPES,
        ...
    ) = _build_derived_sets()
    ```
    `_build_derived_sets` 仅读表（`if cap.is_placeholder: placeholder_types.add(...)`），而 `get_node_capability` 会用类属性覆盖并返回新对象（`action_type="placeholder" if is_placeholder_attr else ...`）。`check_registry_consistency` 只比对 missing_in_table/registry 与 placeholder，不校验 `owns_color_texture`/`owns_depth_texture`/`tile_padding`。
  - 影响: 直接导入 `PLACEHOLDER_NODE_TYPES`（及其它派生集合）的子系统与逐类型查询的子系统在类属性覆盖生效时结论不一致——这正是模块自述要解决的 drift 问题的一个残留面；而 owns_* 字段一旦表与节点实现漂移，错值直接导致 CacheManager 双重释放（崩溃）或纹理泄漏，却无任何一致性检查兜底。
  - 建议: 派生集合文档化"raw 表视图"语义并在消费侧迁移到谓词 API；扩展 `check_registry_consistency` 覆盖 owns_color_texture/owns_depth_texture 与节点实现的对照。
  - 置信度: 高

- [AUD-5011] [Suggestion] 未使用导入（`field`、`FrozenSet`）与裸 `tuple` 返回注解
  - 位置: src/features/composite/core/node_capabilities.py:30-31、1115
  - 证据:
    ```python
    from dataclasses import dataclass, field
    from typing import Dict, List, Literal, Optional, Set, FrozenSet, TYPE_CHECKING
    ...
    def _build_derived_sets() -> tuple:
    ```
    （全文件无 `field(` 调用、无 `FrozenSet[` 注解使用。）
  - 影响: 类型注解缺失/失准（Suggestion 级）；裸 `tuple` 使 `_build_derived_sets` 的 10 元组结构无类型信息。
  - 建议: 删除未用导入；返回注解改为 `Tuple[FrozenSet[str], ..., Dict[str, str]]`。
  - 置信度: 高

### 通过项
- 线程安全：模块级状态全部为 frozen dataclass / frozenset / dict 字面量表，import 后不可变；`get_node_capability` 对 NODE_REGISTRY 只读（CPython dict 并发读写安全）
- GPU 资源生命周期：Constant/Text/Scene3D/camera3d 等自管纹理节点 `owns_color_texture=False`（防 CacheManager 双重释放）、Scene3D/deep_merge `owns_depth_texture=True` 的取值均有注释依据且与既有 BUG 修复语义一致；`get_all_capabilities()` 返回浅拷贝、值不可变，无泄漏
- 表内 90+ 节点条目的 action_type/effect_type 一一对应，未发现 effect_type 缺失的 effect 节点
- `NodeContractClass` 五分类判定顺序（AI → 3D/输出 → 媒体 source → generator → image_filter）与 docstring 一致，未知类型兜底 image_filter 有明确说明
- `_layer_has_parent_cycle` 类的环检测不在本文件；本文件 `check_registry_consistency` 逻辑自洽（raw/final 双层比对设计合理）
- 异常路径：本模块不捕获异常（谓词对 None 容错返回默认值），无掩盖领域错误问题
- 无 GLSL/HDR 代码、无信号、无 undo/redo 交互（维度不适用）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-5000, AUD-5004, AUD-5008 |
| Suggestion | 9 | AUD-5001, AUD-5002, AUD-5003, AUD-5005, AUD-5006, AUD-5007, AUD-5009, AUD-5010, AUD-5011 |

| 文件 | Major | Suggestion |
|------|-------|------------|
| layer_stack_validator.py | 1 | 3 |
| media_drop_payload.py | 1 | 3 |
| node_capabilities.py | 1 | 3 |

总计：Critical 0、Major 3、Suggestion 9。
