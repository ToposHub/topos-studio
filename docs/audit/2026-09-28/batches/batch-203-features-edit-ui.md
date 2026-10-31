# batch-203 审查报告 — src/features/edit/ui（多机位/新建对话框/占位符/HUD/预览变换）

审查日期：2026-09-28。编号区间：AUD-20300–AUD-20399。
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消 / 输入验证 / 性能 / 类型注解。
交叉验证文件（只读）：`src/shared/media/thumbnail_provider.py`、`src/features/edit/core/project.py`、`src/shared/ui/widgets/drag_value.py`、`src/shared/assets/icon_manager.py`、`src/app/controllers/edit_controller.py`、`src/features/edit/core/interchange.py`、`src/shared/export/topos_encoder.py`。

汇总：Critical 0 · Major 3 · Suggestion 14。

---

## src/features/edit/ui/multicam_panel.py (行数: 379)

### 发现

- [AUD-20300] [Suggestion] 宽泛 `except Exception` 静默吞掉缩略图信号连接失败
  - 位置: src/features/edit/ui/multicam_panel.py:223-227, 347-351
  - 证据:
    ```python
    try:
        ThumbnailProvider.instance().frame_thumbnail_ready.connect(
            self._on_frame_thumbnail_ready)
    except Exception:  # noqa: BLE001 — 测试替身/无 GUI 环境下静默
        pass
    ```
    ```python
    def _thumbnail_provider(self):
        try:
            return ThumbnailProvider.instance()
        except Exception:  # noqa: BLE001 — 测试替身环境
            return None
    ```
  - 影响: 若单例初始化或信号连接因真实错误失败（而非测试替身场景），整个角度网格将永远停留在 "…"，无任何日志可排查；违反“不得宽泛 except 掩盖领域错误”铁律（有注释豁免理由，故降为 Suggestion）。
  - 建议: 捕获范围收窄到具体异常类型，或至少 `logger.debug("...", exc_info=True)` 记录一次。
  - 置信度: 高

- [AUD-20301] [Suggestion] `_pending_by_key` 在播放期间发起的请求永不清理（缓慢泄漏），迟到结果会短暂显示过期帧
  - 位置: src/features/edit/ui/multicam_panel.py:331-337, 339-345
  - 证据:
    ```python
    tile.set_request_key(asset_id, path, frame)
    tile.set_busy(True)
    self._pending_by_key[key] = asset_id
    provider.request_frame_pixmap_async(
        asset_id, path, frame, total,
        target_height=_TILE_THUMB_HEIGHT,
    )
    ```
    而提供方在播放挂起时直接丢弃请求、永不发信号（src/shared/media/thumbnail_provider.py:1477-1479）：
    ```python
    if self._playback_active:
        return
    ```
  - 影响: 播放中 scrub 时每个新 (path, frame) 都会登记 pending 但永远等不到回调，`_pending_by_key` 只在 `set_group` 时 clear，长会话下按“访问过的不同帧数”无界增长（条目小，属缓慢泄漏）；另外快速换帧时旧帧的迟到回调会先落到 tile 上，短暂显示过期画面（随后被新帧覆盖，模块 docstring 已声明“静帧 + 慢速刷新”边界，故仅 Suggestion）。
  - 建议: 调用 provider 前先查询其是否可服务（或让 provider 返回 bool），不可服务时不登记 pending；回调匹配时同时校验 `tile.request_key()` 与结果一致再 `set_pixmap`。
  - 置信度: 高

- [AUD-20302] [Suggestion] 两个角度指向同一文件同帧号时 pending 登记互相覆盖，先登记者收不到缩略图
  - 位置: src/features/edit/ui/multicam_panel.py:328-345
  - 证据:
    ```python
    key = (path, frame)
    if tile.request_key() == (asset_id, path, frame):
        continue  # 已请求同一帧
    tile.set_request_key(asset_id, path, frame)
    tile.set_busy(True)
    self._pending_by_key[key] = asset_id
    ```
  - 影响: 若组内两个角度引用同一路径素材且映射到同一源帧（例如同一文件两次导入成两个角度），第二个登记覆盖第一个，第一个 tile 永远停在 "…"；就绪回调只发给最后一个 asset_id。
  - 建议: pending 键改用 `(asset_id, path, frame)` 并在回调侧用请求时记录的匹配信息（或让 provider 回调携带 asset_id）。
  - 置信度: 高（代码路径确定；触发场景少见）

