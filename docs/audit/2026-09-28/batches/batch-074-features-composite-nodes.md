# batch-074 审查报告：src/features/composite/nodes/（P1）

- 任务区间：AUD-7400–AUD-7499
- 文件数：8（2,524 行），全部 Read 通读
- 统计：Critical 0 / Major 4 / Suggestion 13
- 跨模块契约验证：`_dispatch_effect`（base_evaluator.py:1954-2186）确认 `set_alpha`/`sharpen`/`shuffle`/`topos_color`/`transform`/`time_offset`（设计性直通，P0-04）均有分发路径；`time_remap.py:176-202` 确认 `frame_offset` 在源端帧重映射中被消费；`base_evaluator.py:332-336` 确认节点 `cleanup()` 在渲染线程被调用；`base_evaluator.py:989-990` 确认 `_cache_invalidated_needed` 有消费方。

## src/features/composite/nodes/set_alpha_node.py (行数: 105)

### 发现
- [AUD-7400] [Suggestion] mode/value 参数未做白名单与范围校验，非法值静默退化为 constant 模式
  - 位置: src/features/composite/nodes/set_alpha_node.py:89
  - 证据:
    ```python
    mode = self.parameters.get("mode", "constant")
    value = self.parameters.get("value", 1.0)

    return {
        "rgba": TextureResult(
    ```
  - 影响: 旧工程/外部写入的 `mode` 非法字符串（如 "Luma" 大写、拼写错误）不做任何校验直接进入 marker；分发端 `merge_evaluator._apply_set_alpha` 用 `mode_map.get(mode, 0)` 静默回落 constant，用户看到的是"参数改了但画面按常量 alpha 渲染"，无任何提示。`value` 同样未 clamp（可为负/超 1）。
  - 建议: 在 `_do_evaluate` 中按 `ALPHA_MODES` 白名单校验（参照 shuffle_node.py:92-95 的写法），非法值记日志并回落默认；value 用 `_get_param_clamped` 限制到 [0,1]。
  - 置信度: 高

- [AUD-7401] [Suggestion] HELP_TEXT 文档的参数名 `constant_value` 与实际参数 `value` 不一致
  - 位置: src/features/composite/nodes/set_alpha_node.py:34
  - 证据:
    ```python
    参数：
      - mode: 模式（constant/luma/r/g/b/a）
      - constant_value: 常量 alpha 值（当 mode=constant）
    ```
    而 PARAM_DEFS 实际定义（:52）：
    ```python
    ParamDef("value", "float", 1.0, min_val=0.0, max_val=1.0),
    ```
  - 影响: 用户按帮助文档在表达式/脚本里引用 `constant_value` 会取不到值（恒为默认），排错困难。
  - 建议: 统一 HELP_TEXT 为 `value`。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：节点不持有 GL 对象，仅发 effect marker，无泄漏面
- marker 协议：输出占位 TextureResult + `_effect_type/_effect_input/_effect_mask/_effect_params` 符合 base_node 协议；mask 为 None 时分发端 `if effect_mask and effect_mask.is_valid` 安全跳过（已核实）
- bbox/ROI 一致性：compute_output_bbox 直通 rgba 输入，compute_input_roi 对两输入同 ROI，正确
- 无信号/控件交互，无防递归问题；无宽泛 except；类型注解完整

## src/features/composite/nodes/sharpen_node.py (行数: 119)

### 发现
- [AUD-7402] [Suggestion] HELP_TEXT 声称 amount 范围 0.0–2.0，PARAM_DEFS 上限却是 10.0
  - 位置: src/features/composite/nodes/sharpen_node.py:31
  - 证据:
    ```python
    参数：
      - amount: 锐化强度（0.0-2.0）
      - radius: 锐化半径（0.5-10.0）
    ```
    而 PARAM_DEFS（:47-49）：
    ```python
    ParamDef("amount", "float", 1.0, min_val=0.0, max_val=10.0),
    ParamDef("radius", "float", 1.0, min_val=0.0, max_val=50.0),
    ParamDef("threshold", "float", 0.0, min_val=0.0, max_val=10.0),
    ```
  - 影响: 文档/工具提示与 UI 实际可调范围不符（amount 10、radius 50），用户按文档理解参数域会误判。
  - 建议: 同步 HELP_TEXT 与 PARAM_DEFS 的 min/max。
  - 置信度: 高

