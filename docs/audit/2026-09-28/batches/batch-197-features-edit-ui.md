# 批次审查报告：batch-197 [P1] src/features/edit/ui//

- 任务书：docs/audit/2026-09-28/assignments/batch-197.md
- 编号区间：AUD-19700–AUD-19799
- 文件数：5（合计 1,926 行）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失

统计：Critical 1 项，Major 1 项，Suggestion 17 项。

---

## src/features/edit/ui/caption_workspace.py (行数: 567)

### 发现

- [AUD-19700] [Suggestion] Find 命中后 select_clip 被 `_building_ui` 防递归锁抑制，`cue_selected` 不发射，时间线/查看器不会跳转到命中 cue
  - 位置: src/features/edit/ui/caption_workspace.py:502-505（配合 437-446、477-479）
  - 证据:
    ```python
    # Select the first match's row so the editor sees it in the timeline.
    self.select_clip(matches[0].clip_id)
    self._status_label.setText(f"{len(matches)} match(es) for '{needle}'.")
    ```
    ```python
    def select_clip(self, clip_id: str) -> None:
        ...
        self._building_ui = True
        try:
            sm = self._table.selectionModel()
            ...
    ```
    ```python
    def _on_selection_changed(self, *args: Any) -> None:
        if self._building_ui:
            return
    ```
  - 影响: `_on_find` 声称"editor sees it in the timeline"，但程序化选中被 `_building_ui` 锁吞掉（`selectionChanged` 同步触发后早退），控制器收不到 `cue_selected`，时间线/ProgramViewer 不会同步。锁本身是正确的防回环设计（edit_controller.py:9018-9019 有意为之），但 Find 是用户主动动作，也被一并抑制，与面板 docstring"Selection syncs bidirectionally"不完全一致。
  - 建议: 为 Find 路径在 `select_clip` 后显式 emit `cue_selected`（或给 `select_clip` 增加 `notify: bool` 参数，Find 传 True），保持控制器回程同步仍然静默。
  - 置信度: 高

- [AUD-19701] [Suggestion] 未使用的导入：`UpdateCaptionMetadataCommand`、`EditTrack`、`Tuple`
  - 位置: src/features/edit/ui/caption_workspace.py:52-62
  - 证据:
    ```python
    from src.features.edit.core.captions.commands import (
        MergeSubtitleClipsCommand,
        SplitSubtitleClipCommand,
        UpdateCaptionMetadataCommand,
    )
    from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip, TimelineSequence
    ```
  - 影响: `UpdateCaptionMetadataCommand`、`EditTrack` 在文件内零引用（仅 docstring 提及）；`Tuple` 自 `typing` 导入后未用（全文件搜索无引用）。拉起不必要的 import 依赖并误导读者。
  - 建议: 删除三个未用导入（保留 `SubtitleClip`、`TimelineSequence`）。
  - 置信度: 高

- [AUD-19702] [Suggestion] 多选时 `ids[0]` 依赖 `selectedRows()` 返回顺序，"first selected cue" 可能不是模型序最靠前的 cue
  - 位置: src/features/edit/ui/caption_workspace.py:480-488
  - 证据:
    ```python
    ids = self._selected_clip_ids()
    if not ids:
        return
    # Emit the first selected cue (single-cue-driven sync); the controller
    # can extend to multi-cue properties later.
    clip_id = ids[0]
    ```
  - 影响: Qt 的 `selectedRows()` 按选择顺序返回（框选/Ctrl 点选顺序），非行号序；同步到属性面板的 cue 随最后点选顺序漂移，与表格视觉顺序可能不一致。
  - 建议: 取 `min(ids, key=lambda cid: self._model.find_row_by_clip_id(cid))` 或按选中行号排序后取第一行。
  - 置信度: 中

- [AUD-19703] [Suggestion] 取消选择（空选区）时早退，状态栏"N selected"计数不刷新
  - 位置: src/features/edit/ui/caption_workspace.py:480-489
  - 证据:
    ```python
    def _on_selection_changed(self, *args: Any) -> None:
        if self._building_ui:
            return
        ids = self._selected_clip_ids()
        if not ids:
            return
        ...
        self._update_status()
    ```
  - 影响: 用户清空选区后底部状态标签仍显示旧的"selected"数量，与表格实际状态不符。
  - 建议: 空 `ids` 时改为 `self._update_status()` 后 return。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程组件，无 OpenGL/渲染线程交互。
