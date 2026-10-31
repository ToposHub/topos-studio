# V8 批 5：conformance/工具链 收口记录

日期：2026-09-12 · 前置：批 0~4（a71da81b/7dc32603/c25b5dfe/afe0a5bf/822a93c1）·
计划：`topos_v8_format_plan_2026-09-12.md` §3 批 5

---

## 一、conformance 全套（正流 + fuzz + 损伤矩阵）

### 1.1 正流 golden（字节 + 解码像素双冻结）

- 工具：`tests/conformance/golden_v8.c`（`gen|check`，镜像 golden_bitstream 惯例：
  magic "TV81" | version | count | fold | 记录{ len | pixel_fold | bytes }）。
- 记录 3 条（真实 `tc_frame_encode` reserved[0]=9，确定性 LCG 帧）：
  | 记录 | 几何 | 档位 |
  |---|---|---|
  | A | 96×96 422 10bit qp28 | sb=16 tile=32（默认档） |
  | B | 96×96 422 10bit qp20 | sb=8 tile=16（细段/细瓦片） |
  | C | 80×72 444 12bit qp35 | sb=32 tile=64（粗档 + 444/12bit） |
- 入库 `tests/conformance/golden_v8_v1.bin`（116,923 B，与 V7 golden_codec
  60KB 同量级）；fold `f2378492ce84738b`。
- check 语义 = 重编码逐字节比对（编码器无声漂移门禁）+ scan_v8 全瓦片 CRC +
  解码 conceal=0 + **解码像素 fold 比对**（解码器行为冻结）。
- ctest `conformance_v8` ✓。

### 1.2 fuzz（V8 路径全覆盖 + 抓到并修复一个生产 bug）

- 入口 `tests/fuzz/fuzz_v8.c`，两条路径：
  A) 直接路径——输入像 V8 包 → scan + 受限实解码（单面 ≤ 256×256）；
  B) **合成路径**——真实编码器产出合法 V8 包（几何/位深/粒度/qp 取自输入，
  帧内容 LCG(input)）→ LCG(input) 受控字节变异 → scan + 实解码。
  随机 magic 命中率≈0，合成路径保证每个输入深走 V8 扫描/解码/CRC/conceal
  全路径（正流+损伤流一起覆盖；确定性 corpus 因此冗余，不单独入库）。
- ctest `fuzz_v8_replay -gen 512`（每输入含一次真编码+解码，512 次预算）；
  手动 4096 次全过 ✓。
- **fuzz 首跑即抓到生产 bug**（fuzz 价值实证）：
  `v8_frame_decode` 无条件解引用 `strides[p]`——公共契约允许
  `strides == NULL`（= tight，fuzz_codec 对 V7 即此用法），V8 路径 NULL 崩溃
  （EXC_BAD_ACCESS addr=0x0）；同分支缺 `planes_out[p] == NULL` 校验
  （V7 路径有）。两处修复后 fuzz 512/4096 全过。
- ASan 全量构建：**ctest 84/84 全绿**（含 unit_v8_format + 两个新 V8 项）。

### 1.3 损伤流 conceal 矩阵（瓦片域定界语义钉死）

- `tests/native/test_v8_conformance.py` 20 例（0.2s）：
  - 对照 ×3：三种粒度档 clean 解码 == 源像素位精确；
  - 载荷损伤 ×12 = 3 档 {sb4/t5, sb3/t4, sb5/t6} × 4 位置 {瓦片表字节,
    段流数据, 段流终态, 存储 CRC}——断言：concealed==1 且 victim 瓦片
    status==CONCEALED、坏瓦片像素区 == mid(512)、**好瓦片区/其余平面 ==
    参考解码位精确**（瓦片域定界 + 不污染）；
  - 结构损伤 ×5：扩展头保留位非 0 / 段长<4 / 目录裂缝 / 截断 / 拖尾
    → 解码 rc != 0（扫描期明确失败，不产出帧）。
- 布局解析为纯 Python 算术（53+8B 头 + 逐平面 dir/表/流/CRC 区），尾断言
  `off == len(pkt)` 自校验。

## 二、工具链 V8 状态表（含本批接线修复）

| 入口 | 状态 | 本批改动 |
|---|---|---|
| `tc_frame_decode`（查询/实解码） | ✓（批 3/4） | 实解码加 planes NULL 校验 + tight strides 默认 |
| `tc_frame_decode_request` | ✓ | **批 1 遗留 NOT_IMPLEMENTED 占位补齐**：FULL → v8_frame_decode；reduced/auto2k → 明确 NOT_IMPLEMENTED |
| `tc_decoder_decode`（ctx 路径） | ✓（批 4） | — |
| `.topos` 容器 mux/demux | ✓ | `movie_parse_packet_structure` 加 V8 分支（scan_v8_ex(0) 结构校验）；**tpcC 读侧白名单 1..7 → 1..8**（老二进制仍在此明确拒绝 V8——兼容语义保持） |
| `topos_encoder_cli` | ✓ | `--entropy v8` + `--seg-blocks 8|16|32` + `--tile-rows 16|32|64`（非 V8 熵下显式给出 = 参数错误） |
| `topos_decoder_cli` | ✓ | full 直通；`--scale 1/2` → 明确 NOT_IMPLEMENTED（exit 1） |
| `topos_probe_cli --verify` | ✓ | 冒烟 3/3 frames OK（tc_frame_decode 查询路径复用） |
| `topos_inspect` | ✓（批 1） | — |

**CLI 全链路冒烟**（128×96 qp30 v8 sb16/t32 ×3 帧，实测）：
encode 149,436 B 容器 → probe verify 3/3 OK → decode full 3 帧
concealed=0、147,456 B（=3×(128×96+2×64×96)×2B 精确）→ reduced 明确报错。

## 三、本批修复清单（全部加法/契约对齐，V7 位流零接触）

1. `v8_frame_decode`：strides==NULL tight 默认 + planes_out 逐平面校验（fuzz 抓到）。
2. `tc_frame_decode_request` V8 实解码分发（批 3 遗留）。
3. `mov.c` V8 解析分支 + tpcC 白名单 8。
4. encoder CLI V8 旗标。

## 四、验收（计划 §3 批 5）

| 验收项 | 状态 |
|---|---|
| V8 golden/conformance 全套（正流 + fuzz + 损伤 conceal 矩阵） | ✓ §一（golden 3 记录双冻结；fuzz 512 ctest + 4096 手动；矩阵 20 例） |
| topos_inspect/golden 工具/CLI 的 V8 支持 | ✓ §二（inspect 批 1 已达；golden/CLI/容器本批补齐） |
| 全门禁绿 | ✓ release ctest 84/84；ASan 84/84；pytest native+shared 718 passed / 8 skipped（基线 698 + 矩阵 20）；V7 golden SHA 不变（pytest golden 钉死项全绿） |
| 编辑器集成评估（产品化，单列） | ✓ `topos_v8_editor_integration_2026-09-12.md` |
| 生产默认切换建议 → 用户拍板 | ✓ `topos_v8_production_switch_2026-09-12.md`（两个拍板项） |
