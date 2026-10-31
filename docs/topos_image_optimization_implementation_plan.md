# Topos `.toos` 图片与 Topos 视频编码系统优化及生态落地实施计划

**文档状态：** 可执行计划（2026-09-08）  
**适用范围：** `native/topos_codec`、Topos MOV/视频帧链路、`src/shared`、`scripts/toos_tools.py`、Topos Color 图片/序列/播放链路、`external/file_explorer`、OIIO/宿主插件适配层
**格式基线：** `.toos` / TPIM envelope + TPIC image packet  
**当前代码基线：** `1ce02fb6 feat(file-explorer): preview Topos image files`  
**执行原则：** 按依赖和验收门槛推进，不用日历时间作为完成标准；每个阶段完成后单独提交。

**执行记录（2026-09-08）：** O3/O4 的第一批 native reduced-decode 能力已落地：新增固定比例 `1/2、1/3、1/4、1/8`、低频系数裁剪、AVX2/NEON 采样逆变换、跨帧 reduced batch 和 caller-owned host-visible GPU surface 接口。当前路径仍需消费可变长码流中的高频 `(run, level)` 码字，因此“跳过高频熵解码”尚未达到 bitstream-level skip；6K→2K 的真实收益与该限制已记录在 `docs/image/reports/reduced_decode_4k_6k_to_2k_2026-09-08.md`，后续需设计分层/独立长度 coefficient band 才能继续降低熵解码成本。

**专项后续计划：** 真正面向 4K/6K/8K/12K/16K→约 2K 播放的 bitstream-level band skip、2K scalable base、播放器/GPU 接线和量化验收，见 [`docs/topos_prores_style_reduced_decode_implementation_plan.md`](topos_prores_style_reduced_decode_implementation_plan.md)。该专项计划明确：仅裁剪 8×8 高频系数仍会按源块数处理 DC，不能单独满足 12K 接近 2K 负载的目标。

这份计划解决五个问题：

1. `.toos` 在专业画质、位深、Alpha、HDR 和色彩管理上能否做到可验证的正确；
2. 解码、缩略图和图片序列能否降低 CPU、内存和首帧延迟；
3. 素材管理器能否把 `.toos` 当作一等图片格式稳定浏览、筛选和预览；
4. Premiere、DaVinci、OIIO、FFmpeg 等外部工具如何接入而不复制第二套 codec。
5. Topos 视频当前作为帧内中间编码的定位如何稳定落地，以及在不牺牲画质和实时解码的前提下，如何先优化方向预测/熵编码，再为后续版本预留帧间预测、参考帧和运动补偿路线。

---

## 1. 当前基线和未闭合问题

### 1.1 已经具备的能力

| 领域 | 当前实现 | 状态 | 证据 |
| --- | --- | --- | --- |
| 文件封装 | TPIM preamble、directory、IDSC、PIXL、可选元数据 chunk、CRC、checked arithmetic | 已实现 | `native/topos_codec/src/image/`、`docs/image/topos_image_file_spec_v0.md` |
| 图片 codec | YUV 4:2:2、YUV 4:4:4、GBR 4:4:4；10/12-bit；VLC/Rice 路径 | 已实现 | `native/topos_codec/include/topos_image.h`、`docs/image/capability_manifest.json` |
| Alpha | A16 无损模式、受限近似模式、straight/premultiplied 字段 | 已实现，需继续补跨宿主验证 | `tests/media/test_topos_export.py`、`tests/media/test_toos_tools.py` |
| Python 解码 | `ToposImageCodec`、`ToposImageSource`、`.toos` factory 路由 | 已实现 | `src/shared/codec/topos_image_binding.py`、`src/shared/media/topos_image_source.py` |
| 图片序列 | 有界预取、generation cancel、平面缓存、Topos 序列导出 | 已实现，UI/播放接线仍需扩大 | `src/shared/media/topos_image_sequence.py`、`src/shared/export/topos_encoder.py` |
| 视频帧提取 | Topos MOV sample 直接包装成 `.toos`，不解码、不重编码 | 已实现 | `src/shared/media/topos_frame_extract.py` |
| 素材管理预览 | `.toos` 分类、原生解码、WebP 三档缩略图、灯箱预览、元数据 provider | 已实现 | `external/file_explorer/plugins/topos_image/`、`external/file_explorer/static/assets/js/fe-state.js` |
| 测试基建 | golden、C debug/ASan/UBSan、fuzz、Python image tests、file explorer tests | 已实现，需扩真实素材和跨平台矩阵 | `docs/image/SDK.md`、`tests/file_explorer/test_topos_image_preview.py` |

### 1.2 Topos 视频基线

| 领域 | 当前实现 | 状态 | 证据 |
| --- | --- | --- | --- |
| 视频压缩结构 | MOV 封装多个独立 TPIC frame packet；当前视频帧复用图片的 `tc_frame_encode`/`tc_frame_decode` 内核 | 已实现，符合当前帧内中间编码定位 | `native/topos_codec/src/mov/`、`src/shared/export/topos_encoder.py` |
| 帧间工具 | inter prediction、motion vector、参考帧、DPB、GOP、B/P 帧 | 未实现 | 当前 codec 源码无对应解码/参考帧路径 |
| 帧内预测 | 已有 DC/块内预测和变换量化，尚未形成完整方向模式信令 | 部分实现 | `native/topos_codec/src/codec/intra.c` |
| 熵编码 | V1 Rice/V2 canonical VLC 等帧内路径 | 已实现 | `native/topos_codec/src/entropy/`、bitstream spec |
| 算术编码 | CABAC/range context、自适应 level/run 语法 | 未实现 | 当前没有视频专用 arithmetic context |
| 并行 | slice 级线程池、连续 decode context、批量 decode API | 已实现，需扩展到跨帧调度 | `native/topos_codec/src/common/tpool.c`、`decoder_ctx.c` |
| 码控 | 帧级 QP 搜索、AQ/RDO、码率闭环 | 已实现，仍受帧内地板限制 | `tc_frame_encode_sized`、`docs/video_topos_encode_decode_speed_2026-09-07.md` |
| 实测速度 | 2K 422 编码约 136–295 FPS，解码约 303–440 FPS E2E；4K 422 编码约 49–67 FPS，解码约 70–162 FPS E2E | 已有基线 | `docs/video_topos_encode_decode_speed_2026-09-07.md` |
| 当前代体积边界 | 低码率档位会撞 QP63 地板；帧间不复用是帧内中间编码的预期取舍 | 需在帧内范围内优化；帧间压缩属于后续路线 | 真实 H.264 素材速度报告结论 |
| 视频预览/播放 | ToposMovieSession、ToposPlayer、file explorer `.topos` provider | 已实现 | `external/file_explorer/plugins/video_pro/`、`src/shared/media/topos_source.py` |

### 1.3 必须正面解决的问题

| 问题 | 现状 | 影响 |
| --- | --- | --- |
| 浏览器预览精度 | `.toos` 在 file explorer 中转成 8-bit RGB WebP 后由 Chromium 显示 | 预览方便，但不等于 10/12-bit/HDR 专业监看 |
| HDR/色彩管理 | codec metadata 已保存，所有消费端尚未统一完成 ICC/OCIO/CICP 和 PQ/HLG 显示链 | 同一文件在不同宿主中可能亮度、色域或 Alpha 边缘不一致 |
| 解码性能 | 基线包含完整解码、平面转换和 RGB 预览，缩略图路径仍可能先解码整帧 | 4K 首次预览 CPU、内存和延迟偏高 |
| 目标尺寸解码 | v1 `ToposImageCodec.decode(target_size=...)` 尚未真正缩放 | 列表缩略图为 300 px 仍读取完整像素 |
| ROI/tile | v1 明确未支持 | 超大图、局部检视和纹理流送无法只读需要区域 |
| 无损 Profile | ~~`Image Lossless` 仍为 reserved~~【勘误 2026-09-21：profile 3 已改派 Image RAW（TRAW）；无损语义由 RAW Linear 模式 + `--qp 28`/`--half --qp 20` 显式档承载】 | 不能把“视觉无损”误称为数学无损 |
| 序列规模 | 1000+ 帧、跳帧、反向、冷缓存/NAS 还缺完整门禁 | 长序列编辑时缓存和 I/O 风险未知 |
| 外部生态 | OIIO 源码已交付，Nuke/Photoshop/AE/FFmpeg/Resolve/ Premiere 适配尚未形成发布 SDK | 无法在其他软件中稳定读写 `.toos` |
| 分发 | Windows/Linux ABI smoke、安装包签名、公证仍为遗留项 | 用户机器上可能出现“开发机可用、发行包不可用” |
| 视频帧内优先定位 | 相邻帧之间不共享预测信息；静态背景、渐变和运动区域重复写入 | 换取低延迟、可随机访问、稳定解码和中间流程友好；体积上限由后续 inter 版本解决 |
| 视频缺少方向预测 | 结构边缘主要依赖 DC/块内基础预测，AC 残差能量偏大 | 帧内视频和 `.toos` 图片在结构素材上体积偏大 |
| 视频缺少自适应算术熵编码 | VLC/Rice 码表和语法不能随内容更新 | 预测后的零值/小 level 分布没有完全转化成码率收益 |
| 跨帧码控未实现 | `tc_frame_encode_sized` 逐帧搜索 QP，不能利用 GOP 级复杂度、场景切换和 VBV | 当前帧内中间编码不要求；待后续 inter/GOP 版本启用 |
| DPB/参考帧未实现 | 当前解码每帧独立完成 inverse transform/entropy decode | 保持当前解码简单稳定；待后续 inter 版本增加 DPB 和硬件协同 |
| 中间编码容器语义 | 当前优先保证独立 sample、时间戳、metadata、顺序读和抽帧；关键帧索引和错误恢复仍需补齐 | 先服务剪辑/调色中间流程，后续 inter 版本再扩展 GOP seek 语义 |

