# Batch-007 静态审查报告 — color/core: layer / lut_exporter / msl_composition / opengl_render_backend

- 审查日期: 2026-09-28
- 审查方式: 只读静态审查（Read + 只读 grep 核实外部契约；AUD-0710 已用项目 venv 做只读运行时复现，未改动任何仓库文件）
- 问题编号区间: AUD-0700 – AUD-0732
- 汇总: Critical 0 / Major 7 / Suggestion 13

跨文件核实记录（提高置信度用）:
- `AsyncRenderer` 为 `QThread` 子类（src/features/color/core/async_renderer.py:99），故 opengl_render_backend 以 orchestrator 对象作为线程身份判定有效。
- UI 色轮写 `"temp"`/`"shadow"`（src/features/color/ui/color_wheel.py:1235,1238），lut_exporter 读同键，双命名（temperature/shadows 镜像）未构成导出错位，不作为发现。
- `film_grain_params` 定义于 engine（image_engine.py 等），不在 Layer 上。
- LUT 导出唯一调用点: src/app/controllers/deliver_controller.py:699（每次导出新建 LutExporter）。
- 自家 .3dl 解析: src/shared/color/lut_loader.py:141-186 `parse_3dl` 要求首行为整数尺寸。

---

## src/features/color/core/layer.py (行数: 1209)

### 发现

- [AUD-0700] [Major] 位图缓存键使用 `id(bitmap_data)`，对象地址复用或原位修改导致陈旧蒙版渲染
  - 位置: src/features/color/core/layer.py:85（另见 :115 同型）
  - 证据:
    ```python
    elif self.type == MaskShape.BITMAP and self.bitmap_data is not None:
        base += (self.bitmap_data.shape, id(self.bitmap_data))
    ```
  - 影响: ① 旧 bitmap 被 GC 后新数组分配到同一地址且形状相同 → 缓存键相等 → 命中陈旧缓存，画布渲染旧蒙版（渲染错误，无告警）；② bitmap_data 被原位修改（id/shape 不变）同样检测不到变化。`_get_shape_cache_key`（:115）同病。
  - 建议: 缓存键改用内容指纹（如 `hashlib.sha1(bitmap_data.tobytes())` 或递增版本号），或在修改 bitmap_data 的入口显式失效缓存。
  - 置信度: 中（模式确定性高；实际触发频率取决于 AI 蒙版更新路径的对象分配行为）

- [AUD-0701] [Major] 反序列化链路对外部工程数据无结构验证，畸形数据在加载时直接抛未捕获异常
  - 位置: src/features/color/core/layer.py:1141-1142（`p[0]`/`p[1]`）、:200（`data["type"]`）、:1013-1014（`bm.lower()`）
  - 证据:
    ```python
    if pts and len(pts) >= 2:
        xs = np.array([p[0] for p in pts], dtype=np.float32)
        ys = np.array([p[1] for p in pts], dtype=np.float32)
    ```
    ```python
    shape = cls(data["type"])
    ```
    ```python
    bm = data.get("blend_mode", "Normal")
    layer.blend_mode = bm.title() if bm.lower() != "normal" else "Normal"
    ```
  - 影响: 工程文件被手改/损坏时（控制点不是二元组、mask_shapes 项缺 `"type"`、blend_mode 为 null），`from_dict` 在加载路径抛 KeyError/TypeError/AttributeError，整个工程打不开且无诊断信息。
  - 建议: from_dict 各分支对外部字段做类型守卫（isinstance 检查 + 跳过坏项 + 日志），至少把单 shape/单 curve 的失败隔离为局部降级。
  - 置信度: 中（异常路径确定存在；是否被上层 try 包裹未在本批次文件中验证）