- [AUD-20303] [Suggestion] 类型注解缺失：`mousePressEvent(event)`、若干 dict 泛型与 `set_group` 的 group 参数
  - 位置: src/features/edit/ui/multicam_panel.py:131, 152-154, 233-238
  - 证据:
    ```python
    def mousePressEvent(self, event) -> None:  # noqa: N802 — Qt 覆写
    ```
    ```python
    self._asset_infos: dict = {}     # asset_id → {"path","total_frames"}
    self._tiles: dict[str, _AngleTile] = {}
    self._pending_by_key: dict[tuple[str, int], str] = {}
    ```
    ```python
    def set_group(
        self,
        group,
        active_asset_id: str = "",
    ```
  - 影响: `group`、`event` 无类型，dict 值类型未参数化，IDE/静态检查无法校验控制器注入契约。
  - 建议: `event: QMouseEvent`；`group: Optional["MulticamAngleGroup"]`（TYPE_CHECKING 导入）；补齐 dict 泛型。
  - 置信度: 高

### 通过项
- 线程安全：缩略图就绪信号经 `_ThumbnailSignalBridge` 队列回主线程再 emit（thumbnail_provider.py:114-130, 926-930），面板槽在 UI 线程执行，无跨线程 UI 操作。
- 信号防递归：面板为纯展示/意图上报（`switch_angle_requested`/`sync_requested`），不回写自身控件，无递归风险。
- 性能模式：playhead 推帧经 250ms 单发 QTimer 合并去抖；tile 同帧去重（request_key 比对）。
- 输入验证：帧号 clamp 到 `[0, total-1]`；缺 path/total 的角度跳过；`_on_sync_clicked` 校验 currentData 为非空 str。
- 资源生命周期：网格重建用 `takeAt + deleteLater`，无 widget 泄漏；QTimer 有 parent。
- 无 GPU/OpenGL、无 GLSL/HDR 相关代码。
- lambda 用默认参数绑定循环变量（`g=group, a=angle`），无迟绑定闭包陷阱。

---

## src/features/edit/ui/multicam_sync_result_dialog.py (行数: 128)

### 发现

- [AUD-20304] [Suggestion] `ok_rows` 计算后从未使用（死代码）
  - 位置: src/features/edit/ui/multicam_sync_result_dialog.py:63
  - 证据:
    ```python
    ok_rows = [r for r in rows if not r.error]
    hint = QLabel(I18N.tr(
    ```
  - 影响: 无功能影响，但暗示原本计划“仅成功行统计/汇总”的逻辑未实现或残留。
  - 建议: 删除该行，或补上基于 `ok_rows` 的汇总（如平均置信度）。
  - 置信度: 高

- [AUD-20305] [Suggestion] `parent` 类型注解过窄（`Optional[QDialog]`）且模块级 `logger` 未使用
  - 位置: src/features/edit/ui/multicam_sync_result_dialog.py:29, 53-55
  - 证据:
    ```python
    logger = logging.getLogger(__name__)
    ```
    ```python
        rows: Sequence[MulticamSyncResultRow],
        parent: Optional[QDialog] = None,
    ```
  - 影响: Qt 父部件惯例是任意 QWidget（如主窗口）；当前注解会误导调用方并触发类型检查误报。
  - 建议: `parent: Optional[QWidget] = None`；`logger` 若不用于失败行告警则移除。
  - 置信度: 高

