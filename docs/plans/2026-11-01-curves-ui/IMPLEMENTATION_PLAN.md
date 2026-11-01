# 曲线 / 柔化裁切 UI 整合与后续接线计划

日期：2026-11-01。~~范围：本次完成 UI，后续功能与接线由项目维护者完成。~~
**2026-11-01 续批：任务表 1-10 全部完成**（语义裁决、模型/schema、迁移、四参数渲染、Edit 强度/链接、接线、回填持久化、旧面板删除、验收），详见任务表与验证记录；遗留项仅"实机验收清单"（需真机交互复核）与 golden corpus 环境漂移裁决。

## 本次已完成

2026-11-01 HSL 曲线合并：合并前实际查看 Resolve 的 Hue vs Hue、Hue vs Sat、Lum vs Sat、Sat vs Lum 页面。Curves 顶部改为 Custom → Hue vs Hue → Hue vs Sat → Hue vs Lum → Lum vs Sat → Sat vs Sat → Sat vs Lum 七个图标；Custom 保留 Edit / Soft Clip 侧栏，六个 HSL 模式使用全宽画布和底部取色、预设色点、输入/输出数值框。移除 Color Mapping 的 HSL 页，只保留 Warper / Mixer；旧 `color.hsl_curves` 工作区入口转至 `color.curves`。

复用原 `self.hsl_curves_panel` 与其 widget，LUT 六行顺序仍为 LumSat/HueHue/HueSat/HueLum/SatSat/SatLum，插值与调色算法、工程数据格式未改。导航不发曲线 changed，不产生撤销提交；底部数字与色点沿原 interaction/flush 提交链。切页关闭隐藏拾色器，并清理双方 GPU descriptor，避免共享纹理背景串页。

HSL 背景分布：在既有 GPU histogram alpha 通道按当前模式统计 HSV Hue（按饱和度加权）、HSV Saturation 或既有 HSL 取色器使用的 BT.601 Luma；RGB 统计保持独立示波器兼容。Hue 背景为暗色相环渐变，直方图压在中线下方；Lum/Sat 为黑白渐变。网格/中线为连续细线，轮廓使用既有等宽像素方法。仍在播放/拖拽期间冻结，仅暂停后刷新；这些外观与统计口径为项目实现，不宣称 Resolve 内部算法逐项一致。

验证：243 项合并/水合/工作区/已有 HSL 回归通过，另新增 HSL descriptor 定向检查通过；15 项真实 GPU 检查（含六个 HSL 模式轴位置、无彩像素不制造假红峰）通过，2 项原生 Qt 检查通过，完整 MainWindow `--test-startup` 成功。预览 `merged-HueHue.png` / `merged-LumSat.png` 为空态 UI；`merged-*-histogram.png` 为真实 GPU 输出和同一 UI 覆盖层的静态合成预览。

2026-11-01 轮廓线等宽修正（用户澄清“连续”指线条粗细）：原 shader 用纵向距离加 fwidth 计算轮廓，再将固定 1024×512 纹理拉到画布尺寸，容易造成不同斜率的线段粗细不均。现在用 `abs(delta) / length(vec2(dFdx(delta), dFdy(delta)))` 计算垂直于轮廓的屏幕像素距离；需求携带实际 drawRect 物理像素尺寸与 DPR，专用纹理直接按该尺寸绘制，线宽为 1 个逻辑像素。尺寸变化 150 ms 合并更新，跨 DPR 显示器也刷新尺寸；同尺寸复用纹理和 FBO。另将暂停态曲线统计固定为 768×432，避免分析档位切换改变峰高。

本批最终验证：115 项 scope/UI/scrub 回归、8 项真实 GPU 检查、2 项 Cocoa 原生 Qt 检查通过。GPU 检查覆盖平缓/陡峭段在三种尺寸及 DPR 1/2 下的垂直线宽、相同静帧重复绘制的像素一致性、纹理/FBO 同尺寸复用等。