- 信号防递归：`select_clip`/`_select_clip_ids` 均用 `_building_ui` 状态锁包裹程序化选择，`set_sequence` 重建用 begin/endResetModel，模式正确。
- 状态提交一致性：面板为纯视图，split/merge/replace 全部经 `command_requested`/`find_requested` 交控制器统一命令路径（`SplitSubtitleClipCommand.create(clip_id, frame)`/`MergeSubtitleClipsCommand.create(a, b)` 签名已核实与 captions/commands.py 一致；控制器 `_execute_subtitle_command` 捕获异常并显示状态栏消息）。
- 输入验证：空 needle 拒绝、split 帧界检查（`mid <= start or mid >= end`）、merge 恰好两条检查、`find_in_clips` 对 `clip.text` None 容错（`clip.text or ""`）。
- 模型 API：`rowCount/columnCount` 对 parent.isValid() 返回 0、`row_at`/`find_row_by_clip_id` 越界安全；`frame_to_timecode` 对 fps<=0 与 ff 舍入有保护。
- replace 偏移安全：`build_replace_all_text` 从尾部应用 original-offset 匹配，替换文本长度变化不失准。
- GPU 资源生命周期 / GLSL·HDR：不适用（无 GPU/GLSL 代码）。

---

## src/features/edit/ui/chapter_export_dialog.py (行数: 182)

### 发现

- [AUD-19710] [Suggestion] `sequence: Any` / `parent: Optional[Any]` 弱类型，duck-typing 校验全部下沉到 getattr 默认值
  - 位置: src/features/edit/ui/chapter_export_dialog.py:42-46
  - 证据:
    ```python
    def __init__(
        self,
        sequence: Any,
        parent: Optional[Any] = None,
    ) -> None:
    ```
  - 影响: `TimelineSequence` 就在本仓库（timeline_v2.py:2926，`markers`/`fps`/`duration_frames` 均为真实属性），却用 `Any` + `getattr` 兜底；类型错误（如传入错误对象）时静默显示"没有章节标记"而非报错，排障困难。
  - 建议: 类型注解改为 `TimelineSequence`（import 仅用于 typing 可放 TYPE_CHECKING），保留 getattr 兜底用于防御旧数据；`parent` 注解为 `Optional[QWidget]`。
  - 置信度: 高

- [AUD-19711] [Suggestion] 保存路径已选但无章节内容时静默 return，无任何用户反馈
  - 位置: src/features/edit/ui/chapter_export_dialog.py:161-163
  - 证据:
    ```python
        text = self._current_text()
        if not text:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
    ```
  - 影响: 用户选了保存位置后对话框无反应（不保存也不提示），与"复制"路径同样静默；用户可能误以为已保存空文件。
  - 建议: 空 text 时提示"没有章节标记可导出"（或直接在打开保存对话框前检查并提示，避免无效弹窗）。
  - 置信度: 高

### 通过项
- 异常与取消路径：写文件用 `try/except OSError` + `QMessageBox.critical` 精确捕获，无宽泛 except。
- 输入验证：`_fps()` 对 None/非数值/<=0 三重防护回退 24.0；`_markers`/`_duration_frames` 用 getattr 元组/0 兜底；核心格式化（format_youtube_chapters/format_ffmetadata 签名已核实）在无 Qt 的 core 层并已独立单测。
- 信号防递归：`_format_combo` 构造后无程序化变更，`currentIndexChanged → _refresh_preview`（零参 slot 接 int 信号合法）无回环风险。
- 线程安全 / GPU 资源 / GLSL·HDR：不适用（纯展示对话框，无 GL）。
- i18n：文案全部走 `I18N.tr` 且带默认值，`tr(key, default, **kwargs)` 已核实支持 `path=path` 占位。

---

## src/features/edit/ui/clip_audio_effect_dialog_adapter.py (行数: 200)

### 发现