### 通过项
- 异常与取消路径：error 行有完整降级显示（"—" 占位 + 红色前景），取消走 `reject()` 丢弃结果，符合“应用才写回”契约。
- 输入验证：`out_of_range` 越界警示在行内呈现；表格 NoEditTriggers/NoSelection 防误改。
- 无信号回写、无递归风险；`MulticamSyncResultRow` 为 frozen dataclass，字段注解齐全。
- 无线程/GPU/GLSL 代码。

---

## src/features/edit/ui/new_project_dialog.py (行数: 352)

注：文件头第 1 行标记 `SUPERSEDED(2026-08-19 审计): 已被新实现取代且无生产引用，仅存档`。以下发现针对代码本身，生产影响以该标记为准。

### 发现

- [AUD-20306] [Major] 用户选择的音频采样率被静默忽略（`create_timeline` 不接收 sample_rate，硬编码 48000）
  - 位置: src/features/edit/ui/new_project_dialog.py:301-315；src/features/edit/core/project.py:2373-2377
  - 证据:
    ```python
    fps = self.fps_combo.currentData()
    width = self.width_spin.value()
    height = self.height_spin.value()
    sample_rate = self.sample_rate_combo.currentData()

    # 创建项目 - 使用 EditProject 直接创建
    project = EditProject.create(name)

    # 创建默认时间线（使用指定参数）
    project = project.create_timeline(
        name=f"{name} Timeline",
        fps=fps,
        width=width,
        height=height,
    )
    ```
    而 `EditProject.create_timeline` 签名只有 `name/fps/width/height`，内部固定 `sample_rate=48000`（project.py:2377）。
  - 影响: UI 提供 44.1/48/96 kHz 三选一，但无论选什么都创建 48kHz 时间线——功能性欺骗；音频重采样或导出元数据与用户预期不符。因文件已标 SUPERSEDED 且无生产引用，实际影响待定，但若该对话框被复用则缺陷立即成立。
  - 建议: 给 `create_timeline` 增加 `sample_rate` 参数透传，或删除该对话框（SUPERSEDED 存档建议直接移除以免误用）。
  - 置信度: 高

- [AUD-20307] [Suggestion] 项目名未做文件系统字符清洗直接拼接保存路径
  - 位置: src/features/edit/ui/new_project_dialog.py:286-318
  - 证据:
    ```python
    name = self.name_edit.text().strip()
    ...
    project_path = os.path.join(location, f"{name}{EDIT_PROJECT_EXTENSION}")
    ```
  - 影响: 名称含 `/`（macOS/Linux）会拼出不存在的子目录路径导致保存失败；含 `:` 等字符在 Windows 保存失败；极端输入可写入 location 之外意料之外的相对子路径。错误只以通用“保存失败”提示呈现。
  - 建议: 对 name 做非法字符替换/拒绝（或用 `safe_slug`），再拼路径。
  - 置信度: 高

- [AUD-20308] [Suggestion] `RESOLUTION_PRESETS` 在类体（import 时）求值 I18N；构造参数缺类型注解
  - 位置: src/features/edit/ui/new_project_dialog.py:47, 69-70
  - 证据:
    ```python
        "480p (854x480)": (854, 480),
        I18N.tr("project.custom_resolution", "Custom"): None,
    }
    ```
    ```python
    def __init__(self, config_manager, default_location: Optional[str] = None, parent=None) -> None:
    ```
  - 影响: 语言在运行期切换后，字典键固定为 import 时翻译，`_on_resolution_changed` 的键匹配依赖 `resolution is None` 兜底才不出错（该兜底存在，风险低）；`config_manager`/`parent` 无注解。
  - 建议: 键改用稳定标识（如 "custom"），显示时翻译；补齐参数注解。
  - 置信度: 高

### 通过项
- 输入验证：空名/空位置/位置非目录均有拦截；同名文件覆盖有 QMessageBox 确认；保存失败有错误提示。
- `project = project.create_timeline(...)` 链式赋值语义正确——`create_timeline` 返回带新时间线的新 `EditProject`（project.py:2361-2401），并非误把 timeline 赋给 project。
- 信号防递归：`_on_resolution_changed` 仅写 spin 值，spin 无回连 combo 的信号，无环。
- 无线程/GPU/GLSL 代码。

