# EF-01 part3 修复报告 — src/features/edit/core（19 条 Suggestion）

批次：EF-01 子运行 3/3（AUD-17813..19208）
分支：dev（未提交，与其他批次共存于工作树，由主会话统一提交）
白名单：仅 `src/features/edit/core/`
测试：`tests/edit/test_ef01_aud_part3.py`（67 条，全部通过）

## 逐条结论

| ID | 结论 | 位置（文件:行号，修复后） | 说明 |
|----|------|--------------------------|------|
| AUD-17813 | FIXED | scene_cut/cache.py:1131-1173 | `get_scene_cut_cache` 改为按 `os.path.abspath(project_cache_dir)` 键控的模块级惰性单例（双检锁），消除 UI 线程每次加载/保存重复构造 + 全量 stat 扫描 thumbs 目录的开销，多实例 LRU 记账分裂问题一并消除 |
| AUD-17911 | FIXED | scene_cut/filters.py:302-315, 348-350 | process() 开头重建 `_diagnostics`（`_reset_diagnostics()`，与 `__init__` 共用 `_make_diagnostics()`），同一 pipeline 二次 process 计数不再跨次累加 |
| AUD-17913 | FIXED | scene_cut/filters.py:196-248, 385-391, 495-500, 512-517 | motion/black 帧列表追加即按扫描窗（±5/±3）裁剪头部；新增 `motion_frames_seen`/`black_frames_seen` 累计计数器保持 guard 门控与旧"未裁剪全表 len"语义完全一致（含 suppress 分支行为）；长素材 O(N²) 消除 |
| AUD-18015 | FIXED | scene_cut/models.py:505-508 | docstring 公式改为与 metrics.py 实现一致的四项（0.35/0.30/0.20/0.10），注明 motion 不参与、pHash 权重并入 color |
| AUD-18210 | FIXED | serialization.py:563-599 | `_dict_to_asset` 对 keywords/metadata 做 isinstance 校验 + 默认值兜底，rating 用 try/except (TypeError, ValueError) 兜底；显式 JSON null 不再 tuple(None)/dict(None)/int(None) 崩溃 |
| AUD-18215 | FIXED | sidechain_ducker.py:192-252, 258-283 | process() 主循环与 `_smooth_release_to_zero` 改为按阈值穿越分段 + `np.multiply.accumulate` 向量化：release/无信号段与逐样本循环位级一致；attack 段因原循环"加 target 减 target"两次舍入无法闭式重放，差 ≤1 ulp（~1e-15 相对，远低于 int16 量化与仓库 parity 容差 1e-4）；测试含逐样本参考实现对拍 |
| AUD-18323 | FIXED | timeline_clip_frame_provider.py:673-694, 2235-2251 | GL 线程身份首次进入固化；不同线程进入时不再无条件覆写——记 warning、作废全部 GL 记账（`_invalidate_gl_thread_state`）后重绑，杜绝旧线程 clear 被停靠给新线程跨上下文释放 |
| AUD-18325 | FIXED | timeline_clip_frame_provider.py:2153-2189, 2224-2233 | `clear_debug_cache` 补齐与 `clear_source_cache` 相同的跨线程停靠契约（非 GL 线程 → `_debug_cache_clear_pending` 标志，渲染线程入口 drain）；保留方法名（既有测试依赖），行为与名称的落差已在 docstring 显式声明；owned 纹理释放失败从裸 `except: pass` 改为 logger.debug 留痕 |
| AUD-18326 | FIXED | timeline_clip_frame_provider.py:1364-1376 | info=None 早退的默认标签 ("bt709","bt709","bt709","limited") 同样写入 `tags_cache` 按 clip_id 记忆化，无 clip info 素材不再逐帧重探测；探测异常改为 logger.debug 留痕 |
| AUD-18328 | FIXED | timeline_clip_frame_provider.py:205-215, 1188-1258, 1297-1313 | 引入 `_async_decode_generation` 世代号：`_stop_async_decode_thread` 递增世代；worker 绑定创建时世代，循环/解码后/pending 回写前三重世代检查——stop 标志被 `_ensure_async_decode_thread` 复位也不能"复活"旧 worker，过期世代不回写已清空的 pending |
| AUD-18411 | FIXED | timeline_export.py:2862-2880 | 合并改为 OR（`_cancel_requested or _cancel_pending_before_start`），flip 前先清上一轮遗留（此时迟到 cancel 仍走 pending 通道）——竞态取消不再被吞，同时保留"上轮取消不泄漏进新一轮"的原语义 |
| AUD-18511 | FIXED | timeline_frame_mapper.py:104-112 | 按审计"二选一"取文档修正：明确 `source_time_seconds` 为 clip-local 播放时间（不含 source_start 偏移、reverse 不翻转），绝对定位应使用 source_frame/fractional_source_frame；行为不变（既有测试断言 source_time == timeline_time × speed 与此一致） |
| AUD-18605 | FIXED | timeline_metal_export_renderer.py:3495-3527, 4696-4700 | 物化判定抽为纯函数 `_should_materialize_effect_chain`，新增"单效果 + 非 Metal（planar/packed）源 + max(half_x,half_y)>1 → 物化"分支，消除 fused 路径逐 tap 重复 YUV 解码 + 一级调色的 4K 热点 |
| AUD-18809 | FIXED | timeline_ops.py:56-66, 350-365, 800-810, 1578-1591（insert 契约 AUD-18803 已有，未回退） | 新增 `_translate_model_error`；move_clip 跨轨/同轨、add_transition、overwrite 的模型层 ValueError 统一转译为 `TimelineOperationError`（保留原信息 + `from exc` 根因链），与 docstring 承诺一致 |
| AUD-18816 | FIXED | timeline_ops.py:1330-1352 | insert 波移改为单次 `_rebuild(clips=...)` 原子平移（与 ripple_trim 同模式），消除逐片段 `with_clip_updated` 的 O(n²) 全量重叠校验 + 重排序；最终一致性由 `with_clip_added` 统一校验保证 |
| AUD-18905 | FIXED | timeline_preview_renderer.py:2807-3103 | `_composite_layers` 主体（282 行）包入 try/finally，合成循环中途抛异常也恢复/删除 `_current_render_timeline_frame`，陈旧帧号不再泄漏到 MG/comp 缓存键 |
| AUD-18906 | FIXED | timeline_preview_renderer.py:4326-4339 | 淘汰逻辑 `next(iter(...))` 加 StopIteration 防御（空表跳过淘汰），UI 线程 clear() 竞态不再抛 StopIteration 丢帧 |
| AUD-18909 | FIXED | timeline_preview_renderer.py:1308-1345, 1356-1362, 7086-7093 | 新增 `_teardown_comp_gl_resources`（doneCurrent → surface.destroy → 置空 + 关旗，幂等），挂入 `_teardown_decode_resources`（stop() 两条路径共用的"线程退出后"钩子）；外层 shutdown() 在 worker 已退出时补一次兜底调用 |
| AUD-19208 | FIXED | transition_mask.py:41-118, 206-207 | 归一化坐标网格按 (w,h) LRU 缓存 + even/odd interior 判定按 (type,w,h,progress) 精确键 LRU 缓存（容量有界 16/64，锁保护）；返回 float32 每次 astype 新拷贝，调用方修改不污染缓存；重复求值同一帧完全绕开逐边 Python 循环 |

