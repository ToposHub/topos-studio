# 批次报告：batch-315 [P2] src/shared/ai_models//

- 审查日期：2026-09-28
- 编号区间：AUD-31500–AUD-31525（实际使用 AUD-31500–31506、31510–31513、31520–31525）
- 文件（4 个，1,580 行）：`__init__.py`(75)、`ai_mask_engine.py`(454)、`ai_plugin_engine.py`(492)、`denoise_worker.py`(559)
- 汇总：Critical 1 · Major 8 · Suggestion 8
- 交叉验证：已核对 `src/features/color/core/image_engine.py` 对 AIMaskWorker / AIPluginEngine 的实际接线（moveToThread + queued 信号、逐点 emit decode、重入时 terminate 旧 worker），以及 `onnx_env.py` / `model_manager.py` 的模块级导入安全性。`denoise_worker.py` 标注 DORMANT(2026-08-19)，grep 确认生产代码无引用（仅模块内自引用），其发现按"接线前须修"定性。

## src/shared/ai_models/__init__.py (行数: 75)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export 文件。导出 onnx_env 的 7 个环境函数、model_manager 的 4 个符号、inference_engine 的 3 个符号（AIInferenceWorker / AIInferenceEngine / get_inference_engine）。
- 容错导入声明属实：`onnx_env.py` 在 try 内导入 onnxruntime（onnx_env.py:41-42），`model_manager.py` 模块级无 onnxruntime/cv2 导入，故 onnxruntime 缺失时仅 inference_engine 分支触发 `ModuleNotFoundError('onnxruntime')` 并被正确降级（`AIInferenceWorker = None`，`get_inference_engine` 改为抛 RuntimeError），包导入不会失败。
- 非目标 onnxruntime 的 ImportError 正确 re-raise（`if exc.name != "onnxruntime": raise`），不掩盖其他依赖错误。

## src/shared/ai_models/ai_mask_engine.py (行数: 454)

### 发现

- [AUD-31510] [Major] SAM decode 每次请求全量重算所有历史点，会话累计 O(N²) 次解码器推理
  - 位置: src/shared/ai_models/ai_mask_engine.py:391-396
  - 证据:
    ```python
    # 初始化正向和负向累加器
    pos_mask_accum = np.zeros((h, w), dtype=np.float32)
    neg_mask_accum = np.zeros((h, w), dtype=np.float32)

    # 遍历每一个点，单独执行推理，实现"真正的笔刷累加"逻辑
    for p, l in zip(points, labels):
    ```
  - 影响: 调用方（image_engine.add_sam_point，image_engine.py:1778-1779）每新增一个点就携带全量点表 emit 一次 decode；本方法每次都从零累加器开始对全部 N 点逐一执行 `session_sam_dec.run()`（415 行）。N 点笔刷会话累计约 N(N+1)/2 次解码器推理，CPU provider 下单次 decode 延迟随点数线性增长，Smart Brush 交互明显劣化。
  - 建议: worker 内按 embedding 指纹缓存 pos/neg 累加器与已处理点游标，增量只推理新增点；或改为单次多点 prompt 推理（需确认联合语义可接受）。
  - 置信度: 高（重算结构确证；实际卡顿程度取决于 provider 速度）

- [AUD-31511] [Major] SAM encode 失败/取消后 `sam_img_shape` 与 `sam_embedding` 状态不一致，可静默产出错位蒙版
  - 位置: src/shared/ai_models/ai_mask_engine.py:317-318, 346, 354-357
  - 证据:
    ```python
    self.sam_img_shape = img.shape[:2]
    h, w = img.shape[:2]
    ```
    ```python
    self.sam_embedding = self.session_sam_enc.run(None, {input_name: img_input})[0]
    if self._is_cancelled(generation):
        return
    ```
    ```python
    except Exception as e:
        logger.info(f": SAM Encode Exception: {str(e)}")
        if not self._is_cancelled(generation):
            self.error.emit(f"SAM Encode Error: {str(e)}")
    ```
  - 影响: `sam_img_shape` 在推理开始前写入，`sam_embedding` 仅成功后写入且从不失效。切换新图后 encode 失败（或被取消）时，shape 已是新图、embedding 仍是旧图；后续 decode 仅检查 `sam_embedding is None`（371 行），会以旧图 embedding + 新图尺寸计算 scale/orig_im_size（382-385 行），坐标换算全错，静默生成与图像错位的选区。
  - 建议: encode 入口先置 `self.sam_embedding = None`，仅在推理成功后原子地同时提交 shape + embedding。
  - 置信度: 中（需 encode 失败后用户仍触发 decode，但代码路径确定存在）