- [AUD-19720] [Major] `from_dict` 失败被宽泛 except 吞掉并静默替换为 `default()`，提交时会把默认值写回真实效果参数
  - 位置: src/features/edit/ui/clip_audio_effect_dialog_adapter.py:127-131
  - 证据:
    ```python
    # dict → Params dataclass（from_dict 对缺失字段用默认值，容错）
    try:
        initial_params = params_cls.from_dict(dict(params_dict))
    except Exception:  # noqa: BLE001 — 防御：用默认参数兜底
        initial_params = params_cls.default()
    ```
  - 影响: `*Params.from_dict` 对缺失键逐字段兜底，但对类型错误（`float("abc")`）和 `__post_init__` 范围校验（如 StripEQParams 要求 bands ±12 dB、low_cut ∈ [20,1000]）会抛 ValueError——持久化数据经旧版本/手改后超出范围即触发。此时对话框显示的是"全默认"参数（EQ 全平直），用户点确定后 `_on_committed` 把默认值整包 `clip_effect_params_committed` 提交，真实参数被静默覆盖（虽可经 MacroCommand 撤销，但用户无从得知参数被替换的原因）。违反 .clinerules"不得宽泛 except 掩盖领域错误"。
  - 建议: 捕获后至少 `logger.warning` 记录 clip_id/effect_id/异常与降级事实；或降级时标记 dialog 为"默认参数模式"并在标题提示；理想方案是 from_dict 逐字段容错（越界值 clamp 到合法域而非整体抛出）。
  - 置信度: 中

- [AUD-19721] [Suggestion] `_on_committed` 的 `to_dict` 失败被静默丢弃，无日志
  - 位置: src/features/edit/ui/clip_audio_effect_dialog_adapter.py:185-191
  - 证据:
    ```python
    try:
        params_dict = params.to_dict() if params is not None else {}
    except Exception:  # noqa: BLE001
        return
    if not params_dict:
        return
    ```
  - 影响: 用户在对话框里的提交（松手/确定）无声丢失，无任何日志或状态提示，排障困难。
  - 建议: except 分支加 `logger.exception`；空 dict 分支可加 debug 日志。
  - 置信度: 高

- [AUD-19722] [Suggestion] 注册表信号名 `getattr(dialog, committed_attr, None)` 未命中时静默不连接
  - 位置: src/features/edit/ui/clip_audio_effect_dialog_adapter.py:154-159
  - 证据:
    ```python
    committed_signal = getattr(dialog, committed_attr, None)
    if committed_signal is not None:
        committed_signal.connect(
            lambda _tid, params, cid=clip_id, eid=effect_id:
                self._on_committed(cid, eid, params)
        )
    ```
  - 影响: 若 mixer 对话框重命名信号（注册表与 dialog 类失配），对话框可正常打开、正常显示，但任何提交都不会转发——纯静默失效，无告警。
  - 建议: `else: logger.warning(f"dialog {dialog_cls.__name__} 缺少信号 {committed_attr}")`。
  - 置信度: 高

- [AUD-19723] [Suggestion] 缓存对话框复用时不刷新参数，外部变更（undo/属性面板）后被旧值覆盖
  - 位置: src/features/edit/ui/clip_audio_effect_dialog_adapter.py:116-125
  - 证据:
    ```python
    cached = self._dialogs.get(key)
    if cached is not None:
        # 复用已打开的窗口：聚焦并返回（不重建，保留用户调整中的状态）
        if cached.isVisible():
            cached.raise_()
            cached.activateWindow()
            return cached
    ```
  - 影响: 按 `(clip_id, effect_id)` 复用打开中的窗口是合理设计，但窗口打开期间效果参数被 undo/redo 或其他面板改动后，窗口内仍是打开时刻的旧值；用户随后提交会把旧值写成新命令。属于已知取舍，建议至少在文档/注释中明确，或提供 `refresh_params()` 通道。
  - 建议: 在注释中声明该语义，或为复用路径增加"参数已外部变更"检测（比较 params_dict 与 dialog 当前值）。
  - 置信度: 中

### 通过项
- 对话框生命周期：`finished` 弹出缓存 + `WA_DeleteOnClose` 确定性销毁，避免悬挂 C++ 引用（注释含 lldb 实测依据）；`close_all` 遍历副本并对已销毁对象捕获 RuntimeError。
- 信号防递归：lambda 用默认参数绑定 `cid/eid/key`，无闭包变量逃逸问题；`dict(params_dict)`/`dict(params_dict)` 两处防御性拷贝，避免与 dialog 共享可变 dict。
- 依赖正确性：注册表 8 个 dialog 构造签名已核实（StripEQDialog 无 instance_id、其余 7 个有；`*Params.default/to_dict/from_dict` 均存在于 timeline_v2.py:1627-1992+）；惰性 import 规避循环依赖。
- 线程安全：全部 UI 线程；无 OpenGL/GPU 代码（不适用维度跳过）。

---

## src/features/edit/ui/clip_transform_handle_controller.py (行数: 533)

### 发现

