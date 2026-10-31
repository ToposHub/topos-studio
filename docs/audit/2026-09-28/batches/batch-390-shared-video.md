# 批次报告：batch-390 [P2] src/shared/video//

编号区间：AUD-39000–AUD-39099
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失

---

## src/shared/video/external_yuv_renderer.py (行数: 269)

### 发现

- [AUD-39000] [Major] 宽泛 `except Exception` 静默吞掉 GL/着色器领域错误，且 render 失败路径不恢复 GL 状态
  - 位置: src/shared/video/external_yuv_renderer.py:150-152, 226-233, 257-258
  - 证据:
    ```python
    # _ensure_resources (150-152)
    except Exception:
        self.release()
        return False
    # render (257-258)
    except Exception:
        return False
    ```
    以及 render 内的裸 GL 状态修改（226-233）：
    ```python
    glUseProgram(program.glo)
    glUniform1i(glGetUniformLocation(program.glo, "u_y_plane"), 0)
    ...
    glActiveTexture(GL_TEXTURE0 + 1)
    glBindTexture(texture_target, int(uv_plane.texture_id))
    ```
  - 影响: 直接违反铁律"不得宽泛 except 掩盖领域错误"。着色器编译失败、Uniform 设置失败等根因被完全丢弃，调用方（timeline_clip_frame_provider.py）只能记下泛化的 `external_yuv_render_failed`，无任何异常细节可诊断。此外 render 中途抛异常时 active texture unit 停留在 GL_TEXTURE1、裸 `glUseProgram` 绑定残留，且 `output_fbo` 已被 `ctx.clear` 清空，调用方随后进入的 CPU 回退路径需继承这些脏状态。
  - 建议: 在两处 except 中以 `logger.warning(..., exc_info=True)` 记录异常后再返回 False；render 的失败/成功路径结束时将 active texture unit 恢复为 GL_TEXTURE0。
  - 置信度: 高

- [AUD-39001] [Suggestion] 着色器输出无条件 `clamp(0.0, 1.0)` 将 SDR unorm 假设固化在通用 pass 中
  - 位置: src/shared/video/external_yuv_renderer.py:55-56
  - 证据:
    ```glsl
    f_color = vec4(clamp(r, 0.0, 1.0), clamp(g, 0.0, 1.0),
                   clamp(b, 0.0, 1.0), 1.0);
    ```
  - 影响: 类文档声明支持 NV12/P010（P010 承载 10-bit bt2020 HDR 内容），而当前唯一调用方（src/features/edit/core/timeline_clip_frame_provider.py:449 `dtype="f1"` unorm8 FBO）下 clamp 无额外损失；但本类是共享 API，若未来输出到 f16/f32 HDR FBO，转换后 >1.0 的高光值会被静默截断，触碰"HDR 内部数据不得无理由 clamp"铁律。
  - 建议: 按输出目标格式（unorm 才 clamp）或按 color_matrix/range 门控 clamp；至少在类文档中写明"仅支持 unorm 输出目标"的契约。
  - 置信度: 中

- [AUD-39002] [Suggestion] GL 上下文失效后资源缓存永不重建，pass 永久静默退化为 CPU 路径
  - 位置: src/shared/video/external_yuv_renderer.py:117-119
  - 证据:
    ```python
    def _ensure_resources(self) -> bool:
        if self._program_2d is not None and self._program_rect is not None:
            return self._vao_2d is not None and self._vao_rect is not None
    ```
  - 影响: 缓存仅以"非 None"判断有效性。上下文丢失/重建后失效的 program/VAO 不会重建，`render` 每帧走 except 返回 False，外部 GPU 零拷贝路径永久降级且无提示。另外 programs 非 None 而 VAO 为 None 的分支返回 False 却不尝试重建（当前为不可达死分支）。
  - 建议: 提供 `invalidate()` 方法；或 render 捕获到资源失效类异常时清空缓存并重试一次初始化。
  - 置信度: 中

