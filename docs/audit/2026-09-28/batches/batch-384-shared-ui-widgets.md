# 批次审查报告：batch-384 [P2] src/shared/ui/widgets//

- 审查日期：2026-09-28
- 文件数：4（2,223 行）
- 编号区间：AUD-38400–AUD-38499
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

---

## src/shared/ui/widgets/toggle_switch.py (行数: 80)

### 发现

- [AUD-38400] [Suggestion] 无键盘访问性：无 keyPressEvent、未设置 focusPolicy，无法用 Tab/Space 切换
  - 位置: src/shared/ui/widgets/toggle_switch.py:30-55
  - 证据:
    ```python
    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self._checked = checked
        self.setFixedSize(32, 18)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
    ...
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._checked = not self._checked
    ```
  - 影响: 作为"所有面板标准的启用/禁用控件"，键盘用户与辅助功能（accessibility）完全无法操作该开关；与 QPushButton 的 Space/Enter 语义不一致（isDown() 已做兼容，键盘却没有）。
  - 建议: `setFocusPolicy(Qt.FocusPolicy.StrongFocus)` 并实现 `keyPressEvent`（Space/Enter 触发 toggle + 发射 toggled）。
  - 置信度: 高

- [AUD-38401] [Suggestion] `parent` / `event` 参数缺类型注解
  - 位置: src/shared/ui/widgets/toggle_switch.py:30,51,57
  - 证据:
    ```python
    def __init__(self, checked: bool = False, parent=None):
    ...
    def mousePressEvent(self, event):
    ...
    def paintEvent(self, event):
    ```
  - 影响: 与项目强类型规范（所有函数有类型注解）不符。
  - 建议: `parent: Optional[QWidget] = None`、`event: QMouseEvent` / `event: QPaintEvent`。
  - 置信度: 高

### 通过项
- 信号防递归：`setChecked` 程序化更新不发射 `toggled`，仅用户点击发射，设计正确，无递归回路
- 线程安全：纯 UI 线程控件，无跨线程数据
- GPU 资源生命周期：无 OpenGL/ModernGL 使用（QPainter 仅 CPU 绘制）
- 状态提交一致性：无持久化/undo 逻辑，不适用
- 异常与取消路径：无异常路径，`setChecked` 幂等
- 输入验证：无外部输入

---

## src/shared/ui/widgets/topos_controls.py (行数: 293)

### 发现

- [AUD-38402] [Suggestion] mousePressEvent 双重设值：自定义跳转计算后又交 super()，依赖硬编码 14px 手柄宽度与样式一致
  - 位置: src/shared/ui/widgets/topos_controls.py:219-226
  - 证据:
    ```python
    # 原有行为：点击滑槽直接跳转
    if event.button() == Qt.MouseButton.LeftButton:
        if self.orientation() == Qt.Orientation.Horizontal:
            value = self._pos_to_value_horizontal(event.position().x())
        else:
            value = self._pos_to_value_vertical(event.position().y())
        self.setValue(value)
    super().mousePressEvent(event)
    ```
  - 影响: QSlider 原生 mousePressEvent 同样会按当前样式计算并设值。offscreen/Fusion 实测（200px 宽、range 0-100，x=5..195 七个点位）自定义值与原生值恰好一致、仅发射一次 valueChanged；但自定义计算用硬编码 `handle_width = 14`（253-256、273-276 行），一旦实际样式手柄宽度不同（如 macOS Aqua），两次计算将不一致：最终值被 super() 覆盖（自定义计算变成无效功），且同一点击 valueChanged 双发，下游面板可能重复提交参数/渲染请求。
  - 建议: 二选一：完全自管（super() 不再调用，或仅在右键等场景调用），或删除自定义 `_pos_to_value_*` 只依赖原生行为；若保留，用 `_handle_rect()` 的官方 `subControlRect` 几何替换硬编码 14px。
  - 置信度: 高（冗余设值确定）；中（双发/覆盖仅在样式几何不一致时出现，未在 Aqua 样式下验证）

