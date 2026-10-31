# batch-194 审查报告：src/features/edit/ui（ADR/AI 组件）

批次：batch-194 [P1]，编号区间 AUD-19400–AUD-19499，共 6 文件 / 2,668 行。
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消 / 输入验证 / 性能模式 / 类型注解。

汇总：Critical 0 · Major 3 · Suggestion 10。

---

## src/features/edit/ui/__init__.py (行数: 163)

### 发现

（无）

### 通过项
- 通过（无逻辑风险）：纯 lazy re-export 目录——轻量导入 `EditPage`/`edit_style`，30+ 重符号（MediaPoolPanel、TimelinePanel、SceneCut* 系列、QuickProjectDialog 等）经 `__getattr__` + `importlib.import_module` 首次访问加载并缓存（36-94）。
- 线程安全：导入受 Python import 锁保护，`_loaded` dict 写入原子，无共享可变状态。
- 异常路径：未知属性显式抛 `AttributeError` 并带名称信息（94），不掩盖错误。
- TYPE_CHECKING 块仅为 IDE 提示，运行时零开销；`__all__` 与 `_HEAVY_MODULES` 一一对应（125-163）。

---

## src/features/edit/ui/adr_take_sheet_panel.py (行数: 563)

### 发现

- [AUD-19400] [Major] PyAV 解码异常路径泄漏 container 文件句柄
  - 位置: src/features/edit/ui/adr_take_sheet_panel.py:173-190
  - 证据:
    ```python
            import av
            container = av.open(path)
            stream = container.streams.audio[0]
            sample_rate = int(stream.rate or 48000)
            blocks: List[np.ndarray] = []
            channels = 1
            for frame in container.decode(audio=0):
                arr = frame.to_ndarray()  # (channels, n) for fltp
    ```
    （`container.close()` 只在 184 行成功路径执行；178 行 decode 中途抛异常时直接落入 188 行 `except Exception`，container 未关闭。）
  - 影响: 损坏/半写的 take 文件每次试听都会泄漏一个文件句柄与内部缓冲；循环录音会话中反复试听坏 take 可累积耗尽 FD。
  - 建议: 用 `contextlib.closing(av.open(path))` 或 try/finally 保证 close；`stream` 变量当前也未使用，可顺带删除。
  - 置信度: 高

- [AUD-19401] [Major] 试听在 UI 线程同步全量解码整个 take 文件，无时长/大小护栏
  - 位置: src/features/edit/ui/adr_take_sheet_panel.py:125-127, 178-187
  - 证据:
    ```python
        self.stop()
        samples, sample_rate, channels = self._decode(path)
        if samples is None or samples.size == 0:
            return False
    ```
    （`_decode` 内 `for frame in container.decode(audio=0): blocks.append(...)` 后 `np.concatenate(blocks, axis=1)` 全量驻留内存，调用点在双击/按钮的 UI slot 中。）
  - 影响: docstring 声明"短 take 全量解码"，但代码不限制长度：循环录音产生的长 take（如 10 分钟 stereo 48k float32 ≈ 230 MB）双击试听将冻结 UI 数秒并占用大量内存；`itemDoubleClicked` 直连该路径，用户极易触发。
  - 建议: 解码放入后台线程/任务后经信号回 UI；或至少加时长上限（超限提示并拒绝全量解码），播放改用流式喂入。
  - 置信度: 中（设计文档已注明"短 take"取舍，但无任何运行时护栏，风险真实存在）

- [AUD-19402] [Suggestion] `_feed` 忽略 `QIODevice.write` 返回值，部分写入会丢音频数据
  - 位置: src/features/edit/ui/adr_take_sheet_panel.py:149-157
  - 证据:
    ```python
        free = self._sink.bytesFree()
        if free > 0:
            chunk, self._pending = self._pending[:free], self._pending[free:]
            if chunk:
                self._io.write(chunk)
                return
    ```
  - 影响: `QIODevice.write` 允许短写；若返回值 < len(chunk)，已切出 `_pending` 的剩余字节被静默丢弃，试听出现爆音/缺字。
  - 建议: `written = self._io.write(chunk)`；将 `chunk[written:]` 前插回 `_pending`。
  - 置信度: 低（push 模式下通常全量接受，但 Qt 文档不保证）