### 通过项
- 输入验证：planes 数量/类型/api/texture_id/target 匹配/目标尺寸校验完备（181-195），不满足契约时在发出任何 draw 前返回 False
- GLSL 规范：所有浮点字面量显式带小数点；无常量上限循环；`#version 330` 下 sampler2DRect 写法合法；raw glUniform1i 绑定 sampler 单元的做法有注释说明且正确
- GPU 资源生命周期：release() 按 VAO→VBO→Program 顺序释放并置 None；初始化失败路径调用 release()；类文档明确 plane 所有权归调用方（fence 后释放）
- 线程边界：类文档明确仅在渲染线程使用，无可变共享状态
- YUV 数学：limited/full range 的 luma/chroma 展开系数与 bt601/bt709/bt2020 矩阵系数正确；UV 分量按 internal_format 区分 R/A 与 R/G 模式
- 信号防递归 / 状态提交一致性：纯渲染 pass，无 Qt 信号与 undo/redo 参与
- 类型注解：完整（以 Any 表述 moderngl 对象，属有意延迟导入）

---

## src/shared/video/fallback_types.py (行数: 626)

### 发现

- [AUD-39003] [Critical] `export_to_json` 持有非重入锁时再次加锁，调用即确定性死锁
  - 位置: src/shared/video/fallback_types.py:363-371（锁声明 :270，二次加锁 :326）
  - 证据:
    ```python
    def export_to_json(self) -> str:
        import json
        with self._events_lock:
            data = {
                'events': [e.to_dict() for e in self._events],
                'statistics': self.get_statistics(),
            }
        return json.dumps(data, indent=2)
    ```
    而 `get_statistics`（:326）内部同样 `with self._events_lock:`，且 `_events_lock = threading.Lock()`（:270）非重入。
  - 影响: 同线程对非重入 Lock 二次 acquire 将永久阻塞。任何调用 `export_to_json()`（如导出诊断 JSON，典型在主线程）都会冻结整个应用且无法恢复。当前仓库内尚无调用点，属已导出公共 API 的潜伏地雷。
  - 建议: 在锁内只拷贝 events 快照或预先计算统计，锁外组装 JSON；或改用 `threading.RLock`。为 `export_to_json` 补一条单测覆盖死锁路径。
  - 置信度: 高

- [AUD-39004] [Major] `get_recent` 实际返回时间正序，与文档承诺"最新的在前"相反
  - 位置: src/shared/video/fallback_types.py:301-312
  - 证据:
    ```python
    def get_recent(self, limit: int = 10) -> List[FallbackEvent]:
        """
        ...
        Returns:
            事件列表（最新的在前）
        """
        with self._events_lock:
            return list(self._events[-limit:])
    ```
  - 影响: `[-limit:]` 保留最新 N 条但保持时间正序（窗口内最旧在前）。现有消费方 `has_recent_fallback`/`get_last_fallback_reason`（:459, :467）均用 limit=1 恰好不受影响；但任何按文档语义以 limit>1 消费（`get_recent_fallback_events` 已在 `src/shared/video/__init__.py:157` 导出）的代码取 `events[0]` 会拿到窗口内最旧事件，得出错误的"最近一次 fallback"结论。
  - 建议: 返回 `list(reversed(self._events[-limit:]))`，或修正 docstring 为"时间正序"。
  - 置信度: 高（不一致本身确凿；对现有调用方为潜伏影响）

- [AUD-39005] [Suggestion] `get_recent(limit=0)` 因 `[-0:]` 切片语义返回全部事件
  - 位置: src/shared/video/fallback_types.py:312
  - 证据:
    ```python
    with self._events_lock:
        return list(self._events[-limit:])
    ```
  - 影响: `limit=0` 时 `self._events[-0:]` 等价于 `[0:]`，返回全部事件而非空列表，违反"limit=最大数量"的直觉契约（负数 limit 同样产生意外切片）。
  - 建议: 入口处 `limit = max(0, int(limit))` 并对 0 直接返回 `[]`。
  - 置信度: 高

- [AUD-39006] [Suggestion] 模块 docstring 示例 `log_fallback(event)` 用法错误，照抄会抛 AttributeError
  - 位置: src/shared/video/fallback_types.py:26
  - 证据:
    ```python
    event = FallbackEvent(
        reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
        ...
    )
    log_fallback(event)
    ```
  - 影响: `log_fallback` 首个位置参数是 `reason` 枚举（:382-383），传入 FallbackEvent 会构造出 `reason=<FallbackEvent 实例>` 的事件，随后 `log()` 调用 `event.reason.requires_investigation()` 时抛 AttributeError。示例应演示 `fallback_logger.log(event)`。
  - 建议: 修正模块头部示例为 `fallback_logger.log(event)`。
  - 置信度: 高