- [AUD-38403] [Suggestion] `mouseDoubleClickEvent` 等参数缺类型注解
  - 位置: src/shared/ui/widgets/topos_controls.py:76
  - 证据:
    ```python
    def mouseDoubleClickEvent(self, event) -> None:
        """双击事件处理：重置到默认值"""
    ```
  - 影响: 类型注解缺失（Suggestion 级维度）。
  - 建议: `event: QMouseEvent`。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程控件，无共享可变状态
- GPU 资源生命周期：无 GPU 资源
- GLSL·HDR 正确性：不涉及
- 信号防递归：双击重置路径 setValue → valueChanged 为用户主动行为，无双射回路；`enable_velocity_drag` 为 opt-in，默认行为不变
- 速度拖拽状态机正确：`_vdrag_accum = dx * sensitivity` 为绝对位移计算（非增量），无累积误差；垂直方向取负与手柄方向一致；setValue 自动 clamp 到 min/max
- `_handle_rect` 使用 Qt 官方 `subControlRect`（跨平台可靠）
- 输入验证：`groove_width <= 0` / `groove_height <= 0` 有防护；`max(self.width(), 1)` 防除零
- 状态提交一致性：不涉及

---

## src/shared/ui/widgets/vectorscope_widget.py (行数: 1,513)

### 发现

- [AUD-38404] [Critical] HistogramWidget 对 NaN 输入无防护：np.bincount 抛 ValueError 未捕获，PyQt6 槽内未处理异常默认 qFatal 终止进程
  - 位置: src/shared/ui/widgets/vectorscope_widget.py:845-853
  - 证据:
    ```python
    for ch in (2, 1, 0):  # R, G, B（输入 BGR）
        codes = np.clip(small[:, :, ch].astype(np.float32) * 255.0,
                        0, 255).astype(np.int32)
        counts = np.bincount(codes.flatten(), minlength=self.BINS)
    ```
  - 影响: `np.clip` 对 NaN 不生效（NaN 穿透），`astype(np.int32)` 得到 INT_MIN（本仓库 venv 实测：codes=[[-2147483648]]），`np.bincount` 对负值抛 `ValueError: 'list' argument must have no negative elements`（实测复现）。该调用链位于 `scope_panel._process_pending_image`（QTimer 槽）→ `update_histogram`，异常未捕获；全仓库未安装自定义 `sys.excepthook`（已 grep 确认），PyQt6 默认对槽内未处理异常调用 qFatal/abort → 单个 NaN 像素即可使整个应用在直方图模式下崩溃。HDR float 管线（float16/32，除法/LUT 数值）产生 NaN 是真实风险——同文件 CIE 路径 1060 行显式 `nan_to_num` 防护即为例证，波形/直方图路径缺失同等防护。
  - 建议: 在 `_downsample_columns` 或各 `update_*` 入口统一 `np.nan_to_num(arr, nan=0.0, posinf=..., neginf=...)`（与 1060 行 CIE 路径对齐），或对 bincount 输入做 `np.maximum(codes, 0)` 兜底。
  - 置信度: 中（崩溃链已实测锁定；触发需渲染帧含 NaN，CIE 路径的显式防护间接证明 NaN 可达）