---

## 2. 总体目标和不可破坏的约束

### 2.1 产品目标

| 目标 | 可验收定义 |
| --- | --- |
| 画质可证明 | 每个档位公布位深、像素格式、QP/矩阵、PSNR/SSIM/ΔE2000、Alpha 误差和样本范围；不以文件大小单独宣称质量 |
| 色彩可复现 | 给定相同 `.toos`、CICP/ICC/OCIO 和显示变换，参考 CPU、GPU、file explorer、Topos Color 的结果在允许误差内一致 |
| 解码更快 | 分开报告 probe、读取、codec decode、色彩转换、缩放和 WebP 编码；优化以 release build、真实素材和高熵素材共同验收 |
| 浏览器体验稳定 | `.toos` 扫描、列表、缩略图、灯箱、失败重试、元数据显示明确；原文件不被改写，缓存原子落盘 |
| 外部接入可维护 | 所有适配器复用同一个 C ABI 和 Python 参考路径，不在插件中复制 DCT、量化、熵编码或色彩语义 |
| 可演进 | TPIM envelope major/minor 与 TPIC packet 版本独立；未知 optional chunk 可保留，未知 critical chunk 明确失败 |

### 2.2 约束

1. `.toos` 是当前正式图片扩展名；不得重新引入 `.tpic`/`.topos` 兼容别名。
2. codec 的平面顺序固定为 YUV=`Y,U,V`，GBR=`G,B,R`，Alpha 为 plane 3。
3. 文件 metadata 不能覆盖 TPIC 对样本数值解释的 range/matrix/pixel format；ICCP/OCIO 只在 RGB 重建之后生效。
4. 不得在 file explorer 中把原始 `.toos` 当作浏览器可直接显示的图片；必须通过可验证的解码预览路径。
5. 所有速度数字必须注明硬件、OS、构建类型、缓存状态、素材、线程数和 p50/p95/p99。
6. 优化先有标量参考实现和回归测试，再做 SIMD、并行、mmap、GPU 或平台特化。
7. 每个阶段完成后提交代码、测试、基准和文档；未通过验收不得进入依赖它的阶段。

---

## 3. 阶段总览和依赖关系

| 阶段 | 名称 | 主要产出 | 依赖 | 完成门槛 |
| --- | --- | --- | --- | --- |
| O0 | 基线、指标和测试素材冻结 | benchmark runner、质量 corpus、指标 JSON schema、现状报告 | 当前基线 | 所有后续数据可复现 |
| O1 | 色彩、Alpha 和 metadata 语义收口 | 统一转换 API、优先级规则、参考图和误差门禁 | O0 | CPU/GPU/file explorer 参考结果一致 |
| O2 | 专业画质 Profile 和无损边界 | Lossless/Visual-Lossless 规范、档位质量门槛 | O0、O1 | 每个 Profile 有可验证质量报告 |
| O3 | native 缩放解码和 file explorer 预览优化 | target-size decode、缓存复用、原子预览链 | O0、O1 | 预览首帧和内存显著下降，画面无色彩回归 |
| O4 | codec 解码吞吐、并行和内存优化 | slice worker、SIMD、buffer pool、分项 profiling | O0、O1 | release/真实素材达到目标，旧 golden 零回归 |
| O5 | ROI/tile/超大图扩展评估与实现 | 若门槛成立则实现 TILE/TDIR；否则形成明确 deferred 记录 | O3、O4 | 8K/局部检视不超内存预算 |
| O6 | 图片序列长时稳定性和编辑器接线 | 1000+ 帧、跳帧、反向、取消、NAS 矩阵 | O3、O4 | 序列压力测试和错误语义全部通过 |
| O7 | C ABI、Python SDK、CLI 和元数据工具收口 | SDK、inspect/verify/repair、示例和 ABI smoke | O1、O2、O4 | 多平台构建和稳定错误码通过 |
| O8 | 外部软件生态 | OIIO、FFmpeg、QuickLook、Resolve/Premiere/Nuke 适配路线和最小实现 | O7 | 至少一个外部宿主完成读写往返 |
| O9 | 分发、性能发布报告和版本冻结 | 安装包、签名/公证、能力矩阵、发布说明 | O1–O8 | 发布清单和回滚方案完整 |
| V0 | 视频基线和 bitstream 版本冻结 | 确认帧内中间编码定位、视频帧结构、未来 GOP/参考帧预留和视频基准 corpus | O0 | 当前 profile 语义清楚，未来 V3 不与中间流程承诺冲突 |
| V1 | 视频共享色彩/Alpha/容器语义 | MOV metadata、时间戳、Alpha、CICP/ICC/OCIO 和图片路径统一 | O1、V0 | 单帧视频与 `.toos` 参考结果一致 |
| V2 | 视频帧内方向预测 | 4/8/16/32 像素块方向模式、候选模式和模式信令 | V0、O1 | 同画质帧内码率下降，解码开销有上限 |
| V3 | 后续视频帧间预测和参考帧 | P/B 帧、运动估计/补偿、残差、DPB、GOP 和场景切换 | V2、O4 | 作为后续产品线；随机访问、错误恢复、质量和实时播放门禁通过 |
| V4 | 视频自适应熵编码 | range/CABAC 类 context、level/run 语法、概率更新和 checkpoint | V2（inter context 可在 V3 后扩展） | 当前帧内 profile 码率下降且解码稳定，未来 inter 可复用 |
| V5 | 后续视频码控和质量分配 | GOP 级复杂度、VBV/HRD、场景切换、AQ/RDO、QP 地板和目标码率 | V3、V4 | 仅在 inter/GOP 产品线启用，码率误差和质量波动受控 |
| V6 | 视频编码/解码吞吐优化 | 当前帧内任务图、SIMD、内存池、零拷贝；未来扩展跨帧任务和 DPB | V2、V4、O4；inter 子阶段依赖 V3 | 当前中间编码实时门禁先通过，未来 inter 不降低解码体验 |
| V7 | 视频容器和编辑器接线 | 当前独立 sample、时间戳、metadata、图片序列；未来增加关键帧索引和 GOP seek | V1、O6、O7；inter 子阶段依赖 V3/V5 | 当前中间流程稳定，未来长视频 seek/错误恢复再扩展 |
| V8 | 视频外部生态和插件 | FFmpeg、Resolve、Premiere、Topos Player、素材管理器和 SDK | V6、V7、O8 | 先支持当前帧内 profile；inter 能力按 capability 协商 |
| V9 | 视频发布和迁移 | 当前中间编码 profile 发布；后续 inter major/profile、能力协商、安装包和回滚 | V1–V8、O9 | 当前 profile 发布矩阵完整，未来 inter 不破坏现有中间流程 |

依赖图：

```text
O0 ─┬─> O1 ─┬─> O2 ─┬─> O7 ─> O8 ─> O9
    │        ├─> O3 ─┬─> O5
    │        └─> O4 ─┴─> O6
    │
    ├─ 当前帧内中间编码：V0 ─> V1 ─> V2 ─> V4 ─> V6(intra) ─> V7(intra) ─> V8 ─> V9
    │                         └────────────共享 O1/O4/O6/O7/O8/O9
    │
    └─ 后续帧间产品线：V0 ─> V2 ─> V3 ─> V4 ─> V5 ─> V6(inter) ─> V7(inter) ─> V8 ─> V9
```

---

## 4. O0：基线、指标和测试素材冻结

**目标：** 让后续任何“更快”“更小”“画质不变”都能由同一套命令复现。