- [AUD-0702] [Major] PchipInterpolator 要求 xs 严格递增，重复 x 控制点在装有 scipy 时抛 ValueError，行为依赖运行环境
  - 位置: src/features/color/core/layer.py:1148、:1194
  - 证据:
    ```python
    elif HAS_SCIPY:
        pchip = PchipInterpolator(xs, ys)
        xi = np.arange(256, dtype=np.float32)
        result = pchip(xi) / 255.0
    ```
  - 影响: 两个控制点 x 相同（用户垂直拖叠两点即可产生）时，scipy 环境下 `build_curves_lut_from_data` 抛 `ValueError: x must be strictly increasing`（工程加载与曲线编辑两条路径都会触发）；无 scipy 时 `np.interp` 容忍重复 x，同一工程数据在不同机器上表现不一致。
  - 建议: 调用前对控制点按 x 排序并去重（或对重复 x 做 epsilon 展开），保证两条插值路径输入规整且行为一致。
  - 置信度: 中（scipy 前置条件确定；UI 是否已阻止等 x 点未验证）

- [AUD-0703] [Suggestion] apply_grade_patch 写入的 `ai_matte_file` 属性全仓库无读者，属死状态
  - 位置: src/features/color/core/layer.py:744
  - 证据:
    ```python
    if patch.get("ai_matte_file"):
        self.ai_matte_file = patch["ai_matte_file"]
    ```
  - 影响: 全库 grep 仅此一处写、无任何读（from_dict/to_dict 均不消费该属性，水印路径按序列化 dict 而非属性解析）。粘贴属性携带的 AI matte 文件名永远不会生效，注释声称的"加载由水合路径按 assets_dir 解析"未兑现。
  - 建议: 要么在工程水合路径补上 `ai_matte_file` → bitmap 的加载消费，要么删除该赋值并在文档中明确贴属性不携带 AI matte。
  - 置信度: 高（grep 全库验证）

- [AUD-0704] [Suggestion] reset_params 复位不彻底；copy_params_from 声称复制 film_grain_params 但该属性从不在 Layer 上初始化（死代码+误导文档）
  - 位置: src/features/color/core/layer.py:516、:596、:652-653
  - 证据:
    ```python
    self.qualifier_params["enabled"] = False
    ```
    （qualifier 其余参数如 hue_center/sat_low 均不复位，对照组 soft_clip_params/effects_params 是整组重建）
    ```python
    # 复制胶片颗粒参数
    if hasattr(source_layer, 'film_grain_params'):
        self.film_grain_params = copy.deepcopy(source_layer.film_grain_params)
    ```
    （`Layer.__init__` :355-464 无 film_grain_params，该属性定义在 engine 上）
  - 影响: "重置调色"后 qualifier 的抠像区间残留，下次启用即带旧值；docstring 列出的"胶片颗粒参数"实际永不被复制（hasattr 恒 False），误导后续维护。
  - 建议: qualifier 整组按默认重建（或逐键复位）；删除 film_grain_params 分支并修正 docstring（胶片颗粒是 engine 级全局效果，不应出现在图层贴属性清单中）。
  - 置信度: 高

- [AUD-0705] [Suggestion] to_dict 多处直接引用活模型可变对象，序列化产物与模型别名
  - 位置: src/features/color/core/layer.py:938、:953、:967
  - 证据:
    ```python
    "grading_params": self.grading_params,
    ...
    if self.curves_data:
        data["curves_data"] = self.curves_data
    ...
    data["plugins"] = self.plugins
    ```
  - 影响: dict 返回后若延迟写盘（autosave/异步保存），落盘的是用户后续修改过的状态；plugins 列表/params dict 更是被内外共享可双向篡改。
  - 建议: to_dict 统一 `copy.deepcopy` 这些可变字段（工程保存路径对一致性敏感）。
  - 置信度: 高（别名事实确定；实际影响取决于调用方写盘时机）

- [AUD-0706] [Suggestion] AI matte/bitmap 落盘 PNG 时 float→uint8 无 clip，超界值经 astype 回绕将静默损坏资产
  - 位置: src/features/color/core/layer.py:177、:981
  - 证据:
    ```python
    ai_matte_uint8 = (self.ai_matte * 255).astype(np.uint8)
    ```
    （bitmap_data 同型: `bitmap_uint8 = (self.bitmap_data * 255).astype(np.uint8)`）
  - 影响: 注释契约是 [0,1]，但 AI 模型输出常见轻微越界（如 1.0000001 或负残余）；numpy float→uint8 越界转换为平台相关的回绕值（255.00002 → 错误值，-0.001 → 255 附近），保存的 matte 在重载后出现噪点/反相，属静默数据损坏。
  - 建议: `np.clip(x, 0.0, 1.0)` 后再乘 255。
  - 置信度: 中（代码缺陷确定；是否实际触发取决于上游是否严格 clamp）

