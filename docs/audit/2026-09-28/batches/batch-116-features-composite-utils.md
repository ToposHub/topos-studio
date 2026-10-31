# batch-116 审查报告 — src/features/composite/utils

- 批次: batch-116 [P1] 主题 src/features/composite/utils//
- 编号区间: AUD-11600–AUD-11699
- 审查日期: 2026-09-28
- 审查维度: 线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

背景说明：本模块的增量保存机制（`IncrementalSaver`/`save_incremental`/`load_incremental`）目前仅通过 `src/features/composite/core/node_engine.py` 的 `serialize_to_bytes`/`deserialize_from_bytes` 公开，而后者在 src/ 与 tests/ 中均无调用点（grep 验证）。相关发现为潜在路径上的缺陷；`serialize`/`deserialize`/`detect_format` 为通用工具，风险同样为潜在（当前仓库无直接生产调用）。tests/ 中无任何文件 import 本模块。

## src/features/composite/utils/serialization_formats.py (行数: 545)

### 发现

- [AUD-11600] [Critical] 增量 delta 仅追踪硬编码白名单键，fps/duration/layers/layer_stacks 等顶层字段修改被静默丢弃（数据丢失）
  - 位置: src/features/composite/utils/serialization_formats.py:256-263, 271-363
  - 证据:
    ```python
    if not delta.get("nodes_added") and not delta.get("nodes_removed") \
       and not delta.get("nodes_modified") and not delta.get("connections_changed"):
        # No changes, return minimal delta
        return {
            "delta_type": "incremental",
            "base_version": self._base_generation,
            "delta": {"no_changes": True},
        }
    ```
    `_compute_graph_delta`/`apply_delta` 只处理 nodes / connections / backdrops / sticky_notes / groups / viewport_state / viewer_state：
    ```python
    # Backdrops delta
    if base.get("backdrops") != new.get("backdrops"):
        delta["backdrops"] = new.get("backdrops", {})
    # Sticky notes delta
    if base.get("sticky_notes") != new.get("sticky_notes"):
        delta["sticky_notes"] = new.get("sticky_notes", {})
    ```
  - 影响: `NodeGraph.to_dict()`（node_engine.py:2087-2141）还输出 `fps`、`duration_frames`、`layer_stacks`、`layers`、`composite_schema_version`、`graph_revision`、`topology_revision`，均不在白名单内。用户在基线快照之后修改 fps/时长/图层（AE/Nuke 混合图层系统）再增量保存：若只改了这些字段，delta 判定 "no_changes"；若同时改了节点，delta 也不含这些字段的变更。`apply_delta` 从旧 base 重建 → 重新加载后这些编辑全部丢失，且保存过程无任何告警。schema 迁移结果（composite_schema_version）同样无法通过增量保存落盘。
  - 建议: `_compute_graph_delta` 改为对"除已知结构性键外的所有顶层键"做全量比较（或显式加入上述全部键）；更稳妥的做法是检测到未知键变更时降级为 full save；`compute_delta` 的"无变化"判定需覆盖全部顶层键。
  - 置信度: 高（机制在两侧文件中均有真实代码佐证；当前无生产调用方，属潜在路径）

- [AUD-11601] [Major] serialize() 就地修改调用方传入的 dict（写入 `_format_version`），违反无副作用契约
  - 位置: src/features/composite/utils/serialization_formats.py:131-135
  - 证据:
    ```python
    # Add format_version marker for MessagePack
    if format == SerializationFormat.MSGPACK:
        data["_format_version"] = FormatVersion.FORMAT_MSGPACK
    else:
        data["_format_version"] = FormatVersion.FORMAT_JSON
    ```
  - 影响: 输入 dict 被污染一个 `_format_version` 键。当前 `serialize_to_bytes` 每次传入 `to_dict()` 新 dict 故未爆雷，但任何复用/缓存该 dict 的调用方（如 undo 快照、与 base snapshot 的比较）都会被污染；对 `save_incremental` 而言该标记落在 delta 信封顶层而非数据内，语义也是错的。另 `compute_delta` 的 `"full_data": new_data`（line 250）持有引用而非拷贝，存在同类别名风险。
  - 建议: 在函数内 `data = dict(data)`（或 deepcopy）后再注入标记；`full_data` 存 deepcopy。
  - 置信度: 高