| ID | 任务 | 具体实现 | 文件/模块 | 验收 |
| --- | --- | --- | --- | --- |
| O0-01 | 统一基准命令 | 新增 `scripts/bench_topos_image.py`，统一 probe、validate、read、decode、convert、resize、WebP encode 分项计时 | `scripts/`、`docs/image/benchmark_protocol.md` | 同一输入运行两次，JSON 字段和单位完全一致 |
| O0-02 | 固定指标 schema | 定义 `environment`、`source`、`profile`、`threads`、`cache_state`、`latency_ms`、`throughput_fps`、`memory_peak_mb`、质量指标字段 | `docs/image/benchmark_schema.json` | schema 校验命令通过 |
| O0-03 | 素材 corpus | 固定自然影像、渐变、细线、文字、纹理、噪声、HDR 高光、透明边缘、合成图和动画序列 | `tests/fixtures/topos_image/` | 每类至少一张 1080p、2K、4K；授权和 hash 记录完整 |
| O0-04 | 真实素材基线 | 对真实 2K/4K 图片和 Topos 视频抽帧分别测各档位尺寸、PSNR/SSIM/ΔE、编码/解码速度 | `docs/image/reports/` | 报告同时给 p50/p95/p99 和原始命令 |
| O0-05 | 资源监控 | 记录 CPU 利用率、线程利用率、RSS、峰值分配、I/O 吞吐和缓存冷热状态 | benchmark runner | macOS/Linux/Windows 至少各有一组可复现记录；无平台时明确 skipped |
| O0-06 | 质量参考 | PNG/EXR/DPX 作为参考，不直接把 8-bit PNG 作为全部场景真相；保留源位深和色彩 metadata | corpus manifest | 参考文件和 `.toos` 输入的颜色解释完全写入 manifest |

**O0 门禁：** 没有 corpus、环境信息和分项计时的结果不得写入产品宣传或 capability manifest。

---

## 5. O1：色彩、Alpha 和 metadata 语义收口

**目标：** 先保证“同一个文件在不同消费端看起来是同一张图”，再谈压缩和速度。

| ID | 任务 | 具体实现 | 文件/模块 | 验收 |
| --- | --- | --- | --- | --- |
| O1-01 | 统一 metadata precedence | 将 TPIC 数值解释、CICP、ICCP、OCIO 的优先级固化成单一 API；禁止各宿主自行猜 sRGB | `src/shared/media/color_decode.py`、`src/shared/color/types.py`、`native/topos_codec/src/image/` | 相同输入在 Python、CLI、file explorer 结果一致；冲突 metadata 返回明确错误 |
| O1-02 | 完整矩阵/范围映射 | 覆盖 BT.601、BT.709、BT.2020 NCL、full/limited、GBR identity、chroma siting；为未知值建立失败语义 | `color_decode.py`、C image reader | 合成色条、灰阶、纯红绿蓝、边界码值误差在规格内 |
| O1-03 | Transfer 函数 | 为 SDR、linear、PQ、HLG 建立 decode-to-display 和 display-to-linear 两个明确 API；不得把显示 Tone Mapping 混进 codec decode | `src/shared/media/color_decode.py`、`src/shared/media/topos_image_source.py` | PQ/HLG 测试用绝对亮度或参考转换器校验；SDR 不回归 |
| O1-04 | ICC/OCIO 应用边界 | ICC/OCIO 只在数值重建为 RGB 后生效；记录使用的 colorspace、版本和失败原因 | `src/shared/media/`、CLI metadata | ICC/OCIO 缺失、冲突、不可用时行为可诊断；不静默变 sRGB |
| O1-05 | Alpha 语义矩阵 | 覆盖 A16 bit-exact、受限 Alpha、straight、premultiplied、Alpha=0 保留 RGB、线性边界 unpremultiply | `native/topos_codec/`、`src/shared/media/`、tests | 透明边缘合成、白底/黑底、HDR 边缘和多代 round-trip 通过 |
| O1-06 | 窗口和方向 | 验证 display/data window、负原点、pixel aspect、orientation、odd dimensions；明确 preview 是否裁剪或加边 | image source、file explorer provider | 所有字段在 CLI、Python、预览和导出中保持一致 |
| O1-07 | 参考转换器 | 保留慢但清晰的 scalar reference path，作为 SIMD/GPU 结果的真相源 | `tests/media/`、`src/shared/media/` | 随机样本和边界样本与优化路径误差有固定上限 |
| O1-08 | 色彩回归报告 | 对每个 profile 生成 RGB/Alpha/ΔE/亮度误差报告并登记源 hash | `docs/image/reports/color_*.md/json` | 报告可由一条命令重新生成 |

**O1 质量门槛：**

- 8/10/12-bit 样本的存储值解释不漂移；
- Alpha mode 1 对合法输入 bit-exact；
- Visual preview 允许 8-bit 显示量化，但不得改变色彩 metadata 解释；
- PQ/HLG 不能以“看起来差不多”作为通过条件，必须有参考曲线误差。

---

## 6. O2：专业画质 Profile、压缩档位和无损边界

**目标：** 把“视觉无损”“高质量有损”“数学无损”分成可验证的产品语义。

| ID | 任务 | 具体实现 | 文件/模块 | 验收 |
| --- | --- | --- | --- | --- |
| O2-01 | Profile contract | 为 Preview、HQ、XQ、Lossless 写清 pixel format、bit depth、alpha、QP、qmatrix、entropy、用途和禁止事项 | `docs/image/capability_manifest.json`、`docs/image/SDK.md` | CLI、Python、文档三处参数一致 |
| O2-02 | Visual-lossless 质量门槛 | 用自然、纹理、细线、文字、HDR、透明边缘 corpus 测 PSNR/SSIM/ΔE2000；每个档位记录最差样本而非平均值 | benchmark runner | 质量阈值和失败样本写入 JSON；不能只报告单帧平均 |
| O2-03 | Lossless 设计评估 | 设计独立可逆 Profile：可逆预测/颜色变换、Rice/raw escape、bit-exact；若码率或速度不合格，保留 reserved 并明确不支持 | native codec + spec | 全部合法输入 bit-exact；不能把 qp28 特例冒充 Lossless Profile |
| O2-04 | 档位体积稳定性 | 在 1080p/2K/4K 和高熵/低熵 corpus 上测 MB/帧、bits/pixel、PSNR/ΔE；检查 Low/Medium/High/Ultra 单调性 | `docs/image/reports/tier_*.md` | 档位顺序稳定；异常素材单独解释，不改写数据 |
| O2-05 | AQ/RDO 参数边界 | 把 AQ/RDO 作为可测试策略开关，测编码时间、质量、体积；记录解码侧是否完全不受影响 | `src/shared/export/topos_encoder.py`、native encoder | 开关语义和默认值稳定；没有未记录的隐式质量变化 |
| O2-06 | 质量预算策略 | 规定颜色、Alpha、metadata、容器开销各自预算；Alpha 无损超预算时提示或失败，不静默降级 | native image writer、CLI | 预算报告与文件实际大小一致 |

**O2 交付物：** 更新 capability manifest、SDK 支持矩阵、质量报告、CLI `--profile/--tier` 回归、格式规范中的 lossless 章节。

---

## 7. O3：native 缩放解码和素材管理器预览优化

**目标：** 让素材管理器在不牺牲原文件质量的前提下，快速显示适合当前 UI 尺寸的预览。

| ID | 任务 | 具体实现 | 文件/模块 | 验收 |
| --- | --- | --- | --- | --- |
| O3-01 | target-size decode API | 增加 `tc_image_query_decode_buffer(... target_size ...)` 或等价 preview API；明确是 codec 内缩放、解码后缩放还是抽样解码 | `native/topos_codec/include/topos_image.h`、`src/shared/codec/topos_image_binding.py` | API 文档说明精度和色彩语义；无目标尺寸时保持现有结果逐字节一致 |
| O3-02 | native thumbnail API | 提供直接输出 RGB8/RGBA8 或目标 planar 的缩略图入口，避免 Python 解码完整 4K 再缩小 | native image layer、Python binding | 预览路径峰值 RSS 和耗时显著下降；颜色与 O1 参考一致 |
| O3-03 | provider 复用解码 | `external/file_explorer/plugins/topos_image/topos_image_media.py` 复用小图/大图/preview 的单次 decode 结果；同一请求批量生成时不重复解码 | file explorer plugin、thumb engine | 单次任务可生成三档或明确按需生成；缓存命中不再解码 |
| O3-04 | 缩略图缓存一致性 | 统一 `.v` sidecar、源 mtime/size/hash、失败缓存、`.part` 原子替换和重试 | `external/file_explorer/thumb_engine.py`、`thumbs.py` | 中断、磁盘满、源文件替换后不会返回半成品或旧图 |
| O3-05 | 预览色彩模式 | UI 明确区分“快速预览”和“专业查看”；快速 WebP 可 8-bit，专业查看预留 native/GPU 入口 | `fe-state.js`、`fe-render.js`、plugin | UI 不把 8-bit WebP 宣称为原始位深监看 |
| O3-06 | Alpha 预览 | RGBA WebP、透明棋盘或背景色切换，验证 straight/premultiplied 只在显示边界处理一次 | file explorer、shared color | 透明边缘无黑边/白边；无 Alpha 文件不增加无意义通道 |
| O3-07 | 错误诊断 | Topos 库缺失、版本不支持、损坏、metadata conflict 分别显示可读错误并允许重试 | plugin、routes、frontend | 失败不会卡住列表；原文件仍可下载/定位 |
| O3-08 | 浏览器/原生显示路线评估 | 评估 Chromium 8-bit WebP、QImage 16-bit、Metal/OpenGL texture、WASM decoder 四条路线 | `docs/image/preview_architecture.md` | 选定一条专业查看路线并记录未选方案原因 |

