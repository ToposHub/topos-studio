# V 代际收纳批 0 审计（topos_consolidation_audit_2026-09-13）

日期：2026-09-13 · 状态：**批 0 完成（零代码改动，门禁绿：85 ctest + 726 pytest）**
上位计划：`topos_version_consolidation_plan_2026-09-13.md`
性质：决策材料。四张清单 + 术语澄清 + D1~D4 拍板建议。基线门禁与
保留代 golden SHA 已在批 0 提交时记录（18 个 golden bin 的 SHA256 清单见
§6）。行号为 2026-09-13 实测（修正计划文档中的漂移）。

---

## 0. 术语澄清（最重要发现——两套 "em" 编号必须区分）

计划文档混用了 cfg 选择器编号与位流 entropy_mode。实测映射
（`codec.c:314-359` cfg_to_frame_header + `frame_header.c:48-147` 版本规则）：

| cfg reserved[0]（编码选择器） | 代际 | 位流 (major, entropy_mode, cbv) | 处置 |
|---|---|---|---|
| 0 | V1 | (1, –, –) | **保留读写** |
| 1 | V2-VLC | (2, 1, 1) | **保留读写**（RDO 宿主） |
| 2 | V2-Rice | (2, 0, 0) | 退役（写 em2 / 读 (2,0,0) 组合） |
| 3 | V3 | (3, 1, 1) | 退役 |
| 4 | V4 | (4, 2, 2) | 退役 |
| 5 | V5 | (5, 3, 3) | 退役 |
| 6 | V6 | (6, 4, 4) | 退役 |
| 7 | V7-R | (7, 6, 0) | 退役 |
| 8 | V7-R2 | (7, 7, 0) | **保留读写**（产品默认） |
| 9 | V8 | (8, 8, 0) | **保留读写** |

**修正**：计划文档 "V8 em=9"、"V7-R em=7"、"V7-R2 em=8" 均为 **cfg 选择器
编号**；位流侧 V8=major 8/em 8、V7-R=em 6、V7-R2=em 7。后文批 2/3 的
"em" 一律指 **cfg 选择器编号**（与写端 API 对齐），涉及位流时显式写
( major, em ) 二元组。谓词实现：`slice_codec.c:18-49`
（tc_slice_use_vlc/c1/c2/range/rans/rans2）。

---

## 1. 清单一：写点普查（grep reserved[0] / entropy_mode / coding_selection）

### 1.1 产品可达写点（5 处，计划遗漏第 5 处）

| # | 写点 | 文件:实际行号 | 当前值/域 | 收敛后 |
|---|---|---|---|---|
| W1 | 主导出/渲染缓存三元链 | `src/shared/export/topos_encoder.py:693-698` | 嵌套链可产 cfg em {0,1,3,4,5,6,7,8}；产品实际可达恒 8（timeline_export/render_cache 均不含 entropy_mode → 缺省 'rans2'） | 合法域收缩 {v1,v2,rans2}→em{0,1,8}，退役值显式 ValueError |
| W2 | 代理编码（隐式） | `src/shared/media/proxy_generator.py:589-601` | **不设 reserved[0]**，ctypes 零初始化 → em=0(V1) | D3：显式设 8（V7-R2） |
| W3 | 视频 CLI | `native/topos_codec/src/cli/encoder_cli.c:79-88,175` | `--entropy` v1/vlc/intra/intra-range/rans/rans2/v8→em{0,1,3,6,7,8,9}，默认 0 | 收缩 v1\|vlc\|rans2\|v8 |
| W4 | **图片 CLI（计划未列）** | `native/topos_codec/src/cli/image_cli.c:417(默认8),473-481(解析),497-517(赋值),279(usage)` | `--entropy` vlc/v1/intra/intra-range/rans/rans2→em{1,0,3,6,7,8} | 同步收缩 |
| W5 | alpha 预算探测（非产物） | `src/shared/export/topos_encoder.py:1007-1013` | 固定 em=0，探测包不入 mux（注释钉死） | 不改（探测口径，alpha 体积与熵版本无关） |

