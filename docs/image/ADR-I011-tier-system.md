# ADR-I011：图片编码档位体系（--tier）

日期：2026-09-05
状态：已实施（门禁全绿）
关联：ADR-I010（VLC 默认）、`capability_manifest.json` tiers、SDK §3

## 背景

视频链已有六档体系（Topos 422 Proxy/LT/422/HQ/4444/4444 XQ，见
`docs/codec/capability_manifest.json` tiers 与 `topos_profiles.py`）。图片侧
此前只有质量锚点（ADR-I010 的 qp 28/52/58/61），无命名档位。用户定案：

- 命名用 **Low / Medium / High / Ultra**（用户友好；放弃镜像视频档名，
  规避与既有 image profile 名 Image HQ / Image XQ 的撞名——spec §9.1
  "不得复用 codec 名称掩盖像素/质量语义差异"红线因此不触）；
- **GBR 4:4:4 10-bit 不设档**（产品决策：需更高保真直接上 12-bit，
  信息保留更多；Image HQ profile 保持可读可写，只是不入 tier 菜单）；
- 两个家族：4:2:2 10-bit（四档）与 GBR 4:4:4 12-bit（两档）。

## 档位表（manifest tiers 为真相源；CLI `kTierPresets` 为执行面）

| id | 标签 | 质量 | 格式 | qp | 锚点体积* | 保真度锚点 |
|---|---|---|---|---|---|---|
| 422-low | Topos 422 Low | 低 | 4:2:2 10-bit | 63 | 2.33 MB/帧 | 预览级（8-bit 口径 ~13% 像素 ±1~2） |
| 422-medium | Topos 422 Medium | 中 | 4:2:2 10-bit | 61 | 2.69 MB/帧 | PSNR≈62 dB |
| 422-high | Topos 422 High | 高 | 4:2:2 10-bit | 58 | 3.06 MB/帧 | PSNR≈68 dB（无 --tier 时 CLI 默认） |
| 422-ultra | Topos 422 Ultra | 超高 | 4:2:2 10-bit | 52 | 4.02 MB/帧 | 8-bit 源舍回逐位无损 |
| 444-high | Topos 444 High | 高 | GBR 4:4:4 12-bit | 55 | 6.07 MB/帧 | PSNR≈79.5 dB（12-bit 满幅）≡ 422-high 相对质量 |
| 444-ultra | Topos 444 Ultra | 超高 | GBR 4:4:4 12-bit | 22 | 12.75 MB/帧 | 数学无损（Q=1） |

\* 1080p25 实拍素材（H.264 8-bit 源）帧 0 实测，2026-09-05，V2 VLC+AQ/RDO。
12-bit 锚点用 8-bit 源 <<4 上采内容标定；真 12-bit 素材绝对体积会更高。
"低/中/高/超高"为**家族内**相对质量，跨家族不可直接比较（444 无色度子采样）。

## 设计决定

1. **tier ≠ profile**：tier 是质量/体积预设（执行面 = 固定 qp），profile 是
   像素格式×位深语义（IDSC.image_profile）。两个命名空间并存的边界写入
   manifest `$comment` 与本 ADR。
2. **显式参数覆盖档位**：`--tier` 先填充 pixel-format/bit-depth/qp/
   image-profile，命令行显式同名参数仍覆盖（`--tier 422-low --qp 40`
   有效）；未知档位 rc=2 并列出候选。
3. **444-ultra 锚数学无损**：真 12-bit 素材没有幻影位，"超高"必须等于
   无损才有意义；444-high 锚在与 422-high 等效的相对质量（PSNR 满幅换算
   +12.04 dB）。
4. **alpha 与档位正交**：A16 无损直通在所有档位行为一致；"4444 命名暗示
   alpha"的预期由文档澄清（本体系已弃用 4444 命名，进一步降低混淆）。

## 与视频档位的体积差距（如实声明，常见疑问）

同内核下图片档体积显著高于视频档目标（如 422 Proxy 目标 0.61 bpp ≈
0.16 MB/帧 vs 图片 Low 2.33 MB ≈ 9.0 bpp）。原因分层：

1. **基线不同**：H.264 源（用户素材 0.43 bpp）是长 GOP 跨帧编码，不能
   逐帧独立解码；图片档必须单帧自包含（随机访问 + 元数据 + 原子写）。
2. **码率闭环缺失（图片侧）**：视频档走 `tc_frame_encode_sized` 目标
   bpp 的逐帧 qp 搜索；图片档是固定 qp 开环。`--tier` 交付锚点而非
   码率保证。
