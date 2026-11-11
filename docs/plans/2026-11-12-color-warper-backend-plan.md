# Color Warper 后端实施与验证计划

日期：2026-11-12。状态：**全部为后续待实施任务，本文件不代表后端能力已经实现。**

本轮范围是调色页 Color Warper UI 与交互。按用户后续要求，Warper和Mixer已从原Color Mapping容器拆成独立停靠面板；原有两个控件实例和信号继续复用。参考用户截图及本机已打开的 DaVinci Resolve Studio 20；面板背景、通用按钮、下拉与数值框使用 Topos 全局样式。本计划记录把这些 UI 接入真实图像处理、工程模型和渲染链所需工作。新模式的局部网格／笔画／Pin 显示不等于图像已生效；不得将 UI 展示状态误认为可保存或可导出的调色结果。

## 1. 当前事实、范围与实机基线

### 1.1 已有链路

| 已确认文件 | 已有职责 | 后续改动边界 |
| --- | --- | --- |
| `src/shared/ui/widgets/color_warper.py` | legacy HS 坐标编码、Δhue/Δsat 计算与回填、16ms changed 合流基类；旧数据为方阵 `(N,N,2)` | 保持旧数组解释兼容，不把新空间或Luma塞入两分量数据 |
| `src/shared/ui/widgets/warper_canvas.py` | 新HS/CL画布、六角/双网格绘制、命中、多选、Pin、范围、局部平滑/数值、取消和展示快照 | 是UI交互模型；只有受门控的legacy HSV方阵可到旧渲染链，新色彩算法仍待实现 |
| `src/shared/ui/widgets/chroma_warp_canvas.py` | 笔画与Pin的创建、选择、端点/整体拖动、删除、每对象参数及Escape恢复 | 笔画和Pin目前只在展示层，不存在真实图像求解器 |
| `src/features/color/ui/warper_tools.py` | MeshTools/StrokeTools、范围条、带component的数值手势信号 | 复用全局DragValue/ToggleSwitch/ChevronComboBox；不自行提交工程 |
| `src/shared/ui/widgets/warper_icons.py` | Warper专用矢量图标绘制 | 无图像处理职责，不引入GPU资源 |
| `src/features/color/ui/warper_panel.py` | Warper 控件封装、参数回填、reset、向外转发交互信号 | 新 UI 状态和命令经独立信号传递；保持旧信号兼容 |
| `src/features/color/module.py`、`src/app/controllers/dock_layout_controller.py` | 独立注册`color.warper`/`color.mixer`与`dock_warper`/`dock_mixer`，管理停靠和布局迁移 | 组合容器已删除；不新建第二套Warper controller或模型 |
| `src/app/main_window.py` | 创建面板；连接旧 warper_changed / interaction 信号 | 仅组装新的 host 适配；不嵌入色彩算法 |
| `src/app/controllers/layer_controller.py` | `on_warper_changed` 调引擎，切层回填、预览请求、播放缓存失效 | 选择当前 clip / corrector / layer 的权威提交入口 |
| `src/app/controllers/menu_toolbar_controller.py` | `on_interaction_started/ended`，历史快照、图手势结束、自动保存 | 新手势不能另做一套互相竞争的 undo 栈 |
| `src/features/color/core/image_engine.py` | `set_warper_params` 写活动层，向 renderer 提交 `UPDATE_AUX_TEX` | 复用渲染队列；不在 UI 同步创建纹理 |
| `src/features/color/core/layer.py` | 旧 warper_params 的复制、清除、持久化 | 新 schema 需版本与迁移；保留未知字段 |
| `src/features/color/core/composition_uniforms.py` | 从 warper 第一维取得单个 grid_size | 新非方阵与模式描述不能继续只传一个尺寸 |
| `src/features/color/core/shader_nodes.py`、`shader_nodes_msl.py` | GLSL / MSL `apply_warper` | 当前是 HSV 位移；不是 HSP、Chroma-Luma 或笔画求解器 |
| `src/features/color/core/shader_compiler.py`、`msl_composition.py` | 组合管线的 Warper 调用 | 声明新能力与模式的合法组合，保持两端一致 |
| `src/features/color/core/color_graph_texture.py` | 图路径 Warper 纹理绑定 | 与 legacy 引擎使用同一数据契约 |
| `src/features/color/core/color_graph_service.py`、`color_graph_snapshot.py` | 图会话的提交与快照 | 复用已有命令／revision／快照，不只改 UI 或引擎投影 |
| `src/features/color/core/history.py`、`grade_clipboard.py` | 历史与调色复制域 | 新模型需参与深拷贝与类别选择 |
| `src/features/color/core/lut_exporter.py`、`graph_lut_export.py` | 调色／图 LUT 导出 | 按真实后端能力允许或拒绝，禁止静默省略新 Warper |
| `src/features/color/core/scope_graph_sampling.py`、`scope_types.py` | 现有图采样与示波器结果契约 | 优先复用其线程与身份管理经验；不是直接假定现有输出满足 Warper 云图 |