- [AUD-11602] [Major] deserialize() 不校验顶层类型，非 dict 载荷违反返回注解，下游 AttributeError
  - 位置: src/features/composite/utils/serialization_formats.py:196-201
  - 证据:
    ```python
    if format == SerializationFormat.MSGPACK:
        if not MSGPACK_AVAILABLE:
            raise ValueError("MessagePack data but msgpack library not available")
        result = msgpack.unpackb(data, raw=False)
    else:
        # JSON
        result = json.loads(data.decode('utf-8'))
    return result
    ```
  - 影响: 签名声明返回 `Dict[str, Any]`，但 JSON 顶层为 list/str/number（如 `[1,2]`、`"x"`）或 msgpack 顶层非 map 时原样返回。调用链 `deserialize_from_bytes` 中 `if "delta_type" in loaded` 对 list 不报错（成员检查），随后 `from_dict(loaded)` 里 `data.get(...)` 抛 AttributeError，用户得到的是不可理解的崩溃而非清晰的格式错误。违反"外部数据须验证"铁律。
  - 建议: 返回前 `if not isinstance(result, dict): raise ValueError(...)`，错误信息指明期望对象格式。
  - 置信度: 高

- [AUD-11603] [Major] load_incremental 对 base_version 不匹配仅告警仍强行套用 delta，且保存后基线从不推进
  - 位置: src/features/composite/utils/serialization_formats.py:491-500
  - 证据:
    ```python
    if loaded.get("base_version", 0) != saver._base_generation if saver else 0:
        logger.warning("Delta base version mismatch, may have stale data")

    return saver.apply_delta(base_snapshot, loaded) if saver \
        else IncrementalSaver().apply_delta(base_snapshot, loaded)
    ```
  - 影响: 版本不匹配意味着 delta 是相对另一份基线算的，套用后节点/连接可能错乱（如 connections 引用已不存在的节点），产生静默损坏的图并被持久化——仅有一条 logger.warning。此外模块在增量保存后从不更新 `_base_snapshot`/`_base_generation`，正确性完全依赖调用方记得重新 `set_base_snapshot`，遗漏即触发本路径。
  - 建议: 版本不匹配时直接 raise（或降级要求 full data）；在 `save_incremental` 成功后由模块负责推进基线（deepcopy 新数据、generation+1），消除调用方遗忘面。
  - 置信度: 高

- [AUD-11604] [Major] _compute_graph_delta 假定节点结构完好，畸形外部数据触发 KeyError/TypeError
  - 位置: src/features/composite/utils/serialization_formats.py:280-281
  - 证据:
    ```python
    base_nodes = {n["id"]: n for n in base.get("nodes", [])}
    new_nodes = {n["id"]: n for n in new.get("nodes", [])}
    ```
  - 影响: `base.get("nodes")` 若为 dict（遍历产出字符串键，`n["id"]` → TypeError）或节点缺 `id` 键（KeyError）或节点不是 dict（TypeError）时直接崩溃，错误信息不含任何字段上下文；重复 id 也会被静默折叠（后者覆盖前者）。违反"外部数据须验证"。
  - 建议: 入口处校验 `nodes` 为 list 且各元素为含 `id` 的 dict，否则抛带上下文的 ValueError；对重复 id 显式报错或告警。
  - 置信度: 高（机制确定，触发依赖畸形文件，实际概率中等）