3. **内核高压缩段地板**：qp63 量化上限下，1080p 实拍内容实测 ~9 bpp
   地板。视频侧文档同样记录 Proxy 档"qp63 地板超支"（`topos_profiles.py`
   P2 注记）——这是共享内核的已知短板，后续工程方向：帧内方向预测、
   上下文自适应算术编码、qp 值域扩展（ADR-I010 未来事项）。

## 实现

- CLI：`toos encode --tier <id>`（`kTierPresets` 表 + 预扫填充 + 显式
  覆盖）；完成行报告 `tier <id>`。
- manifest：`tiers` 对象（id → label/quality/profile/pf/bd/qp/锚点），
  pytest 交叉校验 CLI 与 manifest 一致。
- 测试（test_toos_tools.py TestCliTiers，5 项）：六档参数映射与解码
  sidecar 一致、422 家族体积有序、两 Ultra 档位精确性、显式覆盖、未知
  档位拒绝。
- 门禁：ctest debug/ASan/UBSan 57/57 ×3、pytest image 67、check_exports
  216≥36。

## 修订（同日）：low/medium 锚点下探——Standard 量化矩阵

用户复核后指出：修订前最低档（422-low 2.33 MB）与 PS 导出的 PNG 8-bit
（≈2.0 MB）同尺寸，梯子下半段失去意义。复盘发现编码器自带的 **Standard
量化矩阵**（`qmatrix_id=1`，spec 附录 A.3，JPEG 式频率加权，高频步长比
flat 粗最多 23×）此前从未在图片链启用——视频链侧亦默认 flat。

修订内容：

- 422-low → **qm1 qp61**：1.38 MB/帧（PSNR 50.8 dB），帧25 交叉验证
  1.57 MB / 50.6 dB——明确低于 PNG；
- 422-medium → **qm1 qp58**：1.84 MB/帧（54.8 dB），帧25 2.08 MB；
- high/ultra/444 两档维持 flat（保真优先，锚点不变）；
- CLI 新增 `--qmatrix 0|1` 显式覆盖；完成行报告 `qm %u`；usage/manifest
  同步 qmatrix 列；pytest 交叉校验含 qmatrix 一致性。

标准矩阵的代价是高频细节软化（感知加权的设计意图），故只用于低/中档；
高保真档保持 flat。修订后梯子：1.38 / 1.84 / 3.06 / 4.02 MB——两档明确
低于 PNG，两档保真优先。

## 修订二（同日）：以 422-high ≈2 MB 重定锚——全阶梯相应缩档

用户复核修订一结果后反馈：档位体积仍普遍偏大，要求**以 422-High 为锚、
体积控制在 ≈2 MB**，其余档位相应缩小。重新实测（帧 0 CLI 文件体积 /
帧 25 交叉验证，V2 VLC+AQ/RDO，同素材 PNG 8-bit ≈2.04 MB）：

| id | 修订一 | 修订二 | 帧 25 | PSNR（10-bit 满幅） |
|---|---|---|---|---|
| 422-low | qm1 qp61 · 1.38 MB | **qm1 qp63 · 1.32 MB** | 1.38 MB | ≈48.5 dB |
| 422-medium | qm1 qp58 · 1.84 MB | **qm1 qp61 · 1.53 MB** | 1.67 MB | ≈50.7 dB |
| 422-high | qm0 qp58 · 3.06 MB | **qm1 qp58 · 1.97 MB** | 2.16 MB | ≈54.9 dB |
| 422-ultra | qm0 qp52 · 4.02 MB | 不变（4.05 MB） | 4.27 MB | 8-bit 舍回逐位无损 |
| 444-high | qm0 qp55 · 6.07 MB | **qm0 qp58 · 5.10 MB** | 5.42 MB | ≈74.7 dB@12bit ≡ 62.7@10bit |
| 444-ultra | qm0 qp22 · 12.75 MB | 不变 | — | 数学无损 |

设计注记：

- **三个体积档收敛到 Standard 矩阵单轴**（qp 63/61/58）：档位区分回归
  纯粹的 qp 质量轴，矩阵差异（flat vs Standard）留给保真语义——flat =
  不做频率加权。
- **保真锚不随体积阶梯移动**：422-ultra 钉在 8-bit 舍回逐位无损、
  444-ultra 钉在数学无损；体积阶梯压得再低也不动这两档。