**兼容性关键事实：** 旧 shader 通过 `rgb2hsv(abs(color))` 采样二维 Δhue/Δsat 纹理，低饱和区域有 fade，恢复时保留输入符号。旧 UI 标签若显示 HSP，也不会把算法变成 HSP。迁移必须把旧工程标为明确的 legacy HSV 行为，不能直接替换色彩空间从而改变画面。

### 1.2 实机参考项与待核实项

| 模式 | UI 观察记录 | 后端必须落实 |
| --- | --- | --- |
| Hue–Saturation | 独立 Hue / Saturation Resolution；6、8、12、16、24；Link；HSV / HSL / HSP | 独立分辨率重采样、准确坐标转换、空间切换语义、色相环绕 |
| Chroma–Luma | 并列 Grid 1 / Grid 2；Chroma / Luma Resolution；Axis Angle；Auto Pin Columns / Rows / Single | 双轴网格的组合、角度旋转、亮度定义、独立尺寸与保护边界 |
| Chroma Warp | Normal / Point-to-Point 添加笔画、Add Pin、Select、Delete；Pin: Chroma Range 0.040、Tonal Range Low 1、High 1、Pivot 0.5、Exposure 0 | 笔画约束求解、局部色度/色调权重、固定点、曝光、模式差异 |
| 公共 Tools | Select / Deselect、Draw Selection、Pin / De-pin、Pull / Push、falloff 增减、反选、选点转 Pin、Column、Ring / Row、All、Reset Selection / Pins | 明确哪些是纯 UI 选择，哪些改变求解约束；手势原子提交 |
| Range / Auto Lock | HS Auto Lock 1 / 2 Points；范围色带；Hue / Sat / Luma 数值；Hue/Sat Smooth / Reset，Luma Reset | 范围到实际像素权重、锁定邻域、数值域、平滑与重置算法 |

上述控件已具备本地UI响应：三模式切换保留各自展示数据；HS Luma使用独立展示值；Hue/Sat平滑分别只改目标分量；CL网格可切换并保留点位/Pin；Chroma Warp支持每笔画和每Pin独立参数。它们尚不等于对应图像算法、持久化或导出已接通。真实密度云仅接受后端传入的overlay，不生成示意云冒充真实图像统计。

以上是观察基线，不是 Resolve 私有算法的规范。当前CL UI提供4、6、8、12、16、24候选值，但完整候选列表与菜单细节仍须在CW-B00复核；每个模式Auto Lock与Auto Pin的优先级、Axis Angle和各数值的精确范围/单位、色彩空间内部常数、场/颜色范围及笔画求解细节均待证据冻结。当前UI采用的有限范围不是已验证的色彩科学规格。未验证项不得用猜测填成确定规范。

## 2. UI / 后端契约建议

本节区分本轮实际UI信号与后续host契约建议；接入时以 `warper_panel.py` 和 `tests/color/test_warper_resolve_ui.py` 为准。当前快照是展示数据，不是正式工程schema。

### 2.1 兼容信号与新信号

- `warper_changed(object)`：保留 `None` 或legacy HS方阵ndarray。常规编辑、标题栏Reset和分辨率更改仅在当前HS、方阵且空间为HSV时发布；HSP/HSL、非方阵、HS Luma及CL/CW均不发旧纹理。`None`表示清除旧warp。独立的全局 `reset_all()` API清全部画布并发布一次None，不受当前查看模式限制。
- `interaction_started()` / `interaction_ended()`：只有受支持的legacy编辑向外发旧手势信号；MeshTools内部数值手势携带component，HS Luma拖动与新模式不创建旧历史。Hue/Sat连续数值拖动合并为一次手势。新增正式command接入后仍须避免双重提交。
- `ui_state_changed(dict)`：实际字段为 `version: 1`、`mode`、`linked`、`show_grid`、`show_distribution`、`modes`。`modes`含三个canvas的独立snapshot；当前不连接GPU，不代表工程已提交。
- `command_requested(dict)`：实际字段为 `mode`、`action`、`payload`、`state`（当前canvas快照）。菜单/工具/参数等发出意图；指针编辑主要经ui_state_changed通知，尚无完整begin/update/commit/cancel命令协议。后续host验证目标、能力、数据、revision并转权威service，成功后回填。当前没有新后端消费者，不伪造成功。