- [AUD-31512] [Suggestion] BiRefNet sigmoid 启发式在 logits 全落 [0,1] 时失效，软蒙版取值未归一化
  - 位置: src/shared/ai_models/ai_mask_engine.py:282-285
  - 证据:
    ```python
    # BiRefNet output might be logits, apply sigmoid if needed
    if mask.max() > 1.0 or mask.min() < 0.0:
        # Safe sigmoid to prevent overflow
        mask = 1 / (1 + np.exp(-np.clip(mask, -50, 50)))
    ```
  - 影响: 若模型输出 logits 且整幅恰好落在 [0,1]（低对比图可能），判定为"已是概率"跳过 sigmoid，mask 为原始 logit 值，下游按 [0,1] 软蒙版使用时浓度/羽化失真。不能改为无条件 sigmoid（会破坏已是概率的输出）。
  - 建议: 在模型注册元数据中显式标注输出类型（logits/prob），或对输出做跨 0 采样统计判定。
  - 置信度: 中

- [AUD-31513] [Suggestion] 异常以 logger.info 记录、未使用导入、日志前缀混乱、方法缺类型注解
  - 位置: src/shared/ai_models/ai_mask_engine.py:297, 355, 452, 45, 250
  - 证据:
    ```python
    except Exception as e:
        logger.info(f": BiRefNet Exception: {str(e)}")
        if not self._is_cancelled(generation):
            self.error.emit(f"BiRefNet Error: {str(e)}")
    ```
    ```python
    from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot, QThread, QMutex, QWaitCondition
    ```
  - 影响: 三处 except 块用 info 级别记录异常，生产日志级别上调后错误不可见；QThread/QWaitCondition 导入未使用；`logger.info(f": Start BiRefNet")` 等前缀格式混乱影响检索；`_load_model`/`process_*` 等主要方法缺类型注解（维度九）。
  - 建议: 异常改 logger.exception；清理未用导入；统一日志前缀；补类型注解。
  - 置信度: 高

### 通过项
- 线程安全：worker 经 moveToThread 至专用 QThread（image_engine.py:219-232），三个槽函数以 queued 连接顺序执行，无并发重入；`_begin_operation`/`request_cancel`/`_is_cancelled` 全程持 `_mutex`，取消代际（generation）设计正确。
- 取消路径：推理后逐阶段检查代际并抑制陈旧结果/错误信号（275, 289, 347, 397, 416, 444 行），符合协作取消规范，未触碰 ONNX 原生取消 API。
- 异常边界：except Exception 统一转 error 信号带上下文前缀，不吞异常（级别误用另见 AUD-31513）。
- 输入验证：模型路径存在性检查（141-142 行，FileNotFoundError）；ort 缺失时明确 RuntimeError（136-139 行）。
- SAM 坐标换算正确：归一化点乘 w/h 再乘 scale（399-400 行），padding 裁剪分支覆盖 256/1024 两种输出（425-429 行）。
- GPU 资源生命周期：无 ModernGL 资源；CoreML+SAM 强制 CPU 的防 segfault 规避（168-170 行）有注释依据；会话随 worker 生命周期持有。
- HDR/着色：本模块为蒙版二值/软蒙版域 [0,1]，最终 clip（442 行）属蒙版语义，非 HDR 数据违规。

## src/shared/ai_models/ai_plugin_engine.py (行数: 492)

### 发现

