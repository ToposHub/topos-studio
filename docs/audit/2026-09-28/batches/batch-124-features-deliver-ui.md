# batch-124 — src/features/deliver/ui（P1）

编号区间：AUD-12400–AUD-12499。审查文件 1 个，逐行通读；关键结论已对照
`video_export_formats.py` / `video_export_presets.py` / `custom_export_presets.py` /
`chevron_combo.py` 取证。

## src/features/deliver/ui/video_export_settings_panel.py (行数: 2954)

### 发现

- [AUD-12400] [Major] 纵横比锁定的宽/高联动处理器从未接线，锁定功能失效
  - 位置: src/features/deliver/ui/video_export_settings_panel.py:2271（处理器定义）、:1444-1458（实际接线）
  - 证据:
    ```python
    # _setup_connections 中宽/高只接了 draft 通知，未接联动处理器：
    for w in (
        self._start_frame_spin, self._end_frame_spin,
        self._width_spin, self._height_spin,
        ...
    ):
        ...
            w.valueChanged.connect(self._on_draft_changed)  # type: ignore[attr-defined]

    # 处理器定义存在但全文件无任何 .connect 引用：
    def _on_width_changed(self, value: float) -> None:
        if not self._aspect_locked or self._syncing_resolution_values:
            return
        if self._aspect_ratio <= 0.0:
            return
    ```
  - 影响: 用户开启 Aspect Lock 后修改 W 或 H，另一侧不会跟随（锁按钮仅更新
    `_aspect_ratio`），自定义分辨率会导出意外纵横比；`_syncing_resolution_values`
    守卫与 `_on_height_changed` 一并成为死代码。
  - 建议: 在 `_setup_connections` 补 `self._width_spin.valueChanged.connect(self._on_width_changed)`
    与 `self._height_spin.valueChanged.connect(self._on_height_changed)`。
  - 置信度: 高

- [AUD-12401] [Major] profile 按存储 id 用 setCurrentText 回填，大小写不匹配导致预设/任务的编码器 profile 静默丢失
  - 位置: src/features/deliver/ui/video_export_settings_panel.py:1667、:2359（回填）、:1938-1939（label 构建）
  - 证据:
    ```python
    # 1938-1939：非 "auto" 的 profile 显示 label 一律首字母大写，data 为原始小写 id
    for p in entry["profiles"]:
        self._profile_combo.addItem(p if p == "auto" else p.capitalize(), p)

    # 1667（load_job）与 2359（_apply_preset_to_draft）用 data 值按显示文本查找：
    self._profile_combo.setCurrentText(vc.profile or "")

    # 覆盖比较随后用草稿值顶掉预设值（2617-2618）：
    if draft_vc.profile != preset.video_config.profile:
        job.video_config.profile = draft_vc.profile
    ```
    （DEFAULT_VIDEO_PRESETS 携带 `profile="high"`，见 video_export_presets.py:89、111）
  - 影响: QComboBox.setCurrentText 是大小写敏感精确匹配，"high"/"baseline"/"main10"
    与显示文本 "High"/"Baseline"/"Main10" 不匹配 → 回填落空、选项停留在默认
    "auto"；`_read_video_config_from_draft` 读出 "auto"，`_apply_draft_overrides`
    将其判为"用户修改"覆盖任务 → 选中携带 profile 的预设或编辑已有任务再入队时
    profile 被静默改为 auto（h264 各档全中；HEVC main10 因 pix_fmt 联动路径幸免）。
    Topos 图片序列/RAW 的 tier 同理：tier id 是 data、显示的是注册表 label，
    `setCurrentText(tier_id)` 无法回填档位。
  - 建议: 两处改为 `_select_combo_by_data(self._profile_combo, vc.profile)`，
    与同函数内其它字段保持一致。
  - 置信度: 高

