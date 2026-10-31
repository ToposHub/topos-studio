# 浮点 .toos 应用面与 RAW（TRAW）产品化——优化计划表

> 状态：**实施中（2026-09-21 起）**——完成台账见 §13。
>
> ## 13. 实施台账（随批次滚动更新）
>
> | 批次 | 状态 | 提交 | 内容 |
> |---|---|---|---|
> | M0 | ✅ 完成 | bbc33b2c | E1/E2 白皮书命名规则（中英）+ E3 注册表 TOPOS_IMAGE_TIERS 22 档 + manifest float 补档 + E5/E6 README 修正 |
> | M1 | ✅ 完成 | 674ceb8a | F1–F7 浮点导入/预览全链 + G2 媒体池缩略图；真实素材冒烟逐位一致 |
> | M2 | ✅ 完成 | 1d957f8b | F8–F12 导出 UI/TMET/i18n + G1 数字码别名 + E10；DNxHR 帧双档冒烟 |
> | M3 | ✅ 完成 | 01d3c1bd | R1 图片 RAW 编码链（CLI 逐位一致）+ R3 UI 露出 + H1 sRGB 默认（golden 双轨）+ H2 ICCP + E8/E11 |
> | M4 | ✅ 完成 | 2cf1c158 + （本批） | 首批（2cf1c158）：R5 容器 tier 8 + R6 CLI TRAW MOV + R12 manifest + raw tier 声明 + E7/E9 规范章节。收尾（本批）：R2 直出车道 CFA 透传（fail-closed：同位深同尺寸 RAW→RAW，任何像素级处理拒绝）+ R7 编码链（全 I 强制 + CFA 平面直入 + V7-R2/V1 载体域 + 比率锚 crf 驱动）+ R8 导出 UI（topos_raw codec 全接线 + 位深×比率 12 档下拉 + manifest ui_codec_ids + i18n）+ R9 读端（topos_source pf=3 探测/解码/几何 + CfaGpuDebayer 渲染分支复用）+ E1 白皮书 §4.6 RAW 档章节（中英） |
> | M4.5（合并） | ✅ 完成 | 07b2dfc0 | bugfix（M0–M4 首批）并入 dev：六文件冲突语义合并（v1.9 双变更/manifest/netproxy+raw 九档/预算+ICCP 字段/白皮书 LP 行+重标定数值），原生 ctest 89/89 + golden 冻结保持 |
> | M5 | 🔶 大半 | 328b93b1 + a8a6ccd2 + 54648a68 + （本批） | **a 批**（328b93b1）：E4 命名闭环回归（registry↔image manifest↔CLI 预设表↔TMET）+ G5 缓存代际钉扎 + G8 海报帧缩放解码 + G9 half LUT + G11 浮点缩放测试。**b 批**（a8a6ccd2）：G3 topos_frame_to_rgb 修色（GBR 直通/PQ·HLG 显示变换）+ W4 CPU debayer（cfa_cpu_debayer，GPU 链回退）+ W5 CFA 缩略图 + G4 GBR 影片代理。**c 批**（54648a68）：W1 静默错标修复（bd16 补名 + 未知组合拒绝）+ W6 i18n 清剿（deliver/export/video 域 183 键双向；全应用余 1576 项积压留档）+ G7 代理链告警+去拷贝。**d 批**（本批）：H3 Adobe RGB（ICCP 载体闭环测试 + spec §6 近似标注注记）+ G13 缓存读回 target_size。**e 批**（6a0ab82b）：G6 HDR 缓存 HALF .toos 化（f16 逐位精确 ≈21% raw）+ G10 单帧 .toos 导出 UI（媒体池右键）。**f 批**（58d389b4）：G12 代理档参数化（位深/444/QP，默认零漂移）+ H5 CMYK 软转换 ICCP 放行（native+Python 双侧，89/89 冻结保持）。**g 批**（本批）：W2 色彩权威消费接线（detect_icc_profile 认 .toos ICCP chunk——色彩页显示链自动接管嵌入 ICC）+ H4 灰度支持评估落档（docs/image/H4-grayscale-support-evaluation.md，结论：暂不立项，YUV 中性面为零成本过渡）。**余项**：W8（TRAW 验收遗留：DNG 锚点复核需样张、V7-B FULL 门需画质语料、严格比率码控 CB 属新选项——三项均独立质量任务，建议随画质线批次安排）；W3 EXR 对照（linear 口径已随 F5 统一，EXR 导入侧归零样验证待排）；W7 备忘无需实施 |
> | M6 | 🔶 仅余 RC5（随 Stage-2） | 851293ad + 068a5acd + bfc550c6 + 0bc9c634 | **RC1**（851293ad）：TRWM 全链——.toos chunk（native 限额注册 + topos_trwm.py 40B 载荷 CRC 校验）+ MOV 'trwm' 原子（tc_mux_set_raw_meta/tc_movie_read_raw_meta 新函数对，零 ABI 扰动，faststart 保留）+ ToposVideoEncoder raw_meta_payload + ToposMediaSource 读侧暴露 + R2 直出透传。**RC7**（068a5acd）：transfer code 20 词表统一（COLOR_TRANSFER_MAP/复合 shader u_transfer=5/evaluator 枚举/帧提供方显式分支）。**RC8**（068a5acd）：black level 进 debayer 双链（GPU 双模板 + CPU + RawParams 字段，默认 0 零漂移）；flat field 维持评估未立项。**RC4 核心**：预览回退源 TRWM as-shot 基线（clip 无 RAW 参数时导入即得文件 WB/EI/黑位初值）。**RC2**（bfc550c6）：相机色域矩阵预设库 camera_gamut_matrices——8 家（AWG3/S-Gamut3/V-Gamut/DWG/RED WG/Canon CG/F-Gamut/BMD Gen5）矩阵由**官方发布原色**（ARRI/Sony/Panasonic/BMD/RED/Canon/Fujifilm 白皮书，全部经公开索引页核验：官方 PDF/ACESCentral/fmtconv/farg crate/公开 Colab 多源交叉）+ NPM + Bradford 推导；验收"对照参考实现抽查"以双锚点测试锁定（709 推导 vs ITU 规范 <1e-6；V-Gamut 推导 vs Panasonic Rev.1.0 §4.2 官方矩阵 ~6e-7）；TRWM sensor_matrix_id 1..8 双向映射，未知相机显式 None 不静默猜。**RC3**（0bc9c634）：CfaGpuDebayer 双模板 gamut_matrix mat3（identity 默认零漂移）+ gamma_mode（linear/2.2/sRGB，display_ootf 兼容折算）+ CPU debayer 同式（einsum 行向量方向修正）+ RawParams color_space/gamma 字段（顺带补 RC8 black_level 持久化缺口）+ RAW 面板色彩空间/伽马下拉（Treat-As 非破坏语义，i18n 双语）+ 预览双链路接线（用户显式 > TRWM id > identity+显式日志）。**RC6**（0bc9c634）：CFA 交付出口——timeline_export CFA 分支由显式拒绝升级为 debayer→IDT→工作空间 BGR（TRWM 基线；高精度 16-bit），与 R2 直出构成双路径；RAW 域 transfer（traw-log0）不再透传到再编码输出（回退 SDR）。**余项**：RC5（工作色空间基座）——working space 基座按计划注记与 composite Stage-2 合流（color_space.py Stage-2 注释：working-space config/ODT config/工程持久化），属跨计划设计决策，待 Stage-2 立项合并评审 |
> | 复查 R1 | ✅ 完成 | （本批） | 2026-09-21 全计划复查（5 路并行审计）：**功能修复**——① RAW 面板连续编辑互相回退（section.parameters 构建期快照 → 改为从 clip 模型重建）；② 导出/CPU 回退只吃 TRWM 不吃用户 RAW 参数（新增 `resolve_debayer_params` 三链同源：用户 RawParams > TRWM > 单位默认，GPU 预览/CPU 回退/交付导出同点取参；导出链 layer→clip.raw 穿透）；③ deliver_controller codec_map 漏 topos_raw（Quick Export 视频 RAW 断线）+ codec_profile 透传；④ Quick Export 浮点 .toos 双重编码（composite 线性 readback 被 sRGB 编码后落 linear 标签——`toos_export_keeps_linear` 契约助手）+ HALF+alpha 写侧 fail-closed；⑤ G1 导出色标：image 资产（.toos 静图/序列）不再被 is_video 过滤、'gbr' matrix 折算 bt709（此前编码器码表 ValueError）；⑥ debayer 奇数几何（GPU linear16 模板 ceil、CPU 按声明宽高截取、预览帧尺寸取元数据）；⑦ ColorConverter 补 TRAW_LOG0 解码（RC7 词表闭环）+ numeric 20 防御映射；⑧ CFA 缩略图接线（W4 落地后 topos_frame_to_rgb 不再回退 None，TRAW MOV 有海报帧）+ CFA 编码码值域校验 + TRWM 损坏告警/保留域/有限性收紧 + G5 缓存 tobytes 去除 + GBR 代理 HDR 告警 + gamma i18n 键（preview_2_2）/media_pool 6 死键修复 + ootf 死复选框移除 + black_level 面板范围 16-bit 放开。**文档修复**——图片 SDK §3 档位表换注册表 22 档权威口径（444 修订六 10-bit qm1/qm0）+ 两处 "Lossless(3) reserved" 矛盾 + manifest $comment 修订六勘误 + container_spec 升 v1.9（tier 域 0..8）+ 位流规范 §3 pf=3/bd16 行 + 视频 SDK TOPOS_MOVIE_CAPS/v1.8 域标签 + 编码仓 README RAW 行/音频措辞 + 视频白皮书命名章 N-4~N-7 补全 + 图片白皮书 38 符号 + risk register 撞号重编（R-I18/19）+ ADR-I001 D1 superseded + **ADR-C058 立档**（视频 RAW 产品档）+ 根 README RAW 档措辞。**仍开放**（如实降级台账原述）：W2 消费半链（检测就绪、调色页显示变换未接——.toos 调色页加载器缺失，随 M5 后续批次）；G13 缓存 target_size 读回参数已备、唯一调用点未接；E3 残留第二档位名表（_CODEC_CATALOGUE 标签未钉测试）；调色页 .toos 打开（file_io 无 .toos 分支）与 .toos TRWM chunk 读写（RC1 图片侧仅限额注册）——三项均登记为后续任务 |
>
> 验证基线（2026-09-21，复查 R1 批后）：焦点 16+16+3+12+135+31+25 全绿；原生 ctest 89/89
>（本批仅动注释/错误串）；golden 14 组逐字节 OK；Python media+deliver+export
> 3467 passed（既有环境失败 5 项与本计划无关：VT bridge ×3、
> zero_copy session_defaults、bench 素材名 2K_topos_proxy/hq 缺失）。
> 建立日期：2026-09-21
> 来源：用户提议（2026-09-21）——① 应用内浮点 .toos 导入/预览/导出与输出规格选择；
> ② 视频/图片 RAW 产品化命名；③ 各处薄弱的预览/导入/导出功能收尾。
> 同日第二轮拍板：RAW 命名定案 `Topos RAW 12-bit 4:1`（N:1 形式）；视频容器 MOV、
> 图片后缀 `.toos`；白皮书补详细命名规则方便用户识别；全项目输出规格选项保持一致
> （→ §2.1 命名规则总纲、E 组任务）。
> 关联已落地工作：ADR-C056（LP 产品档）、ADR-C057（浮点四档阶梯）、
> `docs/codec/topos_traw_format_plan_2026-09-12.md`（TRAW 批 0~5 已全部完成）。