- [AUD-0707] [Suggestion] 公共方法普遍缺类型注解
  - 位置: src/features/color/core/layer.py:27（`MaskShape.__init__(self, shape_type)`）、:355（`Layer.__init__(self, name="Layer")`）、:533、:555、:852、:878、:887、:901、:913、:917 等
  - 证据:
    ```python
    def __init__(self, name="Layer"):
        ...
    def add_shape(self, shape_type):
        shape = MaskShape(shape_type)
    ```
  - 影响: 与 `.clinerules` 强类型约束不符；模型字段多且动态（ai_matte_file 等隐式属性），缺注解放大维护成本。
  - 建议: 补全公共 API 签名注解。
  - 置信度: 高

- [AUD-0708] [Suggestion] 图层模型变更入口无通知机制，undo/revision 同步完全依赖调用方自觉
  - 位置: src/features/color/core/layer.py:533-538、:674-755（apply_grade_patch）、:466-531（reset_params）
  - 证据:
    ```python
    def add_shape(self, shape_type):
        shape = MaskShape(shape_type)
        self.mask_shapes.append(shape)
        self.is_dirty = True
        self.mask_dirty = True
        return shape
    ```
    （只置脏标记，无信号/回调/命令封装；apply_grade_patch、reset_params、add_plugin 同型）
  - 影响: 任何绕过 controller 的直接调用（脚本 API、面板直连、测试代码）都产生不可撤销、revision 不同步的变更；layer_controller 仅对部分入口（如自动校色）显式包了 history.push_state，防绕过无强制力。
  - 建议: 在 Layer 或 engine 层提供唯一的变更入口（命令化或变更信号），直接属性写至少在关键 mutable 字段上收敛到方法。
  - 置信度: 中（结构性风险确定；实际绕过通道数量未在批外文件全量验证）

### 通过项
- 本文件无任何 OpenGL/ModernGL 直接调用，不存在 UI 线程 GL 越界问题
- 序列化/反序列化主链路字段齐全，MaskGroup 递归序列化正确；bitmap/ai_matte 用 imencode+tofile / np.fromfile+imdecode 处理 Unicode 路径（Windows 安全）
- reset_params 清空 grade_keyframes（P1-COLOR-03 注释所述的"关键帧复活"问题已处理）
- build_hsl_curve_lut_from_data 的默认值语义正确（乘子曲线缺省 1.0、HueHue 缺省 0.0）
- copy_params_from 对 numpy 数组用 .copy()、dict 用 deepcopy，重对象无浅别名
- 旧单插件格式 → 插件栈格式的前向兼容迁移存在（:1105-1111），旧 curves_lut 0-255 归一化兼容路径存在
- build_curves_lut_from_data 的 `np.clip(result, 0.0, 1.0)` 是 0-255 控制点归一化语义，非 HDR 数据误钳制

---

## src/features/color/core/lut_exporter.py (行数: 1111)

### 发现

- [AUD-0710] [Major] `_hsv_to_rgb` 的 np.stack 混合数组与标量必抛 ValueError：mixer 激活或 hue_shift≠0 时 LUT 导出必然失败（已运行时复现）
  - 位置: src/features/color/core/lut_exporter.py:651、:655、:659、:663、:667、:671（对照已修复的静态版 :363-373）；调用路径 :533-535（hue_shift）、:790+:826（mixer）
  - 证据:
    ```python
    # Sector 0: 0-60
    mask = (h >= 0) & (h < 60)
    rgb[mask] = np.stack([c[mask], x[mask], 0], axis=-1)
    ```
    静态版（已正确修复，本版漏改）:
    ```python
    rgb[mask] = np.stack([c[mask], x[mask], np.zeros(mask.sum(), dtype=hsv.dtype)], axis=-1)
    ```
    项目 venv 实测（LutExportWorker 实例 + 4³ 单位立方）: `MIXER FAIL: ValueError all input arrays must have the same shape`
  - 影响: 任何启用 RGB 混色器或色相偏移的调色，LUT 导出 100% 失败并弹"Failed to export LUT"错误框（异常被 generate_lut_cpu 的宽 except 接住转 error 信号，不崩进程，但功能完全不可用）；失败还会连带 `QApplication.processEvents()` 重入窗口期的不稳定。
  - 建议: 六个 sector 的 `0` 全部替换为 `np.zeros(mask.sum(), dtype=hsv.dtype)`（与 _hsv_to_rgb_static 对齐），最好直接复用静态版实现消除双份维护。
  - 置信度: 高（运行时复现）

