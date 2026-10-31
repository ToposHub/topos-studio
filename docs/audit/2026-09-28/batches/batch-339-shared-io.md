# 批次报告：batch-339 [P2] src/shared/io//

- 编号区间：AUD-33900–AUD-33999（实际使用 AUD-33901–AUD-33915）
- 审查文件：`src/shared/io/__init__.py` (3)、`src/shared/io/file_io.py` (981)
- 统计：Critical 1 / Major 5 / Suggestion 9
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失

---

## src/shared/io/__init__.py (行数: 3)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export，`from src.shared.io.file_io import FileIO`，`__all__ = ["FileIO"]`。

---

## src/shared/io/file_io.py (行数: 981)

### 发现

- [AUD-33901] [Critical] 预览缓存 EXR 仍用 ZIP(3) 压缩——与 T-P1-71 已文档化的 libdeflate SIGILL 崩溃修复相矛盾
  - 位置: src/shared/io/file_io.py:477
  - 证据:
    ```python
    # _cache_preview（预览缓存写入，477 行）:
                if preview_img.dtype == np.uint16:
                    save_img /= 65535.0
                params = [cv2.IMWRITE_EXR_TYPE, 1, cv2.IMWRITE_EXR_COMPRESSION, 3]

    # 对照 save_image 中 T-P1-71 修复注释（849-854 行）:
                # T-P1-71（libdeflate SIGILL）：默认 exr_compression 由 ZIP(3)
                # 改为 RLE(1)——opencv 轮子的 libOpenEXR 走 ZIP/PIZ 时经
                # libdeflate 派发，本机间歇性非法指令崩溃（.ips 栈
                # libdeflate_zlib_compress ← cv2.imwrite）。
    ```
  - 影响: 所有 16 位/浮点源（RAW `output_bps=16`、16 位 TIFF/PNG）首次加载都会经 `_cache_preview` 走 ZIP 压缩 EXR 编码——正是本仓库文档记载的间歇性非法指令崩溃路径。T-P1-71 只修了 `save_image`，预览缓存路径未同步，受影响机器上每次首次导入 16 位素材都可能触发进程级 SIGILL 崩溃。
  - 建议: `_cache_preview` 的 EXR 压缩改为 RLE(1)（与 save_image 默认一致）；`save_baked_layer` 硬编码的 DWAB(9)（第 83 行）也应确认不受 libdeflate 派发影响后统一。
  - 置信度: 中（SIGILL 为间歇性/环境相关，但崩溃风险由本文件自身注释文档化，且该路径未应用修复的不一致性为高置信）

- [AUD-33902] [Major] `save_image` 宽泛 except 吞掉 `_save_toos_image` 设计性 ValueError，fail-closed 错误文案经公共 API 永不可达
  - 位置: src/shared/io/file_io.py:866
  - 证据:
    ```python
    # save_image（843-847 行调用在 try 内）:
                return FileIO._save_toos_image(path, image, options)
            ...
        except Exception: return False

    # _save_toos_image 精心设计的用户可见错误（918-920 行）:
                raise ValueError(
                    f"RAW 档 {tier.tier_id!r} 要求 CFA 源直出：Quick Export "
                    f"静帧来自 RGB 合成域，请选择整数/浮点档")
    ```
  - 影响: 全仓库调用方（`composite_controller.py:1093`、`deliver_controller.py:162/212/1737`、`composite_render_output.py:663/1055`、`scripting/api.py:469`）均只消费 bool 返回值；`ValueError` 是 `Exception` 子类，"请选择整数/浮点档"、"请去除 alpha 或选整数档"（937-939 行）等面向用户的 fail-closed 提示被静默转为 `False`，用户只看到导出失败而不知原因，也无任何日志。
  - 建议: `except ValueError: raise`（透传设计性错误），或至少 `logger.exception` 记录原始异常后再返回 False。
  - 置信度: 高

- [AUD-33903] [Major] RAW 单例缓存错误路径留下悬挂状态（已 close 对象 + 旧路径），此后旧路径永久解码失败
  - 位置: src/shared/io/file_io.py:799-804（同型问题 340-345）
  - 证据:
    ```python
        with FileIO._raw_lock:
            if FileIO._current_raw_path != path:
                if FileIO._current_raw_obj: FileIO._current_raw_obj.close()
                try:
                    FileIO._current_raw_obj = rawpy.imread(path)
                    FileIO._current_raw_path = path
                except Exception: return None, None, None, raw_params
    ```
  - 影响: `rawpy.imread` 失败时 `_current_raw_obj` 仍是已 close 的旧对象、`_current_raw_path` 仍是旧路径。之后再次请求旧路径时路径相等跳过重开，对已关闭对象调用 `postprocess` 必然抛错并返回 None——此前可正常解码的文件从此持续失败，直到加载第三个不同路径才被清理。`_read_full_image`（340-345 行）同型：imread 抛出经外层 except 返回 None，状态同样悬挂。
  - 建议: 失败分支将 `_current_raw_obj = None; _current_raw_path = None`，使下次调用强制重开。
  - 置信度: 高