独立控件默认HS展示空间为HSP，属于待接入UI；宿主通过 `set_params(legacy ndarray或None)` 回填当前图层时将HS空间恢复HSV，使旧效果和空图层原有HS编辑均可继续使用。`set_params(None)`同时清旧HS几何/选择并取消待发样本。HSP/HSL仍可从底栏选择并预览交互。模式导航只是查看各自UI，不能解释为新模式已在图像处理中激活。标题栏 `on_reset()` 只重置当前模式；公开 `reset_all()` 是全局调用兼容入口，两者语义有意区分。

建议在 UI 信号外增加 host envelope：`request_id`、`context_revision`、`project_id`、`clip_id`、`corrector_id/layer_id`、`gesture_id`、`base_revision`、`phase(begin/update/commit/cancel)`、`operation`、`payload`。身份从 host 当前上下文取得，UI 不自行推断活动目标。

当前 `action` 与拟议 `operation` 的映射由适配器明确转换；上述envelope字段尚未实现，不能假定UI已经提供可靠目标身份或revision。

### 2.2 状态分层

| 状态 | 作用域与保存策略 |
| --- | --- |
| 当前工具、hover、rubber-band、临时绘制轨迹、展开状态 | 展示状态；无需新增调色历史；按 UI 偏好决定是否保存 |
| 选择点集合 | UI 会话状态；若影响后续命令只在命令中传稳定 point ID；不作为像素结果来源 |
| Pin / Auto Lock / Auto Pin 产生的有效约束 | 算法输入；一旦影响图像，必须进入版本化调色模型与 undo |
| 模式、空间、两轴分辨率、Axis Angle、网格位移、笔画、Pin 的范围与曝光 | 文档/调色状态；权威模型、历史、持久化、快照、导出均要覆盖 |
| 密度云、求解纹理、临时缩略采样、GPU handle | 可重建缓存；不保存机器路径、GL/Metal handle 或大数组缓存 |

拟新增 `WarperStateV2`（名称待定，尚不存在）：带 schema version、stable point/stroke IDs、mode、color_space、coordinate convention、axis settings、grid/stroke constraints、algorithm_version。有效网格可为 `height×width×components`，不能再假设方阵；双网格应独立具名。数值必须 finite，尺寸有上限，颜色域/HDR 政策显式声明。

当前网格snapshot含resolution、active_grid、grids、selected/pins索引、tool、falloff、auto_lock、lock_points/axis、range、color_space、axis_angle；HS Luma另存 `[ring, ray, value]` 列表。CW笔画含 `path/mode/parameters`，Pin含 `position/parameters`，选中对象仍用 `(kind, list_index)`。这些都是可JSON序列化的展示快照；stable IDs、跨目标保存/恢复与版本化领域模型仍未实现。正式接入不能把易变化的list index当持久身份。

模式切换应保留各模式独立参数。切换是“仅查看”还是“激活另一套处理”必须单独定义：查看 UI 不应丢失旧结果；正式激活需一次命令并更新有效模式。不允许只切 title 却继续把新画布的坐标上传到旧 HSV。

### 2.3 能力与错误返回

每个动作返回 `{enabled, reason, supported_modes, backend, revision}` 能力和明确的成功/拒绝结果。无素材、无活动 corrector、只读目标、后端不支持、过期 revision、非法数组、求解失败、资源不足分别诊断。未知 schema 必须保留并明确不可编辑/不可渲染，禁止悄悄降级为 identity 或 HSV。

## 3. 实施阶段总表

全部后端任务初始状态为“未开始”。本轮UI交互、旧HSV门控及UI-only回归修复不等于后端阶段完成。CW-B00–B03 为后续算法接入前置；每阶段只启用已过验收的能力。

| ID | 优先级 | 依赖 | 交付物与完成门槛 |
| --- | --- | --- | --- |
| CW-B00 | P0 | 无 | 实机行为矩阵、数值域、参考帧与已知差异清单 |
| CW-B01 | P0 | B00 | 版本化模型、请求/快照/capability 契约；旧工程无视觉回归 |
| CW-B02 | P0 | B01 | 权威命令适配、单手势 undo、切层回填与 revision 保护 |
| CW-B03 | P0 | B01/B02 | 明确坐标与颜色空间转换，legacy HSV 兼容 |
| CW-B04 | P1 | B03 | Hue–Saturation 非方阵、锁点/平滑、HSL/HSP |
| CW-B05 | P1 | B03/B04 | Chroma–Luma 双网格、Axis Angle、Auto Pin |
| CW-B06 | P1 | B03 | Chroma Warp 笔画、Pin 与范围/曝光求解 |
| CW-B07 | P1 | B02/B03 | 异步密度云、viewer picker、范围预览 |
| CW-B08 | P0 | B04–B07 按启用能力 | GL/Metal 渲染、缓存失效、资源释放与失败取消 |
| CW-B09 | P0 | B01/B02、相应算法阶段 | 保存迁移、复制、Gallery、关键帧策略与导出一致性 |
| CW-B10 | P0 交付门槛 | 所有已启用阶段 | UI、GPU、HDR、工程、性能与异常路径证据 |

