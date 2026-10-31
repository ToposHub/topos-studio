# 批次报告：batch-318 — src/shared/ai_models//

- 日期：2026-09-28
- 编号区间：AUD-31800–AUD-31899（实际使用 AUD-31800–AUD-31817）
- 审查文件：6 个，共 2,518 行（全部通读）
- 汇总：Critical 0 · Major 7 · Suggestion 11
- 交叉验证说明：`AIInferenceEngine.request_inference/cancel` 当前在 src/ 内无调用方（composite 的 AIComputeManager 自带复刻 worker，仅引用 `AIInferenceWorker`），相关问题标注了可达性；`RifeInterpolator` 为生产路径（frame_interpolate_node 使用）。

---

## src/shared/ai_models/inference_engine.py (行数: 996)

### 发现

- [AUD-31800] [Major] cancel/cancel_all 持有互斥锁期间同步 wait(3000)，调用线程可被阻塞数秒且所有管理操作被锁死
  - 位置: src/shared/ai_models/inference_engine.py:917-935
  - 证据:
    ```python
    def cancel(self, cache_key: str):
        """Cancel inference for a key."""
        self._lock.lock()
        worker = self._workers.get(cache_key)
        if worker:
            worker.cancel()
            worker.wait(3000)
            self._workers.pop(cache_key, None)
        self._lock.unlock()
    ```
    （`cancel_all` 同模式：先逐个 `cancel()` 再逐个 `wait(3000)`，全程持锁）
  - 影响: 长时间无法中断的 `session.run()` 会让 `wait(3000)` 超时；期间 `self._lock` 被占，`request_inference`/`is_processing`/其他 `cancel` 全部被阻塞。若在 UI 线程调用即界面冻结（cancel_all 最坏 3s×N）。
  - 建议: 持锁仅做 dict 摘取/删除，`wait()` 移出临界区；或将 worker 清理移到 finished 信号回调（已有 `_on_worker_finished` pop 逻辑）。
  - 置信度: 高

- [AUD-31801] [Major] wait 超时后从 `_workers` 弹出 worker，运行中的 QThread 可能失去最后引用被 GC 销毁（"QThread: Destroyed while thread is still running" 崩溃）
  - 位置: src/shared/ai_models/inference_engine.py:924, 934
  - 证据:
    ```python
    worker.cancel()
    worker.wait(3000)
    self._workers.pop(cache_key, None)
    ```
  - 影响: ONNX 推理单次 `session.run()` 不可中断，`wait(3000)` 超时后线程仍在 run() 中；pop 丢弃强引用后若待投递的 queued 信号未持有 sender 引用，Python 侧对象被回收 → C++ QThread 在运行中被析构 → 崩溃/未定义行为。cancel_all 的 `clear()` 同理。
  - 建议: 超时时不 pop（留给 `_on_worker_finished/_on_worker_error` 清理），或将 worker 挂到"待回收"列表直到线程真正 finished 再释放；至少调用 `worker.wait()` 无超时版兜底。
  - 置信度: 中

- [AUD-31802] [Major] tile_size/overlap 无关系校验：stride ≤ 0 时静默输出全黑图像或抛 ValueError；overlap == 1 时除零
  - 位置: src/shared/ai_models/inference_engine.py:140-141, 599, 609, 614-621, 702
  - 证据:
    ```python
    tile_size = self.params.get("tile_size") or get_configured_tile_size()
    overlap = self.params.get("overlap", 64)
    ...
    t = np.arange(overlap) / (overlap - 1)
    ...
    stride = tile_size - overlap

    y_positions = []
    x_positions = []
    for y in range(0, h, stride):
    ```
    以及归一化：
    ```python
    output = output / np.maximum(weight, 1e-5)
    ```
  - 影响: overlap ≥ tile_size 时 stride ≤ 0：stride==0 → `range()` 抛 ValueError（被 run() 捕获报错）；stride<0 → `range(0, h, 负)` 为空 → total_tiles=0、循环体不执行 → output/weight 全零 → 归一化后得到整幅纯黑图并作为 `finished` 结果正常发出（渲染错误级，无任何告警）。overlap==1 时 `np.arange(1)/0` 产生 inf/NaN 权重。UI 下拉限制 256/512/1024（preferences.py:526）+ 默认 overlap=64 时不可达，但 `params["tile_size"]/["overlap"]` 与手改配置 `ai_tile_size`（onnx_env.py:158-161 无下限校验）均可触达。
  - 建议: 入口校验 `overlap = min(max(0, overlap), tile_size // 2)`、tile_size 下限（如 ≥ 128），stride ≤ 0 时直接走非分块路径或报参数错误；total_tiles == 0 时显式报错而非返回黑图。
  - 置信度: 高

