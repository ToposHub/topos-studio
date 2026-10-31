# Topos 原生 2K 预览与 V7-B 完成总计划

**状态：** 重新基线化；V7-B writer 仍为实验性 opt-in，默认写入器保持关闭。  
**日期：** 2026-09-09  
**基线：** `fix-ui` 分支提交 `8a087e6d`，另有未提交的 V7-B transform 试验；后者没有通过门禁，不属于已完成能力。  
**范围：** `native/topos_codec`、TPIC/TOOS、Topos MOV、Python binding、`ToposMediaSource`、预览调度和 GPU 上传。

本文替代“把 V7-B reference mux 当作接近完成”的表述。它把已经交付、已经接入但尚未验收、失败的试验，以及真正能达到目标的后续路径分开记录。只有本文件中标为“已验收”的项目可以作为产品能力宣传。

---

## 1. 固定术语、规格与最终目标

### 1.1 预览基准规格

后续性能报告统一使用下列 16:9 工作规格；这里的 2K 明确是 **1920×1080**，而不是把 2048 宽也混称为 2K。

| 名称 | 源尺寸 | 到 1920×1080 的理想比例 | 用途 |
| --- | ---: | ---: | --- |
| 2K / 1080p | 1920×1080 | 1 | 基线：正常 2K 解码 |
| 4K / UHD | 3840×2160 | 1/2 | 4K→2K |
| 6K（播放基准） | 5760×3240 | 1/3 | 6K→2K 专用路径 |
| 8K / UHD | 7680×4320 | 1/4 | 8K→2K |
| 12K（播放基准） | 11520×6480 | 1/6 | 12K→2K |

`6144×3456`、`12288×6480`、非 16:9、奇数尺寸和非 8 对齐尺寸仍须测试，但归入互操作/边界矩阵；它们不能替代上表的产品性能主表。对 6144×3456，原生 1/3 输出是 2048×1152，不能误报为 1920×1080。

### 1.2 必须实现的产品语义

1. 对新的高分辨率 V7-B 素材，预览读取并解码的主要数据量、熵符号量、逆变换工作和输出内存，都受 1920×1080 基础层上限约束。
2. 4K、6K、8K、12K 的 2K 预览不是先完整解码原图，再 CPU/GPU 缩放；也不是完整读取高频符号后只丢弃像素。
3. 完整解码可恢复与同质量 V2 对等的质量；不能靠大幅降低增强层质量来换取体积数字。
4. 不增加独立 proxy 文件。基础层是同一 V7-B 帧中完整重建所需的低频层；增强层只保存基础层未表达的信息。
5. 旧 V1–V6 和 V7-A 文件永久可读；它们可以使用现有 reduced fallback，但不得称为“2K 工作量”。

### 1.3 不做的承诺

- 不声称复制 Apple ProRes 或 Blackmagic RAW 的私有实现。
- 不声称 Intel/macOS 上所有格式都由专用硬解芯片解码。
- 在质量、体积、编码和端到端预览门禁全部通过前，不把 V7-B 设为默认 writer，也不宣传“任意分辨率接近 2K 解码成本”。

---

## 2. 真实架构结论

### 2.1 已有三条解码路径的能力边界

| 路径 | 可不生成完整源平面 | 可跳过高频熵码字 | 预览工作量受 2K 上限约束 | 状态 |
| --- | ---: | ---: | ---: | --- |
| V1–V6 reduced coefficient decode | 是 | 否 | 否 | 旧流兼容 fallback |
| V7-A coefficient-band reader | 是 | 是 | 否，仍须处理每个源 8×8 block 的低频数据 | 已有参考链 |
| 当前 V7-B spatial base + pixel residual | 是 | 是，base-only 时增强层不读 | 是，**仅 base-only decode** | 参考实现，writer 未过门禁 |
| 目标 V7-B shared transform pyramid | 是 | 是 | 是，且 full 写入不重复编码源像素 | 尚未实现 |

### 2.2 当前 V7-B 为什么不可能通过最终门禁

当前 writer 的工作流是：先把源图下采样并编码为独立 base，再由重建 base 上采样到源尺寸，随后对每个源像素写 enhancement residual。它让 preview 只读 base 的目标已经成立，但写入端仍然额外执行了：

```text
源图 ──下采样──> base 编码
  └─上采样 base──> 每源像素预测误差 ──扫描 / 熵编码──> enhancement
```