### 3.1 阶段状态(随实施更新)

| ID | 状态 | 说明 |
| --- | --- | --- |
| CW-B00 | 媒体基线完成;实机复核待人工 | 素材 probe/manifest/锚点帧/连续区间已冻结(`docs/validation/color_warper_b00/`);行为矩阵按「实测/Topos 声明/未确认」分级;待复核清单(`behavior_matrix.md` §4)补齐前不写成规范 |
| CW-B01 | 完成 | `src/features/color/core/warper_state.py`(schema v2、稳定 ID、冻结数组、legacy_hsv_v1 桥、显式重采样、capability/请求契约);Layer/剪贴板集成(未知版本原文保留);41 项新测试 + 95 项回归全过 |
| CW-B02–B10 | 未开始 | — |

## 4. 每阶段具体步骤与验证

### CW-B00：冻结参考行为

步骤：

1. 在同一 Resolve Studio 20 build、相同面板尺寸与缩放下，分别记录三模式初始状态、所有菜单、完整分辨率列表、选中/禁用状态、工具 tooltip、数值最小/最大/步进及重置值。
2. 用 hue sweep、saturation ramp、luma ramp、灰阶、色卡及真实肤色素材逐项操作；记录每个 gesture 的前后参数、操作顺序、颜色变化与撤销次数。
3. 核实 HS 的 Hue/Sat/Luma 数值表达的是绝对值、偏移值或选点聚合；多选时混合值怎么显示；CL 两网格是顺序合成还是互补投影。
4. 核实 Auto Lock 1/2 Points 的拓扑邻域、画笔选择闭合规则、Pin 拖动政策、Pull/Push 范围与 falloff、Smooth 作用对象、reset selection 与 reset pins 的差异。
5. 为未公开算法定义可验证的 Topos 行为目标；区分“实测相符”“近似”“未确认”，不宣称私有算法逐位一致。

验证：每项有截图/操作记录、输入夹具与预期；至少覆盖单点、多点、空选择、混合 Pin、跨色相接缝、密度改变、模式往返与 undo。没有证据的候选列表/默认值不进入正式 schema。

### CW-B01：领域模型与迁移设计

步骤：

1. 检查 `layer.py`、`color_graph_snapshot.py`、`grade_clipboard.py` 的当前持久化/复制约定；新增版本化结构和校验，不直接把 UI 字典存入 layer。
2. 建立稳定 point/stroke ID；分辨率更改后使用明确重采样策略，不能依赖变化后的数组索引继续指向旧点。
3. 给旧 `warper_params` 标明 `legacy_hsv_v1` 算法；旧 NxNx2 数据按原坐标与低饱和 fade 解释。旧 None 保持 bypass。
4. 明确新旧字段共存与写回政策：读旧工程无损；新工程写入版本；旧读取器无法表达的能力明确诊断。未知字段原样保留。
5. 新模型深拷贝/冻结数组；禁止拖动中的 mutable ndarray 被渲染线程或历史快照共享引用。

验证：旧6/8/12方阵 round-trip、None、非方阵、双网格、空笔画、未知字段、未知版本、NaN/Inf、越界尺寸、损坏 JSON、维数/分量错误；读失败不得修改当前工程。旧 fixture 载入后渲染与升级前基线一致。

### CW-B02：命令与手势生命周期

步骤：

1. 在 app controller 增设适配层，解析 UI command → 当前 clip/corrector 或 standalone layer service；复用 `color_graph_service.py` 与当前 history 提交入口。
2. 区分仅选择/hover 与实质调色；选择不创建历史，网格、笔画、Pin约束、模式激活与参数更改进入模型。
3. begin 捕获目标和基线；update 做可取消的预览；commit 只提交最终值并形成一个 undo；cancel 恢复基线并清理预览。
4. 模式切换、切 clip/layer、关闭面板、undo、reset 时停止 UI 合流 timer，清 pending 样本；过期请求/结果不得覆盖新目标。
5. 数值拖动、输入框 Enter/失焦、工具按钮、viewer picker 汇入相同命令，程序化 hydrate 用状态锁/QSignalBlocker，不二次提交。
6. 确定旧 interaction signal 与新命令手势如何去重；检查 `on_interaction_started/ended`，避免既 push legacy history 又创建新图命令。

