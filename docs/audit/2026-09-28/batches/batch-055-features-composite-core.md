# batch-055 审查报告 [P1] src/features/composite/core/

- 任务书: docs/audit/2026-09-28/assignments/batch-055.md
- 编号区间: AUD-5500–AUD-5599
- 审查维度: 线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 交叉验证文件（只读）: src/features/composite/nodes/video_frame_node.py、src/features/composite/ui/composite_page.py、src/shared/video/frame_provider_service.py、tests/composite/test_composite_video_frame_index.py

## src/features/composite/core/video_context.py (行数: 1105)

### 发现

- [AUD-5500] [Major] shutdown() 先清空 _node_clips 再调 close_clip_for_node，服务级剪辑从未被关闭（资源泄漏）
  - 位置: src/features/composite/core/video_context.py:1045-1052
  - 证据:
    ```python
        with self._lock:
            node_ids = list(self._node_clips.keys())
            self._node_clips.clear()
            self._clip_params.clear()

        # 关闭所有剪辑
        for node_id in node_ids:
            self.close_clip_for_node(node_id)
    ```
    而 close_clip_for_node 的第一步是（行 468-471）：
    ```python
        with self._lock:
            clip_id = self._node_clips.pop(node_id, None)
            if clip_id is None:
                return False
    ```
  - 影响: _node_clips 已被 clear，循环内每个 close_clip_for_node 都 pop 到 None 直接 return False，self._service.close_clip(clip_id) 永远不会执行。docstring 承诺"关闭所有剪辑，释放资源"，实际所有剪辑在 VideoFrameProviderService 侧保持打开（解码器/文件句柄/帧缓存泄漏）。reset_instance()（行 420-422，"用于测试或应用重置"）经此路径，一旦被应用重置或测试复用全局单例触发，泄漏即发生。当前 src/ 无生产调用方（仅类内部引用），属潜伏缺陷。
  - 建议: 在持锁清空前先收集 (node_id, clip_id) 对（node_ids 改为 items 快照），清空后直接对每个 clip_id 调 self._service.close_clip(clip_id)（service 侧为引用计数，按节点逐个调用即可），不再绕道 close_clip_for_node。
  - 置信度: 高

- [AUD-5501] [Major] _compute_source_frame 未验证 source_in/out 区间，duration<=0 时 loop 路径 ZeroDivisionError 或负数模导致错误帧
  - 位置: src/features/composite/core/video_context.py:933-951
  - 证据:
    ```python
        # 检查范围
        source_duration_frames = source_out - source_in + 1

        if source_frame > source_out:
            if loop:
                # 循环：计算偏移后的帧
                offset = (source_frame - source_out - 1) % source_duration_frames
                source_frame = source_in + offset
            elif hold:
    ```
    调用方直接透传节点参数且无交叉校验（video_frame_node.py:705-716 取 self.parameters 后原样传入），ParamDef 仅各自限界（video_frame_node.py:189-190: source_in min=0 max=10^7、source_out min=-1 max=10^7），允许 source_in=10, source_out=9 这类组合；对照同项目节点侧镜像实现有守卫（video_frame_node.py:975-976 `if range_len > 0 and frame_number > source_out:`），本方法缺失。
  - 影响: source_out == source_in - 1（duration=0）且 loop=True 时，`% source_duration_frames` 抛 ZeroDivisionError——request_frame_for_node / request_gpu_frame_for_node 调用点有 try/except 兜底（返回 None，帧缺失），但 prefetch_for_node（video_frame_node.py:1205 调用链，行 1535 处未包裹）会让解码 worker 线程异常退出。source_out < source_in - 1 时 duration 为负，Python 负模产生负 offset，映射出区间外/错误帧（渲染错误且无报错）。另 total_frames=0（ClipInfo 默认值，frame_provider_service.py:514）且 source_out=-1 时同样得到 duration=0。
  - 建议: 在取模前加 `if source_duration_frames <= 0: return -1`（或钳位 source_in/source_out 互换），与节点侧 `range_len > 0` 守卫对齐；set_node_params 处亦可前置校验 source_out >= source_in。
  - 置信度: 高（缺陷事实确定；触发需用户录入倒置区间，UI 目前不阻止）

