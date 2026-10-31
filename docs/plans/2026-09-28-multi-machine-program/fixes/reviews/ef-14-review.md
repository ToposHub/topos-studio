# EF-14 复查报告（GLM5.3）— src/shared/ui

- 复查日期：2026-10-01
- 批次：EF-14（e_flash_batches.json，13 条，白名单 `src/shared/ui`）
- 执行提交：`25695e98d`（25695e98d724237c7a98a3d43eb6993a70d7547d，`[fix-E ef-14] … (13)`）
- 执行报告：`fixes/reports/ef-14.md`
- 复查方式：只读复核（git show 只读 + 当前 HEAD 源码独立核实 + py_compile 抽检；未运行 pytest、未勾选、未改任何源码/计划文件）

## §6 八项检查逐项判定

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | **PASS** | `git show --stat 25695e98d` 共 13 个文件：11 个源码全部在 `src/shared/ui/`（audio_mixer 3 + 根 2 + widgets 6）；1 个测试 `tests/shared/test_ef14_aud.py`；1 个报告 `fixes/reports/ef-14.md`。REMEDIATION_PLAN.md / e_flash_batches.json / FIXLOG.md 零改动。**连带修复核实**：vectorscope FalseColor 图例 QRect NameError——父提交（25695e98d^）中 vectorscope_widget.py:1542 已使用 `QRect` 而 import 行（:20）只有 `QRectF`，即 SDR/HDR 每次重绘必然 NameError 的**既有 bug**，本批补 `QRect` 导入（当前 :19）；文件属白名单 shared/ui，报告"连带修复"一节明确披露。**不构成越界 Critical** |
| 2 | 真实修复（逐条） | **PASS** | 13/13 在当前 HEAD 独立核实成立。重点加码项（缓存键正确性）逐一验证：频响缓存键覆盖 StripEQParams 全部影响频响的字段 + 采样率；热图基底键 (h,w) 与数据形状绑定；FalseColor LUT 与图例键均为 (mode, transfer)。逐 ID 证据见下表 |
| 3 | GLSL/HDR 红线 | **PASS** | 无 GLSL 改动。新增 `np.clip(0,1)` 仅出现在 CIE/热图的**色度域基底显示色**计算（Y=0.5 的 Rec.709 逆变换得到 sRGB 基底，值域本就在 [0,1]）与 FalseColor LUT 索引（整数索引常规做法）——均非 HDR 像素数据钳制。无 601/709 矩阵混用引入 |
| 4 | 并发 | **PASS** | 无新增线程/GL；全部改动为 UI 线程内的缓存与预计算（正是指控要求的方向——把重活从每帧 paint 挪到一次性预计算）。AUD-38417 状态锁辅助用 try/finally 保证释放 |
| 5 | 资源 | **PASS** | 缓存均为有界（按键数量有限：渐变 2+1 支、LUT 1、图例 1、折线 2、热图基底 1）；AUD-38007 五个指标列表改 `deque(maxlen=1000)`，无界增长消除 |
| 6 | 异常 | **PASS** | 新增行零裸 except / 零 `except Exception: pass`；preset_dialog 捕获具体类型 `(OSError, ValueError)` 并 QMessageBox + logger 留痕（AUD-37805） |
| 7 | 测试质量 | **PASS** | 新增 `tests/shared/test_ef14_aud.py` 30 项（与报告一致），每条 ID 有正例；缓存类断言用**对象同一性**（`is`/`is not`）而非值比较，并配失效用例（mode 切换 / transfer 切换 / 形状变化）；`TestAud38417` 含锁释放与用户路径仍发射两类断言。弱断言标注见"测试质量附注" |
| 8 | 回退验证建议 | 已给出 | 见下文（AUD-38209、AUD-38412，含精确原文与 pytest 节点 id） |

## 逐 ID 核实表（13/13，证据为当前 HEAD 的 file:line）

