# V8 批 1：格式冻结 + 版本机骨架（topos_v8_format_spec_batch1）

日期：2026-09-12 · 分支：fix-ui
前置：`topos_v8_format_plan_2026-09-12.md` §3 批 1；批 0 结论
（`topos_v8_table_loss_2026-09-12.md`：瓦片粒度默认 32 块行）。

## 一、V8 位流布局（本批冻结；实现 = src/bitstream/packet.h 注释同文）

```
[53B frame header]（major=8, entropy_mode=8, codebook_version=0,
                   coding_mode=0, version_minor=0；其余字段语义同 V7；
                   slice_count 复用 = 全帧瓦片总数 T ∈ [plane_count, 512]）
[8B V8 扩展头]：[0] segment_blocks_log2 ∈ {3,4,5}（= 8/16/32 块/段）
                [1] tile_rows_log2 ∈ {0,4,5,6}（0=整平面一片；4/5/6 =
                    16/32/64 块行；默认 5=32 行——批 0 实测最优档）
                [2..7] 保留恒 0（非零 → MALFORMED）
per plane p（0..plane_count−1 顺序串联）：
  [段目录 S_p × 12B]：stream_off u32 BE（相对本平面码流区基址）
                      stream_len u32 BE（含 4B 终态）
                      qp_delta_biased u8（V7 slice 同语义，64+delta）
                      保留 u8×3 恒 0
  [瓦片表区 T_p × 350B 定长]：每表 1B flags（V7-R2 位域同构：[1:0] lvl
                      模型、[2] dc 模型、[7:3] 恒 0）+ dc 条件行 5×29 +
                      run 64 + lvl 条件行 5×28；未用行占位 {1,0,...}
                      （row_encode 全零语义——解码侧无条件复现）
  [码流区]：段 0..S_p−1 连续；段流 = 4B 大端终态 + 数据（V7-R2 flush
                      布局同构；后向编码产物）
  [瓦片 CRC T_p × u32 BE]：CRC_i = tc_crc32_update 增量覆盖表 i 的
                      350B + 成员段流字节（连续区间）；不含目录（目录
                      损坏 = 结构性拒绝，不进 conceal 判定）
S_p = ceil(plane_block_rows[p]·plane_block_cols[p] / segment_blocks)
T_p = tile_rows==0 ? 1 : ceil(plane_block_rows[p] / tile_rows)
瓦片 t 成员段 = [seg_first(t), seg_first(t+1))；seg_first(t) =
      ⌊t·tile_rows·cols / segment_blocks⌋（段归首块所在瓦片；跨瓦片
      边界的段归属首块瓦片；零成员瓦片合法——窄平面×大段粒度退化）
精确耗尽：53 + 8 + Σ_p (12·S_p + 350·T_p + Σlen_p + 4·T_p) == 包长
      == frame_packet_size
```

### 冻结决策记录

| 决策点 | 冻结值 | 理由 |
|---|---|---|
| 版本信令 | major=8 + entropy_mode=8，三元组 (8,0,0) 唯一合法 | 复用版本机分派（cfg_to_frame_header `reserved[0]=9`）；老二进制白名单 ≤7 自然拒绝 |
| 瓦片表定长 350B | 全条件行上界常驻，未用行占位 | GPU/宿主 O(1) 定址（表基址 = 表区基址 + tile_id×350）；定长化浪费 ≈ 0.04%（批 0 口径），换免预扫 |
| 段目录 12B | off u32 + len u32 + qp u8 + 3B 保留 | 9B 压缩变体（len u16）留瘦码流档优化（批 0 §2.3/b，批 5 拍板） |
| 瓦片 CRC 范围 | 表 350B + 成员段流字节；不含目录 | 目录损坏属结构错误（整帧拒绝）；CRC 坏 → 该瓦片成员段 conceal（段级，比 V7 带级细） |
| 段 qp | 每段 qp_delta_biased | 语义同 V7 slice；段级码控可用（批 2 编码器接入） |
| DC 预测 | 段局部（段首块无 top/left 因果）；符号层批 2 定义 | 带内跨段因果不存在（段并行解码）；代价 ≤0.3% 量级（批 0 §四解析界），批 2 round-trip 校准 |