验证：一次拖动一个 undo；无移动点击零 undo；Escape/cancel 不留脏状态；拖动中切层、撤销、reset、关闭、renderer 重启，等待超过16ms仍无旧样本发布；失败命令不推进 revision，不污染自动保存。图、时间线和 standalone 三上下文分别测试。

### CW-B03：色彩空间、坐标与 HDR

步骤：

1. 冻结 hue 原点、方向、环绕、sat/chroma 半径、中心和外圈含义；绘制几何与算法坐标独立转换，禁止为了六角网格外观改变旧 hue 编码。
2. 为 HSV/HSL/HSP 规定 RGB 输入所处工作空间、亮度/感知权重、零色度政策与可逆条件；HSP 采用什么常数须在测试中固定。
3. 对负值、超过1的场景线性值、极高曝光、接近零的分母建立连续可解释规则；不得无理由把 HDR RGB clamp 到0–1。
4. 独立保留 legacy HSV 路径，使用 golden fixture 比较旧行为；新空间不靠修改 UI 标签假装实现。
5. 定义颜色管理前后位置、bypass/节点顺序、输入/输出变换，保证 viewer/导出相同。

验证：六主/次色、环绕两侧、灰阶与近灰、黑、负通道、RGB>1、极大/极小有限值；RGB↔空间 round-trip（预先声明误差阈值），identity 不改图，零色度无色相噪点，无 NaN/Inf。CPU reference、GLSL、MSL 对同一 fixture 比较。

### CW-B04：Hue–Saturation 网格与 Tools

步骤：

1. 支持 Hue/Saturation 两轴尺寸独立，Link 开启同步两轴；密度更改明确重采样而非静默清除调色，必要时作为单次可撤销命令。
2. 正确处理 hue 周期接缝与 sat 轴边界；把网格位置转换为稳定位移/约束，不把显示六角形直接当纹理 UV。
3. 接入多点移动、Pin/De-pin、列/环保护、falloff、Pull/Push、Auto Lock 1/2 Points；Pin 作为求解约束，不只改变点颜色。
4. 实现 Hue/Sat/Luma 数值、Smooth/Reset 的经 B00 确认语义；先明确 Luma 改动需要第三分量或独立数据，不能塞入旧两分量纹理。
5. 合并范围色带到选点/有效权重语义，确保空选择和全选的工具行为一致。

验证：6×6、8×12、24×24及Link往返；零位移identity；接缝两侧相同编辑连续；Pin在拖动/平滑/重采样后按约定固定；AutoLock邻域有精确断言；全部reset恢复identity；多选数值mixed-state、输入非法值、工具无选择时不改模型。

### CW-B05：Chroma–Luma 双网格

步骤：

1. 按 B00 结果定义 Grid1/Grid2 的投影、AxisAngle 单位与方向、chroma/luma 数值域和两网格合成顺序。
2. 每个网格独立尺寸、约束和ID；共享模型中的有效模式，不复用HS数组并把sat解释成luma。
3. 将 Auto Pin Columns/Rows/Single 与手动Pin统一成明确约束；区分“自动生成约束”与“用户Pin”，定义后续取消/重置政策。
4. 在色彩空间及HDR政策下求解并更新纹理/缓冲；非活动网格是否同时处理按规格执行。

验证：两网格分别单点/多点编辑、交叠编辑、角度0/边界/完整周期；灰阶亮度单调性、固定luma时chroma变化、固定chroma时luma变化；grid切换不丢数据；AutoPin与手动Pin冲突结果可预测；GL/MSL/导出一致。

### CW-B06：Chroma Warp 笔画求解

步骤：

1. 模型保存源/目标颜色约束、Normal/Point-to-Point、稳定stroke/Pin ID和Pin参数；屏幕笔画路径只用于交互，不以像素坐标作为最终颜色语义。
2. 定义 ChromaRange、TonalRangeLow/High、Pivot、Exposure 的权重函数、单位与合法域；默认值按实机记录，输入校验与错误显示分离。
3. 为 Normal 与 Point-to-Point 建立独立可测试求解过程；删除/选择/重新编辑笔画可回放且结果确定。
4. 求解工作走 worker/渲染队列，支持取消和generation；局部预览不能在QWidget paintEvent同步运行重求解。
5. 笔画相交、重复约束、矛盾Pins和病态数据要有稳定退化规则与诊断；不能抛弃约束却显示成功。