---

## 1. 结论摘要（三条产品线现状）

| 产品线 | 后端/CLI | 应用内导入+预览 | 应用内导出 | 产品命名 |
|---|---|---|---|---|
| 浮点 .toos（HALF，image_profile 4） | ✅ 四档 CLI 预设（float-low/medium/high/ultra，ADR-C057） | ❌ **open 即被拒**（白名单缺 (2,16)） | ❌ 无任何 UI 入口（`gbrph16le` 只有编码器认识） | ✅ `toos Ultra 16bit float` 等 |
| 图片 RAW（TRAW，image_profile 3） | ✅ 全链完成（批 0~5，2026-09-13）；CLI 12 档 `raw12-*`/`raw16-*` | ✅ 已可用（导入 + GPU debayer + WB/EI/OOTF 面板） | ❌ 无导出入口（时间线 CFA 轨直出显式拒绝） | ✅ 已冻结 `Topos RAW 12-bit 4:1`（manifest traw_tiers） |
| 视频 RAW（MOV 载 TRAW） | ⚠️ 仅底座：bitstream profile 7 + `movie_config` 放行；encoder CLI 硬编码 profile3/10bit | ❌ 无读路径（MOV 载 CFA 无产品读端） | ❌ 无 tier/无 UI | ⚠️ 规格名已有，产品档位未立项 |

关键事实（证据）：

- 浮点导入门闸：`src/shared/media/topos_image_source.py:45-51`（`_PIXEL_FORMAT_NO_ALPHA` 缺 `(2,16)`）、`:124-136`（拒绝逻辑）；序列导入同断（`topos_image_sequence.py:247-249`）。
- `decode_float()` 是唯一浮点出口，src 内零调用（`src/shared/codec/topos_image_binding.py:695-711`）。
- 图片导出无 .toos：Quick Export 只有 JPEG/PNG/EXR/TIFF/WebP（`src/shared/export/quick_export_dialog.py:334-341`）；Deliver 图像序列无档位联动（`video_export_settings_panel.py:147,196-204,1930-1933`）。
- TRAW 应用内不可导出：编码器输入域无 CFA（`src/shared/export/topos_encoder.py:125-127`）；时间线 CFA 轨直出拒绝（`src/features/edit/core/timeline_export.py:9924-9931`）；`native_profile==7 → image_profile 3` 分支存在但不可达（`topos_encoder.py:1452-1457`）。
- 视频线无 raw：tier 0..7 无 raw（`include/topos_codec.h:778-788`）；encoder CLI 硬编码（`src/cli/encoder_cli.c:158-159,178-179`）；manifest tiers 无 raw。
- **白皮书滞后两处（E1/E2 来源）**：视频白皮书仍写"六个档位"、LP 档未入（`native/topos_codec/Topos_Codec_白皮书.md:70`，ADR-C056 产品化时漏更）；图片白皮书 §5 仍只列六档整数，浮点四档与 RAW 12 档均未入（`native/topos_codec/Topos_Image_白皮书.md:52-67`，ADR-C057 漏更）。白皮书位置：`native/topos_codec/Topos_Codec_白皮书.md`（+ 英文版 `Topos_Codec_Whitepaper.md`）、`Topos_Image_白皮书.md`。
- **README 滞后多处（E5/E6 来源）**：编码仓 `native/topos_codec/README.md` 视频档位速览仍为六档、无 LP（:23-40），图片档表无浮点/RAW（:54-61），且 444 High/Ultra 位深标注与 CLI 帮助矛盾（README 标 12-bit vs `image_cli.c:332-333`/kTierPresets 标 10-bit，需以实表核对修正），状态段仍写"六档 capability schema"（:73-74）；根 `README.md` HALF 段仍用**已废弃档名** `hf-lossless/hf-2/4/8/16`（:423，ADR-C057 改名 float-* 时漏更，照此敲 CLI 会报错），§5.8 图片叙述仍是 HQ/XQ 旧口径且混杂 profile 类名与 tier 体系（:306、:422），TRAW 段（:426）未引用命名规则，全文无 LP 档。
- **2026-09-21 全项目复查（第三轮）新增发现**：① 文档/规范面——视频 `SDK.md`、图片 `SDK.md`、位流规范（V7-R3 载体零章节、TRAW 的 minor 5/6 与 bd16/pf=3 未入 v1 spec）、ABI 符号清单、`toos_tools.py`、stage_status/risk_register、ffmpeg 打包说明等约 14 处滞后（→ E7–E11）；② 管线面——.toos 色彩标签导入/导出双断链、素材面板 .toos 无缩略图、GBR 影片缩略图颜色静默错误且无代理、节点缓存 payload 未钉 V 代际等 13 项（→ G 组），其中色彩标签断链与 .toos 缩略图为 **P0**。
- 与 `docs/project_full_audit_optimization_plan_2026-09-20.md`（应用/CI/架构域全项目审计）**互补不重叠**：本表专注编码产品面（格式/档位/媒体管线），应用域整改以那份台账为准。
- **2026-09-21 RAW 色彩链调研（I 组来源）**：TRAW 压缩/容器/回放面已完成，但**色彩元数据面零承接**——文件内开发元数据（WB/EI/传感器色矩阵/black level 四项）实现数为 0（IDSC reserved 强制为 0 不可用，无 TRAW chunk，MOV ADR-C021 无 TRAW 原子），WB/EI 只存工程侧 RawParams（重导入即丢、无 as-shot 基线）；debayer 输出传感器原生 RGB **无任何色域矩阵**而文件却标 primaries=1(BT.709)（语义错位）；无 IDT/目标色彩空间概念；OOTF 是临时 gamma2.2 且烘进轨道纹理；**CFA 轨道无任何交付出口**（CPU 导出显式拒绝）；相位序硬编码 RGGB。对照：Color 页照片 RAW 面板已有 16 色彩空间 + 18 伽马选择 UI 与 Bradford 矩阵（`raw_panel.py:99-110`、`shaders.py:492-623`），但与 TRAW 管线完全不通（→ §10 I 组）。
- **2026-09-21 色彩空间域调研（H 组来源）**：.toos 可落盘色彩域 = primaries {1=bt709, 6=smpte170m, 9=bt2020, 12=P3-DCI} × transfer {1=bt709, 8=linear, 16=PQ, 18=HLG, 20=TRAW_LOG0}——**无 sRGB EOTF 码（CICP 13）**，全部写路径默认 1/1（BT.709：primaries 数值≈sRGB 但 EOTF 非 sRGB 趾部）；AdobeRGB 无 H.273 码只能走 ICCP（读链/authority 就绪，**应用写入为零**——导入→.toos 的 ICC profile 当前丢失，`topos_encoder.py:1492` 唯一 extra_chunks 生产调用只写 TMET）；灰度 IDSC channel_model=2 占位但不可达（无 pf 映射、channel_count≥3、无 1 平面载荷模型）；CMYK 零支持且 ICCP 校验显式拒非 'RGB ' profile、CFA 4 平面基础设施因半分辨率几何+Bayer 语义+transfer 冻结对不可复用（→ §9 H 组）。