- [AUD-12402] [Major] 自定义预设从不镜像进草稿，过期草稿反向覆盖自定义预设的设置
  - 位置: src/features/deliver/ui/video_export_settings_panel.py:1784-1787
  - 证据:
    ```python
    if checked and len(self._selected_preset_ids) <= 1:
        preset = next((p for p in DEFAULT_VIDEO_PRESETS if p.preset_id == preset_id), None)
        if preset is not None:
            self._apply_preset_to_draft(preset)
    ```
    （用户预设 preset_id 为 `user_…` 前缀，custom_export_presets.py:103，永不
    出现在 DEFAULT_VIDEO_PRESETS 中）
  - 影响: 勾选自定义预设卡片时草稿不镜像其设置（控件无任何反馈）；而 build_jobs
    走 `_apply_draft_overrides`（2609 起）逐字段比较，过期草稿与自定义预设的
    任何差异（codec/container/分辨率/码率/profile…）都被当作"用户修改"反向覆盖
    预设 → 自定义预设的核心设置实际不生效。
  - 建议: 镜像查找改为 `self._preset_cards[preset_id].preset`（内置与自定义统一
    从卡片取），确保单一来源。
  - 置信度: 高

- [AUD-12404] [Major] 编辑态 job_id 回写到循环内所有任务，可产生重复 id 且编辑态跨 Add 不清理
  - 位置: src/features/deliver/ui/video_export_settings_panel.py:1604-1632
  - 证据:
    ```python
    for i, preset in enumerate(selected_presets):
        job = build_job_from_preset(...)
        ...
        job.allow_placeholder_clips = self._allow_placeholder_check.isChecked()
        if self._editing_job_id:
            job.job_id = self._editing_job_id
        jobs.append(job)
    ```
  - 影响: 编辑已有任务时（`load_job` 不清空 `_selected_preset_ids`）若同时勾选
    多个预设，循环内每个 job 都被赋予同一个 `_editing_job_id` → 入队后同 id
    任务互相覆盖/错乱（队列按 id 索引时即数据丢失）。且成功发出 add /
    add_and_start 信号后 `_editing_job_id` 不清理，`set_project` 换项目也不重置，
    完全依赖控制器记得调 `clear_editing()`，漏调则后续新增持续顶替旧任务。
  - 建议: 仅在 `len(jobs) == 1` 时回写编辑 id；发出 add 信号后即置空
    `_editing_job_id`（并在 `set_project` 中重置）。
  - 置信度: 中

- [AUD-12405] [Major] 预检预览用 `except Exception` 吞掉领域错误，而 Add 路径完全不设防——同一错误前者静默、后者可中止进程
  - 位置: src/features/deliver/ui/video_export_settings_panel.py:2855-2859、:2309-2316
  - 证据:
    ```python
    # 预览路径吞掉一切异常：
    try:
        jobs, _ = self.build_jobs(preflight=False)
    except Exception:  # noqa: BLE001
        self._preflight_label.setText("")
        return

    # 而 Add 路径同一构造逻辑无任何防护：
    def _on_add_clicked(self) -> None:
        jobs, result = self.build_jobs(preflight=True)
    ```
  - 影响: 违反 .clinerules 宽泛 except 禁令：草稿构造阶段的领域错误在预览中被
    静默吞掉（状态行仅清空、`preflight_updated` 消费者拿到过期结果，用户无从
    得知），点 Add 时同一异常在 Qt 槽内未捕获——PyQt6 对槽内未处理异常默认
    qFatal 中止进程 → 界面看似正常、点击即崩溃。
  - 建议: 收窄为领域异常并在标签上显示错误；`_on_add_clicked` /
    `_on_add_and_start_clicked` 对 `build_jobs` 加同域 try/except 弹窗反馈。
    :2490（auto_detect_stem_groups）、:2826-2829（frame_count）两处
    `except Exception` 建议一并收窄。
  - 置信度: 中