native 内部：`codec.c:5680` `tc_v7_frame_encode` 内部 staging
`reserved[0]=2u` 仅作校验载体（帧头随后替换为 V7-A 元组）——批 2 收缩写域
时须一并处理（随批 4 V7-A 归档整链移除，批 2 期间保持内部路径不经过公共
域校验或载体值改用合法域）。mov.c:1797 为读侧 ABI 回读槽，非写点。

### 1.2 Python entropy_mode 面

- 解析 `topos_encoder.py:353`，合法域 `:354-359` =
  `{v1,v2,intra,intra-range,acpair,acpair_table,rans,rans2}`（8 值）；
  缺省 'rans2'（`video_encoder.py:359` dataclass 默认）。
- rdo 耦合 `:390-394`（`rdo_mode='on'` 须 `entropy_v2`）。
- **无其他 Python 写点**：`proxy_generator.py` 全文件无 entropy 字样；
  `timeline_export.py:2462-2513` encoder_kwargs 不含 entropy_mode；
  `TimelineExportConfig`(:229 起) 无 entropy 字段 → 产品导出恒 V7-R2。

### 1.3 UI/preferences 面：零暴露（钉死）

`src/ui` 不存在；`src/features`、`src/app` 零命中。唯一疑似面
`src/shared/codec/topos_profiles.py:301-308` 的 `entropy_default`/
`entropy_selectable`（含退役值）为**无消费者的惰性元数据**——批 2/4 同步
更新文案以免误导。`topos_binding.py:1359` `bitstream_major=int(info.reserved[0])`
为只读回读。

### 1.4 bench/tools 写点（测试基准，非产物）

tools/ 与 native/topos_codec/tools/ 各 bench 脚本直接设 `fc.reserved[0]`
（`bench_toos_entropy.py:75` CHAINS {0,1,7,8}、`bench_ctx_ceiling.py:119`(7)、
`bench_t16_ab.py:417`(7/8) 等约 20 处）。**处置**：退役后 cfg em∈{2..7}
会被 native 域校验拒绝——退役代际的 bench 脚本随之失效，属预期（历史数据
JSON 已落档 `tools/toos_entropy_2026-09-12.json`）；批 3 后如需复跑历史
基准用 `TOPOS_DEV=1` 测试构建。

---

## 2. 清单二：读依赖普查

### 2.1 frame_header 接受清单与规则单点

- 接受清单实际位于 `frame_header.c:575-580`（计划写的 :482-487 已漂移）：
  接受 major 1..8，major 9+ → `TC_ERR_UNSUPPORTED_VERSION`（vfail）。
- 版本规则单点 `tc_frame_header_validate` = `frame_header.c:48-147`（一致），
  函数延伸到 :389（:148 起为 profile/枚举交叉规则）。
- **退役须同时收紧**：(i) :575-580 接受清单 → major {1,2,7,8}；
  (ii) :48-147 规则块拒绝 (2, em0) 组合（V2-Rice）；(iii) 退役信息含
  "generation N retired (ADR-C0xx)"。

### 2.2 V7-B 入口（不受清单收缩影响，证据）

V7-B **不经** frame_header 通用校验：`tc_v7b_packet_is`
（`v7_scalable.c:27-39`）= major 7 + 帧头后 "TPLD" magic + 目录 minor ∈
{1..4}（REFERENCE=1/QUANTIZED=2/TRANSFORM_EXPERIMENTAL=3/PYRAMID=4，
`v7_scalable.h:15-23`）。`tc_frame_decode` 探测链第一个探测即 V7-B
（`codec.c:8486-8498`）。容器侧 `mov.c:349-372` 同构分流。**结论：major
清单收缩不动 V7-B 一根毫毛**（计划 "minor-2~4" 应为 minor-1~4）。

### 2.3 dec_slice_job 逐分支宿主（实际 :7811-8089；计划 :7633-7832 漂移）