---

## 2. RAW 命名拍板（2026-09-21 已定案）

用户提案：视频 `topos raw 12bit 1/2 … 1/16`、`16bit 1/2 … 1/16`；图片 `toos raw …` 同构；并问
图片/视频是否统一、扩展名是否都用 TRAW、图片 RAW 是否直接用 toos。

逐点评估：

| 议题 | 用户提案 | 现状/冻结条款 | 建议 |
|---|---|---|---|
| 档位矩阵 | 12bit × {1/2,1/4,1/6,1/8,1/12,1/16} + 16bit 同构（12 档） | CLI/manifest 已实现**完全相同**的 12 档：`raw12-2/4/6/8/12/16`（qp 45/59/64/66/68/70，默认 4:1）+ `raw16-*`（qp 51/72/76/78/80/82，默认 4:1） | ✅ **矩阵无需改动**，直接采用 |
| 比率书写 | `1/2` 分数形式 | 冻结为 `N:1`（`topos_traw_format_plan_2026-09-12.md:176-181`：位深必带 `-bit`、比率必带 `:1`）；CLI id 已是 `raw12-4` 形式 | 建议**保留 `N:1`**：BRAW/REDCODE 行业母语；`1/2` 易误读为"半分辨率"；"16-bit 16:1" 同框零歧义是冻结条款原论证。若坚持 1/N，属纯显示层改动，需回写 plan §3.3 + 两处 manifest label（→ 决策点 D1） |
| 图片/视频一致性 | 统一规格命名 | 本质同一编码（共享 bitstream profile 7「TRAW」、同一 qp 锚点、同一内核） | ✅ **核心规格词两线统一**：`RAW <bd>-bit <N>:1` 一字不差；仅家族前缀按既有惯例区分（视频 `Topos RAW 12-bit 4:1`，与 `Topos 422 LP` 同族；图片 `toos RAW 12bit 4:1`，与 `toos Ultra 16bit float` 同族小写） |
| 扩展名 | 是否都用 TRAW 后缀 | 图片 = `.toos`（image_profile 3 自描述）；视频 = `.mov`（tpcC profile 7 自描述） | **不新增 `.traw` 扩展名**。TRAW 定位为**共享格式/档位技术名**（bitstream profile 名 + 文档词汇 + UI 副行标识），不是扩展名。理由：`.toos` 是统一图片容器，浮点/RAW 都只是其中 profile（EXR half 仍是 `.exr` 的先例）；ProRes RAW 装在 `.mov` 里是视频侧先例；单开扩展名会分裂工厂路由（按扩展名分发）、文件对话框、缩略图插件与规范，零信息增益 |
| "图片 RAW 直接用 toos" | — | 正是现状 | ✅ 维持：`.toos` + IDSC.image_profile=3 |

**统一命名总表（建议终案）**：

| 线 | 规格名（manifest/文档/CLI 帮助） | 应用内/TMET 显示名 | 载体 |
|---|---|---|---|
| 视频 | `Topos RAW 12-bit 2:1` … `Topos RAW 16-bit 16:1`（12 档） | 同左（codec 下拉家族） | `.mov`（tpcC profile 7） |
| 图片 | `Topos RAW 12-bit 2:1` … `Topos RAW 16-bit 16:1`（同 12 档，同一张表） | `toos RAW 12bit 4:1`（小写家族前缀，位深写法与 float 家族对齐） | `.toos`（image_profile 3） |

### 2.1 命名规则总纲（已拍板；将写入两份白皮书，全产品线统一）

| 规则 | 内容 | 示例 |
|---|---|---|
| N-1 家族前缀 | 视频线 `Topos`（大写，MOV 容器）；图片线 `toos`（小写，`.toos` 文件） | `Topos 422 HQ` / `toos High 12bit` |
| N-2 视频中间片档 | `Topos 422 <后缀>` / `Topos 4444 <后缀>`：Proxy / LT /（默认档无后缀）/ HQ / 4444 / 4444 XQ / LP | `Topos 422 LT`、`Topos 4444 XQ`、`Topos 422 LP` |
| N-3 RAW 档（规格名） | `Topos RAW <bd>-bit <N>:1`：位深必带 `-bit`、比率必带 `:1`、N ∈ {2,4,6,8,12,16}；模式 ≡ 位深（12-bit = log 制作档、16-bit = linear 归档档，Log/Linear 不进档位名）；视频/图片共用同一张 12 档表与 qp 锚点 | `Topos RAW 12-bit 4:1` |
| N-4 图片整数档 | `toos <Quality> <bd>bit`，Quality ∈ {Low, Medium, High, Ultra} | `toos High 12bit` |
| N-5 图片浮点档 | `toos <Quality> <bd>bit float`（HALF 域；未来 32-bit 档仅换位深数字，规则不变） | `toos Ultra 16bit float` |
| N-6 图片 RAW 档（文件内显示名/TMET） | `toos RAW <bd>bit <N>:1`——图片家族小写、位深无连字符，与 N-4/N-5 同风格；规格文档/白皮书层一律用 N-3 带连字符形式 | `toos RAW 12bit 4:1` |
| N-7 单一注册表 | 全部输出规格选择面（视频导出面板 / 图像序列 / Quick Export / CLI `--tier` / probe 显示 / TMET label / tpcD tier label）由同一份档位注册表派生，任何入口看到的档位名一致 | — |
| N-8 档位名不承载的词汇 | Log/Linear、无损、实验性等定位词只进描述文案与文档，不进档位名（TRAW plan §3.3 既有冻结） | — |

### 2.2 拍板记录（2026-09-21，两轮确认后关闭）

- **D1 比率书写 → `N:1`**：定案 `Topos RAW 12-bit 4:1`（与冻结条款、CLI id `raw12-4`、BRAW/REDCODE 行业惯例一致；`1/N` 分数形式不采用）。
- **容器/扩展名 → 视频 MOV、图片 `.toos`**：不新增 `.traw`；TRAW 仅为共享格式/档位技术名（bitstream profile 7 + 文档词汇 + UI 副行标识）。
- **D2 图片 TMET 位深写法 → 无连字符**：`toos RAW 12bit 4:1`（图片家族风格，随 N-6 固化进白皮书）。
- **D3 视频 RAW tier 结构 → 单 tier + 参数化**：`TC_TIER_RAW=8` + 位深 × 比率二级选择（qp 表驱动；LP 单 tier 先例），不开 12 个 tier id。
- **D4 profile 4 内部名「Image HF」定调 → 建议保留协议名、展示层统一 Float**：`Image HF` 是 spec/manifest/头文件里的 profile 枚举名（协议标识，改名波及面大且无信息增益）；展示层（导入显示名已用 `Float 16bit`、CLI 展示串 `image_cli.c:150` 仍写 "Image HF (half float)"）统一用 Float 词汇，spec 注记两词对应关系；随 E11 实施确认。
- **D5 CMYK 路线 → 建议软转换先行（ICCP + LittleCMS，存储保持 RGB）**：H.273 无 CMYK 码、ICCP 校验当前显式拒非 RGB profile；原生 4 通道存储需新格式代际（CFA 的 4 平面基础设施因半分辨率几何、Bayer 语义、transfer 冻结对、读端强制 debayer 四重硬绑定**不可复用**），现阶段不立项；打印工作流经"入口 CMYK→RGB、出口 RGB→CMYK 软转换 + CMYK ICC 嵌入"满足（H5）。
- **D6 TRAW primaries 语义 → 恒 1 占位、色彩权威转移 TRWM 元数据**：pf=3 文件的 CICP primaries 保持 1（占位），真实色彩语义由开发元数据块的 sensor matrix（camera native）承载——H.273 无 camera-native 枚举码，不在共享域私造值污染；debayer 输出即 camera-native RGB，IDT 矩阵从 TRWM 取（spec §6/§9.1 注记，随 RC1/RC2 落地）。