验证：单笔画、多个笔画、同源不同目标、交叉/闭合、重复/零长度、删除、Pin保护、极小range、极端曝光；相同状态重复求解一致；取消/失败恢复前一结果；一次完整笔画一个undo；负值/HDR不过早截断。

### CW-B07：真实密度云、viewer采样与Range

步骤：

1. 明确云图采样点：当前节点输入、输出或指定参考；记录source/frame/node/revision/colorspace标签。不得用随机云或截图示意代替真实采样。
2. 优先在已有GPU帧/图采样链中低分辨率统计，限制采样量与刷新频率；不新增UI同步CPU全帧readback或独立解码。
3. 以generation拒绝切帧/切节点/undo前启动的晚到结果；播放时允许节流，停止时补当前准确结果。
4. viewer picker把图像坐标经现有缩放、平移、裁剪、DPR和输入色彩空间映射为颜色；范围选择/Pin与网格定位复用该颜色坐标。
5. 区分无采样数据、黑图、全透明、无支持后端；空云应诚实为空，不沿用前一个clip的云。

验证：已知色块投影落点正确；黑白/灰阶不冒出彩色密度；viewer各种缩放/高DPI位置一致；播放/seek/跨clip/模式切换无串云；无素材、取消、close后晚到结果不触碰已销毁控件；记录CPU readback与刷新耗时。

### CW-B08：渲染集成、性能与资源

步骤：

1. 同步 legacy composition 与 color graph 路径的模式/尺寸/纹理声明，GLSL/MSL调用一致；不支持的backend由capability拒绝，不静默丢效果。
2. 纹理/SSBO/求解buffer只在既有渲染线程创建与更新；UI发不可变DTO；所有取消/失败/重启走既有deferred-garbage/release机制。
3. cache key包含target、算法version、空间、尺寸、完整约束revision和输入采样revision；选择/hover等纯UI状态不使图像cache失效。
4. 播放预览与最终导出复用算法；允许明确可测的预览分辨率策略，停止后补精确结果，不能永久留下拖动低质量图。
5. 记录4K播放基线和新增开销；要求UI不被同步求解阻塞。60FPS预算总计16.7ms，不能把整个预算都分配给Warper。

验证：重复创建/切模式/重置/关闭100轮后GPU资源计数稳定；模拟纹理创建失败、求解取消、renderer重启与节点删除，释放一次且无use-after-free；4K连续拖动/播放记录p50/p95耗时、dropped frames、内存/显存与UI响应。至少一台GL和一台Metal运行，缺失平台标记待验收。

### CW-B09：工程、复制、关键帧与导出

步骤：

1. 为layer、图snapshot、history、剪贴板和Gallery/预设的Warper域复用同一serializer，避免某一路只复制旧warper_params。
2. 保存新模式完整参数与未知字段；保存期间预览手势按统一提交政策处理，不能把半个笔画或可变数组写盘。
3. 定义网格/笔画拓扑变化的关键帧政策：可插值分量、离散mode/space/size与Pin切换分别处理；暂不支持则能力层明确拒绝，不宣称已支持。
4. LUT可表达性逐模式验证；若算法固定且仅依赖像素颜色，可按已声明域采样LUT；涉及动态素材统计或不能表达的能力必须拒绝或明确烘焙输入条件。
5. 视频导出与预览同一工作空间/算法version，跨片段、复制调色、版本切换和重开工程复原一致。

验证：新旧工程round-trip、Save As、崩溃恢复策略、跨clip复制/撤销、Gallery应用再撤销、版本切换；未支持schema不丢字段；静帧与视频预览/导出逐像素或规定误差比较；LUT导出成功时有色卡差值证据，失败时有明确原因且无半成品冒充成功。

### CW-B10：验收与证据归档

步骤：

1. 每个阶段新增有意义的纯算法/契约/命令/生命周期focused tests；不以重复UI内部字段的测试替代用户行为验证。
2. 在固定面板尺寸、字体、DPR、全局主题下截图对照三模式、各工具选中/禁用、数值混合状态、展开/收起；使用Topos背景/按钮/数值框的差异单列为预期。
3. 用CW-B00的同素材同操作复核视觉与交互，单独记录算法近似差异，不把UI像素近似写为色彩算法已完全一致。
4. 人工跑下方矩阵，归档输入fixture、环境、命令、通过/失败/跳过、GPU/Qt/Resolve版本与性能数据。

最终门槛：每个启用能力均有真实处理、可撤销/可保存/可导出证据；未实现项仍明确待接入；核心渲染变更须按项目规范独立Review并修复Critical/Suggestion后再验收。