这有两个不可绕过的成本：

- enhancement 仍覆盖全分辨率逐像素残差，编码时至少多一次源尺寸扫描和熵编码；
- base 的低频内容与 residual 的低频信息并未由同一组变换系数分割，容易重复表示。

把 residual 统一量化可以压小包体，却会在渐变测试中把全解码 PSNR 从 V2 的约 99 dB 降到约 68 dB；这不是合格的“同质量比较”。把整帧 residual 再作为普通 transform 包编码的试验同样不合格：高频残差依然难压缩，QP 提高后质量骤降。

因此，下一阶段不能继续微调 Rice 参数、预测器或单一量化步长来赌门禁；必须改为**共享分析变换、低频/高频互补分层**的编码架构。

---

## 3. 已完成内容（按证据分级）

### 3.1 已提交且可复现的基础能力

| 范围 | 已完成内容 | 主要证据 |
| --- | --- | --- |
| 固定比例 reduced decode | full、1/2、1/3、1/4、1/8、`AUTO_2K` 请求语义；不创建完整输出平面、限制保留系数、sampled inverse/store | `92f42252`、`3599e5ec`、`5e766579`、`a906887d`；`codec.c`、`color_store.h` |
| V7-A band 码流 | band directory、分带 token、独立 band decoder、公开 reader dispatch | `db681db9`、`68b35ea1`、`9d5b634a`、`6a9ea8b7`、`b41346b2` |
| V7-B base reference | 空间 base、外层目录、base-only/full decode、base range reader、完整 reader dispatch | `c68e260b`、`8f5d3fac`、`e362e895`、`22d84368` |
| MOV / 播放预览接线 | MOV 接受 V7 sample；只取 V7-B base byte range；批量 base range；播放器使用 native preview target | `3fe55f6a`、`5b32d3f5`、`8494bf6c`、`df68aad6` |
| 调度与观测 | V7-B segment/base bytes telemetry、batch decode、残差平面并行、预测器缓存/快速坐标映射 | `f11bad5b`、`cdd05b1d`、`e5d24371`、`e9cd0f8f`、`8a087e6d` |
| 可执行门禁 | V2/V7-B 大小和编码 p50/p95 runner；输出 PSNR-Y；快速开发集和报告生成脚本 | `3c772ca7`、`7db59e5e`、`2461d53b`、`874195ec`；`scalable_encode_gate.c` |

### 3.2 已验证但仅代表局部能力

| 项目 | 已验证结论 | 仍不能据此宣称的内容 |
| --- | --- | --- |
| V7-B base-only decode | 目录读取后可以只读取 base，不读 enhancement；base 可独立解码并缩放至预览目标 | writer 的全码率/全质量经济性 |
| V7-A band skip | 高 band 具有独立长度/CRC，reader 可不读取未请求 band | 8K/12K 解码与 2K 等工作量；B0 仍逐源 block 解码 |
| 固定比例路径 | 逆变换和写回 work 减少，不构造完整输出 | 变长熵解析可按比例减少；旧流仍顺序解析全部码字 |
| 当前 base predictor 优化 | 通过约分坐标映射和专用整数除法减少了一部分 V7-B residual predictor 运算 | 根本解决 V7-B 写入双重工作 |

### 3.3 当前回归验证

在提交 `8a087e6d` 的 release build 上，以下测试于 2026-09-09 通过：

```text
unit_base_residual
unit_mov
unit_v7_scalable
```

这只是针对本次 V7-B 相关改动的 focused test，并非全量 CTest、ASan/UBSan、fuzz 或跨平台验收。

---

## 4. 当前未通过的门禁与测量证据

### 4.1 2026-09-09 快速门禁（确定性 10-bit 4:2:2 渐变输入）

测试命令：

```bash
python3 scripts/bench_topos_scalable_encode_gate.py --quick --iters 1
```

当前 runner 的 6K quick 尺寸尚为 6144×3456；下一阶段会改为本计划第 1.1 节的 5760×3240 作为产品主表。

