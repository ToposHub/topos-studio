# 批次报告：batch-115 — src/features/composite/utils/

- 审查日期：2026-09-28
- 编号区间：AUD-11500–AUD-11599
- 文件数：7（2,770 行）
- 严重度统计：Critical 0 / Major 5 / Suggestion 14

---

## src/features/composite/utils/__init__.py (行数: 1)
### 发现
（无）
### 通过项
- 通过（无逻辑）：文件仅含一行注释 `# Utility functions and helpers`，无代码、无导出。

---

## src/features/composite/utils/expression_parser.py (行数: 1,290)
### 发现
- [AUD-11510] [Major] propagate_change 的求值上下文缺失 time/fps/layer_attrs 等字段，同一表达式与 evaluate_all 结果不一致
  - 位置: src/features/composite/utils/expression_parser.py:1259-1264
  - 证据:
    ```python
    context = ExpressionContext(
        this_params=result[update_node_id],
        node_params=result,
        input_attrs=input_attrs,
        frame=frame
    )

    expression = self._expressions.get(update_key)
    if expression:
        eval_result = self._parser.evaluate(expression, context)
        if eval_result.success and eval_result.value is not None:
            result[update_node_id][update_param_name] = eval_result.value
    ```
    而 evaluate_all（行 1120-1152）传入 time/fps/current_value/value_at_time/loop_in/loop_out/layer_attrs/project_width/height/random_seed。
  - 影响: 走 propagate_change 路径时 `time` 恒为默认 0.0、`layer.*`/`valueAtTime`/`wiggle` 全部退化为默认值——同一帧同一表达式与 evaluate_all 得到不同数值（静默错误结果）；且该路径不做 `math.isfinite` 过滤（行 1159 仅在 evaluate_all 中有），非有限值可直接写入参数，也不更新 `_last_errors`。当前仓库内无调用者（grep 全库仅定义处），属公开 API 的明确缺陷/风险。
  - 建议: propagate_change 复用 evaluate_all 的完整上下文构造（含 time/fps/layer_attrs/random_seed）与 isfinite 过滤、错误记录；或删除该未用路径。
  - 置信度: 高（不一致本身确定；现实影响因无调用者而暂未触发）

- [AUD-11511] [Major] ExpressionManager 全程无锁，依赖图字典在并发修改下迭代可抛 RuntimeError
  - 位置: src/features/composite/utils/expression_parser.py:1188-1196（迭代）；938-983/985-998（修改）
  - 证据:
    ```python
    # _compute_eval_order（渲染求值路径）
    in_degree: Dict[Tuple[str, str], int] = {key: 0 for key in self._expressions}
    ...
    for key, deps in self._dependencies.items():
        for dep_key in deps:
            ...
    # set_expression / remove_expression（编辑路径，UI 侧）
    self._expressions[key] = expression
    self._dependencies[key] = new_deps
    self._dependents[dep_key].add(key)
    ```
  - 影响: node_engine.py:474/840（set_expression，编辑时）与 node_engine.py:1569（evaluate_all，求值路径）共用同一 manager；项目渲染走独立渲染线程/任务队列，若求值与编辑并发，`dict changed size during iteration` 会在渲染路径抛未捕获 RuntimeError，中断整帧求值。`_parse_cached` 的 `pop(next(iter(...)))`（行 243-247）同样可被并发插入打断。
  - 建议: 为 ExpressionManager 增加一把 `threading.Lock` 保护 `_expressions/_dependencies/_dependents` 的读写；或约定所有访问单线程化（经命令队列派发）。
  - 置信度: 中（无锁事实确定；跨线程并发使用未在本次批次内完全确证）

