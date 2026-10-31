# 批次报告：batch-333 [P2] src/shared/export/

任务书：docs/audit/2026-09-28/assignments/batch-333.md
编号区间：AUD-33300–AUD-33399
审查文件：quick_export_dialog.py (1,326)、render_cache.py (264)、sequence_exporter.py (860)

统计：Critical 1 / Major 6 / Suggestion 11

关键验证手段（均为只读）：
- 项目 venv（.venv，Pillow 12.1.1）实测 `Image.fromarray(arr, mode='RGBA;16')` 抛 `TypeError: Cannot handle this data type: (1, 1, 4), <u2`；
- venv 实测 OpenEXR / Imath / imageio / tifffile 均未安装，且 pyproject.toml 与 requirements.txt 均未声明；
- 与 src/features/deliver/core/video_export_formats.py 的 `CODEC_CONTAINER_TABLE` 逐项对比；
- src/shared/media/frame.py 确认 DecodedFrame 契约（dtype 可为 uint8/uint16/float32；planes 2–4 为平面格式）。

---

## src/shared/export/quick_export_dialog.py (行数: 1,326)

### 发现

- [AUD-33301] [Major] 容器-编码表硬编码且已与 deliver 编解码注册表漂移
  - 位置: src/shared/export/quick_export_dialog.py:583
  - 证据:
    ```python
    # 复验 P1-08：Quick Export 与主 Deliver 同用档位 registry（此前只列
    # 四档，Pro444/Extreme 在快速导出不可达）；单一真相源 = capability_manifest
    _CONTAINER_CODECS = {
        "mp4": ["h264", "hevc"],
        "mov": ["h264", "hevc", "prores_proxy", "prores_lt", "prores_422",
                "prores_422hq", "prores_4444", "prores_4444xq",
                "topos_proxy", "topos_lt", "topos", "topos_hq",
                "topos_4444", "topos_4444xq", ...],
        "mkv": ["h264", "hevc"],
        "mxf": ["dnxhr_lb", "dnxhr_sq", "dnxhr_hq", "dnxhr_hqx", "dnxhr_444"],
    ```
  - 影响: 注释声称"单一真相源 = capability_manifest"，实际是本文件硬编码副本，且已与 src/features/deliver/core/video_export_formats.py 的 `CODEC_CONTAINER_TABLE` 漂移：deliver 允许 dnxhr 系列入 mov/mkv、prores 系列入 mkv/mxf，快速导出均未提供；deliver 的 avi/webm 容器在此完全缺失。两个导出入口能力不一致，且后续在注册表新增档位时此处必然再次漏更（P1-08 修过一次同类问题即为例证）。
  - 建议: 改为从 `video_export_formats.allowed_codecs_for_container()` / 档位 registry 派生（如 deliver 侧已有的 `CODEC_CONTAINER_TABLE`），快速导出仅做展示过滤，不再维护第二份列表。
  - 置信度: 高

- [AUD-33302] [Major] LUT 默认目录依赖 os.getcwd()，随进程工作目录漂移
  - 位置: src/shared/export/quick_export_dialog.py:492
  - 证据:
    ```python
    # LUT 路径（默认保存到 luts 目录）
    lut_default_dir = self.default_dir
    luts_dir = os.path.join(os.getcwd(), "luts")
    if os.path.exists(luts_dir):
        lut_default_dir = luts_dir
    ```
  - 影响: cwd 取决于应用启动方式（终端 cd、Finder/桌面启动、打包后 .app 均不同），运行中还可能被其他组件改动。"luts 目录是否存在"因此非确定：同一项目在不同启动方式下 LUT 默认导出位置不一致；若 cwd 恰好有同名 `luts` 目录，用户 LUT 会被写到无关位置。
  - 建议: 以项目目录/用户数据目录（config_manager 已有 default_export_path 体系）为锚定位 luts 目录，去除对 getcwd() 的依赖。
  - 置信度: 高