## 二、本批实现面（版本机骨架；符号层批 2/3）

1. `frame_header.{h,c}`：decode 白名单 + major=8；validate V8 规则组
   （三元组 (8,0,0)、minor 恒 0；其余域检查复用 V2 组）。
2. `packet.{h,c}`：`tc_packet_scan_v8`（扩展头校验/平面区基址推进/目录
   环校验〔off_0==0、off_{i+1}==off_i+len、len≥4、Σlen 有界〕/精确耗尽/
   瓦片段映射/逐瓦片 CRC 标记）；`tc_packet_is_v8` 廉价探测；
   **V7 扫描入口（parse_structure/scan）对 major=8 明确 UNSUPPORTED**。
3. 编码入口占位：`reserved[0]=9` 配置合法（域 {0..9}），`tc_frame_encode`
   /`tc_frame_encode_sized` → NOT_IMPLEMENTED（批 2 落地）。
4. 解码入口占位：`tc_frame_decode`/`decode_scaled`/`decode_request` 对
   V8 包：scan_v8 结构校验 + out_info 填充 → 查询模式 OK / 实解码
   NOT_IMPLEMENTED（批 3 落地）。
5. `topos_inspect`：V8 分支——完整结构报告（区基址/瓦片段映射/逐瓦片
   CRC 标记；符号行 n/a）。

## 三、验收

| 验收项（计划 §3 批 1） | 状态 |
|---|---|
| 帧头 V8 字段 + 段目录/瓦片表布局定稿 | ✓ §一（packet.h 注释同文冻结） |
| frame_header 解析/校验 | ✓ validate V8 规则组 + decode 白名单 |
| 旧二进制对 V8 流 UNSUPPORTED_VERSION | ✓ 双重钉死：老白名单（语义保证）+ 本库 V7 扫描入口显式拒绝（test_v8_format 钉） |
| ctest 新增 V8 头部 fuzz/边界用例 | ✓ `tests/unit/test_v8_format.c`：版本机 5 例 + 结构扫描全字段 + 变异 10 例（sb/tile log2 域、保留位、slice_count 失配、目录裂缝、len<4、截断、拖尾）+ CRC 损伤标记 + 入口占位 3 例 |
| V7 全部 golden SHA 不变 | ✓ ctest 82/82 全绿（含全部 conformance/golden） |
| topos_inspect 识别 V8 | ✓ 合成包端到端报告（Python zlib CRC == C tc_crc32_update 跨实现互证） |
| pytest 门禁 | ✓ 673 passed / 9 skipped（批 0 的 3 例含在内） |

### inspect 冒烟输出（合成 64×320 422，sb=16、tile=16 行、9 瓦片）

```
v8: segment_blocks 16  tile_rows 16  tiles 9  segments 20/10/10
plane Y: dir@61 (20×12B) tables@301 (3×350B) stream@1351 crc@1431
tile   0 plane Y id 0 segs [   0..   8) table@301 stream     32B crc OK
...
summary: tiles 9  crc_bad 0  symbols: n/a (v8 decode batch 3)
```

## 四、批 2 接口预留（未实现，仅登记）

- 编码器：`fill_color_band_tokens` 段变体（段局部 DC 预测）+ 瓦片聚合
  统计 + 逐段后向 rANS + 目录/表/流组装。批 0 的模拟器
  （`tc_dev_rans2_tile_sim`）提供了瓦片模型选择与发射的参考实现。
- 解码器：段目录直读 → 逐段 rANS → CSR → conceal（瓦片 CRC 门控）。
- GPU：`rans2_decode_slice` 段化参数化（批 4）。