- [AUD-19403] [Suggestion] 设备枚举在 UI 线程同步执行，且面板构造期即触发一次
  - 位置: src/features/edit/ui/adr_take_sheet_panel.py:346, 412
  - 证据:
    ```python
        for device_id, description in RecordingManager.list_input_devices():
            self._device_combo.addItem(description, device_id)
        if current:
    ```
    （346 行 `self.reload_devices()` 在 `_build_settings_group` 构造过程中调用；`list_input_devices` 内部走 `QMediaDevices.audioInputs()`。）
  - 影响: 首次调用会初始化平台音频后端，设备多或磁盘/网络音频设备存在时阻塞 UI 线程，拖慢 Edit 页面首帧。
  - 建议: 枚举移到后台或延迟到面板首次可见；构造期仅放"System Default"项。
  - 置信度: 高（代码事实；卡顿幅度依环境而定）

- [AUD-19404] [Suggestion] 试听启动失败静默返回，用户无任何反馈
  - 位置: src/features/edit/ui/adr_take_sheet_panel.py:535-542
  - 证据:
    ```python
    def _on_audition_clicked(self, *_args) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        if self._audition.is_playing():
            self._audition.stop()
            return
        self._audition.play(entry.file_path)
    ```
  - 影响: `play()` 返回 False（无输出设备/解码失败/文件缺失）时按钮看似点了没反应，用户无法区分"没点到"与"失败"。
  - 建议: 检查返回值，失败时置状态栏提示或临时禁用按钮 + tooltip 说明原因。
  - 置信度: 高

- [AUD-19405] [Suggestion] 类型注解缺失/不一致（含 `dbfs: float` 却判 None）
  - 位置: src/features/edit/ui/adr_take_sheet_panel.py:59-60, 469, 486, 529, 460
  - 证据:
    ```python
    def set_level(self, dbfs: float) -> None:
        if dbfs is None or not np.isfinite(dbfs):
            dbfs = -60.0
    ```
    （`_ensure_sheet`/`take_sheet`/`_current_entry`/`monitor_write_block` 均无返回注解；`set_level` 注解为 float 却运行时判 None，应改 `Optional[float]`。）
  - 影响: 与仓库强类型约束不一致，IDE/静态检查无法发现传参错误。
  - 建议: 补齐返回类型注解；`set_level` 改 `Optional[float]`。
  - 置信度: 高

### 通过项
- 信号防递归：`reload_devices`（408-418）与 `set_monitor_enabled`（450-452）程序化更新控件均正确 blockSignals。
- 线程安全：`InputMonitorSink.write_block` 经 RecordingManager 的 `readyRead`→`_on_audio_ready` 在 GUI 线程回调（recording_manager.py:984），无跨线程 Qt 对象调用；`_buffer_lock` 在核心侧覆盖采集缓冲。
- 异常与取消：`play`/`start`/`_decode` 全部捕获并 logger.warning，失败返回 False/None 安全降级，不向 UI 抛异常；`stop()` 幂等。
- GPU/GLSL：纯 QtMultimedia UI 组件，无 OpenGL 资源，不涉 HDR clamp 问题。
- 输入验证：`set_level` 对 NaN/inf/-inf 归一；`set_calibration` 对 None 与缺属性兜底；`current_take_id` 空选返回 ""。
- 状态提交一致性：面板不直接改工程——mark_best/replace 仅发信号由命令栈执行，`mark_best_locally` 仅作命令后回显（554-557）。

---

## src/features/edit/ui/ai_assistant_composer.py (行数: 1508)

### 发现