- [AUD-38405] [Major] Vectorscope 将 HDR 输入 clip(0,1)（附 >1.5 除 255 启发式），超白像素色度被非线性扭曲
  - 位置: src/shared/ui/widgets/vectorscope_widget.py:182-187
  - 证据:
    ```python
    # Step 2: Convert RGB to CbCr (Rec. 709)
    # Handle both 0-1 and 0-255 ranges
    if sampled.max() > 1.5:
        sampled = sampled / 255.0

    # Clamp to valid range
    sampled = np.clip(sampled, 0.0, 1.0)
    ```
  - 影响: 项目内部为 HDR 数据（同文件波形显式支持 -10%..120% 域，scope_panel 对两 widget 喂同一帧）。clip 是逐通道非线性操作，HDR 超白像素（如 1.2, 1.0, 0.8）的 Cb/Cr 与真实值不同 → 矢量示波器对高亮区显示错误色调/饱和度，作为校色精度工具给出误导读数；违反"HDR 内部数据不得无理由 clamp(0.0,1.0)"铁律（矢量图惯例不构成充分理由，且与波形处理不一致）。`>1.5` 阈值也无法区分 8-bit 数据与 0..1.5 区间的 HDR 数据。
  - 建议: Cb/Cr 为色度分量，可先减去亮度均值或直接对未 clamp 数据做矩阵运算后在显示端（已存在的 `outside_mask`）约束半径；归一化检测应由调用方显式传入位深标记而非启发式。
  - 置信度: 高（代码事实与数学扭曲确定；是否属"有意显示域裁剪"未在注释中声明）

- [AUD-38406] [Major] np.random.choice(replace=False) 对全像素索引全量置换：4K 实测 349ms/次 UI 线程卡顿，且逐帧重随机致迹线闪烁
  - 位置: src/shared/ui/widgets/vectorscope_widget.py:161-169
  - 证据:
    ```python
    else:
        # Random sampling to avoid grid patterns
        sample_ratio = target_samples / total_pixels

        # Generate random indices
        num_samples = int(total_pixels * sample_ratio)
        indices = np.random.choice(total_pixels, num_samples, replace=False)
    ```
  - 影响: `replace=False` 需对 8,294,400（4K）个索引做全量 Fisher-Yates 置换，本仓库 venv 实测单次 348.7ms。该计算在 QTimer 槽（UI 线程）执行，节流 66ms 间隔下每次更新都冻结 UI 约 350ms——播放/拖参数时示波器打开即明显卡顿，违背 4K 实时基准。scope_panel 的 `_cpu_analysis_max_width/height` 默认 None（scope_panel.py:140-141，无外部调用 set_cpu_analysis_max_size），全分辨率帧直达此路径。此外逐帧重新随机使热图迹线闪烁。
  - 建议: 改为 O(k) 方案：带放回采样 `rng.choice(n, k, replace=True)`、或系统抽样（等距 + 随机相位）、或首次生成索引后按图像尺寸缓存复用；同时考虑给 vectorscope 默认设置分析分辨率上限。
  - 置信度: 高（耗时已实测；默认无尺寸上限已核实）

- [AUD-38407] [Suggestion] 波形/Parade/Luma 密度路径对 NaN 无防护，NaN 像素静默落入顶行产生假迹线
  - 位置: src/shared/ui/widgets/vectorscope_widget.py:590-598,691-700
  - 证据:
    ```python
    normalized = (chan_float - MIN_V) / RANGE_V
    y_indices = np.clip(
        (1.0 - normalized) * (BINS - 1), 0, BINS - 1
    ).astype(np.int32)
    ...
    flat_indices = y_indices.flatten() * cols + x_indices
    counts = np.bincount(flat_indices, minlength=BINS * cols)
    ```
  - 影响: NaN 穿透 clip，astype 得 INT_MIN，与 cols（1024/768）相乘后 int32 溢出回绕为 0（实测 cols=2/768/1024 均回绕 0，cols=3 则抛 ValueError）→ NaN 像素静默计入顶行 bin，波形顶部出现一条假迹线，误导曝光判断（Inf 会被 clip 正常截断，仅 NaN 危险）。
  - 建议: 与 AUD-38404 一并处理：入口统一 `nan_to_num`。
  - 置信度: 高（回绕行为已实测）