**O3 目标指标：** 以 O0 基线为分母，真实 1080p/4K 图片小图首帧 CPU 时间降低至少 40%，峰值 RSS 降低至少 30%；未达到时保留分项数据并继续定位，不通过调低质量掩盖问题。

---

## 8. O4：codec 解码吞吐、并行和内存优化

**目标：** 优化完整解码和序列吞吐，而不把素材管理预览的 WebP 时间混入 codec 数字。

| ID | 任务 | 具体实现 | 文件/模块 | 验收 |
| --- | --- | --- | --- | --- |
| O4-01 | profile-guided profiling | 对 probe/read/entropy/transform/quant/inverse/color/plane copy 分项采样；保留高熵最坏输入和真实输入两组 | native benchmark、Instruments/perf | 先有火焰图和热点排序，禁止凭感觉改代码 |
| O4-02 | plane buffer pool | 复用 caller-provided plane、scratch、bitstream 和 RGB 转换缓冲；明确线程所有权和清理路径 | C ABI、Python binding、sequence cache | 多帧循环 RSS 稳定，无 use-after-free/数据竞争 |
| O4-03 | slice 并行 | 固定线程预算、slice 粒度和调度策略；避免每帧创建线程；实现取消与错误传播 | native codec、`ToposImageSequenceSource` | 8 核 16 线程利用率可观测；低分辨率不因并行变慢 |
| O4-04 | SIMD 路径 | 在 scalar reference 通过后补 AVX2/NEON/平台 dispatch，关注 transform、quant、scan、bit IO | `native/topos_codec/src/` | 各平台与 scalar 误差/bitstream 规则符合 spec；无未对齐读写问题 |
| O4-05 | I/O 策略 | 比较 pread、buffered read、mmap、异步预读、NAS 冷缓存；不默认启用没有实测收益的 mmap | image reader、benchmark | 本地 SSD、外接盘、NAS 分别有结论；尾延迟写入报告 |
| O4-06 | 解码取消 | 序列源在 seek/切目录/关闭时取消旧 generation，native 层只通过有界检查点响应 | sequence source、decode worker | 旧请求不会覆盖新帧；关闭无后台 native 任务泄漏 |
| O4-07 | 速度/质量双门禁 | 任何 SIMD、并行或内存优化都必须同时跑 golden、fuzz、质量 corpus 和基准 | `scripts/run_tests.sh`、CI | 性能上涨但画质或错误语义回归时拒绝合入 |

**O4 基线参考：** 当前高熵 Debug 报告中 1080p 完整 decode p50 约 96.5 ms，4K 约 381.9 ms；该数字不是产品承诺。O4 必须新增 release build、真实素材、线程数和冷/热缓存结果后，再确定下一组目标。

---

## 9. O5：ROI、TILE 和超大图片评估/实现

**目标：** 只有在 O3/O4 数据证明整帧解码仍不足时，才扩展格式结构；避免为没有收益的复杂度提前改 major version。

| ID | 任务 | 具体实现 | 文件/模块 | 验收 |
| --- | --- | --- | --- | --- |
| O5-01 | 使用场景分析 | 统计 file explorer、Viewer、合成和纹理上传实际需要的区域大小、缩放比例、跳转模式 | benchmark/report | 有真实请求分布，证明 ROI 的收益 |
| O5-02 | TILE/TDIR 草案 | 设计 tile 尺寸、边界、tile directory、独立 CRC、色彩/Alpha 跨 tile 语义和 major version 规则 | `docs/image/topos_image_file_spec_v1.md` | 能描述坏 tile、缺 tile、顺序读取和随机读取行为 |
| O5-03 | 单 tile 参考实现 | 先实现 scalar writer/reader 和小图 golden，不接 UI | native image layer | 单 tile 与整帧结果一致；坏 tile 错误有界 |
| O5-04 | 8K/大图内存门禁 | 测 4K、8K、超宽全景、透明大图的峰值 RSS、临时分配和失败诊断 | benchmark、fuzz | 不超过规格预算；超限不 OOM 崩溃 |
| O5-05 | 结论分支 | 若 target-size decode 已达标，O5 可标记 deferred；若 ROI 通过收益门槛，再提升 file major 并实现 | ADR | 有书面决策，不以“以后再说”结束 |

**建议默认：** 在 target-size decode 和缓存复用完成前，不启动 TILE major version 实现。

---

## 10. O6：图片序列长时稳定性和编辑器接线

**目标：** 让 `.toos` 序列在编辑器、素材管理器和外部导出中稳定支持长序列。

| ID | 任务 | 具体实现 | 文件/模块 | 验收 |
| --- | --- | --- | --- | --- |
| O6-01 | 1000+ 帧压力测试 | 连续、随机、反向、跳帧、重复请求、缺帧、损坏帧、末尾截断、冷缓存/NAS | `tests/media/`、`docs/image/reports/` | 无死锁、缓存有界、错误策略 skip/error/hold 一致 |
| O6-02 | DecodeQueue 接线 | 将 `ToposImageSequenceSource` 接入现有优先级、取消、预取和 playback clock 模型 | `src/shared/media/`、编辑器播放层 | 播放、拖动、反向和切片段不显示旧 generation 帧 |
| O6-03 | 序列导出配置 | 统一 profile/tier、文件命名、padding、元数据继承、Alpha 和失败恢复；避免每帧重新创建 codec | `src/shared/export/topos_encoder.py`、UI | 重新导出可恢复；同输入配置生成确定性输出 |
| O6-04 | 素材管理序列视图 | 将连续 `.toos` 识别为序列，提供首帧、代表帧、序列数量和缺帧提示 | `external/file_explorer/routes.py`、frontend | 单帧仍可独立预览；序列不会污染普通图片排序 |
| O6-05 | 视频抓帧统一接口 | 继续使用 `extract_topos_frame_to_toos()`，加入批量抽帧、覆盖策略、时间戳和元数据继承 | `src/shared/media/topos_frame_extract.py` | 抽帧无需 decode/reencode；输出 `.toos` 可被 file explorer 直接预览 |
| O6-06 | 性能指标 | 测序列吞吐 fps、首帧延迟、seek p95、缓存命中率、峰值 RSS、磁盘读放大 | benchmark | 报告区分编码、读取、解码和显示，不把 UI 时间混为 codec 时间 |

---

## 11. O7：C ABI、Python SDK、CLI 和元数据工具收口

**目标：** 让外部宿主只依赖稳定 API，而不是复制内部实现或解析私有结构。

| ID | 任务 | 具体实现 | 文件/模块 | 验收 |
| --- | --- | --- | --- | --- |
| O7-01 | ABI versioning | 明确 struct_size、abi_version、optional tail、能力查询和错误码稳定策略 | `native/topos_codec/include/topos_image.h` | 新旧 caller 在 capability 范围内可运行；不支持返回明确错误 |
| O7-02 | C++/Rust bindings | 为 probe、decode、write、metadata、sequence session 提供薄封装；不复制 codec | `sdk/` 或现有 bindings | 示例程序可编译并完成 probe/decode/write 往返 |
| O7-03 | Python SDK | 将 `ToposImageCodec`、`ToposImageSource`、序列、frame extract 的公开接口写清输入、输出、所有权和异常 | `docs/image/SDK.md` | 示例代码在干净环境运行；错误可区分格式错误、库缺失、I/O 错误 |
| O7-04 | CLI inspect/verify/repair | `toos probe`、`verify --deep`、`decode`、`encode`、`metadata`、可选安全修复；repair 不覆盖原文件 | `native/topos_codec/src/cli/image_cli.c`、`scripts/toos_tools.py` | 损坏文件有稳定诊断；修复文件重新 verify 通过 |
| O7-05 | Magic/MIME/平台关联 | `.toos` 扩展名、TPIM magic、MIME、macOS UTI、Windows ProgID、Linux desktop entry | `docs/`、安装包脚本 | 扩展名错误但 magic 正确时能诊断；危险伪装不被当作普通图像 |
| O7-06 | ABI smoke matrix | macOS arm64/x86_64、Linux x86_64、Windows x64（可用时）测试加载、符号、probe、decode、write | CI/build scripts | 平台状态明确；没有“未测却标 supported” |

