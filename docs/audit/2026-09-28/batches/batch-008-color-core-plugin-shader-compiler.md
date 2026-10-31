# Batch-008 静态审查报告：color/core 插件与着色器编译模块

- 日期：2026-09-28
- 范围：src/features/color/core/ 下 plugin_manager / preset_meta / project_dependencies / scope_types / shader_compiler / shader_nodes，共 6 文件、2629 行
- 问题编号区间：AUD-0800 – AUD-0843
- 汇总：Critical 0 项；Major 5 项；Suggestion 19 项
- Major 标题索引：
  - [AUD-0800] PluginManager 共享注册表跨线程无锁读写（UI 线程 scan/pop vs 渲染线程 get_plugin）
  - [AUD-0830] comp_cache 键未包含插件内容哈希，插件文件运行期更新后渲染继续使用旧 program
  - [AUD-0831] comp_cache 无容量上限/淘汰策略，结构组合切换无限累积 GL program
  - [AUD-0832] 插件源码名字盲正则改写，@param 名与体内同名标识符冲突可致编译失败或语义破坏
  - [AUD-0840] HSV 转换对负值 HDR 颜色防护不一致，hue_shift/HSL 曲线对负值颜色的处理不可预测
- 备注：审查过程中用只读 grep 核实了外部调用点（menu_toolbar_controller / plugin_panel / async_renderer / effect_catalog / effect_spec / composition_uniforms / model_manager），相关证据在条目中注明。

---

## src/features/color/core/plugin_manager.py (行数: 567)

### 发现

- [AUD-0800] [Major] 共享插件注册表跨线程无锁读写：UI 线程 scan/pop 与渲染线程 get_plugin 并发
  - 位置: src/features/color/core/plugin_manager.py:50, 72-97
  - 证据:
    ```python
    self.plugins: Dict[str, Dict[str, Any]] = {}
    ...
    for filename in os.listdir(abs_directory):
        if filename.endswith('.tglsl'):
            ...
                plugin_data = self._parse_plugin_file(filepath, plugin_id)
                if plugin_data:
                    self.plugins[plugin_id] = plugin_data
    ```
  - 影响: 已核实的跨线程调用点：菜单构建（menu_toolbar_controller.py:307-311，含直接 `get_plugin_manager().plugins.pop(pid, None)`）与 plugin_panel.py:154 在 UI 线程执行 scan/修改；shader_compiler.py:51 的 `get_plugin().get_plugin(plugin_id)` 在渲染线程组合编译时读取。CPython 单条 dict 操作原子，不会崩溃，但渲染线程在 UI 重扫描/剔除期间会读到中间态：插件瞬时缺失 → `plugin_binding_from_instance` 返回 None → 该插件被静默剔除出 composition（仅 warning 日志），或一次组合编译混合新旧注册表状态。
  - 建议: 为 `self.plugins` 提供锁（或以"构建快照后原子替换整个 dict 引用"的方式重扫描）；`reload_plugins`/`scan_plugins` 改为组装局部 dict 后一次性 swap；禁止外部直接 `.pop` 内部字典。
  - 置信度: 高

- [AUD-0801] [Suggestion] _parse_params 数值转换无容错，单行坏参数导致整个插件被丢弃
  - 位置: src/features/color/core/plugin_manager.py:307-321
  - 证据:
    ```python
    if param_type == "slider":
        # slider: default min max
        value_parts = values_str.split()
        if len(value_parts) >= 3:
            param["default"] = float(value_parts[0])
            param["min"] = float(value_parts[1])
            param["max"] = float(value_parts[2])
    ...
    elif param_type == "checkbox":
        # checkbox: default (0 or 1)
        param["default"] = int(float(values_str.split()[0]))
    ```
  - 影响: `@param slider x "L" abc 0 1` 或 checkbox 行缺值（`values_str.split()[0]` IndexError）会从 `_parse_plugin_file` 抛出，被 scan_plugins 的宽泛 except 捕获后整个插件被丢弃，且日志只报文件级异常，无参数级定位——插件作者难以排查是哪一行。
  - 建议: 参数级 try/except（坏参数跳过并 logger.warning 指明 var_name 与原因），避免"一行坏、整插件丢"。
  - 置信度: 高