| 源尺寸 | base | V7-B / V2 文件大小 | V2 / V7-B PSNR-Y | V2 p50 | V7-B p50 | 编码变化 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1920×1080 | 1920×1080 | 922,082 / 921,721（+0.039%） | 99.00 / 99.00 dB | 2.239 ms | 23.141 ms | +933.5% |
| 3840×2160 | 1920×1080 | 3,348,769 / 3,694,451（−9.357%） | 99.00 / 68.02 dB | 35.776 ms | 139.424 ms | +289.7% |
| 7680×4320 | 1920×1080 | 7,694,512 / 9,905,628（−22.322%） | 99.00 / 68.52 dB | 55.380 ms | 362.887 ms | +555.3% |

结论：**BLOCKED**。4K/8K 文件看似更小，但 V7-B 与 V2 不处于相同质量点，不能用该大小差作为收益。编码时间显著超过最终允许的 `p50 ≤ +5%`、`p95 ≤ +8%`。

### 4.2 尚未有资格通过的最终门禁

| 门禁 | 限制 | 现状 | 必须补齐 |
| --- | ---: | --- | --- |
| 全解码质量 | 相对同配置 V2 的客观质量差必须在冻结误差预算内 | 未通过；当前渐变落差约 31 dB | 多素材 PSNR/SSIM/视觉审阅，匹配质量点 |
| 总包体 median / p95 | ≤ +1% / ≤ +3% | 不可判定 | 在相同质量点、完整 corpus 比较 |
| encode p50 / p95 | ≤ +5% / ≤ +8% | 未通过 | 移除第二套全分辨率 residual 生产和重复 probe |
| 8K/12K→2K decode | codec-only 及 I/O/E2E 接近正常 2K 路径；分别报告 ratio | 未完成最终量化 | 用固定规格、冷热缓存、相同线程复测 |
| 播放稳定性 | 10 分钟/1000 帧、seek/scrub/取消无泄漏和旧帧上屏 | 未完成 | 所有 codec、MOV、GPU 路径的长稳测试 |

---

## 5. 未完成项目、问题与处理优先级

### P0：先收口实验状态（最高优先级）

| ID | 未完成项 / 问题 | 处理方式 | 验收 |
| --- | --- | --- | --- |
| P0-01 | 工作树中的 V7-B transform-enhancement prototype 未达质量/体积门禁 | 不把 minor-3 试验写入公开 writer；保留稳定 minor-2 reader 兼容，移除或隔离不可达 writer 试验 | 工作树干净；无不可达生产编码分支；已有 minor-1/2 decode 测试仍过 |
| P0-02 | 现有 benchmark 主表混用 2048-base、6144 6K 等口径 | runner 改用 1920/3840/5760/7680/11520 主表，另保留互操作尺寸附表 | JSON/Markdown 明确实际 source、base、target、比例 |
| P0-03 | 现有 gate 只覆盖确定性输入，不能代表噪声、文字和运动画面 | 冻结公开可复现 corpus manifest：flat、渐变、细线/文字、自然纹理、胶片颗粒、HDR、422/444、10/12-bit、Alpha | SHA、生成器版本、质量参考输出和许可齐全 |
| P0-04 | “已接入”与“已发布”容易混淆 | capability query 和 diagnostics 显示 `reference/experimental/eligible/default` 四种状态 | UI/CLI/日志不能把 fallback 或 opt-in 标成 native scalable release |

### P1：冻结正确的 V7-B 分层语义（最高优先级）

推荐采用**共享分析金字塔（shared transform pyramid）**，而不是继续把像素 residual 当成第二个完整图像编码器。

```text
源平面
  └─ 一次确定性低通/高通分析（按 1/2、1/3 或组合比例）
       ├─ 低通 L：1920×1080 base，独立编码、独立解码
       └─ 高频 H：仅存 L 无法预测的子带，按 level/tile 可跳过编码

预览：只解 L
完整：解 L + H ──同一套确定性 synthesis──> full
```

这可以使用整数 lifting/wavelet 风格的可逆或近似可逆分析，也可以使用经严格证明等价的宏块层级变换；关键不在术语，而在以下不变量：低频和高频必须**互补、无重复、由同一次源帧分析产生**。现有逐 8×8 DCT F-cache 可复用 token/熵编码经验，但不能直接从所有源块的独立 DCT 系数免费生成一个空间 2K base，因此不能把它误当成完成了共享层。