- [AUD-11605] [Suggestion] msgpack 不可用回退 JSON 后，文件内 `_format_version` 仍标记为 MSGPACK
  - 位置: src/features/composite/utils/serialization_formats.py:131-140
  - 证据:
    ```python
    if format == SerializationFormat.MSGPACK:
        data["_format_version"] = FormatVersion.FORMAT_MSGPACK
    else:
        data["_format_version"] = FormatVersion.FORMAT_JSON

    if format == SerializationFormat.MSGPACK:
        if not MSGPACK_AVAILABLE:
            logger.warning("MessagePack requested but not available, falling back to JSON")
            format = SerializationFormat.JSON
    ```
  - 影响: 先写标记后判回退，导致 JSON 输出携带 `FORMAT_MSGPACK(2)`。当前仓库无 `_format_version` 读取方（仅 project_manager.py:346/497 各自写入 1/2），实际影响有限，但一旦有消费方按该字段分派格式即出错。另 `FORMAT_MSGPACK_GZIP(3)`、`FORMAT_JSON_GZIP(4)`、`CURRENT_FORMAT_VERSION` 全仓库无引用，属死常量。
  - 建议: 回退判定提前到写标记之前；删除或落地使用 GZIP 版本常量。
  - 置信度: 高

- [AUD-11606] [Suggestion] 条件表达式优先级使版本校验"碰巧正确"，可读性差且越过私有属性访问
  - 位置: src/features/composite/utils/serialization_formats.py:496
  - 证据:
    ```python
    if loaded.get("base_version", 0) != saver._base_generation if saver else 0:
        logger.warning("Delta base version mismatch, may have stale data")
    ```
  - 影响: 实际解析为 `(loaded.get(...) != saver._base_generation) if saver else 0`——saver 为 None 时条件为 `0`（恒假），行为碰巧符合"无 saver 跳过校验"的意图，但极易在重构中被误改坏；同时 `load_incremental` 直接访问 `saver._base_snapshot`/`saver._base_generation` 私有成员。
  - 建议: 改写为 `if saver is not None and loaded.get("base_version", 0) != saver._base_generation:`，并为 IncrementalSaver 提供公开只读访问器。
  - 置信度: 高

- [AUD-11607] [Suggestion] detect_format 用宽泛 `except Exception` 吞掉 gzip 解压的所有异常
  - 位置: src/features/composite/utils/serialization_formats.py:98-100
  - 证据:
    ```python
    except Exception:
        # Failed to decompress, assume gzip JSON
        return SerializationFormat.JSON, CompressionFormat.GZIP
    ```
  - 影响: `gzip.decompress` 的领域异常是 `gzip.BadGzipFile`/`OSError`/`zlib.error`/`EOFError`；宽泛捕获会把 MemoryError 等非格式错误也归类为"格式未知"，违反"不得宽泛 except 掩盖领域错误"铁律。当前因 deserialize 二次解压会再抛 ValueError，链路尚能暴露损坏，但异常语义被稀释。
  - 建议: 收窄为 `except (OSError, zlib.error, EOFError)`（配合 inner_data 为空的 IndexError 一并处理）。
  - 置信度: 高

- [AUD-11608] [Suggestion] nodes_added 基于 set 遍历，序列化字节不可复现
  - 位置: src/features/composite/utils/serialization_formats.py:286-289
  - 证据:
    ```python
    # Added nodes
    added_ids = new_ids - base_ids
    if added_ids:
        delta["nodes_added"] = [new_nodes[id] for id in added_ids]
    ```
  - 影响: set 迭代顺序不定（跨进程/哈希随机），相同输入两次保存产生不同的 `nodes_added` 顺序 → 文件字节不稳定，影响 diff、基于哈希的缓存失效判断与测试断言。
  - 建议: `sorted(added_ids)` 后再构建列表。
  - 置信度: 高