- [AUD-33303] [Suggestion] 本地化显示文本被用作语义映射键，与 userData 方案不一致
  - 位置: src/shared/export/quick_export_dialog.py:1176（另见 1228、1260、1295）
  - 证据:
    ```python
    fmt_display = self.format_combo.currentText()
    display_to_name = {
        I18N.tr("export.fmt_jpeg", "JPEG"): "JPEG",
        I18N.tr("export.fmt_png", "PNG"): "PNG",
        ...
    }
    fmt_name = display_to_name.get(fmt_display, "JPEG")
    ```
  - 影响: combo 项文本在构造时经 I18N.tr 生成，get_settings / on_video_format_changed 在导出时再次 I18N.tr 构造反查键。I18N 提供 `reload_locale`（i18n.py:297 注释明示"runtime 切换语言"用），一旦语言在两项之间切换，反查失配并静默回退默认值（JPEG/mp4/cube/aac），用户选择被无声丢弃。编码下拉已用 `userData=codec_id`（本文件 601-607 注释也点名了这一风险），格式/容器/LUT/音频四处仍是文本键。当前 `dialog.exec()` 模态用法恰好挡住了切换窗口，属侥幸而非设计保证。
  - 建议: 统一改用 `addItem(text, userData=内部id)` + `currentData()` 取值，删除全部显示文本反查表。
  - 置信度: 中

- [AUD-33304] [Suggestion] 宽高比联动基于原图尺寸且 int 截断，orig=(0,0) 时高度塌缩为 1
  - 位置: src/shared/export/quick_export_dialog.py:1117
  - 证据:
    ```python
    if sender == self.width_spin:
        # 根据宽度调整高度
        ratio = self.orig_h / max(1, self.orig_w)
        new_h = int(self.width_spin.value() * ratio)
    ```
  - 影响: deliver_controller.py:1021-1026 在未加载图像时以 original_size=(0,0) 构造本对话框，此时 ratio=0/1=0，拖动宽度会把高度钉死在 DragValue 下限 1。此外 int() 截断使反复拖动产生累积漂移。自定义分辨率生效后，联动比例仍取原图而非用户当前自定义值，用户无法导出非原比例的自定义尺寸（锁开关形同虚设）。
  - 建议: orig 尺寸无效时禁用联动；用 round 代替截断；锁定比基于当前有效基准对（进入自定义模式时快照）而非 orig。
  - 置信度: 高

- [AUD-33305] [Suggestion] 图像页文件名未做非空/非法字符校验
  - 位置: src/shared/export/quick_export_dialog.py:1189
  - 证据:
    ```python
    filename = self.name_input.text()
    if not filename.lower().endswith(ext):
        filename += ext
    ```
  - 影响: 输入为空时生成 ".jpg"（隐藏文件）；输入含 `/:` 等平台非法字符时导出路径非法，写文件阶段才报错。视频页对 timeline_name 做了 invalid_chars 清理（627-631 行），说明项目已有该意识，但用户手输的图像/LUT 文件名未覆盖。
  - 建议: get_settings 前对三页文件名统一做 strip + 非法字符替换 + 空名回退（LUT/视频页已有空名回退，可复用）。
  - 置信度: 高

- [AUD-33306] [Suggestion] 公共方法与 get_settings 缺类型注解
  - 位置: src/shared/export/quick_export_dialog.py:1156
  - 证据:
    ```python
    def get_settings(self):
        """
        获取导出设置。
    ```
  - 影响: setup_ui / setup_image_page / on_tab_changed / get_settings 等对外契约方法均无参数与返回类型注解；get_settings 返回结构是三方（dialog/deliver_controller/LUT 导出）隐式契约，缺注解加重漂移风险。__init__ 的可选参数亦无注解。
  - 建议: 至少为 get_settings 补 `-> dict[str, Any]`，slots 方法补 `-> None`。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 控件，仅在 UI 线程构造与访问（deliver_controller.py:1056 于 UI 线程创建，exec() 模态），无跨线程共享状态。
