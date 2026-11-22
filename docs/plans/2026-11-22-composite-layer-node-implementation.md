# Topos 合成页图层与节点改造实施计划

更新于 2026-11-22。适用于 PyQt6、ModernGL、GLSL 版本的 Topos Studio。图层时间线负责素材、动画和合成顺序，节点图负责分支、效果和显式路由。两种编辑方式使用同一个工程状态，与预览、导出及撤销保持一致。

## 1. 开工和交接

1. 在仓库根目录阅读 [开发约束](../../.clinerules) 和 [合成架构](../architecture/composite.md)。用项目的启动方式打开 Studio 视频模式，进入「合成」。在工程副本中验收保存和迁移。
2. 每次先写一个能复现缺口的测试，再改任务指定的生产入口。示例中的新类型和方法名是设计草案，合入前按工程现有命名、类型和测试约定调整。
3. 先通过后端测试，再接功能界面。编辑走既有命令入口；渲染消费不可变快照。禁止在 UI 线程解码、混音或操作 GPU。部分失败时一起恢复图层、参数、关键帧、节点和工程登记。
4. 完成后登记提交号、测试命令、系统和驱动、真实样本、结果及剩余限制。各任务独立回归，下游依赖同一组通过的夹具。

### 已有基线与本轮 UI

| 范围 | 当前入口与人工检查 |
| --- | --- |
| 建合成 | 拖素材到空图层时间线或节点图，确认自动建合成、名称唯一，第一批素材已连到可预览的输出。 |
| 时间线 | 单行时间码、帧数及帧率；时间导航窗口、工作区边缘、分栏握柄；A/P/S/R/T 和 U/UU。视觉和交互范围限于合成时长。 |
| 连线 | 拖端点改线、Shift 复制分支、Cmd/Ctrl 点击中点插 Dot、拖节点插入指定支路。画布外松手取消；画布空白可主动断线。右键可选曲线、直线、直角，吸附与 Dot 走同一路径。 |
| 工作区导航 | 图层右键「在节点图中显示」，节点右键「在图层时间线中显示」。多个归属时选具体图层。图层时间菜单可跳入点、出点或设置工作区。双击空白在点击位置建自由节点，双击普通节点打开属性。节点库拖放创建自由节点，再拖节点标题到线上即可插入。 |
| 关键帧和表达式 | 时间条右键打开对应图层菜单，关键帧右键作用于指向的键。Alt/Option 拖向已占用的目标会保留原数据。曲线表达式栏的函数按钮、Ctrl/Cmd+空格提供补全。 |
| 底层设施 | LayerStack v2、受管 Merge A/B/Mask、Transform 重新接入、图层效果插入、关键帧剪贴、参数链接、内部预合成、时间重映射、共享预览与导出求值。下表针对剩余缺口。 |

### 后端任务表

| 编号 | 优先级 | 交付 | 依赖 | 最短验收 |
| --- | --- | --- | --- | --- |
| B01 | P0 | 稳定图层角色、内置变换契约 | 基线 | Blur 放在 Transform 后，主变换、效果、父级引用正确 |
| B02 | P0 | 统一受管接线、图层命令与图事务 | B01 | 面板、脚本、撤销重做结果一致，失败前后图与工程相等 |
| B03 | P0 | 有理时间模型、每层前景门控、图层时间编辑 | B01/B02 | 跨帧率、共享素材不同裁边，预览导出逐帧一致 |
| B04 | P1 | 可编辑合成标记与持久化 | B02 | 创建、移动、改名、删除、撤销、重开 |
| B05 | P1 | 音频元数据、图层 PCM 计划、波形和播放 | B02/B03 | 音频增益、片源延时、循环、静音均正确 |
| B06 | P1 | 音频导出与剪辑页 Comp Clip 混音 | B05 | 测量 A/V 时间，嵌套后同音轨只混一次 |
| B07 | P1 | PSD/PSB 透明合并解码、可重定位分层导入 | B01/B02 | 同名、半透明、负偏移、离线重连、一次撤销 |
| B08 | P2 | PSD 图层样式和保真降级 | B07 | 可编辑样式出图，未知样式需有来源和诊断 |
| B09 | P2 | 外部合成引用、版本选择和依赖审计 | B02/B03 | 可重定位，循环和失联可诊断，旧工程可开 |
| B10 | P2 | 效果通道选择及数据窗口 | B02/B03 | Alpha、HDR、偏移正确，ROI 与全幅一致 |
| B11 | 持续 | 迁移、真实素材、性能和故障验收 | 各任务 | 对应回归和平台验收通过 |

