# F5.1 · 输入—工作—显示—输出色彩闭环——台账

> 日期：2026-10-23；基线 commit：`43e00f850`（feat/f-plan）。输入：三路显示变换专项审计（7 节）。
> 执行方案：审计推荐 C（合同化既有等价）+ B 最小切片（CPU 单点化）；A（预览改 CPU provider 全管线）经性能证据否决（1080p numpy 链 200ms+/帧，代码库自身动因记录）。

## 1. 三路语义表（收口后的声明真值）

| 路径 | 语义 | 真值来源 |
|---|---|---|
| Composite viewer | 线性工作 + ODT（transfer/primaries/tonemap/3D LUT bake） | provider（builtin GLSL 已 parity 对齐；OCIO 时 bake 32³ LUT） |
| Edit 视频预览（GPU 主路径） | 编码信号直通（YUV→RGB 矩阵/range 后 clamp 直出；present FBO passthrough） | 无 ODT——PQ/HLG 输入按 SDR 信号显示（发灰），**已知语义** |
| Edit comp_clip / 浮点平面 / 渲染缓存常量帧（CPU 回退） | 场景线性 → sRGB OETF → uint8 | **本批收口**：`shared/color/display_encode.py` 单点 = provider `linear_to_srgb_np` |
| Color 页视频预览 | 信号直通 + RAW 输出空间可覆写（gamma_map 17 选） | 公式与 provider 同常数（合同测试冻结） |
| 导出 | 信号域直通（防二次编码）+ float>1 场景线性高光经 provider OETF（PQ/HLG 目标时）+ comp_clip 线性回读经 provider | provider `transform_color` |

## 2. 本批实现

| 项 | 内容 |
|---|---|
| 单点显示编码 | `src/shared/color/display_encode.py`：`linear_to_srgb_display`（形状契约 (...,3\|4)、>1 硬剪、BGR 直传）+ `linear_bgr_to_srgb_display_u8`；provider 新增公开别名 `linear_to_srgb_np` |
| 三处内联收口 | timeline_preview_renderer comp_clip 回退（:3529）、`_plane_to_uint8`（:5652）、render_cache_worker 常量帧（:197）——三份手写 OETF 删除，改单点调用 |
| 合同测试（15 用例） | `tests/color/test_f51_display_encode_contract.py`：provider 锚点向量（0/拐点双侧连续/0.5→0.7353581/1.0/>1 硬剪）；wrapper vs 独立 oracle（10 样本×3 通道）；BGR 通道序保持；**GLSL 常数四元组源文本合同**（composite_viewport/composite shaders/color shaders/shared color_decode 四处 GPU 拷贝与 provider 常数一致，改即红）；1-D 三元组 + 非法形状显式拒绝；完整管线 vs 直连函数一致（容差=parity 冻结值 1.5e-3） |

## 3. 过程中暴露并处置的真实问题

1. **OCIO provider 形状脆弱**：非 (h,w,c) 形状经 `transform_color` 抛 reshape 异常后**静默回退恒等**（输入=输出，仅日志）——显示编码单点因此直连传输函数并加形状契约显式拒绝；OCIO 恒等回退本身登记为 provider 层已知降级行为（消费者应校验或走 parity 覆盖的 3D 路径），供后续 provider 健壮化任务参考。
2. float32 下 1e-6 容差过紧（幂段 ~1.2e-6 噪声）——合同容差冻结为 2e-6（直连）与 1.5e-3（跨 provider 管线，对齐既有 parity）。

## 4. 登记的债务/后续（不阻塞闭环声明）

- **约 20 份 OETF 拷贝清单**（审计 §7）：本批收口 CPU 热点外三份 + GPU 四处合同锁定；其余（topos_source 缩略图近似、node_thumbnail、color_space.py 等）保留各自缓存/缩略图语义，待触碰时迁移（合同测试保证不漂移）。
- **HDR 预览统一（Edit/Color 的 IDT+tonemap）**：F5.2 候选——抽 composite_viewport 的 bake/upload/process_display GLSL chunk 到 shared，为 Edit/Color 预览加可选 IDT+ODT；`color.preview.*` 偏好驱动三页。审计已确认复用耦合面（私有方法+raw context，需抽模块）。
- PQ/HLG 输入在 Edit 预览按 SDR 显示——现状语义已显式声明（上表），非静默（本台账即声明）；修正归上述 F5.2 候选。
- 真实相机素材验证：scope 锚点测试（PQ 100nits↔0.508、HLG 75%→203nits、code10 64/940/502）已在显示域合同内；真实 PQ 文件端到端预览对拍归 F5.3（RAW/媒体矩阵，有 test_h265_pq.mp4 素材）。

## 5. 测试汇总

色彩域回归 109 passed + 3 skipped；comp_clip/渲染缓存域 78 passed；合同 15 passed。

## 6. 未验证

- Metal（A3 延期）/D3D11（B 延期）路径对拍——按 F0.1 阻塞映射保留待验证行。
- DeckLink 监看输出色彩（F5.2 设备延期）。