| 行号 | 分支 | 宿主 | 退役后处置 |
|---|---|---|---|
| :7828-7832 | slice CRC32 校验 | 全 legacy 代际 | 保留 |
| :7833-7884 | alpha plane3 流（Rice 行池化） | 带 alpha 的全部 legacy 代际 | 保留（V1/V2-VLC/V7-R2 用） |
| :7924-7943 | V3/V6 intra scratch 池 | V3、V6 | 移除 |
| :7944-7961 | 非 V3/V6 颜色 DC 双行池 | V1/V2/V4/V5/V7-R/V7-R2 | 保留 |
| :7972-8002 | qcap 差分机（dev 工具，`s_dev_qcap_sparse` 默认 0 :6207） | dev-only；内含 V7-R2 稀疏发射 :7980-7993 | 保留（V7-R2 腿），清 V7-R 腿 |
| :8003-8006 | V3/V6 intra 解码 | V3、V6 | 移除 |
| :8007-8009 | direct-scan（默认开启 :1054） | 跨代际生产融合入口；`slice_codec.c:1021-1057` 内部再分派 | 清 C1/C2/V7-R 分派（:1040-1045 附近），保留 V1/V2-VLC/V7-R2 |
| :8010-8014 | generic sink（dev 回退） | 同上 | 保留结构，清退役分支 |
| :8069-8088 | conceal / slice_status | 全部 | 保留 |

注：生产 V7-R2 走 `tc_color_rans2_decode_core`（:1051-1055 附近谓词），
dev qcap 稀疏分支非生产路径。

### 2.4 探测链与错误语义现状

`tc_frame_decode`（`codec.c:8470-8537`）：①:8486 V7-B → ②:8499 V7-A
（major7 ∧ data[45]==5，`codec.c:1060-1066`）→ ③:8503 V8
（`packet.c:82-87`）→ ④:8521 兜底 `tc_packet_parse_structure`
（`packet.c:13-66`：major 9+ → UNSUPPORTED_VERSION；major8 走错入口 →
UNSUPPORTED_VERSION "use tc_packet_scan_v8"；尺寸不符 → MALFORMED）。
**major 3/4/5/6 与 (2,0) 目前被正常接受解码**——退役 = 收紧 ②④ + 规则块。

### 2.5 auto2k/reduced 与 V7-A 解耦（证据钉死）

- `tc_decode_scale_dimensions` :8596-8618、`tc_decode_scale_coefficient_limit`
  :8620-8630：零代际知识。
- `tc_decode_request_resolve` AUTO_2K case **:8738-8769**：只读
  `fh->visible_width/height`。
- `tc_frame_decode_reduced` :8929-9013：V7-B 分支 :8946-8967、V7-A 分支
  :8968-8983（early-return `tc_v7_frame_decode_scaled`）、**通用尾
  :8984-9012 = parse_structure → `tc_decode_slices_scaled_limit`**。
  通用链路对 tc_v7_* 零引用；scaled 语义由 `t->scaled` +
  `coefficient_limit`（:7796-7797, :7900-7901）承载，为共享缩放路径。
- 产品侧：`topos_source.py:1047-1071` 只发 REDUCED/AUTO_2K request；
  binding `packet_base_into`（`topos_binding.py:1483-1489`）把 V7-B 预览
  降为 base packet FULL 解码。**AUTO_2K 对退役代际文件的可用性不受 V7-A
  归档影响。**

### 2.6 V7-A band decode 消费者定界（D1 材料）

全部引用：实现链 `band_codec.{h,c}`、`v7_band_decode.{h,c}`、
`v7_frame_codec.{h,c}`（构建注册 `CMakeLists.txt:73,194`）；调用点仅
codec.c 探测四入口（:8500/:8557/:8878/:8977）+ mov.c:361-371 + 单测 5 文件。
能力位 `TC_CODEC_CAP_V7A_BAND_DECODE`：`topos_codec.h:86`、`version.c:55`、
`docs/capability_manifest.json:24`、binding 镜像 `topos_binding.py:65`
与属性 :547-548、测试断言 `test_topos_binding.py:103`、
`test_capability_manifest.py:163`。

**结论：无任何 binding/ctypes 暴露；产品面（timeline_preview_renderer、
auto2k、reduced）零真实调用。V7-A 的唯一可达性是"喂进 tc_frame_decode* 的
V7-A 包被自动路由"。** → **D1 拍板：归档**（退役规程同批 3：删探测分支 +
mov 判据 + 专用入口三文件 + 能力位四方同步）。

---

