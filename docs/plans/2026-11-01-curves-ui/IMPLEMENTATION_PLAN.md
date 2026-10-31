# 曲线 / 柔化裁切 UI 整合与后续接线计划

日期：2026-11-01。范围：本次完成 UI，后续功能与接线由项目维护者完成。

## 本次已完成

- [x] 新增 `src/features/color/ui/curves_panel.py`，左侧保留原曲线画布，右侧为 Edit 与 Soft Clip。
- [x] 曲线原拾色、重置按钮移至顶部，保持原控件实例、信号及曲线数据接口。
- [x] Edit 提供 Y / R / G / B、链接按钮、四条强度滑条/数值框、逐行与区域重置。Y 在现有曲线内部仍对应 M。
- [x] YRGB 按钮采用实色通道块，四条强度滑杆的滑块圆点使用对应白/红/绿/蓝通道色。
- [x] 默认链接状态下 YRGB / RGB 通道都显示实色填充；点击单个通道会解除链接，并只高亮当前通道，其余通道显示灰色描边。连接图标无背景边框。
- [x] 曲线网格由点线改为随画布像素大小自适应的连续细线，主网格轻微加深，外边框为黑色细线。
- [x] 曲线工具显示时通过示波器 GPU demand 共用实时 RGB 直方图 pass；直方图在曲线后方绘制，不从 GPU 回读像素。
- [x] Soft Clip 提供 RGB、链接与区域重置，参数**仅保留 Low、Low Soft、High、High Soft**。
- [x] 新面板不再展示曝光、对比度、白点、黑点、肩部强度、趾部强度、独立预览图、启用开关。
- [x] 使用全局 `ParamSliderRow`（`DragValue` + `ToposClickableSlider`）、通道按钮样式和图标。
- [x] 整个曲线工具在 dock 限定宽度内始终保持画布与参数左右排布；窄于 760 px 的容器兜底切换为上下堆叠（resizeEvent 自适应）；侧栏支持滚动。
- [x] 曲线 dock 限定 780–1680 px，避免工具区过窄或在超宽窗口下无限扩张。
- [x] 移除独立 `dockSoftClip`、面板菜单注册、默认布局项、默认 tabify 项。
- [x] 内置 HDR 工作区改用曲线面板；旧用户工作区的 `color.soft_clip` 自动解析为 `color.curves`。
- [x] 中文、英文文案；预览图保存于本目录。

预览：`wide.png`（1000×420）、`compact.png`（请求 760×290，按面板最小高度显示）、`active-channels.png`（R 与 G 激活状态）。

## 当前功能边界与兼容措施

**已保留的功能**：原曲线控制点编辑、单通道选择、拾色、曲线重置以及原有 LUT 信号仍工作。

**仅限本地 UI 草稿**：Edit 强度、Edit 链接、多通道编辑、四参数 Soft Clip、RGB 参数切换与链接。Soft Clip 的 RGB 独立值、链接复制、数值/滑条同步和重置可以本地交互，但不会改变图像。草稿尚不按图层/片段保存，切换目标也不回填；重启后恢复默认。不要将 `get_ui_state()` 的结果当成既有渲染参数。

`MainWindow.soft_clip_panel` 暂时保留为**有父对象且隐藏的旧兼容对象**，不创建 dock，不进入面板菜单。现有 layer/file/page controller 与 `ColorStackHydrator` 仍读写它，旧工程数据和原渲染连接继续存在。它不与新侧栏参数互写。本次没有删除旧模型字段、改 shader 或更改工程 schema；待下面迁移任务完成后删除旧 UI 文件和兼容引用。

### 参考与数值口径

~~早期草稿按 Resolve 截图使用 0…1 归一化滑条位置（Low/High 默认 0.50）。~~
**任务 1 已裁决（2026-11-01）：参数语义以移动版 `topos_edit_mobile` 的实现为权威参考**——桌面与移动端共用同一套参数名、范围、默认值与算法，是"移动工程未来可在桌面打开"目标的基础，优先级高于对达芬奇内部实现的猜测。移动版参考实现：

