# F7.1 · 跟踪、Roto、抠像与修补质量 — 验证台账

- 日期：2026-10-24；计划条目：§F7.1
- 测试：新增 test_f71_scene_cut_and_blur.py（5）＋既有跟踪链 190 全绿
  （tests/shared/tracking 190 含新增；keyer/paint/ml_roto/face 套件见调研清单）

## 1. 现状核验（Explore 全量调研结论）

跟踪链已有**异常扎实**的真实覆盖（超出计划文档假设——文档要求"增加"
的手动锚点修正/分段重跟实际 API 与测试均已存在）：
- 点/平面跟踪：金字塔 LK + FB 检查 + RANSAC（translation/trs/affine/
  homography）+ NCC 外观门控 + 五因子置信度；
- 失败检测：TrackStatus×FailureReason 枚举齐全（遮挡/出画/模糊/退化/
  特征不足），失败帧不产假 VALID；
- 手动修正：set/clear_manual_correction、transform_at 手动优先、
  apply_result 不覆盖手动帧（test_tracking_manual_correction）；
- 分段重跟：retrack_failed()（失败帧±3 帧上下文合并，AUD-24905）；
  双向合并（分段/冲突检测/失败分段）；
- 缓存失效：键含素材指纹+帧范围+参考帧+方向+模式+质量+版本+几何
  （AUD-37410）+ 服务层代次防陈旧；
- 镜头集：translation/trs/perspective/occlusion 四镜头 + 逐帧 GT
  （遮挡段附带 9×3 模糊）。

## 2. 本批交付（镜头集缺口补齐）

| 组件 | 内容 |
|---|---|
| scene_cut 镜头 | `make_tracking_fixtures.py` 新增：f=70 画面跳变为完全不同第二场景（卡片消失），GT 带 scene_cut 标记 |
| motion_blur 镜头 | f∈[40,85] 高速平移（34px/帧）+ 方向性水平模糊（核随速度至 45px），GT 带 blur_amount |
| 验收测试 | 5 项：切换后 FAILED+必带原因；**失败不污染后续帧**（切换后无假高置信 VALID）；切换前前向+后向合覆盖正常跟踪（<4px）；模糊段清晰区（后向）VALID；**模糊段 VALID 必须准确**（置信不冒称——实测 LK 金字塔 45px 模糊核下仍 0.12px 精确锁定，断言精度一致而非压制置信，四角误差 <3px 冻结容差） |

## 3. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 遮挡/运动模糊/透视镜头集 | 既有四镜头（含遮挡段模糊）+ 新增独立运动模糊镜头；透视 homography 稳定性既有测试 |
| 失败不污染后续帧 | `test_no_fake_valid_after_cut`（切换后零假高置信 VALID）+ 既有遮挡失败不产假 VALID |
| 失败检测/置信度可解释 | 新增切换镜头 FAILED+reason 断言 + 既有五因子置信度/阈值一致/权重可配测试 |
| 手动锚点修正/分段重跟/失效重算 | 既有（test_tracking_manual_correction / retrack_failed / invalidate）——核验登记，不重复建设 |
| UI 可修正且重开/Undo 生效 | 既有（tracker_panel 状态/重跟接线 + serialization/schema_migration） |
| Composite→Edit→导出一致 | 既有（tracker_node 烘焙关键帧 + tracking_render_apply） |
| 有标注特征/遮罩参考+冻结容差 | 逐帧 GT（H 矩阵+四角+状态标记）+ 本批冻结容差断言（<3px/<4px） |
| 发丝/半透明镜头 | **登记后续片**（keyer 现有断言为硬边半屏；发丝边缘误差带指标待镜头需求） |
| 模型不可用可解释 | 调研结论：FaceTrackNode 生产无真实检测器接线（注入式）、ai_inpaint 为占位、ai_remove（LaMa）为真实修补——**不按节点数宣布画质等价**，声明见 §4 |

## 4. 诚实边界声明（不冒称）

- **真实修补 = ai_remove（LaMa ONNX）唯一**；ai_inpaint_node 为
  PlaceholderNode；FaceTrackNode 无生产检测器（set_face_detector 仅
  测试注入）；COLOR 云跟踪 cloud_available=False。
- 发丝/半透明 keyer 边缘质量：现有测试覆盖 chroma/luma/difference/
  despill 参数与 GPU 合成向量（atol=1e-4），无发丝边缘误差带指标——
  按需求登记后续片。
- COMP 页 TrackerNode 无 retrack_failed/手动修正 UI 入口（analyze 全量
  重跑）——复用 COLOR 页 TrackingService 语义的增量重算登记后续片。

## 5. 登记的后续片（不阻塞验收）

1. 发丝/半透明合成镜头 + keyer 边缘 1px 带内 alpha 过渡指标。
2. COMP 页 TrackerNode 增量 retrack/手动修正入口。
3. 人脸检测器生产接线（或 UI 明示不可用）。