## 3. 清单三：测试宿主地图（85 ctest + fuzz corpus + Python）

### 3.1 专测退役代际（13 项）

| ctest | 宿主 | 证据 |
|---|---|---|
| unit_intra | V3 主 + V6 腿(:216) + V4 腿(:432) + V2-VLC 基线腿(:393) | test_intra.c |
| unit_range | V6 range coder 原语 | 全文件 |
| unit_rans | V7-R 原语差分 | 全文件 |
| unit_rans_slice | V7-R 集成（sel=7） | :3-5,:37 |
| unit_ctx_ceiling | V7-R（sel=7） | :32 |
| unit_band_codec / unit_band_tokens / unit_v7_band_decode / unit_v7_frame_codec / unit_layer_directory | V7-A | 全文件 / :76,:88 / :19-20,:35 / :27-28,:43 |
| unit_v7_frame_encoder | V7-A 桥(:82) + V2-Rice parity(:147) | |
| conformance_codec_intra | V3（golden_codec_v3_intra.bin 16 记录） | golden_codec.c:131-140 |
| fuzz_intra_replay | V3 硬宿主（:34 断言 major==3） | intra_replay.c |

### 3.2 含退役腿（8 项）

unit_stage9（em2 parity 腿 :527-546）、unit_codec（em0..6 校验门
:1933-1942，负测试性质）、unit_mov（coding 4/5/6 腿 :2145,:2204）、
unit_frame_header（em2 负测试 :455-500）、unit_c1_scratch（V7-R + V7-A 腿
:8-9,:34）、fuzz_encode_replay（em=rnd(7) 含 2..6，fuzz_encode.c:60）、
Python `test_topos_binding.py::test_c1_acpair_roundtrip_and_major_header`
(em4 :238)、`test_c2_pair_table_roundtrip_and_major_header`(em5 :258)、
`test_codec_capabilities_are_truthful`（V7-A 位 :103）。

### 3.3 fuzz corpus

- `tests/fuzz/seed_corpus_bitstream/`：256 文件/13 种子，**全部 V1/V2-VLC
  （保留代际），零退役种子**——无需归档。
- `tests/fuzz/seed_corpus/`：4 文件 infra 种子，代际无关——无需归档。
- fuzz_v8：无 corpus（-gen LCG）。
- **golden_codec_v3_intra.bin** = fuzz_intra_replay 的事实 corpus（V3）。
- `corpus_retired/` 不存在，需新建；批 3 实际归档对象仅 v3_intra golden。

### 3.4 批 1 迁移清单（清单 A）

| 测试 | 现宿主 | 处置 |
|---|---|---|
| conformance_codec_intra | V3 | **重建等价覆盖**：kCasesIntra 8-case 网格（73×39/35×21/16×16/1×1 × {10,12}bit × {422,444,GBR} × qp{0,18,24,31,63} × qm{0,1} × slice_rows{1,2} × 内容五类）原样平移到 V8（cfg em9），生成 golden_codec_v8_intra.bin；manifest（test_vector_manifest.json:414-423）同步 |
| fuzz_intra_replay | V3 | CRC 修补变异类移植为 V8 intra replay（gate major==8），golden 换新 |
| unit_intra | V3 主/V6/V4 腿 | V2-VLC 基线腿保留；quality_guard/sized 腿改 em9 + 补 em 拒绝负测；V3/V6 主体随批 3 归档 |
| unit_range | V6 | 归档（range 熵随 V6 退役，无保留宿主） |
| unit_rans | V7-R | 与 unit_rans_slice 一并归档（等价覆盖由 unit_rans2_slice 同构 5 块承接：选择域/差分/确定性/体积/畸形） |
| unit_rans_slice | V7-R | 归档 |
| unit_ctx_ceiling | V7-R | 迁 V7-R2（sel→8，ctx 钩子同源） |
| unit_band_* ×4 + unit_v7_frame_codec + unit_layer_directory | V7-A | 批 4 随 D1=归档移除 |
| unit_v7_frame_encoder | V7-A + V2-Rice parity | 批 4 移除；parity 参考腿（:147）改 V2-VLC 移入 test_slice_codec 范畴或删除（unit_stage9 已有 parity 语义） |
| unit_c1_scratch | V7-R/V7-A 腿 | 删退役腿，保留 sel8 腿 |
| unit_stage9 | em2 parity 腿 | em 循环改 {0,1,8}，payload 一致断言语义保留 |
| unit_mov 版本同步测试 | coding 4/5/6 | 循环域改保留集合 {1,8,9} |
| unit_frame_header | em2 负测试 | 批 3 语义反转为 UNSUPPORTED_VERSION |
| fuzz_encode_replay | em rnd 0..6 | rnd 域改保留集合 {0,1,8,9} |
| test_topos_binding c1/c2 两用例 | em4/em5 | 改断言退役拒绝（批 2 写端语义） |
| test_topos_binding capabilities | V7-A 位 | 批 4 翻转期望 |