- [AUD-31803] [Major] 用户取消以 RuntimeError("Cancelled") 抛出，被统一 except 捕获后走 error 信号 → 主动取消被上报为推理失败
  - 位置: src/shared/ai_models/inference_engine.py:182-185, 484-486, 632-634
  - 证据:
    ```python
    for provider in providers:
        if self._cancelled:
            raise RuntimeError("Cancelled")
    ```
    ```python
    for y in y_positions:
        for x in x_positions:
            if self._cancelled:
                raise RuntimeError("Cancelled")
    ```
    ```python
    except Exception as e:
        error_msg = f"{self.node_type} inference error: {str(e)}\n{traceback.format_exc()}"
        logger.error(f"Error: {error_msg}")
        self.error.emit(self.cache_key, error_msg, self.request_id)
    ```
  - 影响: 取消路径复用异常通道：`_on_worker_error` 会 emit `inference_failed`，UI 将用户取消显示为错误（含完整 traceback 日志噪音），与 run() 中其他 `if self._cancelled: return` 的静默路径（134-136、157-158、167-168）行为不一致。
  - 建议: 定义专用 `CancelledError`，在 run() except 分支中识别后静默返回（或发独立 cancelled 信号），不进入 error/inference_failed。
  - 置信度: 中

- [AUD-31804] [Major] 输入侧无 HDR 范围处理而输出侧无条件 clip(0,1)，与"HDR 内部数据不得无理由 clamp"铁律冲突且行为不对称
  - 位置: src/shared/ai_models/inference_engine.py:778-789, 797-812
  - 证据:
    ```python
    def _prepare_denoise_input(self, image: np.ndarray) -> np.ndarray:
        ...
        img_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        nchw = img_rgb.transpose(2, 0, 1)[np.newaxis, :, :, :]
        return np.ascontiguousarray(nchw, dtype=np.float32)
    ```
    ```python
    result = np.nan_to_num(result, nan=0.0, posinf=1.0, neginf=0.0)
    result = np.clip(result, 0, 1)
    ...
    if blend < 1.0:
        original = self.input_images.get("image")
        if original is not None and original.shape[:2] == result.shape[:2]:
            result = cv2.addWeighted(result, blend, original, 1.0 - blend, 0)
    ```
  - 影响: HDR 流（float 值可 >1.0）未经归一直接喂给按 [0,1] 训练的 ONNX 模型（数值饱和、结果劣化），输出再被硬 clamp 到 [0,1] 丢失高光；随后 blend 又用未 clamp 的原图混合——同一管线内对范围的处理前后不一致。若产品约定 AI 节点仅工作在 SDR 域，也应显式注明并对输入做同样的范围约定/断言。
  - 建议: 输入侧统一做显式范围映射（或文档化仅接受 [0,1] 并在入口校验/记录告警）；输出 clip 加注释说明理由，与 blend 路径统一范围语义。
  - 置信度: 中

- [AUD-31805] [Suggestion] `_save_to_cache` 宽泛 `except Exception: pass` 吞掉缓存保存失败
  - 位置: src/shared/ai_models/inference_engine.py:978-984
  - 证据:
    ```python
    def _save_to_cache(self, cache_key: str, result: np.ndarray):
        """Save result via registered cache callback."""
        if self._cache_save_fn is not None:
            try:
                self._cache_save_fn(cache_key, result)
            except Exception:
                pass
    ```
  - 影响: 缓存写入失败（磁盘满、缓存管理器内部错误）被静默吞掉，之后每次都重算全分辨率推理，无法排查；违反"不得宽泛 except 掩盖领域错误"。
  - 建议: 至少 `logger.exception(...)` 记录，必要时通过信号上报缓存降级状态。
  - 置信度: 高