| ID | 计划指控摘要 | 报告结论 | 复查判定 | 当前 HEAD 证据 |
|----|--------------|----------|----------|----------------|
| AUD-37612 | compact/横向分支每帧新建渐变绕过 A8 缓存 | FIXED | CONFIRMED（附注） | audio_level_meter.py:209-210（`_cached_horizontal_gradients` dict + `_cached_compact_gradient`）；:1219-1232（`_update_layout` 预构建 hot/warm/compact 三支）；:605/:960/:966（paint 复用，不再 new QLinearGradient）。**附注**：横向渐变色标锚点从"填充尖端"改为"整条轨道"（与垂直 A8 同语义），报告"复查注意点"已明示该语义变化并给出理由 |
| AUD-37805 | 预设同步 IO + 保存失败静默 | FIXED | CONFIRMED | preset_dialog.py:121-135（`_refresh_list` 捕获 `(OSError, ValueError)` → 空列表 + QMessageBox.warning + logger.warning exc_info）；:176-178（保存失败 QMessageBox.critical + 日志，不清空输入） |
| AUD-37819 | 频响每次重绘重算 scipy | FIXED | CONFIRMED | strip_eq_dialog.py:52（`_RESP_FREQS` 模块常量采样轴）；:137-152（缓存键 8 元组：mode、tuple(bands)、gain、low_cut、high_cut、low_cut_enable、high_cut_enable、sample_rate）。**键正确性验证**：对照 StripEQParams（timeline_v2.py:1629+）字段清单，键覆盖全部影响频响的字段；`enabled` 只控制是否绘制（False 时不画曲线，不参与响应），无遗漏；`set_params` 还保守地整体置空缓存（:85-86） |
| AUD-38007 | 样本列表无界增长 + _max_history/_history 未用 | FIXED | CONFIRMED | playback_hud.py:286-295（五个指标 `deque(maxlen=self._max_history)`，=1000）；:595-609（`record_sample` 同步写入 `_history` 带时间戳记录并按上限截断）。注：每样本全量排序被 maxlen 封顶在 1000 元素内——报告已注明增量分位不在本条范围，接受 |
| AUD-38009 | docstring 示例用 Panel 构造模型 | FIXED | CONFIRMED | playback_hud.py:46-60（示例改为 `hud_model = hud_panel.model`，并注明原示例错误原因） |
| AUD-38100 | 类文档信号契约与实际不一致 | FIXED | CONFIRMED | video_control_panel.py:20-40（文档删去不存在的 frame_changed、play_toggled() 标注无参、补 first_frame/last_frame，与下方 pyqtSignal 声明逐一对齐）。"只修文档不加信号"的边界选择与 doc-gap 指控类型相称 |
| AUD-38201 | 光谱轨迹/色温曲线每次重绘全量重算 | FIXED | CONFIRMED | cie_scope_widget.py:180-205（`_get_locus_polyline`：Catmull-Rom→Bézier 采样折线 + 波长颜色，一次计算缓存于 `_locus_polyline`）；:207-230（`_get_ct_polyline` 按 `curve_kind`（daylight/blackbody）缓存 (kelvin,x,y) 折线）。**键正确性**：光谱轨迹无参数依赖（固定 CIE 数据）；色温曲线唯一变量即类型，paint 只做 xy→px 映射 |
| AUD-38209 | _padding 构造未初始化，fallback 30 ≠ 绘制 35 | FIXED | CONFIRMED | color_warper.py:18-20（构造即 `self._padding = 35`）；`getattr(self, '_padding', 30)` 全文件 grep 零命中（测试 `test_no_stale_fallback` 锁定） |
| AUD-38219 | paint 每帧全量重算 4 通道曲线 | FIXED | CONFIRMED | curves_widget.py:225（`_cached_curve_values`：按通道控制点坐标快照做键缓存 calculate_curve 的 256 点采样）；:250（paint 走缓存）；测试验证点位变动 key 失配自动重算 |
| AUD-38309 | 顶层 import cv2 未使用 | FIXED | CONFIRMED | lut_gallery.py 中 `cv2` 全文件 grep 零命中 |
| AUD-38408 | 800×800 热图全量重算吃帧预算 | FIXED | CONFIRMED | vectorscope_widget.py:93（`_heatmap_base_cache`）；:289-322（坐标网格/基底 RGB/密度索引按 `(h, w)` 缓存，键来自 `density.shape`（:285 `h, w = density.shape`），密度数组形状固定，zoom 仅作用于刻度绘制（:398）不进该路径——键正确）。密度直方图与对数映射仍按数据每帧更新，未被错误缓存 |
| AUD-38412 | FalseColor LUT/图例每帧重建 | FIXED | CONFIRMED | vectorscope_widget.py:1455-1460（`_get_false_color_lut` 键 `(self._mode, self._transfer)`，键失配自动重建——set_mode/set_color_state 无需显式失效即正确）；:1568-1580（图例色条 QImage 同键 `(mode, transfer)` 缓存；:1580 使用 `QRect`，连带修复生效）。测试覆盖 mode/transfer 双向失效与图例缓存 |
| AUD-38417 | 程序化 setChecked 重发 toggled，基类无状态锁辅助 | FIXED | CONFIRMED | viewport_toolbar_base.py:115（`_programmatic_toggle` 标志）；:119-132（`_set_checked_programmatic`：状态相同早退，置标志 try/finally 包裹 setChecked）；:255-262（`_on_mask_toggled` 程序化时只同步图标直接 return；:264+ split 同构）。用户路径行为不变（测试 `test_user_toggle_still_emits` 锁定） |