- **444-high 质量仍高一档是刻意的**：GBR 全带宽 + flat 矩阵是保真路径，
  不引入 Standard 高频平滑；其缩档受 8-bit 上转换标定素材的幻影位地板
  限制（qp≥59 后体积不再下降，packet ≈4.9 MB），真 12-bit 素材实际体积
  会随 qp 继续下降。
- **无 `--tier` 默认保持 ADR-I010**（qm0 qp58，≈3.2 MB，视觉无损）：
  重定锚后默认质量位于 high 与 ultra 之间、不等价任何档位（usage 已
  如实标注）；是否把默认对齐 high 档属产品决策，未在本修订内擅自变更。

修订后梯子：**1.32 / 1.53 / 1.97 / 4.05 MB**——PNG 锚点档为 422-high。
门禁：pytest tests/media 全绿、ctest debug 57/57。

## 修订三（同日）：ultra 去无损锚——产品语义定案『很好画质 + 相对小体积』

用户定案产品语义：本编码的意义在于**很好的画质 + 相对小体积**；真无损
对目标场景没有意义（追求无损应直接用无损格式）。据此两档 ultra 不再
作为无损锚钉死，随体积阶梯相应缩档：

| id | 修订二 | 修订三 | 帧 25 | 质量 |
|---|---|---|---|---|
| 422-ultra | qm0 qp52 · 4.05 MB（8-bit 舍回逐位无损） | **qm0 qp58 · 3.13 MB** | 3.37 MB | PSNR≈68.6~69.8 dB（视觉无损级） |
| 444-ultra | qm0 qp22 · 12.75 MB（数学无损） | **qm0 qp55 · 5.79 MB** | — | PSNR≈79.5 dB@12bit（视觉无损级） |

要点：

- **真无损退出档位菜单但保留入口**：`--qp 28` 仍是 10-bit 数学无损
  （不入 `--tier` 菜单），无损容器（PNG/EXR/TIFF）承接归档需求。
- **422-ultra 与 high 同 qp（58）仅矩阵不同**（flat vs Standard）——
  梯子语义收敛为：同 qp 下 flat = 更高保真；qp 63/61/58（Standard）
  = 体积轴。ultra 3.13 MB ≈ high 的 1.6 倍，质量 +13.7 dB。
- **默认 ≡ 422-ultra**：无 `--tier` 默认（ADR-I010 的 qm0 qp58 + V2
  VLC）与 `--tier 422-ultra` 输出逐字节一致——修订二遗留的"默认悬在
  档位之外"问题随之消解，裸命令即 Ultra 档。新增逐字节回归测试钉死。
- 测试改向：原"ultra 逐位无损"断言废弃（语义已移除），改为
  **ultra > high 画质序断言**（同输入 PSNR 严格更高，两家族各一）+
  默认 ≡ 422-ultra 逐字节回归。

修订后梯子：**1.32 / 1.53 / 1.97 / 3.13 MB（4:2:2 10-bit）；
5.10 / 5.79 MB（GBR 4:4:4 12-bit）**。门禁：pytest image 68/68、
ctest debug 57/57。

## 修订四（同日）：锚点素材修正 + low/medium 压缩空间结论 + 代理工作流

用户追问：422-low（代理场景）与 422-medium 能否进一步压缩体积？调查
结论分三层：

### 1. 锚点素材修正（重要）

复核标定输入时发现：修订一~三使用的 `raw10/` 帧实为 **YUV 4:4:4 布局**
（12.4 MB/帧），却被按 4:2:2 读取——色度平面实际是 U 平面的上下两半。
全部历史测量内部自洽（编码→解码往返、阶梯排序、PSNR 均有效），但
"色度"统计比真 4:2:2（经 2:1 降采样、更平滑）偏贵，**旧锚点偏大**。
改用真 4:2:2 帧（Y 直采 + 色度 2:1 水平 box）重测：

| id | 旧锚点 | 真 4:2:2（帧0/帧25） | PSNR（不变） |
|---|---|---|---|
| 422-low | 1.32 / 1.38 MB | **1.11 / 1.21 MB** | ≈49 dB |
| 422-medium | 1.53 / 1.67 MB | **1.29 / 1.46 MB** | ≈51 dB |
| 422-high | 1.97 / 2.16 MB | **1.71 / 1.94 MB** | ≈55 dB |
| 422-ultra | 3.13 / 3.37 MB | **2.86 / 3.17 MB** | ≈68~69 dB |
| 444-high / ultra | 5.10 / 5.79 MB | 不变（GBR 标定输入无此问题） | — |

"用户觉得还偏大"的一部分直觉由此解释：真实体积本就比旧锚点低 10~16%。