- [AUD-11512] [Suggestion] 除法除零静默返回 +inf（符号丢失、0/0≠nan），DIVISION_BY_ZERO 错误类型经 `/` 不可达
  - 位置: src/features/composite/utils/expression_parser.py:178-180
  - 证据:
    ```python
    ast.Div: lambda a, b: a / b if b != 0 else float('inf'),
    ast.FloorDiv: lambda a, b: a // b if b != 0 else float('inf'),
    ast.Mod: lambda a, b: a % b if b != 0 else 0,
    ```
  - 影响: `parser.evaluate("-1/0")` 返回 success=True、value=inf（符号错）；`0/0` 得 inf 而非 nan；`ExpressionErrorType.DIVISION_BY_ZERO`（行 39/422-429）除 smoothstep 等 lambda 外基本不可达。直接调用 `ExpressionParser.evaluate` 的调用方（timeline_expression/graph_preflight 等均只 validate，但 API 公开）可拿到 inf/nan 而无错误提示；仅 evaluate_all（行 1159）过滤非有限值。
  - 建议: 统一策略——要么让 `/` 抛 ZeroDivisionError 走既有 DIVISION_BY_ZERO 报告，要么在 `evaluate()` 出口统一做 isfinite 检查并返回 TYPE_ERROR。
  - 置信度: 高

- [AUD-11513] [Suggestion] validate() 禁止一元加号，而求值器却实现了 UAdd，行为自相矛盾
  - 位置: src/features/composite/utils/expression_parser.py:742（FORBIDDEN 含 ast.UAdd）与 183（SAFE_OPERATORS 含 UAdd）
  - 证据:
    ```python
    FORBIDDEN = {
        ...
        ast.UAdd: "unary plus",
        ...
    }
    # 而
    ast.UAdd: lambda a: +a,  # Unary plus
    ```
  - 影响: `validate("+this.x")` 报 "Forbidden operation: unary plus"（用户输入被拒），但同一表达式若绕过 validate 直接 evaluate 可正常求值——校验与求值语义不一致；`ast.Subscript` 同样双处定义（求值器行 479 自带防护）。
  - 建议: 从 FORBIDDEN 移除 ast.UAdd（求值器已安全支持），或在两处保持同一策略。
  - 置信度: 高

- [AUD-11514] [Suggestion] evaluate() 每次调用都对缓存 AST 重新 ast.walk 计数（热路径重复开销）
  - 位置: src/features/composite/utils/expression_parser.py:390-399
  - 证据:
    ```python
    tree = self._parse_cached(expression)
    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        return ExpressionResult(...)
    value = self._eval_node(tree.body, context)
    ```
  - 影响: 播放热路径上每帧 × 每表达式都要 O(节点数) 遍历一遍全树，而 AST 已按字符串缓存、节点数不变——纯重复计算（4096 字符内最多 256+ 节点的遍历每帧重复）。
  - 建议: 在 `_ast_cache` 中随 AST 一并缓存节点计数（value 改为 (tree, count) 元组）。
  - 置信度: 高

- [AUD-11515] [Suggestion] _eval_attribute 尾部嵌套属性分支为不可达死代码
  - 位置: src/features/composite/utils/expression_parser.py:641-649
  - 证据:
    ```python
    # Handle NodeName.param (other node's parameters)
    if obj_name in context.node_params:
        node_params = context.node_params[obj_name]
        if attr_name in node_params:
            return float(node_params[attr_name])
        raise KeyError(f"{obj_name}.{attr_name}")
    # Handle nested attributes (e.g., input1.width)   ← 以下分支不可达
    if obj_name not in ('this', 'input', 'comp', 'thisComp'):
        if obj_name in context.node_params:            ← 恒 False
    ```
  - 影响: 行 635 分支命中时必 return/raise，行 643-649 的 `obj_name in context.node_params` 恒为 False，注释宣称的 "nested attributes (e.g., input1.width)" 实际从未实现——阅读者会误以为嵌套属性被支持。
  - 建议: 删除死分支，或真正实现嵌套属性解析（沿 node.value 链递归）。
  - 置信度: 高

- [AUD-11516] [Suggestion] evaluate_all 的 layer_attrs 回退用 node_id[2:] 魔法剥前缀，空字典 falsy 语义混淆
  - 位置: src/features/composite/utils/expression_parser.py:1124-1126
  - 证据:
    ```python
    layer_attrs=(layer_attrs.get(node_id)
                 or layer_attrs.get(node_id[2:], {})
                 if layer_attrs is not None else {}),
    ```
  - 影响: `node_id[2:]` 无条件剥掉前两个字符（无任何约定文档）；且 `get(node_id)` 命中但值为空 dict（falsy）时会错误回退到剥前缀后的另一个节点的图层元数据，`layer.opacity` 等表达式读到别的节点数据。
  - 建议: 显式 `if node_id in layer_attrs` 判断；把前缀剥离规则写成有名字的辅助函数并注明约定。
  - 置信度: 高