- [AUD-31500] [Critical] 取消路径依赖 QThread.terminate() 强杀正在执行 ONNX 原生推理的线程
  - 位置: src/shared/ai_models/ai_plugin_engine.py:455-460, 487-492
  - 证据:
    ```python
    # Cancel existing worker for this instance
    if instance_id in self._workers:
        old = self._workers[instance_id]
        if old.isRunning():
            old.terminate()
            old.wait()
    ```
    ```python
    def cancel(self, instance_id):
        w = self._workers.get(instance_id)
        if w and w.isRunning():
            w.terminate()
            w.wait()
    ```
  - 影响: terminate() 可在线程持有原生锁/执行 session.run() 中途将其杀死，ONNX Runtime 原生状态（线程池、GPU 命令缓冲）损坏可致崩溃或死锁；若线程阻塞于原生调用 terminate 可能不生效，`old.wait()` 在调用线程（image_engine→UI 路径，image_engine.py:1653）无限阻塞冻结界面。更糟：新旧 worker 通过共享 `_session_cache` 复用同一 session 对象，被杀线程留下的损坏状态会被后续推理继承。
  - 建议: 移除 terminate，改为协作取消——worker 增加 `_abort` 标志并在分块循环与阶段边界检查（同 AIMaskWorker 的 generation 模式）；重入时请求取消旧 worker 后由 finished/error 信号回调启动新任务。
  - 置信度: 中（terminate 危害为 Qt 文档明确警告 + 原生推理上下文推理，未复现实际崩溃）

- [AUD-31501] [Major] 类级 `_session_cache`/`_current_model_path` 无锁跨线程读写，多 worker 并发时缓存被互相清空
  - 位置: src/shared/ai_models/ai_plugin_engine.py:39-41, 88-92, 182-183, 199
  - 证据:
    ```python
    # Shared session cache across all AI plugin workers
    _session_cache = {}
    _current_model_path = None
    ```
    ```python
    if AIPluginWorker._current_model_path != model_path:
        if AIPluginWorker._current_model_path is not None:
            AIPluginWorker._session_cache.clear()
        AIPluginWorker._current_model_path = model_path
    ```
  - 影响: process_plugin 仅对同一 instance_id 去重，不同 instance（不同图层/实例）的 worker 线程并发运行时可同时进入 check-then-clear：A 缓存的会话被 B 清空、`_current_model_path` 互相覆盖，导致同模型重复加载（数百 MB 级内存/显存峰值）或缓存与在用会话不一致。同仓 denoise_worker.py 对同一模式配有 `_cache_mutex`（95-108 行），此处完全缺失。
  - 建议: 仿照 AIDenoiseWorker 用 QMutex（或 threading.Lock）保护 cache 的读、清空与写入，切换模型的清理逻辑移入 `_load_session` 临界区内。
  - 置信度: 高（竞态窗口确证；实际触发需多 AI 插件实例并发）

- [AUD-31502] [Major] 分块推理循环无任何协作取消检查点，取消只能靠 terminate 强杀
  - 位置: src/shared/ai_models/ai_plugin_engine.py:257-266, 355-364（循环内无 abort 检查；类中不存在 abort 标志）
  - 证据:
    ```python
    for y in range(0, h, stride):
        if y + tile_size > h:
            y = max(0, h - tile_size)
        for x in range(0, w, stride):
            if x + tile_size > w:
                x = max(0, w - tile_size)

            y1, y2 = max(0, y), min(h, y + tile_size)
    ```
  - 影响: AIPluginWorker 全程没有 `_abort`/`isInterruptionRequested` 检查（与 AUD-31500 互为因果）：4K 图数百个 tile 的长任务无法优雅取消，唯一取消手段是 terminate 强杀，放大崩溃/死锁风险；同时旧 worker 未结束也能被新任务顶替后残留运行。
  - 建议: 增加原子 `_abort` 标志，在每个 tile 迭代与进度发射处检查，取消时静默返回不 emit 陈旧结果（参考 ai_mask_engine 的代际抑制）。
  - 置信度: 高

- [AUD-31503] [Suggestion] `_load_with_timeout` 超时后泄漏仍在加载的 daemon 线程，且吞掉真实加载异常
  - 位置: src/shared/ai_models/ai_plugin_engine.py:209-223
  - 证据:
    ```python
    def _load():
        try:
            result[0] = ort.InferenceSession(
                model_path, sess_options=sess_options, providers=providers
            )
        except Exception:
            result[0] = None

    import threading
    loader = threading.Thread(target=_load, daemon=True)
    loader.start()
    loader.join(timeout=timeout)
    if loader.is_alive():
        return None
    ```
  - 影响: 超时返回后 daemon 线程继续编译同一模型（GPU 编译可持续数分钟），与下一个 provider 的尝试并发，短时双份模型内存；`except Exception: result[0] = None` 使"加载失败"与"超时"不可区分，排障困难。
  - 建议: 在 `_load` 内以 logger.exception 记录异常；超时后记录泄漏警告并避免对同 provider 反复重试。
  - 置信度: 高

