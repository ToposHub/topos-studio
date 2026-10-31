# F4.5 · 二级调色参数与遮罩动画覆盖 — 验证台账

- 日期：2026-10-24；计划条目：§F4.5（计划选定参数：**Qualifier 亮度区间**）
- 测试：tests/color/test_qualifier_keyframes.py（11）＋回归全绿

## 1. 现状盘点

- `grade_keyframes.py`：GRADE_KEYFRAME_PARAMS（15 基础参数）+
  MASK_KEYFRAME_FIELDS（9 窗口数值字段，P1-COLOR-09 已有=Power Window
  非零覆盖）→ **Qualifier 域此前零动画能力**（qualifier_panel 的
  lum/sat/hue 区间全静态）。
- 求值单点：`evaluate_grade_keyframes`（grade）/`evaluate_mask_keyframes`
  （mask 就地写回）；引擎逐帧枢纽 `image_engine.apply_grade_keyframes`。

## 2. 交付（Qualifier 亮度区间五环贯通）

| 环 | 实现 |
|---|---|
| 记录 | `record_qualifier_keyframe(layer, frame, fields)`——当前值即关键帧，白名单 lum_low/lum_high |
| 求值 | `evaluate_qualifier_keyframes(layer, frame)`——就地写回 qualifier_params，幂等，限定器禁用跳过；接入 `apply_grade_keyframes` 逐帧枢纽（预览/成片同函数） |
| 曲线 | 复用 GradeKeyframeTrack 通用轨道设施（ruler 标记 `all_keyframe_frames` 含新轨道、序列化 `to_dict` 往返一致） |
| 逐参数复制 | `copy_named_tracks(source, names)` 按名深拷贝（只带走点名参数动画，目标层编辑隔离） |
| 静态切换 | `clear_qualifier_animation(tracks)` 清全部 qualifier.* 轨道 → 参数回静态值（求值对无轨道参数恒等） |

## 3. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 人物从阴影走向高光限定器随帧变化 | `test_qualifier_follows_subject_shadow_to_highlight`（0/48/96 三关键帧，中点插值 0.15/0.70 精确） |
| 随机 seek/反放/非零入点/变速一致值 | `test_random_seek_reverse_nonzero_inset_consistent`（乱序帧幂等快照一致）+ `test_speed_change_maps_to_source_frames`（2x → 源帧求值） |
| Undo/重开/成片一致 | `test_undo_friendly_evaluation_does_not_touch_tracks`（求值不写轨道）+ `test_tracks_serialize_through_layer_payload`（序列化往返后同帧同值）；预览/导出共用 `apply_grade_keyframes` 枢纽 |
| 白名单外字段不得由 ◆ 暗示支持 | `is_qualifier_animatable` 唯一判据 + `test_diamond_marker_gate`（lum_soft/sat/invert/blur/points 全部 False；qualifier.invert / mask.* 轨道名解析拒绝） |
| 点集动画另定义拓扑 | 未实现（登记后续片；points 不在白名单不暗示） |

## 4. 登记的后续片（不阻塞验收）

1. qualifier_panel UI：lum_low/lum_high ◆ 标记（用 is_qualifier_animatable
   判据）与记录/清除入口（核心五环已就绪）。
2. sat/hue 区间按证据扩展（白名单登记制，默认静态）。
3. 跟踪与用户动画组合顺序（F7.1 跟踪联验后声明）。
4. 点集（points/grad_points）拓扑与插值兼容定义。

## 5. 诚实边界

- 本期只开亮度区间两参数（计划原文选定的真实必要参数）；其余 qualifier
  字段显式静态（不暗示可动画）。
- 求值写回 layer.qualifier_params 经既有 qualifier 渲染阶段消费——
  GPU shader 侧逐帧重建属渲染管线既有行为，未改。