- [AUD-12403] [Suggestion] 预设镜像字段集小于覆盖比较字段集，fps/pix_fmt/alpha 策略/GIF 色数会被过期草稿顶掉
  - 位置: src/features/deliver/ui/video_export_settings_panel.py:2347-2391（镜像）vs :2609-2638（覆盖比较）
  - 证据:
    ```python
    # 镜像未回填 fps / pixel_format / alpha_budget_policy / gif_color_count：
    self._select_combo_by_data(self._color_space_combo, vc.color_space)
    self._select_combo_by_data(self._bit_depth_combo, vc.bit_depth)
    self._alpha_check.setChecked(vc.alpha)

    # 而覆盖比较覆盖全部字段（含镜像未回填的四项）：
    if draft_vc.pixel_format != preset.video_config.pixel_format:
        job.video_config.pixel_format = draft_vc.pixel_format
    if draft_vc.alpha_budget_policy != preset.video_config.alpha_budget_policy:
        job.video_config.alpha_budget_policy = draft_vc.alpha_budget_policy
    ```
  - 影响: 与 AUD-12401/12402 同机理的残留面：内置预设若携带非默认
    pixel_format（Topos 档）/alpha_budget_policy/gif_color_count/自定义 fps，
    镜像后草稿仍是旧值，覆盖逻辑将其判为用户修改并抹掉预设值。
  - 建议: `_apply_preset_to_draft` 补齐这四项（fps 选 combo、pix_fmt 在
    `_refresh_pix_fmt_choices` 后按 data 选择、GIF 色数回填 spin）。
  - 置信度: 中

- [AUD-12406] [Suggestion] Include audio 复选框重复连接 `_on_draft_changed`，每次切换双倍刷新
  - 位置: src/features/deliver/ui/video_export_settings_panel.py:1437、:1471
  - 证据:
    ```python
    self._audio_check.toggled.connect(self._on_audio_toggled)   # 内部末尾已调 _on_draft_changed
    ...
    self._audio_check.toggled.connect(self._on_draft_changed)   # 直连重复
    ```
  - 影响: 每次切换 Include audio 触发两轮完整 `_on_draft_changed`（含 build_jobs
    与预检重算），冗余刷新。
  - 建议: 删除 1471 行直连（`_on_audio_toggled` 已负责 draft 通知）。
  - 置信度: 高

- [AUD-12407] [Suggestion] 磁盘预设操作只捕获 ValueError，delete 路径完全裸奔，OSError 在槽内未防护
  - 位置: src/features/deliver/ui/video_export_settings_panel.py:791-804、:761-775
  - 证据:
    ```python
    try:
        custom_export_presets.save_user_preset(
            name, self._read_video_config_from_draft(),
            self._read_audio_config_from_draft(),
            self._read_subtitle_config_from_draft())
    except ValueError as exc:
        QMessageBox.warning(self, ..., str(exc))
        return
    ...
    if confirm is QMessageBox.StandardButton.Yes:
        custom_export_presets.delete_user_preset(preset_id)   # 无异常防护
    ```
  - 影响: 预设读写走文件系统（Presets/User/export），权限/磁盘错误（OSError）
    不在捕获范围内，delete 路径无任何防护——槽内未捕获异常会中止进程；外部
    数据（用户预设 JSON）的错误面未闭环。
  - 建议: 三个预设文件操作统一捕获 `(ValueError, OSError)` 并弹窗提示。
  - 置信度: 中

- [AUD-12408] [Suggestion] `_pix_fmt_display_name` 三元表达式两分支相同，非 422/444 格式显示错误兜底
  - 位置: src/features/deliver/ui/video_export_settings_panel.py:260、:267
  - 证据:
    ```python
    sub = "4:2:2" if "422" in name else ("4:4:4" if "444" in name else "4:4:4")
    ...
    else:
        depth = "10"
    ```
  - 影响: else 分支与 444 分支相同（疑似应为 "4:2:0"），默认位深 10——未来若
    传入 "yuv420p" 会显示为 "YUV 4:4:4 10-bit"；当前仅 Topos 名称经过此函数，
    属潜伏显示错误/死逻辑。
  - 建议: else 分支改为 "4:2:0"，默认位深显式处理（8）。
  - 置信度: 高