- [AUD-0711] [Major] "后台 worker" 实为 UI 线程同步执行：线程空转、导出全程阻塞 UI
  - 位置: src/features/color/core/lut_exporter.py:1031（对照 :976-981 的 moveToThread 声明）
  - 证据:
    ```python
    self.worker = LutExportWorker()
    self.worker.moveToThread(self.worker_thread)
    self.worker_thread.start()
    ...
    # Start generation
    self.worker.generate_lut_cpu(layers_data, size, format_type, output_path, title)
    ```
    （直接方法调用在调用者线程执行，未走信号/queued invocation；worker 线程的 event loop 从未收到任何工作）
    后果证据 src/app/controllers/deliver_controller.py:699-712: 导出期间 on_progress 里被迫 `QApplication.processEvents()` 维持进度条刷新。
  - 影响: 65³ = 274,625 格 × 多图层 × HSV 往返/numpy 链路（外加 :898-904 三重 Python 循环逐行写文件）在 UI 线程执行数秒，界面冻结；processEvents 补救同时打开重入窗口（用户可再次触发导出/关闭对话框，与冻结中的状态交叉）。
  - 建议: 改为 `QMetaObject.invokeMethod`/自定义 signal → worker 槽（queued），或直接去掉伪线程用同步实现 + 进度节流；同时向量化文件写出。
  - 置信度: 高

- [AUD-0712] [Major] 导出的 .3dl 首行写 TITLE，自家 `parse_3dl` 也无法读回（round-trip 断裂），且不符合 Autodesk .3dl 规范
  - 位置: src/features/color/core/lut_exporter.py:924-929（对照 src/shared/color/lut_loader.py:151-155）
  - 证据:
    ```python
    with open(path, 'w', encoding='utf-8') as f:
        # Header
        f.write(f"TITLE \"{title}\"\n")
        f.write("# Created by Topos Color\n")
        f.write(f"LUT_3D_SIZE {size}\n")
        f.write(f"LUT_BIT_DEPTH {bit_depth}\n")
    ```
    自家加载器:
    ```python
    header = lines[0].split()
    try:
        dims = [int(v) for v in header]
    except ValueError:
        raise ValueError(f"Invalid .3dl header: {lines[0]!r}")
    ```
  - 影响: `int('TITLE')` 必抛 → `load_lut` 对导出的 .3dl 报 "Invalid .3dl header"，应用内 LUT 面板无法加载自己导出的文件；同时 .3dl 规范首行应为网格尺寸（如 `33 33 33`），TITLE/LUT_3D_SIZE/LUT_BIT_DEPTH 是 .cube 方言关键字，第三方工具（Resolve/Nuke 等）同样解析失败——导出产物是不可用文件。
  - 建议: .3dl 写出改为规范头（首行 `{size} {size} {size}`，仅 # 注释行携带元数据）；.cube 路径保留现格式。
  - 置信度: 高（两侧代码直接对照）