### 3.5 批 3 归档清单（清单 B）

仅 golden_codec_v3_intra.bin → `tests/fuzz/corpus_retired/`（新建；
TOPOS_DEV 回放用）。seed corpus 两目录零退役种子，不动。

---

## 4. 清单四：存量文件面

### 4.1 仓库内（实测扫描）

- 全仓 `*.topos`/`*.tmov`/`*.tpim`：**零文件**。
- `bench_out/`（git 部分跟踪的 bench 产物）：58 个 mov 全部扫 TPIC 帧头
  (major, entropy) —— **(1,0)=408 帧、(2,1)=1396 帧，全部保留代际；
  零 (2,0) V2-Rice、零 major 3..8**。明细：codec_bench/ 与 9 月初 dbg_*
  为 major2-em1（M9 后 VLC）；9/2-9/3 目录为 major1；无 9/6~9/12 实验代
  mov 产物入库。
- `bench_out/` JSON/log/txt 为基准数据档案（含退役代际的历史测量记录），
  非可解码位流，不受退役影响。
- `samples/`（DNG）、`hdri/`、`luts/` 非 topos 位流。

### 4.2 用户本机导出目录（仓库外）

不可扫描（超出仓库范围）。缓解已内建：D2 `TOPOS_DEV=1` 回放 + 退役错误
信息含 ADR 指引 + 旧文件可再生（实验代产物无生产价值，判定见计划 §1.2）。

**结论：退役不影响仓库内任何现存可解码文件；仓库外存量由 D2 兜底。**

---

## 5. 拍板建议（依据上文证据，采用计划建议默认）

| # | 拍板 | 结论 | 依据 |
|---|---|---|---|
| D1 | V7-A 处置 | **归档**（专用入口+band decode 链+能力位按批 3 规程退役） | §2.6：零产品消费者、零 binding 暴露；auto2k/reduced 解耦证据 §2.5 |
| D2 | 退役代读端语义 | 生产 `UNSUPPORTED_VERSION`（含 "generation N retired (ADR-C0xx)"）+ 测试构建 `TOPOS_DEV=1` 保留回放 | 计划建议默认；§4 存量面佐证风险低 |
| D3 | proxy 写入口径 | **统一 V7-R2**：`proxy_generator.py` 显式 `reserved[0]=8`；前后体积 A/B 落档 | 计划建议默认；W2 现状为隐式 em0 |
| D4 | ADR 合并 | V8 生产切换拍板记录并入 ADR-C0xx（切换条件仍独立判定） | 计划建议默认 |

执行序修正（相对计划）：批 2 收缩 native 写域时，`codec.c:5680` V7-A 内部
staging 载体（em=2）须豁免或改道——该路径属批 4 归档对象，批 2 采取
"公共入口域校验收紧、内部桥不受公共校验约束"的最小切口，批 4 整链移除。

---

## 5b. 批 2 执行结果补录（2026-09-13）

- 写域收缩落地：`codec.c` cfg 校验 em∈{2..7} → INVALID_ARGUMENT（信息含
  "retired, V consolidation 2026-09-13, see ADR-C0xx"，写域 {0,1,8,9}）；
  V3/V4/V5/V6 的 reserved[1..2] 专用规则、V6 packet_bound 4096B 特例、
  sized 穷举探针特例、cfg_to_frame_header em2..7 映射分支随写端退役移除。