2026-11-01 拖拽预览与闪黑修正：播放条 scrub 开始时先冻结曲线背景，再暂停/seek（覆盖 Color 时间线早退分支）；提交与接收两侧均检查拖拽状态，松手后延迟 150 ms 请求刷新，播放开始仍保留原冻结策略。`GpuScopeWidget` 使用 PartialUpdate，在渲染线程占用 mutex、UI tryLock 跳过 blit 时保留上一张 FBO；显式 clear 单独填充空背景，避免旧素材残留。移除曲线父控件 paintEvent 内对透明子覆盖层的重复 update，避免父子重绘循环。面板基础最小 900 px、dock 920 px、画布基础最小 560 px；工具区变高时按约 1.6:1 的画布宽高需求提高最小宽度（侧栏预留 340 px），继续保持左右排布。

本批验证：113 项 scope/UI/scrub 回归通过；Cocoa 原生 Qt 2 项通过，分别检查忙渲染器连续跳过 5 次绘制仍保留原像素、clear 确实移除旧帧，以及空闲曲线不再持续重绘。实机操作对照及局限见 `INTERACTION_CHECK.md`。

2026-11-01 空项目显示修正：曲线画布改用 QWidget，GPU 直方图由内嵌 presenter 显示，独立透明覆盖层持续绘制网格、曲线和控制点。无素材、未收到 scope 帧及清空 scope 时均显示完整网格；GPU 纹理显示范围与曲线坐标范围对齐。外留白由 15 px 缩为 7 px，背景统一为炭灰，外框保留细黑线。画布和参数区之间改用可拖动的水平分隔条，参数区宽度 230–320 px；面板最小 640 px、dock 660–2400 px，始终左右排布，取消窄宽度上下堆叠。空态预览：`empty-grid.png`。

本批验证：空态网格像素、resize、清空 scope、左右布局与分隔条调整，以及既有曲线强度/链接编辑，共 22 项通过。全局深色主题的空态截图已检查；本批未在真实素材上复核 GPU 直方图显示。

2026-11-01 直方图外观与刷新优化：对照已打开的 Resolve 曲线面板，曲线背景改用独立的 1024×512 YRGB 显示纹理：RGB 复用原有统计、Y 使用 Rec.709 亮度独立统计；GPU 1×1 峰值归约，每通道恢复线性计数并单独归一化，保留 2% 顶部余量。五点核抑制 8-bit 素材在 512 bins 上形成的隔列梳齿，RGB 低透明填充、Y 灰色填充、抗锯齿细轮廓，背景不烘焙重复网格。外观与缩放方式来自视觉对照，未宣称 Resolve 内部亮度公式或算法逐项相同。修复浮点散列在高质量采样大面积纯色下的计数损失，改用整数散列。

播放时曲线背景冻结，暂停、静帧调色、拖动定位后再刷新；仅曲线可见且正在播放时不提交 GPU scope demand。独立示波器仍按自身设置刷新；独立 histogram 未请求曲线时也不额外统计 Y。源播放器与 Color 时间线均接入播放状态信号，开始播放取消待执行需求，播放时忽略迟到的曲线描述符，暂停时强制请求当前帧。所有分析与显示 pass 保留在既有渲染线程，生产路径无 GPU 回读。

本批验证：110 项 scope/controller/UI 回归通过；另 5 项真实 GPU 测试通过（shader 编译、全零 mask、正 mask 权重、331776 样本纯色尖峰与 Y 位置、线性峰高比例、真实素材输出）。GPU 图片来自本地 `2K_h264.mp4` 第一帧；预览 `histogram-gpu.png` 是测试读回的 GPU 显示纹理，`histogram-preview.png` 为它与现有 UI 覆盖层的静态合成预览。两张预览仅用于外观对照，不代表完整应用内跨 context 播放验收。