- [AUD-0713] [Major] `_load_lut_file` 宽泛 except 静默吞掉 LUT 解析错误，已应用 LUT 损坏时导出悄悄丢失该效果
  - 位置: src/features/color/core/lut_exporter.py:162-169（消费点 :437-441）
  - 证据:
    ```python
    try:
        from src.shared.color.lut_loader import load_lut
        loaded = load_lut(path)
        arr = np.ascontiguousarray(loaded.table, dtype=np.float32)
        arr = arr.transpose(2, 1, 0, 3).copy()  # [b,g,r,c] -> [r,g,b,c]
        return arr, loaded.size
    except Exception:
        return None, 0
    ```
    ```python
    lut3d, lut_size = self._load_lut_file(lut_path)
    if lut3d is not None:
        ...result = self._apply_3dlut_cpu(...)
    ```
  - 影响: 图层挂载的 .cube/.3dl 文件损坏、被移动或格式不被支持时，导出流程无任何告警地跳过该 LUT，用户拿到"看起来成功"的 LUT 文件但缺少核心调色效果——静默错误输出，且错误类型（文件不存在 vs 解析失败）不可区分。
  - 建议: 至少区分 FileNotFound 与解析异常并经 error/警告信号上报（或计入 delivery matrix 报告），禁止裸 `except Exception: return None, 0`。
  - 置信度: 高

- [AUD-0714] [Suggestion] export_lut 每次连接回调不断开，同实例重复导出将累积连接、回调重复触发
  - 位置: src/features/color/core/lut_exporter.py:1022-1028
  - 证据:
    ```python
    if on_finished:
        self.worker.finished.connect(on_finished)
    if on_error:
        self.worker.error.connect(on_error)
    if on_progress:
        self.worker.progress.connect(on_progress)
    ```
  - 影响: 当前唯一调用方每次导出新建 LutExporter（deliver_controller.py:675），风险潜伏；一旦实例被复用（脚本 API/批量导出），第二次导出会触发两份 on_finished，回调中若持有已销毁控件还将抛 RuntimeError。
  - 建议: 连接用 `Qt.ConnectionType.UniqueConnection`，或导出完成后显式 disconnect，或改一次性 lambda。
  - 置信度: 高（机制确定，当前触发条件不成立）

- [AUD-0715] [Suggestion] `_extract_exportable_params` 对嵌套可变参数只做浅拷贝，与活图层共享内部 list/dict
  - 位置: src/features/color/core/lut_exporter.py:1043、:1055、:1059
  - 证据:
    ```python
    data['grading_params'] = layer.grading_params.copy()
    ...
    data['mixer_params'] = layer.mixer_params.copy()
    ...
    data['soft_clip_params'] = layer.soft_clip_params.copy()
    ```
  - 影响: mixer_params 的 value 是 `[h, s, l]` list，浅拷贝后与活图层共享；当前因 worker 同步执行（AUD-0711）无实际竞态，但一旦修为真异步即成跨线程可变共享（UI 改参数 vs worker 读 list）。
  - 建议: 统一 deepcopy；与 AUD-0711 的修复联动验证。
  - 置信度: 高（共享事实确定，危害当前潜伏）

- [AUD-0716] [Suggestion] 文件写出为三重 Python 循环逐行 f.write（65 档 27 万行），且最终硬 clip 到 [0,1] 使 HDR 档位在导出中丢失
  - 位置: src/features/color/core/lut_exporter.py:898-904、:933-939
  - 证据:
    ```python
    for b in range(size):
        for g in range(size):
            for r in range(size):
                rgb = lut[r, g, b]
                # Clamp to valid range
                rgb = np.clip(rgb, 0.0, 1.0)
                f.write(f"{rgb[0]:.6f} {rgb[1]:.6f} {rgb[2]:.6f}\n")
    ```
  - 影响: 性能: 65³ 逐行 f-string 写出是导出耗时主要来源之一，放大 AUD-0711 的 UI 冻结；正确性: exposure 推过 1.0 的 HDR 高光被统一截断为 1.0，而 GPU 管线 apply_3dlut 有 HDR 比例保留语义——导出的 LUT 与实际渲染在高光段不一致（.cube DOMAIN 固定 0-1 属格式约束，但可扩 DOMAIN 或在 UI 明示有损）。
  - 建议: 用 `np.savetxt`/批量 join 一次写出；HDR 丢失至少写入导出确认文案（可与 delivery matrix 报告合并）。
  - 置信度: 中