- [AUD-33904] [Major] `_save_toos_image` 异常路径不保证 `encoder.close()`，绕过编码器"close 覆盖全部退出路径"的资源契约
  - 位置: src/shared/io/file_io.py:971-974
  - 证据:
    ```python
        try:
            enc = ToposImageSequenceEncoder(tmp_dir, cfg)
            enc.open()
            enc.encode_frame(frame)
            enc.close()
            produced = _os.path.join(tmp_dir, "frame_0001.toos")
    ```
  - 影响: `open()`/`encode_frame()` 抛异常（如不支持的尺寸/编码错误）时 `close()` 不执行。`ToposVideoEncoder.close()`（topos_encoder.py:1593 起）负责 `_restore_slice_threads()`（恢复进程级线程数设置）、pipeline drain 与 GOP context 释放（其 C13 注释明确要求覆盖全部退出路径），调用方跳过 close 违反该契约，可能遗留进程级 slice-thread 设置与 native 编码上下文。
  - 建议: 用 try/finally（或 contextmanager 包装）保证 close 被调用；close 自身失败按其设计仅告警。
  - 置信度: 中（正常路径不受影响；异常路径的具体泄漏程度依赖 close 内部实现，未逐行验证 native 层）

- [AUD-33905] [Major] `save_image` 直接 `cv2.imwrite` 写路径，与全文件"imencode+tofile 处理 Windows Unicode 路径"的既有策略不一致
  - 位置: src/shared/io/file_io.py:865（另见 855、858）
  - 证据:
    ```python
    # save_image 导出路径（865 行）:
            else: params = []
            return cv2.imwrite(path, u8_image, params)

    # 对照同文件 _cache_preview（468-471 行）:
                # Use imencode + tofile to handle Unicode paths on Windows
                encoded = cv2.imencode(ext, preview_img, params)
                if encoded[0]:
                    encoded[1].tofile(cache_path)
    ```
  - 影响: Windows 上导出到含非 ASCII 字符的文件名/目录（中文用户常见）时 `cv2.imwrite` 无法打开文件返回 False，导出静默失败；EXR/TIFF/JPEG/PNG 全部分支均受影响（仅 `.toos` 分支经 `shutil.move` 无此问题）。
  - 建议: 导出分支统一改用 `cv2.imencode` + `tofile`。
  - 置信度: 中（平台相关；当前 macOS 开发机不触发，但代码内多处注释表明明确支持 Windows）

- [AUD-33906] [Major] TIFF 导出对 float 图像无条件 `np.clip(image, 0, 1)`，>1.0 高光数据被静默裁剪
  - 位置: src/shared/io/file_io.py:857
  - 证据:
    ```python
            if ext in ['.tiff', '.tif']:
                save_data = (np.clip(image, 0, 1) * 65535).astype(np.uint16) if np.issubdtype(image.dtype, np.floating) else image
                return cv2.imwrite(path, save_data)
    ```
  - 影响: HDR 合成结果（float32，场景线性，高光可 >1.0）导出 TIFF 时高光被裁到 1.0 再量化，违背"HDR 数据不得无理由 clamp(0.0,1.0)"铁律；TIFF 格式本身可承载 float32（cv2 支持 32F TIFF），且裁剪无任何日志/警告，用户无从得知数据丢失。
  - 建议: 提供 float32 TIFF 写出选项；至少保留 uint16 路径时记录被裁警告。
  - 置信度: 中（uint16 量化需要范围映射，但静默裁剪 HDR 高光属数据丢失；EXR 分支无 clamp 是正确对照）

