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
  tests/ui/test_graph_expression_editor.py tests/app/test_composite_graph_open_context.py
```

真实素材目录为 `/Volumes/Project_1/开发项目/topos-color-multitask /topos-color01/tests/test_videos/`，`multitask` 后有一个空格。用 `2K_h264.mp4` 查拖放与范围、`2K_Prores4444_Alpha.mov` 查透明边及图层主链、`4K_H264.mov` 查高分辨率。其余机器可用样本副本。音频先探测流，再用明确 PCM 脉冲和视频闪白做对照；PSD 使用可分发的独立 fixture。大型缓存和样本输出写临时目录。

**结项条件。** 正常生产入口可用，旧工程可读，撤销重做覆盖，相关渲染、真机和故障测试通过。登记复跑材料和命令后更新对应状态。

## 6. 实施状态登记（2026-11-22，B11 批次）

本节登记各任务的提交号、测试范围与剩余限制；复跑命令同 §5（从仓库根目录，`QT_QPA_PLATFORM=offscreen TOPOS_HEADLESS_GL=1 .venv/bin/python -m pytest -q …`）。

| 任务 | 提交 | 覆盖 | 剩余限制 |
| --- | --- | --- | --- |
| B01 稳定图层角色 | 1f87bd23 | `Layer.transform_node_id` 稳定角色 + `layer_roles()` 契约；Blur 后置加效果/删除保护/快捷键/父级/复制/锁/撤销/序列化往返；旧链唯一候选回填、双 Transform 歧义诊断 | 查看器手柄（Viewer overlay）尚未接 `layer_transform_node`，仍按 root 渲染 |
| B02 原子接线 | 1f87bd23 后续 | `NodeGraph.apply_edit` 原因码事务（环/容量/类型/端口/过时 revision/重复命令 ID），失败零修改；`_restore` 精确恢复连接顺序；手势乐观 revision | 预设导入与脚本的写入口仍走 `CompositeEditService`，未全部迁到 `apply_edit` |
| B03 有理时间 | 同批 | `layer_time.py` 分数帧率（NTSC 恢复）、`source_frame_cfr`、slide/trim/slip/split/stretch 纯计划；裁边按帧率换算（25↔24、30↔29.97 精确）；共享读取节点 `shared_source_range` 诊断 | slip/split 的 UI 手势与 VFR 时间戳索引适配层未接；共享素材两层的独立读取节点自动拆分未做（当前靠诊断引导） |
| B04 合成标记 | 同批 | UUID 标记 CRUD 各一步撤销；同帧独立删除；schema v12 迁移规整；缩短合成保留越界标记 | 标尺 UI 投影与右键菜单尚未接 `composition_markers_changed` |
| B05 音频计划 | 同批 | `AudioPlan`/`LayerAudio`（mute>solo 优先）、`sample_boundary` 共享端点（含计划 NTSC 例）、波形分桶与缓存键、`Layer.audio` 往返 | AudioMixer/设备仲裁的真实播放接线与异步探测 UI 未接（纯契约层） |
| B06 导出混音 | 同批 | `comp_audio_mix`：精确 PCM 长度、脉冲落位、两路线性叠加、五态导出诊断 | `composite_render_output` 的 mux 接线与 ffprobe 对拍未做（需真实编码样本） |
| B07 PSD | 同批 | psd-tools 锁版本；清单/指纹/同名不猜名；直通 alpha 解码、`clip_psd_patch` 手算例、导入策略与支持矩阵；无依赖 PSD 字节夹具 | 16 位/PSB 用真实样本对拍未做；合成页一次提交建层的事务接线未接 |
| B08 图层样式 | 同批 | 版本化 `LayerStyleEffect`+溯源；`shadow_under` golden；未验证样式合并+诊断；halo 窗 | GLSL 效果与 Effect Catalog 注册未做（CPU 参考实现就位） |
| B09 外部引用 | 同批 | `CompRef`（pinned/follow）、DFS 环路径、done 复用、深度限、三因分离审计 | 工程读服务/资源解析/PrecompNode 的装载接线未接（纯契约层） |
| B10 通道/窗口 | 同批 | RGB/A 选择按预乘契约恢复（alpha0/负/HDR）；半开窗口空/负坐标、模糊/裁剪/变换/合并传播、ROI≠全幅缓存键 | 节点能力表声明与 tile 调度消费窗口未接；4K 性能基准未跑 |

**回归状态。** 本批次全量：`tests/composite` + 5 个 UI/接线/打开上下文文件共 7800+ 用例通过（含本批新增 100+）。`tests/edit/test_audio_waveform.py` 26 例通过（B05 夹具结构参考）。真机（中文/英文、双屏、窄窗）与真实素材（`/Volumes/Project_1/…/tests/test_videos/`、PSD 独立 fixture）验收留待下一批 UI 接线时一并执行。

**环境。** macOS 15.3（darwin 24.3.0 x64），Python 3.12.13，psd-tools 1.27.0，PyQt6 offscreen + ModernGL（TOPOS_HEADLESS_GL=1）。
