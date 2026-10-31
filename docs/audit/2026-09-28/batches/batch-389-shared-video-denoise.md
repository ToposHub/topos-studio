# batch-389 审查报告：src/shared/video/denoise//

- 批次：batch-389 [P2]
- 编号区间：AUD-38900–AUD-38999
- 文件数：4（829 行），全部通读
- 审查日期：2026-09-28
- 交叉验证（只读）：`src/shared/video/frame_provider_service.py`（open_clip/request_frame_window/close_clip，确认无会话级淘汰）、`src/app/controllers/file_controller.py`（provider 生命周期与 window_ready 消费端）、`src/shared/media/frame.py` / `src/shared/media/gpu_frame.py`（DecodedFrame/GpuDecodedFrame 均含必填 frame_index）、`src/features/color/core/async_renderer.py`（set_video_denoise_window 经 RenderCommand 提交）。

---

## src/shared/video/denoise/__init__.py (行数: 26)

### 发现

（无）

### 通过项

- 通过（无逻辑）：纯 re-export 模块，导出 `MOTION_QUALITIES` / `QUALITY_MODES` / `SPATIAL_MODES` / `SpatialDenoiseParams` / `TemporalDenoiseParams` / `VideoDenoiseParams` / `FrameWindow` / `TemporalFrameWindowCache` 共 9 个符号，与 `__all__` 完全一致，无循环导入（GPU 实现按注释隔离在 features 层）。

---

## src/shared/video/denoise/frame_types.py (行数: 216)

### 发现

- [AUD-38901] [Suggestion] `frame_indices` 回退分支的距离与顺序计算错误
  - 位置: src/shared/video/denoise/frame_types.py:53-59
  - 证据:
    ```python
    before: List[int] = []
    for distance, frame in enumerate(reversed(self.previous_frames), start=1):
        if frame is not None:
            before.append(index_or_fallback(frame, self.center_frame - distance))
    after: List[int] = []
    for distance, frame in enumerate(self.next_frames, start=1):
        if frame is not None:
            after.append(index_or_fallback(frame, self.center_frame + distance))
    ```
  - 影响: `reversed(previous_frames)` 产出的是"远→近"，`enumerate(start=1)` 却把 distance=1 分配给最远帧。当帧对象缺 `frame_index` 时回退值整体错位：2 帧窗口会得到回退值 [center-1, center-2]（实际应为 [center-2, center-1]），既错号又违反 docstring "从最旧到最新" 的顺序承诺。缓解因素：`DecodedFrame.frame_index`（frame.py:315）与 `GpuDecodedFrame.frame_index`（gpu_frame.py:305）均为必填属性，实际路径几乎不会落入回退分支。
  - 建议: 先按近→远枚举（`enumerate(self.previous_frames, start=1)`）再 `reverse()` 结果，使 distance 语义与列表位置一致；或在回退时用 `center_frame - (len - i)` 计算稠密窗口位置。
  - 置信度: 高（逻辑错误确凿；可达性低）

- [AUD-38902] [Suggestion] `TemporalFrameWindowCache` 全库无消费者，锁/LRU 逻辑零投产验证
  - 位置: src/shared/video/denoise/frame_types.py:75-216
  - 证据:
    ```python
    class TemporalFrameWindowCache:
        """时域降噪消费者侧的窗口 LRU 缓存。
        缓存键: (clip_id, frame_index, quality, generation_id, pixel_format, resolution)
        ...
    ```
    （grep 全库仅 `__init__.py` re-export，无任何构造或调用点；暂停态实际路径 window_provider 直接走 request_frame_window，不经此缓存）
  - 影响: 约 140 行含 RLock/LRU/字节预算的并发逻辑从未在生产路径执行，回归风险沉淀；`get` 返回缓存对象本体（非拷贝），未来消费者若改写窗口会破坏字节记账与缓存一致性，当前无约定约束。
  - 建议: 若属计划内未落地能力，在类 docstring 标注状态并补最小单测（put/evict/invalidate_generation 语义）；接入消费者时考虑返回只读视图或在 docstring 明确"窗口放入后不可变"契约。
  - 置信度: 高（无消费者经 grep 证实）

