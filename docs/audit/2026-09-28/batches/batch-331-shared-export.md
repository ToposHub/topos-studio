# 批次报告：batch-331 [P2] src/shared/export/

编号区间：AUD-33100–AUD-33199。审查日期：2026-09-28。
文件：`__init__.py`、`batch_queue_store.py`、`encoder.py`、`export_dialog.py`、`export_panel.py`、`export_source.py`（共 6 个，2,760 行，全部 Read 通读）。
跨文件核验：zh_CN/ja_JP/de_DE/ru_RU/es_ES 等 locale JSON、`src/shared/ui/widgets/drag_value.py`、`src/app/controllers/deliver_controller.py`（调用方）、`src/shared/i18n/i18n.py`（tr 回退逻辑）。

---

## src/shared/export/__init__.py (行数: 271)

### 发现

（无）

### 通过项
- 纯 re-export 模块，无逻辑。导出：encoder 基础配置类型（VideoCodec/VideoEncoderConfig/AudioEncoderConfig/ExportConfig 等）、presets 全家（PRORES_PRESETS/get_prores_config 等）、sequence_exporter、ExportFrameProvider/ExportRange、FFmpegEncoderConfig/FFmpegVideoEncoder/GifVideoEncoder、ToposVideoEncoder、DiskRenderCache/RenderCacheStats/SegmentRenderKey，以及 `src/shared/codec/topos_profiles.py` 的中立层重导出。
- `__all__` 与实际 import 逐一核对无遗漏/无拼写不一致；被引用子模块（presets.py、sequence_exporter.py、frame_provider.py、video_encoder.py、gif_encoder.py、topos_encoder.py、render_cache.py）经目录列举确认存在。
- 文档字符串中的用法示例与导出名一致。

---

## src/shared/export/batch_queue_store.py (行数: 195)

### 发现

- [AUD-33115] [Suggestion] `save_batch_queue(settings=None)` 整文件覆写会抹掉已持久化的 settings，与 docstring 声明的“不覆盖”语义矛盾
  - 位置: src/shared/export/batch_queue_store.py:119-137
  - 证据:
    ```python
    settings: 可选的导出设置（format/options/naming 等），恢复时需要复用。
        传入时会一并持久化；为 None 则不覆盖已存的设置。
    ...
    payload: dict[str, Any] = {
        "items": [item.to_dict() for item in items],
    }
    if settings is not None:
        payload["settings"] = settings
    ```
    随后 `os.replace(temp_path, queue_path)`（L150）原子替换整个文件——settings 为 None 时新文件不含 `settings` 键，旧 settings 直接丢失。
  - 影响: 依赖文档语义的调用方（传 None 保设置）会静默丢失恢复所需的导出设置。当前 deliver_controller 的全部调用点（L1322/1505/1547/1558/1899）均显式传 settings，故未实际触发，属潜伏缺陷，定级 Suggestion。
  - 建议: settings 为 None 时先 `load_batch_queue()` 取旧 settings 合并后再写；或修正 docstring 明示“None 会清除 settings”。
  - 置信度: 高（行为与文档矛盾为事实；实际影响低）

- [AUD-33116] [Suggestion] `load_batch_queue` 对 items 元素无类型校验，单个畸形项导致整表返回空
  - 位置: src/shared/export/batch_queue_store.py:173
  - 证据:
    ```python
    items = [BatchQueueItem.from_dict(d) for d in payload.get("items", [])]
    settings = payload.get("settings")
    return items, settings
    except Exception as e:
        logger.error(f"load_batch_queue failed: {e}")
        return [], None
    ```
    `from_dict` 对非 dict 元素（如 JSON 数组里的字符串）执行 `data.get(...)` 抛 AttributeError，落入外层宽泛 except，其余正常项一并被丢弃。
  - 影响: 手工编辑/半损坏的文件使整个可恢复队列（含 done 状态去重信息）无法恢复。
  - 建议: 逐项 try/except，跳过畸形项并 warning 记录；同时校验 items 顶层为 list。
  - 置信度: 中