- [AUD-19730] [Suggestion] 模块 docstring 描述过时：声称操作"normalized TransformParams (position_x/y, scale_x/y, anchor_x/y)"并做归一化↔像素桥接，实际代码直接使用像素空间字段
  - 位置: src/features/edit/ui/clip_transform_handle_controller.py:3-17（对照 151-168）
  - 证据:
    ```python
    Bridge strategy: convert the clip's normalized params to the pixel-space
    params that composite ``compute_transform_handles`` /
    ``compute_transform_delta_params`` expect (translate = position × canvas px,
    pivot = anchor × canvas px), ...
    ```
    而 `_params_dict` 实际为：
    ```python
    return {
        "translate_x": float(t.translate_x),
        ...
        "pivot_x": float(t.pivot_x),
    ```
  - 影响: TransformParams 已迁移为像素空间模型（clip_params.py:68-108，含 pivot_mode 校验），docstring 仍在描述"normalized position/anchor × canvas px"的转换层，误导后续维护者以为存在换算（也让人怀疑 `translate = position × canvas px` 是否还该乘画布尺寸——实际不乘，是对的）。
  - 建议: 重写模块 docstring 为"直接复用像素空间 TransformParams，无桥接换算"。
  - 置信度: 高

- [AUD-19731] [Suggestion] 死代码：`_IDENTITY` 常量与 `Any` 导入均未使用
  - 位置: src/features/edit/ui/clip_transform_handle_controller.py:42、24
  - 证据:
    ```python
    from typing import Any, Optional
    ...
    # Identity transform tuple (matches the renderer's IDENTITY_TRANSFORM_TUPLE).
    _IDENTITY = (0.0, 0.0, 1.0, 1.0, 0.0, 0.5, 0.5)
    ```
  - 影响: `_IDENTITY` 全文件零引用（注释声称匹配渲染器常量，实为遗留）；`Any` 导入后未使用。7 元组的字段顺序还会随 TransformParams 演进腐化，留着比删掉更危险。
  - 建议: 删除 `_IDENTITY` 与 `Any` 导入；若需 identity 基准，引用 `TransformParams()` 本身。
  - 置信度: 高

- [AUD-19732] [Suggestion] `hit_test_body` 捕获全部异常且不记录日志，与同类方法不一致
  - 位置: src/features/edit/ui/clip_transform_handle_controller.py:264-266
  - 证据:
    ```python
            return _point_in_polygon(ndc[0], ndc[1], corners)
        except Exception:
            return False
    ```
  - 影响: 同文件 `_compute_handles`/`hit_test`/`update_drag` 均用 `logger.exception`，唯独 body 命中测试静默吞异常；一旦 `frame_corners` 结构变化导致 TypeError，move 手势无声失效且无线索。
  - 建议: 补 `logger.exception("clip hit_test_body failed")` 或至少 debug 级日志。
  - 置信度: 高

- [AUD-19733] [Suggestion] `update_hover` 直接调用 `self._viewer.update()`，缺少其他路径都有的 RuntimeError 防护
  - 位置: src/features/edit/ui/clip_transform_handle_controller.py:351-357（对照 324-327 的防护写法）
  - 证据:
    ```python
    def update_hover(self, screen_pos: QPointF) -> None:
        if not self.has_clip() or self._active_handle is not None:
            return
        new_hover = self.hit_test(screen_pos)
        if new_hover != self._hover_handle:
            self._hover_handle = new_hover
            self._viewer.update()
    ```
  - 影响: 若 viewer 的 C++ 对象已销毁（关页/析构窗口时鼠标事件仍入队），`update()` 抛 RuntimeError 会传播进 Qt 事件处理；同文件 `update_drag` 对同一调用特意包了 `except (AttributeError, RuntimeError)`，此处遗漏。
  - 建议: 与 324-327 一致，用 try/except (AttributeError, RuntimeError) 包裹。
  - 置信度: 中

- [AUD-19734] [Suggestion] `on_drag_updated` / `on_drag_ended` 回调属性无类型注解（裸 `= None`）
  - 位置: src/features/edit/ui/clip_transform_handle_controller.py:99-103
  - 证据:
    ```python
        # Callbacks (injected by viewer; all optional).
        # on_drag_updated(clip_id, new_transform: TransformParams)
        # on_drag_ended(clip_id, final: TransformParams, original: TransformParams)
        self.on_drag_updated = None
        self.on_drag_ended = None
    ```
  - 影响: 签名约定只存在于注释中，类型检查器无法校验注入方（program_viewer.py:840-841）与调用方的一致性；`update_drag` 里 `self._clip_id` 实际非 None（has_clip 保证）但注解为 Optional，调用点 `self.on_drag_updated(self._clip_id, ...)` 也失去检查。
  - 建议: 注解为 `Optional[Callable[[str, TransformParams], None]]` / `Optional[Callable[[str, TransformParams, TransformParams], None]]`。
  - 置信度: 高

