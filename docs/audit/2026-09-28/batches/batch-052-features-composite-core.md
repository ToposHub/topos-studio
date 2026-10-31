# batch-052 审查报告：src/features/composite/core/

编号区间：AUD-5200–AUD-5299。审查日期：2026-09-28。
文件数：5，共 1,115 行。跨文件事实均已用只读命令核实（仓库根 `plugins/node_plugins/` 存在；`src/features/plugins/` 不存在；`src/composite` 不存在）。

---

## src/features/composite/core/node_plugin.py (行数: 565)

### 发现

- [AUD-5200] [Major] 应用级插件目录路径解析错误，仓库自带节点插件永不被发现
  - 位置: src/features/composite/core/node_plugin.py:182
  - 证据:
    ```python
    PLUGIN_DIRS = [
        # User-level plugins (highest priority)
        Path.home() / ".topos" / "plugins" / "node_plugins",
        # Application-level plugins
        Path(__file__).parent.parent.parent / "plugins" / "node_plugins",
    ]
    ```
  - 影响: 本文件位于 `src/features/composite/core/`，`parent.parent.parent` 只到 `src/features`，故应用级目录被解析为 `src/features/plugins/node_plugins`（已核实不存在）。仓库真正的应用级目录是仓库根 `plugins/node_plugins/`（含 `posterize_plugin.py`、`vintage_plugin.py`），永不扫描——所有随应用分发的节点插件静默失效，与模块头/类 docstring 及 `src/features/shared/effects/effect_catalog.py:11` 宣称的 `<app>/plugins/node_plugins/` 行为不符。附带发现：这两个自带插件 import 的是重构前路径 `from src.composite.node_plugin import ...`（`src/composite` 已不存在），说明该目录长期处于死代码状态。
  - 建议: 改用 `Path(__file__).parents[4]`（或从应用启动时注入 app root）定位 `plugins/node_plugins`；同时更新自带示例插件的 import 到 `src.features.composite...` 并补一个目录可发现性测试。
  - 置信度: 高

- [AUD-5201] [Major] exec_module 失败后残破模块永久遗留 sys.modules
  - 位置: src/features/composite/core/node_plugin.py:279-280、306-314
  - 证据:
    ```python
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    ```
  - 影响: `exec_module` 抛异常时进入 except 分支记录错误并返回 False，但此前已插入 `sys.modules` 的半初始化模块从不删除（importlib 官方模式要求失败时 `del sys.modules[module_name]`）。结果是：一个加载失败的插件以破损状态留在全局模块表中，任何后续同名 `__import__`/`reload_plugins` 重试语义混乱，且破损模块中已执行到的类/对象仍可被外部拿到。
  - 建议: 在 except 中（或用 try/finally 判断成功标志）执行 `sys.modules.pop(module_name, None)` 后再返回 False。
  - 置信度: 高

- [AUD-5202] [Major] unregister_plugin 会删除被其他插件 override 的同名节点类型
  - 位置: src/features/composite/core/node_plugin.py:434-438（对照 372-378 的 override 注册）
  - 证据:
    ```python
    for node_class in plugin_class.node_types():
        node_type = node_class.node_type

        if node_type in self._node_types:
            del self._node_types[node_type]
    ```
  - 影响: `register_plugin`（372-378 行）对同名 node_type 的处理是"overriding with '<新插件>'"——保留后注册者的类；而 `unregister_plugin` 只按 node_type 键无差别 `del`，不校验当前持有者。卸载"先注册且已被覆盖"的插件 A 时，会把插件 B 覆盖注册的节点一并删除，而 B 仍处于已注册状态——其节点从 `node_types`/`node_categories` 消失，UI 与 `create_node` 均取不到，属功能性缺陷。
  - 建议: 注销时仅当 `self._node_types.get(node_type) is node_class` 才删除；或维护 node_type → plugin_name 的反向归属表。
  - 置信度: 高