- [AUD-33117] [Suggestion] 三个入口均 `except Exception` 宽泛捕获，吞掉编程错误与磁盘/权限错误的区分
  - 位置: src/shared/export/batch_queue_store.py:155-157, 176-178, 188-189
  - 证据:
    ```python
    except Exception as e:
        logger.error(f"save_batch_queue failed: {e}")
        return False
    ```
  - 影响: 铁律“不得宽泛 except 掩盖领域错误”。OSError（磁盘满/权限）与代码 bug（TypeError）同路径静默降级，排障困难；持久化失败仅返回 False，调用方易忽略。
  - 建议: 收窄为 `(OSError, json.JSONDecodeError, ValueError, TypeError)` 并分级告警。
  - 置信度: 中（尽力而为的持久化语义可辩护，但违背铁律字面要求）

### 通过项
- 原子写实现正确：mkstemp 临时文件 + flush + fsync + os.replace，finally 中清理残留 temp（成功 replace 后 exists 为 False，无副作用）。
- 模块级 `_file_lock` 保证同进程读写串行；`has_restorable_items` 复用 load，无锁重入。
- 状态机常量（pending/rendering/done/failed）与 RESTORABLE_STATUSES 定义清晰，与 panel/controller 的 STATUS_COLORS 消息面一致。
- 路径解析覆盖 TOPOS_CONFIG_PATH 覆盖与 Darwin/Windows/XDG 三平台；`from __future__ import annotations` 下类型注解完整（含返回 tuple/Optional）。
- `to_dict`/`from_dict` 字段对称，`from_dict` 对缺失字段有默认值（外部 JSON 输入验证的基本面成立）。

---

## src/shared/export/encoder.py (行数: 440)

### 发现

- [AUD-33118] [Suggestion] `quality`（0-100）直接映射 `crf`，超出 x264/x265 合法范围（0-51）
  - 位置: src/shared/export/encoder.py:177-184
  - 证据:
    ```python
    if self.quality is not None:
        if self.codec in ('h264', 'hevc'):
            options['crf'] = str(self.quality)
        elif self.codec == 'vp9':
            options['crf'] = str(self.quality)
    ```
    docstring 声明 `quality: Quality setting for configurable codecs (0-100)`；libx264/libx265 的 crf 上限为 51，VP9 为 63。
  - 影响: quality>51 时生成非法 FFmpeg 选项（编码失败或被 FFmpeg 拒绝）。已核实 `get_ffmpeg_options` 当前无生产调用方（仅 `__init__.py` docstring 示例），属潜伏缺陷。
  - 建议: 线性映射（如 quality*51/100）或 clamp 到 codec 合法域。
  - 置信度: 高（映射事实确凿；当前无调用方故影响降级）

- [AUD-33119] [Suggestion] `ExportConfig` 未冻结且 `framerate: Optional[Any]` 弱类型；`get_total_frames` 无顺序校验可返回负数
  - 位置: src/shared/export/encoder.py:404-440
  - 证据:
    ```python
    @dataclass
    class ExportConfig:
        ...
        framerate: Optional[Any] = None  # Rational from media module
    ...
    end = self.end_frame if self.end_frame >= 0 else default_total - 1
    return end - self.start_frame + 1
    ```
  - 影响: 与文件自述设计原则 1"Immutable configuration: frozen=True for thread safety"（L16）不一致；start_frame > end 时返回负帧数，下游进度/循环异常。
  - 建议: frozen=True + `framerate: Optional["Rational"]`（TYPE_CHECKING 导入）；get_total_frames 加 max(0, ...) 与参数校验。
  - 置信度: 高

### 通过项
- VideoEncoderConfig/AudioEncoderConfig 均 frozen=True，跨线程共享安全；类型注解完整。
- `get_ffmpeg_codec_name` 对 dnxhr→dnxhd 的 FFmpeg 实情有注释说明；HWACCEL_ENCODER_NAME_MAP 对 'h265'/'hevc' 双拼写对称处理（注释 L294-296 与映射表一致），避免一侧硬编一侧静默回退。
- `hwaccel_encoder_name` 对未知 hwaccel 显式抛 ValueError（不静默换名），空/none 返回 None 语义清晰；`probe_hwaccel_encoder_available` 缓存并明确“注册存在 ≠ 驱动可用”，与注释声明的 AMF 探针先例策略一致。
- `get_ffmpeg_options` 中 bitrate/quality 的 falsy 判断（0 视为未设置）在该值域下合理；extra_options 逐项 str 化防注入非法类型。
- 无 UI/GPU/信号代码，线程与 GLSL/HDR 维度不适用。