- [AUD-12409] [Suggestion] 自定义帧率用整数 QSpinBox 承载，回填非整数 fps 被截断
  - 位置: src/features/deliver/ui/video_export_settings_panel.py:1686
  - 证据:
    ```python
    self._custom_fps_spin.setValue(int(vc.fps))
    ...
    # 读取侧也只能产出整数：
    if data == "custom":
        return float(self._custom_fps_spin.value())
    ```
  - 影响: NTSC 帧率（29.97/23.976）任务回填编辑时被截断为 29/23，再次导出
    即改变时长与音画同步。
  - 建议: 自定义 FPS 改用支持一位小数的 DragValue，或回填时保留原值。
  - 置信度: 高

- [AUD-12410] [Suggestion] 每个 textChanged 字符触发同步 build_jobs + 完整预检，无去抖
  - 位置: src/features/deliver/ui/video_export_settings_panel.py:1469-1470、:2302-2307、:2851-2866
  - 证据:
    ```python
    self._filename_edit.textChanged.connect(self._on_filename_changed)
    self._output_dir_edit.textChanged.connect(self._on_draft_changed)
    ...
    def _on_draft_changed(self, *_args: Any) -> None:
        if self._building_ui:
            return
        self._update_file_size_estimate()
        self._apply_draft_to_preview()
        self._refresh_preflight()   # 内联 build_jobs + run_video_export_preflight
    ```
  - 影响: 文件名/输出目录每敲一个字符就同步构造 job 并跑一次完整预检（含
    序列/帧数探测），长序列时间线上输入会卡顿；4K 实时基准下属可避免的
    重复计算。
  - 建议: 对 textChanged 类高频源加 ~200-300ms QTimer 去抖后再刷新预检。
  - 置信度: 高

- [AUD-12411] [Suggestion] 未用/重复导入与缺失类型注解
  - 位置: src/features/deliver/ui/video_export_settings_panel.py:2869、:186-187、:2840、:170、:2945
  - 证据:
    ```python
    from PyQt6.QtGui import QColor          # 2869：导入后从未使用
    from PyQt6.QtWidgets import QMessageBox # 186：模块顶部已导入
    from src.shared.i18n.i18n import I18N   # 187：模块顶部已导入
    def _sequence_resolver(self):           # 2840：缺返回类型注解
    def confirm_lossy_color_delivery(parent, result: Any) -> bool:  # parent 无注解
    def mouseReleaseEvent(self, event):     # 2945：参数/返回值均无注解
    ```
  - 影响: 死导入、重复导入与类型注解缺失，属 .clinerules 质量要求范畴。
  - 建议: 删除未用导入；补齐 `parent: Optional[QWidget]`、
    `_sequence_resolver(self) -> Optional[Callable[[str], Any]]`、
    `mouseReleaseEvent(self, event: QMouseEvent) -> None` 等注解。
  - 置信度: 高

### 通过项

- 线程安全：纯 Qt 控件面板，仅在 UI 线程操作，无 OpenGL/渲染线程交互，无跨线程共享可变状态。
- GPU 资源生命周期：不创建/持有任何 ModernGL 纹理、缓冲或 FBO，无泄漏面。
- GLSL·HDR 正确性：无 GLSL；位深/像素格式仅作为选择器数据传递，无 HDR 数据被无理由 clamp(0.0,1.0)。
- 信号防递归（守卫体系）：`_building_ui`、`_syncing_naming_fields`（文件名↔自定义名双向同步）、
  `_syncing_format_options`（容器↔编码器互刷）、`_syncing_pix_profile`（profile↔pix_fmt 联动）、
  `_updating_default_fields`（默认文件名回填）守卫齐备且均以 try/finally 复位。
- 输入验证（控件范围）：帧号（0..10M，-1 为 To End 特殊值）、分辨率（16..16384）、FPS（1..240，GIF 1..100）、
  GIF 色数（2..256）、唯一 ID 位数（2..10）、帧把手（0..100000）均设范围；文件名模板经
  expand_filename_template 消毒路径分隔符与非法字符。
- 状态提交一致性（revision）：project_id/project_name/project_revision 随 job 一并写入（1606-1619），
  面板为纯草稿编辑器，无 undo/redo 职责（编辑态生命周期问题见 AUD-12404）。