### 通过项
- export_lut 入口对 size/format/空图层显式校验并同步抛 ValueError（输入验证合格）
- 空间效果（warper/mask/qualifier/grain 等）不可导出的有损性有 delivery matrix 显式确认流程兜底（P1-COLOR-25，deliver_controller 集成）
- cleanup 不使用 QThread.terminate：quit + 有界 wait + 二次 quit 补偿 + 日志告警（符合 P0-COLOR-01 崩溃治理）
- `_apply_3dlut_cpu` 的 SDR 查表 + HDR 高光比例缩放 + 负值加性回加与 GLSL apply_3dlut 语义对齐（注释声明镜像，实现一致）
- `_has_active_mixer_params`/`_has_active_curves_lut`/`_has_active_hsl_curve_lut` 对非 dict/畸形 ndarray 有 try/except 防护，无裸崩溃
- generate_lut_cpu 的 except 会经 error 信号上抛（非完全静默），错误路径不残留半写文件以外的状态（文件由 with 关闭）
- Color Dodge/Burn/Divide 的 0.001 epsilon 与 blend 回退 Normal 的行为与 GLSL 侧约定一致

---

## src/features/color/core/msl_composition.py (行数: 249)

### 发现

- [AUD-0720] [Suggestion] 效果发射顺序完全信任调用方传入的 active_effects 顺序，不按声明的 composition 顺序排序或校验
  - 位置: src/features/color/core/msl_composition.py:222-223（对照 :123-124 注释声明的固定顺序）
  - 证据:
    ```python
    # 管线位 → 调用语句。顺序 = composition main 的效果顺序（GL 侧：
    # blur_sharpen(fx 关→直通) → grading → curves → hsl_curve → soft_clip
    # → mixer → warper → lut3d）——blur_sharpen 恒为管线第一位。
    ...
    body_lines = [f"    {_PIPELINE_CALLS[e]}" for e in active_effects
                  if e in _PIPELINE_CALLS]
    ```
  - 影响: docstring 只约定"按 composition 顺序"传入但无运行期排序/断言；调用方一旦乱序传参（如 `("curves", "grading")`），生成的 MSL 效果次序与 GL 管线不同，输出静默数值分歧——与模块自身 G4（无静默部分状态）的设计目标相悖，且对拍测试可能因测试用例恰好按序而漏检。
  - 建议: 按 `_PIPELINE_CALLS` 的规范顺序重排 body_lines（或对乱序入参 assert/告警）。
  - 置信度: 中（分歧条件存在确定；实际调用方是否总是按序未在本批次验证）

- [AUD-0721] [Suggestion] blend_modes 位未启用时 opacity/matte 完全不参与输出，opacity<1 的管线与 GL 基准分歧且无守卫
  - 位置: src/features/color/core/msl_composition.py:224-231（对照 :19-21 docstring 的等价性前提）
  - 证据:
    ```python
    if "blend_modes" in active_effects:
        body_lines.insert(0, _MSL_FRAGMENT_MASK_SAMPLE)
        body_lines.append(
            "    float3 blended = msl_apply_blend_mode(tex_color.rgb, "
            "color, params.u_blend_mode);")
        body_lines.append(
            "    color = mix(tex_color.rgb, blended, "
            "mask * params.u_opacity);")
    ```
    docstring: "对拍 harness 以 Normal blend + 白 matte + opacity 1 的 GL 输出为基准，二者数学等价"
  - 影响: 不含 blend_modes 位的管线变体直接 `return float4(color, tex_color.a)`，`u_opacity` 被静默忽略；一旦上游用该变体渲染半透明图层，与 GL 输出不一致（docstring 已声明等价前提，但无运行期断言/文档外的防护）。
  - 建议: 在 emit 时对 "opacity≠1 且未启用 blend_modes" 的组合打日志或断言；或无条件发射 mix 尾句（matte 采样以白纹理兜底）。
  - 置信度: 中（限制已文档化，属契约硬度不足而非实现错误）

### 通过项
- 未迁移效果显式 ValueError（G4），无静默降级或跳过（:169-174）
- 内嵌 MSL 浮点字面量全部带 `.0`（vs_composition/epilog/管线调用逐句检查，无缺 `.0`、无隐式 int 提升）
- 无循环，不存在非常量循环边界问题；动态注入变量（tex_color/color/mask/blended）与效果函数作用域隔离，无命名冲突
- 输出无 clamp(0.0,1.0)，HDR 语义保留；uv 由固定全屏矩形 + linear/clamp sampler 契约保证不越界
- 纹理槽位 ABI 以显式属性固定（texture(0)-(5)），签名拼接顺序与槽位解耦，正确
- soft_clip 启用守卫（`u_sc_enabled != 0u`）与 GL main 同款；film_grain 的 `#pragma clang fp contract(off)` 按需注入且有实测依据注释
- 类型注解完整，模块边界与状态机说明清晰