- [AUD-7403] [Suggestion] radius/amount/threshold 求值路径未做类型与范围校验（含 `_tile_padding` 的 int 转换）
  - 位置: src/features/composite/nodes/sharpen_node.py:86
  - 证据:
    ```python
    radius = params.get("radius", 1.0)
    return max(1, int(radius * 2))
    ```
    以及 `_do_evaluate`（:100-103）直接透传：
    ```python
    amount = self.parameters.get("amount", 1.0)
    radius = self.parameters.get("radius", 1.0)
    threshold = self.parameters.get("threshold", 0.0)
    ```
  - 影响: 外部工程文件写入非数值（如 None/字符串）时，`radius * 2` 在 ROI 规划阶段抛 TypeError 中断渲染计划；数值越界则依赖分发端 `max(0.0, float(...))` 兜底（blur_evaluator.py:383 已做，但节点侧不一致）。
  - 建议: `_tile_padding` 用 `_get_param_clamped("radius", 0.0, 50.0)`；`_do_evaluate` 同样走 clamp helper。
  - 置信度: 中

### 通过项
- GPU 资源生命周期：纯 marker 节点，不持有 GL 对象
- tile padding 声明正确（radius*2 与核 footprint 匹配），且正确委托 `requires_roi_expansion` → 基类 `compute_input_roi` 展开路径
- 分发契约：`_apply_sharpen` 存在且对未知 channels 回落 0（已核实），异常参数不致崩
- 无信号递归、无宽泛 except、类型注解完整

## src/features/composite/nodes/shuffle_node.py (行数: 97)

### 发现
（无）

### 通过项
- 输入验证：四个通道来源均做 `in CHANNEL_SOURCES` 白名单检查并带索引回落（:92-95），防止 `list.index` ValueError 崩溃——本批 8 个文件中唯一做了完整参数白名单的节点
- 恒等直通路径正确：identity shuffle 返回输入本身且不带任何 `_effect_*` marker 键，符合 base_node 协议"No-op 路径只返回真实 TextureResult"
- BYPASS_MAPPING 显式定义 `{"image": "image"}`，属基类文档许可的显式映射方案；i18n_title_key、元数据齐全
- marker 索引值与 `CHANNEL_SOURCES` 顺序一致（注释声明须与 shader switch 索引匹配）
- 无 GPU 资源持有、无信号递归、无宽泛 except、无状态突变；类型注解完整

## src/features/composite/nodes/switch_node.py (行数: 145)

### 发现
- [AUD-7404] [Major] `compute_output_bbox` 未将 `which` 转 int，浮点值时端口键永远失配，bbox 回退到首个有效输入
  - 位置: src/features/composite/nodes/switch_node.py:73
  - 证据:
    ```python
    which = params.get("which", 0)
    key = f"input{which}"

    bbox = input_bboxes.get(key)
    if bbox is None or bbox.is_empty:
    ```
    同文件其余两处均做了 int 归一（:95 与 :128）：
    ```python
    which = int(params.get("which", 0))          # compute_input_roi
    which = self._get_param_int("which", 0)      # _do_evaluate
    ```
  - 影响: `which` 是可动画/可表达式的 int 参数，求值管线解析出的值常为浮点（关键帧插值、表达式结果如 1.0）。此时 `f"input{which}"` 生成 `"input1.0"`，与端口名 `"input1"` 永不匹配，bbox 分析静默回退到首个有效输入的尺寸，而实际求值（`_get_param_int`）选中的是 input1——规划尺寸与渲染内容不一致，可导致 ROI 裁切错误或画幅取错输入。
  - 建议: `compute_output_bbox` 改用 `which = int(float(params.get("which", 0)))` 并 clamp 到 input_count-1，与另外两处对齐。
  - 置信度: 中（代码不一致本身确定；触发需 which 为浮点，动画/表达式场景常见）