### 通过项

- 线程安全：`RLock` 覆盖 put/get/invalidate_generation/clear/get_stats 全部共享状态；`put` 的重复键先 pop 旧值再回扣字节，账目守恒；`invalidate_generation` 先收集再删除，避免迭代中修改。
- GPU 资源生命周期：纯 CPU 数据结构，不涉 ModernGL/OpenGL。
- GLSL·HDR 正确性：不做任何颜色处理，无 clamp(0.0,1.0) 类决策。
- 信号防递归：无 Qt 信号。
- 异常与取消路径：`index_or_fallback`/`_estimate_bytes` 用 getattr+精确 `(TypeError, ValueError)` 防御，无宽泛 except。
- 输入验证：`make_key` 对各分量强制 `str()`/`int()` 归一化；负字节钳回 0 防记账漂移。
- 性能模式：`invalidate_generation` O(n) 遍历在预期条目规模内可接受；无 ndarray 的帧按 0 计已有注释说明。
- 类型注解：完整（含内部 `OrderedDict[Tuple, Any]` 注解）。

---

## src/shared/video/denoise/params.py (行数: 264)

### 发现

- [AUD-38903] [Suggestion] `blend` 非法值回退默认 0.5 与字段默认 0.8 不一致
  - 位置: src/shared/video/denoise/params.py:145（对照 :129）
  - 证据:
    ```python
    blend: float = 0.8            # 0-1，历史混合比例上限   # :129 字段默认
    ...
    blend=_clamp(_as_float(self.blend, 0.5), 0.1, 1.0),      # :145 回退默认
    ```
  - 影响: 持久化数据中 `blend: null` 或非数值时，sanitized 结果为 0.5 而非文档默认 0.8，同一参数在"缺字段"（0.8）与"字段非法"（0.5）两条路径产生不同历史混合强度；其余字段（threshold/scene_cut_sensitivity/radius/luma 等）回退默认均与字段默认一致，唯 blend 错位。
  - 建议: 将 `_as_float(self.blend, 0.5)` 的回退值改为 `0.8`，与其余字段的"回退=字段默认"模式对齐。
  - 置信度: 高

- [AUD-38904] [Suggestion] `for_quality` 对未知 quality_mode 静默落入 export 最高成本分支
  - 位置: src/shared/video/denoise/params.py:236-238
  - 证据:
    ```python
    else:  # export
        temporal = replace(p.temporal, frames=3, motion="accurate")
        spatial = replace(p.spatial, mode="enhanced")
    return VideoDenoiseParams(temporal=temporal, spatial=spatial)
    ```
  - 影响: `QUALITY_MODES = ("preview", "paused", "export")` 之外的任何字符串（拼写错误或未来新枚举）都会按 export 处理：accurate 运动估计 + enhanced 空域 + 3 帧窗口，是三者中最贵的路径，方向性错误（未知值应保守降级而非升到最贵）。
  - 建议: 显式匹配三种模式，未知值回退 preview（或断言/日志），与"外部输入须验证"铁律一致。
  - 置信度: 高（行为确凿；当前调用方均传合法值，故降为 Suggestion）

### 通过项