---

## 3. 任务表 A：浮点 .toos 应用内导入/预览（P0）

| ID | 任务 | 落点 | 验收 | 规模 |
|---|---|---|---|---|
| F1 | 白名单补 `(2,16)→'gbrph16le'`；同时消除 `_pixel_format_name` 未知组合静默回退 `yuv422p10le` 的陷阱（改为显式报错） | `topos_image_source.py:45-63,124-136` | HALF 文件 open 通过；未知组合抛 `MediaOpenError` 而非错标 | S |
| F2 | `read_frame` 接 `decode_float()`：sample_kind==HALF → float16 线性值平面；`frame.py` PIXEL_FORMATS 注册 `gbrph16le`（3 平面 float16，GBR 序）；PlaneInfo dtype/位深语义（无满刻度契约） | `topos_image_source.py:185-242`、`frame.py:102-216`、`topos_image_binding.py:695-711` | 浮点 .toos 读出 float16 平面，值域含负值/>1 高光；整数路径不受影响 | M |
| F3 | 序列源同通（首帧探测走 F1/F2） | `topos_image_sequence.py:247-249` | 浮点序列（EXR 序列对等工作流）可导入 | S |
| F4 | GPU 上传 half 路径：float16 平面 → half-float 纹理直传（**不做整数归一化**）；渲染分类器认 `gbrph16le` → rgb_planar half 变体 | `src/shared/video/yuv_gpu_upload.py:689-743,1371-1386`、`edit_gpu_preview_renderer.py:379-449` | 浮点帧 GPU 预览数值正确（对照 EXR half 导入） | M |
| F5 | 线性 display transform：时间线 IDT 映射补 `linear`（不再默认按 bt709）；调色页对 float/linear 静图不再 sRGB 直通 bypass | `timeline_clip_frame_provider.py:1225-1310`、`image_engine.py:2827-2840` | 线性内容显示不再偏暗；与 EXR linear 导入行为一致 | M |
| F6 | 次级路径：CPU 回退合成器认 `gbrph16le`（half→float→显示变换）；文件浏览器缩略图 `_planes_to_rgb` 按 sample_kind 分支 | `timeline_preview_renderer.py:5366-5489`、`external/file_explorer/plugins/topos_image/topos_image_media.py:40-72` | CPU 回退/缩略图不炸、不黑图 | M |
| F7 | 测试：source 层浮点导入（(2,16) 通过 + alpha 变体仍拒的钉扎）、序列导入、渲染分类、（可选）显示回归截图 | `tests/media/test_topos_image_source.py`、`test_topos_half_float.py` 扩展 | 全绿；既有 3175 media 用例无回归 | M |

## 4. 任务表 B：浮点导出与输出规格选择（P0）

| ID | 任务 | 落点 | 验收 | 规模 |
|---|---|---|---|---|
| F8 | 图片序列导出档位联动：`topos_image_sequence` 接入档位/像素格式二级选择——整数 `Low/Medium/High/Ultra × 10/12bit` + 浮点 `Low/Medium/High/Ultra 16bit float`（pix_fmt `gbrph16le`，qp 44/58/72/82 与 CLI 对齐） | `video_export_settings_panel.py:147,196-204,1930-1933`、`topos_profiles.py:326-344`（`tier_pix_fmt_choices` 增 half 名或新增 image-tier 选择函数）、`timeline_export.py:4577-4581` | Deliver 面板可选浮点档并正确落盘（TMET label 见 F10） | M |
| F9 | Quick Export 静帧导出加 `.toos` 格式：格式下拉 + 档位二级选择（同 F8 档位域） | `quick_export_dialog.py:334-341,401-431` | 调色页可一键导出 `toos Ultra 16bit float` 静帧 | M |
| F10 | TMET label 补写：序列/单帧导出器对 image_profile 3/4 也写 tier 展示名（`toos Ultra 16bit float` / `toos RAW 12bit 4:1`）——当前只写 0/1/2 | `topos_encoder.py:1480-1487`、`topos_meta.py:57-70` | 落盘文件读回 label 正确（`read_export_meta`/导入侧显示名一致） | S |
| F11 | i18n：补 `deliver.codec_topos_seq`（当前回退英文）及 toos 图片/浮点档位键（zh_CN + en_US） | `src/shared/i18n/locales/zh_CN.json:996-1015`、`en_US.json` | 无缺键回退 | S |
| F12 | 测试：应用层图片档位导出矩阵（UI→encoder e2e，含浮点四档 + 整数矩阵）、quick export toos | `tests/media/test_topos_image_sequence_export.py` 扩展、`tests/deliver/` | 档位矩阵全绿 | M |

## 5. 任务表 C：RAW 产品化

### C-图片（P1：后端全就绪，纯露出 + 直出）

| ID | 任务 | 落点 | 验收 | 规模 |
|---|---|---|---|---|
| R1 | 图片 RAW 导出编码链：激活 `native_profile==7` 分支；编码器输入域补 `topos_cfa12le/16le`；raw 档位表（12 档 qp 锚定，与 CLI `image_cli.c:412-431` 同表）；TMET label `toos RAW 12bit 4:1`（tier_id=0，ADR-I011 raw 不入 image tier id） | `topos_encoder.py:125-127,680-718,1452-1457,1480-1487`、`topos_profiles.py`（raw 档位表） | 应用导出的 RAW .toos 与 CLI 同档产物逐位一致（qp/几何/TMET） | M |
| R2 | 时间线 CFA 直出：解除 `timeline_export.py:9924-9931` 拒绝——限定 RAW→RAW 直通语义（源即 CFA、无调色/效果/几何变更，或首版仅允许"无滤镜 CFA 轨"，有滤镜仍拒绝并提示先转中间片） | `timeline_export.py:9924-9931`、render plan/合成链 | CFA 轨可整段导出 RAW 序列；有滤镜轨仍干净拒绝 | M/L |
| R3 | RAW 档位 UI 露出：Deliver 图像序列 + Quick Export 增 raw 档位组（位深 × 比率二级选择，默认 12bit 4:1）；UI 副行显示标称数据率（plan §3.3：≈MB/s·GB/min @当前分辨率） | F8/F9 同批落点 | 可选 `toos RAW 12bit 4:1` 导出 | M |
| R4 | 测试 + 文档回写：RAW 应用导出 e2e（对照 CLI）、`docs/image/capability_manifest.json` 露出面说明、README 去"[实验性]"措辞评审 | `tests/media/`、`docs/image/`、`README.md:421-426` | e2e 全绿；manifest/README 与能力一致 | M |

### C-视频（P2：全新产品面，建议独立 ADR-C058）