B04、B07 可以在 B02 后与 B03 并行。B05/B06 共用音频计划；B07/B08 共用导入描述。B09 对应旧审计 CP-049，B04 对应 CP-054，B05/B06 细化 CP-038，B07/B08 细化 CP-070，B10 承接 CP-046/047。跟踪、Deep、农场等已有独立引擎路线图的事项见 [专业合成审计](../composite_pro_parity_gap_plan_2026-10-13.md)。

## 2. 图层角色、接线及时间

### B01 稳定的内置图层变换

**路径。** [Layer](../../src/features/composite/core/layer_model.py)、[LayerStackController](../../src/features/composite/core/layer_stack_controller.py)、[图层溯源](../../src/features/composite/core/layer_provenance.py)、[KeyframesModel](../../src/features/composite/ui/keyframes_model.py)、[变换求值](../../src/features/composite/evaluators/transform_evaluator.py)。

1. 写反例 Constant → Transform → Blur → 本层 Merge A。现有部分代码用输出节点识别图层变换；链首变成 Blur 后，新增效果、快捷键或删除效果可能引用错误对象。对两层使用同一素材，验证编辑一层不会改变另一层。
2. 控制器提供统一接口，解析 source、layer transform、ordered effects、final image feed、managed merge。素材身份由 asset ID 管理，参数仍由既有 Transform 节点及其动画轨道驱动。
3. 二维素材和生成层统一在每层实例登记稳定 Transform 角色。保留自由 Transform 及摄像机、灯光、Scene3D 的原有契约。禁止给素材和生成器复制第二套同名值。
4. 旧工程沿本层明确的图像主链回溯，排除 Mask、其他图层和预合成边界。只有唯一候选才写稳定引用。保留旧节点 ID、动画、表达式和出图；多候选或断链生成可修复诊断。
5. 加效果、父级、变换快捷键、查看器手柄、重置、复制和删除逐项改用角色解析。主 Transform 移除时进入可识别的缺失状态，接回沿用事务认领。重置数值与清除动画作为独立编辑。
6. 字段版本化。先支持旧和新读入，再启用新写入；未识别的扩展字段继续透传。

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class LayerRoles:
    source: str | None
    transform: str | None
    effects: tuple[str, ...]      # 上游到下游
    foreground: tuple[str, str]  # node_id, output_port
    managed_merge: str | None

def choose_legacy_transform(
    main_chain: tuple[str, ...], transform_ids: frozenset[str]
) -> str | None:
    candidates = [node_id for node_id in main_chain if node_id in transform_ids]
    return candidates[0] if len(candidates) == 1 else None
```

**验收。** 扩展 [controller 测试](../../tests/composite/test_layer_stack_controller.py) 和 [Dock 测试](../../tests/ui/test_composite_keyframes_dock.py)。覆盖两个 Transform 的旧工程、Blur 后置、Transform 删除重接、父子、遮罩、锁、复制、撤销及重开。比对迁移前后节点 ID、动画和静态/动画帧图。

### B02 原子接线和图层命令

**路径。** [NodeGraph](../../src/features/composite/core/node_engine.py)、[控制器](../../src/features/composite/core/layer_stack_controller.py)、[编译器](../../src/features/composite/core/layer_stack_compiler.py)、[校验器](../../src/features/composite/core/layer_stack_validator.py)、[撤销栈](../../src/features/composite/core/undo_stack.py)、[页面现有接线入口](../../src/features/composite/ui/composite_page.py)。

1. 枚举节点画布、图层菜单、删除清除、预设导入、脚本、图层复制重排的写入口。复用现有边事务、控制器和 undo。工程恢复保留单独的旧数据诊断路径。
2. 基于 B01 构造候选。v2 A 属于本层前景，B 属于累积背景，Mask 受轨道遮罩和自由图策略约束。生成边及 Output 由编译器管理。释放节点前统计其他层与自由分支引用。
3. 统一预检和提交的容量、端口类型、有向环、锁、父级环、遮罩和引用校验。返回原因码驱动吸附和诊断。先接输出端的半条 Transform 链使用暂存意图，只有完整候选才进入图快照。
4. 用同一 revision 再校验并一次发布节点、边、元数据和关键帧。拒绝过时编辑或用命令 ID 去重。退出锁再通知视图。撤销和重做各应用一份精确差量，包括多输入端口的边顺序。
5. 删除、拆分、复制和多选批处理共用入口。注入序列化、保存和信号失败，验证不会留下孤儿节点或跨面板旧状态。

```python
# 接口草案，复用现有 GraphSnapshot、控制器及边事务。
def apply_layer_edit(command: LayerEditCommand) -> EditResult:
    with graph_write_transaction(expected_revision=command.revision) as tx:
        candidate = command.stage(tx.current_graph, tx.current_layer_stack)
        report = validate_edit(candidate, layer_lock_policy=tx.lock_policy)
        if not report.ok:
            return EditResult.rejected(report.issues)
        entry = tx.commit(candidate, undo_stack=undo_stack)
    publish_committed_revision(entry.revision)
    return EditResult.applied(entry.revision)