- 线程安全：三个 dataclass 均 `frozen=True`，跨线程（UI 构建、渲染线程读取）按值安全，符合模块 docstring 声明的并发模型。
- GLSL·HDR 正确性：纯参数定义，无 GLSL/颜色运算。
- 信号防递归：不涉 Qt。
- 状态提交一致性：`to_dict`/`from_dict` 对称，`from_dict` 强制 `.sanitized()`，损坏/越界持久化值不会进入运行态；quality_mode 明确由渲染上下文决定、不随参数持久化，职责划分清晰。
- 异常与取消路径：`_as_float`/`_as_int` 仅捕获 `(TypeError, ValueError)`，无宽泛 except；非 dict 输入回退默认实例。
- 输入验证：`sanitized()` 对枚举（mode/motion）、范围（luma/chroma 0-100、threshold/blend/scene_cut 0-1、radius 1-10、frames ∈ {1,2,3}）全字段收敛，`_as_bool` 覆盖 bool/int/str。
- 性能模式：`parameters_key` 以 sanitized 值为键保证参数变化必失效历史；`is_active()` 每次重建 sanitized 对象开销可忽略（参数对象调用频率低）。
- 类型注解：完整。

---

## src/shared/video/denoise/window_provider.py (行数: 323)

### 发现

- [AUD-38905] [Major] `shutdown()` 全库无调用点，卸载视频后独立解码会话泄漏
  - 位置: src/shared/video/denoise/window_provider.py:193-205（对照 :88-91、:175-191）
  - 证据:
    ```python
    def clear(self) -> None:
        """作废所有在途请求（换素材/卸载视频时调用）。"""
        self._debounce_token += 1
        self._dispatch_token += 1
    ...
    def shutdown(self) -> None:
        """释放帧服务会话并停止后台线程（应用退出时调用）。"""
        self.clear()
        if self._clip_id:
            ...get_frame_provider_service().close_clip(self._clip_id)...
    ```
  - 影响: grep 全库确认 `shutdown()` 零调用点；`file_controller.unload_video`（file_controller.py:6640-6643）与加载失败路径（:4781-4783）只调 `provider_obj.clear()`，而 `clear()` 仅递增 token、不关闭会话。`_ensure_clip` 以 `share_by_path=False` 打开的独立 ClipSession（:185）只在"换路径"时被关闭（:177-183），且 frame_provider_service 无按数量/内存的会话级淘汰（仅会话内 evict_old_frames）。结果：卸载/关闭当前视频后，该视频的独立解码器句柄与缓存帧滞留服务内，直到打开下一个不同视频或应用退出。
  - 建议: 在 `unload_video`/加载失败路径调用 `provider.shutdown()`（或给 `clear()` 增加 `close_session=True` 参数）；同时修 shutdown 自身的竞态——工作线程可能在 shutdown 关闭会话后经 `_ensure_clip` 重新打开新会话且永不再关（UI 线程写 `_clip_id` 与工作线程读写无锁交叉），建议由单一线程持有会话生命周期或在 shutdown 后置 `_shutdown` 标志使 `_enqueue`/`_process_job` 直接丢弃。
  - 置信度: 高（零调用点与 clear 语义经 grep 与消费端代码证实）

- [AUD-38906] [Suggestion] token 校验与 emit 之间存在 TOCTOU，且消费端不校验 center_frame
  - 位置: src/shared/video/denoise/window_provider.py:121-126
  - 证据:
    ```python
    def _process_job(self, path, frame, before, after, token):
        payload = self._fetch_window(path, frame, before, after)
        if token != self._dispatch_token:
            return  # 请求已过期（seek 到了别处/换了素材）
        # 空 payload（无可用邻帧）同样投递，用于清除渲染器里的旧窗口。
        self.window_ready.emit(payload)
    ```
  - 影响: token 检查（工作线程）与 UI 侧新 `schedule()`/`clear()` 之间存在微秒级竞窗，过期 payload 仍可能投递；消费端 `_on_video_denoise_window_ready`（file_controller.py:6315-6333）只校验 path 与播放态，不校验 `center_frame` 是否等于当前帧，同 clip 内 seek 后短暂应用错误邻帧窗口（新请求到达后自愈）。
  - 建议: 把 dispatch_token 放进 payload，在 UI 线程信号处理器内再比对一次（防御纵深）；消费端补 `payload.get('center_frame')` 与当前帧一致性的校验。
  - 置信度: 中（竞窗极小且自愈，机制与消费端缺口均经代码证实）