- [AUD-19406] [Suggestion] 插件重扫时当前 mode 被静默 normalize，不发 `mode_changed`，controller 侧视图可能滞后
  - 位置: src/features/edit/ui/ai_assistant_composer.py:671, 692
  - 证据:
    ```python
        reload_assistant_plugins()
        specs = get_assistant_mode_specs()
        current_mode = normalize_assistant_mode(self._state.mode)
    ```
    （692 行 `self._state.mode = current_mode` 直接改本地状态；若原 mode 的插件文件夹已被删除，normalize 落回默认值但整个函数不 emit `mode_changed`。）
  - 影响: `ai_edit_controller._on_composer_mode_changed` 只在信号到达时更新 `_current_assistant_mode`；插件被移除后 composer 显示/内部状态已是回退值，controller 仍持有旧 mode id，后续请求路由与新 UI 展示可能不一致，直到用户手动再切换一次。
  - 建议: 当 `current_mode != 原值` 时补发 `mode_changed.emit(current_mode)`，让 controller 同步归一。
  - 置信度: 中（不一致窗口确实存在；controller 端多数路径会再次 normalize，实际触发概率有限）

- [AUD-19407] [Suggestion] 每次打开会话菜单都在 UI 线程同步重扫插件目录
  - 位置: src/features/edit/ui/ai_assistant_composer.py:654-664
  - 证据:
    ```python
        # Folder-based plugins can be added or removed while the application
        # is open. Refreshing when the menu opens keeps the selector truthful
        # without making the composer poll the filesystem continuously.
        self.refresh_assistant_plugins()
        self._conversation_menu.exec(
            self._conversation_btn.mapToGlobal(
                self._conversation_btn.rect().bottomLeft()
            )
        )
    ```
  - 影响: `reload_assistant_plugins()` 同步遍历文件系统并解析清单；目录在网络盘/冷缓存时点击按钮到弹菜单之间出现可感知卡顿。
  - 建议: 加时间戳节流（如 5s 内不重扫）或把扫描移到后台线程完成后再重建菜单。
  - 置信度: 高（同步扫描为代码事实；卡顿程度依环境）

- [AUD-19408] [Suggestion] `item.label` 未转义直接拼入 RichText HTML
  - 位置: src/features/edit/ui/ai_assistant_composer.py:1392-1393
  - 证据:
    ```python
        lbl = QLabel(f"<b>{indent}[{kind_label}] {item.label}</b>")
        lbl.setTextFormat(Qt.TextFormat.RichText)
    ```
  - 影响: label 来自 controller 聚合的活动记录（可能含文件名），带 `<`、`&` 时富文本解析吞字或整段消失；`_status_color`/`icon` 来自固定字典是安全的，但 label 是外部数据。
  - 建议: `item.label` 过 `html.escape()`（或改 PlainText 分两个 label 渲染）。
  - 置信度: 高（未转义为代码事实；触发依赖 label 内容）

- [AUD-19409] [Suggestion] `set_state` 每次全量销毁重建所有活动行控件
  - 位置: src/features/edit/ui/ai_assistant_composer.py:1089-1102
  - 证据:
    ```python
    def _render_items(self, items: list[RunDrawerItem]) -> None:
        # 清空旧条目（保留尾部 stretch）
        while self._items_layout.count() > 1:
            it = self._items_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
    ```
  - 影响: controller 每个进度 tick 都喂新 `RunDrawerState` 时，行数多的运行（每工具一行）会高频 create/destroy QWidget，产生布局重算与 GC 压力。
  - 建议: 按 `item_id` 做差量更新（复用行控件只改文本/状态），或对高频进度只更新现有行属性。
  - 置信度: 高（重建为代码事实；影响取决于上游推送频率）

