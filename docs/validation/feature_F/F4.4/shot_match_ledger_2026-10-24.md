# F4.4 · 批量镜头匹配与人工修正 — 验证台账

- 日期：2026-10-24；计划条目：§F4.4
- 测试：tests/color/test_shot_match.py（15）＋回归（color_context 契约/F4.1 组/F4.2 版本 49）全绿

## 1. 盘点结论（计划"盘点已有 ShotMatchWorkflow 真实消费者"）

`color/ai/color_context.py` 只有类型契约（ShotMatchSuggestion/Report/
ColorMeasurement/GradeOperation），**零算法、零生产消费者**（仅 tests/ai
引用）。本批补齐可解释算法 + 批量提交，复用契约类型不另起 schema。

## 2. 交付

| 组件 | 内容 |
|---|---|
| 测量 | `shot_match.py::measure_shot`：中性簇（**迭代灰世界 2 轮**——全帧均值被彩色内容带偏会把灰墙整体染色，实测墙 chroma 恒 0.095 全军覆没；5×5 去噪后判色度——18% 灰 +2% 独立噪声逐像素色度 ~0.2）+ 肤色簇（校正域 R>G>B 顺序 + 色度界 0.75，偏色下不变）+ 分布/p50/p95/峰值/裁剪比 |
| 匹配 | `match_shot`：曝光 stops=log2(参考/镜头 中性亮度)（校正域锚，色偏不污染）夹 ±2；WB=中性簇通道比增益夹 [1/2.5, 2.5]；饱和比夹 [0.5,1.5]（校正域色度锚）；置信度=中性占比+肤色存在+分布重叠−裁剪罚 |
| 排除守卫 | 剪影（中性亮度<0.01/簇<1%）、极端光色（增益比>2.5）、**HDR 动态范围差异（peak>4.0 且比>4 倍不强拉同分布）**、锁定镜头整体排除（置信压 ≤0.5 必走人工）、品牌色跳过 WB 保色相（曝光仍可对） |
| 参数映射 | `deltas_to_grading_params`：stops↔GradingMath exposure 参数互逆（数值锚定测试）、WB 增益↔temp/tint、饱和比↔saturation 参数——单一映射点可解释 |
| 规划 | `plan_shot_match` → 复用 `ShotMatchReport`（auto ≥0.75 / 需确认 / 硬排除三分区） |
| 批量提交 | `shot_match_apply.py::ApplyShotMatchCommand`：适用建议→**F4.1 微调栈**（scope=clip_trim，设置语义重跑收敛，主栈不动）+ 绑定 clip；每受影响主栈存 **F4.2 `pre-shot-match` 版本锚**（一键激活即回匹配前）；**过时 revision 整体拒绝零半写**；undo 对称（栈表+时间线+版本表）；锁定/排除建议零进入 |

## 3. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 多机位同场景匹配与肤色/中性色基准 | `test_multicam_same_scene_recovers_known_offsets`（已知 +0.5 stop/暖偏 → 恢复 ±0.08）、`test_skin_and_neutral_bases_measured`（中性簇/肤色簇 R>G>B 基准） |
| 剪影/极端光色/HDR 不强拉同分布 | `test_silhouette_...`、`test_extreme_cast_guard`、`test_hdr_dynamic_range_not_flattened`（硬排除 + 置信 ≤0.5） |
| 锁定/品牌色保护 | `test_locked_shot_excluded_and_brand_skips_wb`、`test_locked_clips_never_enter_command`（零进入） |
| 过时 revision 拒绝、取消零写入、单次 Undo | `test_stale_revision_refused_zero_write`、`test_undo_restores_everything` |
| 批量提交/导出一致（复用 F4.1/F4.2） | `test_batch_apply_creates_trims_and_version_anchor`（trim 建/绑 + 主栈版本锚 + 主栈内容不动）、`test_trim_feeds_f41_chain_export_resolution`（匹配 trim 经 F4.1 链被导出解析） |
| 算法适用边界声明 | 模块 docstring：灰世界+肤色锚 CFR 同场景近似，非光谱级；跨场景/极端光比走人工确认 |

## 4. 登记的后续片（不阻塞验收）

1. UI：批量预览面板（建议列表+逐镜头勾选+确认提交）、参考版本/源解释锁定控件。
2. 建议批量预览的字节级"预览渲染"（当前报告可解释文本+参数；帧级预览待 UI）。
3. 分布匹配升级（直方图规格化）与模型路径（ShotMatchWorkflow 契约已留位）。
4. `GradeDiffReport`（CG-09）联动：提交后自动 before/after 度量。

## 5. 诚实边界

- 匹配为统计近似：合成向量恢复 ±0.08 stop；真实多机位素材的恢复精度
  依赖中性/肤色锚的存在（无锚 → 低置信走人工，不硬建议）。
- 批量提交经 F4.1 微调栈——组链上 pre/post 组栈的镜头同样以 trim 叠加
  （顺序语义由 F4.1 锚定）。