- [AUD-31504] [Suggestion] 插件元数据（外部数据）的 tile/overlap 未验证，overlap>=tile_size 时 range 步长为 0 直接 ValueError
  - 位置: src/shared/ai_models/ai_plugin_engine.py:246-251, 344-349
  - 证据:
    ```python
    stride = tile_size - overlap
    input_name = session.get_inputs()[0].name

    # Count tiles for progress
    total_tiles = 0
    for y in range(0, h, stride):
    ```
  - 影响: plugin_data 来自插件清单（PluginManager），作者配置 overlap>=tile_size 时 stride<=0，`range(0, h, stride)` 抛 "arg 3 must not be zero"，经 run() 兜底以晦涩错误呈现，插件完全不可用。违反"外部数据须验证"铁律。
  - 建议: `_process` 入口校验 `tile_size > 0` 且 `0 <= overlap < tile_size`，越界即抛带插件名的明确错误。
  - 置信度: 高

- [AUD-31505] [Suggestion] upscale 输出坐标 int() 截断可能在末行/列留下零权重黑线
  - 位置: src/shared/ai_models/ai_plugin_engine.py:324-325, 382-385, 407-413
  - 证据:
    ```python
    h, w, c = img_rgb.shape
    out_h, out_w = int(h * scale), int(w * scale)
    ```
    ```python
    oy1 = int(y1 * scale)
    ox1 = int(x1 * scale)
    # Actual output tile size = actual input tile size * scale
    o_actual_h = int(actual_h * scale)
    o_actual_w = int(actual_w * scale)
    ```
  - 影响: 画布尺寸 `int(h*scale)` 与逐块 `int(y1*scale)+int(actual_h*scale)` 的向下取整累计可能差 1px，末行/列 weight==0，除以 `np.maximum(weight, 1e-5)`（423 行）后得到 0 → 输出边缘出现 1px 黑线（渲染缺陷）。
  - 建议: 输出统一用 round 并按 out_h/out_w 裁剪，或在归一化前断言 weight>0 的完整覆盖。
  - 置信度: 低（需特定 h/w/scale 组合触发；取整丢覆盖的机制真实存在）

- [AUD-31506] [Suggestion] 模块级无条件 import onnxruntime、run() 用 print_exc 而非 logger、类与方法普遍缺类型注解
  - 位置: src/shared/ai_models/ai_plugin_engine.py:16, 64-67, 43, 69, 141
  - 证据:
    ```python
    import onnxruntime as ort
    ```
    ```python
    except Exception as e:
        import traceback
        traceback.print_exc()
        self.error.emit(self.layer_id, self.instance_id, str(e))
    ```
  - 影响: 与包级"onnxruntime 缺失优雅降级"策略及 AIMaskWorker 的守卫导入不一致，缺失时 image_engine 方法内导入直接抛裸 ImportError；异常栈走 stderr 而非日志系统；`__init__`/`_process`/`_load_session`/`_process_tiled`/`_process_upscale` 均无类型注解（维度九）。
  - 建议: 改守卫导入（ort = None + 调用时明确报错）；traceback 改 logger.exception；补全类型注解。
  - 置信度: 高

### 通过项
- 信号防递归：worker→engine 结果信号单向上报（finished/progress/error），engine 槽在同一线程亲和下顺序执行，无递归风险。
- 异常边界：run() 兜底捕获并 emit error(str)，不吞异常。
- 输入路径验证：模型路径解析与存在性检查（80-85 行，FileNotFoundError 带完整路径）。
- 平铺融合数学正确：fade_in+fade_out 余弦互补（237-238 行），边缘 tile 只在内侧加 ramp（271-278 行），weight 归一化（316 行）；edge tile 零填充区域经 out_h/out_w 裁剪剔除（300-303 行）。
- HDR 契约：docstring 与调用方均定义输入/输出为 float32 [0,1] LDR 域（image_engine.py:1646 以 uint8/255 输入），clamp(0,1) 与契约一致，非无理由 clamp。
- 状态提交一致性：模块不持有 undo/revision 状态（落盘/缓存由 image_engine._on_ai_plugin_finished 负责，不在本批次范围）。
- blend 分支正确：upscale 时先 resize 原图再 addWeighted（129-137 行），并复权后 clip。