```

**验收。** 运行 [编译](../../tests/composite/test_layer_stack_compiler.py)、[校验](../../tests/composite/test_layer_stack_validator.py) 和 [画布接线](../../tests/ui/test_composite_node_wiring.py) 测试。模拟环、过时 revision、缺节点和中途异常，比较失败前后完整图字典、边顺序、关键帧和撤销记录。自由图和 v1/v2 路径均验证。

### B03 统一时间和图层编辑

**路径。** [时间重映射](../../src/features/composite/core/time_remap.py)、[现有范围编辑](../../src/features/composite/ui/timeline_commands.py)、[页面提交](../../src/features/composite/ui/composite_page.py)、[快照](../../src/features/composite/core/graph_snapshot.py)、[帧渲染](../../src/features/composite/render/composite_frame_renderer.py)。

1. 造 CFR 24、25、30000/1001 带逐帧编号的素材，以及 VFR、非零起始 PTS 样本。先确认两个已知问题：裁边把合成帧差当素材帧差，以及同一读取节点被多个图层引用时的取帧与门控冲突。
2. UI 和旧工程使用含尾合成帧，新纯函数内部使用半开区间。帧率保存精确分数。CFR 利用有理时间映射，VFR 查询实际来源时间戳索引，明确首尾取帧与缺帧策略。
3. 提取纯命令计划：滑整层移动本层动画；裁边移动可见边界并保持裁前的同一来源帧；slip 只换素材采样范围；拆分的切点归右，两个层实例共享素材 ID 各持时间和后续动画；伸缩、反向、定格、remap 共用同一时间契约。
4. v2 在每层 A 的求值边界门控，超范围旁通 B。遮罩源、独奏和可见性使用同样的层范围。对同一素材不同时间的两个层，先给独立读取节点，复用素材 ID 和实际解码缓存；需同帧多次求值时再引入带采样时间的求值身份。
5. 预览、无窗口导出及嵌套 Comp Clip 同时改接。时间线上的源入出点、时长、slip/stretch 编辑从同一服务取值。未知剪裁能力必须可诊断。

```python
from fractions import Fraction

def source_frame_cfr(
    comp_frame: int, *, in_frame: int, end_exclusive: int,
    source_origin: Fraction, comp_fps: Fraction, source_fps: Fraction,
    rate: Fraction = Fraction(1),
) -> int | None:
    # CFR 正向恒速示例。反向和 VFR 在各自适配层处理。
    if not in_frame <= comp_frame < end_exclusive:
        return None
    delta = Fraction(comp_frame - in_frame) * source_fps * rate / comp_fps
    position = source_origin + delta
    return position.numerator // position.denominator