- `topos_edit_mobile/shaders/soft_clip.frag` 与 `lib/features/soft_clip/state/soft_clip_state.dart`
- `topos_edit_mobile/lib/features/soft_clip/ui/soft_clip_panel.dart`（滑条范围/小数位口径）
- `topos_edit_mobile/shaders/mega.frag`（管线集成位置：curves → hsl_curve → soft_clip）

**已确认语义（桌面按此实现，UI 滑条用物理值而非归一化位置）**：

| 参数 | 键名 | 默认 | 范围 | 含义 |
|---|---|---|---|---|
| Low | `low_clip` | 0.0 | [-1.0, min(1.0, high_clip)] | 暗部地板 |
| Low Soft | `low_soft` | 0.0 | [0, gap − high_soft] | 暗部软膝宽度 |
| High | `high_clip` | 1.0 | [max(0.0, low_clip), 2.0] | 亮部天花板 |
| High Soft | `high_soft` | 0.0 | [0, gap − low_soft] | 亮部软膝宽度 |

- `gap = max(high_clip − low_clip, 0)`；两个 soft 互斥共享 gap（`low_soft + high_soft ≤ gap`），移动一个 clip 点会把越界 soft 重新压回 gap 内（移动版 `withControl` 约束）。
- **算法 = 指数软膝**：`value = clip ± knee * exp(±(value − start)/knee)`，膝内斜率从 1 无缝衔接并渐近衰减到 0，不放大对比度、不偏色。CPU/GLSL/MSL 三端与移动版逐式对齐。
- **默认值即恒等映射**：`low_clip=0, low_soft=0, high_clip=1, high_soft=0` 时函数严格直通，超白（>1.0）与负 RGB 原样保留给后续节点；只有移动过的 clip 点或打开的软膝才会限幅。
- 结构：R/G/B 每通道独立 + 链接模式（默认链接；链接时把当前通道复制到全部通道，点单通道按钮解除链接并以该通道为源）。
- 管线位置：curves → hsl_curve → **soft clip**（与移动版 mega.frag 一致；桌面现有 legacy soft_clip 槽位相同，新阶段叠加在其后）。
- 处理域：线性浮点 RGB（HDR float 内部处理），不做过早截断。
- 滑条小数位 2；移动版无"启用"开关语义差异——桌面新模型不设 `enabled` 字段，非恒等即生效（移动版 `enabled=false` 导入桌面时映射为全部默认值）。
- **仍然有效**：不得把 `low_soft` 直接接旧模型 `toe_strength`，或把 `high_soft` 接 `shoulder_strength`——旧 8 参数算法作为旧工程兼容路径完整保留，与新 4 参数阶段互不转换。

当前 `CurvesPanel.get_ui_state()` 返回：

```python
{
    "edit_channel": "M",  # 现有画布的活动编辑通道
    "edit_linked": True,
    "edit_amounts": {"Y": 100.0, "R": 100.0, "G": 100.0, "B": 100.0},
    "soft_clip_channel": "R",
    "soft_clip_linked": True,
    "soft_clip_channels": {
        "R": {"low_clip": 0.0, "low_soft": 0.0, "high_clip": 1.0, "high_soft": 0.0},
        "G": {"low_clip": 0.0, "low_soft": 0.0, "high_clip": 1.0, "high_soft": 0.0},
        "B": {"low_clip": 0.0, "low_soft": 0.0, "high_clip": 1.0, "high_soft": 0.0},
    },
}
```

## 后续任务表（本次不实现）