- [AUD-7405] [Suggestion] `_update_ports` 重建端口后不重建 `_port_revisions`，端口 revision 跟踪全链失效
  - 位置: src/features/composite/nodes/switch_node.py:48
  - 证据:
    ```python
    def _update_ports(self):
        """Update input ports based on input_count."""
        # Ensure integer count for range() - handles float input from boundary tests
        input_count = int(self.parameters.get("input_count", 2))
        self.inputs = [Port(f"input{i}", "input", "image") for i in range(input_count)]
        self.outputs = [Port("result", "output", "image")]
    ```
  - 影响: 基类 `__init__` 中 `_init_port_revisions()` 在子类赋 `self.outputs` 之前运行（base_node.py:486 与本文件 :46 的顺序），`_port_revisions` 恒为空；switch 动态重建端口后也不刷新。结果 `bump_output_revision()`/`get_port_revision()` 全部空转（node_engine.py:719 等处调用无效）。当前无 `get_port_revision` 消费者，实际影响低，但 revision 契约形同虚设。
  - 建议: `_update_ports` 末尾对新增输出端口调用 `_init_port_revisions()`（根因修复在 base_node 初始化顺序，属后续批次）。
  - 置信度: 高

### 通过项
- `_bypass_evaluate` 按动态端口重写，正确传递"当前选中输入"而非机械首输入；BYPASS_MAPPING=None 的理由有注释说明
- `_do_evaluate` 对 which 做 clamp + 失效输入回退，异常路径（所选输入无效）行为明确
- `set_parameter` 用本地旧值比较触发端口重建，规避基类返回值问题，有注释说明
- 无 GPU 资源持有、无宽泛 except、无状态提交问题；类型注解完整

## src/features/composite/nodes/text_node.py (行数: 1709)

### 发现
- [AUD-7411] [Major] 每次重栅格化重复执行两次完整文本测量布局（`_measure_text_bbox` 被调用两遍）
  - 位置: src/features/composite/nodes/text_node.py:1336
  - 证据:
    `_do_evaluate_with_caption` 已测量一次（:1154）：
    ```python
    bbox_x, bbox_y, bbox_w, bbox_h = self._measure_text_bbox()
    tex_w = max(1, int(round(bbox_w)))
    tex_h = max(1, int(round(bbox_h)))
    ```
    随后 `_render_text_pixels` 内部再测一次（:1336）：
    ```python
    bbox_x, bbox_y, _bbox_w, _bbox_h = self.text_handle_bbox()
    cx = layout_width / 2.0 - bbox_x
    cy = layout_height / 2.0 - bbox_y
    ```
  - 影响: `_measure_text_bbox` 构建整套 QPainterPath/QTextLayout 字形几何（per-char/弧形模式还逐字 transform），是栅格化中最贵的布局步骤之一。开启逐字动画后每帧重栅格（`_content_hash` 含帧号），布局成本直接翻倍；叠加每个图层（fill/outline/shadow）各自的第三次布局，4K 实时预览的 CPU 开销显著超标。
  - 建议: `_do_evaluate_with_caption` 把已测得的 `(bbox_x, bbox_y)` 作为参数传入 `_render_text_pixels`，删除内部的二次 `text_handle_bbox()` 调用。
  - 置信度: 高

- [AUD-7412] [Major] PIL 回退路径将 HDR 颜色直接乘 255，fill>1.0 时颜色分量越界，headless 导出颜色损坏
  - 位置: src/features/composite/nodes/text_node.py:1550
  - 证据:
    ```python
    fill_rgba = (
        int(float(self.parameters.get("fill_r", 1.0)) * 255),
        int(float(self.parameters.get("fill_g", 1.0)) * 255),
        int(float(self.parameters.get("fill_b", 1.0)) * 255),
        int(float(self.parameters.get("fill_a", 1.0)) * 255),
    )
    ```
    而参数域允许 HDR（:170 `ParamDef("fill_r", "float", 1.0, min_val=0.0, max_val=65535.0, ...)`）。
  - 影响: fill_r=4.0 时分量为 1020，超出 Pillow RGBA 的 0–255 字节域；Pillow 的 ink 打包不做 clamp，越界值会溢出污染相邻通道（或依版本抛错）。无 QApplication 的 headless 导出（AI 自动化/CLI）在 HDR 填充色下输出颜色错误，与 Qt 路径的 HDR 结果不一致——违反"HDR 内部数据不得无理由 clamp/损坏"的项目铁律。
  - 建议: PIL 路径对 RGB 先做显示域折算（如 `min(255, int(v*255))` 或按 Qt 路径一致的 sRGB OETF），并在注释中明确该回退为显示域近似；至少 clamp 到 255 保证不互相污染。
  - 置信度: 中