| ID | 任务 | 落点 | 验收 | 规模 |
|---|---|---|---|---|
| R5 | 容器 tier：`TC_TIER_RAW=8`，tpcD v1.8→v1.9 域 0..7→0..8（纯域扩展，golden byte-freeze 保留，LP 先例 ADR-C056）；mov.c setter/parse 两处校验 + C 测试 | `include/topos_codec.h:778-788`、`src/mov/mov.c`、`tests/unit/test_mov.c` | tier 8 往返 + 边界 9 拒绝；既有 golden 不变 | S/M |
| R6 | encoder CLI：解除 `profile=3u/bit_depth=10u` 硬编码，支持 `--profile 7 --pf 3 --bd 12/16` + raw 12 档预设 | `src/cli/encoder_cli.c:158-159,178-179` | CLI 可产 TRAW MOV；探针回读 profile/pf/bd 正确 | M |
| R7 | Python 编码链：`TOPOS_PROFILE_TIERS` 增 raw tier（单 tier + anchor_qp 按比率表驱动，D3 拍板）；tier⟹强制全 I 帧（gop='intra'，LP⟹ip2 同型规则；V8/V9 载体拒 CFA，流走 V1/V7 代）；输入 `topos_cfa12le/16le` | `topos_profiles.py`、`topos_encoder.py:125-127,680-718` | TimelineExporter 出 Topos RAW MOV，探针 tier 8 + GOP 解码通过 | M |
| R8 | 导出 UI：视频 codec catalogue 增 `topos_raw`（label `Topos RAW`，alpha=False）+ 位深 × 比率二级选择（12 组合）；CFA 轨直出依赖 R2 | `video_export_settings_panel.py:115-148`、`timeline_export.py`、`video_export_job.py`、`deliver_controller.py`、`quick_export_dialog.py`、i18n | 面板可选 `Topos RAW 12-bit 4:1` 导出 | L |
| R9 | 读端：topos_source 读 profile-7 MOV（探测→解码→CFA 平面），工厂路由复用既有 GPU debayer 链（序列侧 `cfa_gpu_debayer` 直接可用）；RAW 参数面板（WB/EI/OOTF）对 MOV RAW 生效 | `src/shared/media/topos_source.py`、`factory.py`、edit 侧既有 cfa 路径 | TRAW MOV 导入即预览（debayer 后），与图片 RAW 体验一致 | M/L |
| R10 | 音频：全 I 帧无 GOP 分段冲突；音频能力本身随 ADR-C053 全局落地状态，RAW 线不单独立项（首版无音频，与 LP 一致） | — | — | — |
| R11 | 测试与文档：test_mov TRAW 往返、导出 e2e（probe→GOP decode）、读端 e2e、ADR-C058（视频 RAW 产品档）、README | `tests/unit/test_mov.c`、`tests/media/test_topos_export.py`、`docs/` | 全绿；ADR/README/manifest 三处一致 | L |
| R12 | manifest 修订：视频侧 `bitstream_profiles."7"` bit_depths 补 16（当前仅 [12]，与帧头规则 bd∈{12,16} 不一致）；`topos_codec.h:183-184` pixel_format 注释补 3=CFA（注释滞后） | `native/topos_codec/docs/capability_manifest.json:76-84`、`include/topos_codec.h` | manifest 一致性测试通过 | S |

## 6. 任务表 D：弱链路收尾（P2/P3）

| ID | 任务 | 落点 | 备注 |
|---|---|---|---|
| W1 | 全局排查"默认回退值"类静默错标（F1 修的 `_pixel_format_name` 模式） | media 层各 source | F1 顺带，单独扫一遍 |
| W2 | `color_authority`/`ocio_colorspace` 产出无消费方——接入色彩管理决策链（当前写进 extra 即终点） | `color_decode.py:130-190`、色彩管理消费侧 | 与 G1 同根（色彩标签链），合并实施 |
| W3 | linear transfer 显示一致性推广（F5 修 toos 线；EXR/其他 linear 源统一口径） | `timeline_clip_frame_provider.py`、`image_engine.py` | F5 验收含 EXR 对照 |
| W4 | CPU 回退渲染器补 CFA（当前只 GPU 链有 debayer）——"CPU debayer"本就是 TRAW 计划遗留项 | `timeline_preview_renderer.py:5366-5489` | plan `:433-441` 遗留清单 |
| W5 | 缩略图插件浮点/CFA 支持（F6 覆盖 float；CFA 缩略图可走 base-only 缩放解码，批 3 已有底座） | `external/file_explorer/plugins/topos_image/` | |
| W6 | i18n 缺键清剿（F11 之外全局扫 `deliver.`/`export.` 回退英文的键） | `src/shared/i18n/locales/` | |
| W7 | 32bit float 未来档：spec §15 sample_kind=2 已 reserved；命名沿用 `Low..Ultra 32bit float`（用户已定）；实施待需求 | spec/manifest 域检查已拒，无需防御 | 仅备忘 |
| W8 | TRAW 计划遗留验收项：DNG 样张 ≥3 组锚点复核（批 1 验收项未闭合）、V7-B FULL 合并 P4 质量门、严格比率码控 CB（后补选项） | `topos_traw_format_plan_2026-09-12.md:228-229,264-267,439-440` | 与产品化无依赖 |
| W9 | ADR-I001 D9（"profile 3 reserved"）已被 2026-09-13 改派取代——补 superseded 标记防误导（ADR-I015 同查） | `docs/image/ADR-I001-stage0-decisions.md:31` | 文档卫生 |

## 7. 任务表 E：文档与规范一致性——白皮书/README/SDK/位流规范/脚本（P1；命名规则章节与失效内容修正建议最先落地）

> 来源：2026-09-21 第二轮拍板——"白皮书回头做相应更改，把详细的命名规则都写进去，
> 方便用户识别"；"项目的输出规格选项也都尽量保持一致"。

| ID | 任务 | 落点 | 验收 | 规模 |
|---|---|---|---|---|
| E1 | 视频白皮书更新：§4 档位家族补 **LP 档**（ADR-C056 产品化时白皮书漏更，当前仍写"六个档位"）；补视频 RAW 档位段落与规格表（随 C-视频 R5–R12 落地同步）；新增"命名规则"章节（§2.1 总纲 N-1~N-8 原文落地，含档位名识别指南：家族前缀→产品线、位深/比率/后缀词汇→能力域）；英文版同步 | `native/topos_codec/Topos_Codec_白皮书.md:68-108`、`Topos_Codec_Whitepaper.md`（英文版同构段落） | 白皮书档位表与 `capability_manifest.json` 完全一致（proxy…lp + raw）；中英版对齐；命名规则章节覆盖 N-1~N-8 | M |
| E2 | 图片白皮书更新：§5 档位体系补**浮点四档**（ADR-C057 漏更，当前仍只列六档整数）；补 **RAW 12 档**（`raw12-*`/`raw16-*`，CLI 已有、白皮书未提，含输入格式说明：4 相位平面 R/Gr/Gb/B 依次拼接 uint16 LE）；命名规则章节（N-1~N-8 图片侧）；核对示例命令 `--tier` 名与 CLI 现行预设一致（当前 §5 表为旧口径） | `native/topos_codec/Topos_Image_白皮书.md:52-119` | 白皮书档位表覆盖整数 + 浮点 + RAW 全部 22 档，与 CLI `--tier` / manifest 三方一致 | M |
| E3 | 输出规格选项一致性：建立**单一档位注册表**（display name ↔ tier id ↔ TMET label ↔ CLI `--tier` id 四向映射，按 N-7），视频导出面板/图像序列/Quick Export/probe 显示/TMET 写入全部改为从注册表取名；清剿既有不一致（`deliver_page.py:360` 的 "Topos Image Sequence" 与 i18n 缺键 `deliver.codec_topos_seq` 回退英文并存；新旧 deliver 面板标签口径不一） | `src/shared/codec/topos_profiles.py`（注册表落点）、`src/features/deliver/`、`src/shared/export/quick_export_dialog.py`、probe/导入侧显示点 | 同一档位在任何入口显示同名；代码中无第二份硬编码档位名字符串表 | M/L |
| E4 | 命名一致性回归测试：从注册表枚举全部档位名，断言 manifest（native + image 双份）/ CLI 预设表 / i18n 键 / TMET 写读闭环一致——扩展既有 manifest 一致性测试 | `tests/media/test_capability_manifest.py` | 新用例全绿；后续新增档位漏同步任一处即红 | M |
| E5 | **编码仓 README 同步**：视频档位速览表补 LP 行（RAW 行随 C-视频落地补）；图片档位表补浮点四档与 RAW 12 档；修正 444 High/Ultra 位深标注矛盾（以 `kTierPresets` 实表为准统一 README 与 CLI 帮助两处）；"六档 capability schema"等状态段措辞更新（七档）；档位显示名与 §2.1 规则对齐（README 现"Topos 422 Low"式与 TMET "toos Low 10bit" 式并存，随 E3 注册表统一） | `native/topos_codec/README.md:23-40,54-74`、`src/cli/image_cli.c`（帮助文本同批核对） | README 档位表与 CLI `--help`、manifest 三方一致；无失效/矛盾标注 | M |
| E6 | **根 README 同步**：§5.8 HALF 段档名 `hf-lossless/hf-2/4/8/16` → `float-low/medium/high/ultra`（**现行错误信息，建议 M0 先行单独修**），无损表述按 spec §15 口径（显式 `--half --qp 20` 路径）；图片格式叙述按 N-4/N-5/N-6 重写——HQ/XQ 是 profile 类名，与 Low..Ultra tier 体系分开表述（:306、:422）；TRAW 段补 `Topos RAW 12-bit 4:1` 命名引用与白皮书链接，"[实验性]" 措辞随 R4 评审（:426）；视频编码叙述补 LP 档 | `README.md:306,421-426`（视频段落定位随实施核） | 根 README 无失效档名与缺失档位；与白皮书/编码仓 README 口径一致 | M |
| E7 | **视频 SDK.md 更新**（滞后最重的活文档）：补 LP 档（tier 7 / tpcD 写入 / 锚 qp72 / no-alpha / tier⟹gop='ip2'）与 profile 7 + bd16 能力域；位流/容器规范引用升级（v1.5 / v1.8）；§4 补 tpcD 档位写入说明；修正"v1 容器无音频轨"等过期陈述 | `native/topos_codec/docs/SDK.md:10-24,77-79,148-177,254-263` | SDK 与 container_spec v1.8 / manifest 一致；LP 有接入说明 | M |
| E8 | **图片 SDK.md 更新**：支持矩阵补 profile 3=RAW / 4=HALF 两行，删"HALF 非目标"（非目标词表改 FLOAT32/tile/mipmap/多图）；tier 表补浮点四档与 RAW 十二档；C ABI 列表补 `tc_image_half_to_codes`/`tc_image_codes_to_half`；Python 段补 `decode_float()`；"36 项公共符号"计数更新；修两处"Image Lossless (3) reserved"矛盾陈述 | `docs/image/SDK.md:10-17,21-43,68-127,150-152,178` | 与 spec §15/§9.1、manifest 三方一致 | M |
| E9 | **位流规范补章**：`bitstream_spec_v1.md` version_minor 枚举补 5/6（TRAW 扩展代）、bit_depth 补 16、pixel_format 表补 3=CFA——TRAW 目前只活在计划文档与 manifest 里，未入正式规范；新立 **V7-R3（em 8）章节**——LP 载体已产品化（ADR-C056），不能只活在 ADR-C048 与计划文档中（9 个 bitstream_spec_*.md 中 em 8 零命中） | `native/topos_codec/docs/bitstream_spec_v1.md:123-141`、新 V7-R3 章节（并入 v7 spec 或独立文件） | 规范与 golden/实现对齐；LP/TRAW 有正式规范面 | M/L |
| E10 | **ABI 清单与工具脚本**：`public_symbols_v1.txt` 追加 `tc_image_half_to_codes`/`tc_image_codes_to_half`（头文件已声明、dylib 已导出，但不在 ABI 钉死清单——新 API 无契约保护）；`scripts/toos_tools.py` decode 对 sample_kind=HALF **显式拒绝**（当前把 HALF 文件当整数 u16 直接出 PNG，静默产出错误像素），docstring 声明适配器只覆盖整数 GBR 族 | `native/topos_codec/tests/abi/public_symbols_v1.txt`、`scripts/toos_tools.py:71-107,186-193` | check_exports 覆盖新符号；toos_tools 不再产出错误像素 | S |
| E11 | **档案与状态文档收尾**：manifest `:357`"六档统一 qmatrix_id=1"改七档口径（真相源自身过期）；`docs/image/stage_status.json` 追加 TRAW/HALF/float-ladder revision 并刷新 snapshot_date（当前停在 09-08）；`risk_register.md` R-I04 reserved 示例集更新（去 HALF/Lossless，入 FLOAT32/tile）+ 补浮点/RAW 专项风险行；`packaging/ffmpeg/README.md` 登记 LP/float(bd16)/RAW 为解码 wrapper 后续项（当前 bd>12 即 AV_PIX_FMT_NONE）；TPIC 总计划书与视频 V2.0 计划书文档头加勘误块（.tpic→.toos、profile 3 改派 RAW、档位命名以 manifest/ADR 为准）；`topos_image_optimization_implementation_plan.md:62` 加勘误；ADR-I001 D1/D9 补 superseded-by 标记；CLI 展示串 "Image HF (half float)" 按 D4 换词 | `docs/image/stage_status.json`、`docs/image/risk_register.md:10`、`packaging/ffmpeg/README.md:10`、`docs/Topos Image（TPIC）….txt`、`docs/image/ADR-I001-stage0-decisions.md:23,31`、`native/topos_codec/src/cli/image_cli.c:150` 等 | 全部状态文档与真相源一致；无过期"reserved"陈述残留 | M |

