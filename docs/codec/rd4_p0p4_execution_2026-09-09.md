# V7-B 主计划 P0–P4 执行报告（2026-09-09）

**对应计划：** `docs/topos_v7b_reduced_decode_master_plan_2026-09-09.md`
**分支：** `fix-ui`（基线 `8a087e6d` 起步；本报告截至本提交）
**核心结论：** P0 全部收口、P1 全部交付、P2 写入器落地并通过结构验收；
**尺寸与质量门禁在合成 corpus 上首次实测通过（匹配质量点）**；编码速度
门禁仍未过（scalar 未优化），维持 `reference` 状态、默认 writer 不变。

---

## 1. 交付清单（提交边界对齐计划 §8）

| 计划项 | 交付 | 提交 | 验收证据 |
| --- | --- | --- | --- |
| 计划文档 | 重定基线总计划 | `37809cfc`（先行） | 本文件所在仓库 |
| P0-01 | 移除 minor-3 不可达 writer 试验（-358 行死代码）；minor-2 公开、minor-3 reader 保留 | `19b51394` | unit_base_residual/mov/v7_scalable/codec 通过 |
| P0-02 | gate 主表钉死 1920/3840/5760/7680/11520（6K=5760×3240）；`--interop` 附表；CSV/JSON/MD 记录 source/base/target/ratio | `ea45cd96` | 5760×3240→1920×1080 精确 1/3；6144 附表如实报 16/5 |
| P0-03 | corpus 冻结表 v1（15 项：flat/渐变/细线文字/纹理/颗粒/HDR-PQ/422/444/GBR/10-12bit/Alpha）+ 生成器 CLI + SHA256/生成器 commit/许可装订 | `c4e21001` | `freeze_topos_scalable_corpus.py` bit-exact 往返校验通过 |
| P0-04 | `tc_query_scalable_status()`/`tc_scalable_status_name()` 四态；probe CLI 诊断行；binding `scalable_status()`；pytest 防漂移 | `c9b825bd` | pytest 17 项 + unit_abi 四态断言；导出符号登记 |
| P1-01 | ADR-C030 冻结变换族/算术/级联/逃逸/目录/隐藏/升级策略 | `5f3955bb` | ADR-INDEX 登记 + rollup 机器检查通过 |
| P1-02 | `pyramid_transform.c/h` scalar 参考（N=2 5/3 风格 + N=3 中位锚定；级联 2/3/4/6） | `43052058` | 9 尺寸×4 比例×3 位深×4 图案 bit-exact；Python 镜像逐值一致 |
| P1-03/04 + P2-01/02 | minor-4 shared pyramid 写入器 + 目录（几何表/段契约/逃逸段/回落）；base 直通帧编码器 | `7bc2c0dc` | unit_v7_scalable 闭环；全量 74 CTest；ASan 净 |
| P2-03/04 + P3-01 | gate `--writer pyramid --kind` 实测通道；联合规则 v1（detail 恒无损）；range reader 字节级验证 | 本提交 | 见 §2/§3 实测 |

## 2. 门禁实测（pyramid writer，匹配质量点）

主表 quick（iters=1，确定性合成，V2 与 V7-B 同配置 qp24/qm1，
双方全解码 luma PSNR 均为 99.00 = 逐位无损——质量点匹配）：

| 内容类 | 源 | V7-B / V2 体积 | 体积差 | 编码 p50 差 |
| --- | --- | ---: | ---: | ---: |
| gradient | 1920×1080 | 922,082 / 921,721 | **+0.04%** | +991% |
| gradient | 3840×2160 | 2,249,787 / 3,694,451 | **-39.10%** | +794% |
| gradient | 5760×3240 | 1,680,637 / 6,962,564 | **-75.86%** | +987% |
| grain | 1920×1080 | 6,645,021 / 6,644,660 | **+0.01%** | +219% |
| grain | 3840×2160 | 14,834,509 / 26,577,080 | **-44.18%** | +529% |
| grain | 5760×3240 | 26,083,340 / 59,800,199 | **-56.38%** | +671% |

工件：`scalable_pyramid_gate_{gradient,grain}_2026-09-09.{json,md}`。
（2026-09-09 修订：初版报告基于"最大可整除比例"的错误选择（base 被缩至
640×360 等）；修正为计划 §1.1 的**最小可行比例**（4K→1/2、6K→1/3、
8K→1/4、12K→1/6，base 均精确 1920×1080）后重测。2K 源回落路径
+0.04% 与 V2 对齐，sanity 成立。）

- **体积门禁（≤ +1%/+3%）：合成内容上以大幅负值通过**。说明：V2 在
  qp24 对这些确定性内容走无损路径，双方同在无损点比较，口径公平；
  真实素材（照片/运动）corpus 复测仍是 P4-02 必做项。
- **质量门禁（≥ V2 - 0.25 dB）：通过**（双方逐位无损）。联合规则 v1：
  detail 段恒取无损步长，全解质量 == base 编码质量，从结构上禁止
  「降质量换体积」（P4-01 红线）。