- [AUD-0802] [Suggestion] select 参数解析失败时产物缺少 default/options 键，下游静默回退索引 0
  - 位置: src/features/color/core/plugin_manager.py:335-344
  - 证据:
    ```python
    elif param_type == "select":
        # select: default "option1" "option2" ...
        select_pattern = r'([\d.]+)\s+(.+)'
        select_match = re.match(select_pattern, values_str)
        if select_match:
            param["default"] = int(float(select_match.group(1)))
            # Extract quoted options
            options = re.findall(r'"([^"]+)"', select_match.group(2))
            param["options"] = options
    ```
  - 影响: `select_match` 不匹配（如默认值不是数字）时，param dict 既无 `default` 也无 `options`。已核实下游 effect_catalog.py:438-444 用 `.get("default", 0)`/`.get("options") or []` 容错，不会崩溃，但选项列表为空、默认静默取 0，插件行为与作者意图不符且无任何日志。
  - 建议: 解析失败时补写 `param["options"] = []` 并记录 warning，或在 schema 层显式标记该参数解析失败。
  - 置信度: 高

- [AUD-0803] [Suggestion] .tglsl 与 .tai 同名（同 basename）时 .tai 静默覆盖 .tglsl 注册项
  - 位置: src/features/color/core/plugin_manager.py:85-93
  - 证据:
    ```python
    # Scan for AI plugins (.tai files)
    for filename in os.listdir(abs_directory):
        if filename.endswith('.tai'):
            filepath = os.path.join(abs_directory, filename)
            plugin_id = os.path.splitext(filename)[0]

            try:
                plugin_data = self._parse_ai_plugin_file(filepath, plugin_id)
                if plugin_data:
                    self.plugins[plugin_id] = plugin_data
    ```
  - 影响: 两个循环写同一个 `self.plugins[plugin_id]`；`foo.tglsl` 与 `foo.tai` 并存时后者无条件覆盖前者，同名 shader 插件无提示消失。
  - 建议: 覆盖前检测冲突并 logger.warning，或 id 命名空间区分（如 `foo` / `foo.tai`）。
  - 置信度: 高

- [AUD-0804] [Suggestion] 预览质量注册发生在参数解析之前，解析失败的插件在质量管理器留下残留注册
  - 位置: src/features/color/core/plugin_manager.py:146-152
  - 证据:
    ```python
    if preview_cost:
        preview_quality_level = self._parse_preview_cost(preview_cost)
        # 注册到效果预览质量管理器
        if preview_quality_level:
            self._register_plugin_preview_quality(plugin_id, name, preview_quality_level)

    # Extract parameters (with i18n support)
    params = self._parse_params(content, plugin_id)
    ```
  - 影响: `_parse_params`（152 行）抛异常时插件整体被丢弃（不进 `self.plugins`），但 149 行已先把它注册进 `get_effect_preview_quality_manager()`——重型效果注册表里残留一个不可用插件的条目，且重复扫描会重复注册。
  - 建议: 把 `_register_plugin_preview_quality` 移到 `_parse_plugin_file` 成功返回前（或 scan 层解析成功后再注册）。
  - 置信度: 高

- [AUD-0805] [Suggestion] scan_plugins 增量合并不清理已删除插件文件的陈旧条目，reload_plugins 全仓无调用方
  - 位置: src/features/color/core/plugin_manager.py:72-97, 509-520
  - 证据:
    ```python
    def reload_plugins(self, directory: str = "plugins") -> Dict[str, Dict[str, Any]]:
        ...
        self.plugins.clear()
        return self.scan_plugins(directory)
    ```
  - 影响: scan_plugins 只增不删：删除 .tglsl 文件后再扫描，旧条目仍留在 `self.plugins` 并继续出现在菜单/可被引用。带清理语义的 `reload_plugins` 在 src/ 下没有任何调用方（grep 核实，composite/node_plugin.py 的同名方法是另一类）；menu_toolbar_controller.py:311 需要手动 `plugins.pop` 剔除禁用插件，佐证该缺口。
  - 建议: scan_plugins 以目录清单为准做差集删除，或把运行期刷新统一走 reload_plugins 语义。
  - 置信度: 高（机制确定；触发依赖"运行期重扫描"场景，该场景已被 menu_toolbar_controller 证实存在）

- [AUD-0806] [Suggestion] 插件目录缺失时 scan 副作用建目录，且 makedirs 失败会未捕获抛出
  - 位置: src/features/color/core/plugin_manager.py:68-70
  - 证据:
    ```python
    if not os.path.exists(abs_directory):
        os.makedirs(abs_directory, exist_ok=True)
        return self.plugins
    ```
  - 影响: 只读语义的"扫描"函数带写文件系统副作用；当配置的 plugin_directory 指向无权限位置时 `os.makedirs` 抛 OSError 逸出到调用方（plugin_panel.py:154 在 UI 路径直接调用，可能打断面板初始化）。此外该分支返回的是旧的 self.plugins 而非空 dict，语义易误读。
  - 建议: 目录缺失仅记日志返回空结果，把目录创建留给显式的初始化/导入流程。
  - 置信度: 高（机制）；影响概率低

