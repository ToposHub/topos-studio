# Batch-010 静态审查报告：color/core 时域降噪 + 工作区预设

- 日期：2026-09-28
- 审查文件：
  - `src/features/color/core/video_temporal_denoise.py`（520 行）
  - `src/features/color/core/workspace_presets.py`（297 行）
- 辅助核读（仅为验证结论，不属于本批审查范围）：`video_denoise_gl.py`、`video_denoise_shaders.py`、`src/shared/video/denoise/params.py`、`video_denoise_pipeline.py`、`async_renderer.py`（调用点/线程上下文）、`tests/video_denoise/test_temporal_denoise_state.py`
- 问题编号区间：[AUD-1000–AUD-1099]

---

## src/features/color/core/video_temporal_denoise.py (行数: 520)

### 发现

- [AUD-1000] [Major] `force_reset()` 后同一 clip/generation/params 下状态机永久卡死在 delta==0 分支，时域降噪静默失效
  - 位置: src/features/color/core/video_temporal_denoise.py:82-90（配合 :52-57、:150-154、:187-191）
  - 证据:
    ```python
    if reset_reason is None:
        delta = 0
        if self.last_frame_index >= 0:
            delta = frame_index - self.last_frame_index
        if delta == 0:
            # 同帧重绘（调色参数变化/viewport 刷新）：画面内容没变，
            # 历史依然有效——复用而不重置，也不推进累积计数。
            self.last_delta = self.last_delta  # 保持
            return self.accumulated_frames > 1
    ```
    而 `reset()`（:52-57）只清 `last_frame_index=-1 / accumulated_frames=0`，不清 `clip_key/generation_id/parameters_key/size`；`force_reset()`（:150-154）直接复用它。`process()` 的异常回退路径（:187-191）每次 GL 异常都会调用 `force_reset()`。
  - 影响: `reset()` 之后 `last_frame_index == -1`，若下一次 `prepare()` 的 clip/generation/params/size 与重置前完全相同（连续播放中一次瞬态 GL 异常即触发；`RESET_VIDEO_DENOISE_HISTORY` 命令在 identity 不变时同样触发），`delta` 保持 0，走进"同帧重绘"分支提前 return，`last_frame_index`/`accumulated_frames` 永远不推进——`history_usable` 恒为 False，`_process_causal` 恒走无历史直通分支（:213-225），本片段余下播放期间时域降噪静默失效，直到换素材/seek/改参数。`tests/video_denoise/test_temporal_denoise_state.py` 未覆盖 reset-后-同-identity 场景。另：`self.last_delta = self.last_delta` 为无效自赋值。
  - 建议: "同帧重绘"判定应显式要求已有历史，例如 `if self.last_frame_index >= 0 and frame_index == self.last_frame_index:`；或让 `reset()` 同时清空 identity 字段（clip_key/generation_id/parameters_key/size），使下次 prepare 走完整重置+推进路径。补一条 reset-后恢复的单元测试。
  - 置信度: 高

- [AUD-1001] [Major] `_replay_window` 对邻帧降采样使用当前帧尺寸作为 `u_texel`，混合分辨率时运动估计标定错误
  - 位置: src/features/color/core/video_temporal_denoise.py:405、:455、:460（对照契约 :301-311、自认 :469-471）
  - 证据:
    ```python
    width, height = current_tex.size          # :405 只取当前帧尺寸
    ...
    self._downsample_luma(frames[0], luma_prev, width, height)   # :455
    self._downsample_luma(frame_tex, luma_cur, width, height)    # :460
    ```
    而 `_downsample_luma` 内 `prog["u_texel"].value = (1.0 / width, 1.0 / height)`（:310）以传入尺寸作为被采样纹理的 texel；:469-471 注释自认 "邻帧可能是降采样上传（细节先天偏软）"，`async_renderer._get_video_denoise_window_textures` 也按每个邻帧自身尺寸上传（"原始数据尺寸"），并无尺寸一致性保证。
  - 影响: 当邻帧分辨率 ≠ 当前帧（proxy/降采样窗口）时，邻帧 16-tap box 的 UV 足迹只有当前帧 luma 的一半，cur/prev luma 的等效低通滤波不一致 → 零运动下 SAD 存在系统性残差，块匹配置信度下降/矢量估计偏差，Paused/Export 窗口模式（本应为高画质档）出现降噪强度衰减或轻微错位涂抹。
  - 建议: `_downsample_luma` 的 `u_texel` 改用被采样纹理自身的 `tex.size`（如 `prog["u_texel"].value = (1.0 / source_tex.size[0], 1.0 / source_tex.size[1])`），与因果路径（:201 传入的是 source_tex 尺寸）保持契约一致。
  - 置信度: 中（混合尺寸场景由代码注释与上传逻辑证实存在，实际触发频率取决于 proxy 管线配置）