## src/shared/ai_models/denoise_worker.py (行数: 559)

（模块头部标注 DORMANT(2026-08-19)，生产代码无引用；以下发现按接线前必须修复项对待）

### 发现

- [AUD-31520] [Major] 控制器持有已 deleteLater 的 QThread 包装，二次调用 is_running/start_processing 触碰已销毁对象抛 RuntimeError
  - 位置: src/shared/ai_models/denoise_worker.py:533-536, 515-517, 552-554
  - 证据:
    ```python
    # Clean up when finished
    self._worker.finished.connect(self._thread.quit)
    self._worker.error.connect(self._thread.quit)
    self._thread.finished.connect(self._thread.deleteLater)
    ```
    ```python
    if self._thread and self._thread.isRunning():
        self.abort()
        self._thread.wait()
    ```
    ```python
    def is_running(self) -> bool:
        """Check if processing is running."""
        return self._thread is not None and self._thread.isRunning()
    ```
  - 影响: 线程结束后 deleteLater 在控制器线程事件循环中销毁 QThread 的 C++ 对象，但 `self._thread` 引用未清空；此后 `is_running()`/`wait()`/再次 `start_processing()` 调用 `isRunning()` 会抛 "wrapped C/C++ object of type QThread has been deleted"（RuntimeError），第二次降噪即可能崩溃调用流程。
  - 建议: 连接 `self._thread.destroyed` 将 `self._thread` 置 None，或在 quit 后的 finished 回调中统一解引用，访问前以 sip/isdeleted 防御。
  - 置信度: 高（PyQt deleteLater 生命周期语义确定）

- [AUD-31521] [Major] 用户取消以 error 信号上报为"发生致命错误"，混入完整 traceback
  - 位置: src/shared/ai_models/denoise_worker.py:379-381, 287-293
  - 证据:
    ```python
    for x_idx, x in enumerate(x_positions):
        if self._abort:
            raise RuntimeError("Processing aborted")
    ```
    ```python
    except Exception as e:
        # 抓取静默崩溃的核心法宝
        error_msg = f"发生致命错误: {str(e)}\n{traceback.format_exc()}"
        logger.info("❌" * 20)
        logger.error(f"[Denoise Worker] 子线程崩溃了！\n{error_msg}")
        logger.info("❌" * 20)
        self.error.emit(error_msg)
    ```
  - 影响: abort 触发的 "Processing aborted" 走与真实崩溃相同的路径，UI 收到"发生致命错误: Processing aborted"+堆栈，把正常取消呈现为致命故障；且 abort 是协作取消唯一出口，无法区分静默取消与错误。
  - 建议: 取消用独立信号/标志（如 cancelled()），process 内先判 `self._abort` 分流，不并入 error。
  - 置信度: 高

- [AUD-31522] [Major] start_processing 在调用线程阻塞 wait()，abort 仅在块间生效，长推理期间 UI 冻结
  - 位置: src/shared/ai_models/denoise_worker.py:513-517, 380, 300-318
  - 证据:
    ```python
    def start_processing(self, image: np.ndarray, params: dict):
        """Start denoising in a background thread."""
        if self._thread and self._thread.isRunning():
            self.abort()
            self._thread.wait()
    ```
  - 影响: `wait()` 阻塞调用线程（按类文档用法为 UI 线程）直到旧任务退出；`_abort` 只在 provider 循环（126 行）、块循环边界（380 行）与阶段点（242/267 行）检查，`_process_single` 整图推理（311 行 session.run）及单个大 tile 期间无法中断——CPU 上可达数十秒，期间界面完全冻结。
  - 建议: 重入时不阻塞等待：请求取消后立即返回/排队新任务，由旧任务 finished 回调启动；文档明示单块推理的最坏不可中断时长。
  - 置信度: 高