- [AUD-5502] [Major] _timeline 共享可变状态完全绕过 _lock，UI 写入与渲染/解码线程读取存在数据竞争
  - 位置: src/features/composite/core/video_context.py:522-529, 175-184, 668-669, 732
  - 证据:
    ```python
        def set_timeline_frame(self, frame: int) -> None:
            ...
            self._timeline.update_frame(frame)
    ```
    ```python
        def update_frame(self, frame: int) -> None:
            ...
            self.current_frame = frame
            if self.fps > 0:
                self.time_seconds = frame / self.fps
    ```
    ```python
        # 确定时间线帧号
        if timeline_frame is None:
            timeline_frame = self._timeline.current_frame
    ```
    而 _lock 只保护 _node_clips/_clip_params（行 449、468、488、988、1011、1026、1045），timeline 的读（request_frame_for_node:669、request_gpu_frame_for_node:730/732、get_stats:1034）与写（set_timeline_frame/set_timeline_fps/set_exporting/set_render_quality）均在锁外；get_timeline_context()（行 513-520）还把可变单例直接暴露给调用方。
  - 影响: 本类自述设计为"所有 composite node evaluate 都能拿到 timeline frame"（evaluate 运行在渲染/解码 worker 线程，文件头注释亦确认 worker 线程会进入本类），而 set_timeline_frame/set_exporting 由 UI 线程调用时：update_frame 的 current_frame+time_seconds 双字段写可被撕读；fps 变更与帧映射并发导致错帧；set_exporting 在导出中翻转 render_quality 会让同一导出内 proxy/master 混取（文件自己强调 VT/CPU 混显是需避免的问题）。当前 src/ 生产代码尚未接线 timeline 写入 API（仅 tests/composite/test_composite_video_frame_index.py 单线程调用），风险为 API 级潜伏缺陷，但模块 docstring 的使用示例（行 29-44）正是这种跨线程用法。
  - 建议: timeline 读写统一纳入 _lock（或为 TimelineContext 提供原子快照 copy/snapshot 方法，读侧取快照）；get_timeline_context 返回防御性副本或只读视图；set_exporting 的质量切换与请求路径用同一锁串行化。
  - 置信度: 中（竞争窗口与设计意图确定；当前生产调用图未实际触发）

- [AUD-5503] [Suggestion] request_gpu_frame_for_node 缺返回类型注解；is_stale 注解用了非法的小写 callable
  - 位置: src/features/composite/core/video_context.py:694-710
  - 证据:
    ```python
        interactive: bool = False,
        deadline_s: Optional[float] = None,
        is_stale: Optional[callable] = None,
        coarse: bool = False,
    ):
        """请求 GPU 帧（节点专用接口，macOS VT 零拷贝优先）。
    ```
    方法签名无 `->` 返回注解（对比 request_frame_for_node:622 有 `-> Optional[DecodedFrame]`；本方法实际返回 GpuDecodedFrame 或 DecodedFrame 联合）。
  - 影响: `callable` 是内置函数而非类型，`Optional[callable]` 不是合法类型注解（因 `from __future__ import annotations` 未求值才不报错），类型检查器报错且调用方无法得知回退时返回类型变化；返回类型缺失削弱强类型约束（.clinerules 要求全函数类型注解）。另 _compute_source_frame 声明 `fps: float`（行 879）却在行 911 检查 `fps is None`，注解与实现不符。
  - 建议: 改为 `is_stale: Optional[Callable[[], bool]] = None`（Callable 已在行 98 导入）；补 `-> Optional["GpuDecodedFrame"]` 或联合类型注解并在 docstring 说明回退返回 DecodedFrame；fps 注解改为 Optional[float] 或删掉死 None 分支。
  - 置信度: 高

- [AUD-5504] [Suggestion] TimelineContext.update_time 用 int() 截断，与 _compute_source_frame 已修复的同类浮点差一帧问题不一致
  - 位置: src/features/composite/core/video_context.py:186-195
  - 证据:
    ```python
        def update_time(self, seconds: float) -> None:
            """
            ...
            """
            self.time_seconds = seconds
            if self.fps > 0:
                self.current_frame = int(seconds * self.fps)
    ```
    同文件行 923-927 已明确记载该类问题并用 round 修复："round 而非 int 截断：timeline/fps*fps 的浮点往返在部分帧号上得到 N-0.0000...1，截断会得到 N-1"。
  - 影响: seconds 由 frame/fps 浮点往返而来时（如 201/25 再 *25），int() 截断得到 N-1，时间驱动的播放头/导出起点偏一帧——与文件声明的"导出范围第一帧不偏帧"目标（行 88、230）相悖。
  - 建议: 与 _compute_source_frame 一致改用 `int(round(seconds * self.fps))`。
  - 置信度: 中（浮点截断问题确定；实际偏帧取决于调用方是否存在往返换算，src/ 内暂无调用方）

- [AUD-5505] [Suggestion] RenderQuality.AUTO 语义未实现，to_frame_quality 恒返回 'proxy' 且全库无调用者
  - 位置: src/features/composite/core/video_context.py:116-139, 246-261
  - 证据:
    ```python
        AUTO: 自动选择（根据 render_mode 决定）
        ...
        def to_frame_quality(self) -> str:
            if self == RenderQuality.EXPORT:
                return 'master'
            else:
                return 'proxy'
    ```
    get_quality_for_mode（行 246-261）同样只在 render_mode=='export' 或 render_quality==EXPORT 时给 'master'，AUTO 与 PREVIEW 完全同义。
  - 影响: AUTO 的 docstring 承诺"根据 render_mode 决定"，实际 to_frame_quality 不接收 render_mode、恒映射 'proxy'，与文档矛盾；一旦调用方按文档使用 AUTO 将得到错误质量。全库 grep 无 to_frame_quality 调用者，属死接口，误导后续开发。
  - 建议: 要么实现 AUTO（to_frame_quality 接受 render_mode 参数并在 AUTO 时委托 get_quality_for_mode），要么删除 AUTO 值与 to_frame_quality 死方法，收敛质量决策入口到 get_quality_for_mode。
  - 置信度: 高（行为与文档矛盾、无调用者均为可验证事实）