- [AUD-31806] [Suggestion] `AIInferenceWorker.finished` 遮蔽 QThread 内置 `finished` 信号
  - 位置: src/shared/ai_models/inference_engine.py:67
  - 证据:
    ```python
    finished = pyqtSignal(str, np.ndarray, int)  # cache_key, result, request_id
    preview = pyqtSignal(str, np.ndarray, int)   # cache_key, preview_result, request_id
    ```
  - 影响: 同名不同签名的新信号遮蔽了基类线程生命周期信号：无法再用 `worker.finished` 感知线程真正结束（AUD-31801 的安全回收正需要它），语义混淆、易误连接。
  - 建议: 改名为 `result_ready`/`inference_finished` 之类，保留内置 `finished` 用于生命周期管理。
  - 置信度: 高

- [AUD-31807] [Suggestion] 会话缓存 check-then-act 竞态可重复建会话，且缓存无淘汰、worker 完成后未 deleteLater
  - 位置: src/shared/ai_models/inference_engine.py:465-470, 523-525, 937-940, 73
  - 证据:
    ```python
    AIInferenceWorker._cache_mutex.lock()
    if model_path in AIInferenceWorker._session_cache:
        session = AIInferenceWorker._session_cache[model_path]
        AIInferenceWorker._cache_mutex.unlock()
        return session
    AIInferenceWorker._cache_mutex.unlock()
    ```
    ```python
    AIInferenceWorker._cache_mutex.lock()
    AIInferenceWorker._session_cache[model_path] = session
    AIInferenceWorker._cache_mutex.unlock()
    ```
  - 影响: 查缓存与建会话之间解锁，两个 worker 同路径并发时各自加载一份 ONNX（GPU EP 下双份显存峰值，其中一份成孤儿）；`_session_cache` 无上限淘汰（`params["model_path"]` 自定义路径可累积常驻内存/显存）；`_on_worker_finished` 仅 pop 不 `deleteLater()`，与 model_manager 的下载 worker 处理方式不一致。
  - 建议: 用"double-checked"或在锁内以占位标记防重复加载；为缓存加简单 LRU/数量上限；worker 清理统一 `deleteLater()`。
  - 置信度: 高

- [AUD-31808] [Suggestion] 死代码与文档不符：`_emit_preview` 结果未存储、`_run_inference` 的 tile 形参从未使用、`import time` 未使用
  - 位置: src/shared/ai_models/inference_engine.py:31, 552-554, 952-961
  - 证据:
    ```python
    def _on_worker_preview(self, cache_key: str, result: np.ndarray, request_id: int):
        """Handle preview result."""
        ...
        # Store preview for UI access
        self.inference_preview.emit(cache_key)
    ```
    ```python
    def _run_inference(
        self,
        image: np.ndarray,
        mask: Optional[np.ndarray],
        session: ort.InferenceSession,
        tile_size: Optional[int] = None,
        overlap: Optional[int] = None,
    ) -> np.ndarray:
    ```
  - 影响: 注释声称"Store preview for UI access"但 `result`（预览图像）被丢弃，接收方只拿到 cache_key 拿不到图像；`tile_size/overlap` 形参是死参数误导调用者；`import time` 未使用。
  - 建议: 删除死参数与未用导入；预览结果若需 UI 访问则存储或随信号转发，否则修正注释。
  - 置信度: 高

### 通过项
- 信号防递归：worker→engine 为单向发射、engine 再发射自身信号，无递归环路；stale 检测在 finished/preview/error 三处一致应用。
- 异常兜底：run() 捕获所有异常并 emit error，不裸崩工作线程。
- 线程边界：`_latest_request_id` 读写虽有未加锁读（GIL 下原子），整体 stale 机制方向正确。
- outpaint 几何：feather 掩膜与 blend 掩膜互为补集、坐标 clamp 正确；`_blend_outpaint_result` 区域裁剪对齐原图尺寸。
- 瓦片融合：Hanning 权重外边界不衰减、pad 区域权重为 0、输出按 weight 归一，重叠区处理正确。
- 模型路径解析：节点类型映射 + 自定义路径存在性检查（os.path.exists）。

---

## src/shared/ai_models/model_manager.py (行数: 543)

### 发现