```

**验收。** 扩展 [重映射与门控](../../tests/composite/test_time_remap_and_layer_gates.py)。对共享素材两层设不同起点及范围；逐帧验证首尾、反向、拆分连贯、遮罩父子、自由分支和重开。对照实际帧号、源帧 trace 及预览/导出 alpha。测试单帧合成、末帧与非整数映射。

## 3. 标记、音频和 PSD

### B04 持久化合成标记

**路径。** [NodeGraph 序列化](../../src/features/composite/core/node_engine.py)、[graph_migration](../../src/features/composite/core/graph_migration.py)、[Comp Clip 数据](../../src/features/edit/core/comp_clip_data.py)。

1. 明确标记由合成拥有。加稳定 UUID、整型帧、名称、可选时长和颜色。同帧可有多标记，使用 UUID 编辑。播放工作区、关键帧及外部只读标记分别建模。
2. 实现创建、改名、移动、伸缩、删除命令，拖动释放时只提交一次。缩短合成时隐藏或标出越界标记，保留位置以支持后续恢复。
3. 接现有保存、复制和迁移。标尺展示只读投影，操作走命令，导出可消费标记而不依赖 Qt。

**验收。** 同帧建立两标记，只删其中一个；改名和拖动各一步撤销。保存重开、缩短再恢复、移动工作区，逐项核对 ID、帧位置和标签。

### B05 音频元数据、PCM、波形与播放

**路径。** [媒体分类](../../src/features/composite/core/media_drop_payload.py)、[音频元数据](../../src/shared/media/metadata.py)、[剪辑 PCM](../../src/features/edit/core/clip_pcm.py)、[波形](../../src/features/edit/core/audio_waveform.py)、[AudioMixer](../../src/features/edit/core/audio_playback.py)、[设备仲裁](../../src/shared/playback/audio_arbiter.py)。

1. 异步探测流索引、时间基、声道、采样率和起始 PTS。覆盖纯音频、带声视频、无声视频和离线素材。纯音频层进混音计划，图像 Merge 不接音频。图片可见性和音频静音可单独控制。
2. 每层音频定义 asset ID、stream ID、gain dB、mute、solo、可选 gain 动画，时间直接引用 B03。规定 track 与 clip 静音、独奏和增益的优先级。先支持正向恒速，其余变速或保音调明确按能力启用。
3. 合成可复用的不可变音频计划。计算范围、源 PTS、重采样、声道、增益和叠加，调用共享 PCM 服务或将可复用逻辑提至共享层。处理音频流起始时间和编码器延迟。
4. 工作线程给波形做分桶 min/max 包络，缓存按素材修订、流、样本窗口和缩放级别区分。换工程、seek 或 loop 时刷新 epoch 并丢弃迟到的队列块。播放拿设备仲裁，停播、退出和取消均释放。

```python
from fractions import Fraction

def sample_boundary(frame: int, fps: Fraction, sample_rate: int) -> int:
    position = Fraction(frame * sample_rate) / fps
    return position.numerator // position.denominator