- [AUD-39007] [Suggestion] 单例 `__init__` 的 `_initialized` 检查-设置非原子；`import time` 未使用
  - 位置: src/shared/video/fallback_types.py:47, 264-268
  - 证据:
    ```python
    import time          # 全文无 time.* 使用

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
    ```
  - 影响: 若两个线程在单例首次创建期并发调用 `FallbackLogger()`，check-then-set 竞态可导致重复初始化（重建 `_events`/`_events_lock`，丢失已记录事件）。因模块级单例在导入时已完成初始化（:375），实际触发概率极低。未使用的 `time` 导入属死代码。
  - 建议: 将 `_initialized` 检查移入 `_lock` 保护内，或直接删除 `__init__` 判重逻辑（单例仅经模块级实例使用）；删除 `import time`。
  - 置信度: 高（代码事实），影响低

- [AUD-39008] [Suggestion] extract_* 辅助函数提前返回阻断回退链，且依赖私有属性、可能返回字符串 "None"
  - 位置: src/shared/video/fallback_types.py:490-492, 518-521, 550-551, 499-500, 606-611
  - 证据:
    ```python
    if hasattr(source, 'metadata') and source.metadata:
        return getattr(source.metadata, 'codec_name', None)   # 提前 return，不回退 source.codec_name
    ...
    if hasattr(source, 'backend'):
        return str(source.backend)                             # backend=None 时得到 "None"
    ...
    if hasattr(session, '_degradation_level'):
        degradation_level = session._degradation_level          # 私有属性耦合
    ```
  - 影响: metadata 存在但 `codec_name`/`pixel_format` 缺失时直接返回 None，不尝试后续的 `source.codec_name`/`source.pixel_format` 回退，fallback 事件丢失关键上下文；`str(None)` 产生哨兵字符串 "None" 污染统计分组；读取 `_codec`/`_degradation_level`/`_source_type` 私有属性使提取逻辑对实现重构脆弱。
  - 建议: metadata 分支取值失败时继续回退链而非 return；`backend` 判 None 后再 str；优先约定公共访问器，私有属性仅作最后手段。
  - 置信度: 高（行为确凿），影响低

### 通过项
- FallbackEvent dataclass 字段完整、to_dict/to_log_string 键一致，`__post_init__` 一致性告警合理
- 容量控制：`_max_events=100` 裁剪旧事件防止无界增长；`log()` 在锁外执行日志 IO，避免持锁阻塞
- get_statistics/get_all/get_recent 均返回拷贝，不泄漏内部列表
- 枚举分类方法（is_user_forced/is_technical_failure/requires_investigation）逻辑正确
- 类型注解完整；本模块无 GPU/Qt 信号/undo 参与维度

---

## src/shared/video/frame_provider_protocol.py (行数: 215)

### 发现

- [AUD-39009] [Suggestion] `FrameProvider.get_frame` 返回类型 `object` 过弱
  - 位置: src/shared/video/frame_provider_protocol.py:190-199
  - 证据:
    ```python
    def get_frame(self, frame_index: int) -> object:
        """Get a frame at the given index.
        ...
        Returns:
            Image data (numpy array or similar).
        """
    ```
  - 影响: 跨 feature 最小协议的核心取值方法没有任何可用的类型信息，消费方只能盲转或忽略类型检查，削弱了"完整类型注解"的项目原则与静态检查价值。
  - 建议: TYPE_CHECKING 下引入更精确的返回类型（如 `Optional["np.ndarray"]` 或项目统一的帧类型别名），或在文档字符串外以注解表达 ndarray 语义。
  - 置信度: 高（注解确实为 object），影响低

### 通过项
- 通过（其余无逻辑问题）：纯 Protocol 声明文件，方法体均为 `...`，无运行时逻辑
- 两个协议职责划分清晰（FrameProviderProtocol 面向播放/导出，FrameProvider 面向跨 feature 最小访问），`@runtime_checkable` 使用正确
- DecodedFrame/VideoMetadata 通过 TYPE_CHECKING 导入，避免运行时循环依赖
- 与 SourceManager/ColorFrameProviderAdapter 的引用计数约定在 close() 文档中表述一致