---

## src/features/edit/ui/new_timeline_dialog.py (行数: 677)

### 发现

- [AUD-20309] [Major] 宽高比锁定状态下选择分辨率预设，最终 W/H 按锁定比例漂移，与所选预设不符且无提示
  - 位置: src/features/edit/ui/new_timeline_dialog.py:573-582, 597-611
  - 证据:
    ```python
    def _on_resolution_changed(self, index: int) -> None:
        if index == 0:
            return
        preset_name = self.resolution_combo.itemText(index)
        res = RESOLUTION_PRESETS.get(preset_name)
        if res:
            self.width_spin.setValue(float(res[0]))
            self.height_spin.setValue(float(res[1]))
            self._update_aspect_label()
    ```
    ```python
    def _on_width_changed(self, value: float) -> None:
        if self._aspect_locked and value > 0:
            new_h = max(64, min(16384, int(round(value / self._aspect_ratio))))
            self.height_spin.blockSignals(True)
            self.height_spin.setValue(float(new_h))
    ```
  - 影响: 预设设置 width 先触发锁定重算 height（偏离预设高），随后设置 height 又触发锁定重算 width（覆盖预设宽）。例：在 16:9 锁定下选 "4K DCI (4096x2160)"，最终得到 3840x2160；combo 仍显示 "4K DCI"，`get_settings()` 返回漂移值，用户静默得到非预期分辨率。若“锁优先”是有意取舍，则至少预设显示与实际值不一致是明确缺陷。
  - 建议: 选预设时临时解锁（或先算目标 ratio 更新 `self._aspect_ratio` 再赋值），保证 W/H 与所选预设一致；或在锁定时按预设 ratio 重新锚定锁比例。
  - 置信度: 高（机制推演两遍确认）

- [AUD-20310] [Major] 编辑模式下序列 fps 不在 8 个预设内时无回退处理，保存会静默把时间线改写成 combo 当前值
  - 位置: src/features/edit/ui/new_timeline_dialog.py:520-530
  - 证据:
    ```python
    for index, (_label, preset_fps, preset_num, preset_den) in enumerate(FPS_PRESETS):
        if fps_num is not None and fps_den is not None:
            matches = int(fps_num) == preset_num and int(fps_den) == preset_den
        else:
            matches = fps_value is not None and abs(float(fps_value) - preset_fps) < 0.001
        if matches:
            self.fps_combo.setCurrentIndex(index)
            break
    ```
  - 影响: 对比分辨率匹配有 `for...else` 回退 Custom（538-544 行），fps 匹配没有任何 else 分支，且 combo 本身没有 Custom fps 项。导入/程序化产生非预设 fps（如 12、15）的序列进入“时间线设置”后，combo 停留在默认值（配置默认或 23.976），用户仅改名字点保存，`_with_timeline_fps` 会把整条序列按错误 fps 重缩放——数据被静默改写。
  - 建议: 不匹配时增加 Custom fps 数值输入（DragValue），或至少在不匹配时告警并阻止保存；短期可先在无匹配时保持只读提示。
  - 置信度: 中（机制确定；触发依赖存在非预设 fps 序列，如外部工程导入）

- [AUD-20311] [Suggestion] `_on_accept` 校验失败仅 setFocus，无任何错误提示
  - 位置: src/features/edit/ui/new_timeline_dialog.py:630-645
  - 证据:
    ```python
    name = self.name_edit.text().strip()
    if not name:
        self.name_edit.setFocus()
        return
    if name in self._existing_names and name != self._original_name:
        self.name_edit.setFocus()
        return
    ```
  - 影响: 重名/空名/时间码格式错误时 OK 按钮看似“无效”，用户得不到任何原因反馈（对比 new_project_dialog 有 `_show_error`）。
  - 建议: 复用错误提示（QMessageBox 或行内红字 + tooltip 说明）。
  - 置信度: 高