- 信号防递归：slider↔DragValue 双向同步（376-385）、宽高联动（1120-1129）、编码下拉重建（926-938）均正确使用 blockSignals；`_update_gif_visibility` 的 setChecked 触发 on_video_audio_toggled 但后者只 setEnabled/setVisible，无再入环。
- GPU 资源生命周期：不涉及 OpenGL/ModernGL 对象。
- GLSL/HDR 正确性：不涉及着色器；EXR 精度/压缩选项仅透传索引。
- 异常与取消路径：标准 QDialog accept/reject；GIF 强制 include_audio=False（1293 行）正确防住无音频容器带音频导出。
- 状态提交一致性：不涉及 undo/redo/revision/持久化（偏好读取经 config_manager，方向单向）。

---

## src/shared/export/render_cache.py (行数: 264)

### 发现

- [AUD-33310] [Suggestion] get() 检测到损坏条目只计数不清理，失败被重复支付
  - 位置: src/shared/export/render_cache.py:201
  - 证据:
    ```python
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        self._corruptions += 1
        self._misses += 1
        return None
    ```
  - 影响: 损坏的 .bin/.json 留在磁盘，同一 key 每次导出调度都重新读取（4K 段可能数十 MB）+ sha256 全量计算后才判定失败；`corruptions` 计数随访问次数而非损坏条目数增长，stats 失真，且 put() 可修复但 get() 从不触发删除。
  - 影响: 建议 get() 判定损坏时顺带 unlink 两个文件（或标记待清理），使失败只付一次。
  - 建议: 损坏即删（本就设计为可重算的缓存）；如担心误删可仅记入待清理列表。
  - 置信度: 高

- [AUD-33311] [Suggestion] 不可结构化字段回退 repr，digest 可能含对象地址而不稳定
  - 位置: src/shared/export/render_cache.py:35
  - 证据:
    ```python
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)
    ```
  - 影响: 未实现 to_dict/.value 的自定义对象（如直接塞进 effects/grade 的配置对象）经 repr 序列化：默认 repr 含 `0x7f...` 地址 → 同一语义输入跨进程/跨实例 digest 不同，磁盘缓存永不命中（只多不少的 IO）；两个不同对象又可能因相同 repr 碰撞误命中。`_freeze`（38-54 行）对这些对象同样原样保留，hash 稳定性取决于实现。
  - 建议: 对无法结构化的值显式 raise（要求调用方传纯数据/to_dict 对象），或至少断言 repr 不含 " at 0x"。
  - 置信度: 中

- [AUD-33312] [Suggestion] invalidate() 只捕 FileNotFoundError，Windows 被占用文件抛 PermissionError 直接上抛
  - 位置: src/shared/export/render_cache.py:243
  - 证据:
    ```python
    for path in (data_path, meta_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    ```
  - 影响: 模块 docstring 明示面向 "Windows export scheduling"；Windows 下文件被其他句柄（杀毒扫描、另一导出进程）打开时 unlink 抛 PermissionError，未被捕获，沿失效调用链上抛可能中断导出调度。
  - 建议: 增加 `except PermissionError: 记日志并跳过`（失效失败的条目最终会被 get() 的损坏路径兜住）。
  - 置信度: 中

### 通过项
- 线程安全：单个 RLock 覆盖全部 stats 读写与文件 IO，get/put/invalidate/stats 临界区完整，无锁外共享可变状态。
- 输入验证：SegmentRenderKey.__post_init__ 校验 fingerprint/帧范围/source_kind/版本字段与 delivery×source_kind 组合；from_mapping 校验缺失字段并强转 in/out_frame；get() 三重校验 key_digest/sha256/size；put() 拒绝非 bytes。
- 一致性（P2-17）：构造期 _freeze 归一化使 __eq__/__hash__/digest 三者对 tuple/list/dict 等价形式判定一致，to_dict/digest 经 _canonical 输出确定序 JSON。
- 原子写：mkstemp 同目录 + fsync + os.replace，崩溃时不会留半写文件污染命中路径（残留 .tmp 会被 finally 清理）。
- 类型注解：完整。
- GPU 资源生命周期：注释明确只存编码产物不存 GPU 句柄，实现相符。