- [AUD-31523] [Major] overlap==1 时 ramp 计算除以零产生 NaN 权重，输出边缘黑边且被 nan_to_num 静默掩盖
  - 位置: src/shared/ai_models/denoise_worker.py:338-341
  - 证据:
    ```python
    if overlap > 0:
        t = np.arange(overlap) / (overlap - 1)  # 0 to 1
        fade_in = 0.5 * (1 - np.cos(np.pi * t))   # 0 → 1
        fade_out = 0.5 * (1 + np.cos(np.pi * t))  # 1 → 0
    ```
  - 影响: overlap==1（params 可传入）时 `np.arange(1)/0` 得 NaN（仅 RuntimeWarning），fade_in/out 为 NaN → 边缘 tile_mask 变 NaN → output/weight 累积 NaN，最终被 272 行 `np.nan_to_num` 置 0，图像边缘出现黑色条带，无任何异常抛出（渲染错误）。对照 ai_plugin_engine.py:236 同一公式已用 `max(overlap - 1, 1)` 防护，此处遗漏。
  - 建议: 分母改为 `max(overlap - 1, 1)`，与 plugin engine 对齐；并在入口校验 overlap 合法范围（见 AUD-31524）。
  - 置信度: 中（行为确定，触发需 overlap==1 的非常规配置）

- [AUD-31524] [Suggestion] 外部参数与图像输入未验证：overlap>=tile_size 致 range 步长 0，非 3 通道图像致 cv2.error
  - 位置: src/shared/ai_models/denoise_worker.py:349, 356, 252
  - 证据:
    ```python
    stride = tile_size - overlap
    ```
    ```python
    for y in range(0, h, stride):
    ```
    ```python
    img_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    ```
  - 影响: overlap>=tile_size 时 stride<=0 → range() 抛 ValueError（被 process 捕获后以晦涩"发生致命错误"呈现）；传入 BGRA/灰度图时 cvtColor 抛 cv2.error。params 来自调用方（UI 参数），按铁律应验证。
  - 建议: process 入口校验 `tile_size > 0`、`0 <= overlap < tile_size`、`image.ndim == 3 and image.shape[2] == 3`，给出明确参数错误。
  - 置信度: 高

- [AUD-31525] [Suggestion] error 路径不清理 worker 对象，每次失败泄漏一个 AIDenoiseWorker（含会话引用）
  - 位置: src/shared/ai_models/denoise_worker.py:526-528, 540-545
  - 证据:
    ```python
    self._worker.progress.connect(self.progress.emit)
    self._worker.finished.connect(self._on_finished)
    self._worker.error.connect(self.error.emit)
    ```
    ```python
    def _on_finished(self, result: np.ndarray):
        """Handle finished signal and clean up."""
        self.finished.emit(result)
        if self._worker:
            self._worker.deleteLater()
            self._worker = None
    ```
  - 影响: 只有 finished 走 `_on_finished` 做 deleteLater + 解引用；error 仅转发信号，worker 对象（及 moveToThread 后的线程内对象）泄漏至下次 start_processing 覆盖 `self._worker`，反复失败时累积。
  - 建议: 增加 `_on_error` 槽，与 `_on_finished` 相同的清理逻辑后再转发 error。
  - 置信度: 高

### 通过项
- 会话缓存互斥：`_cache_mutex` 覆盖读/清空/写入（95-108, 153-155, 177-179 行）；存在轻微 check-then-act 窗口（两段锁之间），后果限于同模型重复加载，无字典损坏风险。
- 平铺融合数学正确：fade_in/fade_out 余弦互补（334-347 行），边缘 tile 用 BORDER_REFLECT_101 反射填充且 mask 恒零填充防伪影（419-436 行），块边界 y/x 位置回拉避免越界（356-364 行）。
- abort 检查点存在于 provider 循环（126 行）、加载后与推理后（242/267 行）及每块迭代（380 行）。
- 异常边界：process 兜底捕获并附带完整 traceback 转发 error 信号，不吞异常（取消误报另见 AUD-31521）。
- 输入路径验证：model_path 缺省/相对路径归一（215-220 行）与存在性检查（222-223 行）。
- 进度信号：10-90 分块区间 + 5/10/95/100 阶段点，单调不回跳。
- 类型注解：public 方法与控制器方法注解完整。
- HDR 契约：docstring 明确输入/输出 float32 [0,1]，clamp 与契约一致；blend 使用 clamp 后副本（249 行 original_image）在 LDR 契约下自洽。
- 信号防递归：无 UI 控件编程更新路径；信号单向（worker→controller→UI）。
- DORMANT 标注合规：头部注释说明休眠与处置建议。