| ID | 实施内容 | 主要产物 | 验收 |
| --- | --- | --- |
| P1-01 | ADR：冻结 transform family、边界延拓、422 chroma phase、rounding、level/tile geometry、error/concealment 和版本升级策略 | `ADR` + bitstream spec | 两个独立 scalar 实现逐样本一致；所有字段有大小/范围上限 |
| P1-02 | scalar reference analysis/synthesis | `pyramid_transform.c/h`；1/2、1/3、1/4、1/6 的原语或可证明组合 | 无量化时 bit-exact 或冻结的最大误差；odd/非 8 对齐/12-bit/Alpha 覆盖 |
| P1-03 | shared coefficient/subband representation | 不创建第二份全分辨率像素 residual；带有明确 ownership 的 tile/level buffers | allocation、读写量和 source pass 数可观测；无 source-sized duplicate image buffer |
| P1-04 | V7-B minor-4 directory | base 和 detail level/tile 均有 offset/size/CRC；可跳过高频，不破坏 minor-1/2 reader | 截断、重叠、溢出、未知 critical flag 的安全测试和 fuzz |

### P2：实现真正的一次分析、两层写入器（最高优先级）

| ID | 实施内容 | 关键约束 | 验收 |
| --- | --- | --- | --- |
| P2-01 | base writer 直接消费 pyramid low-pass | 4K/6K/8K/12K base 精确为 1920×1080；互操作尺寸记录实际几何 | base-only packet 和 full packet 解出的 base 逐像素一致 |
| P2-02 | enhancement writer 消费 high-pass subbands | 不写 `source - upsample(base)` 的整像素平面；不重复存 low-pass | full synthesis 后与 reference 质量对等；subband byte accounting 可审计 |
| P2-03 | 联合 rate control | 一个 frame-level lambda/quality 目标联合分配 base 与各 detail level；禁止 base/residual 各自 `sized` 搜索 | probe 次数、p50/p95 受上限；最终目标与 V2 质量匹配 |
| P2-04 | `scalable-fast` 单次写入 | 高分辨率写入路径只做一次 source analysis；不 dry-run 两个完整编码候选 | profile 显示无第二次 full-resolution residual scan |
| P2-05 | `size-guarded` 离线策略 | 只在允许更慢的离线 profile 做有限候选比较；容器 capability 明确布局 | 选择逻辑可重现，永不污染 realtime profile |

### P3：读路径、SIMD、调度与 GPU（高优先级）

| ID | 实施内容 | 验收 |
| --- | --- | --- |
| P3-01 | minor-4 base-only range reader | MOV/TOOS 只读取 header、bounded directory、base；I/O telemetry 证明 enhancement bytes 为 0 |
| P3-02 | 2K decode kernels | base transform/entropy 的 scalar、AVX2、NEON 实现；dispatch 与 scalar 一致 |
| P3-03 | 1/3 快速路径 | 对 5760×3240→1920×1080 使用固定三相分析/synthesis 表；不走通用坐标除法 | 与 generic reference 一致且 benchmark 更快 |
| P3-04 | 跨帧任务图 | I/O、base entropy、base inverse、GPU upload 按帧限流；禁止 nested parallelism oversubscription | 8/16 线程下 p95、取消和 frame generation 正确 |
| P3-05 | GPU 输出 surface | codec 写 caller-owned mapped surface；平台层处理 IOSurface/PBO/fence；显示继续走 planar shader | 没有 Python/NumPy full-frame 中间拷贝；上传字节约为 2K |

### P4：质量、性能、安全与上线（高优先级）

| ID | 实施内容 | 验收 |
| --- | --- | --- |
| P4-01 | 匹配质量的 gate | 对每个 corpus 项先使 V7-B 达到相对 V2 的冻结 PSNR/SSIM/视觉阈值，再比较大小/编码速度 | 不允许以低质量 smaller packet 通过 |
| P4-02 | decode matrix | 对 2K/4K/6K/8K/12K，分别测 V2 full、legacy reduced、V7-A、V7-B base-only；冷/热缓存、codec/I-O/upload/E2E 分开 | 输出 FPS、p50/p95/p99、bytes、symbols、blocks、IDCT、RSS |
| P4-03 | 正确性/安全矩阵 | scalar/AVX2/NEON × 1/2/4/8/16 threads，422/444/GBR/Alpha、10/12-bit、odd geometry、损坏目录/段 | ASan/UBSan/fuzz 无 OOB/泄漏；错误码稳定 |
| P4-04 | 播放长稳 | 1000 帧或 10 分钟顺播、反向、seek、scrub、暂停转 full、取消 | RSS 有界、无旧帧上屏、无 GPU 资源泄漏 |
| P4-05 | rollout | `experimental` → opt-in creator → 高分辨率新素材默认；任一 telemetry/regression 失败可退回 V2 | 发布报告、capability/version、MOV/TOOS 同步齐全 |