- [AUD-7413] [Suggestion] caption 覆盖直接改写共享 `self.parameters["text"]`，依赖"渲染实例分离 + 单线程求值"的隐式假设
  - 位置: src/features/composite/nodes/text_node.py:1125
  - 证据:
    ```python
    had_text = "text" in self.parameters
    previous_text = self.parameters.get("text")
    self.parameters["text"] = caption_text
    try:
        return self._do_evaluate_with_caption(
            inputs, ctx, fb_manager, resource_pool, render_mode)
    finally:
        if had_text:
            self.parameters["text"] = previous_text
    ```
  - 影响: 已验证当前架构下 text 属源节点、渲染侧实例与 UI 实例分离（node_capabilities.py:1200 + base_evaluator `_sync_render_nodes`），单渲染线程内串行执行，暂无实际竞争。但该模式对未来"求值中途读取 parameters 的诊断/序列化/并行求值"零防御，属于脆弱的临时态突变。
  - 建议: 将 caption 文本作为参数传入 `_do_evaluate_with_caption(text_override=...)`，栅格函数从显式参数取文本，消除对共享 dict 的临时改写。
  - 置信度: 中

- [AUD-7414] [Suggestion] 测量函数 `_measure_text_bbox_pil` 以副作用改写 `font_size` 参数，绕过 set_parameter 状态机
  - 位置: src/features/composite/nodes/text_node.py:1623
  - 证据:
    ```python
    # 自适应字号（与 _clip_to_text_params 一致）
    fs = int(self.parameters.get("font_size", 0))
    if fs <= 0:
        self.parameters["font_size"] = max(12, int(h * 0.055))
    ```
  - 影响: 测量路径直接写 parameters：不触发 `param_revision`/`_dirty`/undo 记录，且与下次 `_sync_render_nodes` 的快照值（0）来回震荡；持久化时若恰好在改写后序列化（headless 实例不会，但同一方法可经 `text_handle_bbox` 在 UI 实例上触达），会把自适应值写进工程文件，破坏"参数只能经 set_parameter 变更"的提交一致性约定。
  - 建议: 自适应字号作为局部变量返回/传参，不落盘 parameters；或经 `set_parameter` 提交。
  - 置信度: 高

- [AUD-7415] [Suggestion] PIL 字体缓存设计：每个新 (family,size,bold) 键都全盘 os.walk 系统字体目录，类级缓存无上限
  - 位置: src/features/composite/nodes/text_node.py:1456
  - 证据:
    ```python
    candidates: list[str] = []
    for font_dir in self._PIL_FONT_SEARCH_PATHS:
        if not os.path.isdir(font_dir):
            continue
        # Walk the directory tree (handles nested font folders).
        for root, _dirs, files in os.walk(font_dir):
    ```
    类级共享缓存（:1423）：
    ```python
    _pil_font_cache: Dict[Tuple[str, int, bool], Any] = {}
    _pil_font_files: list[str] = []
    ```
  - 影响: font_size 参数域 1–2048 且可动画，headless 导出中字号变化时每个新整数尺寸键都触发一次系统字体目录全量遍历（macOS /System/Library/Fonts 树不小）；`_pil_font_cache` 按 (family,size,bold) 无上限累积、跨实例共享永不淘汰，长会话内存单调增长。另 :1526 `orig_font = font` 为死变量。
  - 建议: 缓存"family → 文件路径列表"一次，尺寸变化只调 `ImageFont.truetype(path, size)`；为 `_pil_font_cache` 加简单 LRU 上限；删除死变量。
  - 置信度: 高

- [AUD-7416] [Suggestion] `_build_char_placements` 空字形分支的注释与代码矛盾（空白字符是否消耗 stagger 槽位）
  - 位置: src/features/composite/nodes/text_node.py:962
  - 证据:
    ```python
    if glyph.isEmpty():
        # Whitespace/control char — still consumes stagger slot
        # positioning above, produces no paint.
        if not ch.isspace():
            stagger_i += 1
        continue
    ```
  - 影响: 注释说空白字符"仍消耗 stagger 槽位"，代码却只对非空白的空字形（控制字符类）递增 `stagger_i`，空白不消耗。两种语义都会得到可看的动画，但意图不清会导致后续修 bug 时改错方向（例如打字机效果中空格是否占拍）。
  - 建议: 明确产品语义后统一注释与代码（typewriter 通常空格占拍），二者择一。
  - 置信度: 高