- V7-A 内部桥载体：`tc_v7_frame_encode` staging 由 em=2 改 em=0（V1 与
  V2-Rice 编码路径逐字节一致，stage9 parity 锚），桥在批 4 归档前保持可用。
- bd≥13 熵域门：`reserved[0] < 7` → `∈{8,9}`（em7 V7-R 写端退役后 bd16
  写面只剩 rans2/v8；读面能力查询不受影响）。
- 双 CLI 收缩：encoder_cli `--entropy v1|vlc|rans2|v8`（retired 值显式
  error 行）；image_cli `--entropy vlc|v1|rans2`（em7/6/3 分支与 usage
  说明同步）。
- Python 面：`topos_encoder.py` 合法域 {v1,v2,rans2}，退役值构造期
  ValueError（信息含退役代际与文档指引，禁止静默回落）；三元链 :693 收缩
  三态；proxy_generator 显式 `reserved[0]=8`；`topos_profiles.py`
  entropy_selectable 收缩 ["rans2","v2","v1"]；video_encoder.py 注释同步。
- 代理口径 A/B（D3 风险登记项，合成 MIXED 1920×1080 422 10bit qm1
  slice_rows=32）：qp24 V1=5,710,190 B vs V7-R2=4,615,606 B（**−19.2%**）；
  qp28 V1=5,292,791 B vs V7-R2=4,214,602 B（**−20.4%**）——代理更小，属
  改善；旧代理可再生，缓存基准漂移方向为占用下降。
- 退役写路径拒绝钉死：test_codec（validate 域 em2..7 + 10..12 负测）、
  test_rans2_slice 选择域（承接 test_rans_slice，语义反转）、Python
  `test_retired_entropy_write_rejected`（em2..7 参数化 6 例）、
  `test_retired_entropy_modes_rejected_at_construction`（5 退役值 + 域外值）。
- 宿主迁移收尾：test_rans2_slice 差分/体积锚基线由 em7 改 V1/V2-VLC
  （体积锚实测：纹理 qp33 rans2 31,774 B < VLC 35,894 B；平纹 qp88
  rans2−VLC = 903 B ≤ 9×231+64 界）；test_v7_frame_encoder 参考腿
  em2→em0；test_rans_slice.c 归档（ctest 85→84，−1 为随代际退役的测试
  文件；其选择域/差分/体积/畸形覆盖由 test_rans2_slice 承接，Python 侧
  +6 参数化净增）。
- 门禁：ctest 84/84、pytest 730 passed、保留代 golden SHA 零变化。

## 5c. 批 3 执行结果补录（2026-09-13）

- 读端闸门（frame_header.c 单点）：decode 接受清单对 major 3..6 前置拦截；
  validate 规则块兜底 major 3..6 + (2, em0)=V2-Rice + (7, em6)=V7-R →
  `TC_ERR_UNSUPPORTED_VERSION`，信息含
  "generation N (… ) retired (V consolidation 2026-09-13, see ADR-C0xx)"。
  所有 legacy 解码路径（tc_packet_scan / parse_structure / tc_frame_decode
  / decode_request / decode_reduced / mov 容器）都经此单点，无需散点改动。
- **内部规则域复用豁免**：`validate_v7a_common_fields` 把 V7-A 元数据伪装成
  (6,4) 复用 V6 域规则——重构出 `validate_impl(fh, allow_retired)`，公共
  validate 恒 = replay 旗标，V7-A 规则载体传 allow_retired=1（代际裁决归
  V7-A 专用合同）。这是批 3 唯一的隐性消费者（测试插桩定位）。
- **TOPOS_DEV_REPLAY 双重门**（D2）：CMake option（默认 OFF=生产零回放面，
  编译于 topos_codec 目标）；ON 时还需运行期 `TOPOS_DEV=1`。三态实测：
  默认构建/回放构建无 env → 归档 V3 golden 拒绝（UNSUPPORTED_VERSION）；
  回放构建 + TOPOS_DEV=1 → 真实 V3 归档流成功解码（考古可用）。