---

## src/shared/export/sequence_exporter.py (行数: 860)

### 发现

- [AUD-33320] [Critical] packed 整数帧数据不归一化，uint8 输入导出为全白/255 倍过曝图像
  - 位置: src/shared/export/sequence_exporter.py:332
  - 证据:
    ```python
    if frame.is_packed and frame.data is not None:
        data = frame.data
        # Ensure RGBA format
        if data.shape[-1] == 3:
            alpha = np.ones(data.shape[:2], dtype=data.dtype)
            data = np.dstack([data, alpha])
        return data
    ```
    而 `_apply_color_policy` 假定 `data: RGBA float array`（[0,1] 域，417-424 行），`_export_png` 直接 `np.clip(data, 0.0, 1.0)`（649 行）。
  - 影响: DecodedFrame 契约明确 packed data 的 dtype 可为 np.uint8/uint16（src/shared/media/frame.py:250），本函数对整数 dtype 原样返回、全程无 /max_val 归一化。uint8 帧走 PNG/JPG 路径时 clip(0,255→1) 输出近乎纯白；走 EXR(SCENE_LINEAR) 路径时 uint8 值以 0-255 线性值写入 float16 EXR——亮度放大 255 倍的垃圾 HDR，属静默渲染错误。缓解因素：本模块当前在仓库内无实际调用方（仅 __init__.py 再导出），缺陷处于潜伏状态；一旦按其文档接口（"frame: DecodedFrame instance (from media module)"）接入即触发。
  - 建议: `_extract_frame_data` 对整数 dtype 统一 `astype(np.float32) / max_val`（按 frame.bit_depth），并在入口断言 float 域；`processed_data` 路径同样校验。
  - 置信度: 中（代码缺陷确定性高；按 frame.py 契约可达，但当前无调用方）

- [AUD-33321] [Major] `_planar_to_rgba` 少于 3 平面时 u/v 未绑定，帧导出必然抛错
  - 位置: src/shared/export/sequence_exporter.py:367
  - 证据:
    ```python
    # U, V planes (may be subsampled)
    if len(planes) >= 3:
        u = planes[1].astype(np.float32)
        v = planes[2].astype(np.float32)
    ...
    max_val = (2 ** frame.bit_depth) - 1 if frame.bit_depth else 255
    y_norm = y / max_val
    u_norm = u / max_val - 0.5
    v_norm = v / max_val - 0.5
    ```
  - 影响: frame.py:39 定义平面格式为 2-4 个平面（2 = Y+A 之类）。len(planes)==2 时跳过 u/v 赋值，395 行直接引用触发 UnboundLocalError，被 export_frame 的 except Exception 包装为 IOError("Frame export failed")——灰度/双平面帧全部无法导出，且报错信息不指明根因。
  - 建议: planes<3 时按灰度处理（u=v=0 或复制 Y），或显式 raise 带原因的 ValueError。
  - 置信度: 高

- [AUD-33322] [Major] 16-bit PNG 使用 Pillow 不支持的 'RGBA;16' 模式，导出必失败（已实测复现）
  - 位置: src/shared/export/sequence_exporter.py:654
  - 证据:
    ```python
    if self._bit_depth >= 16:
        # 16-bit PNG
        data_16 = (data_clipped * 65535).astype(np.uint16)
        img = Image.fromarray(data_16, mode='RGBA;16')
    ```
  - 影响: 项目 venv（Pillow 12.1.1）实测 `Image.fromarray(uint16 RGBA, mode='RGBA;16')` 抛 `TypeError: Cannot handle this data type: (1, 1, 4), <u2`。Pillow 不支持 16-bit RGBA PNG 编码。凡 format='png' 且 bit_depth>=16 的导出 100% 失败（export_frame 转 IOError 上抛）。
  - 建议: 16-bit RGBA PNG 改经 tifffile/imageio 或手写 PNG 编码；无可用后端时显式报"不支持"而非让 TypeError 逃逸。
  - 置信度: 高（已实测）