# 相邻区间共享样本端点，累计时长不随帧数舍入漂移。
start = sample_boundary(100, Fraction(30000, 1001), 48000)
stop = sample_boundary(101, Fraction(30000, 1001), 48000)
assert (start, stop) == (160160, 161761)
```

**验收。** 复用 [剪辑波形测试](../../tests/edit/test_audio_waveform.py) 的夹具结构。制作有已知样本位置的脉冲及同时间闪白画面，测 seek、工作区首尾、循环十次、静音/独奏、两路相加、不同采样率和设备切换。检查抖动、串音及无音轨 UI。

### B06 导出和剪辑页 Comp Clip 音频

**路径。** [合成输出](../../src/features/composite/render/composite_render_output.py)、[剪辑导出](../../src/features/edit/core/timeline_export.py)、[Comp Clip](../../src/features/edit/core/comp_clip_data.py)、[编码接口](../../src/shared/export/encoder.py)。

1. 在线预览和离线导出消费 B05 同一时间和混音计划。按最终选择的合成范围生成精确 PCM 长度，视频容器只 mux 一次，序列按明确契约提供独立 WAV 或能力提示。
2. 在导出快照中冻结音频依赖版本和范围。取消或失败清理音频临时文件和 worker，不发布未完成文件。区分静音、缺素材、无流和错误。
3. 通过剪辑页现有 PCM 来源 API 接入 Comp Clip，解析子合成时间范围和采样率。封装操作的原音轨归属要明确，避免独立音频与 Comp Clip 再各混一次。旧工程无音频字段维持原表现。

**验收。** 核对 PCM 长度、脉冲位置及 AAC/PCM mux 后解码时延。用 `ffprobe -show_streams -show_format -of json <文件>` 查流信息。剪辑嵌套与合成直接导出的音频做差，覆盖含剪裁、静音、增益和重开的实例。 [FFmpeg ffprobe 文档](https://ffmpeg.org/ffprobe.html)

### B07 PSD/PSB 透明读取与分层导入

**路径。** [Composite 图像读取](../../src/features/composite/core/image_loader.py)、[ImageInput](../../src/features/composite/nodes/image_input_node.py)、[共享 PSD 路径](../../src/shared/io/file_io.py)、[工程资源解析](../../src/shared/project/resource_resolver.py)、[控制器](../../src/features/composite/core/layer_stack_controller.py)。

1. 锁定验证过的 psd-tools 版本，为合成补独立 PSD 解码接线。CPU worker 返回带 alpha 的像素、画布和 ICC 元数据，预览与导出共用。记录 PSB、无合并图、16 位及颜色模式的支持矩阵。
2. 解析 manifest，含文档 asset ID、修订、PSD 源层 ID、结构路径、BBox、层序、可见性、不透明度及 blend、clip、group、effects 标记。无层 ID 用结构路径加有效指纹，再关联遇歧义需明确选择，禁止按显示名猜。
3. 首批仅拆可保真的普通像素层。生成图层、画布偏移、相对资源引用，保持原序和隐藏状态。预乘、填充、图层 opacity 分别定义，禁止重复乘 alpha。存在透传组、调整、智能对象或剪贴时，按影响范围合并并记录具体原因。
4. 一次提交素材登记、合成、层序、节点和输出。缓存按素材修订、层选择器、工作空间和精度区分，导入失败和取消后无临时资源遗留。实测合成 API 对 alpha、opacity、ICC、样式和位深的行为，再选读法。 [psd-tools 图层 API](https://psd-tools.readthedocs.io/en/latest/reference/psd_tools.api.layers.html) · [psd-tools 合成 API](https://psd-tools.readthedocs.io/en/latest/reference/psd_tools.composite.html)

```python
def clip_psd_patch(
    bbox: tuple[int, int, int, int], canvas: tuple[int, int],
) -> tuple[tuple[int, int], tuple[slice, slice]] | None:
    # 左上原点，右下排除。第一个结果是画布坐标，第二个是图层像素切片。
    left, top, right, bottom = bbox
    width, height = canvas
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(width, right), min(height, bottom)
    if x0 >= x1 or y0 >= y1:
        return None
    return (x0, y0), (slice(y0 - top, y1 - top), slice(x0 - left, x1 - left))
```

**验收。** 8×8 半透明、负偏移和完全在画布外的 fixture 手算像素。再验证同名、隐藏、混合、剪贴组、16 位、无合并图、重新定位、一次撤销和打开新工程副本。预览导出做透明边像素对照。

### B08 可编辑图层样式

**路径。** [效果目录](../../src/features/shared/effects/effect_catalog.py)、[节点能力](../../src/features/composite/core/node_capabilities.py)、[统一求值](../../src/features/composite/evaluators/graph_evaluator_mixin.py)。

1. 先为颜色叠加、投影和外发光各录一份 PSD 和原软件输出，验证后注册可编辑参数。再排描边、内阴影及渐变。输入含 alpha，按语义解析 fill、opacity、颜色、方向和半径。
2. 样式映射为版本化效果数据和明确顺序。投影算法从原始 alpha 出发，按半径扩窗、滤波、偏移并预乘着色，再与前景合成。tile 的 halo 包括偏移及滤波影响范围。预览与导出共享同一实现。
3. 保存原样式描述、转换版本和支持身份。无法可靠复现时保留 B07 的合并语义和诊断；调节已支持样式时原 PSD 文件无需改写。

下面是有投影的一个像素，用来写渲染 golden。输入颜色空间已经转换为合成工作空间，前景是预乘 RGBA，模糊和偏移后的投影 alpha 已由上游计算。

```python
def shadow_under(
    foreground: tuple[float, float, float, float],
    shadow_rgb: tuple[float, float, float],
    shadow_alpha: float,
) -> tuple[float, float, float, float]:
    r, g, b, a = foreground
    sr, sg, sb = shadow_rgb
    coverage = (1.0 - a) * shadow_alpha
    return r + sr * coverage, g + sg * coverage, b + sb * coverage, a + coverage