- [AUD-38408] [Suggestion] 800×800 热图全量重算在 UI 线程，多次 640k 元素幂/对数运算吃帧预算
  - 位置: src/shared/ui/widgets/vectorscope_widget.py:200-229,244-321
  - 证据:
    ```python
    bins = 800
    ...
    cb_grid, cr_grid = np.meshgrid(cb_coords, cr_coords, indexing='xy')
    ...
    pixel_norm = np.log1p(pixel_density) / np.log1p(pixel_density.max())
    pixel_norm = np.power(pixel_norm, 0.8)
    ...
    brightness = np.power(pixel_norm, 0.7) * 2.0
    ```
  - 影响: `_create_heatmap_qimage` 每次生成 6 组 800×800 float 网格并做多次 `np.power`/`log1p`/fancy-index，全部在 UI 线程；虽有 15FPS 节流，仍可能与 AUD-38406 叠加造成可感卡顿。
  - 建议: 降 bins（示波器显示尺寸通常 <800px）；r_base/g_base/b_base 网格与索引映射仅依赖 bins，可模块级缓存一次。
  - 置信度: 中（量级估算，未逐项计时）

- [AUD-38409] [Suggestion] zoom=2 时 graticule 刻度圈/靶框固定不缩放，与数据错位导致饱和度读数错误
  - 位置: src/shared/ui/widgets/vectorscope_widget.py:344-357,392-402,427-436
  - 证据:
    ```python
    zoom_factor = float(self._zoom_level)
    heatmap_rect = QRectF(
        center_x - radius * zoom_factor,
        ...
    )
    ...
    # Draw graticule (overlay) - UI elements stay fixed size
    self._draw_graticule(painter, center_x, center_y, radius)
    ...
    # 75% saturation circle (outer)
    painter.drawEllipse(QPointF(cx, cy), radius * 0.75, radius * 0.75)
    ```
  - 影响: zoom=2 时数据放大 2 倍而刻度圈仍在原半径，75% 饱和度圈实际对应数据 37.5% 饱和度——调色师放大检查时按刻度读数会得到 2 倍误差的饱和度判断。注释表明是有意设计（"fixed regardless of zoom"），但语义上刻度应跟随数据域。
  - 建议: graticule 圈半径乘以 1/zoom_factor（或在 zoom>1 时标注"刻度不随缩放"）。
  - 置信度: 高（行为由代码确定；是否属可接受设计取舍存疑）

- [AUD-38410] [Suggestion] HistogramWidget.paintEvent 死代码：`rect.height() and rect.width() or (0, 0)` 立即被下一行覆盖
  - 位置: src/shared/ui/widgets/vectorscope_widget.py:868-869
  - 证据:
    ```python
    rect = self.rect()
    w, h = rect.height() and rect.width() or (0, 0)
    w, h = rect.width(), rect.height()
    ```
  - 影响: 第一行计算结果无条件被第二行覆盖，属遗留死代码，混淆阅读。
  - 建议: 删除 868 行。
  - 置信度: 高

- [AUD-38411] [Suggestion] HistogramWidget 每次重绘逐点 drawEllipse（3×256 个椭圆），无 QImage 缓存
  - 位置: src/shared/ui/widgets/vectorscope_widget.py:892-901
  - 证据:
    ```python
    for ch, color in enumerate(channel_colors):
        hist = self._hist_rgb[ch]
        pts = []
        for i in range(self.BINS):
            x = w * i / (self.BINS - 1)
            y = top - hist[i] * (top - 10)
            pts.append(_QRectF(x - 1.0, y - 1.0, 2.2, 2.2))
        painter.setPen(QColor(*color[:3]))
        for p in pts:
            painter.drawEllipse(p)
    ```
  - 影响: 每帧 768 次 drawEllipse + 768 次 numpy 标量索引，且与同文件其他 scope widget（预渲染 QImage）模式不一致；窗口遮挡/移动等任意重绘都重复付出。
  - 建议: 数据到达时预渲染成 QImage（与其他 widget 一致），或至少用 drawPoints/折线。
  - 置信度: 高