- [AUD-11609] [Suggestion] apply_delta 的 nodes_modified 匹配为 O(N×M) 线性扫描
  - 位置: src/features/composite/utils/serialization_formats.py:401-417
  - 证据:
    ```python
    for mod in delta.get("nodes_modified", []):
        nid = mod.get("id")
        for node in result.get("nodes", []):
            if node.get("id") == nid:
    ```
  - 影响: 每个被修改节点都全量扫描节点列表；大图（数千节点、批量改参数）下加载重建退化为平方级。保存/加载路径虽非 60FPS 热路径，但影响大项目打开时间。
  - 建议: 先建一次 `{n.get("id"): n for n in result["nodes"]}` 索引再套用修改。
  - 置信度: 高

- [AUD-11610] [Suggestion] 对外部文件的 gzip 解压无大小上限（解压炸弹）
  - 位置: src/features/composite/utils/serialization_formats.py:90, 185-187
  - 证据:
    ```python
    if compression == CompressionFormat.GZIP:
        try:
            data = gzip.decompress(data)
    ```
  - 影响: 项目文件可能来自共享/下载，恶意或损坏的 gzip 可在解压时膨胀至耗尽内存（detect_format 处还会解压第二次，放大代价）。属"外部数据须验证"范围。
  - 建议: 解压前检查压缩块声明大小或用流式解压 + 最大字节数上限，超限即拒绝并抛 ValueError。
  - 置信度: 中

- [AUD-11611] [Suggestion] IncrementalSaver 无任何锁保护，跨线程共用将产生数据竞争
  - 位置: src/features/composite/utils/serialization_formats.py:206-234
  - 证据:
    ```python
    def __init__(self):
        self._base_snapshot: Optional[Dict[str, Any]] = None
        self._base_generation: int = 0

    def set_base_snapshot(self, snapshot: Dict[str, Any], generation: int = 0) -> None:
        """Set the reference snapshot for delta computation."""
        self._base_snapshot = copy.deepcopy(snapshot)
    ```
  - 影响: 本项目渲染走独立线程、保存可能在 worker 上执行；若 saver 被 UI 线程 `set_base_snapshot` 与保存线程 `compute_delta` 同时使用，`_base_snapshot` 引用替换与遍历之间无 happens-before 保证，delta 可能基于半更新状态。当前无生产调用方，属潜在风险。
  - 建议: 为 `_base_snapshot`/`_base_generation` 加 `threading.Lock`（读侧快照引用替换为原子操作），或在文档中明确限定单线程使用。
  - 置信度: 中（竞争窗口存在性确定，实际调用拓扑未知）

- [AUD-11612] [Suggestion] 内建名遮蔽：参数 `format` 与循环变量 `id`
  - 位置: src/features/composite/utils/serialization_formats.py:116, 289
  - 证据:
    ```python
    def serialize(
        data: Dict[str, Any],
        format: SerializationFormat = SerializationFormat.JSON,
    ```
    ```python
            delta["nodes_added"] = [new_nodes[id] for id in added_ids]
    ```
  - 影响: 遮蔽内建 `format`/`id`，降低可读性并易在后续扩展中引入隐性 bug；纯质量问题。
  - 建议: 重命名为 `fmt`/`node_id`。
  - 置信度: 高

### 通过项

- GPU 资源生命周期：纯数据序列化工具，不创建/持有任何 ModernGL 纹理、缓冲或上下文
- GLSL·HDR 正确性：不涉及着色器与像素数据，无 clamp(0.0,1.0) 风险
- 信号防递归：无 Qt 信号/控件，无递归触发面
- 异常与取消路径（部分）：deserialize 对 gzip 损坏显式转为 ValueError、JSON 解码错误以领域异常透传，msgpack 缺失时加载方报错/保存方回退路径清晰（detect_format 的宽泛 except 已单列 AUD-11607）
- 类型注解：所有公共函数、方法与类属性均有完整类型注解（命名遮蔽见 AUD-11612）
- 格式辅助一致性：get_extension 与 parse_extension 对 json/msgpack/gzip 组合的往返映射一致（".tgproj"/".tgproj.mp"/".tgproj.gz"）
- 格式检测：gzip/JSON/msgpack 魔数检测逻辑与本模块序列化输出自洽，未知内容默认 JSON 保证了旧文件兼容