assert shadow_under((0.5, 0.0, 0.0, 0.5), (0.0, 0.5, 0.0), 0.5) == (
    0.5, 0.125, 0.0, 0.75
)
```

**验收。** 比较半透明边、图层外投影、尺度变化、两样式叠加、HDR 值、一键关闭和保存重开。验证没有重复应用源层 opacity。

## 4. 外部合成、通道及渲染

### B09 外部合成引用

**路径。** [PrecompNode](../../src/features/composite/nodes/precomp_node.py)、[工程读服务](../../src/shared/project/topos_project_read_service.py)、[资源解析](../../src/shared/project/resource_resolver.py)、[依赖审计](../../src/shared/project/dependency_audit.py)、[图渲染服务](../../src/features/composite/render/composite_graph_render_service.py)。

1. 定义目标 project ID、comp ID、版本指纹、相对引用和显式更新策略。内部预合成通过现有加载路径。外部包只读取图与资源数据。
2. 以 `(project_id, comp_id)` 为 DFS 键。active 集识别回边并给出路径，done 集复用已验证子图。生产设置解析深度、包体积及并发限制，长链用显式栈。
3. 预览与导出用同一目标图、目标输出、时间和格式参数。导出冻结引用 revision。缺文件、缺输出、插件不匹配分开报；接现有依赖审计、重新定位和归档流程。

```python
from typing import TypeAlias

CompKey: TypeAlias = tuple[str, str]

def find_cycle(
    start: CompKey, dependencies: dict[CompKey, tuple[CompKey, ...]],
) -> list[CompKey]:
    # 小型夹具用的 DFS，返回第一个回路的完整项目/合成身份。
    path: list[CompKey] = []
    active: set[CompKey] = set()
    done: set[CompKey] = set()

    def visit(key: CompKey) -> list[CompKey]:
        if key in active:
            return path[path.index(key):] + [key]
        if key in done:
            return []
        path.append(key)
        active.add(key)
        for child in dependencies.get(key, ()):
            cycle = visit(child)
            if cycle:
                return cycle
        active.remove(key)
        path.pop()
        done.add(key)
        return []

    return visit(start)
