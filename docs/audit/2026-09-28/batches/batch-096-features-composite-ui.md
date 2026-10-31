# batch-096 审查报告 — src/features/composite/ui//

审查人：子智能体（AUD-9600–AUD-9699）
日期：2026-09-28
文件数：2（608 行）

---

## src/features/composite/ui/composite_render_dialog.py (行数: 552)

### 发现

- [AUD-9600] [Major] 帧范围无 start<=end 交叉验证，倒置区间会静默产生空渲染/负数总帧数
  - 位置: src/features/composite/ui/composite_render_dialog.py:517-518（控件定义 381-391）
  - 证据:
    ```python
    settings["start_frame"] = int(self.start_frame_spin.value())
    settings["end_frame"] = int(self.end_frame_spin.value())
    ```
    两个 DragValue 各自独立限幅（min_v=0, max_v=1000000），`get_settings()` 与 Render 按钮路径均不校验 start<=end；下游 `composite_render_output.py:367` 串行路径用 `max(0, end_frame - start_frame + 1)` 兜底为 0 帧，而并行序列路径 `composite_render_output.py:1018` 直接 `total = end_frame - start_frame + 1` 可为负数。
  - 影响: 用户输入 start=100 / end=0 后点击"渲染"，得到静默零帧渲染、无任何错误提示；并行序列阶段还会向进度回调传入负的 total。
  - 建议: 在 `get_settings()`（或 Render 按钮 clicked 槽）中校验 start<=end，非法时弹提示并阻止 accept；或在两个 spin 之间加联动下限约束。
  - 置信度: 高（对话框侧缺口确定；下游影响已核实到 composite_render_output.py:367/1018）

- [AUD-9601] [Suggestion] 跨层导入 shared 模块的下划线私有符号
  - 位置: src/features/composite/ui/composite_render_dialog.py:37-42
  - 证据:
    ```python
    from src.shared.export.export_panel import (
        ExportSectionWidget,
        _apply_compact_button_style,
        _apply_text_input_style,
        _make_param_label,
    )
    ```
  - 影响: features 层依赖 shared 层私有 API（`_apply_*` / `_make_*`），export_panel 重构或改名私有符号时会直接破坏 Composite 对话框，且此类依赖不会被"公共接口"约定保护。
  - 建议: 将这三个样式助手提升为 export_panel 的公共名称（或移入 shared/ui/widgets 样式模块）后导入。
  - 置信度: 高

- [AUD-9602] [Suggestion] 质量控件对所有图像格式恒显示且恒写入导出选项
  - 位置: src/features/composite/ui/composite_render_dialog.py:285-301（注释 "Quality (JPEG/WebP)"）、513
  - 证据:
    ```python
    # Quality (JPEG/WebP)
    ...
    self.quality_spin = DragValue("", min_v=1, max_v=100, default=95, decimals=0, suffix="%")
    ...
    settings["options"] = {"quality": settings["quality"]}
    ```
  - 影响: PNG/TIFF/EXR（无损格式，EXR 为 HDR 无损载体）下质量滑块无意义却始终可见并随设置传出，UI 语义误导（用户可能认为 EXR 导出是有损压缩）。
  - 建议: 根据所选格式动态显隐质量行，仅对 jpeg/webp 写入 quality 选项。
  - 置信度: 高

- [AUD-9603] [Suggestion] base_name 未校验路径分隔符等非法字符
  - 位置: src/features/composite/ui/composite_render_dialog.py:493、510-512、527-529
  - 证据:
    ```python
    base_name = self.name_input.text().strip() or "composite"
    ...
    settings["output_path"] = os.path.join(
        out_dir, f"{base_name}.{_ext_for(ext)}"
    )
    ```
  - 影响: 名称含 `/`（或 Windows 下 `:`、`\`）时 `os.path.join` 拼出嵌套/非法路径，目录不存在时导出在渲染阶段才报错；与"外部数据须验证"铁律不完全相符。
  - 建议: 对 name_input 做非法字符过滤或校验器（QValidator），或对拼接结果做存在性/可写性预检并提前提示。
  - 置信度: 高（代码路径确定，实际触发为中等影响）

- [AUD-9604] [Suggestion] 类型注解不完整：bare tuple 参数与未注解 parent
  - 位置: src/features/composite/ui/composite_render_dialog.py:112-119
  - 证据:
    ```python
    def __init__(
        self,
        parent=None,
        ...
        resolution: tuple = (1920, 1080),
        frame_range: tuple = (0, 0),
    ```
  - 影响: `tuple` 未参数化（应为 `tuple[int, int]`），`parent` 无注解；降低可读性与静态检查覆盖（维度：类型注解缺失）。
  - 建议: 改为 `parent: Optional[QWidget] = None`、`resolution: tuple[int, int]`、`frame_range: tuple[int, int]`。
  - 置信度: 高

### 通过项
- 线程安全：纯 QDialog UI，无 OpenGL 调用、无线程访问，符合"UI 线程不得操作 OpenGL"铁律
- GPU 资源生命周期：不涉及 GPU/ModernGL 资源
- GLSL·HDR 正确性：无 shader 代码；EXR 无损格式在列，未对 HDR 输出做不当 clamp
- 信号防递归：`radio_video.toggled` 仅切换行可见性，不回写信号源；程序化更新仅发生在构造阶段，无递归风险
- 状态提交一致性：不触碰 undo/redo/revision/持久化
- 异常与取消路径：取消→reject、渲染→accept 路径清晰；`_pick_directory` 对空返回有判断；空目录/空名称有默认回退（`or self._default_dir` / `or "composite"`）
- 性能模式：一次性 UI 构建，无热路径；组合框 userData 传递避免字符串反查

---

## src/features/composite/ui/composite_scope_widgets.py (行数: 56)

### 发现
（无）

### 通过项
- 通过（重导出 + 薄配置层）：导出 `CompositeScopePanel`（=共享 `ScopePanel` 别名）、`MODE_ORDER`、`SCOPE_MODE_ORDER`、`SCOPE_MODES`、`configure_composite_scope_panel`
- 线程安全 / GPU 资源生命周期：不涉及（描述中 GpuScopeEngine 归 Composite renderer 所有，本文件不接触）
- 信号防递归：无信号连接
- 状态提交一致性：不触碰 undo/revision
- 异常与取消路径：无异常面
- 输入验证：`configure_composite_scope_panel` 传入的 `set_available_sources` / `set_cpu_analysis_bounds` 均已核实存在于 `src/features/color/ui/scope_panel.py:589/799`，参数形态匹配
- 性能模式：`set_cpu_analysis_bounds(768, 512)` 为 CPU 回退页设置合理上限，符合性能基准约束
- 类型注解：TypeVar 绑定与函数签名注解完整