- [AUD-31809] [Major] 下载非原子且异常中断不清理残留文件：部分文件在下次启动被 `_sync_registry` 仅凭存在即标记为可用，且注册表 sha256 全为 None 无法校验
  - 位置: src/shared/ai_models/model_manager.py:204-213, 229-232, 302-305（sha256 示例 80-89）
  - 证据:
    ```python
    with open(self.target_path, "wb") as f:
        while True:
            if self._cancelled:
                # Clean up partial download
                f.close()
                try:
                    os.remove(self.target_path)
    ```
    ```python
    except urllib.error.URLError as e:
        self.finished.emit(self.model_id, False, f"Download failed: {e.reason}")
    except Exception as e:
        self.finished.emit(self.model_id, False, f"Error: {str(e)}")
    ```
    ```python
    if os.path.exists(local_path):
        model_info.local_path = local_path
        model_info.is_available = True
        model_info.file_size = os.path.getsize(local_path)
    ```
  - 影响: 显式取消才删除半截文件；网络中断/写盘异常路径（229-232）不清理，且直接写最终路径（无 .part + 原子 rename）。下次启动 `_sync_registry` 只看 `os.path.exists` 就置 `is_available=True`，损坏/半截模型被当作可用 → 推理加载失败或结果异常。同时注册表所有条目 `sha256=None`（lama/sam 等），`validate_model` 形同虚设且无调用方——违反"外部数据须验证"。
  - 建议: 下载至 `<name>.part` 成功后 `os.replace` 原子落位；异常路径同样清理 .part；`_sync_registry` 对无 sha256 的模型至少校验文件大小>0 或 ONNX 可加载性；为可下载模型补充校验和并在成功回调中调用 `validate_model`。
  - 置信度: 高

- [AUD-31810] [Suggestion] `_save_manifest` 非原子写入，崩溃可致 manifest.json 截断损坏
  - 位置: src/shared/ai_models/model_manager.py:287-293
  - 证据:
    ```python
    def _save_manifest(self):
        """Save manifest to disk."""
        try:
            with open(self._manifest_path, "w", encoding="utf-8") as f:
                json.dump(self._manifest, f, indent=2)
        except IOError as e:
            logger.error(f"Failed to save manifest: {e}")
    ```
  - 影响: 写入途中崩溃/断电产生截断 JSON；`_load_manifest` 的 JSONDecodeError 兜底会整体重置为 `{}`，下载/注册记录丢失（可由 `_sync_registry` 部分重建，但手动 `register_local_model` 的记录会丢）。
  - 建议: 写临时文件后 `os.replace` 原子替换。
  - 置信度: 高

- [AUD-31811] [Suggestion] MODEL_REGISTRY 为模块级可变全局、单例创建无锁，`cancel_download` 在调用线程同步 wait(3000)
  - 位置: src/shared/ai_models/model_manager.py:57, 538-543, 395-401
  - 证据:
    ```python
    MODEL_REGISTRY: Dict[str, ModelInfo] = {
        # Denoise models
        "nafnet_denoise": ModelInfo(
    ```
    ```python
    def cancel_download(self, model_id: str):
        """Cancel an ongoing download."""
        worker = self._download_workers.get(model_id)
        if worker:
            worker.cancel()
            worker.wait(3000)
            self._download_workers.pop(model_id, None)
    ```
    ```python
    def get_model_manager() -> AIModelManager:
        global _model_manager_instance
        if _model_manager_instance is None:
            _model_manager_instance = AIModelManager()
        return _model_manager_instance
    ```
  - 影响: 注册表 dataclass 字段在下载回调中被改写（496-531），任何线程都可读（rife 在渲染线程调用 get_model_path）；改写虽经信号排队到管理者线程，但模块级共享 + `get_model_manager()` 无锁惰性创建意味着多线程首调可各建一个 manager 并发跑 `_sync_registry`（内部 `threading.Lock` 是实例级，护不住跨实例）。`cancel_download` 在 UI 线程 wait(3000) 最多冻结 3 秒（urlopen 读超时 60s 时必然超时）。
  - 建议: 单例创建加模块级锁或提前在主线程初始化；`cancel_download` 改为仅 `cancel()` + 由 finished 回调清理；文档化 MODEL_REGISTRY 的线程所有权。
  - 置信度: 中