### 通过项
- 信号防递归：`AiAssistantComposer.set_state`/`AiRunDrawer.set_state` 均置 `_updating` 且 try/finally 成对释放（781-869、1041-1083）；combo 回填一律 blockSignals + findData 定位（798-845）；所有 `_on_*` slot 入口检查 `_updating`。
- 线程安全：纯 UI 线程组件，无跨线程共享状态；`set_state` 由 controller 在 UI 线程喂入。
- GPU/GLSL：无 OpenGL 资源、无 HDR 数据路径。
- 资源生命周期：菜单以按钮为 parent；`_RotatingRing` 定时器随 `set_active(False)` 停止（231-239），隐藏时无空转定时器；旧行控件 `deleteLater` 释放。
- 状态一致性：footer 状态只在 set_state 回填，操作信号（mode/permission/profile/reasoning）只通知意图，符合"不改写正在运行 job"约束——run 期间 `_conversation_btn`/`_mode_combo` 正确禁用（812-815）。
- 异常路径：`_trf` 对 format 失败兜底（KeyError/IndexError）；`_compact_current_item` 对空列表由调用方先判空（1167）。
- 输入验证：`profile_options` 重复 id 去重（826-830）；未知 status/permission/kind 均有字典默认值（191-211、1176-1184）。

---

## src/features/edit/ui/ai_brief_editor.py (行数: 208)

### 发现

- [AUD-19410] [Major] 批准编辑会静默丢弃不在固定 4 项清单里的自定义 beat
  - 位置: src/features/edit/ui/ai_brief_editor.py:126-129, 172-176, 196
  - 证据:
    ```python
        beats = tuple(
            self._beats_list.item(i).text()
            for i in range(self._beats_list.count())
            if self._beats_list.item(i).checkState() == Qt.CheckState.Checked
        )
    ```
    （`_beats_list` 只在 126-129 行装固定 4 项 `"hook"/"problem"/"product_value"/"call_to_action"`；`_collect_brief` 用 `replace(base, ..., required_beats=beats)` 整体覆盖。）
  - 影响: `EditorialBrief.required_beats` 类型为任意字符串元组（editorial_models.py:226 注释"如 …"，不限于 4 项）——由 AI 生成/导入的带自定义 beat 的 brief，经 `set_brief` 回填后列表里根本没有对应项，用户一旦点"批准"，自定义 beat 被无声删除并随 revision+1 提交，属于状态提交不一致的数据丢失。
  - 建议: `set_brief` 时把 `brief.required_beats` 中不在固定清单的项追加进 `_beats_list`（带勾选态），保证 collect 是无损往返。
  - 置信度: 中（数据模型允许自定义 beat 已核实；实际工作流中是否会产生自定义 beat 取决于上游 planner）

- [AUD-19411] [Suggestion] 时长 min/max 被静默 clamp 改写，无用户反馈
  - 位置: src/features/edit/ui/ai_brief_editor.py:188-192
  - 证据:
    ```python
            target_duration=DurationTarget(
                self._dur_ideal.value(),
                min(self._dur_min.value(), self._dur_ideal.value()),
                max(self._dur_max.value(), self._dur_ideal.value()),
            ),
    ```
  - 影响: 用户输入 min=800/max=700 等非法组合时被无声改为 720/720，批准后约束与表单显示不一致（spinbox 仍显示旧值），用户不知情；虽然保证了 `DurationTarget.__post_init__` 不变量（editorial_models.py:128-133 不抛错），但缺少提示。
  - 建议: 批准前校验并在越界时给出提示/标红，或批准后把 clamp 结果写回 spinbox。
  - 置信度: 高

### 通过项
- 信号防递归：`set_brief` 用 `_updating` + try/finally（146-168）；表单控件本身未连接 change 信号，只有 `_approve_btn.clicked`，无递归面。
- 状态一致性：只发 `brief_patch_requested`（object payload），不直接改 timeline/undo；每次批准经 `with_revision_bumped()` 产生新 revision（202），符合"批准后版本化保存"。
- 输入验证：audience/tone split 后 strip 且过滤空串；`Platform`/`SubtitlePolicy` combo 从枚举值填充，`Platform(currentText())` 构造安全；DurationTarget clamp 保证不变量成立不会抛 ValueError。
- 异常路径：无外部 IO，无宽泛 except。
- 线程/GPU：纯 UI 线程组件，无 OpenGL 资源。

---