## 5. 测试命令与人工验收表

### 5.1 已验证可运行的UI基线

2026-11-12 最终UI验证：以下组合 **22项全部通过（6.99s）**，包括原有8项和新增14项。覆盖模式隔离、分辨率、选择/Pin/拖动、Luma展示及手势隔离、分量平滑、数值手势、CW参数/取消、回填、浮窗、大画布，以及从其他模式执行全局reset_all清除旧HSV效果。截图、媒体probe及限制见 `docs/validation/color_warper_20261112/README.md`。此结果不代表任何后端阶段完成。

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/color/test_warper_resolve_ui.py \
  tests/color/test_color_mapping_panel.py \
  tests/color/test_fix_wd4g23_aud.py::TestAud25814WarperPanelSetParamsResetGuard \
  tests/shared/test_ef14_aud.py::TestAud38209Padding -q
```

环境基线：Python 3.12.14、PyQt6 6.11.0、Qt runtime 6.11.2、pytest-qt 4.5.0。

### 5.2 后续接入的现有回归入口

下列文件均已确认存在；本次文档工作**未执行这一扩展组**。每阶段按影响选择运行，不需要无理由重复全量测试。

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/color/test_composition_uniform_manifest.py \
  tests/color/test_metal_composition_mixer_warper.py \
  tests/color/test_color_graph_texture.py \
  tests/color/test_color_graph_backend_capabilities.py \
  tests/color/test_color_graph_cancel_resources.py -q

QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/color/test_history.py \
  tests/color/test_image_engine_history.py \
  tests/color/test_grade_clipboard.py \
  tests/color/test_color_graph_persistence.py \
  tests/color/test_graph_lut_export.py -q
```

后端新增测试文件名由实施阶段决定，不预先声称存在；上述UI测试文件已存在。建议按color contract、颜色空间reference、三种solver、command lifecycle、schema migration、GPU parity各自组织；GPU用例缺硬件应skip并说明原因，skip不算完成跨平台验收。

`tests/color/conftest.py` 会比较测试前后git status；测试输出用tmp_path，截图/日志归档在测试完成后复制，不在测试运行中改源码或生产cache。

### 5.3 人工矩阵（每项记录结果和证据）

| 场景 | 操作 | 通过标准 |
| --- | --- | --- |
| HS基础 | 选择/多选、拖动、Hue/Sat数值、Pin、Smooth、逐轴Reset | 云/网格/图像与数值同步；Pin有效；单手势单undo |
| HS密度/空间 | Link开关，非方阵，HSV/HSL/HSP往返 | 无静默清空；legacy工程画面不变；新空间真实处理 |
| CL | Grid1/2独立编辑、AxisAngle、AutoPin三方式 | 两网格状态保留、处理组合明确、重开一致 |
| Chroma Warp | Normal与Point-to-Point、添加/删除Pin、编辑全部Pin数值 | 图像与约束真实变化；删除可撤销；参数范围准确 |
| Viewer采样 | 缩放/平移/高DPI下取色；播放时连续取色 | 坐标/颜色正确，晚到结果不串clip |
| 空状态 | 无素材/无目标/空选择/全Pin | 工具能力诚实，未提交虚假历史，无崩溃 |
| 生命周期 | 拖动中切层、撤销、reset、关闭、取消、重启renderer | 无陈旧timer/worker覆盖；资源释放；状态可恢复 |
| 工程 | 保存重开、旧项目、新字段、跨clip复制/Gallery | 模式和算法版本保真；未知字段保留 |
| HDR | 负值、>1、低饱和、黑、极高曝光、宽色域 | 无非预期截断/NaN/Inf；identity和bypass稳定 |
| 输出 | GL/Metal、静帧/视频、LUT允许与拒绝 | 同算法同颜色管理；不支持时明确失败 |
| 性能 | 4K播放+最大网格/多笔画持续操作 | 记录p50/p95、帧率、UI响应和资源；性能门槛有实机证据 |

### 5.4 用户指定真实素材与逐阶段验证

真实视频统一优先使用项目内 `tests/test_videos`。以下三个路径已通过文件系统检查确认存在，本次文档同步没有解码、修改或重新编码这些原始素材：

| fixture路径 | 用途 | 核验要求 |
| --- | --- | --- |
| `tests/test_videos/2K_h264.mp4` | 常规压缩输入、播放/seek、切目标与取消、预览/导出冒烟 | 在B00记录实际codec、分辨率、帧率/timebase、时长、pix_fmt、color metadata及文件hash |
| `tests/test_videos/2K_prores422.mov` | 编辑中间格式输入、密集数值/网格操作、帧一致性与复制保存 | 同样读取实际metadata；不能只靠文件名推断颜色空间或位深 |
| `tests/test_videos/2K_DNxHR HQX 10bit.mov` | 高位深候选输入、细微色差、跨decoder/后端比较 | 实际10bit与色度抽样先由probe确认；文件名含空格，命令与fixture配置必须完整引用 |