- [AUD-33323] [Major] EXR/TIFF/DPX 依赖链整体缺失且未声明，默认 vfx 推荐路径在当前环境必然失败
  - 位置: src/shared/export/sequence_exporter.py:544
  - 证据:
    ```python
        except ImportError:
            # Fallback: try imageio or PIL
            logger.warning("OpenEXR not available, using fallback")
            self._export_imageio_exr(filepath, data)
    ```
    及 607-613 行 16-bit TIFF 路径：
    ```python
    try:
        import tifffile
        tifffile.imwrite(filepath, data_16)
    except ImportError:
        import imageio
        imageio.imwrite(filepath, data_16)
    ```
  - 影响: venv 实测 OpenEXR、Imath、imageio、tifffile 全部未安装，requirements.txt / pyproject.toml 也未声明其中任何一个（仅 pillow）。`create_sequence_exporter(use_case='vfx')`（EXR/16bit，模块默认推荐配置）的完整链路为：OpenEXR 缺→imageio 缺→TIFF→tifffile 缺→在 except ImportError 处理器内再 `import imageio` 抛出新 ImportError（此处无人捕获，落入 _export_tiff 外层宽泛 except）→PNG 16-bit→'RGBA;16' TypeError→IOError。整条默认路径不可用；`_export_dpx` 同样依赖 imageio。嵌套 except ImportError 内再次 import 缺失模块的写法使回退自身成为故障源。
  - 建议: 依赖加入 requirements（或启动时探测并禁用相关格式）；回退链改为单次能力探测（初始化时缓存可用后端），不在异常处理器内 import。
  - 置信度: 高（已实测依赖缺失）

- [AUD-33324] [Major] 格式降级静默更换实际文件，返回/记录路径与磁盘不符；宽泛 except 掩盖领域错误
  - 位置: src/shared/export/sequence_exporter.py:622
  - 证据:
    ```python
    except Exception as e:
        logger.error(f"TIFF export failed: {e}")
        # Fallback to PNG
        png_path = filepath.replace('.tiff', '.png').replace('.tif', '.png')
        self._export_png(png_path, data, frame)
    ```
    export_frame 随后仍 `self._exported_files.append(filepath)`（313 行，原 .tiff 路径）。同型降级：EXR→.tiff（566-567）、DPX→.tiff（729-730）。
  - 影响: 三处降级都会把用户请求的 HDR 级格式（EXR/TIFF/DPX）静默换成 LDR PNG/TIFF（8-bit、clip [0,1]），且 export_frame 返回并登记的是原始扩展名路径——磁盘上并不存在。`get_export_stats` 统计不存在的文件（os.path.exists 过滤后 total_bytes 恒 0），`cleanup()` 因路径不存在跳过 unlink，真实写出的降级文件永久残留。外层 `except Exception` 直接违反"不得宽泛 except 掩盖领域错误"铁律：磁盘满、权限、数组形状错误全部被吞成格式替换。
  - 建议: 降级要么显式上抛携带原因的异常，要么改写 filepath 并把真实路径传入 _exported_files；TIFF 外层 except 至少排除 OSError 中的关键类别并记录目标格式变化。
  - 置信度: 高