- [AUD-31812] [Suggestion] `ModelDownloadWorker.finished` 遮蔽 QThread 内置 finished；`Callable`/`asdict` 导入未用
  - 位置: src/shared/ai_models/model_manager.py:36-37, 170-171
  - 证据:
    ```python
    progress = pyqtSignal(str, float)   # model_id, progress (0.0-1.0)
    finished = pyqtSignal(str, bool, str)  # model_id, success, error_message
    ```
  - 影响: 同 AUD-31806 的遮蔽问题；`from typing import ... Callable` 与 `asdict` 均未使用，徒增导入开销与噪音。
  - 建议: 改名 `download_finished`；删除未用导入。
  - 置信度: 高

### 通过项
- 下载去重：同 model_id 重复调用 `download_model` 被拒（376-378）。
- manifest 损坏兜底：`_load_manifest` 捕获 JSONDecodeError/IOError 重置为空，不会崩构造函数。
- 显式取消路径清理半截文件正确（206-213）。
- 目录创建：`os.makedirs(cache_dir, exist_ok=True)`，legacy 缓存目录迁移逻辑有注释说明。
- 类型注解：公开 API 注解完整。

---

## src/shared/ai_models/nafnet.py (行数: 149)

### 发现

- [AUD-31813] [Suggestion] 全模块无类型注解；构造器使用可变默认参数
  - 位置: src/shared/ai_models/nafnet.py:75
  - 证据:
    ```python
    def __init__(self, img_channel=3, width=32, middle_blk_num=1, enc_blk_nums=[1, 1, 1, 1], dec_blk_nums=[1, 1, 1, 1]):
    ```
  - 影响: 违反项目强类型约定（.clinerules：所有函数有类型注解）；可变默认列表是 Python 反模式（当前未被变异，实际风险低）。
  - 建议: 补类型注解，可变默认改为 `Tuple[int, ...]` 元组或 None 哨兵。
  - 置信度: 高

### 通过项
- 模型结构逻辑正确：`check_image_size` 的 pad 与 forward 末尾 `x[:, :, :H, :W]` 裁剪对称；encoder/decoder skip 连接顺序正确（`encs[::-1]`）。
- LayerNorm2d 对 MPS 的 `.contiguous()` 处理有注释说明理由。
- 纯 PyTorch 定义模块，无线程/GPU 资源管理问题（转换工具使用）。

---

## src/shared/ai_models/onnx_env.py (行数: 353)

### 发现

- [AUD-31814] [Suggestion] `_get_config()` 懒单例无线程保护；模块函数无类型注解；`get_configured_tile_size` 对配置值无合法性校验
  - 位置: src/shared/ai_models/onnx_env.py:60-66, 158-161
  - 证据:
    ```python
    def _get_config():
        """Lazy singleton ConfigManager accessor."""
        global _config_instance
        if _config_instance is None:
            from src.shared.config.config_manager import ConfigManager
            _config_instance = ConfigManager()
        return _config_instance
    ```
    ```python
    def get_configured_tile_size():
        """Returns the user-configured default AI tile size, or 512."""
        config = _get_config()
        return config.get("ai_tile_size") or 512
    ```
  - 影响: 本模块函数会被推理 worker 线程调用（inference_engine._load_onnx_session → get_configured_providers），多线程并发首调可重复构建 ConfigManager；`ai_tile_size` 手改配置为小值（如 64）时原样返回，直接触发 AUD-31802 的 stride≤0 全黑输出——缺少下限钳制这一最后防线；全模块函数缺返回类型注解。
  - 建议: 单例创建加锁；`get_configured_tile_size` 返回 `max(256, int(value))` 类似的钳制；补类型注解。
  - 置信度: 高

### 通过项
- Intel Mac / 黑苹果 CoreML 死锁隔离逻辑正确且有清晰注释（222-237）。
- ort 缺失时优雅降级：返回空列表/CPU + `_require_onnxruntime()` 给出可操作错误信息，且 __init__.py 依赖该行为做导入兜底。
- provider 优先级链（CUDA→CoreML→DirectML→CPU）与用户显式偏好（cpu/cuda/coreml/directml）回退逻辑完整，均有日志。
- `get_optimal_providers` 对 `ort.get_available_providers()` 异常有兜底。

---

## src/shared/ai_models/rife_interpolator.py (行数: 141)

### 发现