### 通过项
- 坐标系正确性：`_screen_to_image_px` 的 NDC→图像像素（Y 翻转、top-left 原点）与 `screen_pixels_to_ndc`（viewer_handles.py:752-777，先乘 dpr）互为逆运算，`_ndc_to_view_px` 中 dpr 抵消推导正确；`compute_transform_handles`/`compute_transform_delta_params`/`TransformHandlesData`（frame_corners/pivot/rotate 等）关键字参数已逐一核实匹配。
- 状态提交一致性：控制器不改模型，拖拽经回调上报（final + original 供 undo 命令），`end_drag` 用 frozen dataclass 相等性判断是否有变化（TransformParams 为 `@dataclass(frozen=True)`，eq 可用），选择变化时 `_reset_drag` 丢弃在途拖拽。
- 变换约束：`max_scale=5.0` 与 TransformParams `__post_init__` 上限一致；`hit_test` 过滤 "center" 避免与 pivot 手柄争抢的注释与实现一致。
- 线程安全 / GPU：仅 UI 线程；绘制全部走 QPainter 叠加层（paintGL 内 QPainter block），无 OpenGL 调用；无资源泄漏（不持有 ModernGL 对象）。
- 绘制性能：每帧常量级句柄数（10 个），AA 显式开启，QColor/QPen 在绘制方法内局部导入（每帧 dict 查找开销，可接受）。

---

## src/features/edit/ui/comp_clip_settings_dialog.py (行数: 444)

### 发现

- [AUD-19740] [Critical] fps 下拉的 "Custom" 项无输入控件，选中后确定触发 `float("__custom__")` ValueError，在调用方 finished 槽内未捕获导致崩溃
  - 位置: src/features/edit/ui/comp_clip_settings_dialog.py:441（配合 268-270、397-407）
  - 证据:
    ```python
    # get_settings():
    "fps": float(self._fps_combo.currentData() or 24.0),
    ```
    ```python
    # _build_timing_group() —— Custom 是普通可选项：
    self._fps_combo.addItem(
        I18N.tr("comp_clip_settings.custom", "Custom"), _CUSTOM_KEY)
    ```
    ```python
    # _sync_fps_combo() —— 现有值不在预设时自动选中 Custom：
    for i in range(self._fps_combo.count()):
        data = self._fps_combo.itemData(i)
        if data == _CUSTOM_KEY:
            continue
        if data is not None and abs(float(data) - cur_fps) < 0.001:
            self._fps_combo.setCurrentIndex(i)
            return
    self._fps_combo.setCurrentIndex(self._fps_combo.count() - 1)
    ```
  - 影响: Custom 的 userData 是字符串 `"__custom__"`（truthy），`float("__custom__")` 抛 ValueError。两条可达路径：(1) 新建模式用户手动选择 Custom 后点 OK；(2) 编辑模式下现有 comp clip 的 fps 不在 8 个预设内（如 12/15/48/120）时 `_sync_fps_combo` 自动落在 Custom，用户不改任何东西直接点 Apply 即触发。异常发生在 edit_controller.py:5197 的 `finished` 槽内（该槽只有 try/finally 无 except），未处理异常逃出 Qt 槽，PyQt6 默认行为是打印回溯并 qFatal 终止进程——应用程序崩溃、设置无法保存。
  - 建议: (a) 为 Custom 提供真正的 fps DragValue 输入（选中 Custom 时显示），`get_settings` 读取该值；或 (b) 短期移除 fps 下拉的 Custom 项（预设外 fps 走最近值或保留原值），并在 `get_settings` 里对非数值 currentData 显式校验回退并告警，而非直接 float()。
  - 置信度: 高

- [AUD-19741] [Suggestion] `background_color` 初始值未验证长度/类型，畸形数据直接进 `linear_rgba_to_qcolor` 解包
  - 位置: src/features/edit/ui/comp_clip_settings_dialog.py:121-123（配合 309-312）
  - 证据:
    ```python
    self._init_background_color = tuple(
        init.get("background_color", _DEFAULT_BACKGROUND_COLOR)
    )
    ```
    ```python
    self._bg_color_control = ColorPickerControl(
        self._bg_color_value, parent=color_row)
    ```
  - 影响: 传入 3 元组 RGB、字符串或嵌套结构时 `tuple()` 原样通过，`linear_rgba_to_qcolor`（color_picker_control.py:22，要求 `tuple[float, float, float, float]`）在对话框构造期解包失败/取值越界——对话框都打不开。当前调用方（edit_controller `_collect_comp_clip_settings`）为内部采集，风险低，但违反"外部数据须验证"。
  - 建议: 构造时校验 `len == 4` 且各分量可 float、clamp 到 [0,1]，否则回退 `_DEFAULT_BACKGROUND_COLOR` 并 log。
  - 置信度: 中