- 拒绝钉死：新增 `test_retired_read.c`（patch 保留代合法包构造退役包 +
  corpus_retired 真实 V3 golden 16 记录全拒绝 + 保留三代对照腿 +
  validate 层退役组合断言）；`test_frame_header` V2-Rice patch 断言随
  退役反转；Python `test_retired_generation_read_rejected`（patch+CRC 重算
  fixture，错误信息含 retired/ADR-C0xx）。
- inspect/decoder CLI：退役文件经闸门打印完整 retired 诊断行（含回放
  指引），满足"诊断工具能解释为什么打不开"。
- 门禁：ctest 85/85（+unit_retired_read）、pytest 731（+1）、保留代
  golden SHA 零变化。插桩临时代码已全部移除。
- 环境事件记录：批 3 期间宿主 exec 路径曾整体楔死（新进程内核态 U 不可
  杀，shell 内建存活），约 40 分钟后自愈；楔死前的 2 项测试失败即本节
  所述内部复用问题，修复后全绿。

## 5d. 批 4 执行结果补录（2026-09-13）

- **D1 执行 = V7-A 归档**（审计 §2.6 证据：零产品消费者、零 binding 暴露）：
  - 探测链四入口（codec.c tc_frame_decode / decode_scaled / decode_request /
    decode_reduced）与 mov.c V7-A 目录分支：非回放构建 →
    `UNSUPPORTED_VERSION` "generation 7 (V7-A band decode) retired …"。
    **V7-B 保留不受影响**：mov 分支重排为 V7-B（tc_v7b_packet_is）优先，
    仅非 V7-B 的 em5 包（= V7-A 专用语法）被拒。
  - 写端：`tc_v7_frame_encode` 公共入口拒绝（INVALID_ARGUMENT，仅
    TOPOS_DEV_REPLAY 构建 + TOPOS_DEV=1 可达）。
  - 代码保留策略：band 语法源文件（band_codec/v7_band_decode/v7_frame_codec
    等）不入删除——随 D2 回放面保留在回放构建中；生产构建行为零可达。
- **能力位下线（manifest 先行）**：capability_manifest.json
  `v7a_band_decode: false` + status_note 更新 → version.c flags 移除该位 →
  topos_codec.h 常量保留（ABI 兼容）注释退役 → binding 镜像注释同步 →
  test_abi 期望 flags 31、test_topos_binding `assert not v7a_band_decode`、
  manifest 门测试自动钉（false==false）。
- 测试面：V7-A 专属 6 项（band_codec/band_tokens/v7_band_decode/
  v7_frame_codec/v7_frame_encoder/layer_directory）改为**仅 TOPOS_DEV_REPLAY
  构建编译注册**，main 自带 setenv("TOPOS_DEV","1")；回放构建实测 6 项全绿。
  c1_scratch V7-A 腿移除；test_retired_read 新增 V7-A 探测拒绝腿
  （patch em5 判据 + 还原自检）。
- 门禁：生产构建 ctest 79/79（85−6 回放专属）、pytest 731、保留代
  golden SHA 零变化；回放构建 7 项测试全绿 + 考古回放双向验证。

## 5e. 批 5 执行结果补录（2026-09-13）

- **ADR-C046《位流代际生命周期与退役规程》入库**
  （`native/topos_codec/docs/ADR-C046-bitstream-generation-lifecycle.md`）：
  代际终态表（cfg em ↔ 位流 major/em 双编号冻结）、六步退役规程、版本预算
  纪律（新 major 入产窗口登记 / 实验轴 dev 旗标隔离 / 进新 major 必须登记
  前序死轴退役评审点 / em·major 编号永久封存）、保留代保护条款、
  validate_impl 内部复用豁免实施注记。
- **D4 合并拍板**：V8 生产切换记录并入 ADR-C046 §7（三重开条件仍以
  topos_v8_production_switch_2026-09-12.md 为准，独立判定语义不变）。
- 交叉引用：TRAW 计划（V7-B 保留代条款）与 V9 计划（§4 版本预算纪律）
  各加一行指向 ADR-C046；历史 a/rd/C 系列实验报告未改（档案性质）。
- SDK/CLI 文档：codec README 写域段重写（V3/V4/V5 实验描述 → 退役说明 +
  合法域 {0,1,8,9}）；docs/image/SDK.md `--entropy vlc|v1|rans2` + 默认档
  说明对齐（rans2 默认 + intra/rans 退役）。