---

## 8. 任务表 G：管线正确性与性能（2026-09-21 全项目复查新增）

> 复查方向性结论：色彩标签从 IDSC → VideoMetadata → 资产库 → 导出配置这条链是当前最大
> 系统性盲区（G1/G3/G7 及 F5/W2 同根）；浮点缩放解码 native 侧已就绪（`decode()` 的
> target_size 对 sample_kind=HALF 无门控），缺的只是测试（G11）与消费方接线（F2/F4）。

| ID | 任务 | 落点 | 验收 | 规模 |
|---|---|---|---|---|
| G1（P0） | **.toos 色彩标签全链打通**：`ToposImageSource.open` 把 IDSC 色彩码（primaries/transfer/matrix，含 8=linear、20=TRAW_LOG0）写入 `VideoMetadata` 对应字段（当前恒 None）→ `project.py` 资产入库的条件拷贝恢复生效 → 导出侧 `_resolve_export_color_metadata` 不再静默落 BT.709 SDR；`_detect_source_color_space` 映射表同步扩 linear/TRAW_LOG0 并兼容 toos 数字码（当前只认 FFmpeg 字符串） | `topos_image_source.py:153-170,229-232`、`src/features/edit/core/project.py:4835-4842`、`timeline_export.py:4361-4401`、`timeline_clip_frame_provider.py:1270-1278` | linear/GBR/PQ 序列导入→预览 IDT 与导出标签双正确（E2E 断言 tag 透传） | L |
| G2（P0） | **素材面板 .toos 缩略图**：`_load_fast_thumbnail` 增 `.toos` 分支（decode + 8-bit 化）——当前落入 cv2.imdecode 恒 None，媒体池显示占位符；.toos 序列同断 | `src/shared/media/thumbnail_provider.py:694-758` | .toos 图/序列在媒体池有正确缩略图 | M |
| G3（P1） | **`topos_frame_to_rgb` 修色**：GBR 布局平面直通（当前无条件套 YUV 矩阵——GBR 影片海报帧/filmstrip 颜色静默错误，非失败）+ PQ/HLG transfer 做 OETF 显示变换（当前直接 clip）；海报帧与 filmstrip 两个消费点同修 | `topos_source.py:1952-1992`、`thumbnail_provider.py:789,1781-1805` | GBR/PQ 源缩略图颜色与 GPU 预览一致 | M |
| G4（P1） | **GBR（pf=2）topos 影片代理**：`_run_topos` 增 GBR→YUV 显式转换（复用 ColorConverter）或 pf2 直通——当前显式拒收且无回退（TPIC 无法被 ffmpeg 解码），GBR 影片只能全分辨率播放 | `src/shared/media/proxy_generator.py:578-586` | GBR 影片可生成代理，播放走代理路径 | M |
| G5（P1） | **节点缓存 payload 钉 V 代际**：`encode_toos_payload` 显式设 `fc.reserved[0]=8`——全仓唯一未钉扎的编码写点（ctypes 零初始化→隐式 V1，且 V1 熵模型放弃 rans2 压缩收益，缓存文件偏大）；顺带去掉每帧 `tobytes()` | `src/features/composite/render/topos_cache_codec.py:157-171` | 缓存文件缩小（rans2 生效）；全仓再无隐式代际写点 | S |
| G6（P2） | **HDR 节点缓存 HALF .toos 化**：image_profile 4（HALF）payload 替代 npz f16 回退——当前 HDR 中间结果（[0,1] 外 payload 被正确拒绝后）全部未压缩落盘；编码链 `gbrph16le` 已就绪 | `topos_cache_codec.py:129-149`、`disk_cache_store.py:167-171` | HDR 缓存体积显著下降；往返精度达标（half 域 ≤2 ULP 或显式声明口径） | M |
| G7（P2） | **代理链口径对齐与去拷贝**：`_run_pyav_to_topos` 未知色彩标签改显式失败/告警（对齐 `_run_topos` 的 R-13 策略——当前静默回退 code 1=709，HDR 源代理可能错标）；去掉每帧 3× 全帧 `tobytes()`（binding 支持指针直传）；full-range 标签保留 | `proxy_generator.py:891-930,958-971` | 未知标签源不再静默错标；代理生成耗时下降 | M |
| G8（P2） | **海报帧缩放解码**：`read_topos_frame_rgb` 传 `target_size` 走 native reduced 解码——当前 4K/8K 源为一张 1024px 缩略图做全帧解码再 cv2.resize | `thumbnail_provider.py:773-801`、`topos_source.py:1995-2014` | 海报帧生成耗时大幅下降；像素对照全帧解码下采样一致 | S |
| G9（P2） | **half↔code 映射 LUT 化**：65536 项 uint16 LUT gather 替换 uint32 `np.where` 链（每平面 2–3 份全帧临时，8K 约 1.5–2GB 内存流量），或接 native `tc_image_half_to_codes` | `src/shared/codec/topos_image_binding.py:95-130` | decode_float 8K 耗时/内存下降有基准数据；逐位结果不变 | S/M |
| G10（P2） | **单帧导出 UI 入口**：监视器/导出面板加"导出当前帧为 .toos（零再编码）"，直连现成 `extract_topos_frame_to_toos`（LP P 帧拒收、TRAW 映射、TMET 转发均已实现，当前全仓无 UI 消费方——已建成未通电） | UI 侧（监视器/导出面板）、`topos_frame_extract.py` | 时间线任意帧可零再编码存 .toos，TMET/档位正确 | M |
| G11（P2） | **浮点×缩放解码测试**：`decode`/`decode_float` × sample_kind=HALF × target_size（native 与 python_resize 两路径）几何/数值一致性——当前零覆盖，阻碍 F2/F4 接线信心 | `tests/media/test_topos_half_float.py`、`test_topos_image_source.py:95-136` | 两路径全绿，浮点缩放语义被钉住 | S |
| G12（P3） | **代理档参数化**：12-bit 源不再强制 `_color_to_10` 右移量化、444→422 色度简单平均降采样改为可选保留（当前硬编码 10-bit/422/profile3/qp34） | `proxy_generator.py:587-700` | 代理档可配置；默认行为不变或显式声明变更 | M |
| G13（P3） | **收尾杂项**：Metal 内核测试（`TOPOS_METAL_KERNEL_TESTS` 默认 opt-out）CI 常态化评估——GPU 内核回归常态缺席；节点缓存命中 `decode()` 增 target_size 缩放读回 | `tests/media/test_topos_422_surface_kernels.py:297-299`、`topos_cache_codec.py:188` | 按项评估结论落档 | S |