---

## src/shared/export/export_dialog.py (行数: 280)

（文件头标记 DORMANT(2026-08-19)：已实现有测试、无生产入口。以下发现按代码本身后果定级，实际曝光风险受休眠状态限制。）

### 发现

- [AUD-33110] [Major] `get_settings` 解析标签文本用英文 "Result: " 前缀，而标签写入用 `I18N.tr("export.result")`（zh="结果"），非英文 locale 勾选 resize 后读取设置即抛 ValueError
  - 位置: src/shared/export/export_dialog.py:273-274（写入侧为 89/206/247）
  - 证据:
    ```python
    # 写入侧（L247）：
    self.output_size_label.setText(f"{I18N.tr('export.result', '结果')}: {new_w} x {new_h}")
    # 解析侧（L273-274）：
    txt = self.output_size_label.text().replace("Result: ", "").split(" x ")
    settings["options"]["resize"] = (int(txt[0]), int(txt[1]))
    ```
    已核对 locales：`export.result` 在 zh_CN="结果"、ja_JP="結果"、de_DE="Ergebnis"、ru_RU="Результат"、es_ES="Resultado"，`replace("Result: ", "")` 均不命中。
  - 影响: 全部 8 个非英文 locale 下，勾选“调整图像大小”后调用 get_settings → `int("结果: 1920")` ValueError；该调用发生在导出按钮流程中，未处理异常在 Qt 槽路径可触发 qFatal 中止（PyQt6 默认行为）。同型缺陷在 export_panel.py 已通过硬编码英文双侧一致规避、在格式映射处已用反向映射修复（见 AUD-33112），本文件未同步。
  - 建议: resize 尺寸存为成员属性而非解析显示文本；双端字符串若必须解析则同源（同一常量）。
  - 置信度: 高

- [AUD-33111] [Major] `update_output_size` 先读 `val` 再 `setRange` 触发 QSpinBox 重入 `valueChanged`，标签最终用陈旧值计算，与控件实际值不符
  - 位置: src/shared/export/export_dialog.py:214-221（连接于 81/87）
  - 证据:
    ```python
    mode = self.resize_mode.currentText()
    val = self.resize_value.value()          # L215：读到 2048
    ...
    if mode == I18N.tr("export.percentage", "百分比"):
        self.resize_value.setSuffix(" %")
        self.resize_value.setRange(1, 500)   # clamp 2048→500，重入 valueChanged→update_output_size
        new_w = int(self.orig_w * val / 100) # L221：仍用陈旧 val=2048
    ```
  - 影响: 从 px 模式（2048）切到百分比：spin 显示 500%，重入调用先把标签按 500% 刷新，外层继续用 2048 覆盖——标签与 get_settings 解析出的 resize 约 4 倍偏差，导出尺寸错误。同时违反铁律“程序化更新控件须状态锁/防递归信号”（未用 blockSignals）。
  - 建议: setRange/setSuffix 前后 blockSignals，clamp 后重读 value 再计算。
  - 置信度: 高（QSpinBox.setRange 收紧会 clamp 并发 valueChanged 为 Qt 文档化行为）

