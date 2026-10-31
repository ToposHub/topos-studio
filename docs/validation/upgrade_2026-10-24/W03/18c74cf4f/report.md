# W03 · 运行中驻留源失败后的真实恢复 —— 验证报告

- 基线提交：18c74cf4f（W02 收口）
- 机器：B（Windows 11 Pro 26200 / i9-13900K / RTX 4090 621.01 / av 18.1.0 /
  cuda-bindings 13.4.2 / PyQt6 6.11.0）
- 对应计划：W03（W-I02），前置 W02 ✅

## 交付内容

### 1. 导入失败关联 asset/source/generation（不再只记丢帧）

- `EditGpuPreviewRenderer` 新增信号
  `cuda_resident_import_failed(asset_path, reason, generation_id)`；
  `_import_cuda_resident_frame` 三处失败路径（会话不可用 / bind 失败）均按
  素材上报，帧号/任务代随层传递（`layer_data['asset_path']` 由 TPR
  worker 装载，`task.generation_id` 随调用传入）。
- 会话失效恢复：bind 失败且会话已 destroyed 时丢弃缓存的死会话引用
  （`_cuda_interop_session = None`），下一次导入自动重建新会话——不永久
  卡死在死句柄。
- `timeline_preview_renderer`：cuda_resident 层携带 asset_path；
  `set_gpu_renderer` 连接新信号到 `_on_cuda_resident_import_failed` 槽
  （方法借用方/shell 无此槽时跳过连接，行为同旧版）；槽转交管理器，
  管理器异常/缺位只留痕不外抛（GL 信号链不崩 UI）。

### 2. 按素材降级：停用新驻留请求 + 租约归零后重建下载/CPU 源 + 限次重试

`EditGpuDecodeManager`：

- `note_resident_import_failure(asset_path, reason)`（任意线程可调）：
  同步记账并立即拉起降级门——`_create_source` 3.4b 分支此后不再为该素材
  创建 `win_cuda_resident` 源（`_resident_route_allowed_for` 门 + 
  `decode_manager.resident_route_demoted` trace）。
- 后台降级关闭（`_apply_resident_demotion` 线程，dedup 防重入）：**只关**
  该素材 source_type==win_cuda_resident 的源——复用 P2-09 租约契约
  （`_wait_for_source_leases` 排水 → 超时登记 `_deferred_close_sources`
  → 最后一个租约归零补关闭），绝不在解码中途关闭；同素材其它类型源、
  其它素材的驻留源一律不动（故障隔离）。warmup generation bump 防止
  在飞 warmup 把驻留源重新插回（降级标志保证后续 `_create_source` 只会
  返回回退源）。
- 重建：关闭后下一次 `get_frame` 走 3.5 下载 planar / CPU 分支自然重建
  （当前片段下一帧自动恢复，无迟到提交——换工程后无该素材请求即无动作）。
- 限次重试：每次失败记 cycle；冷却窗口（默认 300s）内不重试驻留，窗口
  过期允许重新探测；cycle > 2 后本会话不再自动重试（防单坏源反复抖动），
  只能 `reset_resident_demotions()`（诊断/显式恢复）或 `close_all()`
  （新会话，`invalidate_all_sources` 画质切换保留降级）清除。
- 与 B2.1 全局门闩互补：全局门闩管"连续 3 次失败停**所有**新源路由"，
  本机制管"单素材停用+重建"——一个坏源不拖垮其它正常源。

### 3. 恰一次释放

旧驻留源关闭恰一次：降级关闭在锁内 pop、锁外按 id 去重 close（与
close_source/逐出同构）；租约未归零时推迟关闭由 `_release_source` 归零
补执行（恰一次）。失败帧 cu_frame.release() 保持幂等契约。

## 测试证据

### L1 逻辑（全平台，tests/edit/test_resident_failure_recovery_w03.py，17 绿）

