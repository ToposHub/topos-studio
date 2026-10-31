# C5 小 nnz 内核特化——开题复剖析 + 批量字节追加（2026-09-12）

**触发**：真实素材矩阵判定 r2e 内核占编码 66~68%（> 55% 门，
`topos_real_footage_matrix_2026-09-12.md` §四）。

**结论**：

1. 开题首务的内核内部分布剖析（六段计时器，
   `tc_dev_r2e_stage_ns`，`TOPOS_CODEC_PROFILE=1`）**发现一个此前
   未识别的大项**：r2e 尾段的逐字节 `put_bits_inline(8)` 刷写循环
   占 r2e 线程时 **19.3%**（4K 444 qp58：48.1ms/帧线程时——每帧
   ~7.3M 次带边界检查调用，而等量 memcpy 只要 ~1ms）。
2. 修复：`tc_bitwriter_put_bytes` 批量字节追加（reservoir 空时
   memcpy 直提交，与逐字节路径 flush 后输出逐位一致）替换 V7-R/V7-R2
   两处尾段循环。**位精确：12/12 包 SHA256 一致（含 sized 码控路径）；
   A/B +6.4% 编码墙钟**（4K 444 qp58 steady，28.3→26.6ms/帧）——过
   +3% 入库门。
3. **计划标的"小 nnz 特化"判定不成立**：复剖析后 r2e 内部剩余 =
   后向环 75.5%（ADR-C045 串行依赖地板，已判死）+ collect_joints
   17.0%（ADR-C045 §5 访存地板，已判死）+ 模型选择/前缀表/fastdiv
   ~1%（不值做）；fill（量化/扫描小 nnz 候选区）仅占 band 忙时
   ~11% 且 AVX2 已在线。**C5 就此收口**——与 v2 计划"C 域终态"
   判断一致，编码侧剩余空间确认只在 A 轴（GPU）。

## 一、r2e 六段剖析（4K 444 10bit qp58 steady，60 帧，16t 线程时）

| 段 | 修复前 | 占 r2e | 修复后 | 占 r2e |
|---|---|---|---|---|
| [0] collect_joints | 35.27 ms/帧 | 14.1% | 35.09 | 17.0% |
| [1] 模型选择 rows_cost×4 | 1.11 | 0.4% | 1.25 | 0.6% |
| [2] 前缀表 row_enc/dec | 0.38 | 0.2% | 0.37 | 0.2% |
| [3] fastdiv+scratch | 0.45 | 0.2% | 0.34 | 0.2% |
| [4] 后向环 put | 163.94 | 65.8% | 155.55 | 75.5% |
| [5] 刷写+bitwriter 拷贝 | **48.08** | **19.3%** | **13.50** | 6.6% |
| 合计 | 249.24 | | 206.11（−17.3%） | |

（剖析口径含 `sample` 交叉验证：`color_tokens_rans2_encode` 自采样
18.7% + 内联进 `enc_band_job` 的克隆 ~26.6%——内联归因不可靠，
故上表以六段计时器为准。剩余 13.5ms = `tc_rans_enc_flush` +
emit_done 的 payload memcpy，属真实访存流量。）

## 二、实现

- `bitio.h/c`：`tc_bitwriter_put_bytes(bw, src, n)`——`hold_bits==0`
  （字节对齐、reservoir 空）时 `bw_ensure_bytes` + memcpy 直提交；
  非零回落逐字节 `bw_put(8)`（任意位对齐位精确；调用方场景不会走到）。
  M6 reservoir 不变量不受扰。
- `codec.c`：V7-R（table+scratch）与 V7-R2（prefix+scratch）两处尾段
  循环替换为两次 `put_bytes`。调用点保证 reservoir 空（band 首写、
  全字节流）。
- 剖析计时器保留为常设 M0 器材（flag 关闭时每 slice 6 次分支）。

## 三、验收

- 位精确：6 配置 × encode_frame + encode_sized = 12/12 SHA256 一致
  （新旧代码交替 stash 复核）；
- A/B：4K 444 qp58 steady 25s 循环——旧 28.3ms/帧（35.3fps）→
  新 26.6ms/帧（37.6fps）= **+6.4%**；
- 门禁：ctest 81/81（含 fuzz bitstream corpus）；pytest
  tests/native+tests/shared 657 通过。

## 四、证据

- /tmp/a2_prof/r2e_stages.py（六段剖析驱动）、sha_check.py（位精确
  清单）、enc_profile.py（A/B steady 环）；
- 剖析样本：/tmp/a2_prof/enc_sample.txt（`sample` 交叉验证）。