---

## src/features/color/core/opengl_render_backend.py (行数: 200)

### 发现

- [AUD-0730] [Suggestion] facade 创建的真实 GL 资源不登记入 self.registry 台账，脱离 release/report 视图
  - 位置: src/features/color/core/opengl_render_backend.py:123-149（对照 async_renderer.py:5937-5949 的登记实践）
  - 证据:
    ```python
    def create_texture(self, descriptor: TextureDescriptor) -> Any:
        """在 orchestrator 的共享 context 中创建纹理（LINEAR + clamp）。"""
        self._assert_render_thread("create_texture")
        ctx = self._require_ctx()
        tex = ctx.texture(...)
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        ...
        return tex
    ```
    （无 registry.register 调用；现有登记仅发生在 orchestrator 的 `_register_core_gpu_resources`，覆盖 run() 初始化的核心资源）
  - 影响: 模块头注释承诺"本类只做契约包装与资源台账"，但经 facade 创建的纹理/FBO 不进台账——release() 不覆盖、resource_report 不可见；当前 create_texture/create_target 无生产调用者（grep 验证仅 initialize/registry 被用），属潜伏缺口，Phase 4 切换后经 facade 创建的资源将成泄漏盲区。
  - 建议: create_texture/create_target 内部（或文档化契约中）补 registry.register（纯台账，release_fn=None 与现行模式一致）。
  - 置信度: 高（事实确定；当前无实际调用者，影响潜伏）

- [AUD-0731] [Suggestion] release() 不做渲染线程守卫，registry.release_all 会在调用线程执行 release_fn，与 create_* 的严格守卫不对称
  - 位置: src/features/color/core/opengl_render_backend.py:108-117
  - 证据:
    ```python
    def release(self) -> None:
        """清空资源台账并标记 RELEASED（渲染线程调用）。

        注意：若登记时提供了 ``release_fn``，经 ``registry.release_all``
        会在调用线程上执行——渲染线程契约由调用方保证。
        """
        if self._state == GraphicsBackendState.RELEASED:
            return
        self.registry.release_all()
    ```
  - 影响: create_texture/create_target 有 `_assert_render_thread` 硬守卫（违例抛 BackendThreadViolationError），release 却只靠 docstring 约定；一旦 UI 线程调用 release 且台账中存在会触碰 GL 的 release_fn，即在错误线程执行 GL 释放（崩溃/驱动错误）。当前所有登记均 release_fn=None（async_renderer 实践），风险潜伏。
  - 建议: release() 同样套 `_assert_render_thread`，或至少在存在带 release_fn 的记录时强制守卫/打日志。
  - 置信度: 高（不对称事实确定；当前无实际 GL 释放回调）

### 通过项
- create_texture/create_target 有 `_assert_render_thread` 硬守卫，且 `AsyncRenderer` 为 QThread 子类（async_renderer.py:99 验证），以 orchestrator 对象做线程身份判定有效，UI 线程调用必被显式拒绝
- initialize() 的状态机显式: RELEASED → 报错（禁止隐式复用）、ctx 未就绪 → FAILED + BackendNotReadyError，无静默降级；唯一调用点在 AsyncRenderer.run() 内（渲染线程，async_renderer.py:467-470 验证）
- present() 只读描述符、不做 GL 调用；render_pass 经 orchestrator.submit 命令队列（既有跨线程安全入口），无平行渲染路径
- 纹理创建参数（LINEAR + CLAMP_TO_EDGE, repeat 关闭）与 GLContextManager 语义一致，避免 tile 边缘伪影
- 资源真实释放按 deferred 模式留给 AsyncRenderer._cleanup（渲染线程），本类只清台账——GPU 生命周期职责划分明确且有注释
- 类型注解完整；模块放置在 feature 侧的边界理由记录在案