- [AUD-38412] [Suggestion] FalseColor LUT 与图例条每帧重建，未按 (mode, transfer) 缓存
  - 位置: src/shared/ui/widgets/vectorscope_widget.py:1433,1489-1495
  - 证据:
    ```python
    else:
        lut = build_false_color_lut(self._mode, self._transfer)
    ...
    lut = build_false_color_lut(self._mode, self._transfer)
    strip = np.repeat(lut[np.newaxis, :, :], 8, axis=0)
    strip = np.ascontiguousarray(strip)
    strip_img = QImage(strip.data, lw, lh, strip.strides[0],
                       QImage.Format.Format_RGB888).copy()
    ```
  - 影响: `update_false_color` 每帧构建 LUT；`paintEvent` 每次重绘再建一次 LUT 并分配 8×1024×3 数组 + QImage 拷贝，纯浪费（LUT 仅依赖 mode/transfer）。
  - 建议: set_mode/set_color_state 时缓存 LUT 与图例 QImage。
  - 置信度: 高

- [AUD-38413] [Suggestion] `_waveform_density` 与 `WaveformWidget.update_waveform` 内嵌 `get_waveform_channel` 逻辑重复
  - 位置: src/shared/ui/widgets/vectorscope_widget.py:585-603,691-704
  - 证据:
    ```python
    def get_waveform_channel(chan_float: np.ndarray) -> np.ndarray:
        ...
        normalized = (chan_float - MIN_V) / RANGE_V
        y_indices = np.clip((1.0 - normalized) * (BINS - 1), 0, BINS - 1).astype(np.int32)
        x_indices = np.tile(np.arange(cols), (rows, 1)).flatten()
        flat_indices = y_indices.flatten() * cols + x_indices
        counts = np.bincount(flat_indices, minlength=BINS * cols)
    # —— 与模块级 helper _waveform_density 逐行等价 ——
    def _waveform_density(chan_float: np.ndarray, bins: int = 300) -> np.ndarray:
        ...
        normalized = (chan_float - MIN_V) / RANGE_V
        y_indices = np.clip((1.0 - normalized) * (bins - 1), 0, bins - 1).astype(np.int32)
        x_indices = np.tile(np.arange(cols), (rows, 1)).flatten()
        flat_indices = y_indices.flatten() * cols + x_indices
        counts = np.bincount(flat_indices, minlength=bins * cols)
    ```
  - 影响: 两份等价实现，HDR 域常量（MIN_V/MAX_V）各自硬编码，将来改动（如域调整、NaN 防护）极易只改一份造成 widget 间不一致。
  - 建议: `update_waveform` 复用 `_waveform_density`。
  - 置信度: 高

- [AUD-38414] [Suggestion] CieChromaticityWidget 对 ndim==2 输入 `image.shape[2]` 直接 IndexError；RGB→BGR→RGB 往返反转无实际意义
  - 位置: src/shared/ui/widgets/vectorscope_widget.py:1128-1135
  - 证据:
    ```python
    if image is None or image.shape[2] < 3:
        return
    small = _downsample_columns(image[:, :, ::-1] if image.shape[2] == 3
                                else np.ascontiguousarray(image[:, :, :3]),
                                512)
    # 注意：_downsample_columns 输入按 BGR 约定；此处强制构造 BGR
    bgr = small
    rgb = bgr[:, :, ::-1].astype(np.float32)
    ```
  - 影响: 灰度 2 维输入时 `shape[2]` 抛 IndexError 而非优雅返回（输入验证缺陷，`update_cie` 只检查了 None/size）；RGB 反转成 BGR 再反转回 RGB 净效果等于直接下采样 RGB，徒增两次视图构造，注释与实际语义不符，误导后续维护。
  - 建议: 用 `image.ndim != 3 or image.shape[2] < 3` 判断；删除双重反转，直接 `_downsample_columns(image[:, :, :3], 512)`。
  - 置信度: 高（代码事实；调用方 scope_panel 目前只传 3/4 通道，触发概率低）