无 INVALID / DUPLICATE / BLOCKED 条目：19 条计划行描述与代码现状核对一致，根因全部落在白名单内。

## .clinerules 约束自查

- UI 线程零 GL 调用：AUD-18325/18323/18909 全部遵循"非 GL 线程停靠 / 线程退出后销毁"契约；无任何新增 UI 线程 OpenGL/ModernGL 调用
- 无裸 `except:`；AUD-18325/18326 把两处静默吞错改为 logger.debug 留痕，其余 except 均收窄类型
- 新增/修改函数均有类型注解（`_translate_model_error`、`_should_materialize_effect_chain`、`note_motion_frame`、`_teardown_comp_gl_resources` 等）
- 无 HDR 数值钳制、无 601/709 矩阵改动；AUD-18326 只记忆化既有默认值，不改变色彩判定结果

## 验证关卡输出摘要

1. **py_compile**：12 个改动源文件 + 测试文件全部编译通过（GATE1_PY_COMPILE_OK）
2. **新测试**：`pytest tests/edit/test_ef01_aud_part3.py -q -p no:cacheprovider` → **67 passed in 1.64s**（覆盖 19 条 ID 的正例 + 反例/回退敏感性）
3. **定向既有测试**（无新增失败）：
   - test_scene_cut_cache.py + test_sidechain_ducker.py + test_timeline_frame_mapper.py → 82 passed
   - test_timeline_clip_frame_provider.py → 60 passed
   - test_timeline_ops.py + test_native_transitions.py + test_custom_transitions.py → 98 passed
   - test_timeline_preview_renderer.py → 156 passed
   - test_transition_effects.py + test_scene_cut_models.py → 151 passed
   - test_timeline_export.py → 39 passed, 5 skipped（既有 skip，非本次引入）
4. **回退敏感性（抽 2 条）**：AUD-18906（淘汰防御回退 → `StopIteration` 复现，测试 FAILED）与 AUD-19208（缓存调用回退 → mask 缓存键缺失，测试 FAILED）；还原修复后对应测试恢复 PASSED。备份/还原通过文件副本完成，未使用任何 git 命令。

未触碰：REMEDIATION_PLAN.md、白名单外文件、其他批次代理正在修改的目录；未执行任何 git add/commit/branch/push。

## 改动文件清单

源文件（12）：
- src/features/edit/core/scene_cut/cache.py
- src/features/edit/core/scene_cut/filters.py
- src/features/edit/core/scene_cut/models.py
- src/features/edit/core/serialization.py
- src/features/edit/core/sidechain_ducker.py
- src/features/edit/core/timeline_clip_frame_provider.py
- src/features/edit/core/timeline_export.py
- src/features/edit/core/timeline_frame_mapper.py
- src/features/edit/core/timeline_metal_export_renderer.py
- src/features/edit/core/timeline_ops.py
- src/features/edit/core/timeline_preview_renderer.py
- src/features/edit/core/transition_mask.py

测试（1，新建）：
- tests/edit/test_ef01_aud_part3.py
