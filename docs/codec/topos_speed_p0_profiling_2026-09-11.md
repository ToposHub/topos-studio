# 速度计划 P0 地基批——编码首次剖析 + 解码 rans2 装配剖析（2026-09-11）

速度总计划（`topos_speed_optimization_plan_2026-09-11.md`）P0 批交付：
M0.1 基准协议、M0.2 编码热点首次剖析、M0.3 解码 rans2 专项剖析。
工具：`tools/prof_p0.py`（产品码控编码采样基座 + 包落盘）+
`tools/prof_decode.c`（沿用）；方法 = 帧常驻内存纯调用计时 +
`sample` 10~12s 符号归因（inclusive 占比，i9-9900K）。
构建钉 release/perf（M0.1）；素材 = DNxHR 444 解码（444 族共享源，
ADR-C042 口径）/ prores422HQ 解码（422 族），24 帧。

## M0.2 编码热点（首次系统剖析）

4K 444 pro444（qp 落点 70-71，4.277 bpp 贴 C043 目标）：稳态
38.1~38.9 ms/帧；**首帧 sized 搜索 92 ms = 2.4× 稳态帧**（≈4 探针 ×
13 ms + F-cache 准备）；24 帧摊销后搜索占 9.4%。4K 422 hq 对照
（qp 62，稳态 24.1 ms/帧 = 41.5 fps，与 C043 矩阵 40.2 一致）同构。

band 作业内部（inclusive，两族一致）：

| 成分 | 占 band 作业 | 说明 |
|---|---|---|
| **rans2 token 编码**（`color_tokens_rans2_encode`，含内联体） | **79%** | 编码主战场 |
| └ `tc_rans_put` 反向符号环 | **41%** | 逐符号 u32 除法（S4 靶心） |
| └ collect_joints + 精确代价模型 + 模型行编码 | 38% | S5 第二靶 |
| 量化 + token 生成（fill_color_band*，含内联） | 21% | AVX2 nat 量化已在线 |
| DCT（F-cache，DCT-once） | 不可见（<2%） | 一次/帧，无优化空间 |
| Python binding/驱动 | ~6% wall | 非热点 |

qp hint 检查：`tc_dev_set_qp_hint`（codec.c:3936）只是 dev 关闭开关，
跨帧 hint **默认已启用**——92 ms 已是含 hint 成本，无免费加速。
F-cache 流量假设被否定：100 MB/帧级访存未成为采样热点（DCT-once
+ 探针只读不写的结构下 LLC 命中可维持）。

## M0.3 解码 rans2 专项（产品 qp 落点）

三格采样（4K 444 pro444 qp70 / 2K 444 / 4K 422 hq）**同构**，
rans2 解码核占 slice 作业 99%：

| 成分 | 4K444 | 2K444 | 4K422 | 说明 |
|---|---|---|---|---|
| **sink 路径**（`tc_c1_store_sink` → store_block → 单块 `dinv`） | **47%** | 48% | 53% | S1 靶心 |
| └ AVX2 单块 dequant+inverse | 43% | 44% | 48% | rans2 无批 4 路径 |
| 符号环自体（原语+memset+上下文选择，内联） | 53% | 52% | 47% | ADR-C041 已判平台，**勿动** |
| LUT 重建 + 模型行解码 + per-slice 分配 | **<2%（不可见）** | <2% | <2% | **S2 前提被否定** |
| 空闲（cond_wait，16t） | 14% | 31% | 23% | 4K 444 worker 大体吃满 |

另证：产品 qp 域 DC-only 快路命中率低（dinv 调用 ≈ store 块数的
95%+）——块普遍带少量 AC，单块→批 4 的脚手架摊销空间真实存在。
4K 444 16t 异常（3.8× vs 8K 7.3×）：采样期 worker 忙碌 86%，
非饥饿型；维持"带宽/尾部失衡"假设，S6 低优先不变。

## 判定（计划预期值修订）

| 项 | 判定 | 依据 |
|---|---|---|
| S1 rans2 融合路径移植 | **GO（不变）** | sink 实测 47~53%，其中单块 dinv 43~48% + 指针/memset 开销；预期维持 +8~15% |
| S2 lazy LUT / 池化 | **REJECT（P0 关闭）** | 装配成本实测 <2%，无收益空间——省出一个原计划批次 |
| S4 编码除法消除 | **GO（升格为编码主杠杆）** | `tc_rans_put` 实测 41% band 时间，除法为其主指令 |
| S5 编码实时专项 | 靶心修订 | 第二靶 = collect_joints+代价模型（38%）；sized 搜索摊销仅 9.4%（降级）；F-cache 流量假设否定 |
| S6 16t 扩展性 | 维持低优先 | 非饥饿型，带宽/失衡结构性 |

P1 批次因此修订为：**S1（解码融合）→ S4（编码除法）**，两项均位
精确、互不依赖，可分批独立过门（系统级交错 A/B ≥+3%、回归五配置
+ golden 逐位 + 新旧 dylib 差分）。S5 第二靶（collect/cost 38%）
待 S4 落地后重剖析再定（S4 会改变该环占比）。

## 复现

```bash
export TOPOS_CODEC_LIB=native/topos_codec/build/release/topos_codec.dylib
# 编码采样：python3 tools/prof_p0.py --fixture /tmp/p0f/src4k444_10.raw \
#   --width 3840 --height 2160 --frames 24 --tier pro444 --seconds 30 & → sample <pid> 12
# 解码采样：prof_decode（perf 构建）+ TOPOS_SLICE_THREADS=16 + sample 10
# 夹具：ffmpeg -i "tests/test_videos/4K_DNxHR 444 10bit.mov" -frames:v 24 \
#   -f rawvideo -pix_fmt yuv444p10le /tmp/p0f/src4k444_10.raw（422 同理）
# 符号归因：sample 树 inclusive 聚合（sed 's/^[+:|! ]*//' + awk 计数）
```