- [AUD-38907] [Suggestion] 模块 docstring 称宽度上限 1920，实际常量为 2560
  - 位置: src/shared/video/denoise/window_provider.py:6（对照 :30）
  - 证据:
    ```python
    - CPU 转换为归一化 RGBA float16（宽度上限 1920，控制上传与显存预算）；   # :6
    ...
    _MAX_NEIGHBOR_WIDTH = 2560                                              # :30
    ```
  - 影响: 文档与实现不一致，读者按 1920 估算显存（4K→1920 约 16.6MB/帧）会低估约 78%（2560×1440×4ch×2B ≈ 29.5MB/帧）；常量旁注释的算术（2560）才是真实口径。
  - 建议: 将 docstring 的 1920 更新为 2560，或注明历史调整。
  - 置信度: 高

- [AUD-38908] [Suggestion] `_debounce_token` 只写不读，属死状态
  - 位置: src/shared/video/denoise/window_provider.py:44、78、90
  - 证据:
    ```python
    self._debounce_token = 0        # :44
    ...
    self._debounce_token += 1       # :78（schedule 内）
    self._debounce_token += 1       # :90（clear 内）
    ```
  - 影响: 全文件无任何读取/比较点，真正生效的是 `_dispatch_token`；死状态误导维护者以为 provider 内含去抖逻辑（docstring 也称"去抖合并由调用方完成"），增加误改风险。
  - 建议: 删除 `_debounce_token`，仅保留 `_dispatch_token`。
  - 置信度: 高

- [AUD-38909] [Suggestion] 多处宽泛 except 静默吞错，违反"不得宽泛 except 掩盖领域错误"铁律
  - 位置: src/shared/video/denoise/window_provider.py:75-76、179-181、201-202
  - 证据:
    ```python
    except Exception:
        return                      # :75-76 schedule 内 temporal 检查
    ...
    except Exception:
        pass                        # :180-181 _ensure_clip 关闭旧会话
    ...
    except Exception:
        pass                        # :201-202 shutdown 关闭会话
    ```
  - 影响: `params.temporal.is_active()` 的任意属性错误（如参数结构变更引入的 AttributeError）被静默吞掉，表现为"暂停态降噪悄悄不生效"且无日志；close_clip 失败无痕迹，会话泄漏难以定位。对比同文件 `_fetch_window`/`_worker_loop` 均已 `logger.exception`，此处不一致。
  - 建议: 三处至少补 `logger.debug/exception`；schedule 的检查应缩小到预期异常或前置类型判断。
  - 置信度: 高

- [AUD-38910] [Suggestion] `_MATRIX_MAP` 未覆盖 bt2020c，未知矩阵静默按 BT.709 转换
  - 位置: src/shared/video/denoise/window_provider.py:212-218、294
  - 证据:
    ```python
    _MATRIX_MAP = {
        'bt709': 0,
        'smpte170m': 2,
        'bt470bg': 2,
        'bt2020nc': 1,
        'bt2020': 1,
    }
    ...
    matrix=matrix_map.get(_MATRIX_MAP.get(matrix_str, 0), ColorMatrix.BT709),
    ```
  - 影响: BT.2020 恒定亮度（bt2020c）及其他未列举矩阵（如 smpte240m/fcc）的平面帧会按 BT.709 矩阵做 YUV→RGB，邻帧颜色系统性偏移；对比色度、scene-cut 检测在 HDR 广色域素材上失真。发生频率低（bt2020c 素材罕见）。
  - 建议: 在 `_MATRIX_MAP` 补 'bt2020c' 映射（或映射到 BT2020_NC 并注明近似），未知矩阵至少 debug 日志一次。
  - 置信度: 中（缺口确凿；实际素材中 bt2020c 出现率未知）