---

## 12. O8：外部软件生态路线

**目标：** 通过一个稳定 SDK 让外部应用读写 `.toos`，而不是维护多套不一致的解析器。

| 优先级 | 适配器 | 第一阶段能力 | 第二阶段能力 | 验收 |
| --- | --- | --- | --- | --- |
| P0 | OIIO | `ImageInput`/`ImageOutput`、metadata、YUV/GBR、Alpha、10/12-bit | ROI、tile、multi-view（若格式支持） | `oiiotool` probe/read/write/compare 往返；无 SDK 时 stub 语法门禁 |
| P0 | FFmpeg | image2 demuxer/muxer 或专用 codec wrapper，至少支持单帧和序列 | filter graph 色彩 metadata、硬件路径 | `ffmpeg -i input.toos` 能转 PNG/ProRes，颜色和 Alpha 报告通过 |
| P1 | QuickLook/系统缩略图 | Finder 快速缩略图和 UTI 注册 | 原生 16-bit/HDR preview | 缩略图不调用整套 GUI，不阻塞 Finder |
| P1 | DaVinci Resolve | 图片序列导入/导出或 SDK 适配器，保留色彩/Alpha metadata | 原生单文件读写、GPU decode | Resolve round-trip 和 timeline playback 报告 |
| P1 | Premiere Pro | Importer/Exporter 或 image sequence workflow，明确插件进程 ABI | GPU/缓存优化 | PR 导入、导出、重开、Alpha 和色彩检查 |
| P2 | Nuke | Reader/Writer、frame range、metadata、channel 映射 | 多层/AOV（需新格式扩展） | Nuke read/write compare；不把多层能力伪装成 v1 |
| P2 | Photoshop/AE | 先支持序列或渲染插件工作流 | 原生图层/合成结构（独立立项） | 只在 API 和授权可用后进入，不阻塞 codec v1 |

**生态规则：** 外部适配器只使用公开 C ABI/SDK；所有颜色、Alpha、窗口、错误和版本语义以格式规范为准。

---

## 13. O9：分发、发布和回滚

| ID | 任务 | 具体实现 | 验收 |
| --- | --- | --- | --- |
| O9-01 | 能力清单 | 更新 `capability_manifest.json`、SDK、README、file explorer 文档和插件说明 | 文档中的每个 supported 都有测试证据 |
| O9-02 | 原生库打包 | macOS arm64/x86_64、Linux、Windows 的 dylib/so/dll、rpath、资源路径和 fallback | 干净机器运行 probe/decode/thumbnail |
| O9-03 | 安装签名 | macOS 公证、Windows 签名、Linux 包校验和版本信息 | 安装包可验证，失败时可诊断 |
| O9-04 | 发布基准 | 生成真实素材质量、码率、编码、解码、预览首帧、内存和线程利用率报告 | 报告带 commit、环境、命令、素材 hash |
| O9-05 | 回滚策略 | 保留旧 major reader、独立 capability gate、插件禁用开关和缓存失效策略 | 新版本读取失败时不会破坏原文件或缓存 |

---

## 14. 统一测试和验收矩阵

### 14.1 正确性

| 类别 | 必测内容 | 通过标准 |
| --- | --- | --- |
| 文件结构 | bad magic、截断、重叠 directory、越界 offset、重复 chunk、CRC 错误、未知 critical | 稳定错误码；不崩溃、不超额分配 |
| 样本值 | 0、最大值、mid-gray、纯色、梯度、噪声、细线、奇数尺寸 | scalar reference 和优化路径误差在规格内 |
| 色彩 | BT.601/709/2020、full/limited、SDR/PQ/HLG、ICC/OCIO conflict | 参考转换器和消费端结果一致 |
| Alpha | A16、bounded、straight/premultiplied、透明边缘、Alpha=0 RGB 保留 | bit-exact 或报告最大误差 |
| 版本 | major/minor、未知 optional/critical、TPIC 独立版本 | 不兼容明确拒绝；兼容扩展可读取 |
| 兼容 | 旧 `.toos` golden、视频 TPIC golden、图片序列 | 旧门禁零回归 |

### 14.2 性能

必须同时记录：

- probe、validate、文件读取、codec decode、色彩转换、缩放、WebP 编码；
- p50、p95、p99、max、吞吐 fps、MB/s；
- 单线程、8 核 16 线程、限制线程数；
- Debug/Release、冷缓存/热缓存、本地 SSD/外接盘/NAS（可用时）；
- 峰值 RSS、临时分配、缓存命中率、CPU 利用率和 I/O 利用率。

建议的阶段门槛：

| 路径 | O3/O4 目标 |
| --- | --- |
| file explorer small thumbnail | 相对当前真实素材基线 CPU 时间降低 ≥40%，峰值 RSS 降低 ≥30% |
| file explorer preview thumbnail | 不因提高 preview 尺寸而重复解码同一源；缓存命中不读源文件 |
| full image decode | Release build 在真实素材上 CPU 时间降低 ≥30%；高熵最坏输入单独报告 |
| 1000+ 帧序列 | 缓存有界、无旧 generation 覆盖、seek p95 和首帧延迟可复现 |
| 编码 | 质量指标不下降的前提下，分别报告 AQ/RDO 开关和线程扩展效率 |

### 14.3 file explorer 专项

| 场景 | 结果 |
| --- | --- |
| 扫描 `.toos` | 分类为 image，列表可见，扩展名大小写不影响 |
| small/large/preview | 三档生成、缓存、sidecar、源替换失效和重试正确 |
| 灯箱 | 双击、键盘导航、缩放、透明背景、失败提示正常 |
| 元数据 | 分辨率、位深、格式、Profile、Alpha 信息可见；损坏文件不拖垮列表 |
| 兼容缺库 | 插件可加载；单个 `.toos` 明确显示 Topos runtime 缺失 |
| 并发 | 双 worker、不重复任务、关闭时无 native 线程泄漏 |

---

## 15. 风险和处理策略

| 风险 | 触发信号 | 处理 |
| --- | --- | --- |
| HDR 预览“看起来对”但数值错误 | PQ/HLG ΔE 或亮度误差超限 | 立即回到 O1 参考转换器；禁止只调 Tone Mapping 参数掩盖 |
| 并行解码收益小 | CPU 利用率低、线程扩展效率低于 1.3x | 先分析 slice 粒度和内存带宽，再决定是否增加线程 |
| 目标尺寸解码改变画质 | 小图边缘、细线、色度抽样出现差异 | 保留完整解码 fallback；target-size API 先以参考路径对照 |
| ROI/TILE 使格式复杂度失控 | writer/reader、fuzz 和 metadata 规则大幅增加 | O5 标记 deferred，优先完成 full-frame 稳定路径 |
| 外部 SDK ABI 漂移 | 插件在不同构建加载失败 | struct_size/abi_version、capability query 和 ABI smoke 强制门禁 |
| 原生库未随安装包 | file explorer plugin active 但预览失败 | 安装包启动自检、明确日志、可用版本路径和用户可见诊断 |
| 有损档位被称为无损 | 质量报告只有 PSNR 或单帧结果 | 文档强制分离 Lossless、Visual-Lossless、Lossy 三种词汇 |
| 长序列缓存增长 | RSS 随帧数线性增加 | 字节预算、LRU、generation cancel、1000+ 帧压力门禁 |

---

## 16. 每阶段提交模板

每个 O 阶段至少产生一个提交，提交内容必须包含：

```text
<type>(topos-image): <阶段结果>

变更：
- 实现的 API/模块
- 更新的格式或产品语义
- 兼容性影响

验证：
- 运行的命令
- 通过/跳过/失败数量
- 性能和质量报告路径

门禁：
- golden / fuzz / ASan / UBSan
- 真实素材 hash 和环境
- 已知 deferred 项
```

推荐提交顺序：