- [AUD-5203] [Major] register_plugin 注册非原子：异常路径留下幽灵节点类型且重试会产生分类重复
  - 位置: src/features/composite/core/node_plugin.py:369-398、400-409
  - 证据:
    ```python
    self._node_types[node_type] = node_class
    self._node_categories[category].append(node_type)

    if self.on_node_registered:
        self.on_node_registered(node_type, node_class)

    # Store plugin
    self._plugins[info.name] = plugin_class
    ```
  - 影响: 节点类型先写入共享注册表，插件本体最后（387 行）才入 `_plugins`。若循环中 `node_class.node_type` 属性缺失或 `on_node_registered` 回调抛异常，except 分支返回 False，但已写入的 `_node_types`/`_node_categories` 条目不回滚——形成无归属插件的"幽灵"节点类型；又因插件未入 `_plugins`，重试不会命中"already registered"检查，379 行的 `append` 无去重，分类列表会累积重复 node_type，UI 节点面板出现重复项。
  - 建议: 先完成全部校验与回调试探，最后一次性提交（或异常时按已写入键回滚）；`_node_categories[category].append` 前判重。
  - 置信度: 中（触发依赖回调/属性异常，但状态不一致逻辑确凿）

- [AUD-5204] [Suggestion] docstring 虚构 "Discovery uses a file lock"，实际无任何锁
  - 位置: src/features/composite/core/node_plugin.py:163-164
  - 证据:
    ```python
    Thread Safety:
    - Registration methods are not thread-safe. Call from main thread only.
    - Discovery uses a file lock to prevent concurrent access.
    ```
  - 影响: 全文件无任何文件锁/线程锁实现。依赖此声明从多线程调用 `discover_plugins` 会产生 dict 竞态与重复模块加载，误导维护者。
  - 建议: 删除该行，或如实声明整个管理器非线程安全（含 `get_plugin_manager` 单例的惰性初始化）。
  - 置信度: 高

- [AUD-5205] [Suggestion] 注册时写入的 PluginInfo.path/enabled 不持久，查询结果恒为默认值
  - 位置: src/features/composite/core/node_plugin.py:389-391、478-501
  - 证据:
    ```python
    # Update plugin info
    info.path = source_path
    info.enabled = True
    ```
    而 `get_plugin_info`/`get_all_plugin_info` 每次都 `cls.plugin_info()` 重新构造对象（docstring 示例插件亦如此实现）。
  - 影响: 390-391 行对局部 `info` 的修改随即丢失，后续 `get_plugin_info()` 返回的 path 恒为 ""、enabled 恒为构造默认——插件面板/诊断无法显示来源路径。
  - 建议: 管理器自行持有 `Dict[str, PluginInfo]`（注册时构建一次），查询直接返回副本；或要求插件缓存 info。
  - 置信度: 中（取决于插件端 plugin_info() 是否每次新建，文档示例是新建）

- [AUD-5206] [Suggestion] on_load 成功后注册失败，不回滚 on_unload，钩子不平衡
  - 位置: src/features/composite/core/node_plugin.py:348-362
  - 证据:
    ```python
    if not plugin_class.on_load():
        ...
        return False

    node_types = plugin_class.node_types()
    if not node_types:
        logger.warning(f"Plugin '{info.name}' provides no node types")
        return False
    ```
  - 影响: `on_load()` 已执行（可能申请了资源/注册了回调）后因 node_types 为空返回 False，管理器视作未注册，但插件侧初始化副作用留存且永不收到 `on_unload()`。
  - 建议: 在提前 return False 的路径上调用 `plugin_class.on_unload()`（忽略其异常并记录）。
  - 置信度: 高

- [AUD-5207] [Suggestion] reload_plugins 不清理 sys.modules 中的旧插件模块
  - 位置: src/features/composite/core/node_plugin.py:519-536
  - 证据:
    ```python
    # Clear loaded modules cache
    self._loaded_modules.clear()
    self._plugin_errors.clear()

    # Re-discover
    return self.discover_plugins()
    ```
  - 影响: 仅清空私有缓存；`sys.modules` 中旧 `topos_node_plugin_*` 条目残留。被删除的插件文件其模块永久滞留（内存/命名空间泄漏），两目录同 stem 插件还会共用同一 module_name 互相覆盖。
  - 建议: reload 时同步 `sys.modules.pop(name)` 所有 `topos_node_plugin_` 前缀条目。
  - 置信度: 高