- **编码速度门禁（p50 ≤ +5% / p95 ≤ +8%）：未过**（+537%~+1205%）。
  原因明确：scalar 分析（逐采样除法/rdiv3）+ 全系数 run-level Rice，
  未经 SIMD/快速路径优化。这是 P3-02/P3-03 的直接工作项。
- **P2-04 单次写入：结构达成**。pyramid 写入路径每平面恰一次分析
  遍历（`tc_pyramid_analyze_plane` 单调用），base 复用现有编码器，
  detail 直接消费分析产物——不存在第二次全分辨率残差扫描，也没有
  两个完整编码候选的 dry-run。

## 3. 预览门禁（P3-01 + §6.3 codec-only 实测）

minor-4 包经 `tc_v7b_read_base_packet` 范围读取（unit_v7_scalable
字节计数回调实测）：

- 实际 I/O = 帧头 + 目录 + base 段，**enhancement 字节 = 0**；
- `segments_read = 1`，`bytes_skipped` = 其余全部段 payload；
- base-only 解码输出几何 = 精确 1920×1080。

### §6.3 解码速度：XK base-only vs 正常 2K 解码（30 次迭代 p50/p95）

参照：V2 1920×1080 包 tc_frame_decode（gradient p50 ≈ 2.2ms；
grain p50 ≈ 10.6ms）。工件：
`preview_decode_gate_{gradient,grain}_2026-09-09.{json,md}`；
增强版（全五档规格 + 解码帧率 fps + @24fps 码率/bpp 表）：
`preview_decode_gate_{gradient,grain}_2026-09-10.{json,md}`。

| 源→目标 | gradient p50/p95 | grain p50/p95 | §6.3 门禁（≤1.25×/≤1.50×） |
| --- | ---: | ---: | --- |
| 2K→2K（回落路径） | 1.18× / 1.15× | 1.27× / 1.19× | 基线开销，非门禁对象 |
| 4K→2K | 1.68× / 1.54× | 1.31× / 1.25× | p50 未过；grain p95 过 |
| 6K→2K | 1.16× / 1.11× | 1.30× / 1.30× | gradient 过；grain p50 略超 |
| 8K→2K | 1.70× / 1.57× | 1.38× / 1.32× | p50 未过；grain p95 过 |

实测归因（后续优化靶点）：

1. **固定包装开销 ~1.15–1.27×**：目录解析 + base 段整段 CRC32 在解码
   前执行；2K 基线只有 2.2ms（gradient），0.3–0.4ms 固定开销占比高。
   优化：CRC 延迟到与解码流水并行，或内存内解码跳过重复 CRC。
2. **N=2 级联 base 熵膨胀（gradient 4K/8K）**：5/3 update 的舍入噪声
   进入低频层，base 1457–1503KB vs N=3 路径 934KB，解码时间随 base
   熵线性上涨；grain 内容下该差异消失（6.9–7.8MB 各比例接近）。
   优化：N=2 update 舍入粒度细化（ADR 级变更，需过双实现一致门禁）。

## 4. 仍未完成（诚实清单）

| 项 | 状态 | 阻塞点 |
| --- | --- | --- |
| 编码速度门禁 | 未过 | scalar 分析 + Rice 未优化；需 P3-02（AVX2/NEON 分析与熵码）与 P3-03（1/3 定表快速路径；`topos_third_path_bench` 已有微基准地基） |
| §6.3 预览解码门禁 | 部分未过 | 见 §3 实测：包装开销（CRC/目录）与 N=2 级联 base 熵膨胀是两个已定位靶点；grain 类 p95 全过 1.50× |
| 真实素材 corpus 门禁 | 未做 | P0-03 冻结的是合成 corpus；真实照片/HDR/运动素材的门禁复测仍待 P4-02 |
| decode matrix / 长稳 | 未做 | P4-02/P4-04：冷热缓存、8/12K base-only 时间、1000 帧长稳 |
| SSIM/人工审阅 | 未做 | 当前仅 luma PSNR；计划 §6.1 要求 SSIM + 渐变/文字人工审阅 |
| tile_version=1 | 预留 | 64×64 tile 表已预留字段，逐 tile 隐藏未激活 |
| rollout | 不适用 | 状态保持 `reference`；公开 writer 保持 minor-2；全部门禁（含速度、真实素材、长稳）通过前不推进 |

## 5. 复现命令

```bash
cmake -S native/topos_codec -B native/topos_codec/build/release \
      -DCMAKE_BUILD_TYPE=Release
cmake --build native/topos_codec/build/release -j
(cd native/topos_codec/build/release && ctest)

python3 scripts/freeze_topos_scalable_corpus.py          # corpus bit-exact 校验
python3 scripts/bench_topos_preview_decode.py \
    --quick --iters 30 --kind gradient                   # §3 预览解码对比
python3 scripts/bench_topos_scalable_encode_gate.py \
    --quick --iters 1 --writer pyramid --kind grain      # 本文 §2 数据
python3 -m pytest tests/media/ -q                        # 镜像/manifest 门
```