| 提交 | 对应阶段 | 允许进入下一阶段的条件 |
| --- | --- | --- |
| `docs(image): freeze optimization baselines` | O0 | benchmark schema、corpus、基线报告可复现 |
| `feat(image): unify color and alpha semantics` | O1 | 色彩/Alpha/metadata 参考测试通过 |
| `feat(image): define quality and lossless profiles` | O2 | Profile manifest、质量报告和 CLI 回归通过 |
| `perf(image): add target-size preview decode` | O3 | file explorer 预览和内存门槛通过 |
| `perf(codec): optimize image decode throughput` | O4 | 性能提升且 golden/fuzz/质量零回归 |
| `feat(image): add roi tile support` 或 `docs(image): defer roi tile` | O5 | ROI 收益门槛有数据结论 |
| `feat(image): harden long sequence playback` | O6 | 1000+ 帧压力和取消语义通过 |
| `feat(sdk): publish stable Topos Image API` | O7 | ABI smoke 和示例程序通过 |
| `feat(interop): add <adapter>` | O8 | 至少一个外部应用往返通过 |
| `release(image): package and publish capability matrix` | O9 | 干净机器、签名和回滚验证通过 |

---

## 17. 图片侧完成定义（O0–O9）

只有同时满足以下条件，才可以把 `.toos` 宣称为“专业图片格式完成”：

1. `.toos` 文件结构、版本、错误码、Alpha、色彩 metadata 和 Profile 语义已冻结；
2. Lossless、Visual-Lossless、Lossy 的词汇和测试标准没有混用；
3. CPU 参考路径、优化路径、file explorer 预览和 Topos Color 消费路径色彩结果一致；
4. 1080p、2K、4K 真实素材和最坏输入都有质量、速度、内存和码率报告；
5. file explorer 支持列表、缩略图、灯箱、元数据、失败诊断和缓存失效；
6. 1000+ 帧序列通过播放、seek、反向、取消、缺帧和关闭压力测试；
7. C ABI、Python SDK、CLI 和至少一个外部适配器完成读写往返；
8. macOS、Linux、Windows 的支持状态明确，未验证的平台不会标记为 supported；
9. 安装包包含正确的原生库、版本检查、签名/公证和回滚路径；
10. 每个已完成阶段都有代码、测试、基准、文档和独立 Git 提交。

在上述条件完成前，`.toos` 可以作为 Topos Color 和 AssetDeck 的开发中专业图片格式使用，但不应宣称已经全面替代 OpenEXR、DPX、ProRes 4444 或所有 NLE/VFX 软件的原生图片管线。

## 18. Topos 视频优化详细实施计划（V0–V9）

视频与 `.toos` 图片共用同一个帧编码核心，因此方向预测、变换量化、色彩/Alpha 语义和自适应熵编码必须尽量落在 `native/topos_codec` 的共享层。当前视频明确定位为剪辑/调色中间流程使用的帧内编码：独立 TPIC 帧包带来低延迟、易解码、易抽帧和稳定 seek。帧间预测、参考帧、GOP、跨帧码控和更复杂的关键帧语义保留在后续产品线，不作为当前帧内中间编码的完成门槛。

### 18.1 V0：视频基线、格式版本和实验矩阵

| ID | 任务 | 具体实现 | 文件/模块 | 验收门槛 |
| --- | --- | --- | --- | --- |
| V0-01 | 记录当前纯帧内事实 | 对 `tc_frame_encode`、`tc_frame_encode_sized`、MOV sample 写入和 `tc_frame_decode` 画出调用链；明确当前没有 motion vector、参考帧、DPB、P/B、GOP | `native/topos_codec/src/codec/`、`native/topos_codec/src/mov/`、`src/shared/export/topos_encoder.py` | 文档和源码证据一致，任何性能报告都标注 all-intra |
| V0-02 | 冻结实验矩阵 | 固定 422/444、10/12-bit、2K/4K、24/30/60 fps、静态/平移/快速运动/切镜/闪光/噪声/HDR/Alpha 素材 | `tests/fixtures/topos_video/`、`docs/video/` | 每类素材有 hash、帧率、色彩和授权记录 |
| V0-03 | 建立视频指标 schema | 记录 file bytes、bitrate、bits/frame、QP、PSNR/SSIM/ΔE、temporal stability、encode/decode p50/p95/p99、首帧、seek、RSS、CPU 利用率和掉帧 | `docs/video/benchmark_schema.json`、`scripts/` | 同一命令可重跑并生成机器可读 JSON |
| V0-04 | 版本和能力协商 | 为 inter/GOP/算术熵设计新的 major bitstream 标识和 capability flags；当前开发阶段可以直接切换，不要求旧项目兼容，但 reader 必须对 profile 能力明确失败 | `native/topos_codec/include/`、`docs/video/` | 版本、profile、entropy、prediction、GOP 能力不会靠隐式猜测 |
| V0-05 | 建立对照组 | 同一素材测当前 Topos all-intra、方向预测 only、inter only、方向+inter、方向+inter+arithmetic，并与 ProRes/DNxHR/H.264 记录尺寸和速度 | `docs/video/reports/` | 每次优化有可回退的对照数据，不能只报单一最好结果 |

**V0 门禁：** 没有 bitstream 版本、素材 hash、质量指标和 p50/p95 速度的结果，不得进入 V2 的帧内优化；V3 是后续 inter 产品线，必须在单独的实验分支和 profile 中推进。

### 18.2 V1：图片与视频共享的色彩、Alpha 和 MOV 语义

| ID | 任务 | 具体实现 | 验收门槛 |
| --- | --- | --- | --- |
| V1-01 | 复用图片参考转换器 | 视频每帧的 pixel format、range、matrix、transfer、primaries、ICC/OCIO、Alpha straight/premultiplied 与 `.toos` 使用同一套优先级和校验 | 同一帧抽成 `.toos` 后，与视频解码得到的平面值和 RGB 结果在误差门内一致 |
| V1-02 | 帧 metadata 继承 | 定义容器级默认值、sample/frame 覆盖值和关键帧重新声明规则，禁止每帧重复写冗余 ICC | metadata 继承、覆盖和缺失字段都有 golden |
| V1-03 | MOV 时间语义 | 统一 timebase、PTS/DTS、duration、SAR、帧率、旋转、HDR/Alpha 标记及 edit list | 长短帧率、变帧率和 23.976/29.97/59.94 录入后无漂移 |
| V1-04 | 单一错误语义 | native、Python、播放器、file explorer 对坏 sample、缺 metadata、未支持 profile 返回可区分错误码 | 错误不会被当作黑帧或静默色彩转换 |

### 18.3 V2：视频帧内方向预测（先拿到共享内核收益）

| ID | 任务 | 具体实现 | 文件/模块 | 验收门槛 |
| --- | --- | --- | --- | --- |
| V2-01 | 预测模式集合 | 在现有 8×8 DCT 和块量化前加入 DC、水平、垂直、对角线等基础模式，稳定后扩展到 8/16/32 模式；不在第一版强行实现 35 模式全搜索 | `native/topos_codec/src/codec/intra.c`、新 `intra_predict.c` | 边界像素、slice 起点、422 色度和 10/12-bit 均有 golden |
| V2-02 | 模式信令 | 为每个块或块组写入 mode、可用邻居 mask 和必要的模式预测；VLC 版本先复用现有 bit writer | `native/topos_codec/src/bitstream/`、spec | 任意块序列可独立解析，截断流不会越界 |
| V2-03 | 快速候选选择 | DC/梯度代价做候选筛选，再对少量模式计算 SATD/SSE；质量档可扩大候选，速度档限制候选数 | encoder config/profile | 快速档与全候选的质量差异有上限，编码时间增长可解释 |
| V2-04 | 共享解码路径 | 解码器按已信令模式生成预测块，仅对残差做 inverse transform；保留标量参考实现，再接 SIMD | `native/topos_codec/src/codec/` | `.toos` 图片和视频 intra sample 结果一致，解码 p95 不超过门槛 |
| V2-05 | 量化与地板复评 | 方向预测后重新标定 QP、矩阵和零系数策略，重新评估当前 QP63 地板；低档位不再靠重复细节撑大文件 | `tc_frame_encode_sized`、profile manifest | 同 PSNR/ΔE 下帧内码率下降，低档位 size gap 明显拉开 |

**V2 门禁：** 先只落地“方向预测 + 现有 VLC”，在静态、边缘、渐变和纹理素材中确认码率收益与解码速度，再进入 inter；如果收益不达标，必须保留报告和原因，而不是直接叠加更多语法。

### 18.4 V3：后续产品线的帧间预测、运动补偿、参考帧和 GOP

本阶段不是当前帧内中间编码的必做项。只有当未来产品需要更低的视频体积，并且可以接受 GOP 依赖、seek 回退、错误传播和更复杂的解码调度时，才启动本阶段；当前 profile 继续保持逐帧独立编码。

