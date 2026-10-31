# L01 组件规格契约（spec schema）

> 依据 `docs/upgrade_plan_L_content_library_2026-10-24.md` §8 L01。
> 这是**拟定交付契约**：字段由现有 parser/registry 适配（Mac 集成者窄补丁），
> 不是新造万能插件引擎。机器可读载体是 `manifest.py` 的 `ComponentManifest`。

## 1. 字段组（七组，每组件必填）

| 字段组 | 字段 | 适配到宿主的机制（现有） |
|---|---|---|
| 身份 | pack_id、stable effect/node/transition ID、version、source/content hash、参数 schema hash、最低宿主契约、未知版本保留策略 | `plugin_identity.py`（PluginIdentity 8 态状态机，M07.1 已在生产）；`.tglsl` 的 `@version`；EffectSpec.plugin_* 字段 |
| 参数 | 类型/default/min/max/options、单位、neutral/bypass 语义、可动画性、输入验证（NaN/Inf/非法枚举） | Composite `ParamDef`（ptype 枚举一致）；`.tglsl @param` 四类为 ABI 子集 |
| 图像 | 输入/输出工作域/gamut/transfer、精度、alpha straight/premult、输出范围、采样边界、尺寸/像素比单位、bbox/ROI/padding | `.tglsl` 的 `@input_space/@output_space/@alpha/@precision`；节点 capability 的 roi_policy/tile_padding |
| 端口/时间 | 输入角色/可选 mask/缺输入行为、输出名/类型；time_domain/origin、有理 fps、帧/子帧采样、seed/资源依赖 | Port 声明（隐式可选）；转场 progress/对齐机制；**frame/seed 无宿主接口→time_domain=static** |
| 能力 | 每页面×预览/视频导出/LUT 烘出×后端×静态/动画 分别 supported/unsupported/untested | `@export_support` 白名单 fail-closed；CPU oracle 与生产 CPU 后端**分列** |
| 资源 | 纹理/map 来源、许可、hash、相对资源 ID、缺失诊断、归档规则 | M20 闭包未开始→外部资源项一律 untested/暂缓，程序资源先行 |
| 验证 | 独立 oracle、手算向量、实际 GPU 输出、基准、用户流程、已知限制、命令 | `tests/content_library_v1/`；GPU 证据 L02 起补 |

## 2. 状态与声明纪律

- 暂存区组件只能 `planned` / `component_ready`；`integrated+` 由集成者按 §10 表声明。
- 数学上有 neutral 设置的效果必须给 neutral 默认并配恒等测试；
  源生成/matte 输出类没有自然恒等时，**明确 bypass 语义**，不写无意义恒等。
- 所有可见参数在规定素材（`fixtures/`）上有真实像素影响（滑杆有效性测试）。
- 能力矩阵默认 `untested`；声明 `supported` 必须有证据引用（commit/命令/输出 hash）。

## 3. time_domain 冻结口径（L01 全库默认）

当前宿主 `.tglsl` ABI **无 frame/seed/time uniform**（引擎不注入时间；Glitch_Blocks/
Film_Grain 文件注释明示）。因此：

- 全部组件默认 `time_domain="static"`，效果时间恒 0，不从窗口重绘推帧；
- 默认 seed 为参数中的固定值，复制沿用；如提供每实例随机模式，只在创建时生成并持久化；
- 动态模式（F07/F08/F09 动画、C09 frame mode、X 系列逐帧噪声）**一律过"正式 frame/seed
  子门"后才声明**；该子门由 L02/L03 集成者按宿主接口落地，落地前组件只发静态模式；
- 时域项（拖尾/光流模糊/时域降噪）需要历史帧 provider，不在本库范围。

## 4. CPU oracle 约定（references/）

- 参考实现 float64 内部计算，接口 float32 进出（`references/oracle_conventions.md`）；
- scene-linear RGB、straight alpha；负值/HDR 透传，除定义允许的权重/mask 域外不 clamp；
- oracle **不得 import 宿主 GPU/渲染代码生成自己的预期**（禁自证）；
- 手算向量（小尺寸、可笔算复核）与 oracle 输出一并登记 hash。

## 5. 夹具（fixtures/）

生成器 `generate_fixtures.py` 纯算术（整数哈希 value noise，不用随机流），
跨 numpy 版本确定性；`fixture_manifest.json` 记录每个夹具的
name/shape/dtype/sha256（canonical float32 C-order .npy 字节）/用途/许可。
测试重新生成并比对 hash（防生成器漂移）。夹具集：

| 夹具 | 用途 |
|---|---|
| gray_ramp_linear 64×32 | 亮度映射类（F16/F17/F04…） |
| hdr_probes 8×1（-0.25/0/0.18/1/4.5/12 + 高饱和色） | HDR 数值覆盖（§9.1 共同验收门） |
| color_patches 8×8（肤色簇/原色/次色/18% 灰） | 肤色保护/分区/饱和类 |
| hdr_point_light 96×48（峰值 4.5/12 高斯点光） | halation/starburst/glow 类 |
| alpha_fringe_hair 64×64 RGBA straight（彩边+发丝） | alpha/边缘类（C17/C03、premult 验收） |
| uv_grid 64×64（R/G=uv，网格线+角标） | UV 重采样类（X09/X12-X17、C12/C13） |
| short_seq_8 8×32×16（确定性移动边缘） | 转场序列（X 全系，A/B 双源对） |
| paper_tex_64 / rank_field_64（程序生成，CC0） | 纸纹/排线、rank-field 转场 |

## 6. 与宿主格式的边界

- manifest JSON **不冒充**宿主预设格式（Color .topos/sidecar、Edit 效果/转场载荷、
  Composite nodes/connections JSON）；转换入口是 L05 的事，且只启用已过 L02/L03/L04 的能力。
- node_type 命名空间冻结为 `lib1_*`（如 `lib1_gradient_map`）；不覆盖现有 99 键。
- 转场注册 ID 冻结为 `lib1_<slug>`；用户配方 base 白名单扩展由 L03 集成者定稿。