- 记账：冷却窗口内禁用/窗口过期允许重探/cycle 超限永久禁用（时间前进
  不可恢复）→ 显式 reset 恢复；close_all 后拒绝记账。
- 关闭：只关该素材驻留源（同素材 planar 源、他素材驻留源不动）；租约
  持有中推迟关闭 → 归零后恰一次补关闭；无驻留源 no-op；重复报告在
  作业在飞时只记账不排第二个关闭作业；异步线程真实关闭源。
- `_create_source` 门（monkeypatch sys.platform=win32 越过平台门，复用
  d2ce07f2e 手法）：降级素材不再创建驻留源（fail-closed None），
  同门下其它素材仍获驻留源。
- 渲染器上报：会话不可用/bind 失败均发 `(asset, reason, generation)`，
  源帧恰一次释放；destroyed 会话引用丢弃 / 活会话保留。
- TPR 槽：转发到管理器；管理器抛异常/缺位不外抛。

### L3 真机端到端（tests/edit/test_cuda_resident_failure_recovery_l3.py，1 绿 ×2 轮）

真实生产链（真管理器路由 + 真 QThread 渲染线程 + 共享 GL 上下文）：

1. 基线：4K H264 驻留路由渲染非黑；
2. **注入 UV 拷贝失败**（interop bind 首次调用失败一次，reason=
   `copy_failed:uv_injected`）：该帧 render_error，信号按素材+代上报；
3. 降级：旧驻留源 close 恰一次（实例计数验证）；`resident_demotion_stats`
   透出原因/次数/冷却；
4. **自动恢复**：下一帧 get_frame 重建为 `vt_planar`（下载 planar，非
   驻留非 CPU），同一渲染线程重新呈现非黑画面；
5. **后续 seek 正确**：帧 100 经重建源真实 GOP seek 路径渲染非黑、内容
   随帧更新；
6. **故障隔离**：2K H265 他轨仍 `cuda_resident` 路由且渲染非黑；全局
   门闩未触发（`import_failure_latch_tripped() is False`）。

### 回归

- tests/media/test_windows_cuda_resident.py 37 绿；test_cuda_resident_
  lifecycle_w02.py 20 绿；test_cuda_resident_capability_w04.py（W04）17 绿；
- tests/edit/test_cuda_resident_playback_l3.py 3 绿 ×2 轮（含 W04 改动后）；
- tests/edit/test_timeline_preview_renderer.py + test_idle_prebuffer.py
  168 绿；test_edit_gpu_decode_manager.py 49 绿；
- tests/edit 全组：与基线逐文件对拍（stash 前后），**新增失败 0**
  （中途发现 1 个 shell 借用方缺新槽的真实回归——`set_gpu_renderer`
  连接改为槽可选后归零；另全组单进程长跑存在顺序污染，逐文件隔离后
  与基线完全一致）。

### 顺带修复（W03 回归扫描发现的真实 Windows 缺陷，非驻留链）

- `generated_media_materializer`：Windows 盘符路径（`C:\…`）被
  urlparse 误判为单字母 scheme → "不支持的媒体句柄协议：c"；按
  盘符+绝对路径还原为本地文件分支（`test_local_file_is_reused` 复绿）。
- `tests/edit/test_edit_gpu_decode_manager.py` 4 个 macOS VT 策略用例
  补 darwin 平台门（monkeypatch sys.platform，全平台可跑；策略门本身
  不变）。

## 已知限制与如实登记

- 降级重建为"下一帧恢复"：失败帧本身被跳过（render_error → 无迟到
  半帧提交），不做失败帧内重试（重试会拖住当前帧呈现节奏）。
- 降级关闭的 warmup generation bump 会使**在飞**的其它素材 warmup 丢弃
  一次结果（best-effort 后台预热，下一次 get_frame 重建；不影响正确性）。
- 本机为单 4090 CUDA 设备：跨设备指针不匹配路径由 L1 替身覆盖（真机
  无第二台 CUDA 设备可注入）。