| ID | 任务 | 具体实现 | 文件/模块 | 验收门槛 |
| --- | --- | --- | --- | --- |
| V3-01 | 后续 profile 的 GOP 结构 | 先实现 I/P，不急于 B 帧；支持固定 GOP、IDR、场景切换强制 I，低延迟 profile 使用短 GOP | `native/topos_codec/src/video/`、MOV writer | 后续 profile 的延迟、seek 距离和错误传播范围明确 |
| V3-02 | DPB/参考帧 | 建立有界 decoded picture buffer、引用计数、帧号/PTS 映射和内存池，避免把整段视频留在内存 | `decoder_ctx.c`、新 `dpb.c` | 1000+ 帧 RSS 不随序列长度线性增长 |
| V3-03 | 运动估计 | 先做整数像素分层搜索，再按 profile 增加半像素/四分之一像素；使用块 SAD/SATD、搜索范围和 early exit | `motion_search.c`、SIMD kernels | 静态/平移素材有可测收益，快速档搜索开销受限 |
| V3-04 | 运动补偿和残差 | 支持固定 8/16/32 块候选、skip/merge、MV 预测、残差 DCT/量化；与 V2 方向 intra 共用残差和熵接口 | `inter.c`、`residual.c` | P 帧解码 deterministic，边界和色度 MV 不出现条纹 |
| V3-05 | 参考帧策略 | 首版限制参考帧数量，禁止无界链；场景切换、闪光、快速运动和滚动快门需降低引用深度 | encoder/decoder config | 损坏一个 sample 后，错误影响不超过当前 GOP；可从下一个 IDR 恢复 |
| V3-06 | B 帧后置 | 只有 I/P 版本满足 seek、延迟、解码吞吐和编辑器门禁后，才评估双向 B 帧；B 帧不进入低延迟默认档 | spec、rate control | 任何引入 B 帧的收益和额外 DTS/PTS 复杂度有单独报告 |

### 18.5 V4：自适应算术熵编码

| ID | 任务 | 具体实现 | 验收门槛 |
| --- | --- | --- | --- |
| V4-01 | range coder | 实现 deterministic range coder、bit-exact flush/termination、字节对齐和错误检测；不声称与标准 CABAC 兼容 | 标量编码/解码互逆，随机输入和截断输入安全 |
| V4-02 | 上下文模型 | 按 intra/inter、块位置、邻域零值、significant、level、run、MV 差分建立小型 context；slice/关键帧定义 reset 点 | context 状态不会跨独立 sample 泄漏，解码可并行分片 |
| V4-03 | level/run 语法 | 用零游程、level 分层和 escape 码替代目前固定族 VLC；保留 profile 级快速 VLC fallback 便于速度对照 | 稀疏残差和高能量残差都能无歧义解析 |
| V4-04 | 速度优化 | 先用 C 参考实现对比码率，再做查表、批量 renorm、SIMD 和线程边界优化 | 算术路径解码 p95 增长有上限；不能以吞吐下降换取不可见的小收益 |
| V4-05 | 组合实验 | 按“方向预测 → inter → arithmetic”顺序测量边际收益，分别报告码率、质量、编码和解码成本 | 只有在收益/成本比达标时才设为默认 entropy_mode |

### 18.6 V5：后续 inter 产品线的视频码控、质量分配和 QP 地板

V5 依赖 V3 的 GOP 和参考帧，服务后续帧间 profile。当前帧内中间编码仍以逐帧 QP 搜索和稳定的低延迟输出为主，是否启用 V5 由未来产品的目标码率和延迟要求决定。

| ID | 任务 | 具体实现 | 验收门槛 |
| --- | --- | --- | --- |
| V5-01 | GOP 级复杂度 | 汇总 I/P（以及未来 B）帧的预测残差、运动量、纹理和场景变化，分配帧级 bit budget | 同一目标码率下质量波动比逐帧 QP 小 |
| V5-02 | VBV/缓冲模型 | 支持 constant quality、VBR 和目标 bitrate；记录 buffer fullness、瞬时峰值和长期误差 | 目标码率误差、峰值和 buffer 都有可配置上限 |
| V5-03 | AQ/RDO 重新标定 | 方向/inter 残差改变后重新评估 AQ strength、RDO 候选和快速档关闭项；不要假设旧参数仍最优 | 开启/关闭 RDO 的质量、速度和体积都有同源样本报告 |
| V5-04 | 地板和场景切换 | 取消“所有场景共用 QP63 地板”的隐式限制；为静态背景、复杂运动和切镜设 profile 范围 | 422 Low/Medium 不再因地板导致码率接近；不出现不可接受的块效应 |
| V5-05 | 可选两遍分析 | 对离线导出提供低成本 first pass 复杂度统计；实时模式只使用 lookahead 窗口，不强制两遍 | 离线质量收益与额外时间透明可见，实时档无隐藏全片扫描 |

### 18.7 V6：编码/解码吞吐、8 核 16 线程和实时播放

V6 分成两条子路线：先优化当前帧内中间编码的 slice、SIMD、内存和 I/O；未来 inter 产品线再增加跨帧任务图和 DPB 调度。当前版本的首要门禁是解码稳定、首帧低延迟和实时播放，不能为了未来的 inter 结构提前牺牲这些特性。

| ID | 任务 | 具体实现 | 验收门槛 |
| --- | --- | --- | --- |
| V6-01 | 跨帧任务图 | 编码端把 lookahead、运动搜索、残差、熵编码和写 sample 拆成有依赖的任务；解码端按 DPB ready 状态调度，而不是简单创建每帧线程 | 8 核 16 线程不会因 oversubscription 变慢，任务队列无死锁 |
| V6-02 | 线程预算 | 区分 slice workers、frame workers、I/O workers 和预览转换 workers；提供 1/2/4/8/16 logical thread profile | 记录 CPU 利用率、扩展效率和 p95；默认线程数不压垮宿主 UI |
| V6-03 | SIMD 和内存 | 优化 SAD/SATD、预测、残差、DCT、量化、range renorm、颜色转换；复用 plane/DPB buffer，减少 memcpy | release build 在 2K/4K 真实素材有可重复收益，结果 bit-exact 或在定义误差内 |
| V6-04 | 零拷贝和异步 I/O | MOV sample 写入使用分段 buffer/顺序写；播放器避免“解码→临时 RGB→再拷贝”的重复路径 | 编码端 I/O 不成为瓶颈，解码首帧和稳定播放 RSS 可控 |
| V6-05 | GPU/硬件协同评估 | 先抽象 backend capability；再评估 VideoToolbox/Metal 或其他平台硬件能否承载 Topos 的 bitstream，不把不可验证的硬件支持写成承诺 | CPU fallback 永远可用；硬件路径有一致性、色彩和回退测试 |
| V6-06 | 解码优先门禁 | 对 2K/4K 422、444、10/12-bit 规定 realtime FPS、seek p95、首帧延迟和掉帧策略；编码优化不能降低播放门槛 | 播放器在目标帧率下稳定，解码 p95 和随机 seek 通过报告 |

### 18.8 V7：视频容器、播放、seek 和图片序列统一接口

| ID | 任务 | 具体实现 | 验收门槛 |
| --- | --- | --- | --- |
| V7-01 | 独立 sample/关键帧索引 | 当前 profile 将每个独立 sample 记录为可直接访问；未来 inter profile 再记录 IDR/keyframe、帧号、PTS、byte offset 和 GOP 范围 | 当前 seek 直接定位 sample；未来 inter seek 回退到最近关键帧，索引损坏有重建命令 |
| V7-02 | 解码会话 | 扩展 `ToposMovieSession`/`ToposPlayer`，支持 sequential、random seek、reverse step、cancel、EOF 和 damaged sample | API 语义与 native decoder context 一致，不隐藏全片预解码 |
| V7-03 | 帧提取语义 | 当前 all-intra profile 的任意 sample 都可直接包装为 `.toos`；未来 P/B 帧必须从最近 IDR 解码后生成 `.toos`，接口返回实际成本和源帧 metadata | 当前中间流程保持零拷贝抽帧；未来 inter 不误导用户认为任意帧都能零拷贝提取 |
| V7-04 | 音频和外部 metadata | 明确音频 pass-through、无音频和未来音频 track 策略；保留 timecode、reel、HDR、Alpha、色彩和拍摄 metadata | 导入/导出不会静默丢音频或关键 metadata |
| V7-05 | 错误恢复 | 当前做 sample CRC/长度检查、跳过坏 sample、日志和 UI 诊断；未来 inter 增加跳过坏 P 帧并从下一个 IDR 恢复 | 单个坏帧不会拖垮素材浏览器和长视频播放 |
| V7-06 | 图片序列模式 | 视频模式导出的 Topos 图片序列复用同一 DPB/worker/缓存策略；单张 `.toos` 仍走图片接口 | 序列导出与视频导出速度、码率和画质差异有可解释报告 |

### 18.9 V8：FFmpeg、Resolve、Premiere 和素材管理器生态

V8 先围绕当前帧内中间编码完成稳定导入、导出、预览和图片序列支持；未来 inter profile 通过 capability 协商接入，不能让宿主误判随机 seek、抽帧和延迟语义。