- [AUD-20312] [Suggestion] 起始时间码校验过松：接受越界值与一位数值，且 config 默认值绕过 validator
  - 位置: src/features/edit/ui/new_timeline_dialog.py:507-512, 639-644
  - 证据:
    ```python
    tc = self.start_tc_edit.text().strip()
    # Basic timecode validation
    parts = tc.split(":")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        self.start_tc_edit.setFocus()
        return
    ```
    ```python
    if cfg:
        tc = cfg.get("default_timeline_start_timecode") or "00:00:00:00"
    else:
        tc = "00:00:00:00"
    self.start_tc_edit.setText(tc)
    ```
  - 影响: "99:99:99:99"（分量越界）与 "0:0:0:0"（一位数，validator 只拦键盘输入，`setText` 绕过）都能通过；该字符串经 `format_settings["start_timecode"]` 存入序列，导出侧 fail-fast 降级为不写时码（topos_encoder.py:227-246），解析侧 `parse_timecode` 会抛 ValueError——显示与导出行为不一致且用户无感知。校验强度也低于 validator 声明（`\d{2}:...`）。
  - 建议: 校验分量范围（ff < fps、ss/mm < 60）与两位格式；config 值载入前同样验证，非法回退 "00:00:00:00"。
  - 置信度: 高（本文件行为与下游解析均已核实）

- [AUD-20313] [Suggestion] `existing_names` 参数缺类型注解
  - 位置: src/features/edit/ui/new_timeline_dialog.py:129-136
  - 证据:
    ```python
    def __init__(
        self,
        config_manager=None,
        parent=None,
        existing_names=None,
        initial_settings: Optional[dict] = None,
    ```
  - 影响: 其余参数有注解，`existing_names` 应为 `Optional[Iterable[str]]`；轻微契约不清。
  - 建议: 补 `Optional[Iterable[str]] = None`。
  - 置信度: 高

### 通过项
- 信号防递归：宽高比锁联动使用 `blockSignals(True/False)` 包住对侧赋值（597-611），DragValue.setValue 有同值守卫（drag_value.py:190-197），无 ping-pong 递归；`make_link_button` 的 `toggled` 信号存在且只读状态（icon_manager.py:610-644）。
- 状态提交一致性：edit_mode 下重名校验排除 `_original_name`；默认名自动避让 `_existing_names`。
- 性能模式：`_gcd/_aspect_label` 计算廉价；`_rebuild_display_formats` 仅在 fps 变化时重建。
- 输入验证：宽高经 DragValue 范围钳制（64–16384）；锁联动再 clamp 同范围。
- 线程安全：纯 UI 线程对话框，无并发路径；无 GPU/GLSL 代码。

---

## src/features/edit/ui/placeholder_dialog.py (行数: 84)

### 发现

- [AUD-20314] [Suggestion] `parent: Optional[object]` 类型过宽
  - 位置: src/features/edit/ui/placeholder_dialog.py:25-26
  - 证据:
    ```python
    def __init__(self, default_fps: float = 24.0, parent: Optional[object] = None) -> None:
        super().__init__(parent)
    ```
  - 影响: 注解允许任意对象传入 `QDialog.__init__`，静态检查无法拦截误传。
  - 建议: `parent: Optional[QWidget] = None`。
  - 置信度: 高

### 通过项
- 输入验证：`default_fps` 对 TypeError/ValueError/非正值有三重回退（24.0）；时长秒转帧 `max(1, round(...))` 保证至少 1 帧；空名拒绝 accept。
- 异常路径：OK/Cancel 均走标准对话框语义，无吞异常。
- 无信号回写、无线程/GPU/GLSL 代码；其余注解齐全。

---

## src/features/edit/ui/playback_hud.py (行数: 213)

### 发现