---

## 6. 量化验收门槛（必须同时满足）

### 6.1 同质量定义

门禁比较前，V7-B full decode 必须达到冻结的质量匹配规则。建议首版采用以下保守口径，并在 P1-01 ADR 中正式冻结：

- 每素材的 V7-B luma PSNR 不低于同配置 V2 的 `0.25 dB`；
- SSIM 不低于同配置 V2 的 `0.002`；
- 渐变、文字、细线、颗粒和运动序列由人工 review 判定无新增的持续 alias、ringing 或 flicker；
- 色度、HDR 和 Alpha 不仅看 luma PSNR；Alpha lossless profile 必须 bit-exact。

数值阈值可在 corpus 冻结后调整一次，但调整必须同时记录原因和前后全表，不能为单一失败样本临时放宽。

### 6.2 writer 门禁

在相同质量点、相同输入、同一 release build、固定线程数下：

| 指标 | 通过条件 |
| --- | ---: |
| V7-B 总包体 median | 不大于 V2 的 +1% |
| V7-B 总包体 p95 | 不大于 V2 的 +3% |
| `scalable-fast` encode p50 | 不慢于 V2 的 +5% |
| `scalable-fast` encode p95 | 不慢于 V2 的 +8% |
| 编码次数 | 主路径一次 shared analysis；不允许两次全分辨率 encode/probe |

### 6.3 2K preview 门禁

| 指标 | 通过条件 |
| --- | --- |
| 输出几何 | 主表 4K/6K/8K/12K 均精确为 1920×1080 |
| enhancement I/O | V7-B base-only preview 读取 enhancement bytes = 0 |
| base decode 负载 | entropy symbols、inverse blocks、output bytes 与 2K base 的数量级一致，不能随 source block 数线性上涨 |
| codec-only 时间 | 8K、12K base-only p50/p95 以正常 2K decode 为参照单独公布；目标是在完成优化后不高于 2K 的 1.25× / 1.50× |
| E2E 播放 | I/O、GPU upload 与 codec 分项公布；超过实时预算时质量策略丢弃过期帧，不阻塞 UI |

门禁中的 1.25×/1.50× 是产品目标而非当前实测；若硬件、base profile 或质量要求需要修订，必须在报告中保留原目标与原因。

---

## 7. 当前实验工作树说明

截至写本计划时，工作树有下列未提交修改：

```text
native/topos_codec/src/codec/v7_scalable.c
native/topos_codec/src/codec/v7_scalable.h
native/topos_codec/tests/unit/test_mov.c
```

其中包含对“把全部 residual 编成单个 transform enhancement”的 minor-3 原型和更通用的 MOV base locator 测试。该原型没有通过质量/速度/体积门禁，且公开 writer 已回退到稳定的 minor-2 Rice 路径。后续 P0-01 必须先把可保留的兼容/测试改动与失败原型分离，再提交；在此之前它不能被当作完成成果或合入默认路径。

---

## 8. 推荐执行顺序与提交边界

```text
P0 基线收口与 corpus
  → P1 ADR + scalar pyramid reference
  → P2 shared writer + joint rate control
  → P3 base-only I/O / SIMD / scheduler / GPU
  → P4 matched-quality gates + long-run + rollout
```

每个以下边界单独提交并附带对应测试或报告：

1. `docs(codec): reset v7b scalable completion plan`（本文件）；
2. `test(codec): freeze 1080p scalable benchmark corpus`；
3. `docs(codec): specify v7b shared transform pyramid`；
4. `feat(codec): add scalar scalable pyramid reference`；
5. `feat(codec): write v7b shared base and detail layers`；
6. `perf(codec): add scalable pyramid simd decode`；
7. `test(codec): gate v7b matched-quality encode and preview`；
8. `feat(playback): enable gated v7b auto-2k rollout`。

任何阶段若没有通过自己的门禁，应提交测试、报告和关闭状态，但不推进默认 writer。这样可以保留已经有价值的 reader/preview 工作，同时避免把未达标的编码设计固化进用户文件。