- [AUD-5208] [Suggestion] check_dependencies 返回类型为裸 tuple，无参数化注解
  - 位置: src/features/composite/core/node_plugin.py:140
  - 证据:
    ```python
    def check_dependencies(cls) -> tuple:
    ```
  - 影响: 类型检查器无法校验 `(bool, List[str])` 结构，与文件其余部分的严格注解风格不一致。
  - 建议: 改为 `-> tuple[bool, list[str]]`。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：本文件不创建/持有任何 ModernGL 对象，on_load/on_unload 钩子将资源管理委托给插件。
- GLSL·HDR 正确性：无着色器代码。
- 信号防递归：无 Qt 信号。
- 异常路径：三处 `except Exception` 均带 exc_info 日志、写入 `_plugin_errors` 并触发 `on_plugin_failed` 回调，未静默掩盖（宽捕获用于外部插件代码属合理）。
- 输入验证：spec/loader 为 None 时显式抛 ImportError；依赖缺失、on_load 失败、空 node_types 均有检查。

---

## src/features/composite/core/output_targets.py (行数: 164)

### 发现

- [AUD-5209] [Suggestion] 重复/空 node id 在 node_order 中相互覆盖，依赖集与顺序可能失真
  - 位置: src/features/composite/core/output_targets.py:90-95
  - 证据:
    ```python
    node_list = list(nodes)
    node_order = {
        str(_read_value(node, "id", None)
           or _read_value(node, "node_id", "")): index
        for index, node in enumerate(node_list)
    }
    ```
  - 影响: 序列化数据（外部输入）若含重复 id 或多个缺 id 节点，dict 推导使后者覆盖前者、空 id 全部折叠为 ""，`upstream` 映射与 `dependency_node_ids` 排序随之失真，preflight/求值裁剪（base_evaluator.py:503 依赖此结果）可能漏算或多算依赖分支。
  - 建议: 构建时检测重复/空 id，重复时保留首见索引并记日志，或将异常 id 节点排除出依赖图。
  - 置信度: 中（触发依赖调用方图数据质量；正常唯一 id 场景无影响）

### 通过项
- 线程安全：纯函数，无共享可变状态。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及。
- 异常与取消路径：无异常路径，纯计算。
- 输入验证：同时兼容 Node 对象/快照/序列化 dict（`_read_value` 双读），非 dict 的 parameters 回退空 dict；`normalize_output_name` 对空/空白回退 "display"；连接引用未知节点 id 时被忽略。
- 性能模式：依赖遍历为 O(V+E) 迭代式 DFS，`visited` 防环防死循环，`dict.fromkeys` 去重直接输入，结果为不可变 tuple/frozenset。
- 状态提交一致性：无 undo/持久化职责（消费方为 preflight 与 evaluator）。
- 类型注解：完整（含 `from __future__ import annotations` 下的现代容器注解）。

---

## src/features/composite/core/quality_profile.py (行数: 61)

### 发现

（无）

### 通过项
- 输入验证：`normalize_quality_profile` 对枚举/字符串统一归一并回退 preview；`quality_profile_for_request` 对非法 `proxy_scale` 用 try/except (TypeError, ValueError) 回退 1.0，且明确不变异输入。
- 线程安全：纯函数，无状态。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交：不涉及。
- 异常路径：float 转换异常被窄化捕获并回退，无掩盖。
- 性能模式：常量集合判断，无开销。
- 类型注解：完整。

---

## src/features/composite/core/runtime_context.py (行数: 139)

### 发现