### 通过项
- 损坏/非 UTF-8 插件文件在扫描层有 try/except 兜底且记录日志（81-82、94-95），不会让扫描崩溃
- AI 插件必需字段 `@ai_model` 缺失时显式返回 None 并记 info（413-416），不产生半成品注册
- capability 声明键白名单 + 后端缺省值逻辑自洽（182-211），未知键值不注入可执行内容
- i18n 键仅用于查表翻译（I18N.tr），无 eval/动态导入风险
- `@ai_scale`/`@ai_tile_size` 等数字解析异常会被扫描层兜底（不崩溃）
- 项目根路径推导 `parents[4]` 与实际层级一致（core→color→features→src→root）

---

## src/features/color/core/preset_meta.py (行数: 222)

### 发现

- [AUD-0810] [Suggestion] sidecar 写失败完全静默，模块无任何日志
  - 位置: src/features/color/core/preset_meta.py:110-118, 157-165
  - 证据:
    ```python
    def save_preset_meta(topos_path: str, meta: PresetMeta) -> bool:
        """写 {name}.meta.json；失败不抛（静帧抓取不因此中断）。"""
        try:
            ...
        except Exception:
            return False
    ...
    def _write_jpeg(img: np.ndarray, path: str, quality: int) -> bool:
        try:
            ...
        except Exception:
            return False
    ```
  - 影响: "不抛出"是有意设计，但连 debug 级日志都没有（模块未定义 logger）：磁盘满/权限问题导致缩略图、参考帧、meta 全部缺失时，用户与开发者都无可观测信号，问题只能靠肉眼发现。
  - 建议: 保留返回 False 语义，但增加 `logger.debug/warning` 记录失败路径与原因。
  - 置信度: 高

- [AUD-0811] [Suggestion] _to_uint8_bgr 不处理 float16/uint16 输入，JPEG 编码将静默失败
  - 位置: src/features/color/core/preset_meta.py:138-145
  - 证据:
    ```python
    def _to_uint8_bgr(image: np.ndarray) -> np.ndarray:
        if image.dtype in (np.float32, np.float64):
            img = (np.clip(image, 0.0, 1.0) * 255.0).astype(np.uint8)
        else:
            img = image.copy()
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        return img
    ```
  - 影响: float16（项目 HDR 内部格式之一）或 uint16（10-bit 源）输入走 `image.copy()` 原样传给 `cv2.imencode('.jpg', ...)`——JPEG 编码器只接受 8U，返回 ok=False，sidecar 静默缺失。是否触发取决于上游传入的渲染帧 dtype（本批次内无法确认）。
  - 建议: else 分支按 dtype 归一化（uint16 → >>8 或 /257；float16 视同 float32 走 clip*255 路径）。
  - 置信度: 中（机制确定，实际 dtype 未在本批次内核实）

- [AUD-0812] [Suggestion] _resize_max 无异常保护，极端长宽比下 int() 归零使 cv2.error 逸出 write_still_images
  - 位置: src/features/color/core/preset_meta.py:148-154, 178-189
  - 证据:
    ```python
    def _resize_max(img: np.ndarray, max_side: int) -> np.ndarray:
        h, w = img.shape[:2]
        if max(h, w) <= max_side:
            return img
        scale = max_side / max(h, w)
        return cv2.resize(img, (int(w * scale), int(h * scale)),
                          interpolation=cv2.INTER_AREA)
    ```
  - 影响: 极端长宽比（如 1×4000）时 `int(w * scale)` 可为 0，`cv2.resize` 抛 cv2.error；write_still_images 中 `_to_uint8_bgr`/`_resize_max`（178、181、187 行）都在 `_write_jpeg` 的 try 之外，异常会传播给静帧抓取调用方，与模块其余部分的防御风格不一致。
  - 建议: _resize_max 内对目标尺寸做 `max(1, ...)` 下限并 try/except，或把整条写 sidecar 链路纳入统一兜底。
  - 置信度: 高（机制）；触发概率低

### 通过项
- load_preset_meta 容错完整：文件缺失/JSON 损坏/非 dict 均返回 None，from_dict 有 `_META_FIELDS` 字段白名单，恶意/手改 meta.json 不会注入多余字段
- clamp(0.0,1.0) 仅出现在 uint8 JPEG sidecar 生成路径——输出介质本身 SDR，属于合理 clamp，不构成 HDR 管线问题
- 无 OpenGL/线程维度代码（纯函数模块）；无信号递归面
- `_jsonify` 对 ndarray/numpy 标量做了完整序列化转换，避免 json.dump 崩溃

---

## src/features/color/core/project_dependencies.py (行数: 300)

### 发现