## 9. 任务表 H：图片色彩空间域扩展（2026-09-21 用户需求新增）

> 需求：图片格式默认 sRGB；支持 Adobe RGB、CMYK、灰度等常见模式。
> 设计红线：spec §7 / ADR-I014 冻结原则不变——**读侧缺失标签仍硬错、不静默猜 sRGB**；
> "默认 sRGB"落成**写侧显式标注**（写 transfer=13），不是读侧回退。旧文件（已标 1=bt709）
> 不回填、不重解释。

| ID | 任务 | 落点 | 验收 | 规模 |
|---|---|---|---|---|
| H1（P1） | **sRGB 成为整数档写侧默认**：C 帧头校验域与命名表增 transfer **13（sRGB EOTF，CICP 标准码）**，顺带评估同批开放 4=gamma2.2、primaries 11=P3-D65（显示工业常用，当前只有 12=P3-DCI）；走新 minor 代门控（旧读端干净拒绝，v1.2–v1.7 先例）；CLI 默认、codec cfg 0 默认、Python 写侧码表、编码器默认全部切 13；**旧文件语义零漂移**（已落盘 transfer=1 保持 BT.709 解释）；浮点（linear 8）/RAW（LOG0·linear 冻结对）默认不变 | `frame_header.c:449-458,823-832`、`codec.c:306-309`、`topos_binding.py:88-105`、`image_cli.c:658-663,688-689`、`topos_encoder.py:248-290` | 新写整数档 .toos 读回 transfer=13 且按 sRGB EOTF 显示；旧文件行为逐位不变；golden 新旧双轨用例；Python 解码枚举 `ColorTransfer.SRGB`（已备好）接通 | M |
| H2（P1） | **ICCP 应用写入接线**：导出 .toos 时把源 ICC profile（`color_manager.detect_icc_profile`）写入 ICCP chunk + `iccp_ref=1`——`write_packet` 的 extra_chunks 通道现成，当前唯一生产调用只写 TMET，导致导入→.toos **ICC profile 丢失**；与 G1 色彩标签链同批验收（CICP 数值码 + ICCP 权威两级并存，矛盾按 spec §7 硬错）；ICCP 上限 8MiB 校验已有 | `topos_encoder.py:1490-1493`、`color_manager.py:204,319-327`、`topos_image_source.py:108-118`、`color_decode.py:188-189` | 带 ICC 的 JPEG/TIFF/PNG → .toos → 再导出全程 profile 保留；`display_authority=ICCP` 路径可用；与 CICP 矛盾文件被拒 | M |
| H3（P2） | **Adobe RGB 支持**：载体 = ICCP（H2 后写 AdobeRGB1998.icc 即可，H.273 无 Adobe primaries 码是行业标准现实）；CICP 侧近似标注口径写进 spec §6 注记（建议 primaries=1 + ICCP 权威覆盖）；导入 Adobe RGB 源（TIFF/JPEG/PSD）识别→显示→导出全链验证 | 依赖 H2；`color_decode.py`、spec §6/§7 注记、色彩管理 profile 集合 | Adobe RGB 图导入→.toos→显示/导出色彩正确（对照 Photoshop 口径抽查） | S |
| H4（P3，评估后立项） | **灰度（GRAY）原生支持**：IDSC channel_model=2 已占位但整链不可达（无 pf→GRAY 映射、channel_count≥3、无 1 平面载荷模型）——需新 pf/profile 枚举代 + 单平面几何推导 + IDSC 交叉校验 + Python 注册名 + 渲染链，属新载体代际；先出评估（体积收益 vs GBR 三等平面、YUV444 折中方案），建议独立 ADR 评审后开工 | `image_container.c:390-394,770-779`、`frame_header.c:385-390,427-429`、`topos_image.h:79` | 评估结论落档；若立项：1 平面端到端（编码/解码/渲染/导出）+ 体积对比报告 | L |
| H5（P3，按 D5 路线） | **CMYK 支持（软转换路线）**：放宽 ICCP 校验允许 'CMYK ' colorSpace（当前 `tci_validate_iccp` 显式拒非 RGB profile）；入口经 LittleCMS CMYK→RGB、出口 RGB→CMYK（打印交付），存储保持 RGB；复用既有 CMYK 软打样链（image_engine/shaders 的 CMYK Simulation）；UI 遵循历史原则"不支持时明确禁用而非静默 sRGB"（2026-06-28 计划已记录）；原生 4 通道存储不立项（D5） | `image_container.c:615-640`、`color_decode.py:120-125`、`color_manager.py:80-118`（SWOP/FOGRA profile 已打包）、`image_engine.py`/`shaders.py:376-383` | CMYK ICC 可嵌入/读回不拒；CMYK 源导入显示正确；导出 CMYK TIFF 走软转换往返抽查 | M |

---

## 10. 任务表 I：RAW 色彩工作流（2026-09-21 新增，对标专业视频 RAW）

> 对标结论：专业 RAW（BRAW/ProRes RAW/R3D）的"选择色彩空间"是**渲染期决策**
> （debayer/IDT 阶段非破坏选择），不是烘焙进文件——文件保持 camera-native 像素 +
> 携带传感器校准元数据（原生色域矩阵/WB/black level/EI）。TRAW 架构（debayer 应用层 +
> RAW 参数非破坏）方向本就正确，缺的是元数据与 IDT 选择面。**可复用资产**：Color 页
> 照片 RAW 面板已有 16 色彩空间 + 18 伽马选择 UI 与 Bradford 矩阵（`raw_panel.py:99-110`、
> `shaders.py:492-623`）——注意其矩阵输入假设是 Linear ProPhoto D50，移植到 TRAW 时
> 矩阵源须换 sensor native。视频 RAW（C-视频 R5–R12）落地时 MOV 侧同步承载 TRWM 原子。