- [AUD-5210] [Suggestion] clip_effects 将可变 EffectInstance 引用带入"frozen"上下文，跨线程别名风险
  - 位置: src/features/composite/core/runtime_context.py:60-65
  - 证据:
    ```python
    # Phase 4: the clip's EffectInstance chain, supplied when
    # input_stage == "after_clip_effects" so the provider can run them
    # on the after_color texture. Empty tuple by default (no effects).
    clip_effects: tuple = ()
    # Read-only-at-the-boundary caption snapshot supplied by the Edit timeline.
    caption_input: object | None = None
    ```
  - 影响: frozen dataclass 仅保证字段不可变；tuple 内的 EffectInstance 是共享可变对象，而类头声称"frozen dataclass，不可变"。该上下文跨线程传递给渲染 worker（项目渲染走独立线程），若 UI/undo 侧在求值期间修改同一实例，缺乏文档保障（docstring 仅对 caption_input 声明 read-only-at-the-boundary），存在数据竞争窗口。
  - 建议: 在 docstring 中明确 clip_effects 的所有权/只读契约，或边界处做快照（深拷贝/参数冻结），与 caption_input 同等对待。
  - 置信度: 中（契约层面缺陷；实际竞争取决于 provider 实现与调用时序）

- [AUD-5211] [Suggestion] quality/input_stage 为自由字符串，未与 CompositeQualityProfile 归一化对接
  - 位置: src/features/composite/core/runtime_context.py:56-57
  - 证据:
    ```python
    quality: str = "preview"
    input_stage: str = "source_raw"
    ```
  - 影响: docstring 注明取值 (preview, proxy, master) 与 (source_raw, after_color, after_clip_effects)，但无 Literal/枚举约束，且 "proxy"/"master" 与同目录 quality_profile.py 的 draft/preview/final 体系不一致——下游按字符串比较时拼写错误静默落入默认分支。
  - 建议: 用 `Literal[...]` 或复用 CompositeQualityProfile 归一化函数收口。
  - 置信度: 中

- [AUD-5212] [Suggestion] clip_effects 类型注解为裸 tuple
  - 位置: src/features/composite/core/runtime_context.py:63
  - 证据:
    ```python
    clip_effects: tuple = ()
    ```
  - 影响: 无法表达元素类型（EffectInstance 序列），静态检查与 IDE 补全失效。
  - 建议: 改为 `tuple[EffectInstance, ...]`（TYPE_CHECKING 导入）。
  - 置信度: 高

### 通过项
- 线程安全：frozen dataclass，字段不可变（见 AUD-5210 对内容别名的保留意见）。
- GPU 资源生命周期：按设计不含 texture/GPU 句柄（docstring 明示由 Provider 提供）。
- GLSL·HDR / 信号防递归 / 异常取消 / 状态提交：不涉及（纯数据载体）。
- 性能模式：轻量值对象，工厂方法无额外开销。
- `__repr__` 截断 graph_id 无越界风险。

---

## src/features/composite/core/runtime_diagnostics.py (行数: 186)

### 发现

- [AUD-5213] [Suggestion] publish_many 逐条加锁，可合并为单次临界区
  - 位置: src/features/composite/core/runtime_diagnostics.py:66-68
  - 证据:
    ```python
    def publish_many(self, records: Iterable[RuntimeDiagnostic]) -> None:
        for record in records:
            self.publish(record)
    ```
  - 影响: N 条记录 N 次 lock 获取/释放；高频渲染诊断批量发布时在 UI 线程与 worker 间放大锁竞争（影响很小）。
  - 建议: 校验后在单次 `with self._lock:` 中 `self._records.extend(valid)`。
  - 置信度: 高（属实，影响轻微）

### 通过项
- 线程安全：`threading.Lock` 保护 deque 的写/快照/清空，`snapshot` 在锁内拷贝为 tuple，锁外只读。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及（模块明示无 Qt/GL/日志副作用）。
- 异常与取消路径：`publish` 对错误类型抛 TypeError（窄化、不吞）；无宽 except。
- 输入验证：非 dict 输入返回空元组；`model_diagnostics`/`model_import_diagnostics` 非 list/tuple/set 时忽略；空串代码跳过；`to_dict` 输出 JSON-safe。
- 性能模式：deque(maxlen) 有界防膨胀；dedup 用 set，顺序稳定。
- 状态提交一致性：无 undo/持久化职责。
- 类型注解：完整。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-5200, AUD-5201, AUD-5202, AUD-5203 |
| Suggestion | 10 | AUD-5204–AUD-5213 |

全部 13 条发现集中在 node_plugin.py（插件加载/注册生命周期）；output_targets/quality_profile/runtime_context/runtime_diagnostics 整体质量良好，无 Critical 级问题。