- [AUD-0820] [Suggestion] DependencyReport.from_dict 不校验 entries 元素类型，损坏的外部 JSON 会以 AttributeError/ValueError 崩溃
  - 位置: src/features/color/core/project_dependencies.py:133-138, 76-87
  - 证据:
    ```python
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DependencyReport":
        return cls(entries=[
            DependencyEntry.from_dict(e)
            for e in data.get("entries", [])
        ])
    ...
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DependencyEntry":
        return cls(
            kind=str(data.get("kind", "")),
    ```
  - 影响: 模块 docstring 明确报告"可嵌入工程快照或独立 JSON 导出"，即 from_dict 会消费外部数据；若 entries 中混入字符串/数字（手工编辑或截断文件），`data.get` 抛 AttributeError，`int(data.get("size_bytes"))` 抛 ValueError——读取方崩溃而非优雅报错。
  - 建议: from_dict 对非 dict 元素跳过并记日志（或收集解析错误返回），size_bytes 等数值转换包 try。
  - 置信度: 高（机制）；实际触发依赖外部文件质量

- [AUD-0821] [Suggestion] _resolve_path 的存在性检查分支冗余，两个分支返回同一值
  - 位置: src/features/color/core/project_dependencies.py:187-196
  - 证据:
    ```python
    if os.path.isabs(path):
        return path
    if base_dir:
        candidate = os.path.join(base_dir, path)
        if os.path.exists(candidate):
            return candidate
        return candidate  # 保留解析结果供 missing 报告定位
    return path
    ```
  - 影响: `os.path.exists(candidate)` 判断后两个分支都 `return candidate`，存在性检查是死逻辑，误导读者以为缺失时有不同处理；实际缺失定位由 `_entry_for_file` 的 isfile 判断承担。
  - 建议: 删除冗余 exists 判断，直接 `return candidate` 并保留注释。
  - 置信度: 高

- [AUD-0822] [Suggestion] 通过 getattr 访问 AIModelManager 私有 `_registry`，跨模块脆弱耦合
  - 位置: src/features/color/core/project_dependencies.py:260-267
  - 证据:
    ```python
    if model_manager is not None:
        registry = getattr(model_manager, "_registry", None) or \
            getattr(model_manager, "registry", None) or {}
        for model_id, info in registry.items():
            local_path = getattr(info, "local_path", None)
    ```
  - 影响: 已核实 src/shared/ai_models/model_manager.py 的注册表值为 ModelInfo dataclass（含 local_path 属性），当前可用；但依赖下划线私有成员意味着 model_manager 重构（如改 dict 存储）会静默得到空注册表 → AI 模型段整体缺失，报告无声变窄。
  - 建议: 在 AIModelManager 上提供公开的 `iter_models()`/`registry_snapshot()` 接口供这里调用。
  - 置信度: 高（机制）；影响低（当前实现匹配）

### 通过项
- _file_sha256 对 OSError 容错返回空串、_entry_for_file 对 getsize OSError 兜底——文件消失竞态不崩溃
- compare_reports 的哈希对比方向正确（基线 ok→当前 missing、同名不同 hash 均显式报告，不静默替换）
- plugin_manager 参数用 `is not False` 哨兵允许跳过插件段，调用方（menu_toolbar_controller.py:1604）未传 model_manager 时不做无谓模型哈希
- 纯数据/纯函数模块：无 GL、无线程、无信号维度；类型注解完整

---

## src/features/color/core/scope_types.py (行数: 206)

### 发现

（无——未发现达到报告标准的问题。）

### 通过项
- 纯数据模块（frozen dataclass + Literal），无 PyQt/ModernGL/NumPy 依赖，与文档声明一致，可安全跨线程传递（不可变）
- `is_frame_valid_for_demand` 的双 revision 门禁（revision 精确匹配 + source_revision 不回退）逻辑正确，与模块头部描述的防"示波器闪回"语义一致
- ScopeGpuFrame 只携带纹理描述符（id+尺寸），不含像素数组，符合"GPU 资源不跨线程传数据"的约定，且 docstring 明确要求消费方在 GL mutex 窗口内使用
- `mode_needs_*` 谓词与 'split' 模式定义（waveform+vectorscope）自洽
- 无外部输入解析、无文件 I/O、无异常路径

---

## src/features/color/core/shader_compiler.py (行数: 430)

### 发现