| 顺序 | 状态 | 工作 | 主要位置与完成标准 |
|---|---|---|---|
| 1 | 已完成 | 明确参数语义 | 以移动版 `topos_edit_mobile` 实现为权威参考完成裁决（见上方"参考与数值口径"）：四参数物理值/范围/默认值、指数软膝算法、默认恒等保留 HDR、RGB 链接复制语义、管线位置与处理域均已确认并落入 UI 草稿。 |
| 2 | 已完成 | 新参数模型与 schema | 新模块 `src/features/color/core/curves_soft_clip.py`：默认值/归一化（兼容移动版 camelCase 与 red/green/blue 别名）/withControl 约束移植/指数软膝 CPU 参考/恒等判定。`Layer` 新增 `curves_soft_clip`、`curves_edit`（默认/重置/复制/粘贴/序列化全接）；`ColorStackData` 升 v3（平铺键 `curves_soft_clip`/`curves_edit`，非空才写，未知 payload 保留，读端不分支版本号）。 |
| 3 | 已完成 | 旧工程迁移 | 旧 8 参数 `soft_clip_params` 保留原算法渲染、不转换；v2 工程载入侧栏键缺失即恒等默认，重开/再保存所有旧字段逐键不变（测试 `test_legacy_v2_layer_dict_loads_identity_and_resaves_stable`）；`build_curves_lut_from_data` 重写为与 widget 同构（先 master 后通道），消除重开分叉，空曲线返回恒等斜坡。 |
| 4 | 已完成 | 四参数处理实现 | GLSL `CURVES_SOFT_CLIP_SNIPPET`（指数软膝，编译期门控 `has_curves_soft_clip` 进 program 缓存键，无运行期开关 uniform）；uniform `u_cs_*` 追加在基础段末尾（既有偏移不变、值恒写防串参）；MSL 第 12 个迁移效果；CPU `apply_curves_soft_clip_image`（非有限像素直通）；成片导出（逐层载荷/平铺键/应用点/快速路径门控）、LUT 烘焙、advanced 门控、delivery matrix 新 op、缩略图 dirty 判定全部接入。真机 GL↔MTL↔CPU 三端对拍（SDR ≤1/255、HDR ≤2e-3 rel + CPU 参考复验）全过。 |
| 5 | 已完成 | Edit 强度与曲线链接 | 强度语义裁决为 **LUT 混合**：`layer.compose_curves_lut` 权威合成（master 增量按 Y 强度缩放、通道叠加增量按各自强度缩放，100=全额、0=直通），widget 与工程重建共用同一实现（消除 M+通道组合的重开分叉；外推斜率量纲修正，GPU edge clamp 下视觉等价）。链接编辑落在画布（加点/拖动 dy 镜像/删点/双击重置同步全通道，默认关闭零回归），Y 对应 M 映射沿用 `set_active_channel`。 |
| 6 | 已完成 | UI 信号与 command 接线 | `CurvesPanel.soft_clip_changed` / `edit_changed` 正式信号（16ms 节流 + interaction 结束 flush 最终值），`interaction_started/ended` 复用 `on_interaction_started` 撤销快照通道（一次连续拖动 = 一次 undo）；engine 新增 `set_curves_soft_clip_params` / `set_curves_edit_params`（归一化 + 活动层 + LUT 重建 + UPDATE_AUX_TEX + grading 渲染）；UI 不直接调 GPU。画布控制点仍走原 `changed`，两路不双提交；timeline clip 经既有 `UpdateColorStackCommand` layers 快照自动随行。 |
| 7 | 已完成 | 图层/片段回填与持久化 | `Layer` 序列化全链路（to_dict/from_dict/reset/copy/paste）已在批次 2 落地；`update_from_layer` 随图层切换整体回填（草稿不跨目标泄漏）；hydrator `_apply_curves_palette_to_panel`（活动层快照优先、平铺键兜底）+ 无引擎时收集兜底；page_controller 注入 `curves_palette`；file_controller 视频加载重置改为曲线工具整体回默认；grade 剪贴板 curves 类别扩展为控制点+强度+四参数整体粘贴；undo/redo 经快照恢复 + update_from_layer 回填闭环。 |
| 8 | 已完成 | 曲线背后的直方图 | 曲线 dock 显示时，GPU scope demand 同时输出 histogram descriptor，复用 histogram accumulation/reduce/display；曲线 widget 在 GPU blit 后叠画网格、曲线和控制点。实机验收仍需校对坐标和强度。 |
| 9 | 已完成 | 彻底删除隐藏旧面板 | `soft_clip_panel.py` 已删（git rm）；`ui/__init__.py` 导出替换为 CurvesPanel；MainWindow 删除导入/创建/4 条旧信号连接；layer_controller 回填改注释说明；page_controller 面板集合移除 `soft_clip_panel`；file_controller 视频加载重置改为曲线工具整体回默认（画布 reset + 侧栏 set_layer_state）；hydrator 删除 `_apply_soft_clip_to_panel` / `_read_soft_clip_params`（无引擎图层时 legacy 8 参数保持空 dict，图层路径始终权威）；i18n 清理 `soft_clip.*` 13 键与 `panel.soft_clip`（保留 `gradecat.soft_clip`——legacy 粘贴类别、`delivery.op_soft_clip`——delivery 矩阵、`curves_palette.soft_clip`）；测试替身同步清理（hydrator/wd1g7/layer_ai_library/presets_gallery/auto_color/page_controller）。legacy 8 参数渲染路径与脚本 API 保留（旧工程兼容）。 |
| 10 | 待办 | 验收与性能 | 建立参考数值/图像、RGB 非链接、soft=0、HDR 超白/负值、重置、undo/redo、目标切换、旧工程迁移及 preview/export 一致性测试；检查 4K 拖动节流、最终值 flush、资源释放、失败/取消路径。 |