### 通过项
- 输入验证：表达式长度上限（4096）、AST 节点数上限（256）、禁节点（Import/Lambda/推导式/下标/星号/海象）与函数白名单齐全；求值器对未知节点类型/函数一律 ValueError，无任意代码执行面。
- 异常与取消路径：evaluate 对 SyntaxError/ZeroDivision/TypeError/KeyError/ValueError/OverflowError 分类完整，complex 结果经 float() 转 TypeError 被捕获。
- 循环依赖：Kahn 拓扑排序 + _last_cyclic_keys 兜底报错（行 1173-1178），不静默使用陈旧值。
- set_expression/remove_expression 的正反向依赖图增删对称，无泄漏边。
- 求值结果 isfinite 过滤（evaluate_all 路径）防止 inf/nan 写入参数。
- 类型注解总体完整（_eval_node 的 node 参数除外，属 AST 节点惯用法）。

---

## src/features/composite/utils/keyframe.py (行数: 45)
### 发现
（无）
### 通过项
- 通过（纯 re-export）：从 src.shared.keyframe 转发 Keyframe/KeyframeTrack/ColorKeyframeTrack/VectorKeyframeTrack/DiscreteKeyframeTrack/InterpolationType 及 interpolate 等全部 17 个符号，`__all__` 与导入一致；已核对 src/shared/keyframe.py 中所有名称真实存在，导入不会失败。
- 顶部标注 DEPRECATED 与迁移去向，符合规范。

---

## src/features/composite/utils/pass_fusion.py (行数: 529)
### 发现
- [AUD-11500] [Major] CONSERVATIVE 尺寸兼容检查把"尺寸未知"当兼容，且唯一生产调用点传入空 cache，检查恒为空转
  - 位置: src/features/composite/utils/pass_fusion.py:234-247、343-347；调用点 src/features/composite/evaluators/base_evaluator.py:619-621
  - 证据:
    ```python
    # _check_size_compatibility
    for node_id in chain_ids:
        size = _get_texture_dimensions(node_id, snapshot, cache)
        if size is not None:
            ...
            elif size != reference_size:
                compatible = False   # 只有"已知且不同"才判不兼容
    # analyze_fusion
    texture_sizes, size_compatible = _check_size_compatibility(
        current_chain_ids, snapshot, cache or {})
    if fusion_mode == FusionMode.CONSERVATIVE and not size_compatible:
    ```
    而 base_evaluator.py:619 `fusion_plan = analyze_fusion(plan, snapshot, fusion_mode=...)` 未传 cache（cache 在其后才创建），`_get_texture_dimensions`（行 216-223）对空 cache 恒返回 None。
  - 影响: FusionMode.CONSERVATIVE 的契约是 "Only fuse when texture sizes match"，实际所有节点尺寸均为 unknown → compatible=True → 保守模式从不因尺寸拒绝融合；一旦链内实际纹理尺寸不一致，融合 pass 按同尺寸假设采样，可能产生错位/拉伸像素（渲染错误风险）。缓解因素：当前可融合类型均为无 roi_expansion 的逐像素颜色节点，尺寸通常一致，故现实中未必触发。
  - 建议: `_check_size_compatibility` 对"尺寸未知"返回 compatible=False（或单独的 unknown 状态在 CONSERVATIVE 下拒绝）；或调用点传入可用的尺寸信息（如各节点 estimated_bbox）。
  - 置信度: 高（逻辑空转确定；实际渲染错误取决于链内尺寸是否可能不一致）

- [AUD-11501] [Suggestion] get_fusion_mode_from_config 对非字符串配置值直接 .lower() 会抛 AttributeError
  - 位置: src/features/composite/utils/pass_fusion.py:488-492
  - 证据:
    ```python
    mode_str = config.get("pass_fusion_mode", "conservative")
    try:
        return FusionMode(mode_str.lower())
    except ValueError:
        return FusionMode.CONSERVATIVE
    ```
  - 影响: 配置值为 int/bool/None 之外的类型（如配置文件被写成数字）时 `mode_str.lower()` 抛 AttributeError 未捕获——当前调用点 base_evaluator.py 已用 `str(mode_val)` 防护，但该函数是公开工具函数，直接复用会崩。
  - 建议: `isinstance(mode_str, str)` 检查或 `str(mode_str).lower()`。
  - 置信度: 高