统计：CONFIRMED 13/13。ISSUE 0，Critical 0。

## 红线发现明细

1. **裸 except / 吞错**：新增行零命中（源文件内 `except Exception` 为零）。
2. **UI 线程 GL / moderngl**：零涉及。
3. **HDR 钳制**：新增 `np.clip(r/g/b_base, 0, 1)` 位于热图**基底显示色**（Cb/Cr 色度网格经 Rec.709 逆变换、Y=0.5 的 sRGB 基色），非 HDR 像素数据；`np.clip(luma * (LUT_SIZE-1), 0, LUT_SIZE-1)` 为 LUT 整数索引。均合规。
4. **601/709**：无矩阵改动（新增注释中"Rec.709 inverse formula"为既有热图基底的固定假设，未混用）。
5. **既有 bug 连带修复**（QRect）：属白名单文件、已在报告披露、修法最小（import 行追加），见 §6 表第 1 项。

## 测试质量附注（非阻塞）

- 强项：缓存断言全部用对象同一性（`is`）+ 失效用例（换 mode/transfer/shape 后缓存必须换新）；`TestAud38417` 四例覆盖程序化不重发/用户仍发射/锁释放。
- 弱点：
  1. `TestAud38009/38100` 为文档字符串内容断言（doc-gap 类相称，但属字符串匹配）；
  2. `TestAud38209::test_no_stale_fallback`、`TestAud38309::test_no_top_level_cv2` 为源码文本 grep，可被动态 import 绕过（回归护栏足够）；
  3. AUD-37612 测试只验证缓存行为与失效，不验证渐变锚点视觉语义（语义变化本身在报告中披露，留给人工评审——见下）。

## 值得中心注意的点

1. **AUD-37612 渐变锚点语义变化**：横向电平条渐变色标从"锚定当前填充尖端"改为"锚定整条轨道"（与垂直 A8 缓存同语义，换取每帧零分配）。若产品上必须保留"尖端锚定"观感，需另行方案（报告已给出权衡说明）。这是本批唯一的行为/视觉语义变化。
2. AUD-38007 的 `_history` 从"从不写入"变为"每样本一条（封顶 1000）"，导出日志体积上限增加约 1000 条 × 6 字段（export_log 的 `[-100:]` 截断不变）——功能上是修复（声明字段终于被使用），但导出体积有实质变化，报告已披露。

## 回退验证建议（供中心会话抽查）

1. **AUD-38209** — 文件 `src/shared/ui/widgets/color_warper.py`（:20）
   - 当前原文（old）：
     ```python
     self._padding = 35
     ```
   - 回退代码（new）：删除该行（构造不再初始化 `_padding`）
   - 应失败节点：`tests/shared/test_ef14_aud.py::TestAud38209Padding::test_padding_initialized_in_constructor`（回退后 `w._padding` 抛 AttributeError）
2. **AUD-38412** — 文件 `src/shared/ui/widgets/vectorscope_widget.py`（:1455-1460）
   - 当前原文（old）：
     ```python
     key = (self._mode, self._transfer)
     if self._fc_lut_cache is None or self._fc_lut_cache[0] != key:
         self._fc_lut_cache = (key, build_false_color_lut(self._mode, self._transfer))
     return self._fc_lut_cache[1]
     ```
   - 回退代码（new）：
     ```python
     return build_false_color_lut(self._mode, self._transfer)
     ```
   - 应失败节点：`tests/shared/test_ef14_aud.py::TestAud38412FalseColorCache::test_lut_cached_per_mode_transfer`（同一性断言 `is lut1` 失败）

## 最终结论

**PASS-with-notes**（Critical 0 / ISSUE 0）

- 13/13 条在当前 HEAD 全部独立核实成立；缓存键正确性（本批重点加码项）全部验证无误。
- 非阻塞 notes：AUD-37612 渐变锚点语义变化与 AUD-38007 `_history` 接线带来的导出体积变化，均已在执行报告披露，建议中心知悉（视觉语义如有异议属产品决策，不构成复查否决项）。
- 建议：允许 `check_fixes.py --mark`（commit 填 25695e98d724237c7a98a3d43eb6993a70d7547d）。
