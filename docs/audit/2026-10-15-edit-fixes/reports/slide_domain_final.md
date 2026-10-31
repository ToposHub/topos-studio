# 第四轮 Slide 领域修复收口

## 范围与结果

本轮保留工作区已有修复，仅处理 Slide 领域语义、对应 focused tests，以及主代理追加授权的 `clip_params.py` 曲线扩展缺陷。未提交 Git commit。

- `timeline_ops.slide_clip` 保持两侧原有 gap：前邻居右缘与后邻居左缘分别增加同一 delta；选中片段只平移，邻居外侧边缘固定。零偏移返回原 sequence；锁轨拒绝，包括零偏移。
- 邻居源窗使用模型统一的 `with_right_edge_source_values` / `with_left_edge_source_values`；`copy_clip_with` 保持 FPS、reverse、采样 phase 与 speed curve clock。
- `get_slide_bounds` 使用同一源窗映射及前/后素材总帧数限制；未知素材尾端不额外假造上限，仍检查源起点、有效时长及时间线边界。单侧邻居与 isolated 情况已覆盖。
- 修正 Slide 选中片段平移动画后的动态扩展属性丢失：从 temporal shift 结果提取动画/effects，通过原 clip 的 `copy_clip_with` 原子合并。
- 旧 `test_slide_gapped_neighbors_both_directions` 曾固化吞 gap 的行为，已改为两个 50 帧 gap 各自守恒。

## 曲线扩展根因与修复

新增逐帧矩阵复现 smooth 曲线、30fps 源 / 24fps 时间线、左扩展 9 帧后，保留帧从源 125 漂移到 126（反放对称）。原 `sliced_by_window` 为扩展而平移控制点、改变曲线域总长，导致重新生成的积分 LUT 改变原坡与时钟。

现窗口扩展保持原 `speed_points` 与 `curve_length` 不变，允许负 `curve_offset`，显式保持 `curve_time_offset`。超出原曲线的部分按首速/尾速恒速延伸。源位置、反向时间映射、窗口时长与 frame blend 均使用这一不可变域。frame blend 权重先在原曲线域计算，再扣整数窗口偏移，避免整数平移造成浮点低位丢失。已有 v4 phase / curve time 字段与序列化格式保持兼容，无新增 schema 字段。

## 验证

新增 `tests/edit/test_round4_slide_domain.py` 共 53 项：

- 两种源 FPS、正放/反放、慢速/分数速度/step/smooth、正负 delta 的完整保留帧数组精确比较。
- 非零 CFR phase、两侧 gap、不可变输入、素材 bounds 穷举与首个越界 delta、锁轨、单侧/孤立片段。
- clip/ruler marker 作用域、邻居裁掉 marker、command JSON undo/redo、素材上限 JSON 传递。
- smooth 扩展的原域恒等、首尾速度 handles、JSON clock、精确 frame blend 源帧与权重，无放宽容差。

最终命令：

```sh
.venv/bin/python -m pytest -q \
  tests/edit/test_round4_slide_domain.py \
  tests/edit/test_round4_core_time_windows.py \
  tests/edit/test_smooth_speed_ramp.py \
  tests/edit/test_timeline_clip_speed.py \
  tests/edit/test_frame_blend.py \
  tests/edit/test_fix_renderers_aud3.py \
  tests/edit/test_transition_and_slide_transactions.py \
  tests/edit/test_fix_w6g8_aud6.py
```

结果：**209 passed**（18.61s）。`compileall` 与所涉文件 `git diff --check` 均通过。

与 `finish_slip_slide_ui` 已对齐 `prev_max_source_frames` / `next_max_source_frames` 参数名，其负责 commands/UI 的素材上限传递与 marker 快照；上述测试已覆盖集成结果。当前范围已收口，不继续扩展。