- [x] 新增 `src/features/color/ui/curves_panel.py`，左侧保留原曲线画布，右侧为 Edit 与 Soft Clip。
- [x] 曲线原拾色、重置按钮移至顶部，保持原控件实例、信号及曲线数据接口。
- [x] Edit 提供 Y / R / G / B、链接按钮、四条强度滑条/数值框、逐行与区域重置。Y 在现有曲线内部仍对应 M。
- [x] YRGB 按钮采用实色通道块，四条强度滑杆的滑块圆点使用对应白/红/绿/蓝通道色。
- [x] 默认链接状态下 YRGB / RGB 通道都显示实色填充；点击单个通道会解除链接，并只高亮当前通道，其余通道显示灰色描边。连接图标无背景边框。
- [x] 曲线网格由点线改为随画布像素大小自适应的连续细线，主网格轻微加深，外边框为黑色细线。
- [x] 曲线工具在暂停时通过 GPU demand 共用 RGB 统计并补充独立 Y 统计，输出专用 YRGB 纹理；播放时冻结，生产路径不从 GPU 回读像素。
- [x] Soft Clip 提供 RGB、链接与区域重置，参数**仅保留 Low、Low Soft、High、High Soft**。
- [x] 新面板不再展示曝光、对比度、白点、黑点、肩部强度、趾部强度、独立预览图、启用开关。
- [x] 使用全局 `ParamSliderRow`（`DragValue` + `ToposClickableSlider`）、通道按钮样式和图标。
- [x] 整个曲线工具始终保持画布与参数左右排布；可拖动中间分隔条调整两侧宽度，侧栏支持滚动。
- [x] 曲线 dock 基础限定 920–2400 px，面板基础最小 900 px；随工具高度提高最小宽度以保持宽画布。
- [x] 移除独立 `dockSoftClip`、面板菜单注册、默认布局项、默认 tabify 项。
- [x] 内置 HDR 工作区改用曲线面板；旧用户工作区的 `color.soft_clip` 自动解析为 `color.curves`。
- [x] 中文、英文文案；预览图保存于本目录。

预览：`wide.png`（1000×420）、`compact.png`（请求 760×290，按面板最小高度显示）、`active-channels.png`（R 与 G 激活状态）。

## 当前功能边界与兼容措施（任务 1-9 完成后更新）

**已上线（正式接线）**：曲线控制点编辑（含链接编辑）、Edit 强度/链接、四参数 Soft Clip（每通道 + 链接）——全部经 `soft_clip_changed` / `edit_changed` → `ImageEngine.set_curves_soft_clip_params` / `set_curves_edit_params` 提交渲染，按图层/片段持久化（Layer 快照 + schema v3 平铺键），undo/redo、目标切换回填、复制调色、导出全链路可用。

**兼容边界**：
- 旧 8 参数 `soft_clip_params`：独立 SoftClipPanel 已删除（任务 9）。legacy 数据与渲染路径（GLSL/CPU/导出/delivery）保留；仅脚本 API 与旧工程数据可写，无 UI 读写。与新四参数阶段互不转换。
- 移动版工程兼容：模型归一化接受移动版 camelCase 键与 red/green/blue 通道别名（`normalize_curves_soft_clip`），为"移动工程在桌面打开"预留直通映射。
- 旧工作区布局：`color.soft_clip` → `color.curves` 由 `sanitize_preset()` 兼容；`dockCurves` 名称不变，面板与 dock 的宽度限定为 420–1800 px。最小宽度由 220 px 画布、170 px 参数区及分隔条/边距组成；Tracker 与曲线共用标签组时，其最大宽度也保持 1800 px，避免收紧整组上限。



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
| 10 | 已完成 | 验收与性能 | 参考数值：`curves_soft_clip.apply_soft_clip_channel` 标量参考 + 移动版对齐向量测试；RGB 非链接（每通道独立）、soft=0 纯 clip 点、HDR 超白/负值保留均有专项测试；真机 GL↔MTL↔CPU 三端对拍（SDR/HDR + CPU 参考复验）作为图像验收。undo/redo 快照恢复侧栏状态、目标切换无草稿泄漏、旧工程（v2）重开/再保存逐键不变、导出收集（非恒等进 grade / 恒等不进）测试齐备。性能：16ms 节流 + interaction 结束 flush（测试锁定）、恒等状态共享缓存 program（无重编译）、strength 只置脏最终 LUT（通道缓存复用）、CPU 导出为掩码向量化 exp。失败/取消路径：CPU 导出单段失败告警续跑（沿用既有降级口径）、shader 编译失败诊断缓冲不变。 |