- [AUD-0830] [Major] comp_cache 缓存键未包含插件内容哈希，插件文件运行期更新后渲染继续使用旧 program
  - 位置: src/features/color/core/shader_compiler.py:253-258（对照 :84）
  - 证据:
    ```python
    plugin_key = tuple(
        (binding.spec.plugin_id or binding.spec.effect_type,
         binding.enabled, _binding_structural_params(binding))
        for binding in bindings
    ) if bindings else ()
    key = (has_curves, has_warper, has_lut3d, has_soft_clip, grain_enabled, has_qualifier, has_hsl_curve, has_mixer, plugin_key)
    ```
    而 spec 本身携带内容身份：`plugin_content_hash=compute_content_hash((raw_src,)),`（:84）
  - 影响: 已核实运行期存在插件重扫描路径（menu_toolbar_controller.py:307 `get_plugin_manager().scan_plugins(plugin_dir)` 在打开效果菜单时执行；plugin_panel.py:154 亦然）。插件文件被编辑/重导入后 `self.plugins[plugin_id]` 换了 shader_code，但结构键 (plugin_id, enabled, structural) 不变 → `comp_cache` 命中旧编译产物，用户看到的仍是旧效果，直到某个结构参数变更才重编译。项目专门为插件维护了 content_hash（FX-M07）却未用于缓存失效。
  - 建议: plugin_key 中加入 `binding.spec.plugin_content_hash`（或 version），使内容变化天然触发重编译；同时可顺带解决重扫描后新旧源不一致问题。
  - 置信度: 高

- [AUD-0831] [Major] comp_cache 无容量上限与淘汰策略，GL program 随结构组合切换无限累积
  - 位置: src/features/color/core/shader_compiler.py:133-134, 258-260, 403-404
  - 证据:
    ```python
    def __init__(self, ctx):
        self.ctx = ctx
        self.comp_cache = {}
    ...
    if key in self.comp_cache:
        return self.comp_cache[key]
    ...
    try:
        prog = self.ctx.program(vertex_shader=vertex_shader, fragment_shader="\n".join(shader_code))
        self.comp_cache[key] = prog
        return prog
    ```
  - 影响: 键是 9 个布尔/结构维度的笛卡尔积 × 每个插件 select 值组合。长调色会话中反复开关 curves/qualifier/grain/插件与切换 select 会持续生成新 program，cache 只增不减（grep 核实 src/ 内无任何外部清理/淘汰调用）；释放只发生在 `release()`（:136-151），调用点为渲染线程 shutdown（async_renderer.py:5999、6079）与导出桥清理。GLSL program 及其 driver 侧驻留内存随之增长，挤压 4K 长会话的 GPU 预算。
  - 建议: 引入 LRU/容量上限（如按最近使用淘汰并即时 release 被逐出 program——需在渲染线程 GL 上下文内执行），或对"当前不活动结构"的 program 做延迟回收。
  - 置信度: 高（机制确定；增长速率取决于使用模式）

- [AUD-0832] [Major] 插件源码正则改写是名字盲替换：@param 名与插件体内任意同名标识符冲突可致编译失败或语义破坏
  - 位置: src/features/color/core/shader_compiler.py:306-313（对照 :320-323）
  - 证据:
    ```python
    # Replace function name: process( -> process_P{p_idx}(
    code = re.sub(r'\bprocess\s*\(', f'process_P{p_idx}(', code)
    # Replace parameter variable names with namespaced uniforms
    plugin_manifest = binding.uniform_manifest(prefix=f"u_P{p_idx}_")
    for ub in plugin_manifest:
        code = re.sub(
            r'\b' + re.escape(ub.param_name) + r'\b',
            ub.uniform_name, code,
        )
    ```
  - 影响: 改写作用于整个插件源文本。若插件作者把 @param 命名为 `mix`、`texture`、`color`、`uv` 或任何与体内局部变量/内建函数同名的词，所有出现处一律被替换成 `u_P{n}_...`：轻则产生未声明/重复声明标识符编译失败（可被 :320-323 与编译错误诊断捕获），重则函数签名参数（如 `vec3 color`）被一并改名、与 uniform 形成遮蔽——编译可通过但参数传递语义被改写。现有的 `u_*` 残留检查只能拦截 `u_` 前缀标识符，拦不住被破坏的内建调用与局部名。
  - 建议: 改写前对 param_name 做保留字/常见标识符黑名单校验（解析期拒绝并给出明确诊断）；或改为只改写 uniform 声明行与已知引用模式，而非全文 `\b` 替换；至少在 `_parse_params`（plugin_manager）侧即拒绝与 GLSL 内建/宿主 snippet 标识符同名的参数。
  - 置信度: 高（机制）；实际触发取决于插件作者命名（中）