- [AUD-38911] [Suggestion] `_packed_to_rgb` 对未知 4/3 通道格式按 BGRA/BGR 猜测
  - 位置: src/shared/video/denoise/window_provider.py:319-322
  - 证据:
    ```python
    if data.shape[-1] == 4:
        return cv2.cvtColor(data, cv2.COLOR_BGRA2RGB)
    if data.shape[-1] == 3:
        return cv2.cvtColor(data, cv2.COLOR_BGR2RGB)
    return None
    ```
  - 影响: `pixel_format` 不在枚举内但通道数为 3/4 时（如 'argb'、'gbrp' 打包变体或未来解码器新格式），按 BGR 系猜测转换；若真实为 RGB 系则红蓝互换，邻帧与中心帧颜色空间不一致直接污染时域累积。
  - 建议: 对未识别格式返回 None（跳过该邻帧，与缺帧退化语义一致）或记录 debug 日志；不做的颜色猜测比猜错更安全。
  - 置信度: 中（当前枚举已覆盖常见格式，触发面窄）

- [AUD-38912] [Suggestion] `_ensure_clip` 的 `service` 参数缺类型注解
  - 位置: src/shared/video/denoise/window_provider.py:167
  - 证据:
    ```python
    def _ensure_clip(self, service, path: str) -> Optional[str]:
    ```
  - 影响: 本文件其余公共/私有方法均全注解，唯此处裸参数；`service` 实为 `VideoFrameProviderService`（延迟导入避免循环依赖），缺注解削弱 IDE/静态检查与一致性。
  - 建议: 使用字符串注解 `"VideoFrameProviderService"`（配合 TYPE_CHECKING 导入）补齐。
  - 置信度: 高

### 通过项

- GPU 资源生命周期：本模块零 OpenGL/ModernGL 调用；f16 ndarray 经 `window_ready` → `renderer.set_video_denoise_window` → `RenderCommand.SET_VIDEO_DENOISE_WINDOW`（async_renderer.py:1076-1082）由渲染线程消费，符合"UI 线程不得操作 OpenGL"铁律；`quality='master'` 的独立会话（share_by_path=False）正确隔离了逐帧 seek 与播放解码器（G-P0）。
- GLSL·HDR 正确性：uint8/255.0、uint16/65535.0 归一化，float 数据直通不重复缩放，全程无 clamp(0.0,1.0)，输出 float16 与项目 HDR 内部格式一致。
- 信号防递归：provider 不触碰任何 UI 控件；`window_ready` 从工作线程发射经 Qt 队列连接投递 UI 线程，机制正确（类 docstring "在 UI 线程发射"措辞不严谨，实为"在工作线程发射、UI 线程交付"，无行为问题）。
- 线程模型：单后台 daemon 工作线程串行消费 `queue.Queue`，`_ensure_worker` 以 `_worker_lock` 保护创建；`_clip_id`/`_clip_path` 仅工作线程写（shutdown 例外，见 AUD-38905）。
- 异常与取消路径：`_worker_loop` 捕获后 `logger.exception` 继续循环不崩线程；转换失败逐帧跳过并记 frame_index；取帧失败返回空 dict 且仍投递以清除渲染器旧窗口——"缺失即退化，绝不阻塞交互"的设计成立。
- 输入验证：`schedule` 校验空路径/负帧号/temporal 未启用；before/after 钳到 [1,3]；`_frame_to_rgba_f16` 校验宽高/维度/通道数，None 帧经宽高检查自然跳过。
- 性能模式：`import cv2` 与 color_decode 均延迟导入；`_MAX_NEIGHBOR_WIDTH=2560` + INTER_AREA 控制上传与显存（4K→2560 约 29.5MB/帧 f16，4 邻帧≈118MB，注释算术核对无误）；`QTimer.singleShot(0)` 派发避免在调用方栈内同步入队。

---

## 汇总

| 严重度 | 数量 |
|--------|------|
| Critical | 0 |
| Major | 1（AUD-38905） |
| Suggestion | 11（AUD-38901–38904、38906–38912） |