- [AUD-38415] [Suggestion] 'gamma22' 传输特性用 2.4 幂（名义 2.2），'bt709' 用纯幂近似分段函数
  - 位置: src/shared/ui/widgets/vectorscope_widget.py:1142-1143
  - 证据:
    ```python
    elif self._transfer in ('bt709', 'gamma22'):
        linear = np.power(np.clip(rgb, 0.0, None), 2.4)
    ```
  - 影响: gamma22 名义指数 2.2 却用 2.4；BT.709 场景传递函数为分段线性+2.4 幂，纯 2.4 幂在暗部偏差明显 → CIE 色度图对 gamma22/bt709 输入的线性化有系统偏差，色域判断（尤其暗部主导的像素）失真。
  - 建议: gamma22 用 2.2；bt709 实现标准分段 inverse OETF（或如属有意近似，加注释说明偏差量级）。
  - 置信度: 中（不匹配确定；是否有测试锁定该近似未验证——注释称与 color_transform_provider 一致并由 parity 测试锁定，若对方同为 2.4 则需两处同步改）

- [AUD-38416] [Suggestion] 各 widget `__init__(self, parent=None)`、`paintEvent(self, event)` 等参数缺类型注解
  - 位置: src/shared/ui/widgets/vectorscope_widget.py:71,323,538,619,714,780,839,1084,1211,1362,1458 等
  - 证据:
    ```python
    def __init__(self, parent=None):
        super().__init__(parent)
    ...
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
    ```
  - 影响: 类型注解缺失（Suggestion 级维度）；部分模块级函数（如 `pq_code_to_nits(code)`）也无注解。
  - 建议: 统一补 `parent: Optional[QWidget]`、`event: QPaintEvent` 等。
  - 置信度: 高

### 通过项
- 线程安全：所有 update_* 由 UI 线程 QTimer 节流调用，`_pending_image` 先取引用后置 None 的交接模式正确；帧数据经 Qt 信号跨线程投递（scope_panel → widget），无共享可变竞争
- GPU 资源生命周期：无 OpenGL 资源；所有 QImage 构造均显式 `.copy()` 脱离 numpy 缓冲（315-319、612-616、736-738、797-799、1191-1192、1440-1442、1493-1494），无悬垂缓冲
- QTimer 节流模式正确：单发定时器 + `isActive()` 判断，~15FPS 上限；`resizeEvent` 重启 pending 处理
- PQ/HLG 常量与 SMPTE ST 2084 / ARIB STD-B67 发布值一致（_PQ_M1.._PQ_C3、_HLG_A.._HLG_C），`hlg_code_to_display_nits` 正确声明 BT.2408 近似用途
- CIE 路径 NaN/Inf 防护：`linear_rgb_to_xy` 内 `nan_to_num`（1060 行）；和为零像素返回 (-1,-1) 并经 valid 过滤
- `set_color_state`（CIE）正确丢弃过期密度避免用错误传输矩阵展示（1107-1111）
- mask 形状与下采样后图像不一致时以 INTER_NEAREST 重采样对齐（1156-1159）
- `set_zebra_threshold` 阈值 clamp 到 0.5–1.2（1386）
- `build_false_color_lut` 尾档包含上界处理正确（1329）
- FalseColor zebra/伪彩仅用于显示域（clip 1.2 显示灰度），符合显示端用途
- 信号防递归：FalseColor 的 mode_changed/zebra_threshold_changed 仅在值变化时发射；`set_zoom`/`set_scale_mode` 变化时才重算

---

## src/shared/ui/widgets/viewport_toolbar_base.py (行数: 337)

### 发现