- [AUD-31815] [Suggestion] 会话硬编码 CPUExecutionProvider，忽略用户 GPU 偏好与会话线程配置；`_SESSION_CACHE` 模块级无锁
  - 位置: src/shared/ai_models/rife_interpolator.py:34, 68-71
  - 证据:
    ```python
    _SESSION_CACHE: dict = {}
    ...
    import onnxruntime as ort
    self._session = ort.InferenceSession(
        str(path), providers=["CPUExecutionProvider"])
    _SESSION_CACHE[str(path)] = self._session
    ```
  - 影响: onnx_env 提供的 `get_configured_providers()`/`get_session_options()`（用户 GPU 设备、处理线程数）对 RIFE 不生效；该类是生产路径（frame_interpolate_node 每帧调用），4K 帧插值走 CPU 会成为明显瓶颈。docstring 强调确定性输出（byte-identical），CPU 可能是有意为之以保证可复现，但代码未注明。`_SESSION_CACHE` 无锁，多渲染线程并发首帧可能重复建会话（GIL 下 dict 读写原子，影响小）。
  - 建议: 若为确定性设计请加注释说明；否则改用 `get_configured_providers()` 并复用 `get_session_options()`；缓存访问可复用 inference_engine 的 QMutex 模式。
  - 置信度: 中

### 通过项
- 可测试性：session/model_manager 均可注入，符合契约注释。
- 输入验证：`t` 双向 clamp（84）；`astype+ascontiguousarray` 拷贝防外部数组突变（85-86）。
- 优雅降级：模型缺失/推理失败均回落线性混合并置 `used_fallback` 供 UI 呈现质量层级（93-107），异常不外泄。
- `cache_signature` 含双帧 rev、t（6 位精度）、hd_mode，维度完整。
- t 边界直接返回输入副本（87-90），语义正确。

---

## src/shared/ai_models/swinir.py (行数: 336)

### 发现

- [AUD-31816] [Major] `PatchMerging.forward` 引用未定义名 `dim` → NameError，调用即崩（当前 DORMANT 未接线）
  - 位置: src/shared/ai_models/swinir.py:213
  - 证据:
    ```python
    B, L, C = x.shape
    x = x.view(B, H, W, C)

    x0 = x[:, 0::2, 0::2, :]
    x1 = x[:, 1::2, 0::2, :]
    x2 = x[:, 0::2, 1::2, :]
    x3 = x[:, 1::2, 1::2, :]
    x = torch.cat([x0, x1, x2, x3], -1)
    x = x.view(B, -1, 4 * dim)
    ```
  - 影响: `forward` 作用域内没有 `dim`（构造器里是 `self.dim`）→ 一旦 `PatchMerging` 前向被调用立即 NameError 崩溃。文件头部已标注 DORMANT(2026-08-19)、无生产入口，故线上未触发；但接线前必须修复。原版 SwinIR 此处为 `4 * C`。
  - 建议: 改为 `x.view(B, -1, 4 * C)` 或 `4 * self.dim`，并补一条前向单测。
  - 置信度: 高

- [AUD-31817] [Suggestion] 无类型注解；可变默认参数；`drop_path` 参数被静默替换为 nn.Identity
  - 位置: src/shared/ai_models/swinir.py:116, 258-259
  - 证据:
    ```python
    self.drop_path = nn.Identity()
    ```
    ```python
    def __init__(self, img_size=64, patch_size=1, in_chans=3,
                 embed_dim=96, depths=[6, 6, 6, 6], num_heads=[6, 6, 6, 6],
    ```
  - 影响: `SwinTransformerBlock` 接收 `drop_path` 形参（BasicLayer 还计算了 `drop_path[i]` 列表分发）但被 `nn.Identity()` 无条件覆盖，随机深度失效且无说明；可变默认列表反模式；全模块无类型注解。
  - 建议: 用传入的 drop_path 构建（或删参并注释）；默认参数改元组；补类型注解。
  - 置信度: 高

### 通过项
- 窗口划分/反划分、shift 注意力掩码计算（calculate_mask 的 -100 屏蔽、pad 后裁剪）符合 SwinIR 标准实现。
- 文件头部 DORMANT 标注与处置建议清晰（半年内无接线计划建议删除）。
- 纯模型定义，无线程/GPU 资源管理职责。

---

## 汇总统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 7 | AUD-31800, 31801, 31802, 31803, 31804, 31809, 31816 |
| Suggestion | 11 | AUD-31805–31808, 31810–31815, 31817 |