```

**验收。** A→B、A→B→C、A→B→A、两个不同时间引用同一 B、绑定特定 revision 后升级、断开文件再 relink、保存重开、取消导出。旧工程内部预合成经过同样的回归。

### B10 通道选择和数据窗口传播

**路径。** [能力表](../../src/features/composite/core/node_capabilities.py)、[通道求值](../../src/features/composite/evaluators/channel_evaluator.py)、[Merge](../../src/features/composite/evaluators/merge_evaluator.py)、[tile 调度](../../src/features/composite/core/tile_scheduler.py)、[缓存键](../../src/features/composite/render/cache_manager.py)。

1. 先为验证过的单输入图像效果试点 RGB/A 参数。明确 Mask、Mix 和合成顺序，未选通道从对应输入恢复，Alpha 改变时按预乘契约恢复颜色。3D、Deep、纯生成器等分别声明能力。
2. 为验证过的节点声明半开数据窗，支持空和负坐标。Blur 按 halo 扩窗，Crop 求交，Transform 用四角及采样半径保守外包，Merge 按模式传播。数据窗和合成画布分别建模。
3. 节点能力决定局部或全幅。缓存键包括窗口、帧、采样域和输入 revision。先做 ROI/全幅同帧差异，再测 4K 小图层、多分支和满屏，证明收益后决定默认策略。

**验收。** 比对 alpha=0、负值和 HDR、画布外窗口、模糊跨 tile、不同 Mask 和颜色通道。无法安全推导窗口的效果走全幅校验路径。记录首帧、稳态 p95、缓存命中和峰值显存。

## 5. B11 可复用的验收步骤

| 阶段 | 交接证据 |
| --- | --- |
| 纯逻辑 | 正常、边界和无效输入。对照旧工程打开、再保存、再重开三个阶段的 IDs、引用和参数。合成 schema 与层栈版本分别检查。 |
| Qt | QTest 驱动鼠标、快捷键和拖放，核对工程和触发信号。真机覆盖中文、英文、单/双屏 Dock、窄窗口。 |
| 渲染 | 相同帧、分辨率、色彩空间和输出精度下对照 Preview/Export。记录节点、源帧、误差及 GPU。 |
| 稳定性 | 重复撤销重做、故障注入、保存失败、取消、切工程、设备变化、资源离线重连。确认无孤儿节点、旧回调或重复音频。 |
| 性能 | 同一机器、驱动、素材、分辨率和冷热缓存，量出拖动延时、4K p95 帧时、首帧、峰值内存。 |

从根目录执行测试。Windows 使用当前虚拟环境的解释器，并用 PowerShell 设置同等环境变量。

```bash
QT_QPA_PLATFORM=offscreen TOPOS_HEADLESS_GL=1 .venv/bin/python -m pytest -q \
  tests/composite tests/ui/test_composite_node_wiring.py \
  tests/ui/test_composite_keyframes_dock.py tests/ui/test_keyframes_alt_drag_copy.py \
  tests/ui/test_graph_expression_editor.py tests/ui/test_graph_bugfix_data_safety.py \
  tests/ui/test_keyframes_panel_animation_e2e.py tests/ui/test_graph_cross_platform.py \
  tests/app/test_composite_graph_open_context.py tests/edit/test_composite_graph_data.py \
  tests/edit/test_comp_clip_promotion_lifecycle.py tests/edit/test_audio_waveform.py \
  tests/edit/test_clip_pcm.py tests/edit/test_audio_playback.py \
  tests/edit/test_nested_audio_flatten.py tests/edit/test_timeline_export.py \
  tests/ui/test_audio_arbiter_e6.py tests/ui/test_topos_audio.py \
  tests/media/test_ffmpeg_source_seek_math.py tests/project/test_project_module_preservation.py \
  tests/project/test_handoff_export.py tests/project/test_resource_resolver.py \
  tests/project/test_package_contract.py tests/project/test_dependency_audit.py