- [AUD-38417] [Suggestion] mask/split 按钮程序化 setChecked 会再次触发 toggled 并重发 mask_toggled/split_toggled，基类未提供状态锁辅助
  - 位置: src/shared/ui/widgets/viewport_toolbar_base.py:146,176,233-243
  - 证据:
    ```python
    self.mask_btn.toggled.connect(self._on_mask_toggled)
    ...
    def _on_mask_toggled(self, checked: bool) -> None:
        """Toggle mask icon color: orange when active, default when inactive."""
        color = IconColors.ORANGE if checked else IconColors.DEFAULT
        self.mask_btn.setIcon(icon_mask(color))
        self.mask_toggled.emit(checked)
    ```
  - 影响: 外部程序化同步点（如 src/features/color/ui/canvas.py:2347、2360 的 `self.mask_btn.setChecked(canvas._show_mask_overlays)`）未加 QSignalBlocker，程序化更新也会完整走 `_on_mask_toggled` → 再发射 `mask_toggled` → 触发下游状态处理。当前下游写回同值而幂等终止，但基类把防递归责任完全外推给每个调用点，违反"程序化更新控件须状态锁防递归信号"的项目约定，三页工具栏互相同步时存在信号往复风险。
  - 建议: 基类提供 `set_mask_checked(checked)` / `set_split_checked(checked)` 辅助方法，内部用 QSignalBlocker 包裹 setChecked + 仅更新图标。
  - 置信度: 中（重发路径确定；实际往复未复现，依赖下游幂等）

- [AUD-38418] [Suggestion] `parent` 参数缺类型注解；`set_timeline_choices(timelines: Any)` 弱类型
  - 位置: src/shared/ui/widgets/viewport_toolbar_base.py:78,288-292
  - 证据:
    ```python
    def __init__(self, object_name: str, parent=None):
    ...
    def set_timeline_choices(
        self,
        timelines: Any,
        active_timeline_id: Optional[str],
    ) -> None:
    ```
  - 影响: `Any` + `getattr(timelines, "items", lambda: ())()` 鸭子类型使静态检查完全失效：传入无 `.items` 的容器（如 list）会被静默判空并 hide()，问题难以定位。
  - 建议: 声明 `timelines: Mapping[str, Any]` 或 `Sequence[Tuple[str, Any]]` 并显式分支处理。
  - 置信度: 高

### 通过项
- 信号防递归（timeline 选择器）：`set_timeline_choices` 用 QSignalBlocker 包裹 clear/addItem/setCurrentIndex，程序化填充不触发 `timeline_switch_requested`；`_on_index_changed` 对 `itemData` 为 None 有防护（333-337）
- `_show_guides_panel` 延迟导入避免循环依赖；panel 以 `Qt.WindowType.Popup` 呈现（自动失焦关闭），实例缓存在 self 上无重复创建
- `showPopup` 屏幕边界 clamp（329-330）且 `self.screen()` 判空
- `update_zoom_label` 以 hasattr 防护 zoom_label 未创建的情况
- `_add_fps_indicator -> QLabel` 返回类型与 RealtimeFpsIndicator（QLabel 子类）实际相符
- 线程安全：纯 UI 线程；`record_presented_frame`/`set_playback_state` 为简单委托
- GPU 资源生命周期：无 GPU 资源
- 输入验证：guide_changed 信号参数透传由 ViewerGuidesPanel 保证；`set_playback_state` 默认 target_fps=24.0
- 状态提交一致性：不涉及（视图层信号转发，无持久化）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-38404 |
| Major | 2 | AUD-38405, AUD-38406 |
| Suggestion | 15 | AUD-38400–38403, AUD-38407–38416, AUD-38417–38418 |
| 合计 | 18 | — |

关键实测证据：
1. AUD-38404：本仓库 venv 中 `np.clip(NaN*255,0,255).astype(int32)` → `-2147483648`，`np.bincount` 抛 `ValueError`（已复现）；仓库无自定义 excepthook。
2. AUD-38406：`np.random.choice(8294400, 200000, replace=False)` 实测 348.7ms。
3. AUD-38402：offscreen 默认样式下自定义值与原生值七点位一致、单次发射（冗余但当前无可见缺陷；macOS Aqua 样式未验证）。