- [AUD-33112] [Suggestion] `fmt_map`/格式判断用硬编码英文键匹配 combo 的 I18N 显示文本，仅因各 locale 均未翻译格式名而侥幸未爆
  - 位置: src/shared/export/export_dialog.py:259-260, 276-278（combo 项为 107-113 的 `I18N.tr("export.fmt_*")`）
  - 证据:
    ```python
    fmt_map = {"JPEG": ".jpg", "PNG": ".png", "OpenEXR": ".exr", "TIFF": ".tif", "WebP": ".webp"}
    ext = fmt_map.get(self.format_combo.currentText(), ".jpg")
    ...
    if settings["format"] == "OpenEXR":
    ```
    已核对 8 个 locale 的 fmt_jpeg/fmt_exr/fmt_webp 均保留英文名（JPEG/OpenEXR/WebP），故当前恰好匹配。
  - 影响: 任一 locale 翻译格式名（或默认值调整）即：扩展名恒回落 .jpg、EXR 精度/压缩选项丢失。export_panel.py:1326-1333 已加 display→name 反向映射修复，本文件未同步。
  - 建议: 复制 panel 的 fmt_map_display_to_name 反向映射（或用 itemData 存内部名）。
  - 置信度: 高（脆弱性事实确凿；当前无实际错配）

- [AUD-33113] [Suggestion] `update_output_size` 只防护 `orig_w == 0`，`orig_h == 0` 时 height/short_edge 分支除零
  - 位置: src/shared/export/export_dialog.py:210-211, 229-231, 244-245
  - 证据:
    ```python
    if not self.resize_check.isChecked() or self.orig_w == 0:
        return
    ...
    new_h = val
    new_w = int(self.orig_w * (val / self.orig_h))   # orig_h==0 → ZeroDivisionError
    ```
  - 影响: 构造参数 original_size=(w, 0) 或仅设置宽度时，“高度/短边”模式除零崩溃（export_panel.py:1274/1296 同型，已并入本条覆盖）。
  - 建议: 防护条件改为 `self.orig_w <= 0 or self.orig_h <= 0`。
  - 置信度: 中（触发依赖异常的 original_size 输入；默认 (0,0) 会被 orig_w==0 拦截）

- [AUD-33114] [Suggestion] ExportDialog 全类方法缺类型注解
  - 位置: src/shared/export/export_dialog.py:13-280（setup_ui/get_settings/update_output_size 等均无参数与返回注解）
  - 证据:
    ```python
    def get_settings(self):
        fmt_map = {"JPEG": ".jpg", ...}
    ```
  - 影响: 违反项目强类型约束；返回 dict 结构只能靠读实现。
  - 建议: 补全参数/返回类型（settings dict 可用 TypedDict）。
  - 置信度: 高

### 通过项
- DORMANT 标记与说明明确（无生产入口，风险受控）。
- 无 GPU/OpenGL 触碰、无跨线程 UI；对话框仅在 UI 线程构造与使用。
- `on_format_changed` 的显隐切换用 `options_layout.setRowVisible`，比较用同键 `I18N.tr("export.fmt_exr")` 双侧一致，本身在任意 locale 下成立。
- resize 模式比较（update_output_size 内）均为同键 tr 双侧比较，locale 无关正确。

---

## src/shared/export/export_panel.py (行数: 1541)

### 发现

- [AUD-33100] [Critical] 自定义命名模式输入非法 format 串触发未捕获 ValueError，Pattern 输入框每敲一键即调用，键入 `{` 即崩
  - 位置: src/shared/export/export_panel.py:49-53（generate_export_filename）；触发链 896 → 1444-1469（update_naming_preview）
  - 证据:
    ```python
    elif mode == "Custom Pattern":
        try:
            return custom_pattern.format(name=original_name, n=index, ext=output_ext)
        except (KeyError, IndexError):
            return f"{original_name}_{index:0{seq_digits}d}{output_ext}"
    ```
    且 L896：`self.custom_pattern_input.textChanged.connect(self.update_naming_preview)`；L1461-1467 直接以输入框原文调用本函数。实测 `'{}'.format(name='a', n=1, ext='.jpg')` 抛 `ValueError: Single '{' encountered in format string`；`'{n:0dd}'` 抛 `ValueError: Invalid format specifier`——均不在捕获列表。
  - 影响: 用户在 Pattern 框键入 `{`、`}` 或非法格式说明符（输入过程几乎必然经过中间态）→ textChanged 槽内未处理 ValueError → PyQt6 槽内未处理异常默认 qFatal 中止进程，应用整体崩溃且可稳定复现；导出按钮路径（get_settings L1354、_start_batch_export）同样崩溃。本面板为生产件（deliver_controller 深度接线）。
  - 建议: 捕获列表扩为 `(KeyError, IndexError, ValueError)`；或在 update_naming_preview 入口预检 pattern。
  - 置信度: 高（format 行为已实测复核）