## src/features/edit/ui/ai_context_summary.py (行数: 84)

### 发现

（无）

### 通过项
- 通过（无逻辑风险）：只读展示组件——单个 QLabel 展示素材/分析/revision 摘要，无信号发射、无外部数据解析、无 IO。
- 信号防递归：`set_summary` 的 `_updating` 锁 try/finally 成对（53-80）；因不发射信号，锁属冗余但无害。
- 输入验证：对 `analyzed_count <= 0`、`analysis_fresh` 三态分支完整，颜色与文案对应。
- i18n：kwargs 经 `_trf` 兜底格式化，未加载翻译目录时不会显示字面 `{assets}`。

---

## src/features/edit/ui/ai_decision_inspector.py (行数: 142)

### 发现

- [AUD-19412] [Suggestion] 锁定/拒绝/替换按钮不按 decision 状态门控
  - 位置: src/features/edit/ui/ai_decision_inspector.py:68-77
  - 证据:
    ```python
        btn_row = QHBoxLayout()
        lock_btn = QPushButton(tr("ai_decision.lock", "锁定"))
        lock_btn.clicked.connect(lambda: self.locked.emit(self._decision_id))
        reject_btn = QPushButton(tr("ai_decision.reject", "拒绝"))
        reject_btn.clicked.connect(lambda: self.rejected.emit(self._decision_id))
        replace_btn = QPushButton(tr("ai_decision.replace", "替换"))
        replace_btn.clicked.connect(lambda: self.replaced.emit(self._decision_id))
        btn_row.addWidget(lock_btn)
    ```
  - 影响: 已 `locked` 的 decision 仍显示"拒绝/替换"，已 `rejected` 的仍显示"锁定"，点击发出与状态矛盾的意图信号，依赖 controller 逐一忽略；UI 层未体现状态机约束，易让用户误以为操作生效。
  - 建议: 按 `card.state` 禁用/隐藏不适用的动作（如 locked 只留"替换/解锁"，rejected 不再显示"拒绝"）。
  - 置信度: 高（按钮无条件渲染为代码事实；是否造成实际缺陷取决于 controller 过滤逻辑）

### 通过项
- 信号防递归：`set_decisions` 重建行期间 `_updating` + try/finally；行内按钮信号只向上转发，无回环。
- 资源生命周期：清行时 `takeAt` + `deleteLater`（125-129），无控件累积泄漏。
- 线程/GPU：纯 UI 线程组件，无 OpenGL 资源、无 HDR 数据。
- 输入验证：未知 `card.state` 有默认边框色与 "?" 图标兜底（40-45、57）。
- 连接正确性：`clicked(bool)` 连 0 参 lambda 合法（PyQt 自动截断信号参数），`locked/rejected/replaced` 直连 panel 信号转发无误。
- 约束符合：只写 decision/override 意图信号，不直接改 clip（符合 TAEP §11.3 分层）。

---

## 审查方法备注

- 6 个文件全部完整通读（ai_assistant_composer.py 1,508 行整读）。
- 交叉核实：`I18N.tr` kwargs 格式化路径（src/shared/i18n/i18n.py:132-199，未加载目录时 default 也会 format，故 ai_decision_inspector 的直连 tr 调用安全）；`DurationTarget` 字段序 (ideal, min, max) 与不变量（editorial_models.py:121-133，编辑器 clamp 正确防 ValueError）；`RecordingManager` 监听回调线程（readyRead 于 GUI 线程，recording_manager.py:984，无跨线程 Qt 调用）；`LatencyCalibration` 为带 `input_latency_ms` 属性的 dataclass（recording_manager.py:318-332）；`begin_monitor/monitor_write_block/end_monitor` 由 edit_controller.py:2756-2807 接线，非死代码。
- 未发现 Critical：本批次全部为 UI 组件，不触碰 OpenGL/渲染线程/HDR 数据；最重的三项（FD 泄漏、UI 线程全量解码、beats 静默丢失）均为错误路径或特定输入下的 Major。