- [AUD-0833] [Suggestion] binding.index 默认 0 且编译入口不校验唯一性，手工构造的 bindings 可产生重复 process_P0
  - 位置: src/features/color/core/shader_compiler.py:234-241, 300-313
  - 证据:
    ```python
    bindings: list[PluginBinding] = list(plugins) if plugins else []
    for binding in bindings:
        if not isinstance(binding, PluginBinding):
            raise TypeError(
                "build_composition_shader 的 plugins 必须是 PluginBinding "
    ```
    （已核实 src/features/shared/effects/effect_spec.py:269 `index: int = 0`）
  - 影响: 入口只校验类型不校验 index；若调用方手工构造 PluginBinding（未经 `plugin_bindings_from_instances` 赋原始位置），多个插件 index 同为默认 0，都会被改写成 `process_P0` → 重复函数定义，编译失败。此外 `plugin_key` 也不含 index，结构键无法区分这类冲突。
  - 建议: build_composition_shader 入口对 `[b.index for b in bindings]` 做唯一性断言，冲突即抛出带指引的 ValueError。
  - 置信度: 高（机制）；当前主路径（from_instances）不受影响

- [AUD-0834] [Suggestion] 编译失败不做负缓存，失败结构在渲染循环中反复重组装、重编译
  - 位置: src/features/color/core/shader_compiler.py:402-424
  - 证据:
    ```python
    try:
        prog = self.ctx.program(vertex_shader=vertex_shader, fragment_shader="\n".join(shader_code))
        self.comp_cache[key] = prog
        return prog
    except Exception as e:
        logger.error(f"Shader Compilation Error: {e}")
        ...
        return None
    ```
  - 影响: 失败结果（None）不进 cache——同一失败键每次到达都重新执行全部正则改写、源码拼接与 driver 编译尝试。正则全文替换对长插件源开销可观，且 driver 编译失败通常伴随日志/诊断上报被逐帧重复触发（record_compile_failure 重复写入）。
  - 建议: 对失败键做带时间戳的负缓存（或标记该 key 为 poisoned，直到结构/插件内容变化），限制重复诊断上报频率。
  - 置信度: 中（机制确定；重复频率取决于渲染循环对 None 结果的重试策略，后者不在本批次内）

- [AUD-0835] [Suggestion] plugin_bindings_from_instances 的非 dict 分支为死代码
  - 位置: src/features/color/core/shader_compiler.py:110-114（对照 :42-45）
  - 证据:
    ```python
    for p_idx, instance in enumerate(instances or []):
        binding = plugin_binding_from_instance(instance)
        if binding is None:
            pid = instance.get("plugin_id") if isinstance(instance, dict) \
                else type(instance).__name__
    ```
    而 `plugin_binding_from_instance`（:42-45）对非 dict 已先抛 TypeError：
    ```python
    if not isinstance(instance, dict):
        raise TypeError(
            f"插件实例必须是 dict，得到 {type(instance).__name__}"
        )
    ```
  - 影响: `else type(instance).__name__` 分支不可达（非 dict 在上游已抛 TypeError 且未被捕获），两段代码对"非 dict 输入"的预期行为互相矛盾，误导维护者以为该函数能容错非 dict。
  - 建议: 统一语义：要么在循环内捕获 TypeError 记 warning 并跳过（使 else 分支可达），要么删除死分支并在 docstring 中明确 TypeError 会传播。
  - 置信度: 高

- [AUD-0836] [Suggestion] 注释与事实不符：u_tex_size 实由 GRADING_SNIPPET 声明，而非 BLUR_SHARPEN_SNIPPET
  - 位置: src/features/color/core/shader_compiler.py:283
  - 证据:
    ```python
    BLUR_SHARPEN_SNIPPET,  # Must be before FILM_GRAIN_SNIPPET (declares u_tex_size)
    ```
    实际声明在 shader_nodes.py:24（GRADING_SNIPPET 首行 `uniform vec2 u_tex_size;`；composition_uniforms.py:71-72 亦注明 "u_tex_size 归 GRADING_SNIPPET"）。
  - 影响: 编译顺序正确性靠 GRADING_SNIPPET 恰好排在前面保证；错误注释会误导后续重排 snippet 顺序的人，一旦有人"按注释"调整顺序将产生未声明标识符编译失败。
  - 建议: 修正注释指向 GRADING_SNIPPET。
  - 置信度: 高

- [AUD-0837] [Suggestion] 类型注解缺失（Suggestion 级，仅记录）
  - 位置: src/features/color/core/shader_compiler.py:132, 207-221, 426-430
  - 证据:
    ```python
    def __init__(self, ctx):
        self.ctx = ctx
    ...
    def build_composition_shader(
        self,
        has_curves=False,
        ...
    def _params_to_rgb(self, hue, sat, bright):
    ```
  - 影响: `__init__`/`build_composition_shader`/`_params_to_rgb` 及 `plugin_binding_from_instance(instance)` 无参数与返回类型注解；build_composition_shader 成功返回 program、失败返回 None 的 Optional 语义未在签名上表达。
  - 建议: 补齐注解（`-> Optional[moderngl.Program]` 等）。
  - 置信度: 高