- [AUD-33101] [Major] 命名模式以本地化显示文本与硬编码英文比较/传参，非英文 locale 下命名功能整体失效（含默认模式的后缀行）
  - 位置: src/shared/export/export_panel.py:1210-1213（_on_naming_mode_changed）；1354 与 1417（把 currentText 传入 generate_export_filename，其内部 L35-55 全为英文比较）
  - 证据:
    ```python
    show_prefix = mode in ["Prefix + Filename", "Prefix + Sequence"]
    show_suffix = mode == "Filename + Suffix"
    show_seq_digits = mode == "Prefix + Sequence"
    show_custom = mode == "Custom Pattern"
    ```
    而 combo 项为 `I18N.tr("export.mode_prefix_filename")` 等（L831-837）；已核对 zh_CN.json：mode_filename_suffix="文件名 + 后缀"、mode_prefix_filename="前缀 + 文件名" 等，8 个 locale 均有非英译文。
  - 影响: 非英文 locale 下四个 show_* 恒 False——连默认“文件名 + 后缀”模式的后缀输入行都不显示；generate_export_filename 回落 else 分支返回 `原始名+扩展名`，用户选择的命名模式被静默忽略（导出文件名错误）。同文件格式选择处已用 fmt_map_display_to_name 反向映射修复（L1326-1333、1407-1414、1450-1457），命名处遗漏，修复模式不一致本身就是佐证。
  - 建议: combo addItem 时以 itemData 存内部模式名（或用 currentIndex 判别），所有逻辑分支用内部名，显示文本只做展示。
  - 置信度: 高（译文已核对）

- [AUD-33102] [Major] `update_output_size` 先读值后 setRange 引发重入 valueChanged，标签以陈旧值计算，导出尺寸与控件实际值偏差约 4 倍
  - 位置: src/shared/export/export_panel.py:1279-1290（连接于 949/962）
  - 证据:
    ```python
    val = int(round(self.resize_value.value()))      # L1279：读到 2048
    if mode == I18N.tr("export.resize_percent"):
        self.resize_value._suffix = " %"
        self.resize_value.setRange(1, 500)           # L1285：clamp 2048→500 并 emit valueChanged
        new_w = int(self.orig_w * val / 100)         # L1286：仍用陈旧 val=2048
    ```
    已核 drag_value.py:206-209：`setRange` 调 `setValue(self._value)`，clamp 变化后 emit valueChanged（L190-197）→ 重入本方法；重入调用以 500 刷新标签后返回，外层继续用 2048 覆盖标签（L1314）。
  - 影响: px 值 2048 切“百分比”：spin 显示 500%，"Result" 标签与 get_settings 解析出的 resize 却按 2048%（≈20.5 倍）计算——导出图片实际尺寸错误（渲染输出错误级缺陷）；且违反铁律“程序化更新控件须状态锁防递归信号”（此路径未用 blockSignals）。
  - 建议: setRange/改 suffix 前后 blockSignals（DragValue 已提供自定义 blockSignals，L199-202），或 clamp 后重读 value 再计算。
  - 置信度: 高（DragValue.emit 路径已读源码确认）

- [AUD-33103] [Major] resize 尺寸以解析显示标签文本为数据源，orig_w==0 时导出 resize=(0,0)，且设计脆弱（对话框版已实际爆雷，见 AUD-33110）
  - 位置: src/shared/export/export_panel.py:1375-1377 与 1434-1436（写入侧 1269/1314，early-return 1274）
  - 证据:
    ```python
    if self.resize_check.isChecked():
        txt = self.output_size_label.text().replace("Result: ", "").split(" x ")
        settings["options"]["resize"] = (int(txt[0]), int(txt[1]))
    ```
    而 `update_output_size` 在 `self.orig_w == 0` 时直接 return（L1274），标签停留在初始化的 "Result: 0 x 0"（L1269 由 update_resize_ui 硬编码写入）。
  - 影响: (1) 仅拖入文件而未打开图像（set_original_size 未被调用）时勾选缩放导出 → resize=(0,0) 传给下游，0 尺寸输出/下游除零风险，且无输入验证拦截；(2) 以显示文本为唯一数据载体，本地化或文案改动即静默解析错（export_dialog.py 同型代码已在非英 locale 崩溃，证明该模式真实危险）。
  - 建议: 尺寸存成员属性（如 self._last_output_size），get_settings 读属性并校验 >0，禁用导出或提示。
  - 置信度: 高