| ID | 任务 | 具体实现 | 验收门槛 |
| --- | --- | --- | --- |
| V8-01 | C ABI/SDK | 对 frame、video session、seek、capability、profile、错误码和线程配置提供稳定 C ABI；Python 只做参考封装 | ABI smoke、示例程序和多线程释放测试通过 |
| V8-02 | FFmpeg | 先提供 demux/decoder，再提供 encoder；映射 timebase、color metadata、keyframe、seek 和 hardware fallback | `ffprobe`/转码/抽帧往返可重复，失败 profile 明确报错 |
| V8-03 | DaVinci/Premiere | 规划 SDK/IO plugin 或中间适配层，支持导入、导出、时间线 seek、音频策略和图片序列模式 | 至少一个宿主完成短片和长片往返；不复制 codec 核心 |
| V8-04 | 素材管理器 | `external/file_explorer/plugins/video_pro` 支持 Topos capability、缩略图、帧预览、seek、错误诊断和 `.toos` 抽帧 | 图片/视频预览入口一致，缓存键包含 bitstream/profile 版本 |
| V8-05 | 发布能力矩阵 | 记录各平台、位深、色彩、Alpha、inter、arithmetic、硬件路径和 NLE 支持状态 | 未验证能力明确标记 experimental/unsupported |

### 18.10 V9：视频 major 版本、发布和回滚

当前发布对象是帧内中间编码 profile；V3 之后的 inter profile 作为后续 major 或明确的能力扩展单独发布。

| ID | 任务 | 具体实现 | 验收门槛 |
| --- | --- | --- | --- |
| V9-01 | 发布版本 | 冻结视频 major、profile manifest、entropy/prediction/GOP flags、最低 reader 能力和错误码 | 新文件可被 capability query 解释，缺能力不会静默解码 |
| V9-02 | 跨平台构建 | macOS/Linux/Windows release、ASan/UBSan、fuzz、长序列和多线程构建矩阵 | 每个平台都有构建、golden、性能和安装包记录 |
| V9-03 | 性能/画质报告 | 发布方向预测、inter、arithmetic、rate control 的逐阶段边际收益，与 ProRes/DNxHR/H.264 对照 | 报告同时包含尺寸、PSNR/SSIM/ΔE、temporal stability、encode/decode p50/p95/p99 |
| V9-04 | 回滚和迁移 | 保留开发期 decoder feature gate、profile downgrade、坏文件诊断和源素材不改写策略 | 失败升级可回到上一 reader，源 MOV 和 `.toos` 不被破坏 |

## 19. 视频专用验收矩阵

| 类别 | 必测内容 | 通过条件 |
| --- | --- | --- |
| 画质 | 静态、平移、快速运动、细线、文字、纹理、噪声、渐变、切镜、闪光、HDR 高光、Alpha 边缘 | 报告 PSNR/SSIM/ΔE、块效应、ringing、闪烁、拖影和场景切换；不能只看单帧 PSNR |
| 压缩 | 当前 all-intra、V2 intra、V4 arithmetic 的帧内逐阶段对照；未来再加入 V3 I/P、V5 rate control | 当前 profile 在同画质下减少 bits/frame；未来 inter profile 再评估跨帧收益，任何阶段都不能以不可接受的 temporal artifact 换取数字 |
| 编码速度 | 2K/4K、422/444、10/12-bit、24/30/60 fps，1/2/4/8/16 线程 | 记录 p50/p95/p99、CPU 利用率、I/O 和 RSS；8 核 16 线程没有过度调度损失 |
| 解码速度 | sequential、随机 seek、反向单步、首帧、冷/热缓存、CPU/GPU 路径 | 达到 profile 规定实时 FPS；seek p95、首帧和掉帧策略均可复现 |
| 长序列 | 当前独立 sample 的 1000+ 帧、变帧率、取消、暂停、EOF、释放重建；未来增加长 GOP | 当前 profile RSS 有界、无死锁、无线程泄漏；未来 inter 再增加 GOP 依赖和恢复门禁 |
| 容错 | 截断 header、坏 CRC、坏 sample、损坏 metadata；未来 inter 增加缺 keyframe/丢 P 帧 | 当前 profile 错误码明确并安全停止或跳过坏 sample；未来 inter 再验证 IDR 恢复 |
| 互操作 | Topos MOV ↔ `.toos`、FFmpeg、Resolve/Premiere、file explorer、Topos Player | metadata、颜色、Alpha、timecode、seek 和音频策略有明确结果 |

## 20. 视频风险、顺序和停止条件

| 风险 | 处理顺序 | 停止/回退条件 |
| --- | --- | --- |
| inter 提升压缩却拖慢解码或 seek | 先 I/P、短 GOP、有限 DPB，再评估 B 帧和更长 GOP | 解码实时门禁失败时回退 GOP/参考帧数量，不牺牲播放器体验 |
| 方向预测增加模式开销 | 先少量候选和 VLC，利用真实素材测边际收益 | 码率收益低于模式信令和解码成本时保留 DC 快速 profile |
| arithmetic coder 省码率但过慢 | 先 C 参考，再 renorm/查表/SIMD；把 entropy_mode 做成 profile | 解码 p95 超门槛时默认回 VLC，算术路径继续作为实验能力 |
| 参考帧错误传播 | IDR、CRC、DPB 引用计数和坏帧恢复优先于极限压缩 | 单坏帧影响超过 GOP 或无法安全 seek 时缩短 GOP/减少引用 |
| 码控使画质跨帧波动 | 先固定 GOP 和 lookahead，再逐步加入 AQ/RDO/两遍 | temporal stability 不达标时禁止发布该 rate-control profile |
| 线程池过度并行 | 按 frame/slice/I/O 分层设预算，使用性能计数器和锁竞争分析 | 8/16 线程扩展效率低于单线程基线收益阈值时减少并行层级 |
| 容器和 codec 语义不同步 | 先冻结 spec、sample index、timebase 和 capability manifest | writer/reader/player 任一方无法解释 profile 时禁止生成文件 |

当前帧内中间编码的实施顺序为：`V0 基线 → V1 共享语义 → V2 方向帧内 → V4 自适应熵 → V6(intra) 解码优先吞吐 → V7(intra) 容器/播放器 → V8/V9 发布`。后续帧间产品线从 `V3 I/P 与 DPB` 开始，再接入 `V5 跨帧码控`、`V6(inter)` 和 `V7(inter)`。每一步都必须保存同一素材的结果，下一步不得覆盖上一阶段的对照组。

## 21. 图片与视频联合完成定义

图片与当前帧内视频 profile 的完成条件如下：

1. `.toos` 的 TPIM/TPIC 结构、视频帧内 profile、entropy、prediction 和 capability flags 已冻结；
2. 图片与视频使用同一套可验证的位深、range、matrix、transfer、primaries、ICC/OCIO、Alpha 和错误语义；
3. 方向预测和 arithmetic entropy 的每一步都有独立 golden、fuzz、质量、码率、编码和解码报告；
4. 当前视频保持逐帧独立 sample，可稳定完成顺序播放、随机抽帧、图片序列导出、取消、EOF 和错误诊断；
5. 2K/4K、422/444、10/12-bit、24/30/60 fps 的真实素材在质量、体积、解码实时性、首帧和 seek p95 上达到当前中间编码 profile 的发布阈值；
6. 8 核 16 线程的编码、解码、I/O 和预览 worker 有明确预算，性能提升来自可复现的 profiling，而不是盲目增加线程；
7. Topos Player、素材管理器、FFmpeg 及至少一个 Resolve/Premiere 等宿主能按 capability 正确导入、预览、导出和抽帧；
8. 单张 `.toos`、Topos 视频帧和视频模式图片序列之间的转换不会静默改变颜色、Alpha、时间戳或位深；
9. macOS、Linux、Windows 的支持状态、硬件加速、ABI、安装包、签名、公证、回滚和源素材保护均有验证记录；
10. 每个 O/V 阶段都有独立 Git 提交、命令、环境、素材 hash、指标 JSON、报告和已知 deferred 项。

后续 inter 产品线的额外完成条件：

1. V3 的 I/P、DPB、GOP、运动补偿、错误传播边界和关键帧索引已冻结；
2. V5 的 VBV、跨帧码控、场景切换和 temporal stability 门禁通过；
3. V6/V7 的随机 seek、IDR 恢复、P/B 帧抽取、长 GOP 内存和解码实时性门禁通过；
4. inter profile 通过独立 major 或 capability 扩展发布，不改变当前帧内中间编码的低延迟和直接抽帧语义。

因此，当前 TOPOS 视频可以以“帧内中间编码”完成和发布；帧间压缩是后续降低体积的产品线，而不是当前定位下必须补齐的缺陷。