- [AUD-33907] [Suggestion] 预览缓存命中/未命中返回的 dtype 与值域不一致（uint16 0-65535 vs float32 0-1）
  - 位置: src/shared/io/file_io.py:473-481（对照 305-320）
  - 证据:
    ```python
    # 未命中：full/high/low 均为解码原始 dtype（RAW/16位图为 uint16）
                low_img = FileIO._resize_to_fit(full_img, FileIO.LOW_RES_SIZE)

    # 命中：uint16 预览被归一化为 float32 后存 EXR，读回即 float32 [0,1]
                save_img = preview_img.astype(np.float32)
                if preview_img.dtype == np.uint16:
                    save_img /= 65535.0
    ```
  - 影响: 同一 16 位源文件两次加载，`load_image` 返回的 high/low 一次是 uint16（0-65535）、一次是 float32（0-1），API 契约随缓存状态漂移。当前下游 `image_engine.set_image` 的 `normalize_to_f32`（image_engine.py:1980-1985）按 dtype 分支归一化未出错，但任何假定固定 dtype 的新消费方都会踩坑，属潜在脆弱性。
  - 建议: 在 `_get_cached_preview` 读回后按需还原 dtype，或在 `load_image` 出口统一归一化 dtype/值域。
  - 置信度: 高（不一致客观存在；当前影响为潜在，因下游已做 dtype 感知归一化）

- [AUD-33908] [Suggestion] `_read_full_image` 文件句柄未用 with 关闭
  - 位置: src/shared/io/file_io.py:365-367
  - 证据:
    ```python
            else:
                stream = open(path, "rb")
                bytes_data = bytearray(stream.read())
                numpyarray = np.asarray(bytes_data, dtype=np.uint8)
    ```
  - 影响: `stream` 依赖函数退出时的引用计数回收关闭；异常期间 traceback 持有帧会延长 FD 存活，Windows 上 FD 打开期间会阻止同文件的移动/删除。CPython 下为瞬时泄漏，非累积性。
  - 建议: 改为 `with open(path, "rb") as stream:`。
  - 置信度: 高

- [AUD-33909] [Suggestion] 裸 `except:` 捕获 BaseException（两处），违背项目"宽泛 except"铁律
  - 位置: src/shared/io/file_io.py:442（另见 552）
  - 证据:
    ```python
                try:
                    img_data = np.fromfile(cache_path, dtype=np.uint8)
                    return cv2.imdecode(img_data, cv2.IMREAD_UNCHANGED)
                except:
                    continue
    ```
  - 影响: 裸 except 连 `KeyboardInterrupt`/`SystemExit` 一并吞掉，且掩盖缓存文件损坏等真实错误原因（无日志）。552 行同型（Exp. Comp. 数值解析）。降级为 `except Exception` 即可消除关停信号被吞风险。
  - 建议: 两处改为 `except Exception:`，缓存读取处可加 `logger.debug` 记录损坏文件名。
  - 置信度: 高

- [AUD-33910] [Suggestion] `load_image` 建缓存目录为 TOCTOU 竞态写法，无 `exist_ok=True`
  - 位置: src/shared/io/file_io.py:295-296
  - 证据:
    ```python
        if not os.path.exists(FileIO.CACHE_DIR):
            os.makedirs(FileIO.CACHE_DIR)
    ```
  - 影响: 首次运行时若两个线程（如导出 worker 与 GUI 打开图像）同时通过 exists 检查，后到者 `makedirs` 抛 `FileExistsError` 且不被捕获，异常直接冒泡到调用线程。同文件 `_cache_preview`（456 行）已正确使用 `exist_ok=True`，写法不一致。
  - 建议: 改为 `os.makedirs(FileIO.CACHE_DIR, exist_ok=True)`。
  - 置信度: 中（竞态窗口仅限缓存目录不存在时；并发调用场景未完全证实）

- [AUD-33911] [Suggestion] `save_baked_layer` 文档声称"保留 32 位浮点精度"但实际写 HALF(1) 16 位 EXR
  - 位置: src/shared/io/file_io.py:59（对照 81-83）
  - 证据:
    ```python
        This preserves 32-bit floating point precision while minimizing file size.
        ...
            # - exr_type=1 (HALF float - 16-bit, good balance of precision and size)
            params = [cv2.IMWRITE_EXR_TYPE, 1, cv2.IMWRITE_EXR_COMPRESSION, 9]
    ```
  - 影响: docstring 与实现自相矛盾；实际精度为 half（约 11 位尾数），baked AI 层高光细节数据存在低于预期的量化。行为本身可能是刻意取舍，但文档误导后续维护者。
  - 建议: 修正 docstring，或如需真 32 位精度改 `IMWRITE_EXR_TYPE=2`（FLOAT）。
  - 置信度: 高（矛盾客观；量化影响程度未量化评估）

- [AUD-33912] [Suggestion] `_save_toos_image` 中 a16 `pix_fmt` 分支为不可达死代码
  - 位置: src/shared/io/file_io.py:947-949
  - 证据:
    ```python
        if has_alpha and tier.family == 'float':   # 932 行：fail-closed raise
            raise ValueError(...)
        ...
        pix_fmt = tier.pix_fmt                      # 946 行
        if has_alpha and tier.family == 'float':    # 947 行：永假
            pix_fmt = 'topos_gbraph16a16'
    ```
  - 影响: 932-939 行已对 `has_alpha and tier.family == 'float'` 无条件 raise，947-949 行同条件分支永不执行，`topos_gbraph16a16` 覆盖逻辑为死代码，误导读者以为该路径可达。
  - 建议: 删除 947-949 行，或在 raise 处保留说明性注释指向未来开放 a16 时的实现位置。
  - 置信度: 高