- [AUD-11502] [Suggestion] _get_texture_dimensions 的 snapshot 参数未使用，注释与实现不符
  - 位置: src/features/composite/utils/pass_fusion.py:209-223
  - 证据:
    ```python
    def _get_texture_dimensions(node_id: str, snapshot: "GraphSnapshot",
                                cache: Dict[str, Dict]) -> Optional[Tuple[int, int]]:
        node_cache = cache.get(node_id, {})
        for port_name, tex_result in node_cache.items():
            if hasattr(tex_result, 'width') and hasattr(tex_result, 'height'):
                return (tex_result.width, tex_result.height)
        # Try to get from snapshot color input dimensions
        return None
    ```
  - 影响: 注释声称会尝试 snapshot 的输入尺寸，实际直接 return None——这也是 AUD-11500 中 unknown 尺寸的另一根源；误导维护者以为有兜底。
  - 建议: 实现 snapshot 兜底（读源节点 bbox/尺寸）或删除参数与注释。
  - 置信度: 高

- [AUD-11503] [Suggestion] current_shader_type 状态被维护但 flush_chain 从不读取（死状态）
  - 位置: src/features/composite/utils/pass_fusion.py:315/316-319/407-423
  - 证据:
    ```python
    current_shader_type: str = "fused"
    def flush_chain():
        nonlocal current_chain_ids, current_chain_types, current_shader_type
        ...
        shader_type = _determine_shader_type(current_chain_types)  # 重新计算
    ```
  - 影响: flush_chain 总是由 types 重算 shader_type，主循环里对 `current_shader_type` 的赋值（行 416/423/374）全部无效，纯冗余状态，徒增阅读成本。
  - 建议: 删除 current_shader_type 变量。
  - 置信度: 高

### 通过项
- 融合条件完整：capability.fusible + action=="effect" + 单输入 + 输出端口≤1 + fan_out≤1 + roi_expansion==0 + _params_fusable 参数级防护（P1-9 注释明确），未实现参数的节点正确回退为不融合。
- 阶段顺序兼容性检查（子序列判定）对未知类型返回 False，capability 扩展的新类型安全落入 unfused。
- capability-fusible 但不在两个着色器类型表中的类型经 `_is_order_compatible` 安全回退为不融合（已核对 base_evaluator 执行路径：链内节点除首节点外跳过、异常回退空 cache，与 plan 一致）。
- 纯 CPU 分析模块，无 GPU 资源/线程/UI 信号问题；类型注解完整；MD5 仅用作缓存键无安全用途。

---

## src/features/composite/utils/path_versioning.py (行数: 100)
### 发现
（无）
### 通过项
- 输入验证：`path or ""` 防 None；lookbehind 边界避免 "av1"/"rev2" 误识别；`bump_version` 下限 0、保留零填充。
- glob 转义正确（`_globmod.escape` 包裹前后缀，仅版本位为通配）；`*` 不跨目录边界。
- stepped_version 的方向过滤 + 最近邻选择逻辑与 Nuke 行为描述一致；latest_version 取升序末位正确。
- 类型注解完整；纯函数无状态，无线程/资源问题。

---

## src/features/composite/utils/perf_monitor.py (行数: 422)
### 发现
- [AUD-11520] [Suggestion] 丢帧计数可被 get_snapshot 提前清零，首帧前的丢帧永久丢失、history 遥测系统性偏低
  - 位置: src/features/composite/utils/perf_monitor.py:249-282
  - 证据:
    ```python
    frame_copy = replace(self._frame) if self._frame is not None else None
    if include_pending_dropped and frame_copy is not None and self._dropped_count > 0:
        frame_copy = replace(frame_copy, dropped_requests=...)
    ...
    def get_snapshot(self) -> PerfSnapshot:
        with self._lock:
            snapshot = self._copy_snapshot_locked(include_pending_dropped=True)
            # Reset dropped count after reading
            self._dropped_count = 0
    ```
  - 影响: (1) `self._frame` 为 None（首帧 record_frame 之前）时 pending drops 不并入快照但仍被清零——丢帧计数无声蒸发；(2) UI 轮询 get_snapshot 会"偷走" drops，随后 record_frame 写入 history 的 PerfFrameStats.dropped_requests 偏低，build_perf_report 的 dropped_requests 汇总低估。
  - 建议: 只有实际并入快照时才清零（`if frame_copy is not None: self._dropped_count = 0`），或 dropped 计数改为单调累计、由读取方求差。
  - 置信度: 高（逻辑确定；影响限于遥测准确性）