### 通过项
- program 释放路径完整：release() 按 id 去重后逐一 release 并清空 cache，容错单次失败（:136-151）；grep 核实 async_renderer.py:5999/6079 与 export_denoise_bridge.py:162 均调用
- 编译失败与"插件引用未声明 uniform"均有显式诊断上报（record_compile_failure，含 request_id/layer_id/plugin_ids，不含源码路径），无静默吞错；失败返回 None 由调用方记录，符合 G5
- `u_tex_size`/`u_visible_rect` 属 snippet-owned 但已核实 manifest 同步收录（composition_uniforms.py:255-256），:293 的 host_uniform_names 检查不会误伤引用它们的插件
- 缓存键设计正确区分了"值参数走 uniform（不进键）"与"structural select 进键"，避免拖动滑块触发重编译
- 顶点着色器与 main() 中无缺 `.0` 字面量、无非常量循环、无对 HDR 数据的无理由 clamp
- has_active_mixer_params/has_active_curves_lut 的恒等判定异常方向安全（异常时视为激活，宁可多渲染不漏渲染）

---

## src/features/color/core/shader_nodes.py (行数: 904)

### 发现

- [AUD-0840] [Major] HSV 转换对负值 HDR 颜色的防护不一致：hue_shift 与 HSL 曲线直接对可能为负的 color 做 rgb2hsv
  - 位置: src/features/color/core/shader_nodes.py:168-172, 262-267（对照 MIXER :584-598、WARPER :628-646）
  - 证据:
    ```python
    // 9. Hue Shift
    if (hue_shift != 0.0) {
        vec3 hsv = rgb2hsv(color);
        hsv.x = fract(hsv.x + hue_shift / 360.0);
        color = hsv2rgb(hsv);
    }
    ```
    与 HSL 曲线入口：
    ```python
    vec3 apply_hsl_curves(vec3 color, sampler2D hsl_lut_tex) {
        // Step 1: Convert RGB to HSV (we use HSV for better hue handling)
        vec3 hsv = rgb2hsv(color);
    ```
    而 MIXER/WARPER 对同一问题有显式防护：
    ```python
    vec3 sign_c = sign(color);
    vec3 hsv = rgb2hsv(abs(color));
    ...
    return sign_c * hsv2rgb(hsv);
    ```
  - 影响: 本管线明确保留负值（GRADING 注释"Negative values: linear extension to allow recovery"、CURVES"HDR 高光软性保护"、LUT3D 负数加性保留），且 apply_grading 内 hue_shift 位于 lift/gamma 之后——负值颜色到达 `rgb2hsv` 时，`d / (q.x + e)` 对负 q.x 产生负饱和度、hue 分支选择失真，hue_shift 与 HSL 曲线对超黑（负值）区域的调色结果不可预测（可能产生随机色相翻转伪影）。同类代码（MIXER/WARPER）已示范正确做法，此处属遗漏而非设计取舍。
  - 建议: hue_shift 与 apply_hsl_curves 入口采用与 MIXER 相同的 `sign_c = sign(color); hsv = rgb2hsv(abs(color)); ... return sign_c * hsv2rgb(hsv);` 模式（注意 HSL 曲线的亮度乘子需先作用于 |color| 再还原符号）。
  - 置信度: 中（代码路径确定：负值可达这两处；实际视觉影响幅度未在运行环境验证）

- [AUD-0841] [Suggestion] apply_blur_sharpen 的 USM 迭代循环边界为运行时值，违反项目"for 循环边界必须是常量"约定
  - 位置: src/features/color/core/shader_nodes.py:538, 543
  - 证据:
    ```python
    int iters = (mode == 1) ? clamp(iterations, 1, 10) : 1;
    ...
    vec3 raw_pix = texture(tex, uv).rgb;
    for(int i = 0; i < iters; i++) {
    ```
  - 影响: 桌面 GLSL 330 允许动态循环条件，当前可编译运行；但项目规范（.clinerules/AGENTS：for 循环边界必须是常量，且 P7-07 明确存在 MSL 后端目标）要求常量边界。iters 已被 clamp 到 1-10，具备直接改写为 `const int MAX_ITERS = 10; for(int i = 0; i < MAX_ITERS; i++) { if (i >= iters) break; ... }` 的条件。
  - 建议: 改为常量上界 + 内部 break/条件跳过，兼顾驱动展开优化与移植目标。
  - 置信度: 高（事实）；当前无运行时错误