## 接线时的注意事项

- 当前侧栏操作不能发原 `CurvesWidget.changed`；已有曲线控制点操作仍走原信号，不要重复连接导致双提交。
- Edit 区域重置目前只复位四条强度；顶部重置只复位原曲线。正式产品需确定整面板重置、区域重置和单通道重置的 undo 范围。
- Soft Clip 链接时本地值跟随当前通道复制至 RGB；解链后各通道独立，区域重置作用于当前通道。正式语义若不同，应同步修改模型、UI 与测试。
- 不为本次 UI 更改整体布局 schema 或重置用户布局。`dockCurves` 名称保留，旧独立 dock 不再创建；工作区 ID 兼容由 `sanitize_preset()` 完成。
- 归档 `src/features/color/ui/tools_panel.py` 没有生产引用，本轮未改；它仍使用基础画布。如未来重新启用，应改用 `CurvesPanel`。

## 验证记录

- UI 测试：`tests/color/test_curves_panel_ui.py`，验证草稿不触发渲染、RGB 本地链接/解链、拾色与曲线重置信号保留、窄窗口可达及旧工作区入口兼容。
- 回归：ColorStackHydrator、workspace presets、LayoutStore、曲线采样缓存、dock layout controller。150 项现有回归通过；6 项新增 UI/兼容测试通过（这些用例在 GPU 直方图接线前运行，后续改动尚未重跑）。
- 可视检查：本目录三张截图用于 UI 通道状态与布局；GPU 直方图需在实际 GPU 渲染器下进一步实机验收。
- Python 编译检查、`git diff --check`；GPU 直方图接线后尚未运行测试或实机 GPU 验收。
- 当前虚拟环境没有安装 Ruff，未运行 Ruff 检查。
- **2026-11-01 续批验证**：curves 面板 7 项、模型/水合/历史/剪贴板/delivery 163 项、渲染/CPU/uniform 137 项、project 555 项、layer 全套通过。
- **预存失败（与本计划无关）**：`tests/color/test_shader_golden_corpus.py::test_bit_identical_to_baseline` 的 `grading_extreme/checker` 用例在 e7ffa56（本计划所有改动之前）即失败，且与当前哈希逐位一致——判定为本机 GL 驱动/环境的数值漂移（golden 基线在别的环境归档），非本计划引入。未重归档（重归档会掩盖漂移），待维护者在其确认基准环境上复核后决定 `TOPOS_UPDATE_SHADER_GOLDEN=1`。