- [AUD-33104] [Suggestion] 直接操纵 DragValue 私有成员 `_suffix`/`_update_display()`（含 reset 路径），而公共 `setSuffix()` 存在且行为等价
  - 位置: src/shared/export/export_panel.py:1184-1187, 1283-1289, 1312-1313
  - 证据:
    ```python
    self.resize_value._suffix = " px"
    self.resize_value.setRange(1, 10000)
    self.resize_value.setValue(2048)
    self.resize_value._update_display()
    ```
    已核 drag_value.py:232-235 提供公共 `setSuffix(suffix)`（置 _suffix 并 _update_display）。
  - 影响: 破坏封装，DragValue 内部重构（改名/重设显示机制）即三处齐断；私有 API 无兼容承诺。
  - 建议: 全部改用 `setSuffix()`；reset 路径同步使用。
  - 置信度: 高

- [AUD-33105] [Suggestion] `_source_label`/`_timeline_label` 全文件从未创建，set_export_source/set_timeline_id 的 UI 更新分支是死代码
  - 位置: src/shared/export/export_panel.py:661, 680
  - 证据:
    ```python
    source_label = self.__dict__.get("_source_label")
    if source_label is not None:
    ...
    timeline_label = self.__dict__.get("_timeline_label")
    ```
    setup_ui 全流程无任何 `_source_label`/`_timeline_label` 赋值（已 grep 全文件核对）。
  - 影响: P9.3 来源显示永远不更新，误导维护者以为有 UI 反馈；`__dict__.get` 也绕过了正常的属性查找惯例。
  - 建议: 删除死分支，或真正创建并维护这两个标签。
  - 置信度: 高

- [AUD-33106] [Suggestion] 多处 UI 文案绕过 i18n 硬编码英文，与初始化的 i18n 文本混排
  - 位置: src/shared/export/export_panel.py:1250, 1269/1314, 1469, 1092, 1237-1241, 1228
  - 证据:
    ```python
    self.file_count_label.setText(f"{count} files")          # 初始化却是 I18N "0 个文件"（L733）
    self.output_size_label.setText(f"Result: {new_w} x {new_h}")
    self.naming_preview_label.setText(f"Preview: {preview}") # 初始化是 I18N（L902）
    self.cancel_btn = QPushButton("Cancel Export")
    remove_action = menu.addAction("Remove Selected")
    ```
  - 影响: 中文界面出现英文混排；其中 "Result: " 前缀同时是 get_settings 反向解析的锚点（与 AUD-33103 同根，本地化它就会重演 AUD-33110 的崩溃，当前靠“不敢翻译”维持正确）。
  - 建议: 统一 `I18N.tr` + kwargs 占位；Result/Preview 前缀与解析逻辑一并按 AUD-33103 方案解耦。
  - 置信度: 高

- [AUD-33107] [Suggestion] `set_auto_file` 移除旧 auto 项后若新路径已是手动项则早退，不发 files_changed
  - 位置: src/shared/export/export_panel.py:503-509
  - 证据:
    ```python
    for i in range(self.count() - 1, -1, -1):
        if self.item(i).data(self.AUTO_ROLE):
            self.takeItem(i)
    # If file already exists as manual item, don't add duplicate
    for i in range(self.count()):
        if self.item(i).data(Qt.ItemDataRole.UserRole) == file_path:
            return          # 列表已变化但不 emit
    ```
  - 影响: 文件计数标签（file_count_label）与列表态短暂失真（按钮态由 add_current_file 随后手动 _update_export_button 部分缓解，计数标签无人刷新）。
  - 建议: 早退前判断本次是否发生 takeItem，发生则 emit。
  - 置信度: 高