- [AUD-5506] [Suggestion] set_node_params/get_node_params 以 clip_id 为键，共享同一文件的多个节点参数互相覆盖；且该 API 与 VideoNodeParams 在 src/tests 均无调用者
  - 位置: src/features/composite/core/video_context.py:444-456, 966-1012, 1061-1082
  - 证据:
    ```python
        Note:
            多个节点引用同一文件共享 clip_id
        ...
        with self._lock:
            self._node_clips[node_id] = clip_id
            # 初始化节点参数（如果还没有）
            if clip_id not in self._clip_params:
                self._clip_params[clip_id] = VideoNodeParams()
    ```
    set_node_params(clip_id, ...) / get_node_params(clip_id) 均按 clip_id 存取（行 988-1012），_node_clips 明确支持多 node_id → 同一 clip_id。
  - 影响: 两个 VideoFrameNode 引用同一文件但需要不同 source_in/out 时，后设置的参数覆盖前者，无法表达 per-node 参数；实际帧请求路径（request_frame_for_node）绕过 _clip_params 直接接收显式参数，故该存储当前无消费者——全库 grep 确认 set_node_params/get_node_params/VideoNodeParams 在 src/ 与 tests/ 均无调用者（死代码）。
  - 建议: 若保留 API，键改为 node_id（与 _node_clips 对齐）或存 (node_id, clip_id) 二级映射；若确认弃用则删除 _clip_params、set/get_node_params 与 VideoNodeParams，减少误导。
  - 置信度: 高（覆盖语义与无调用者均为可验证事实）

- [AUD-5507] [Suggestion] has_vt_source_for_node 宽泛 except Exception 直接返回 False，掩盖服务错误并反转 CPU 帧放行策略
  - 位置: src/features/composite/core/video_context.py:759-762
  - 证据:
    ```python
        try:
            return self._service.has_vt_source(clip_id)
        except Exception:
            return False
    ```
  - 影响: 方法自身 docstring 说明返回值决定放行策略——False（未挂载 VT）时 CPU 帧立即放行。任何服务侧异常（含真实领域错误）都被吞掉并当作"无 VT 源"，触发 docstring 警告的 VT/CPU 颜色混显场景；违反 .clinerules"不得宽泛 except 掩盖领域错误"，且无任何日志。
  - 建议: 缩窄捕获范围（服务侧声明的具体异常）或至少 logger.warning 记录异常；对"clip 不存在"与"查询出错"区分返回语义。
  - 置信度: 中（宽泛 except 与策略反转的事实确定；实际触发依赖服务侧抛错场景）

### 通过项

- 信号防递归: 文件不含 Qt 信号；frame-ready handler 注册/注销/读取均在 _lock 下，clear 用身份比较防误清新页面 handler，handler 在锁外调用（无死锁），异常路径为 T-P1-71 文档化的预期降级（composite_page.py 蹦床模式互证）
- GPU 资源生命周期: 本文件不直接持有 ModernGL/纹理资源，GpuDecodedFrame/DecodedFrame 仅透传（deferred_import CVPixelBuffer 由渲染线程绑定，符合 deferred-release 架构）；剪辑生命周期经 service 引用计数（close_clip_for_node 先判共享再关服务，正确），唯 shutdown 排序缺陷见 AUD-5500
- GLSL·HDR 正确性: 不涉及（无 GLSL 代码、不触碰像素/颜色数据，帧对象不透明透传，无 clamp 风险面）
- 状态提交一致性: 不参与 undo/redo/revision；to_dict/from_dict 序列化对称存在但生产未接线（无持久化调用方，风险面有限）
- 异常与取消路径: request/prefetch 系列对 clip_info 缺失统一返回 None/False 不抛出；fps<=0 回退素材真实帧率有充分注释（行 663-665、725-727、782-784、833-835、907-911，含历史回归说明）；round 修复浮点截断（行 923-927）正确
- 性能模式: 锁粒度合适，所有 service 调用（open/close/request/prefetch/get_clip_info）均在 _lock 外执行不持锁做 IO；get_stats 聚合一次性加锁；单例创建用类级锁双检正确
- 输入验证（部分通过）: timeline_frame/fps/quality 的 None 与非法值回退路径完备（fps<=0 三处防御）；source_in/out 区间交叉验证缺失已列 AUD-5501
- 类型注解（部分通过）: 除 AUD-5503 所列三处外，公共方法均有参数与返回注解，dataclass 字段类型完整