```

真实素材目录为 `/Volumes/Project_1/开发项目/topos-color-multitask /topos-color01/tests/test_videos/`，`multitask` 后有一个空格。用 `2K_h264.mp4` 查拖放与范围、`2K_Prores4444_Alpha.mov` 查透明边及图层主链、`4K_H264.mov` 查高分辨率。其余机器可用样本副本。音频先探测流，再用明确 PCM 脉冲和视频闪白做对照；PSD 使用可分发的独立 fixture。大型缓存和样本输出写临时目录。

**结项条件。** 正常生产入口可用，旧工程可读，撤销重做覆盖，相关渲染、真机和故障测试通过。登记复跑材料和命令后更新对应状态。

## 6. 实施状态登记（2026-11-22）

已提交的后端基线为 `1f87bd23` 至 `8acdbe5a`。本轮生产接线与回归在工作区。

| 任务 | 本轮结果 | 仍需验收或能力边界 |
| --- | --- | --- |
| B01 稳定图层角色 | 查看器手柄、快捷键、父级、效果和复制使用稳定主 Transform。旧链唯一候选可迁移，歧义有诊断。重新保存图层和层栈时透传扩展字段。 | 旧工程与媒体回归见 §5。 |
| B02 原子接线 | 输出端先接时仅在画布暂存，两端齐全后一次提交；脚本、预设和节点画布共用图事务。覆盖失败无部分修改、边序和撤销记录。 | 保存故障及重新打开纳入回归。 |
| B03 有理时间 | 默认媒体范围使用精确帧率。图层菜单支持滑移、伸缩和拆分。共享源的图层编辑可分离读取节点。VFR 后台建索引，预览与导出共用取帧和门控。 | 失效或重复 PTS 时走原取帧路径。 |
| B04 合成标记 | 标尺支持同帧 UUID 独立编辑、时长、拖动一次提交与撤销；工程合并和 `.topos` 重开已覆盖。 | 档案仍保存超出当前合成长度的标记。 |
| B05 音频计划 | 支持纯音频拖入、后台流探测、图层波形、独立增益/静音/独奏。预览接共享设备仲裁和检测默认设备变化。 | 合成音频支持正向恒速；反向、变速曲线、保音调和图层增益动画仍为能力限制。 |
| B06 导出混音 | 视频导出单音轨 mux，音频输出完整后才发布。图片序列有格式提示。剪辑直接放置和嵌套 Comp Clip 使用同一 PCM 来源，并保留已有对应音频片段的归属。 | 已对拍 PCM MOV、AAC MP4 和 Opus WebM。跨设备长期同步仍需覆盖。 |
| B07 PSD | PSD/PSB 后台探测，时间线分层一次提交，稳定选择器和相对路径支持迁移。用真实 16 位、透明及复杂图层样本比较预览和导出。 | 自由节点画布按合并图导入；未认证的模式按来源合并图和诊断处理。 |
| B08 图层样式 | 颜色叠加、投影、外发光的受支持子集已接效果目录与共享 GPU 求值。保留原始样式和转换版本。 | 复杂样式保留合并语义；无可信来源时报告原因。文字抗锯齿边仍有误差。 |
| B09 外部引用 | Precomp 可选择、重定位及固定或跟随版本。导出冻结依赖。自包含工程链可随交接包内嵌，迁移并删除原工程后仍可重开。 | 自动嵌入上限 256 MiB；松散素材或不可完整审计依赖显示在交接报告。外部包的宿主实时输入会给出诊断。 |
| B10 通道/数据窗口 | Blur 的 RGB/A 选择、GPU 预乘恢复和 ROI 窗口、halo、缓存身份已接入。8 瓦片与全幅绝对误差容限 0.002。 | ROI 保留按需启用。多种 4K 负载与实际显存峰值尚待测量。 |
| B11 平台验收 | 回归覆盖工程迁移、Qt、真实 VFR 像素、故障回滚、导出与交接。Cocoa 平台 76 项页面、图层面板和画布测试通过。Studio 主窗口完成 2K H.264 拖入、定位、真实像素读回和正常退出，合成线程正确退出。 | 当前设备只有单屏，双屏 Dock 和其他操作系统的真机验收继续开放。 |

**素材。** 之前的实拍回归覆盖 2K/4K H.264 和 ProRes 4444 Alpha。本轮追加带 2 秒首帧 PTS 的不等间隔 FFV1/MKV，裁剪、拆分和重开后，GPU 预览与导出六个像素取样一致。psd-tools 1.27.0 样本中的 16 位 PSD/PSB 和透明 PSB 通过相同解码链，复杂样式样本的预览与导出最大 RGBA 差为 0。与 Photoshop 保存的栅格比较，归一化 sRGB 平均误差为投影 0.00128、外发光 0.00292。外发光最高误差 0.0784 位于文字抗锯齿边缘。

**音频。** `ffprobe` 确认带样本脉冲的两帧 MOV 为 24fps H.264 和一条 48kHz PCM 音轨，均从 0 起始，时长均为 0.083333 秒，共 4,000 个音频样本。源裁切后的脉冲位于样本索引 200，剪辑嵌套混音逐样本一致。AAC MP4 的音画流同为 1 秒，Opus WebM 解码得到 48,000 个样本，两者的脉冲同样位于索引 200。素材音轨延后 125 ms 的样本落在预期位置 6,200。Steinberg UR22C DAW 的原生输出完成十次定位和重新起播，并释放设备。

**性能。** 4K checker → Blur → Output 对比 768×432 ROI（13 次），全幅与 ROI 的首帧为 574.38/367.71 ms，稳态 p95 为 3.69/6.20 ms，缓存峰值为 506.25/508.85 MiB。

**最终回归。** §5 命令通过 8,343 项，跳过 19 项，预期失败 1 项。Cocoa 复跑 `tests/app/test_composite_graph_open_context.py`、`tests/ui/test_composite_node_wiring.py` 和 `tests/ui/test_composite_keyframes_dock.py`，76 项通过。真实窗口冷启动的验收还修复了 GPU 示波器切换分析尺寸时的 OpenGL 1282，复跑出图及正常退出均通过。

**环境。** macOS 15.3（darwin 24.3.0 x64），AMD Radeon RX 6900 XT，OpenGL 4.1 ATI-6.1.13，Python 3.12.13，psd-tools 1.27.0，PyQt6 6.11.0。