- [AUD-7417] [Suggestion] 空文本/纯空白文本按整画布尺寸分配 RGBA32F 纹理
  - 位置: src/features/composite/nodes/text_node.py:526
  - 证据:
    ```python
    if not text.strip():
        return (0.0, 0.0, float(w), float(h))
    ```
    加上 :1155-1156 直接以 bbox 尺寸建纹理：
    ```python
    tex_w = max(1, int(round(bbox_w)))
    tex_h = max(1, int(round(bbox_h)))
    ```
  - 影响: 清空文字后节点仍持有 1920×1080×4ch×4B ≈ 33MB 的全零 float32 纹理（4K 画布则 ~132MB），多个空文本节点累计可观，纯粹浪费。
  - 建议: 空文本时用 1×1 占位纹理（配合现有 transform 放置逻辑）或在 evaluate 前短路返回透明占位。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：纹理按内容签名释放重建（:1165-1169），`cleanup()` 由 `_sync_render_nodes` 在渲染线程调用（base_evaluator.py:332-336 已核实），`_cache_invalidated_needed` 有真实消费方（base_evaluator.py:989-990 已核实），无泄漏/跨线程 GL 调用
- GLSL·HDR 正确性：fill/outline/shadow 走 numpy Porter-Duff over 合成，RGB 不 clamp（仅 alpha clip 到 [0,1]，合规），纹理 dtype='f4'，输出标注 straight alpha + color_space=SRGB 交由中央 IDT 转线性——HDR 链路正确
- 缓存一致性：`_content_hash` 排除 GPU 侧 transform 参数（拖 gizmo 不重栅），`params_require_frame_cache` 正确声明逐字动画的帧依赖；caption 经临时 text 换入后哈希随内容变化，缓存失效正确
- 帧时间处理：`_update_current_frame` 结合 time_range.comp_start 与异常值回落，正确
- `_over`/`_gaussian_blur`/`_separable_convolve` 数值防护（1e-7 分母、nan_to_num、kernel 上限 64）到位
- Qt 线程使用：QImage/QPainter 离 GUI 线程绘制为 Qt 允许的用法，QApplication 缺失时 PIL 回退，防御完整

## src/features/composite/nodes/time_offset_node.py (行数: 93)

### 发现
（无）

### 通过项
- 通过（设计性直通）：已核实分发端 `time_offset` 显式按 P0-04 直通（base_evaluator.py:2151-2153），帧偏移由 `time_remap.py:176-177` 在源端帧请求时消费 `parameters.get("frame_offset")`——marker/直通/上游重映射三层契约一致，非未接线死代码
- bbox/ROI 直通一致；无 GPU 资源持有；参数读取带默认值；类型注解完整；无信号/异常路径问题

## src/features/composite/nodes/timeline_clip_input_node.py (行数: 163)

### 发现
- [AUD-7407] [Major] 宽泛 `except Exception` 吞掉 provider 全部异常并返回空结果，export 模式下静默丢帧
  - 位置: src/features/composite/nodes/timeline_clip_input_node.py:142
  - 证据:
    ```python
            except Exception as e:
                logger.warning(f"TimelineClipInputNode: provider failed: {e}")

        # Fall back to cached texture only for legacy callers that explicitly
        # pre-seed the node. In timeline runtime mode, returning a cached
        # texture after provider failure can display the previous clip/frame.
        if not has_runtime_provider and self._cached_texture is not None:
    ```
    另有内层静默吞错（:119-121）：
    ```python
                    try:
                        from dataclasses import replace
                        runtime = replace(runtime, input_stage=node_stage)
                    except Exception:
                        pass
    ```
  - 影响: provider 路径上任何异常（GL 错误、数据损坏、编码缺陷）都被压成一条 warning 后返回 `{}`。preview 下符合"无纹理不算错误"的设计，但 `render_mode` 为 full/export 时同样静默，导出成品会出现黑帧/丢帧且渲染任务仍报成功——领域错误被宽泛 except 掩盖，违反铁律。
  - 建议: 至少按 `render_mode` 区分：export/full 下抛出（或上报结构化错误使任务失败），preview 维持降级；内层 replace 失败也应记 debug 日志而非纯 pass。
  - 置信度: 中