- [AUD-19742] [Suggestion] 死代码：`_color_to_qcolor`、`_color_swatch_style` 无任何调用；`C` 导入未使用
  - 位置: src/features/edit/ui/comp_clip_settings_dialog.py:24、71-88
  - 证据:
    ```python
    from src.shared.ui.style_sheet import C, DIALOG_BASE_STYLE
    ...
    def _color_to_qcolor(rgba: tuple[float, float, float, float]) -> QColor:
        """将 scene-linear RGBA 转为拾色器使用的 QColor。"""
        return linear_rgba_to_qcolor(rgba)
    ...
    def _color_swatch_style(rgba: ...) -> str:
        """生成色块的 QSS（28x28 方块，与 node_properties_panel 一致）。"""
    ```
  - 影响: 全仓库 grep 确认两个私有助手（含 sRGB 换算注释与 QSS 生成）零引用——迁移到 ColorPickerControl 后的遗留；`C` 颜色常量导入后未用（文件内仅 docstring 提及 "C.* 颜色常量"，实际代码用字面量 "#bbb"）。
  - 建议: 删除两个函数与 `C` 导入，或将标签颜色改回 `C.*` 常量以兑现 docstring。
  - 置信度: 高

- [AUD-19743] [Suggestion] 名称为空时静默拒绝接受（仅 setFocus），无提示信息
  - 位置: src/features/edit/ui/comp_clip_settings_dialog.py:413-419
  - 证据:
    ```python
    def _on_accept(self) -> None:
        """校验并接受。"""
        name = self._name_edit.text().strip()
        if not name:
            self._name_edit.setFocus()
            return
        self.accept()
    ```
  - 影响: 用户点 OK/Apply 无反应也无解释，只能猜测是名称问题。
  - 建议: 用状态标签或 tooltip 提示"名称不能为空"（对话框无状态栏，可复用 hint 标签或 `QMessageBox`）。
  - 置信度: 高

### 通过项
- 信号防递归：宽高联动（`_on_width_changed`/`_on_height_changed`）与预设应用（`_on_resolution_preset_changed`）全部 `blockSignals` 包裹对向控件，`_sync_resolution_combo` 程序化改选中同样 blockSignals，无递归/回环。
- 宽高约束：DragValue 范围 [64, 16384] 与锁比联动 clamp（`max(64, min(16384, ...))`）一致；锁比在锁定瞬间重新捕获比率、预设应用后同步比率，逻辑正确。
- 分辨率 Custom 安全：`get_settings` 的宽高取自 DragValue（数值控件），不取下拉 data，无 fps 同款问题。
- 状态提交一致性：对话框只经 `get_settings()` 返回值与调用方交互（edit_controller 走 create/update_comp_clip 命令路径），自身不改项目状态。
- 线程安全 / GPU 资源 / GLSL·HDR：不适用（模态设置对话框，无 GL；背景色以 scene-linear RGBA 存储并经 linear_rgba_to_qcolor/qcolor_to_linear_rgba 换算，方向正确无盲目 clamp）。
- 控件依赖：`make_link_button(initial_linked, tooltip)`、`DragValue(label, min, max, default, decimals, suffix)`、`ColorPickerControl(rgba, parent)` + `color_changed(QColor)` + `.swatch/.hex_label` 签名均已核实匹配。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-19740 |
| Major | 1 | AUD-19720 |
| Suggestion | 17 | AUD-19700/19701/19702/19703、AUD-19710/19711、AUD-19721/19722/19723、AUD-19730/19731/19732/19733/19734、AUD-19741/19742/19743 |

复核说明：所有跨模块断言（SubtitleClip/TransformParams/Params 类工厂方法、viewer_handles 函数签名、mixer 对话框构造签名、edit_controller 调用点与异常处理）均已 Read/grep 到真实定义处核实；未发现线程安全、GPU 生命周期、GLSL/HDR 正确性类问题（本批次全部为 UI 层文件，无直接 GL 代码）。
