# V8 批 2：编码器——段化多状态 rANS（topos_v8_encoder_batch2）

日期：2026-09-12 · 分支：fix-ui
前置：批 0（`topos_v8_table_loss_2026-09-12.md`）、批 1（
`topos_v8_format_spec_batch1_2026-09-12.md`，布局冻结）。

## 一、实现概要（全部在 `src/codec/codec.c`，V8 编码节）

- **入口分派**：`frame_encode_ready` 顶部 `version_major==8` →
  `v8_frame_encode_ready`（plain 路径真实现；sized 路径仍 NOT_IMPLEMENTED
  ——码控集成随批 5；alpha 未支持）。
- **段 token 化**（`v8_tokenize_segment`）：从瓦片量化条纹重放
  (run,lvl) 对——与 `fill_color_band_from_f` 逐位同构（zigzag 跳扫 +
  rice map + EOB）；**DC 预测段局部因果**（left=段内前块、top=段内
  上块行，i≥cols；实参钳位防下溢）。
- **瓦片模型**（批 0 三原语直接复用）：成员段聚合计数 →
  `rans2_sim_pick_models` argmin → `v8_write_table`（定长 350B，未用
  行占位）→ `tile_sim_build_row` row_encode→row_decode 同源模型
  （解码端从表字节重建同一模型，freq≥1 契约由 byte≥1 保）。
- **逐段后向 rANS**：`rans2_encode_stream_with_models`（批 0 值语义
  引擎）以段为块区间直接可用——段首块 dc ctx 恒 0 与
  `rans2_collect_joints` 的 per-call first_block 语义天然一致。
- **组装**：per plane [dir S×12B][tables T×350B][streams][crc T×4B]
  → 帧级 [53B header][8B ext][plane×3]；帧头最后写（frame_packet_size
  已定）；自检 = scan_v8 全瓦片 CRC。
- **粒度信令**：`reserved[3]`=sb_log2（默认 4）、`reserved[4]`=tile_log2
  （默认 5；0=默认档——tile_log2=0 整平面仅解码侧定义）；config 校验
  为 V8 放开 reserved[3]/[4]，enc_frame_setup 域校验。
- 串行实现（正确性/golden 优先）；并行化随批 4/5。

## 二、过程中钉死的三件事实（非显然，记录在案）

1. **enc_scratch_acquire 同槽同指针**：grow-only 语义下二次 acquire
   （need ≤ 水位）返回同一指针——qtile 与 dir/tables 分两次申请 = 静默
   别名覆盖（pass-2 重 token 化读到被目录覆盖的量化条纹 → freq=0 符号
   → rANS 状态发散 → overflow）。V8 路径单块申请内雕刻。
2. **enc_band_tok 是指针结构**：dc_m/npair/run/lvl_m 为指针；内联数组
   的 v8_seg_tok 必须经 `v8_seg_view` 构建指针视图借用，直接 cast 会把
   dc_m[0]/dc_m[1] 当指针解引用（段错误）。
3. **C 函数实参无条件求值**：`tc_dc_predict(..., seg_dc[i − cols])` 在
   i<cols 时下溢巨索引——即便 has_top=0 不读值也会先崩溃。钳位索引。

## 三、验收（计划 §3 批 2）

| 验收项 | 状态 |
|---|---|
| 段局部 DC 预测 + 瓦片聚合统计收集 | ✓（§一；qtile 两遍重放免大 token 存储） |
| 逐段后向 rANS + 目录/瓦片表写出 | ✓（批 1 布局逐字段；inspect 端到端核验） |
| 码率 vs V7-R2 落在批 0 预测 ±1pp | ✓ **Δ +1.21~5.04%**（真实素材 3 源 × 4K/8K × qp20/30/44，18 配置；批 0 预测 +1.4~5.2%）。`tools/bench_v8_encode.py` + `tools/v8_encode_size_2026-09-12.json`。DC 段局部代价（批 0 未计入项）实测 ≈ +0.1~0.3pp |
| golden V8 基准流冻结入库 | ✓ `test_v8_encode.py::V8_GOLDEN_SHA`（3 几何 × qp 20/30/44，SHA256 钉死；跨批硬门禁） |
| round-trip（配合批 3） | 批 3 交付（解码器） |
| 全量门禁 | ✓ ctest 82/82（V7 golden 全绿）；pytest 685 passed / 9 skipped |

编码端粒度信令测试：sb∈{8,16,32} × tile∈{16,32,64} 全部编码 + scan
自检通过；非法 log2 域拒绝。

## 四、批 3 接口预留

- `tc_frame_decode` 实解码分派（现 NOT_IMPLEMENTED）：scan_v8 → 逐段
  rANS 前向解码（瓦片表 LUT 单次构建）→ CSR → 反量化 → IDCT 重建 →
  conceal（瓦片 CRC 门控，段级清零）。
- 位精确验收基准 = 编码侧 token 重放（`v8_tokenize_segment`）+ 批 0
  差分方法学（C 捕获 ↔ 解码器 ↔ numpy oracle 三方位）。