- [AUD-33108] [Suggestion] 拖放/添加不校验文件类型，任意文件可入导出队列（外部输入未验证）
  - 位置: src/shared/export/export_panel.py:470-474, 478-483
  - 证据:
    ```python
    for url in event.mimeData().urls():
        file_path = url.toLocalFile()
        if os.path.isfile(file_path):
            self.add_file(file_path)
    ```
  - 影响: .txt/.pdf 等任意文件可入队，错误推迟到渲染期才以 failed 项暴露，浪费整轮批量时间；铁律“外部数据须验证”。
  - 建议: 按支持的媒体扩展名白名单过滤并提示。
  - 置信度: 中（下游 controller 会把失败项标 failed 兜底，非崩溃）

- [AUD-33109] [Suggestion] `_start_batch_export` 输出目录无效时静默 return，用户点击导出无任何反馈
  - 位置: src/shared/export/export_panel.py:1401-1403
  - 证据:
    ```python
    export_dir = self.path_input.text()
    if not export_dir or not os.path.isdir(export_dir):
        return
    ```
  - 影响: 用户点击“批量导出”后界面毫无响应，看似按钮失灵。
  - 建议: 状态栏/toast 提示无效目录并高亮 path_input。
  - 置信度: 高

### 通过项
- 信号防递归（质量控件）：quality_slider↔quality_val_label 双向同步均用 blockSignals 包裹（L1013-1023），set_default_export_settings/_reset_format_section 亦用 blockSignals 正确包裹（L1496-1498, 1198-1200）——该维度在质量路径无问题（resize 路径问题单列 AUD-33102）。
- 线程安全：set_item_status/set_batch_progress/set_batch_paused 等 UI 回调经核由 deliver_controller 在 UI 线程调用（QTimer.singleShot 调度链，L1453、1505 上下文），无跨线程 UI 访问；本面板自身不触碰 OpenGL/GPU 资源。
- `_ClickDefocusFilter` 以 parent=widget 构造并额外持有引用（install_click_defocus L347-352），无 GC 回收导致过滤器失效的问题；对交互控件的放行清单合理。
- 列表项删除一致性：`_on_item_delete_clicked`/`remove_selected` 删除后调用 `_refresh_all_row_indices` 维持 widget 内 row 索引与实际行号一致；remove_selected 用 `self.row(item)` 现算行号，删除移位安全。
- `add_file` 去重（UserRole 全表比对）正确；restore_items 清空重建并带状态，files_changed 单次发射。
- generate_export_filename 的 Custom Pattern 已捕获 KeyError/IndexError（仅漏 ValueError，见 AUD-33100）；其余模式字符串拼接无注入面。
- update_output_size 的模式比较均为同键 `I18N.tr` 双侧比较（L1282-1310），locale 无关正确（问题仅在命名模式与标签解析，单列）。

---

## src/shared/export/export_source.py (行数: 33)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 `ExportSource` Protocol 定义——`source_id: str`、`title: str` 属性与 `is_available()`/`estimate_frames()` 方法（返回注解 bool/int 齐全），供 deliver 解耦 edit 特性使用；文件头 DORMANT 标记明确。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-33100 |
| Major | 5 | AUD-33101, AUD-33102, AUD-33103, AUD-33110, AUD-33111 |
| Suggestion | 14 | AUD-33104–33109, AUD-33112–33119 |

主题模式：本批次最集中的问题簇是 (a) 以显示文本（I18N 后的 combo/label 文本）作为逻辑数据源——命名模式比较（33101）、格式映射（33112）、resize 标签解析（33103/33110）同根；(b) setRange 触发的重入 valueChanged + 陈旧值读取（33102/33111）；(c) 用户输入 format 串未捕获 ValueError（33100）。`__init__.py` 与 `export_source.py` 无逻辑问题；`batch_queue_store.py` 与 `encoder.py` 仅有 Suggestion 级问题（原子写与不可变配置的实现面良好）。