| ID | 任务 | 落点 | 验收 | 规模 |
|---|---|---|---|---|
| RC1（P1） | **TRAW 开发元数据块（TRWM）**：新 chunk 承载 as-shot WB、EI、black level、cfa_layout（RGGB/BGGR/GRBG/GBRG——当前硬编码 RGGB）、sensor matrix 引用；.toos 走新 chunk（IDSC reserved 强制 0 不可用，`image_container.c:332-337`）、MOV 走 ADR-C021 追加原子；C API 读写扩展（`tc_image_read_metadata`/mask 现只认 6 种 chunk）；导出端把 RawParams 回写、读侧作 as-shot 基线 | `topos_image.h:38-53,233-264`、`image_container.c`、`src/mov/mov.c`（ADR-C021 扩展）、`image_cli.c`、Python binding、`topos_encoder.py` | TRAW 文件往返保留 WB/EI/black level/cfa_layout；重导入不再丢 RAW 参数；chunk CRC/上限校验齐 | L |
| RC2（P1） | **传感器色域矩阵与相机配置文件**：sensor matrix（native↔XYZ）元数据结构 + 相机预设库（ARRI AWG3 / S-Gamut3 / V-Gamut / DaVinci Wide Gamut / RED WG / Canon Cinema Gamut / F-Gamut / BMD Film Gen5——`raw_panel.py` 16 空间的矩阵数据可复用为预设源）；primaries 字段语义按 D6 迁移（恒 1 占位、权威在 TRWM） | TRWM 结构（RC1）、`raw_panel.py:99-110` 矩阵源、spec §6/§9.1 注记 | 无 TRWM 的旧文件按"未知相机"处理（显式提示，不静默猜）；带矩阵文件 debayer 后色彩正确 | M |
| RC3（P1） | **debayer IDT 阶段**：`CfaGpuDebayer` 增 gamut mat3 uniform（camera-native → 工作色空间），由 RawParams 新增 color_space/gamma 字段驱动；Edit 页 RAW 面板增色彩空间/伽马下拉（复用 raw_panel 的 UI 结构，矩阵源换 sensor native）——对齐 ProRes RAW "Treat as" / BRAW color science 的非破坏语义 | `cfa_gpu_debayer.py:29-41,148-159,309-329`、`clip_params.py:977-981`、`edit_properties_panel.py:2806-2839` | 同一 TRAW 素材切选不同目标空间，画面色彩正确变化且不破坏原像素；预设矩阵对照参考实现抽查 | M |
| RC4（P2） | **as-shot 基线语义**：读侧把文件 WB/EI（TRWM）作为 debayer 初值，面板显示为相对 as-shot 的偏移量（当前缺省 1.0/0.0、重导入即丢） | `topos_image_sequence.py`、`edit_gpu_preview_renderer.py:2602-2611`、RawParams 默认值链 | 导入 TRAW 即得 as-shot 初值；偏移量序列化进工程 | S |
| RC5（P2） | **工作色空间基座**：定义项目 working space（对齐 composite Stage-2 既有计划的 ACEScg 选项，`color_space.py:32-34`）；debayer 输出进 working space 而非显示直通；把临时 gamma2.2 OOTF 从 debayer 移到 viewer ODT 阶段（当前烘进轨道纹理与普通轨混合，`clip_params.py:966-967` 已注明"仅查看语义"） | `src/features/composite/core/color_space.py:8-34`、`ocio/minimal.ocio`、`cfa_gpu_debayer.py:156-158,236-238` | CFA 轨与普通轨在同一 working space 混合语义一致；OOTF 不再烘进轨道纹理 | L |
| RC6（P2） | **CFA 交付出口**：当前 TRAW 无任何可交付路径（CPU 导出显式拒绝 `timeline_export.py:9924-9929`）——补 GPU debayer→working space 导出路径，与 R2 的 RAW→RAW 直出构成双路径（再编码档 / 保持 RAW） | `timeline_export.py:9924-9931`、GPU 导出渲染链 | TRAW 素材可导出常规视频档（经 debayer）或 RAW（R2 直出）；导出与预览一致 | M |
| RC7（P2） | **通用色彩链接纳 TRAW**：`_detect_source_color_space` 补 'topos-traw-log0'/CFA 分支（当前 unknown→默认 bt709，与 CFA 专用分支两套语义）；与 G1/F5/H1 词表一次性统一 | `timeline_clip_frame_provider.py:1270-1278`、`color_decode.py` | TRAW 素材在任何通用消费路径不落 bt709 默认 | S |
| RC8（P3） | **black level / flat 域（校准余项）**：black level 减除进 debayer；flat field（平场校准）评估——TRAW 计划 v1 范围内但零实现的最后一项 | `cfa_gpu_debayer.py`、TRWM 字段 | 评估结论落档；black level 端到端生效 | S |

---

## 11. 实施顺序建议（里程碑）

| 里程碑 | 内容 | 前置 |
|---|---|---|
| M0（先行） | E1/E2 白皮书"命名规则"章节（§2.1 总纲落地）+ E3 档位注册表骨架——先定名，后续所有实施从注册表取名；顺带先行修正已失效文档（E6 根 README `hf-*` 旧档名、E5/E6 两处 LP 缺档） | 无 |
| M1（P0） | A 组 F1–F7 + G2（.toos 素材面板缩略图）：浮点导入+预览全链打通 | M0 |
| M2（P0） | B 组 F8–F12 + E3 收尾 + G1（色彩标签全链）+ E10（toos_tools 防错像素）：浮点/整数图片档位导出 UI + TMET label + i18n | M1（预览可验证导出物） |
| M3（P1） | C-图片 R1–R4 + E2 + E8（图片 SDK）+ E11（档案状态文档）+ H1/H2（sRGB 写侧默认 + ICCP 写入）：图片 RAW 导出露出（命名按 §2.1） | M2（档位选择面复用；H2 与 G1 色彩链同批设计） |
| M4（P2） | C-视频 R5–R12 + E1 + E7（视频 SDK）+ E9（位流规范补章）：视频 RAW 绿地（建议 ADR-C058 立项后开工） | M3（R2 直出语义、R3 档位 UI 复用） |
| M5（P3） | D 组 W1–W9 + E4 命名一致性回归 + G3–G13 分批 + H3–H5（AdobeRGB / 灰度评估 / CMYK 软转换）：弱链路、性能与色彩域收尾 | 按项独立（H3 依赖 M3 的 H2） |
| M6（P2） | I 组 RC1–RC8：RAW 色彩工作流（TRWM 元数据 → 相机配置文件 → debayer IDT 选择面 → as-shot → 工作色空间 → 交付出口） | M3（RAW 露出后才有消费面；RC5 与 composite Stage-2 计划合流；RC6 依赖 R2） |

## 12. 风险与非目标

- **非目标**：本计划不改变已冻结的格式语义（§15 HALF 域、TRAW profile 7 契约、tpcD golden）；不做 BRAW/DNG 互操作（TRAW plan §7 明确不做）；不做帧间/长 GOP RAW。
- **风险 1**：F4/F5 显示链改动触及 GPU 上传与色彩管理公共路径，需对照 EXR half/linear 导入做显示回归，避免波及既有整数 .toos/EXR 工作流。
- **风险 2**：R2 CFA 直出的"允许哪些滤镜"语义需明确（首版建议 fail-closed：任何像素级处理即拒绝直出），否则导出物与预览不一致。
- **风险 3**：视频 RAW 是新 tier 域扩展（v1.9），读旧写旧兼容靠 minor 门控先例（LP 已验证该模式），但需全量 golden 门禁确认 byte-freeze。
- **风险 4**：白皮书与 README 是对外文档——档位表与命名规则更新必须与 manifest、CLI、UI 四方同步，漏一处即误导集成方（LP、浮点两次产品化都已出现白皮书/README 漏更，根 README 至今仍写着已废弃的 `hf-*` 档名）。E4 命名一致性回归测试是机制化防漏手段，应先于任何新档位落地。
- **风险 5**：G1 色彩标签链修复触及导入持久化（project.py 资产字段）、预览 IDT、导出配置三侧词表，必须与 F5/W2 同批设计验收，防止出现第三套 transfer 词表；旧工程文件无标签资产的迁移语义需一并定案（建议按"无标签=维持现状 709"不回填）。
- **风险 6**：H1 把默认 transfer 从 1(bt709) 切到 13(sRGB) 属语义敏感变更——必须走 minor 代门控 + golden 新旧双轨，旧文件**不回填不重解释**；spec §7"缺失不猜 sRGB"冻结原则保持（默认是写侧显式标注，非读侧猜测）。H 组与 G1/F5 的词表需一次性统一设计，避免出现第四套 transfer 词汇（FFmpeg 字符串 / toos 数字码 / ColorTransfer 枚举 / ICCP 之外再分叉）。
- **风险 7**：RAW 色彩链（I 组）跨 .toos chunk / MOV 原子 / debayer shader / 工程参数四层，且与既有照片 RAW 面板（ProPhoto D50 假设）、composite 工作色空间（Stage-2 计划）存在矩阵源差异——RC1 定 TRWM 结构时须一次性对齐三处矩阵约定（sensor native / ProPhoto D50 / ACEScg），否则移植 UI 时矩阵语义错位；TRWM 属新 chunk，需 minor 代门控与旧读端干净拒绝。
- **比率档为 CQ 语义**：档位名中的比率是定位标签，体积随内容浮动（plan §1.1.1 冻结）；UI 副行应显示标称数据率而非承诺精确比率（RED 的教训，plan §3.3）。