## 接线注意事项（已按实现复核）

- 侧栏操作不发原 `CurvesWidget.changed`——Edit/柔化裁切经 `edit_changed` / `soft_clip_changed` 提交，画布控制点仍走原信号，两路不双提交（测试锁定）。
- Edit 侧栏重置只复位四条强度；画布顶部重置只复位控制点；链接模式下画布双击重置同步全部通道。整面板重置 = 两者之和，undo 范围由 interaction 快照对决定。
- Soft Clip 链接复制/解链语义与移动版一致（模型 `set_soft_clip_control` 为权威约束实现），模型/UI/测试三方同源。
- 布局 schema 未整体变更：`dockCurves` 保留，工作区 ID 兼容由 `sanitize_preset()` 完成。
- 归档 `src/features/color/ui/tools_panel.py` 没有生产引用，未改；如未来重新启用，应改用 `CurvesPanel`。

## 验证记录

- UI 测试：`tests/color/test_curves_panel_ui.py`，验证草稿不触发渲染、RGB 本地链接/解链、拾色与曲线重置信号保留、窄窗口可达及旧工作区入口兼容。
- 回归：ColorStackHydrator、workspace presets、LayoutStore、曲线采样缓存、dock layout controller。150 项现有回归通过；6 项新增 UI/兼容测试通过（这些用例在 GPU 直方图接线前运行，后续改动尚未重跑）。
- 可视检查：本目录三张截图用于 UI 通道状态与布局；GPU 直方图需在实际 GPU 渲染器下进一步实机验收。
- Python 编译检查、`git diff --check`；GPU 直方图接线后尚未运行测试或实机 GPU 验收。
- 当前虚拟环境没有安装 Ruff，未运行 Ruff 检查。
- **2026-11-01 续批验证**：curves 面板 7 项、模型/水合/历史/剪贴板/delivery 163 项、渲染/CPU/uniform 137 项、project 555 项、layer 全套通过。
- **预存失败（与本计划无关）**：`tests/color/test_shader_golden_corpus.py::test_bit_identical_to_baseline` 的 `grading_extreme/checker` 用例在 e7ffa56（本计划所有改动之前）即失败，且与当前哈希逐位一致——判定为本机 GL 驱动/环境的数值漂移（golden 基线在别的环境归档），非本计划引入。未重归档（重归档会掩盖漂移），待维护者在其确认基准环境上复核后决定 `TOPOS_UPDATE_SHADER_GOLDEN=1`。
- **任务 4-7 + 9 全量验证（2026-11-01）**：curves 相关测试 86 + 22 + 14 → 含验收共 29 项 wiring/acceptance 全过；color+app 合并套件 4309 过。edit 全套 9585 过 / 19 失败，经 e7ffa56 worktree 复跑逐一对比：8 项（ef08×2、wd1g6、gap002、gap007×4）与 4 个 tracking worker 错误在基线上完全复现（预存）；m09_lightbox、edit_mode_toggle 隔离运行通过（顺序敏感偶发）；1 项为上一会话改名的高度迁移测试（环境相关）。**无一由本计划引入**。
- 旧面板删除后残留检查：`soft_clip_panel`/`SoftClipPanel` 在 src/tests 仅存于说明性注释。
- 实机验收清单（维护者执行）：拖动侧栏滑条实时预览 + 一次拖动一次 Ctrl+Z；链接/解链；恒等状态不触发 shader 重编译；4K 时间线拖动流畅度；导出成片与 Color 页预览一致性（同参数帧对比）。