- [AUD-0842] [Suggestion] FILM_GRAIN_SNIPPET 头部注释错误声称 u_tex_size 由 BLUR_SHARPEN_SNIPPET 声明
  - 位置: src/features/color/core/shader_nodes.py:739-740
  - 证据:
    ```python
    uniform vec4 u_visible_rect; // 视口在原图上的 x, y, width, height (图像空间绝对物理像素坐标)
    // 注意：u_tex_size 由 BLUR_SHARPEN_SNIPPET 声明，这里直接使用
    ```
    实际声明在 GRADING_SNIPPET（本文件 :24 `uniform vec2 u_tex_size;`）；BLUR_SHARPEN_SNIPPET（:361 起）内无该声明。
  - 影响: 纯注释失实（编译顺序上 GRADING_SNIPPET 在最前，当前无害），与 shader_compiler.py:283 的错误注释同源，重排 snippet 时易引发误判。
  - 建议: 注释改为"由 GRADING_SNIPPET 声明"。
  - 置信度: 高

- [AUD-0843] [Suggestion] 亮度系数实为 BT.601 但注释声称 Rec.709，色度学口径不统一
  - 位置: src/features/color/core/shader_nodes.py:266-267（同类用法 :72, :123, :152, :176, :460, :817）
  - 证据:
    ```python
    // Calculate luminance (Rec.709 luma coefficients)
    float luma = dot(color, vec3(0.299, 0.587, 0.114));
    ```
  - 影响: `vec3(0.299, 0.587, 0.114)` 是 BT.601 系数，注释却写 Rec.709（应为 0.2126/0.7152/0.0722）。该 luma 被广泛用于 vibrance、mid_detail 蒙版、饱和度、胶片颗粒响应蒙版、HSL 曲线亮度轴——在 BT.709/HDR 管线中统一使用 601 系数使这些加权与显示色度学不一致（绿红加权偏差显著），且注释误导后续维护。
  - 建议: 统一决策：要么全库换用 Rec.709 系数（需评估视觉回归），要么修正注释并集中定义单一 `LUMA_COEFF` 常量避免各处散抄。
  - 置信度: 高（系数与注释的事实关系确定）；是否构成需修复的视觉问题取决于团队口径

### 通过项
- 浮点字面量规范：全文件抽查（含 COMMON/GRADING/CURVES/BLUR/MIXER/LUT3D/BLEND/QUALIFIER/GRAIN/SOFT_CLIP）未发现缺 `.0` 的整数字面量用作 float（`.1031`、`1e-5` 均为合法 float 字面量）
- 其余循环边界均为常量（bilateral 5×5、bokeh const 28、motion const 32、fast_blur 12、grain fbm 3、toe/shoulder 3、mixer 8、gamma/curves 3）
- HDR 保护总体到位：CURVES 用斜率外推而非 clamp、GRADING gamma 对 >1 线性延拓、shadows 的 clamp 仅用于构造幂曲线并加回残差、LUT3D 用比例缩放保留 >1 高光且负数加性保留、hsv.z 用 max(...,0.0) 而非 clamp 到 1、SOFT_CLIP 无 [0,1] 截断
- 边缘采样 UV 越界依赖纹理 wrap 设置：已核实 async_renderer 创建管线纹理时设置 `repeat_x/y = False`（clamp-to-edge，async_renderer.py:1060-1061、1208-1221 等），blur/mid-detail 核的越界采样按 clamp 语义安全
- 除法均有防护：rgb2hsv 的 e=1.0e-10、blur 系列的 tex_size<=0 早退、blend 的 max(vec3(0.001),...)、toe/shoulder 的 range>0 分支前提（black_point<low / white_point>high）
- 动态注入隔离（插件侧）由 shader_compiler 的 u_P{n}_ 前缀机制承担，本文件 snippet 间无变量名冲突（u_tex_size/u_visible_rect 单点声明，manifest snippet_owned 同步）
- 无 Python 侧状态/信号/线程代码（纯 GLSL 常量字符串模块），不存在信号递归与状态提交维度问题

---

## 批次小结

| 文件 | Critical | Major | Suggestion |
|------|----------|-------|------------|
| plugin_manager.py | 0 | 1 | 6 |
| preset_meta.py | 0 | 0 | 3 |
| project_dependencies.py | 0 | 0 | 3 |
| scope_types.py | 0 | 0 | 0 |
| shader_compiler.py | 0 | 3 | 4 |
| shader_nodes.py | 0 | 1 | 3 |
| 合计 | 0 | 5 | 19 |

优先修复建议：AUD-0830/AUD-0831（shader 缓存正确性与资源增长）与 AUD-0800（插件注册表跨线程）对渲染正确性/稳定性影响最直接；AUD-0832/AUD-0840 属于 HDR/插件正确性风险，建议结合插件作者文档（PLUGIN_DEV_GUIDE）一并约束命名与负值语义。