- [AUD-11521] [Suggestion] record_3d_stats 保存调用方传入的可变对象引用，未做防御性拷贝
  - 位置: src/features/composite/utils/perf_monitor.py:238-245
  - 证据:
    ```python
    def record_3d_stats(self, stats: "Render3DStats"):
        with self._lock:
            self._render3d = stats
    ```
  - 影响: 调用方（Scene3D 渲染线程）若在 record_frame 之前原地复用/修改该 Render3DStats 对象，期间 UI get_snapshot 拿到的 replace 拷贝反映的是被修改后的值；与 `_copy_snapshot_locked` 对 nodes/frame 均即时拷贝的做法不一致。
  - 建议: `self._render3d = replace(stats)` 入库即拷贝。
  - 置信度: 中（取决于调用方是否复用对象）

### 通过项
- 线程安全：单一 `threading.Lock`，临界区仅 dict/deque 赋值（微秒级），无 GPU/耗时操作持锁，符合模块契约。
- 快照隔离：get_snapshot/get_history 均经 `replace()` 深拷贝分发，UI 与渲染线程数据完全解耦。
- `record_frame` 中对 `_request_timestamp` 的读取虽在锁外，但 begin_frame/record_frame 均由同一渲染线程调用（base_evaluator.py:641/1064），无跨线程竞争。
- history 为有界 deque（maxlen），导出路径 build_perf_report 全 JSON-safe、除零有防护（default/空列表分支）。
- 类型注解完整。

---

## src/features/composite/utils/preset_manager.py (行数: 383)
### 发现
- [AUD-11530] [Major] 预设名未做路径安全验证即拼接文件路径，import_preset 的名字来自外部 JSON，存在路径穿越写/删任意 .json 风险
  - 位置: src/features/composite/utils/preset_manager.py:142、217、251-252、302、353
  - 证据:
    ```python
    # import_preset —— name 取自被导入的外部文件内容
    name = data.get("name", Path(file_path).stem)
    ...
    dest_path = self._presets_dir / f"{name}.json"
    # save_preset / delete_preset / rename / update 同样直接拼接
    file_path = self._presets_dir / f"{name}.json"
    ...
    if file_path.exists():
        file_path.unlink()
    ```
  - 影响: 导入恶意/损坏 JSON（`"name": "../../whatever"`）可在 presets 目录外写任意 `.json` 文件（内容为该 JSON 本身），delete_preset 更是无需名称存在于缓存即可 unlink 目录外任意 `.json`——违反"外部数据须验证"铁律，属任意文件覆盖/删除（限定 .json 后缀）风险。save_preset 的 name 来自 UI 文本输入（node_properties_panel.py:1485 仅 strip），同样未过滤 `/`、`..`、非法文件名字符。
  - 建议: 校验 name 为安全文件名（正则 `^[\w\-. ]+$` 且不得含路径分隔符/`..`），或以 `Path(name).name` 归一化后再拼接。
  - 置信度: 高

- [AUD-11531] [Major] rename_preset 先删旧文件再写新文件，写失败时旧文件已丢失且内存状态分裂
  - 位置: src/features/composite/utils/preset_manager.py:247-268
  - 证据:
    ```python
    preset = self._presets[old_name]
    preset["name"] = new_name          # 先改内存
    ...
    try:
        if old_path.exists():
            old_path.unlink()          # 先删旧文件
        with open(new_path, "w", encoding="utf-8") as f:
            json.dump(preset, f, indent=2, ensure_ascii=False)
        ...
    except IOError as e:
        logger.warning(f"Error renaming preset: {e}")
        return False
    ```
  - 影响: 新路径写失败（磁盘满/权限/非法字符）时旧文件已被删除——预设数据永久丢失（仅剩内存副本且其 name 已被改为 new_name、缓存键仍为 old_name，内存/磁盘状态分裂）；下次 reload 后该预设彻底消失。数据丢失路径明确。
  - 建议: 顺序改为"先写新文件、成功后再删旧文件"；写失败时回滚 `preset["name"] = old_name`。
  - 置信度: 高