- [AUD-33325] [Suggestion] 色度上采样假设整数倍缩放，非整除分辨率形状失配
  - 位置: src/shared/export/sequence_exporter.py:373
  - 证据:
    ```python
    if u.shape != (h, w):
        scale_h = h // u.shape[0]
        scale_w = w // u.shape[1]
        u = np.repeat(np.repeat(u, scale_h, axis=0), scale_w, axis=1)
    ```
  - 影响: 奇数高度（如 1081）配 4:2:0 时 h//u.shape[0] 余 1，repeat 后 u 为 1080 行 ≠ h，后续 np.stack 形状不匹配抛 ValueError → 帧导出失败。scale 为 0（u 平面异常大）时更会产出空数组。
  - 建议: 用 np.kron/插值到精确 (h, w)，或对余数行/列做边缘补齐。
  - 置信度: 中

- [AUD-33326] [Suggestion] LOG 色彩策略实现与注释公式不符
  - 位置: src/shared/export/sequence_exporter.py:465
  - 证据:
    ```python
    # Cineon Log encoding (for DPX)
    # Log V = (685 * (10^(0.0026 * L - 0.073)) - 685) / 1023
    # Simplified approximation
    ...
    log_val = np.log10(rgb + 0.01) * 0.5 + 0.5
    rgb = np.clip(log_val, 0.0, 1.0)
    ```
  - 影响: 注释给出 Cineon 标准公式，实现是另一条粗略近似（黑位抬 0.01、动态域 clip 到 [0,2]），两者输出色值显著不同；DPX 交付物按标准 Cineon 解算会偏亮/偏灰，且黑位不为 0。
  - 建议: 实现注释中的 Cineon 公式（或注明近似仅用于预览），并去掉对 HDR 输入的 [0,2] 预 clip 或说明理由。
  - 置信度: 高

- [AUD-33327] [Suggestion] cleanup() 先 clear 后取 len，清理计数恒为 0
  - 位置: src/shared/export/sequence_exporter.py:800
  - 证据:
    ```python
    self._exported_files.clear()
    logger.info(f"Cleaned up {len(self._exported_files)} exported files")
    ```
  - 影响: 日志恒报 "Cleaned up 0 exported files"，误导排查（与 AUD-33324 叠加时更甚——记录路径本就不存在）。
  - 建议: 先取 len 存局部变量再 clear。
  - 置信度: 高

- [AUD-33328] [Suggestion] SequenceExportConfig/metadata/start_frame 为死代码；FrameInput 类型别名无意义
  - 位置: src/shared/export/sequence_exporter.py:108
  - 证据:
    ```python
    @dataclass
    class SequenceExportConfig:
        ...
        metadata: dict = field(default_factory=dict)
        overwrite: bool = True
        start_frame: int = 0
    ```
    及 80 行 `FrameInput = Union[np.ndarray, Any]  # DecodedFrame or raw numpy array`。
  - 影响: SequenceExportConfig 在全仓库无使用（__init__ 只收平铺参数），其 metadata 嵌入、start_frame 偏移均未实现——文档宣称的能力实际不存在。`Union[np.ndarray, Any]` 被 Any 吸收，等于无约束，且该别名本身也无人引用。
  - 建议: 删除死代码或在 __init__ 真正接入 Config；FrameInput 删除或改 `Union[np.ndarray, "DecodedFrame"]`。
  - 置信度: 高

### 通过项
- 线程安全：无可变共享状态跨线程暴露的证据；_exported_files 为实例内单线程追加（导出任务各自持实例）。
- GPU 资源生命周期：不涉及 OpenGL/ModernGL。
- GLSL：不涉及着色器。
- SCENE_LINEAR 策略直通不 clip（427-429 行），EXR HDR 直写 float16/32 符合"HDR 内部数据不得无理由 clamp"；PNG/JPG 8/16-bit 整数格式的 clip 属格式固有能力，有理由。
- generate_filename 的异常处理收窄到 (ValueError, KeyError) 并回退默认命名，符合窄异常原则。
- _export_jpg 正确丢弃 alpha 并 4:4:4 子采样；压缩/质量参数按格式复用已在 docstring 声明。
- get_export_stats 对不存在文件做 os.path.exists 过滤，避免统计抛错（但见 AUD-33324 的路径失真影响）。