- [AUD-7408] [Suggestion] `_cached_texture`/`_revision` 为死状态机：生产代码从不注入非空纹理，`_revision` 无任何消费方
  - 位置: src/features/composite/nodes/timeline_clip_input_node.py:67
  - 证据:
    ```python
    # Cached texture from runtime context
    self._cached_texture: Optional[TextureResult] = None
    self._revision: int = 0
    ```
    及 :73-81：
    ```python
    def set_cached_texture(self, texture: Optional[TextureResult]) -> None:
        """Set cached texture from runtime context. ..."""
        self._cached_texture = texture
        if texture is not None:
            self._revision += 1
        self._dirty = True
    ```
  - 影响: 全库 grep 仅 composite_page.py:9156 调用 `set_cached_texture(None)`（清空），从不注入纹理；`_revision` 递增后无任何读取方。遗留的 legacy 回退路径（:148-149）实为不可达代码，且 to_dict/from_dict 覆写也是纯透传死代码（:154-163），维护成本与误读风险。
  - 建议: 删除 `_cached_texture`/`_revision`/`set_cached_texture` 及透传覆写，或在注释中明确标注 legacy 并给出退役计划。
  - 置信度: 高

- [AUD-7409] [Suggestion] 每次 evaluate 跨 feature 导入私有符号 `_to_texture_result`
  - 位置: src/features/composite/nodes/timeline_clip_input_node.py:127
  - 证据:
    ```python
    # Convert internal _TexResult to TextureResult if needed
    from src.features.edit.core.timeline_clip_frame_provider import _to_texture_result
    tex = _to_texture_result(tex)
    ```
  - 影响: features→features 的下划线私有 API 依赖，且放在每帧执行的热路径内（函数级 import 有查表开销，更重要的是层级耦合）：edit 模块重构该私有函数即静默破坏 composite 输入。
  - 建议: 在 edit 侧把 `_to_texture_result` 提升为公开 API（或在 shared 层提供转换），节点顶层 import 一次。
  - 置信度: 高

### 通过项
- 线程模型：纹理获取经 provider 在渲染线程完成，节点自身不触碰 GL 对象
- ctx 兼容处理：CompositeEvalContext 与裸 moderngl.Context 双路径显式区分，`set_gl_context` 前有 hasattr 防护
- `input_stage` 替换用 dataclasses.replace 保持不可变风格（虽吞错，见 AUD-7407）
- 返回空 dict 而非崩溃的设计在 preview 下正确（模块头注释明确声明）；无信号递归问题

## src/features/composite/nodes/topos_color_node.py (行数: 93)

### 发现
- [AUD-7410] [Suggestion] 类未定义 PARAM_DEFS，`bypass`/`intensity` 等参数无 UI 元数据、校验与动画声明
  - 位置: src/features/composite/nodes/topos_color_node.py:44
  - 证据:
    ```python
    self.parameters = {
        "stack_source": "clip_stack",   # clip_stack | preset | embedded
        "color_stack_id": "",            # For clip_stack mode
        ...
        "bypass": False,
        "intensity": 1.0,
    }
    ```
  - 影响: 本批其余节点均以 PARAM_DEFS 声明参数域；本类只有裸 dict：属性面板无法自动生成/校验这些参数（依赖未知的外部专用面板），`intensity`（0–1 语义）不可声明动画/表达式，外部工程写入非法 `stack_source` 字符串也无白名单拦截（分发端 `_apply_topos_color` 的兜底行为未在本批验证）。另有 `bypass` 参数与基类 `disabled` 双通道并存，语义易混。
  - 建议: 为 stack_source（option: clip_stack/preset/embedded）、intensity（float 0–1，可动画）、bypass（bool）补 PARAM_DEFS；或在类注释中说明由哪个专用面板接管。
  - 置信度: 中

### 通过项
- marker 协议：bypass/intensity≤0 直通路径返回输入本身且不带 `_effect_*` 键，合规；effect marker 含完整参数快照，分发端 `_apply_topos_color` 存在（base_evaluator.py:2080 已核实）
- 无 GPU 资源持有、无信号递归、无宽泛 except；参数默认值集中在 `__init__`，序列化经基类 to_dict 深拷贝，状态提交无问题；`role` 字段的 timeline 同步豁免语义有注释说明