- [AUD-11532] [Suggestion] 所有持久化写入非原子（open("w") 先截断），中途崩溃/断电损坏文件
  - 位置: src/features/composite/utils/preset_manager.py:144-145、258-259、304-305、329-330、355-356
  - 证据:
    ```python
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(preset, f, indent=2, ensure_ascii=False)
    ```
  - 影响: 写入过程中崩溃留下半截 JSON，下次 _load_presets 将其作为损坏文件跳过（warning），该预设静默丢失。
  - 建议: 写临时文件 + `os.replace()` 原子落盘。
  - 置信度: 高

- [AUD-11533] [Suggestion] load_preset 返回内部缓存 dict 引用，调用方可意外篡改缓存
  - 位置: src/features/composite/utils/preset_manager.py:155-164
  - 证据:
    ```python
    def load_preset(self, name: str) -> Optional[dict]:
        return self._presets.get(name)
    ```
  - 影响: 调用方（node_editor_actions.load_preset 后续将节点放入图并可能补 id/改参数）对返回 dict 的任何原地修改都会污染内存缓存，绕过 update_preset_description 等受控路径，造成"下次加载内容变了但文件没变"的隐性分叉。
  - 建议: 返回 `copy.deepcopy(preset)`（nodes/connections 嵌套结构需深拷贝）。
  - 置信度: 高

- [AUD-11534] [Suggestion] 默认 presets 目录取自 os.getcwd()，与文档"项目根目录"不符且随启动目录漂移
  - 位置: src/features/composite/utils/preset_manager.py:47-50
  - 证据:
    ```python
    # 默认为项目根目录下的 presets/
    # 尝试从当前工作目录推断
    cwd = Path(os.getcwd())
    self._presets_dir = cwd / "presets"
    ```
  - 影响: 从不同 cwd 启动应用（或打包后 cwd 为 /）时，用户已有预设"消失"、新预设写到无关目录；多处 `PresetManager()` 就地新建（node_editor_actions.py:1345 等）会各自落到当次 cwd。
  - 建议: 默认目录改为应用数据目录（QStandardPaths/AppDataLocation）或显式注入的项目根，去掉 cwd 推断。
  - 置信度: 高

- [AUD-11535] [Suggestion] 加载时同名预设静默覆盖、导入不校验 version 字段
  - 位置: src/features/composite/utils/preset_manager.py:77-78、287-299
  - 证据:
    ```python
    name = data.get("name", file_path.stem)
    self._presets[name] = data        # 后加载的文件覆盖先前的同名预设
    ...
    if not self._validate_preset(data):
        return None
    # data.get("version") 从未被检查
    ```
  - 影响: 两个文件声明相同 "name" 时后者在加载/导入中静默覆盖前者（用户无感知丢预设）；未来 PRESET_VERSION 升级后，旧/新格式预设被无差别导入，无迁移与拒绝路径。
  - 建议: 加载/导入时检测重名并告警（或以文件 stem 为准）；校验 `data.get("version")` 与 PRESET_VERSION 兼容性。
  - 置信度: 高

### 通过项
- 异常处理：JSON 解析/IO 错误均被捕获并记日志（update_preset_description 的 IOError 静默属小瑕疵未单列），无宽泛 except；损坏文件跳过不致崩溃。
- _validate_preset 对 nodes/connections 的类型与必备键有基础校验（内容级 schema 校验缺失见 AUD-11535 相关讨论）。
- list_presets 排序对 Windows 时钟粒度做了确定性处理（时间戳+插入序倒序），注释清晰。
- 无线程/GPU/信号维度（纯 UI 侧文件管理）；类型注解完整。

---

## 汇总
| 文件 | Critical | Major | Suggestion |
|------|----------|-------|------------|
| expression_parser.py | 0 | 2 | 5 |
| pass_fusion.py | 0 | 1 | 3 |
| preset_manager.py | 0 | 2 | 4 |
| perf_monitor.py | 0 | 0 | 2 |
| keyframe.py / path_versioning.py / __init__.py | 0 | 0 | 0 |
| **合计** | **0** | **5** | **14** |