这三份是仓库真实媒体回归基线，不自动覆盖HDR、负值、4K或所有颜色空间。HDR/负值/色相接缝/已知色块需额外程序生成的小型线性float夹具；4K性能需真实4K或明确标注来源的派生样本。派生图像、临时工程和导出只写tmp_path或验证输出目录，不覆盖原视频，不假称2K基线已经完成4K/60FPS验收。

通用取帧协议：B00读取真实时长后冻结每份素材的首帧、中段、尾部可解码帧及至少一段连续播放区间；记录帧索引、PTS、时间域、decoder和颜色管理配置。后续阶段复用同一manifest，避免把不同帧或不同变换的差异算作Warper误差。如媒体缺失、codec不支持、probe失败，要明确skip/失败原因；不能使用另一素材冒充原fixture通过。

| 阶段 | 真实素材的具体操作 | 验证证据 |
| --- | --- | --- |
| CW-B00 | 对三个fixture完成probe/manifest；在Topos与Resolve导入同一段、同一帧，录制三模式基准操作 | 素材hash、metadata、reference frame/PTS、面板截图、已确认/未确认语义表 |
| CW-B01 | 分别以三个fixture创建含legacy HSV及新模式数据的临时工程，保存/重开/迁移 | 素材引用保持、状态round-trip、legacy渲染基准不变、未知字段保留 |
| CW-B02 | 每个fixture做一次网格拖动和数值拖动；播放/seek中切素材、切层、undo、cancel/reset | 每手势历史次数、revision/目标身份、16ms窗口后无过期写入，三种输入均无串状态 |
| CW-B03 | 用三个fixture的固定帧分别运行identity/小位移/空间切换；另加生成色卡与HDR浮点夹具 | 输入/输出帧、工作空间、误差阈值、GL/MSL/CPU参考差值；不由媒体文件名推断HDR |
| CW-B04 | 三个fixture分别执行HS多点、Pin、独立分辨率、Link、Hue/Sat平滑、reset | 相同命令/参数的预览、undo/redo帧；压缩/中间格式均identity正确，接缝另用生成夹具 |
| CW-B05 | 三个fixture固定帧分别编辑Grid1/2、AxisAngle、AutoPin，并播放连续区间 | 双网格独立状态及合成输出、Pin保护、复开项目与静帧/播放一致 |
| CW-B06 | 三个fixture分别添加Normal/Point-to-Point笔画与多Pin，修改曝光/范围后取消、删除再undo | 约束回放、求解确定性、异步取消、失败恢复及对应真实图像差异 |
| CW-B07 | 每份素材播放、seek首/中/尾帧、跨素材快速切换和viewer取色 | 云图frame/node/revision标签与manifest一致，无旧云、无UI同步全帧readback；已知落点另用色卡 |
| CW-B08 | 三份素材连续播放+最大支持网格/多笔画；重复切换/取消/renderer重启 | 各codec的延迟/掉帧/资源统计；单列解码开销与Warper开销；4K另跑，不把2K结果升级描述 |
| CW-B09 | 分别保存重开、复制调色/Gallery，再导出每份素材的同一短区间与固定帧 | 项目状态、原素材引用不变、同PTS预览/导出色差；LUT可表达性单独判断 |
| CW-B10 | 对三份素材重放最终启用能力的相同操作清单；跑空目标/取消/失败和GL/Metal矩阵 | fixture逐项通过/失败/skip、截图、日志、耗时、backend与机器信息；缺平台/缺真实4K明确待验收 |

正式验收报告须同时区分：纯UI tests通过、三个真实媒体fixture通过、合成色彩科学夹具通过、实际GPU平台通过、4K性能通过；其中任意一类不能代替其他类别。

## 6. 后续实施顺序与关闭条件

推荐顺序：先完成B00–B03和旧HSV兼容，再接HS、双网格CL、ChromaWarp；密度云可在契约稳定后独立实现。每批仅启用已通过B08/B09检查的能力，不把全部UI按钮同时宣告支持。

关闭本计划前需逐项填写阶段状态、实现提交、测试命令、参考截图与已知差异；所有“待核实”有结论。任何只有UI局部响应而没有权威提交、图像变化、保存/undo/输出证据的能力，继续标记“未接后端”。