### 2. 内核地板确认（low/medium 无可再压的合法杠杆）

- **修订四时 qp 轴已顶格**：qp≤63 是头域硬上限（6-bit），当时 low = qm1
  qp63 已是 Standard 矩阵的量化最粗点；修订五在不扩展 qp 的前提下改用
  422 Low Compact（qm3），继续收缩少量中高频码量。
- **无 8-bit 编码**：YUV 链 v1 仅 bit_depth=10（`topos_codec.h`），无
  "降 8-bit 缩体积"选项。
- **无 4:2:0 / chroma qp 偏移**：pixel_format 仅 4:2:2 / 4:4:4，配置
  无逐平面 qp。
- **slice_rows / 熵模式 / AQ/RDO 实测非体积杠杆**：qp63 下 slice_rows
  16→128 仅 −0.6%。

结论：**1.11 MB/帧就是现行内核在 1080p 全分辨率下的体积地板**。要再
下探只能动内核：qp 值域扩展（>63，最廉价）、帧内方向预测 + 上下文自
适应算术编码（实质修复，视频档 0.16 MB/帧级别的必经之路，ADR-I010
未来事项）。

### 3. 代理场景的正确答案：半分辨率工作流

对"low 主要用于代理"的语义，正确的减体积手段是**降分辨率而非压质量**：
960×540 + `--tier 422-low` 实测 **0.39 / 0.40 MB/帧**（约为全分辨率
low 的 30%，PSNR≈47 dB@540p，代理观感良好）——每字节换回的观感远好于
在全分辨率上继续压质量。已写入 SDK §3 代理推荐流；tier 保持纯质量
预设语义，分辨率由输入决定。

门禁：pytest image 68/68、ctest debug 57/57。

## 修订五（2026-09-07）：422 Low 轻度压缩矩阵

在真 4:2:2 锚点上，Low（qm1 qp63）为 1.117 MB，Medium（qm1 qp61）为
1.365 MB。为让 Low 和 Medium 的体积梯度更清楚，同时保留现有 qp=63 上限，
图片层新增 `qmatrix_id=3`（422 Low Compact），Low 改为 qm3 qp63；Medium
和 High 保持 qm1 qp61/58。新矩阵只提高中高频步长，DC/低频和解码路径不变。

同一帧实测 Low 为 1.081 MB，PSNR 46.49 dB；原 Low 为 1.117 MB、46.93 dB，
体积减少 3.2%，与 Medium 的差距从 0.248 MB 增到 0.284 MB。完整矩阵、
格式约束和验证记录见 `ADR-I013-422-low-compact-matrix.md`。

## 修订六（2026-09-12）：444 家族 12-bit → 10-bit（ProRes 4444 同位深口径）

触发：三方对照实测（`toos_vs_jpg_png` / `toos444_vs_jpg_png` / `prores_4444_vs_422hq`）
显示旧 444 预设（GBR 12-bit + qm2 Compact）在 422 原生素材上「又贵又不升质」
——qm2 紧凑矩阵 + 12-bit 容器税叠加；而 ProRes 4444（实为 10-bit）以
+28~35% 码率买到 +2.4~3.4dB。修订内容：

- `444-high` = GBR **10-bit**（Image HQ）qm1 qp63：1080p 实测 0.76/1.76 MB
  （prores422HQ/DNxHR HQX），PSNR 50.4~52.8 dB——比 422-ultra 更小的 444 入门档。
- `444-ultra` = GBR **10-bit**（Image HQ）qm0 qp58：1080p 实测 2.68/4.10 MB，
  PSNR 58.6~59.9 dB——**高于 422-ultra（54.8~56.2 dB）约 +4dB**（4:4:4 去掉
  色度二次采样的实测收益），RGB 原生图形/文字内容上对 422 有结构性优势。
- GBR 12-bit（Image XQ）保留为 `--bit-depth 12` 显式档，移出预设；
  qm2 矩阵保留（仅 12-bit 档可用）。

选型语义更新：8-bit/10-bit 422 素材 → 422 家族；RGB 原生或需全色度
分辨率 → 444-ultra；位精确归档 → `--qp 28` 或 PNG。capability_manifest
与 CLI 档位表已同步，pytest TestCliTiers 交叉校验绿。

实测：4K 444-ultra（新）60.1/58.3 dB @ 11.1/13.8 MB（vs 422-ultra
56.2/54.8 dB @ 4.7/6.8 MB）；444-high 52.2/51.8 dB @ 3.0/5.7 MB。
门禁：pytest test_toos_tools 16/16。