- **i18n 决策**：退役信息不做 10 locale 翻译——证据：entropy_mode 产品
  UI 零暴露（审计 §1.3），退役错误仅两类读者：开发者（构造期
  ValueError，中文 + 文档指引）与诊断工具用户（native 错误行自带
  "generation N retired (… ADR-C0xx)" 诊断语义 + 回放指引）。翻译该诊断
  串（含 ADR 编号）反增维护面而无 UI 命中点；如未来导出 UI 需要本地化
  退役提示，按 topos_profiles capability 元数据扩展（该处已同步
  selectable 域）。
- 一致性 grep：活跃代码零 retired-em 残留写点；ADR-C046 被
  TRAW/V9/image-SDK/本审计四方引用；计划文档状态行更新为结项。

## 6. 基线记录（2026-09-13 批 0 提交时点）

- 门禁：cmake build release ✓；ctest 85/85 ✓（62.8s）；
  pytest tests/native tests/shared = 726 passed, 10 skipped ✓（81.5s）。
- 保留代 golden SHA256 基线（18 bin）：见提交 `refactor(v-retire): 批 0`
  的提交信息附表，及 `/tmp/golden_sha_baseline.txt`（会话内）。
  关键项：golden_codec_v1.bin e7178b45…、golden_codec_v2_vlc.bin a6655a40…、
  golden_v8_v1.bin 596c2e55…、golden_mov_v1.bin 8e7e9010…。

## 7. 结项后整体复查（2026-09-13，双 Explore 清扫 + 门禁扩面）

- **门禁盲区修正**：批 1~5 的三命令门只跑 `tests/native tests/shared`，
  漏 `tests/media`。扩面后暴露两处隐藏缺陷（均修复）：
  1. **TestAdrRollup 红测**：批 5 入库 ADR-C046 未登记 ADR-INDEX.md，
     违反索引维护规则（`test_index_lists_all_adrs`）。补 C046 当前行，
     并同步修正 C029（→已归档）、C034（→代际已退役）、C035（→被 C038
     取代，文件头补 `superseded-by:` 元数据）、C036（"默认仍 rans"过时）。
  2. **批 2 新测试从未被执行**：`test_retired_entropy_modes_rejected_
     at_construction` 把 `''` 当域外值断言抛错——实际 `''`/None 属"未设置
     → 回落默认 rans2"（与 aq_mode/rdo_mode 同口径，有意设计）。修正
     期望为显式断言回落语义；退役值五元组 + 大小写归一（'RANS'）拒绝
     行为本就正确。
- **工具链残留**：`tools/bench_toos_entropy.py` 仍含 `("v7_rans", 7)`
  退役链（运行即触发绑定层 retired 拒绝）——删除该链并同步 docstring；
  `bench_codecs_matrix.py` "产品编码器同口径"过时注释修正。
- **native 死代码/文档收口**：image_cli.c 熵名打印的 6/3/7 不可达分支
  删除；fuzz_encode.c V3 closed-loop 死逻辑（域内恒 false）与过期注释
  清除；test_v8_format.c §"NOT_IMPLEMENTED" 段首注释对齐批 2 真实现；
  test_vector_manifest.json：v3_intra generator 修正为"已冻结不可再生"
  （写端退役无 replay 豁免，再生需回溯收纳前 commit）、checks 改指
  test_retired_read、conventions.golden_output 路径对齐实际位置。
- **复核结论（未改）**：C 写读门禁无非 replay 泄漏（V7-A 完整帧头 (7,5,5,2)
  自出生即过不了公共 validate，非收纳引入）；V7-B 路由隔离完好；manifest
  记录/文件实测一致；产品面（UI/预设/QSettings/i18n）零退役值入口。
- 复查门禁：cmake build release ✓；ctest 79/79 ✓；
  pytest tests/native tests/shared tests/media = **3558 passed, 65 skipped** ✓；
  golden 18+1 bin SHA256 零变化 ✓（含 corpus_retired ba143f1e…、
  v7r2_intra 6146ef0b…）。