- [AUD-33913] [Suggestion] `_apply_raw_tone_and_detail` 全仓库无调用（死代码）
  - 位置: src/shared/io/file_io.py:734
  - 证据:
    ```python
        @staticmethod
        def _apply_raw_tone_and_detail(img, params):
            if img is None: return None
            fimg = img.astype(np.float32)
    ```
  - 影响: 56 行的 RAW 调色实现（含 clip(0,1) HSV 处理）在 src/ 与 tests/ 下均无任何调用点（已 grep 验证仅此定义处），维护成本与误导风险。
  - 建议: 确认弃用后删除，或迁移至实际使用 RAW 参数处理的模块。
  - 置信度: 高

- [AUD-33914] [Suggestion] `update_raw_decoding` 两处 `except Exception` 静默返回，无日志
  - 位置: src/shared/io/file_io.py:804、822
  - 证据:
    ```python
                except Exception: return None, None, None, raw_params
            ...
            except Exception: return None, None, None, raw_params
    ```
  - 影响: RAW 参数调整（WB/预览）失败时无任何诊断信息，用户只看到画面不更新；同函数 rawpy 缺失分支（793-795 行）有 `logger.error`，风格不一致，排障困难。
  - 建议: 两处加 `logger.exception("raw decode failed: %s", path)` 后再返回。
  - 置信度: 高

- [AUD-33915] [Suggestion] 公共方法大量缺失类型注解
  - 位置: src/shared/io/file_io.py:288、510、792、831
  - 证据:
    ```python
        def load_image(path):          # 288 行，无参数/返回注解
        def get_metadata(path):        # 510 行
        def update_raw_decoding(path, raw_params, preview_only=False):  # 792 行
        def save_image(path, image, options=None):                      # 831 行
    ```
  - 影响: 作为 shared 层公共 IO API，与同文件已注解的 `save_baked_layer(filename: str, image_array: np.ndarray) -> bool` 风格不一，返回 `(np.ndarray|None, np.ndarray|None, np.ndarray|None)`、`(dict)`、`bool` 等契约只能靠读实现确认。
  - 建议: 补齐参数与返回类型注解（tuple 用 `Optional[np.ndarray]` 展开或 TypeAlias）。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：本文件不持有 ModernGL/GL 对象（纯 CPU 侧 EXR/图像缓存），GPU 纹理上传由调用方（async_renderer/image_engine）完成，无越界 GL 调用。
- 信号防递归：纯静态工具类，无 Qt 信号/控件，不适用。
- 状态提交一致性：不涉及 undo/redo/revision 栈；baked cache 的 save/load/delete/cleanup_orphaned 围绕同一 BAKED_CACHE_DIR，文件名契约一致（`load_ai_plugin_result` 的 BGR 契约有明确注释并与 image_engine.py:1662 的消费注释互证）。
- 线程安全（常规路径）：RAW 单例缓存访问均持 `_raw_lock`，无嵌套获取、无死锁；缓存损坏可经 `_get_cached_preview` 回退自愈（缺陷仅在错误路径，见 AUD-33903）。
- 输入验证：rawpy/PSDImage 可选依赖缺失时返回明确失败并记录日志，不阻断普通图像导入；文件存在性检查（load_image/get_metadata/update_raw_decoding）到位；exiftool 子进程用列表参数无 shell 注入且有 2s timeout。
- HDR 正确性（读路径）：EXR/RAW 读入全程无 clamp；`load_ai_plugin_result` 保持 BGR 线性数据不转换，符合 P0-COL-03 通道契约。
- 性能模式：full/high/low 三级预览 + 基于 size/mtime 的缓存键（`_v2` 后缀使旧缓存自动失效）避免重复缩放；16 位预览缓存为 HALF EXR 显著减小 IO。
- 模块级 `OPENCV_IO_ENABLE_OPENEXR`（11 行）与 src/main.py:51 双重设置，入口先行，导入顺序风险低。
- `toos_export_keeps_linear` 的 EXR/浮点档线性、整数档 sRGB 判定逻辑与注释中 F2/H1 契约一致。
- 异常与取消路径（baked 层）：save/load/delete_baked_layer 均记录日志并返回结构化失败（缺陷见 AUD-33902/33914）。
