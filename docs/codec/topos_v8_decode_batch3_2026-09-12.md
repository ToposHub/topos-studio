# V8 批 3：CPU 参考解码器（topos_v8_decode_batch3）

日期：2026-09-12 · 分支：fix-ui
前置：批 1（布局冻结）、批 2（编码器 + q-hash oracle 契约）。

## 一、实现概要（`src/codec/codec.c` V8 解码节）

- **两阶段并行**（`v8_frame_decode`）：
  1. 结构扫描 `tc_packet_scan_v8_ex(verify_crc=0)`（免 CRC——与 V7「主线程
     不预扫 payload CRC」同设计）；
  2. 任务 1 = 瓦片 CRC 并行重算（每瓦片一次，回写 view）；
  3. 任务 2 = 段块（CHUNK=64 段）并行解码：`tc_rans_dec_init` → 前向符号环
     （DC cat+suffix → 段局部 DC 因果重建 → run/lvl 对环 → EOB）→ 逐块
     融合重建（`tc_color_store_block_xy` + `dinv` 内核，免 q 二传）。
- **瓦片表 LUT 单次构建**（批 1 已知坑的落实）：每平面 T 张 350B 表 →
  `v8_tile_dec_build`（dc/run/lvl 模型 + LUT，行基址固定 5+1+5 定长布局）
  ——段/段块共享只读。
- **段级 conceal**：瓦片 CRC 坏 → 全瓦片段全零 q（DC-only 闭式 → mid 填
  充）；段解码半途失败（MALFORMED/TRUNCATED）→ 该段补 mid。语义与 V7
  `dev_qcap_zero_slice` / GPU mid 重建逐值一致。
- **段级 qp**：dir 条目 biased 值去重 → 预建量化 ctx 缓存（典型 1 项；
  免每段重建表——初版每段 init 曾致 4K 并行 1.9× 劣化）。
- **q-hash oracle**：段内块序折叠（`tc_symbol_hash_mix`）+ 段/瓦片/平面
  加法合并（分组无关——编解码两侧任务分组可不同）。默认 OFF（生产零开
  销），`tc_dev_v8_set_qhash(1)` 供差分测试；导出
  `tc_dev_v8_enc_qhash`/`tc_dev_v8_dec_qhash`。

## 二、过程中钉死的三件事实

1. **解码并行效率先于符号开销**：初版按平面串行提交瓦片任务（1K² 12 作
   业）并行比仅 2.9×；全帧单批 + 段块作业后 4K 劣化从 9% 收敛到 0.7%。
   串行符号环本身只慢 ~4%（qcache 前 12%，qcache 后达标）。
2. **定长表 = 行基址固定**：瓦片表 5+1+5 行全部在场（未用行占位），解码
   器行基址必须按定长布局推进——按 ctx_n 推进会把 run 行读到 dc 行上
   （批 1 冻结决策的解码侧对应物）。
3. **hash 合并必须分组无关**：任务拆分自由度（瓦片 vs 段块）要求跨层合
   并用加法；mix 链对分组敏感（曾致编解码 hash 结构性失配）。

## 三、验收（计划 §3 批 3）

| 验收项 | 状态 |
|---|---|
| 与批 2 码流 round-trip 位精确 | ✓ q-hash 逐位相等（4 几何 × qp20/30/44/58，pytest `test_v8_roundtrip_qhash_bit_exact`）；像素级核验（conceal 全 mid、值域 [0,1023]） |
| CPU 解码 4K 不劣于 V7-R2 超过 5% | ✓ **4K qp30 真实素材：V8 29.0ms vs V7 28.8ms（劣化 0.7%）**；1K² 口径 1.08× |
| 段级 conceal | ✓ 瓦片 CRC 损伤 → 对应瓦片 CONCEALED、concealed_slices 计数、其余瓦片不受影响（pytest `test_v8_conceal_tile_crc`）；解码失败段补 mid（ctest 合成全零流 9/9 瓦片 conceal） |
| 差分测试进 tests/native | ✓ `tests/native/test_v8_roundtrip.py`（7 例） |
| 全量门禁 | ✓ ctest 82/82（V7 golden 全绿）；pytest 692 passed / 9 skipped |

## 四、批 4 接口预留

- GPU：`rans2_decode_slice` 段化参数化（smeta → 段目录直读；threads=(S,)）
  ——CPU 解码器即位精确参考（q-hash oracle 复用）。
- 能力画像 + 一次性校准微基准；dispatch 波次 = ceil(S / 有效并发)。
- 编辑器集成（输出直留 GPU 纹理 / ring buffer / CRC 重叠）随批 5。