- [AUD-20315] [Suggestion] 诊断数值的格式化/比较位于 try 块之外，getter 返回非数值会让 `_refresh` 每 tick 抛异常
  - 位置: src/features/edit/ui/playback_hud.py:169-179, 181-196
  - 证据:
    ```python
    if self._get_diagnostics is not None:
        try:
            diag = self._get_diagnostics() or {}
        except Exception:  # noqa: BLE001
            diag = {}
        diag_parts = []
        total_p95 = diag.get("total_ms_p95")
        if total_p95:
            color = "#7fd18b" if total_p95 <= 33.3 else "#ffb454"
            diag_parts.append(
                f"<span style='color:{color}'>p95 {total_p95:.1f}ms</span>"
            )
    ```
  - 影响: try 只包住取数；若注入的 diagnostics getter 返回字典里混入 str（如 "12.3"），`<= 33.3` 抛 TypeError / `:.1f` 抛 ValueError——timer 槽异常被 PyQt 打印、HUD 该 tick 中断，500ms 一次刷屏 traceback。同理 `top_fallback`/`source_mix` 字符串未经 HTML 转义直接进 RichText，含 `<`/`&` 会破坏富文本。契约上是内部注入数据，故仅 Suggestion。
  - 建议: 将“取数 + 数值化 + 格式化”整体纳入 try，或对数值字段做 `float(...)` 包裹与 isinstance 校验；文本字段用 `html.escape`。
  - 置信度: 中（取决于注入方契约；机制本身确定）

- [AUD-20316] [Suggestion] 反向播放时实测 fps 冻结在旧值，不更新也不清零
  - 位置: src/features/edit/ui/playback_hud.py:116-124
  - 证据:
    ```python
    if playing and self._last_frame is not None and self._last_t is not None:
        dt = now - self._last_t
        delta = frame - self._last_frame
        if dt >= 0.2 and delta >= 0:
            self._measured_fps = delta / dt
    elif not playing:
        self._measured_fps = 0.0
    ```
  - 影响: 负速度播放时 `delta < 0`，条件不成立且不在 `elif not playing` 分支，`_measured_fps` 保留上一次正向播放的值——HUD 显示 ×-1 速度配一个过期的正实测 fps，达标颜色判断随之失真。仅诊断层失真。
  - 建议: `delta < 0` 时取 `abs(delta) / dt`，或反向时清零显示 n/a。
  - 置信度: 高

### 通过项
- 性能模式：轮询只读 controller 公开属性，无信号钩子、不侵入播放热路径，符合模块声明；`WA_TransparentForMouseEvents` 不拦截底层交互。
- 生命周期：`showEvent/hideEvent` 正确启停 QTimer；父对象 Resize 经 eventFilter 重定位；timer/label 均 QObject 父子归属，无泄漏。
- 异常路径：stats/diagnostics getter 的取数有 try 吞错且注释说明“HUD 永不打扰播放”，属合理的诊断降级。
- 类型注解齐全（Callable/Optional 注入参数）。
- 无 GPU/GLSL 代码；控制器属性经 getattr 容错读取，controller 缺失显示“无播放数据”。

---

## src/features/edit/ui/preview_view_transform.py (行数: 89)

### 发现

（无）

### 通过项
- 通过（无逻辑问题）：纯数据类，无 Qt/GPU/线程/信号代码。
- 输入验证：`set_actual_size/zoom_at/display_rect_px` 对 texture/viewport 非正尺寸全部早退；`fit_scale > 0` 有守卫；zoom 钳制在 [min_zoom, max_zoom]。
- 数学正确性：`display_rect_ndc` 的 y 轴翻转（`1 - (y+h)/vh*2`）符合 Qt 上左原点 → GL NDC 上正的约定；zoom_at 以锚点保持相对位置，公式自洽。
- 可选改进（不计入发现）：`mode: str` 可收紧为 `Literal["fit", "manual", "actual"]`，防止调用方写入任意字符串。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-20306, AUD-20309, AUD-20310 |
| Suggestion | 14 | AUD-20300–20305, AUD-20307–20308, AUD-20311–20316 |