- [AUD-1002] [Suggestion] 运动估计失败路径不推进历史纹理，历史与 luma_prev/注释语义失配（单帧拖影）
  - 位置: src/features/color/core/video_temporal_denoise.py:231-235
  - 证据:
    ```python
    motion = self._estimate_motion(luma_cur.texture, luma_prev.texture, p, qw, qh)
    if motion is None:
        # 运动估计失败：仅做零运动累积（warp 退化为直采）
        self._store_prev_luma(luma_cur, qw, qh)
        return source_tex
    ```
  - 影响: 注释声称"仅做零运动累积"，实现却是完全直通且不更新 hist 表面：luma_prev 推进到当前帧、CPU 状态照常 `accumulated_frames += 1`，但 GPU 历史纹理停留在更早帧的输出。下一帧恢复正常时，会以"当前帧 + 1 帧运动量 warp 的过龄历史"做累积（历史滞后 2 帧、运动只补偿 1 帧），运动内容上产生一帧错位拖影；之后自愈。触发条件为 motion shader 编译失败或运动表面分配失败（低频）。
  - 建议: 失败分支将当前帧 copy 进 next_hist 并 `_swap_history()`（与 :213-225 无历史分支一致），使历史与 luma_prev 同步推进；或至少把注释改为与实现一致。
  - 置信度: 高（代码行为确定，影响范围小）

- [AUD-1003] [Suggestion] 因果模式步骤 5 的历史推进是一次额外的全分辨率 f16 拷贝（4K 热路径每帧成本）
  - 位置: src/features/color/core/video_temporal_denoise.py:268-277
  - 证据:
    ```python
    # 5. 历史推进：next_hist = 输出
    next_hist.use()
    self._ctx.viewport = (0, 0, width, height)
    copy_prog = self._prog("copy", vs.COPY_FRAGMENT)
    out_tex = out.texture
    if copy_prog is not None:
        out_tex.use(0)
        copy_prog["u_texture"].value = 0
        self._quad.draw(copy_prog)
    ```
  - 影响: 4K 下 accum 先渲到 `out` 再整帧拷贝到 `next_hist`，每帧多一次 ~66MB（RGBA16F）读+写的全屏 pass；且返回的 `out_tex` 下帧仍会被重写，该拷贝并不能延长返回纹理的寿命。对 4K/60FPS 实时预算是可避免的 ~0.5-1ms 级开销。
  - 建议: 评估让 accum 直接渲染进 `next_hist` 并返回 `next_hist.texture`（下游仅采样不写入该 key），删除 copy pass；若下游确有写入冲突风险，则保留但记录该决策。
  - 置信度: 中（开销确定存在；可去除性需下游消费方确认）

- [AUD-1004] [Suggestion] 多个核心方法参数/返回值缺类型注解
  - 位置: src/features/color/core/video_temporal_denoise.py:193、:285、:313、:390-395、:415
  - 证据:
    ```python
    def _process_causal(self, source_tex, p, width, height, history_usable):
    def _store_prev_luma(self, luma_surface, qw: int, qh: int) -> None:
    def _estimate_motion(self, luma_cur, luma_prev, p, qw, qh, key_prefix: str = ""):
    def _replay_window(self, frames, p, width, height):
    ```
    （`process_window` 的 `neighbor_textures`/`current_tex` 亦无注解，:392-393）
  - 影响: 与项目强类型约束不符；`_replay_window(frames, p, ...)` 的 frames 元素类型、`_estimate_motion` 返回 `Optional[RenderSurface]` 等关键契约只能靠阅读实现确认。
  - 建议: 补齐 `moderngl.Texture`/`TemporalDenoiseParams`/`Optional[RenderSurface]`/`Sequence[Optional[moderngl.Texture]]` 等注解。
  - 置信度: 高

### 通过项
- 线程安全：本模块所有 GL 访问限定渲染线程；UI 侧改参数/重置历史经渲染命令队列在 GL mutex 下执行（`async_renderer.py:977、:990` 验证），无 UI 线程直调 ModernGL、无可变对象跨线程信号。
- GPU 资源生命周期：表面统一由共享 `RenderSurfaceCache` 按 (key,size) 复用，重建时旧表面释放；关闭路径 `pipeline.release()` 依序释放 programs/surfaces/quad（`video_denoise_pipeline.py:215-218`），经渲染线程 registry 调用；失败路径均回退返回源纹理，无泄漏、无重复创建不释放。
- 窗口重放使用独立 `"window_"` 前缀表面，不污染因果历史状态（且有 `tests/video_denoise_gpu.py:259` 回归测试覆盖）。
- GLSL/HDR：所用 shader 常量循环边界均为编译期常量（MAX_SEARCH=5、固定 ±2/±1 区间，运行期半径用 continue 裁剪）；浮点字面量均带 `.0`；`TEMPORAL_ACCUM_FRAGMENT` 全程无 `clamp(0.0,1.0)` 于 RGB（HDR 安全，仅对权重/UV clamp）；`hist_uv` 有 `clamp(vec2(0.0), vec2(1.0))` 边界保护，luma/motion 表面 `repeat_x/y=False` 边缘钳制。
- seek/flush/取消状态重置条件齐全（换素材/世代/尺寸/参数/帧号跳变/方向反转，:73-95）且有对应单元测试；`process_window` 完全不触碰因果状态。
- 异常路径不阻塞播放：`process`/`process_window`/`debug_overlay` 均捕获并回退源纹理（`_downsample_luma` 的 RuntimeError 会被 `process` 的 except 接住）。
- 性能设计：热路径零表面分配（缓存复用）、运动估计在 1/4 分辨率进行、权重用有理近似避免 exp()。

---

## src/features/color/core/workspace_presets.py (行数: 297)

### 发现

- [AUD-1005] [Major] 配置读取异常被吞为空列表，随后任何保存都会整体覆盖写回，永久丢失全部用户预设
  - 位置: src/features/color/core/workspace_presets.py:187-191（配合 :231、:213）
  - 证据:
    ```python
    raw = None
    if config is not None:
        try:
            raw = config.get(CONFIG_KEY)
        except Exception:
            raw = None
    if isinstance(raw, dict):
        raw = raw.get("items")
    if not isinstance(raw, (list, tuple)):
        return []
    ```
    （:184-195，`load_user_workspaces` 开头）
    随后读-改-写链：`:231 presets = [p for p in load_user_workspaces(config) if p.name != clean_name]` → `:213 config.set(CONFIG_KEY, {"items": [_preset_to_dict(p) for p in presets]})` 整体覆盖。
  - 影响: ConfigManager.get 一次瞬态失败（IO/锁/损坏）会使 `load_user_workspaces` 返回 [] 且无任何日志；此时用户再保存任意一个工作区，`save_user_workspace` 基于空列表重建并整体覆盖 `color_workspaces`——原有全部用户预设被静默永久删除。删除路径（:247-251）同理基于可能为空的加载结果，但只影响返回值不造成破坏。
  - 建议: 读失败与"确实没有数据"必须区分：至少在 except 分支记录日志并向上传播失败（返回 None 或抛出），`save_user_workspace`/`delete_user_workspace` 在加载失败时拒绝写回（fail-safe，不做覆盖）。
  - 置信度: 高（代码路径确定；触发依赖 ConfigManager 一次读取失败）

- [AUD-1006] [Major] `_write_user_workspaces` 静默吞掉写入异常且无日志，`save_user_workspace` 仍返回新 preset 造成保存成功假象
  - 位置: src/features/color/core/workspace_presets.py:212-215、:238-240
  - 证据:
    ```python
    try:
        config.set(CONFIG_KEY, {"items": [_preset_to_dict(p) for p in presets]})
    except Exception:
        pass
    ```
    而 `save_user_workspace` 结尾 `_write_user_workspaces(config, presets); return preset`，调用方（`src/app/controllers/color_workspace_controller.py:136`）以非 None 返回值当作保存成功。
  - 影响: 磁盘写入失败时用户看到"已保存"，preset 对象照常返回并在当前会话生效，重启后预设消失；且整个文件无 logger，失败无任何痕迹可查。
  - 建议: 写失败至少 `logger.exception`；让 `_write_user_workspaces` 返回 bool，`save_user_workspace` 失败时返回 None（或在 controller 层提示保存失败）。
  - 置信度: 高

### 通过项
- 输入验证（维度 7 重点）：`_preset_from_dict` 对工程/config 数据逐字段类型校验（dict/str/list/元组），preset_id 强制 `user.` 前缀、name 非空、scope_source 白名单（graded/source/reference），畸形条目返回 None 丢弃，重复 id 去重（:196-203）——不会因手写/迁移数据崩溃。
- 内置预设不可伪造/不可删除：`delete_user_workspace` 校验 `user.` 前缀，内置 `builtin.*` 永不可删；加载路径也拒绝非 `user.` 条目冒充。
- `_ALWAYS_VISIBLE` 在保存与加载两条路径都强制并入（:170-171、:230），viewer 常驻不变量一致。
- `sanitize_preset` 按 PanelRegistry 现状过滤 + 遗留 HSL Curves/Mixer/Warper → color.mapping 迁移 + work_mode 裁剪（未知 work_mode 值默认不过滤，偏安全开放）+ viewport 兜底，无越界引用。
- 纯数据/纯函数模块：无 GL、无 Qt 信号、无共享可变状态（frozen dataclass + frozenset 默认工厂正确），无线程与信号递归问题。
- 序列化不绕过 undo/revision 体系：工作区属 UI 偏好持久化（ConfigManager），非文档模型状态，不涉及 command/service 提交一致性。
- 类型注解完整（公共函数与 dataclass 字段均有注解）。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-1000, AUD-1001, AUD-1005, AUD-1006 |
| Suggestion | 3 | AUD-1002, AUD-1003, AUD-1004 |
